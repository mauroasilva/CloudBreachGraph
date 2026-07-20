# Learnings — Phase 2 (Domain Models, Graph Construction & Relationship Mapping)

> Prior-phase learnings: `docs/learnings/learnings_phase1.md` **exists and is complete**. Phase 2
> coded directly against its §2b normalized-dict shapes; no contract reconstruction was needed.

## 1. What this phase delivered

- **`model/resources.py`** — five frozen-style dataclasses, each with a `from_collected(dict)`
  classmethod consuming Phase 1's normalized dicts:
  - `Eni` — `id, subnet_id, vpc_id, interface_type, description, status, availability_zone,
    requester_id, requester_managed, attachment_instance_id, private_ips, security_groups`.
    `private_ips`/`security_groups` are flattened to `list[str]` from the raw
    `PrivateIpAddresses[]`/`Groups[]`.
  - `Ec2Instance` — `id, state, instance_type, vpc_id, subnet_id, name` (`state` = `State.Name`;
    `name` = the `Name` tag or `None`).
  - `LoadBalancer` — `node_id, name, lb_type, scheme, dns_name, vpc_id, arn`. **Two constructors**
    because ELBv2 and Classic differ: `LoadBalancer.from_collected(elbv2_dict)` (ALB/NLB/GWLB,
    `node_id = LoadBalancerArn`) and `LoadBalancer.from_classic(classic_dict)`
    (`node_id = LoadBalancerName`, `lb_type = "classic"`, reads Classic's odd `VPCId` key).
    `.elb_token` property returns the `app/<name>/<id>` (or `net/`,`gwy/`) ARN suffix for
    description matching; `None` for Classic.
  - `Subnet` — `id, vpc_id, cidr, availability_zone, name`.
  - `Vpc` — `id, cidr, is_default, name`.
- **`model/graph.py`** — `Node(id, type, label, attributes)`, `Edge(source, target, relationship,
  attributes)`, and `Graph`:
  - `add_node` merges attributes on duplicate id (incoming wins per-key), upgrades a placeholder
    label/type; `add_edge` de-dupes on `(source, target, relationship)` (first wins).
  - `nodes` sorted by `(type, id)`; `edges` sorted by `(source, target, relationship)`.
  - `get_node(id)` accessor; `to_dict()` returns the Phase 3 contract (see §2).
- **`mapping/builder.py`** — `build_graph(collected: dict) -> Graph` implementing the §5 rules in
  the requested order, plus `_parse_elb_description`, node factories, and the attribution helper.
- **Tests** — `tests/test_resources.py` (8), `tests/test_graph.py` (5), `tests/test_builder.py`
  (15). Total suite now **58 tests, fully offline** (30 Phase 1 + 28 Phase 2).

Layout matches `docs/02_architecture.md §2` exactly — no deviations. `model/` and `mapping/` were
empty packages from Phase 1; `output/` and `cli.py` remain untouched (Phase 3).

## 2. Interface contract for the next phase (Phase 3 renders this)

### 2a. `Graph.to_dict()` structure

```python
{
  "meta":  { ...collected["meta"]..., "tool_version": "0.1.0" },
  "nodes": [ {"id": str, "type": str, "label": str, "attributes": dict}, ... ],  # sorted (type, id)
  "edges": [ {"source": str, "target": str, "relationship": str, "attributes": dict}, ... ],
           #                                                    sorted (source, target, relationship)
}
```

- `meta` is Phase 1's `collected["meta"]` passed through verbatim (`target`, `region`,
  `accounts`) **plus** `tool_version` (from `cloudbreachgraph.__version__`).
- **No `generated_at` is added** — that would break deterministic test output. Phase 3's JSON
  writer should stamp a timestamp at write time if it wants one, not `build_graph`.

### 2b. Node `type` values and the `attributes` keys emitted per type

| `type` | `label` source | `attributes` keys |
|--------|----------------|-------------------|
| `eni` | the ENI id | `interface_type, status, availability_zone, description, requester_id, requester_managed, private_ips (list[str]), security_groups (list[str])` |
| `ec2_instance` | `Name` tag or id | `state, instance_type, vpc_id, subnet_id` |
| `load_balancer` (real) | LB name | `lb_type ("application"/"network"/"gateway"/"classic"), scheme, dns_name, vpc_id` |
| `load_balancer` (unresolved fallback) | parsed name or key | `synthetic: True, unresolved: True, interface_type` |
| `subnet` (real) | `Name` tag or id | `cidr, availability_zone, vpc_id` |
| `subnet` (synthetic) | the subnet id | `synthetic: True, unresolved: True` |
| `vpc` (real) | `Name` tag or id | `cidr, is_default` |
| `vpc` (synthetic) | the vpc id | `synthetic: True, unresolved: True` |
| `ec2_instance` (synthetic) | the instance id | `synthetic: True, unresolved: True` |

Phase 3 should treat any node carrying `attributes["synthetic"]` as a placeholder (e.g. dashed
outline in DOT). Real and synthetic nodes of the same type differ **only** by attribute keys.

### 2c. Edge `relationship` values and `attributes`

| `relationship` | source → target | `attributes` |
|----------------|-----------------|--------------|
| `attached_to` (instance) | ENI → EC2 instance | `{}` (no `match_rule`) |
| `attached_to` (LB) | ENI → load balancer | `{"match_rule": "elbv2_description" \| "classic_elb_description" \| "interface_type_fallback"}` |
| `in_subnet` | ENI → subnet | `{}` |
| `in_vpc` | subnet → VPC | `{}` |

Only load-balancer attachment edges carry `match_rule` — Phase 3 can label those edges with it.

## 3. Decisions & rationale

- **Referenced-only subnet/VPC nodes (ENI-anchored graph).** Per `docs/01_overview.md` and §5,
  the ENI is the anchor: subnet nodes arise from ENI `SubnetId` references (§5.1) and VPC nodes
  from those subnets (§5.2). A collected subnet/VPC that **no ENI references is not emitted** —
  the graph is the reachability map around ENIs, not a full inventory dump. This keeps the
  "every subnet has exactly one `in_vpc` edge" invariant trivially true (each subnet node comes
  from the ENI loop and gets exactly one VPC edge). *If Phase 3 wants orphan subnets/VPCs shown,
  that's a deliberate extension, not a bug.*
- **`match_rule` only on LB edges.** The docs mandate `match_rule` for load-balancer attribution
  (§5.4). Instance attachment (§5.3) is unambiguous, so its edge carries `{}` rather than a
  synthetic rule name — keeps the signal meaningful.
- **Synthetic VPC hint from the ENI.** A synthetic subnet (not in the collected set) has no
  `Subnet.VpcId`, so its `in_vpc` edge uses the **ENI's** `VpcId` (the documented cross-check
  field in §4). If that is also missing, the target is a placeholder id `unknown-vpc:<subnet_id>`
  (synthetic VPC node) so the edge never dangles.
- **Synthetic EC2 instance for a referenced-but-missing instance.** §5.3 doesn't spell this out,
  but if `Attachment.InstanceId` names an instance absent from `describe-instances` (IAM scope,
  race, cross-account), we emit a `synthetic/unresolved` instance node rather than drop the edge.
  Mirrors the subnet/VPC synthetic rule and keeps the graph closed.
- **Fallback LB node key (§5.4.3).** Keyed by the parsed description token if present
  (`net/<name>/<id>`), else the classic name, else `unresolved-lb:<eni_id>` (last resort avoids
  false-merging unrelated bare LB-type ENIs onto one node). Label is the middle `<name>` segment
  when a token exists.
- **`add_node` merge = incoming-wins-per-key.** Safe because the builder only ever creates a
  synthetic placeholder for a resource it has already confirmed is **absent** from the collected
  set (membership check first), so a real node never overwrites/keeps a stale `synthetic` flag in
  practice. Duplicate real adds (same LB/subnet referenced by many ENIs) merge idempotently.
- **Edges de-duped on `(source, target, relationship)`.** Prevents accidental parallel edges and
  keeps ordering stable; the loop structure already guarantees one `in_subnet` per ENI and one
  `in_vpc` per subnet.

## 4. Deviations from the plan

- **`LoadBalancer` has two constructors, not one.** `docs/03_phase_plan.md` says "each with a
  `from_collected(dict)`". ELBv2 and Classic shapes differ too much (ARN vs. name, `VpcId` vs.
  `VPCId`) for one constructor to be honest, so `from_collected` handles the ELBv2 shape and
  `from_classic` handles Classic. Documented here so Phase 3 (and any test) knows both entry
  points.
- Otherwise no deviations. Node/edge/`to_dict` shapes match `docs/02_architecture.md §6` and the
  `docs/03_phase_plan.md` Phase 2→3 contract.

## 5. Gotchas, surprises & AWS quirks

- **ELB description formats — verified against the Phase 1 fixtures, ALB & NLB confirmed.**
  - ALB ENI `Description = "ELB app/my-alb/50dc6c495c0c9188"`, `InterfaceType = "interface"` →
    matched to the ELBv2 ARN suffix `:loadbalancer/app/my-alb/50dc6c495c0c9188`. ✅
  - NLB ENI `Description = "ELB net/my-nlb/1a2b3c4d5e6f7a8b"`,
    `InterfaceType = "network_load_balancer"` → ARN suffix match. ✅ (Note: the NLB resolves via
    **rule 1 (`elbv2_description`)**, *not* the interface-type fallback, because the description
    matched a collected LB. The fallback fires only when the description doesn't resolve.)
  - **Classic ELB (`"ELB <name>"`, no slash) and GWLB (`"ELB gwy/..."`) are UNVERIFIED against a
    real capture** — no fixture ENI exercises them. Their handling is covered by unit tests with
    hand-built bundles and by the documented format, but a real-account ENI should be captured to
    confirm. In particular: does a Classic-ELB ENI ever contain a `/` in its LB name (which would
    fool the "no slash ⇒ classic" heuristic)? Classic ELB names can't contain `/`, so the
    heuristic should hold, but confirm.
- **`_parse_elb_description` is prefix-strict:** it only treats a description as ELB-related if it
  starts with the literal `"ELB "`. NAT-gateway ENIs (`"Interface for NAT Gateway ..."`),
  VPC-endpoint, RDS and Lambda ENIs fall through to "no attachment" — exactly the §5 tail
  behavior. The NAT fixture confirms this.
- **`Attachment.InstanceId` is the sole instance signal.** Phase 1 guarantees `Attachment` is
  always a dict with `InstanceId` present-or-`None`, so the check is a plain truthiness test — no
  `KeyError`/`None`-guard on `Attachment` itself needed.
- **Determinism holds across runs** (`test_build_is_deterministic`): sorting by `(type, id)` /
  `(source, target, relationship)` gives byte-stable `to_dict()` output for stable JSON/DOT diffs.

## 6. Known gaps / TODO for later phases

- **Phase 3:** consume `Graph.to_dict()` (§2). Color nodes by `type`; render `synthetic` nodes
  distinctly (dashed); label LB edges with `attributes["match_rule"]`; cluster subnets/ENIs inside
  their VPC (`subgraph cluster_*`). Add `generated_at` at write time if desired (kept out of
  `build_graph` for determinism).
- **Orphan resources:** collected subnets/VPCs/instances/LBs with **no** ENI reference are not in
  the graph (see §3). If a full-inventory view is wanted, add them as isolated nodes — a conscious
  Phase 3+ choice, not implied by v1.
- **Real-account ELB verification:** capture and add fixtures for a Classic-ELB ENI and a GWLB ENI
  to promote §5's Classic/GWLB rules from "unit-tested against synthetic data" to "verified".
- `flow_logs` / other roles: unaffected — new node/edge types plug into the same `Graph` model.

## 7. How to verify this phase

```bash
pip install -e '.[dev]'          # or: pip install pytest ruff
python -m pytest -q              # 58 tests, fully offline via tests/fixtures/
python -m pytest tests/test_builder.py tests/test_graph.py tests/test_resources.py -q  # Phase 2 only
ruff check . && ruff format --check .
```

Quick manual smoke of the graph shape:

```bash
python -c "
from tests.conftest import load_fixture
from cloudbreachgraph.aws import collectors, runner
from cloudbreachgraph.config import ResolvedAccount, ResolvedTarget
from cloudbreachgraph.mapping.builder import build_graph
import json
fx={('ec2','describe-network-interfaces'):'ec2_describe-network-interfaces.json',('ec2','describe-instances'):'ec2_describe-instances.json',('elbv2','describe-load-balancers'):'elbv2_describe-load-balancers.json',('elb','describe-load-balancers'):'elb_describe-load-balancers.json',('ec2','describe-subnets'):'ec2_describe-subnets.json',('ec2','describe-vpcs'):'ec2_describe-vpcs.json'}
runner.run_aws=lambda args,**k: load_fixture(fx[tuple(args[:2])])
r=ResolvedTarget(target='prod',roles={'network':ResolvedAccount('prod-audit','111111111111','us-east-1')})
print(json.dumps(build_graph(collectors.collect_all(r)).to_dict(), indent=2, default=str))
"
```
