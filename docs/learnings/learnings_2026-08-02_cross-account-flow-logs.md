# Learnings — 2026-08-02 cross-account-flow-logs

## 1. What this change delivered
Fixes for the `--flow-logs` path triggered by a real cross-account run (VPCs in account X, flow-log
S3 objects in account Y), plus precision/completeness on the fetch:

- **Config-vs-storage account split.** `describe-flow-logs`, the CloudTrail history
  (`collect_ip_allocation_events`, `collect_historical_enis`) and the CloudWatch reader now run in
  the **network** account; only the S3 object I/O (`list-objects-v2` + `get-object`) runs in the
  **archive** account. `describe-flow-logs` is queried **once** (network account) and that one config
  drives both the graph nodes and the record fetch — the reader no longer re-runs it under a second
  profile. Driven by `collect_all` → `_collect_flow_logs_role` (the `flow_logs` role is special-cased
  out of the generic `ROLE_COLLECTORS` loop).
- **Archive-account auto-resolution.** No hand-binding needed: `_ArchiveResolver` tries the
  primary/network profile's `list-objects-v2` first (that existing call *is* the probe), and on
  `AccessDenied`/`Forbidden` falls back through the configured `account_id → profile` accounts, first
  success wins; exhausted → `FlowLogFetchError` naming the bucket + profiles tried. An explicit
  `[targets.<t>.roles].flow_logs` binding (or `--profile`) skips the trial.
- **VPC coverage check** (`check_vpc_coverage`, run **before** any S3 I/O): if the discovered flow
  logs reference **none** of the discovered `vpc-`/`subnet-`/`eni-` ids → `FlowLogCoverageError` (the
  cross-account symptom); uncovered VPCs → stderr warning. Surfaced in `meta.flow_log_coverage`.
- **Precision + completeness.** The S3 download is filtered to objects whose embedded `fl-…` id maps
  to a discovered VPC (secondary guard: the key's `AWSLogs/<acct>/` matches the network account).
  Per-VPC object/record counts are tracked; a covered-but-empty VPC is warned, and if *every* covered
  VPC is empty the run raises `FlowLogCoverageError`.
- Files: `aws/collectors.py` (the bulk), `config.py` (`archive_fallback_candidates`), `cli.py`
  (`_build_archive_access`, `_flow_logs_explicitly_bound`, `FlowLogCoverageError` handling),
  `README.md`, `docs/02_architecture.md §5.7`+§11.7, new `tests/test_flowlog_cross_account.py`.

## 2. Interface contract for the next change
- **`collectors.ArchiveAccess(primary, explicit=None, candidates=())`** — the S3 resolution strategy.
  `primary` = network account (tried first); `explicit` set ⇒ fast path (no trial); `candidates` =
  ordered fallback `ResolvedAccount`s. Built by the CLI (`_build_archive_access`) and passed to
  `collect_all(..., archive_access=...)`. When omitted (`--from-cache`, simple callers), the S3 read
  uses the `flow_logs` role's own account directly as an explicit binding — so single-account and
  cache paths are unchanged.
- **`collect_all(resolved, *, roles, cache_dir=None, archive_access=None)`** — new kwarg. The
  `flow_logs` role is handled by `_collect_flow_logs_role`, not the generic loop.
- **`check_vpc_coverage(flow_logs, vpcs, subnets, enis) -> FlowLogCoverage`** — pure, raises
  `FlowLogCoverageError` on the all-foreign case. `FlowLogCoverage` carries `covered_vpcs`,
  `uncovered_vpcs`, `in_scope_fl_ids`, `fl_to_vpc`, and `.meta(per_vpc)`.
- **`fetch_flow_log_records(flow_logs, network, archive, *, coverage=None, network_account_id=None)
  -> (records, per_vpc, resolved_archive)`** — the two-account record fetch. Uses normalized
  `flow_logs` dicts (from `collect_flow_logs`).
- **New exception `FlowLogCoverageError`** (caught in `cli.main` → exit 1), distinct from
  `FlowLogFetchError`.
- **`_normalize_flow_log` now keeps `LogFormat`** so the one shared config source is enough for the
  CloudWatch reader's field-index derivation (it previously read `LogFormat` off a *raw*
  re-`describe`; with the query done once we must carry the field through normalization).
- **`_run_unit(..., access_denied_signals=False)`** — when True (the S3 list probe), an
  `AccessDenied`/`Forbidden` raises the internal `_S3AccessDeniedError` (drives the fallback) instead
  of `FlowLogFetchError`. `SignatureDoesNotMatch` still aborts. `_read_s3_records`/
  `_read_cloudwatch_records` gained keyword-only params (`resolver`, `allowed_fl_ids`, `fl_to_vpc`,
  `network_account_id`, `per_vpc`) that all default to the legacy single-account behaviour — the
  `FLOW_LOG_READERS` registry + `collect_flow_log_records` (still used by the older tests) are
  untouched.

## 3. Decisions & rationale
- **Two-account threading via a driver special-case (option A), not registry surgery.** The prompt
  offered (a) an explicit two-account fetch driven outside `ROLE_COLLECTORS`, or (b) reassigning the
  registry so config/CloudTrail/CloudWatch live under a network-resolved role. (b) is awkward because
  the collectors are opt-in (`--flow-logs`); statically moving them into `ROLE_COLLECTORS["network"]`
  would run them unconditionally. (a) keeps the `collector(profile, region)` contract intact for
  every untouched collector, keeps `meta.accounts` meaningful, and localises the split to
  `_collect_flow_logs_role`. The registry entry stays as documentation of the role's outputs.
- **The auto-resolution can't catch the config bug — the coverage check must.** The config-in-wrong-
  account mistake is a *silent success*: `describe-flow-logs` in account Y returns Y's flow logs with
  no error. So a permission-triggered fallback (which only fires on `AccessDenied`) never sees it.
  That's why coverage (§C) is a separate, mandatory check run **before** the S3 fetch — and why it
  fails on "no discovered VPC covered" rather than on any auth signal.
- **Bucket owner is not derivable from config.** The `LogDestination` ARN (`arn:aws:s3:::bucket/...`)
  has no account; the object key's `AWSLogs/<acctId>/` segment is the **source** account (the VPC
  account), not the bucket owner; `DeliverLogsPermissionArn` is a delivery role in the source
  account. So resolution is "try the configured profiles", never "compute the owner". Candidate
  order is just sorted-by-alias (I skipped the optional source-account-hint prioritization: the hints
  name the source account, not the owner, and the key hint is only available post-listing — not worth
  the complexity or the false confidence).
- **`--expected-bucket-owner` is a guard, not a discovery tool.** It returns 403 on **both**
  owner-mismatch and no-access (indistinguishable from outside), so it can't select the working
  profile. Worse, pairing `--expected-bucket-owner <A>` with account A's own profile *rejects* the
  common valid case (source account A has cross-account read but is **not** the bucket owner — a plain
  read succeeds, the expected-owner check 403s). So it's never used for discovery. It's only viable
  as post-resolution defense-in-depth using the *resolved* account id; I left it out entirely to
  avoid breaking the cross-account-read case, and a test asserts no owner-probe is issued.
- **Precision by `fl-…` id in the object filename.** Every VPC flow-log S3 object embeds its flow-log
  id (`..._vpcflowlogs_<region>_fl-<hex>_...`). Filtering listed keys to the in-scope `fl-…` set
  (built from the network-account config) before downloading is what stops a shared/central bucket
  from dragging in other VPCs'/accounts' objects. NOTE: the extraction regex is `fl-[0-9a-f]+` —
  real flow-log ids are lowercase hex; **test fixtures must use hex ids** (an id like `fl-s3...`
  won't round-trip through the key extractor). This bit me while writing the tests.
- **Completeness is fetch-side; NODATA is reader-side.** "Covered VPC yielded zero *objects*" (wrong
  window/prefix/region/account) is distinct from "objects exist but are all NODATA" (idle VPC — the
  existing size-aware probe handles that). Per-VPC accounting counts CloudWatch events too, so a VPC
  logging only to CloudWatch isn't falsely flagged empty.
- **CloudWatch-in-network is an assumption.** The CloudWatch log group is assumed to live with the
  VPCs (network account). A future cross-account-CloudWatch setup would need its own resolution;
  noted here and in §5.7.

## 4. Deviations from the plan
- Source-account-hint candidate prioritization: **not implemented** (see §3 — hints name the source,
  not the owner; low value, added false confidence). Candidate order is sorted-by-alias.
- `--expected-bucket-owner` guard: **not added** (see §3). Skipping satisfies the DoD ("if at all").

## 5. Gotchas & surprises
- `_normalize_flow_log` dropped `LogFormat` before this change; the legacy single-account
  `collect_flow_log_records` re-ran `describe-flow-logs` **raw** and read `LogFormat` off that. Once
  the query is done once and normalized, the field must be carried through normalization or custom
  CloudWatch formats silently fall back to the default v2 layout.
- Ordering: **destination-type validation runs before the coverage check.** A `kinesis-data-firehose`
  flow log with a foreign/empty `ResourceId` would otherwise hard-fail on coverage first;
  `_collect_flow_logs_role` calls `_group_flow_logs_by_dest` up front so the unsupported-destination
  error still wins (kept `test_unsupported_flow_log_destination_exits_cleanly` green).
- S3 `AccessDenied` only becomes a fallback signal on the **list** probe (`access_denied_signals`);
  a per-object `get` denial after the account is resolved still aborts (systemic) — a resolved
  account that can list but not get is a genuine misconfig, and the legacy
  `test_s3_systemic_access_denied_aborts` (which denies on `get`, `archive=None`) still passes.
- `--from-cache` has no live accounts: `collect_all` gets no `archive_access`, so the S3 read uses
  the (profile=None) role account as an explicit binding — one source, no fallback, no crash. Coverage
  still computes from the cached vpcs/subnets/enis and lands in `meta.flow_log_coverage`.

## 6. Known gaps / TODO
- Cross-account **CloudWatch** destinations aren't handled (assumed network-account).
- Candidate prioritization by object-key/`DeliverLogsPermissionArn` hints is a possible future
  refinement (currently sorted-by-alias).
- `--expected-bucket-owner` post-resolution guard could be added later using the resolved account id
  (only if it doesn't break the cross-account-read case).

## 7. How to verify
```bash
pip install -e '.[dev]'
pytest -q                                   # 409 tests, offline
ruff check . && ruff format --check .

# The cross-account behaviour is in tests/test_flowlog_cross_account.py:
#  - account split (config→network, S3→archive), config-queried-once, single-account no-regression
#  - auto-fallback success / exhausted, explicit-binding-skips-trial, no expected-bucket-owner probe
#  - coverage hard-fail (before any download) / partial-warn
#  - precision (only in-scope fl-… downloaded), completeness (empty-VPC warn / all-empty error)
# --from-cache coverage meta: tests/test_cli.py::test_from_cache_flow_logs_records_coverage_meta
```
