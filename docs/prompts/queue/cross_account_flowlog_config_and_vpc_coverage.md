## CHANGE REQUEST — Cross-account flow logs: discover config in the VPC account, auto-resolve the archive account for S3, and validate VPC coverage

**What I want**
Two related fixes to the `--flow-logs` path, both triggered by a real cross-account run:

1. **Discover flow-log *configuration* in the VPC (network) account, not the log-archive account.**
   Today the whole `flow_logs` role — including `aws ec2 describe-flow-logs` — runs under the
   `flow_logs` account's profile. But a VPC Flow Log's *configuration* is an EC2 resource that lives
   in the **same account as the VPCs it's attached to** (the `network` account). Only the delivered
   log **objects** live in the archive account (the S3 bucket, often a separate log-archive account).
   So when VPCs are in account **X** and their flow logs are delivered to a bucket in account **Y**,
   the current code runs `describe-flow-logs` against **Y** and finds Y's flow logs — which have
   nothing to do with X's VPCs. The tool then downloads objects unrelated to the VPCs it mapped.
   Correct behaviour: run `describe-flow-logs` (and the CloudTrail history + CloudWatch reads) in the
   **VPC/network account**, and use the **flow-log-archive account** only for the S3 object I/O
   (`s3api list-objects-v2` + `get-object`).

2. **Validate that the flow logs actually cover the VPCs we discovered.** After mapping N VPCs, the
   tool must reconcile them against the flow-log configs it found: fail loudly (or clearly warn) when
   the flow logs reference **none** of the discovered VPCs/subnets/ENIs (the exact symptom of the
   cross-account mistake), and warn about discovered VPCs that have **no** flow log configured. In my
   run there were **4 VPCs**, and the flow logs being downloaded had nothing to do with any of them —
   the tool should have caught that immediately instead of fetching thousands of unrelated objects.

3. **Auto-resolve the archive account for the S3 read (don't force an explicit binding).** By default,
   attempt the S3 object I/O with the **primary/network** profile first (in many centralized setups
   the source account is granted read on the bucket). Only on a permission failure
   (`AccessDenied`/`Forbidden`) fall back: resolve an alternate profile from the config's
   `account_id → profile` map and retry; if no configured profile can read the bucket, error out with
   a clear message. An explicit `flow_logs` role binding, when present, is used directly as a fast
   path (skip the trial). **Scope note:** this auto-fallback is **S3-access-only** — the config bug
   (#1) is a *silent success* (`describe-flow-logs` returns the wrong account's logs with no error),
   so a permission-triggered fallback cannot catch it; the coverage check (#2) is what catches that.
   And note the bucket **owner** account is *not* derivable from the config: the `LogDestination` ARN
   carries no account, the object key's `AWSLogs/<acctId>/` segment is the **source** account (not the
   bucket owner), and `DeliverLogsPermissionArn` is a role in the source account. So resolution is
   "try configured profiles" (optionally prioritized by those hints), not "compute the owner".

**Acceptance criteria**
- With `network` bound to account X and `flow_logs` bound to account Y (a `[targets.<name>.roles]`
  binding), `describe-flow-logs`, `cloudtrail lookup-events` (IP-allocation + historical ENIs) and
  any CloudWatch `logs filter-log-events` reads run under **X**; `s3api list-objects-v2` +
  `get-object` run under **Y**. The flow logs found are X's, and the S3 prefixes fetched are the ones
  X's flow logs deliver to.
- Single-account runs (network and flow_logs resolve to the same account, the common case) behave
  exactly as before — no regression.
- **Auto-resolution:** with no explicit `flow_logs` binding, the S3 read is attempted under the
  primary profile; on `AccessDenied`/`Forbidden` it retries under the configured profile(s) resolved
  by `account_id`, using the first that succeeds; if none can read the bucket it errors with a message
  naming the bucket, the profiles tried, and how to bind the archive account. An explicit binding
  skips the trial. `AccessDenied` no longer hard-aborts the S3 read before the fallback is exhausted.
- Coverage check: if the discovered flow logs reference none of the discovered VPC/subnet/ENI ids,
  the run fails with an actionable message naming the mismatch (and the likely cause: config queried
  in the wrong account). VPCs with no flow log configured are reported (stderr), not fatal.
- Read-only guarantee intact; stdlib only; deterministic graph output; `ruff`-clean; tests offline.

---

## Read first (only what's relevant)
- `docs/02_architecture.md` — **§5.7** (flow logs), **§10–§11** (accounts / roles / targets, esp.
  **§11.3** resolution precedence, **§11.6** role registry, **§11.7** the collection loop), **§9**
  (read-only + error handling: prefer a partial graph clearly flagging what's missing over aborting).
- `docs/04_conventions.md` (hard rules + the mandatory learnings-file protocol).
- `docs/learnings/learnings_2026-08-01_flow-log-start-end.md`,
  `docs/learnings/learnings_2026-08-01_flowlog-fastfail-and-s3-cache.md`,
  `docs/learnings/learnings_2026-08-02_flowlog-size-aware-probe.md` (how the flow-log fetch,
  fast-fail probe, and size-aware/tail-trim reader are built — you're extending that reader).
- `README.md` — the flag/target docs and the "different profile for ENIs vs flow logs" guidance.
- Source: `src/cloudbreachgraph/config.py` (`resolve_target`, `ResolvedTarget`, `ResolvedAccount`,
  `Target.roles`), `src/cloudbreachgraph/aws/collectors.py` (the `flow_logs` role + `collect_all`),
  `src/cloudbreachgraph/cli.py` (`_active_roles`, `_collect_live`, `_collect_from_cache`),
  `src/cloudbreachgraph/mapping/builder.py` and `mapping/flowlogs.py` (where flow-log config +
  records become graph nodes/edges, and where a coverage check fits).

## Current state (the exact seams)
- **Roles resolve to independent accounts already.** `config.resolve_target(...)` returns a
  `ResolvedTarget` with one `ResolvedAccount{profile, account_id, region}` **per role**, so a
  `[targets.<name>.roles]` binding can already put `network` in X and `flow_logs` in Y. The grammar
  is fine — the bug is purely in *which role runs which command*.
- **`collect_all` (collectors.py) runs each role's collectors with that role's single
  `(profile, region)`.** The `flow_logs` role's collector list is:
  ```
  "flow_logs": [collect_flow_logs,           # aws ec2 describe-flow-logs      -> .FlowLogs[]
                collect_ip_allocation_events, # aws cloudtrail lookup-events    -> allocations
                collect_historical_enis,      # aws cloudtrail lookup-events x4 -> reconstructed ENIs
                collect_flow_log_records]     # readers: logs filter-log-events / s3 get-object
  ```
  All four run under the flow_logs account. That's the bug: the first three (config + CloudTrail
  history) and the CloudWatch reader belong to the **VPC account**; only the S3 reader belongs to the
  **archive account**.
- **`collect_flow_log_records` re-runs `describe-flow-logs` itself** (collectors.py, ~line 1011) to
  learn each flow log's destination, then dispatches per `LogDestinationType` via `FLOW_LOG_READERS`
  (`cloud-watch-logs` -> `_read_cloudwatch_records`; `s3` -> `_read_s3_records`). So the config query
  happens **twice**, both under the flow_logs profile.
- `_read_s3_records` lists (`s3api list-objects-v2`) and gets (`get-object`) under the profile/region
  passed to the reader; `_read_cloudwatch_records` calls `logs filter-log-events` similarly.
- `_normalize_flow_log` keeps `ResourceId` (a `vpc-`/`subnet-`/`eni-` id), `LogDestinationType`,
  `LogGroupName`, `LogDestination` (S3 ARN), status and `TrafficType` — enough to reconcile against
  discovered VPCs and to know where objects live.
- The `network` role already collects `vpcs`, `subnets`, `network_interfaces` — the discovered-id
  universe for the coverage check.

## Design guidance

### A. Split the flow-log work across two accounts
The clean model: **the flow-log configuration, CloudTrail history, and CloudWatch reads are
network-account operations; only S3 object I/O uses the archive account.**

- Discover `describe-flow-logs` **once**, under the **network** account's `ResolvedAccount`, and pass
  that config into the record fetch (stop `collect_flow_log_records` re-running it under the wrong
  profile). The flow-log *config* nodes and the *record* fetch should share one config source.
- Run `collect_ip_allocation_events` and `collect_historical_enis` (CloudTrail `lookup-events`) under
  the **network** account — the ENIs/allocations they reconstruct are the VPC account's resources.
- In the record fetch, split by destination type:
  - `cloud-watch-logs` -> `logs filter-log-events` under the **network** account (the log group lives
    with the VPCs). Note this assumption in the learnings; if a future setup uses cross-account
    CloudWatch, revisit.
  - `s3` -> `list-objects-v2` + `get-object` under the **archive (`flow_logs`) account**.
- Thread this cleanly rather than hacking a second profile through the `collector(profile, region)`
  contract. Options (pick one, justify in the learnings):
  - Give the flow-log record fetch an explicit two-account signature (network account for config +
    CloudWatch, archive account for S3), resolved in `cli.py` where both `ResolvedAccount`s are in
    hand, and drive it outside the generic `ROLE_COLLECTORS` loop; **or**
  - Reassign the role registry so `describe-flow-logs`/CloudTrail/CloudWatch collectors live under
    `network` (or a `network`-account-resolved role) and a dedicated storage role carries only S3.
  Keep `collect_all`'s per-role provenance (`meta.accounts`) meaningful, and keep the
  `collector(profile, region)` contract intact for the untouched collectors.
- **Single-account default:** when `network` and `flow_logs` resolve to the same account/profile
  (no cross-account binding, and under `--profile`/`--account`), behaviour must be identical to
  today. The split only matters when the two roles resolve to different accounts.
- **Region** follows the same split: the S3 calls use the archive account's region (the bucket's
  region — the object key path encodes it, e.g. `.../vpcflowlogs/<region>/...`); config/CloudTrail/
  CloudWatch use the network region. Use each `ResolvedAccount.region`.
- **`--from-cache`** must keep working: the cache replay path (`_collect_from_cache`) has no live
  accounts, so the two-account split must degrade to the single cached source without error.

### B. Resolve the archive account automatically (primary → AccessDenied fallback)
Don't force users to hand-bind `flow_logs` for the common centralized-logging case (the source
account is often granted read on the archive bucket). Resolution order for the **S3 read**:

1. If `flow_logs` is **explicitly bound** to an account (`[targets.<name>.roles]`), use that profile
   directly — no trial.
2. Otherwise attempt the S3 `list-objects-v2`/`get-object` under the **primary/network** profile.
3. On `AccessDenied`/`Forbidden`, **fall back**: from `AccountConfig` build the `account_id → profile`
   map and retry the read under each configured profile (each distinct from the primary), first
   success wins. Bounded (config is small), read-only, and — since a candidate may be an expired SSO
   session — reuse the existing `CredentialsExpiredError`/`aws sso login` recovery. Optionally
   **prioritize** candidates using the object key's `AWSLogs/<acctId>/` segment or the flow log's
   `DeliverLogsPermissionArn` (map that account id to a profile and try it first) — treat these as
   hints only (they name the **source** account, not necessarily the bucket owner).
4. If no configured profile can read the bucket, **error** with an actionable message: the bucket,
   the profiles tried, and how to bind the archive account explicitly.

Reclassify S3 `AccessDenied`/`Forbidden` in the error tiers (`aws/collectors.py`): today it's
SYSTEMIC → hard-abort; it must instead **drive this fallback** and only abort once the candidates are
exhausted. Keep other systemic errors (clock skew, `SignatureDoesNotMatch`) aborting as they do.
**Important scope limit:** this permission-triggered fallback is S3-only and cannot detect the
config-in-wrong-account bug — `describe-flow-logs` in the wrong account *succeeds* and returns the
wrong logs. Section C is what catches that; do not rely on auth errors for it.

### C. Validate VPC coverage (would have caught this immediately)
After the `network` role has produced `vpcs`/`subnets`/`network_interfaces` and the flow-log config
is known, reconcile them (a good home is `mapping/builder.py` / `mapping/flowlogs.py`, where both are
already in scope, or a small helper called from `cli.py` before the S3 fetch to fail *before*
downloading):
- Build the discovered-id set: all `vpc-*`, plus their `subnet-*` and `eni-*` ids.
- A flow log "covers" a discovered resource if its `ResourceId` is in that set (a flow log can be
  attached at VPC, subnet, or ENI scope — map subnet/ENI back to their VPC).
- **Hard failure:** if **no** discovered VPC is covered by **any** flow log (every flow log's
  `ResourceId` is foreign), raise an actionable error — this is the cross-account symptom. Name the
  counts ("discovered 4 VPCs; describe-flow-logs returned N flow logs, none referencing them") and
  the likely cause ("flow-log config was queried in the wrong account — it must run in the VPC
  account; check the `network` vs `flow_logs` target binding"). Do this **before** downloading
  objects so a misconfig fails fast, not after thousands of gets.
- **Soft warning (stderr):** discovered VPCs with no flow log configured (they'll have no flow-log
  analysis) — informational, not fatal, per §9.
- Consider surfacing coverage in `meta` (e.g. `meta.flow_log_coverage: {vpcs_total, vpcs_covered}`)
  so it's visible in `graph.json`. Deterministic (sorted), no wall-clock.

## Hard rules (docs/04_conventions.md)
- Python 3.11+, full type hints, **stdlib only**. Read-only: only `describe-*`/`list-*`/`get-*`/
  `head-*` + the existing `sts`/`cloudtrail lookup-events`/`logs filter-log-events` reads, and the
  error-gated `aws sso login`. No mutating calls.
- Deterministic graph output (sorted; no wall-clock). `ruff check` + `ruff format --check` clean.
- Tests fully offline — mock at the `runner` boundary (`run_aws`, `download_object`). The existing
  `tests/test_collectors.py::fake_aws` fixture already records `(args, profile, region)` per call —
  use it to assert *which account* each command ran under.

## Tests to add (offline)
- **Account split:** a two-account `ResolvedTarget` (network=X, flow_logs=Y). Assert `describe-flow-
  logs`, `cloudtrail lookup-events`, and `logs filter-log-events` run with **X**'s profile, while
  `s3api list-objects-v2` and `get-object` run with **Y**'s profile (assert on the recorded
  `profile`/`region` per call).
- **Single account:** network and flow_logs resolve to one account -> every flow-log command runs
  under that one profile (no regression); `--profile`/`--account` paths unaffected.
- **Config queried once:** `describe-flow-logs` is not run twice under different profiles.
- **Auto-fallback success:** no `flow_logs` binding; the S3 read raises `AccessDenied` under the
  primary profile, then succeeds under a second configured profile -> assert the retry ran under that
  profile and records were parsed. (Mock `download_object`/`run_aws` to deny for profile X and serve
  for profile Y.)
- **Auto-fallback exhausted:** `AccessDenied` under every configured profile -> actionable error
  naming the bucket and the profiles tried; no silent empty graph.
- **Explicit binding skips the trial:** with `flow_logs` bound, the S3 read runs only under the bound
  profile (no attempt under the primary).
- **Coverage hard-fail:** discovered VPCs {A,B,C,D}; flow logs all reference foreign `ResourceId`s ->
  actionable error raised **before** any `get-object`. Assert no S3 download happened.
- **Coverage partial:** some VPCs covered, one not -> run proceeds, warns about the uncovered VPC.
- **`--from-cache`** with flow logs still builds a graph offline (no live accounts) and doesn't crash
  on the split.

## Definition of done
- [ ] `describe-flow-logs` + CloudTrail history + CloudWatch reads run in the **network** account;
      S3 `list-objects-v2`/`get-object` in the **flow_logs (archive)** account; single-account runs
      unchanged; `describe-flow-logs` queried once.
- [ ] Archive-account auto-resolution: primary profile first, `AccessDenied` fallback across
      configured `account_id → profile` candidates (explicit binding skips the trial), clear error
      when exhausted; S3 `AccessDenied` reclassified from hard-abort to fallback-then-abort.
- [ ] VPC-coverage reconciliation: hard-fail (before S3 downloads) when no discovered VPC is covered;
      warn on VPCs without a flow log; optional `meta.flow_log_coverage`.
- [ ] `pytest` passes offline with the new tests; `ruff check` + `ruff format --check` clean.
- [ ] Read-only + determinism preserved; `--from-cache` still works.
- [ ] Docs updated: `README.md` (the network-vs-flow_logs account model, the auto-resolution/fallback,
      and the coverage check), `docs/02_architecture.md` (§5.7 the cross-account split + auto-resolution
      + coverage; §11 how the two roles map to accounts), IAM notes (network account needs
      `ec2:DescribeFlowLogs`, `cloudtrail:LookupEvents`, `logs:FilterLogEvents`; archive account needs
      `s3:ListBucket`+`s3:GetObject`).
- [ ] Write `docs/learnings/learnings_<YYYY-MM-DD>_<slug>.md` (per docs/04_conventions.md), committed
      with the code — capture the config-vs-storage account model, the two-account threading choice,
      the primary→AccessDenied auto-resolution (and why it can't catch the silent config bug), the
      bucket-owner-not-derivable finding, the CloudWatch-in-network assumption, and the coverage-check
      placement (before S3 I/O).

## Git
- Branch off the latest `main`. Commit in logical chunks; push. Do **not** open a PR unless asked.
