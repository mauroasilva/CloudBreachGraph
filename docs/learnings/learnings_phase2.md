# Learnings — Phase 2 (Domain Models, Graph Construction & Relationship Mapping)

> Prior-phase learnings: `docs/learnings/learnings_phase1.md` **exists and is complete**. Phase 2
> coded directly against its §2b normalized-dict shapes; no contract reconstruction was needed.

## 1. What this phase delivered

- **`model/resources.py`** — one dataclass **per AWS resource type**, each with a
  `from_collected(dict)` classmethod consuming Phase 1's normalized dicts:
  - `Eni` — `id, subnet_id, vpc_id, interface_type, description, status, availability_zone,
    requester_id, requester_managed, attachment_instance_id, private_ips, security_groups`.
    `private_ips`/`security_groups` are flattened to `list[str]` from the raw
    `PrivateIpAddresses[]`/`Groups[]`.
  - `Ec2Instance` — `id, state, instance_type, vpc_id, subnet_id, name` (`state` = `State.Name`;
    `name` = the `Name` tag or `None`).
  - `Elbv2LoadBalancer` — `arn, name, lb_type, scheme, dns_name, vpc_id`; `.node_id` property
    (= `arn`) and `.elb_token` property (the `app/<name>/<id>` / `net/` / `gwy/` ARN suffix used
    to match ENI descriptions). `from_collected` reads a `load_balancers_v2` dict.
  - `ClassicLoadBalancer` — `name, scheme, dns_name, vpc_id`; `.node_id` property (= `name`),
    `.lb_type` property (constant `"classic"`). `from_collected` reads a `load_balancers_classic`
    dict, including Classic's odd `VPCId` (capital P-C) key. **No `elb_token`** — Classic ELBs
    are matched by name, not ARN token.
  - `Subnet` — `id, vpc_id, cidr, availability_zone, name`.
  - `Vpc` — `id, cidr, is_default, name`.

  **ELBv2 and Classic are two separate dataclasses on purpose** (they are two separate AWS APIs):
  a schema change to one can't ripple into the other. The mapping layer treats them uniformly
  through a small structural `Protocol` (`_LoadBalancerLike` in `builder.py`) — no shared base
  class, so the two stay decoupled while both render to the single `load_balancer` node.
- **`model/graph.py`** — `Node(id, type, label, attributes)`, `Edge(source, target, relationship,
  attributes)`, and `Graph`:
  - `add_node` merges attributes on duplicate id (incoming wins per-key), upgrades a placeholder
    label/type; `add_edge` de-dupes on `(source, target, relationship)` (first wins).
  - `nodes` sorted by `(type, id)`; `edges` sorted by `(source, target, relationship)`.
  - `get_node(id)` accessor; `to_dict()` returns the Phase 3 contract (see §2).
- **`mapping/builder.py`** — `build_graph(collected: dict, *, include_orphans=False) -> Graph`
  implementing the §5 rules in the requested order, plus the `_LoadBalancerLike` protocol,
  `_parse_elb_description`, node factories, and the attribution helper.
- **Tests** — `tests/test_resources.py` (8), `tests/test_graph.py` (5), `tests/test_builder.py`
  (22). Total suite now **65 tests, fully offline** (30 Phase 1 + 35 Phase 2).

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

Entrypoint signature Phase 3 calls:

```python
from cloudbreachgraph.mapping.builder import build_graph
build_graph(collected: dict, *, include_orphans: bool = False) -> Graph
```

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

- **ENI-anchored graph by default, orphans opt-in via `include_orphans`.** Per
  `docs/01_overview.md` and §5, the ENI is the anchor: subnet nodes arise from ENI `SubnetId`
  references (§5.1) and VPC nodes from those subnets (§5.2). By default a collected resource that
  **no ENI references is not emitted** — the graph is the reachability map around ENIs, not a
  full inventory dump. `build_graph(collected, include_orphans=True)` flips this on for **every
  resource type**: it *also* emits every collected subnet (with its own `in_vpc` edge), VPC,
  EC2 instance and load balancer that no ENI references, as isolated nodes. Instances and LBs
  have no outgoing edges in this model, so an orphan of either is a standalone node carrying its
  own `subnet_id`/`vpc_id` (etc.) attributes. Implementation is idempotent: the orphan pass
  re-adds *all* collected resources of each type and `add_node`'s merge makes re-adding an
  already-referenced one a no-op, so only the unreferenced ones are actually new. Either way the
  "every subnet node has exactly one `in_vpc` edge" invariant holds. Phase 3 must surface this as
  a CLI flag — see §6.
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

- **Two load-balancer dataclasses instead of one `LoadBalancer`.** `docs/03_phase_plan.md` lists a
  single `LoadBalancer` dataclass. Because ELBv2 and Classic ELB are separate AWS APIs with
  materially different shapes (ARN vs. name identity, `VpcId` vs. `VPCId`, token vs. name
  matching), they are split into `Elbv2LoadBalancer` and `ClassicLoadBalancer` so a future change
  to one can't ripple into the other. Each honors the literal "one `from_collected(dict)`" contract
  (no `from_classic` overload). Both satisfy the `_LoadBalancerLike` protocol in `builder.py` and
  render to the single `load_balancer` graph node, so the Phase 3 node contract is unchanged.
- **`build_graph` gained an `include_orphans` keyword** (default `False`) so the tool can show
  resources with no matching ENI — subnets, VPCs, EC2 instances **and** load balancers. This is a
  superset of the documented behavior — the default is exactly the ENI-anchored graph the docs
  describe. Phase 3 wires it to a CLI flag (§6).
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
- **Phase 3 — wire the orphan flag (requested).** `build_graph` already accepts
  `include_orphans: bool = False` (§3/§4). The CLI must expose it — recommended:
  `--include-orphans` (store_true, default off) to show **all** resources with no matching ENI
  (subnets, VPCs, EC2 instances, load balancers), passed straight through as
  `build_graph(collected, include_orphans=args.include_orphans)`. Nothing else in the pipeline
  changes.
- **Real-account ELB verification:** capture and add fixtures for a Classic-ELB ENI and a GWLB ENI
  to promote §5's Classic/GWLB rules from "unit-tested against synthetic data" to "verified".
- `flow_logs` / other roles: unaffected — new node/edge types plug into the same `Graph` model.

## 7. How to verify this phase

```bash
pip install -e '.[dev]'          # or: pip install pytest ruff
python -m pytest -q              # 65 tests, fully offline via tests/fixtures/
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
