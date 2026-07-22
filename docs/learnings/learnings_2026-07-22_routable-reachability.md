# Learnings — 2026-07-22 routable-reachability

Follow-up to `learnings_2026-07-22_eni-reachability-mapping.md` (same day): that change added
`can_reach` edges from security-group rules; this one adds **route-table–based routability** and
splits each edge into `routable_can_reach` / `not_routable_can_reach` (or plain `can_reach` when
undetermined).

## 1. What this change delivered
- **`aws/collectors.py`**: `collect_route_tables` (`aws ec2 describe-route-tables` →
  `.RouteTables[]`) + `_normalize_route_table` / `_normalize_route` / `_route_target` (collapses the
  many mutually-exclusive next-hop keys — `GatewayId`, `NatGatewayId`, `TransitGatewayId`,
  `VpcPeeringConnectionId`, … — into one `Target` string). Registered as the **8th** collector of the
  `network` role (`ROLE_COLLECTORS`/`ROLE_RESULT_KEYS`, key `"route_tables"`).
- **`model/resources.py`**: `RouteTable` + `Route` dataclasses (`is_main` from any association's
  `Main`; `subnet_ids` from associations; `target`/`state` per route).
- **`mapping/routing.py`** (NEW module): `RouteResolver` — resolves each ENI's effective route table
  (explicit subnet association, else VPC main RT) and `classify(kind, cidr, ctx)` returns the
  relationship. Relationship constants `RELATIONSHIP_ROUTABLE` / `_NOT_ROUTABLE` / `_UNDETERMINED`
  live here.
- **`mapping/builder.py`**: builds a `RouteResolver` and passes it to `_map_reachability`, which now
  records each source's `kind`/`cidr` and computes the edge relationship per `(source, ENI)`.
- **`output/dot_export.py`**: `_edge_extra` colors reachability edges by routability — routable solid
  red (`#E53935`), not-routable grey dashed (`#9E9E9E`).
- **`output/html_export.py`**: `_REACH_RELS` (the three relationships); the two `== "can_reach"`
  edge checks in `_vpc_group_of` / `_ringed_view_data` widened to `in _REACH_RELS` so the ringed
  outer-ring grouping still works regardless of the routability verdict.
- Fixture `tests/fixtures/ec2_describe-route-tables.json` (public RT → igw, private RT → nat, main
  RT); route-tables entry added to every test module's `_COMMAND_FIXTURES`.
- Docs: `README.md` (routable/not-routable subsection), `02_architecture.md` (§2, §3, §4, **§5.6**,
  §6, §7, §11.6), `05_roadmap.md`.

## 2. Interface contract for the next session
- **Edge relationships** now: `attached_to`, `in_subnet`, `in_vpc`, and the reachability family
  **`can_reach` / `routable_can_reach` / `not_routable_can_reach`**. Anything that branched on
  `relationship == "can_reach"` must use the family (see `html_export._REACH_RELS`, or
  `.endswith("can_reach")` in tests). Node types are unchanged (route tables are **not** graph nodes
  — they only drive the edge relationship).
- **Bundle** now has a `route_tables` key (8 keys in the `network` role). `RouteTable`/`Route` are in
  `model/resources.py`; the routability decision is `mapping/routing.py::RouteResolver`.
- **Undetermined fallback**: with no route-table data (old `--from-cache` dir, missing permission),
  `RouteResolver` returns plain `can_reach`. This is intentional and load-bearing for backward compat.

## 3. Decisions & rationale
- **Route tables joined the `network` role** (8th collector), same reasoning as security groups —
  they live with the ENIs.
- **Routability is edge *relationship*, not an attribute.** The user asked for different edge
  "types"; encoding it in `relationship` (a free string the whole pipeline already keys on) is the
  natural fit and keeps `ports` as the only attribute.
- **New `mapping/routing.py` module** rather than growing `builder.py`: the route-analysis logic
  (RT resolution + CIDR containment + gateway-prefix classification) is cohesive and testable on its
  own, and `builder.py` was already the largest mapping file. Recorded in `02_architecture.md §2`.
- **Documented-approximation model, not a simulator.** internet→needs igw default + public IP;
  in-VPC CIDR / peer SG→local route (routable); external CIDR→internet path or a `vgw-/tgw-/pcx-`
  route. NACLs, route propagation, and cross-VPC peering path validation are explicitly out of scope
  (README + §5.6 say so) — over-claiming here would be worse than an honest approximation.
- **"public subnet" alone is not enough for internet-routable** — the ENI must also have a public
  IP. In the fixtures this is what makes `eni-00alb` (public subnet, no public IP)
  `not_routable_can_reach` while `eni-00instance` (public subnet, public IP) is `routable`.

## 4. Deviations from the plan
- None structurally. This realises the "route_tables / distinguish allowed vs routable" follow-up
  that the previous learnings file flagged.

## 5. Gotchas / AWS quirks
- A route's next hop is spelled in one of ~9 mutually-exclusive keys; `local` is a `GatewayId`
  value (not a separate field). `_route_target` collapses them in priority order.
- The **main** route table has an association with `Main: true` and (usually) **no** `SubnetId`; it
  is the fallback for any subnet without an explicit association.
- `describe-route-tables` returns a route's `State` as `active`/`blackhole`; blackhole routes are
  skipped (`_active`). Missing `State` is treated as active.
- Route-table associations can carry an `AssociationState` sub-object; we only need `Main`/`SubnetId`.
- Every test module that drives `collect_all` needs the `("ec2","describe-route-tables")` fixture
  entry — same footgun as the security-groups collector.

## 6. Known gaps / TODO
- No NACL evaluation, no TGW/VPN/DX route-propagation resolution, no cross-VPC peering path check —
  a `pcx-`/`tgw-` route is trusted to mean "reachable" without verifying the far side.
- IPv6 routability is coarse: an `internet` node dedups v4+v6, and `_has_default_internet_route`
  accepts a default on either family; a v6-only default with a v4-only public IP isn't distinguished.
- Egress-only internet gateways (`eigw-`) are (correctly) not treated as inbound-routable, but also
  not surfaced anywhere.

## 7. How to verify
```bash
pip install -e '.[dev]'
pytest                       # 199 passing, fully offline
ruff check . && ruff format --check .
cloudbreachgraph --from-cache tests/fixtures --output-dir /tmp/cbg-out
#   graph.json: internet:eni-00instance... -> routable_can_reach (public subnet + public IP)
#               internet:eni-00alb...      -> not_routable_can_reach (public subnet, no public IP)
```
