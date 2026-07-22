# Learnings — 2026-07-22 eni-reachability-mapping

## 1. What this change delivered
Maps **how each ENI is reachable** from its security-group inbound rules, as new graph
nodes/edges that flow into every output (JSON, DOT, HTML force/ringed/overlap-free).

- **`aws/collectors.py`**: new `collect_security_groups` (`aws ec2 describe-security-groups`
  → `.SecurityGroups[]`), plus `_normalize_security_group` / `_normalize_ip_permission`
  (keeps identity + **ingress** `IpPermissions` only; egress dropped). Registered in the
  `network` role: appended `collect_security_groups` to `ROLE_COLLECTORS["network"]` and
  `"security_groups"` to `ROLE_RESULT_KEYS["network"]` (data-only, per §11.6).
- **`model/resources.py`**: new `SecurityGroup` + `SgIngressRule` dataclasses.
  `SgIngressRule.port_label()` → `"tcp/443"`, `"tcp/8000-8100"`, `"all"` (protocol `-1`), or
  bare protocol (`"icmp"`).
- **`mapping/builder.py`**: new `_map_reachability(graph, enis, security_groups)`, called as
  step 5 of `build_graph` (always on, independent of `--include-orphans`). For each ENI × its
  SGs × each ingress rule, adds a **source node** + `can_reach` edge (source → ENI) with a
  `ports` attribute. `_INTERNET_CIDRS = {"0.0.0.0/0", "::/0"}`.
- **`output/dot_export.py`**: styles for the three new node types; `ports` shown on
  `can_reach` edge labels; **removed** the old public-IP-based shared `Internet` decoration
  (replaced by real per-ENI `internet` nodes).
- **`output/html_export.py`**: `_TYPE_COLORS` for the new types; ringed layout gains a **5th
  ring** (`_RING_COUNT = 5`) — reachability sources sit on the outermost ring 4, aligned to the
  mean angle of the ENIs they reach.
- Fixture `tests/fixtures/ec2_describe-security-groups.json`; tests across
  `test_builder/test_resources/test_collectors/test_output/test_convert` (+ the shared
  `_COMMAND_FIXTURES` maps in every test module that drives `collect_all`).
- Docs: `README.md` (Outputs, new "ENI reachability" section, ringed description),
  `02_architecture.md` (§3, §4, **§5.5**, §6, §7, §11.1, §11.6), `05_roadmap.md`.

## 2. Interface contract for the next session
- **New node types** (all IDs namespaced so they never collide): `internet` (`internet:<eni-id>`,
  label `Internet`, attrs `{}`, **per-ENI, never shared**), `cidr` (`cidr:<cidr>`, attr
  `{"cidr": ...}`, shared), `security_group` (`sg-source:<group-id>`, attr `{"group_id": ...}`,
  shared, label = peer SG name or the id).
- **New edge relationship**: `can_reach`, **source → ENI**, attr `{"ports": "<summary>"}`.
- **Collected bundle** now has a `security_groups` key (7 keys in the `network` role, not 6). Any
  test/tool that mocks `collect_all` or asserts bundle shape must include it.
- `SecurityGroup.from_collected(normalized_dict)` and `SgIngressRule` live in
  `model/resources.py`; `port_label()` is the canonical port-summary formatter.
- Ringed layout ring indices are now: 0 VPC · 1 subnet · 2 ENI · 3 EC2/LB · **4 reach sources**
  (`html_export._ring_of`, `_REACH_TYPES`). Any new outer-ring work extends the `(1,2,3,4)`
  tuples in `_optimize_cluster`/`_reduce_crossings`.

## 3. Decisions & rationale
- **Security groups folded into the `network` role, not a new role.** SGs live in the same
  account as the ENIs they govern, so a separate role/account binding buys nothing (contrast
  `flow_logs`, which is genuinely cross-account). Roadmap updated to say so.
- **Per-ENI `internet` nodes** (change request's explicit ask): a single shared Internet node
  would fan a spoke to every internet-facing ENI → many long crossing edges. CIDR and peer-SG
  sources *are* shared, because a specific range/SG usually reaches few ENIs and sharing conveys
  "this source reaches these N ENIs".
- **Reachability is not route-gated.** We surface a `0.0.0.0/0` SG rule as an `Internet` source
  even on a private-only ENI. The SG rule is the recorded "who is allowed to connect"; adding
  route-table/public-IP reachability is future work (noted in §5.5 and roadmap `route_tables`).
- **Ports aggregated per (source, ENI)** before emitting one deduped edge, so multiple rules from
  the same source collapse to one edge with a sorted, comma-joined `ports` value (deterministic).
- **Radius:** new types use the default node radius (9) — no `radiusFor`/`_NODE_RADII` edits, so
  the Python overlap math and the JS drawing stay in sync automatically. Only `_TYPE_COLORS` (and
  DOT `_TYPE_STYLE`) were extended; node `color` ships in the payload, so no JS template change.

## 4. Deviations from the plan
- Removed the DOT-only shared `Internet` node (public-IP heuristic). Public-IP exposure is still
  visible: the ENI's `Public IP:` DOT label line and the HTML red "exposed" outline both key off
  `public_ips`, untouched. `graph_io.load_dot` keeps the legacy `Internet`→`public_ips` fold so
  *old* `.dot` captures still load; new output never emits that literal node.

## 5. Gotchas / AWS quirks
- `describe-security-groups` returns both `IpPermissions` (ingress) and `IpPermissionsEgress` —
  only ingress matters for "who can reach"; egress is dropped in normalization.
- Protocol `"-1"` means *all traffic* and has **no** `FromPort`/`ToPort` → `port_label()` → `"all"`.
- **`ipaddress.is_private` ≠ the anonymizer's RFC1918 notion.** `203.0.113.0/24` (TEST-NET-3) is
  `is_private == True` in stdlib but the anonymizer treats only 10/172.16-31/192.168 as private
  and maps it to a *public* replacement. The reachability CIDR fixture uses `203.0.113.0/24`, which
  forced `test_cidr_keeps_prefix_length_and_class` to compare class via an explicit RFC1918 check.
- Adding the `network` role's 7th collector means **every** test module that mocks
  `runner.run_aws` via a `_COMMAND_FIXTURES[tuple(args[:2])]` lookup needs the
  `("ec2","describe-security-groups")` entry, or `collect_all` KeyErrors.

## 6. Known gaps / TODO
- No route-table / NACL reachability yet — a `0.0.0.0/0` rule is reported regardless of whether the
  ENI is actually routable. Pairing with a future `route_tables` collector would let the map mark
  *allowed* vs *reachable*.
- A shared `cidr`/`security_group` source that reaches ENIs across several VPCs is grouped (in the
  ringed layout) with the **first** VPC only (deterministic via sorted edges); its spokes to other
  VPCs cross cluster boundaries. Per-ENI `internet` nodes never have this issue.
- NACLs and NLB security groups (recently added by AWS) are not separately modelled; NLB/ALB SGs
  are captured only via the ENI's own `Groups[]`.

## 7. How to verify
```bash
pip install -e '.[dev]'
pytest                       # 185 passing, fully offline
ruff check . && ruff format --check .
# End-to-end against the checked-in fixtures (now includes a security-groups fixture):
cloudbreachgraph --from-cache tests/fixtures --output-dir /tmp/cbg-out
#   -> /tmp/cbg-out/graph.json has internet:/cidr:/sg-source: nodes + can_reach edges
cloudbreachgraph-to-html /tmp/cbg-out/graph.json --ringed -o /tmp/cbg-out/ringed.html
#   -> reachability sources on the outermost ring
```
