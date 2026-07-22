# Learnings — 2026-07-22 eni-ownership-nat-gateways

## 1. What this change delivered
Gave previously-ownerless service ENIs a real owner, so **every ENI in the account attributes to
an owner node**. Concretely, added **NAT gateways** and **VPC endpoints** as ENI owners:

- `model/resources.py` — new dataclasses `NatGateway` and `VpcEndpoint`, each with
  `from_collected`. Both expose an authoritative `eni_ids` list (NAT: from
  `NatGatewayAddresses[].NetworkInterfaceId`; endpoint: from `NetworkInterfaceIds[]`). NAT also
  carries `public_ips` (from `NatGatewayAddresses[].PublicIp`).
- `aws/collectors.py` — `_normalize_nat_gateway`, `_normalize_vpc_endpoint`,
  `collect_nat_gateways` (`aws ec2 describe-nat-gateways` → `.NatGateways[]`),
  `collect_vpc_endpoints` (`aws ec2 describe-vpc-endpoints` → `.VpcEndpoints[]`). Both registered
  in `ROLE_COLLECTORS["network"]` / `ROLE_RESULT_KEYS["network"]` (bundle keys `nat_gateways`,
  `vpc_endpoints`) — data-only registry additions, no driver/CLI/config change (§11.6).
- `mapping/builder.py` — builds an `eni_owner: dict[eni_id -> (Node, match_rule)]` from the two
  resources' ENI lists, and `_attribute_eni` consults it **after** instance attachment and
  **before** ELB description parsing. New node factories `_nat_gateway_node` /
  `_vpc_endpoint_node` (node types `nat_gateway`, `vpc_endpoint`). New `match_rule`s:
  `nat_gateway_address`, `vpc_endpoint_interface`. `--include-orphans` now also emits any NAT
  gateway / VPC endpoint no ENI references (chiefly Gateway S3/DynamoDB endpoints, which own no ENI).
- `output/dot_export.py` + `output/html_export.py` — colors/shapes/detail lines/radii for the two
  new node types; both are VPC-clustered (dot) and land on **ring 3, the load-balancer ring**
  (html ringed layout) because `_ring_of` returns the default 3 for them. JS `radiusFor` updated
  in both templates to keep drawn radii in step with `_NODE_RADII`.
- `graph_io.py` — `_BARE_ATTR` entries so the DOT round-trip recovers `nat_gateway.state` and
  `vpc_endpoint.service_name`.
- `anonymize.py` — added `vpce` to `_ID_PREFIXES` so endpoint ids anonymise like other resource ids.

## 2. Interface contract for the next session
- Collected bundle (`collect_all`) now has two more `network`-role keys: `nat_gateways`,
  `vpc_endpoints` (lists of normalized dicts). Any code that asserts the exact bundle key set must
  include them (all test `_COMMAND_FIXTURES` maps + collector-shape tests were updated).
- Graph gained node types `nat_gateway` and `vpc_endpoint`; both use the existing `attached_to`
  relationship (ENI → owner), exactly like `ec2_instance` / `load_balancer`. Anything that
  special-cases node types (ring assignment, colors, clustering, detail lines, round-trip bare
  attrs) must handle them — all such sites were updated.
- New `match_rule` values on `attached_to` edges: `nat_gateway_address`, `vpc_endpoint_interface`.

## 3. Decisions & rationale
- **Authoritative ENI-id lists, not description parsing.** NAT gateways and VPC endpoints both
  publish exactly which ENIs they hold, so attribution keys on the ENI id — robust and
  unambiguous, unlike the ELB `Description` heuristic. This is the pattern to prefer for any
  future owner.
- **Same ring/layer as load balancers** (the change request's explicit ask). Implemented for free:
  `_ring_of` already returns ring 3 (the "everything else under the VPC" ring) for any
  non-vpc/subnet/eni/sg/source type, and dot clusters them by `vpc_id`. Gave them LB-sized radii
  (13) and the `component` shape with distinct fills so they read as the same class but are
  distinguishable.
- **Scope: NAT gateways + VPC endpoints only.** These are the two big ownerless-ENI classes with a
  clean authoritative `describe-*` ENI list. RDS/Lambda/EFS/TGW lack one and are left as documented
  follow-ups (README + roadmap) rather than attributed by fragile description parsing.
- **Priority: instance → owner map → ELB → interface_type fallback.** Instance attachment still
  wins outright (§5.3 invariant "never more than one owner"); the owner map is the strongest
  remaining signal. Orderings can't actually collide (a NAT/endpoint ENI is never ELB-described).

## 4. Deviations from the plan
None. Followed the §11.6 "new resource = registry entry + collectors + graph mapping" recipe; no
config-grammar or CLI change (the request explicitly wanted none).

## 5. Gotchas, surprises & AWS quirks
- A **Gateway** VPC endpoint (S3/DynamoDB) owns **no** ENI — `NetworkInterfaceIds` is empty; it's a
  route-table target. So it contributes no attribution and only appears under `--include-orphans`.
  Only `Interface` / `GatewayLoadBalancer` endpoints own ENIs.
- The shared test fixture `ec2_describe-network-interfaces.json` gained a 5th ENI
  (`eni-00vpce00000000006`, a `vpc_endpoint`), which rippled into every `_COMMAND_FIXTURES` map
  (6 test files) and the collector count/shape assertions. Adding a describe-* collector means
  **every** test that mocks `runner.run_aws` via a `_COMMAND_FIXTURES` dict needs the new keys or
  it KeyErrors — including `test_anonymize.py`, which is easy to miss.
- The existing `test_nat_gateway_eni_has_no_attachment` was inverted by this change and became
  `test_nat_gateway_eni_attaches_to_its_nat_gateway`.
- The html template has **two** `radiusFor` JS functions (force view + static-layout view); both
  must be updated together.

## 6. Known gaps / TODO for later
- Remaining ENI owners without an authoritative ENI list: **RDS/ElastiCache, Lambda VPC ENIs, EFS
  mount targets, Transit Gateway attachments.** Add each via the §5.4 recipe once a reliable
  ownership signal is found. See `docs/05_roadmap.md` → "Remaining ENI owners (follow-up)".
- NAT public IP is surfaced on the `nat_gateway` node (and flags it `public`), but routability
  (§5.6) still only reads `eni.public_ips`; a NAT ENI's public addressing isn't fed into the
  reachability routability check. Out of scope here.

## 7. How to verify
```bash
pip install -e '.[dev]'
pytest                      # 223 tests, all offline
ruff check . && ruff format --check .
# End-to-end offline against the checked-in fixtures:
cloudbreachgraph --from-cache tests/fixtures --output-dir /tmp/cbg-out
# Confirm every ENI has an attached_to owner, incl. the NAT gateway and VPC endpoint:
python -c "import json;d=json.load(open('/tmp/cbg-out/graph.json'));\
print([(e['source'],e['target'],e['attributes'].get('match_rule')) for e in d['edges'] if e['relationship']=='attached_to'])"
```
