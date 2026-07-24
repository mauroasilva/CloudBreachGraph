# 02 — Architecture

This is the technical reference every phase relies on. The **relationship-mapping rules**
in section 5 are the core of the application — read them carefully.

## 1. Technology choices

- **Language:** Python 3.11+ (use standard library where possible).
- **Data source:** AWS CLI v2, invoked via `subprocess` with `--output json`. **Not boto3.**
- **Packaging:** `pyproject.toml` (PEP 621), console entry point `cloudbreachgraph`.
- **Runtime dependencies:** aim for **zero required** third-party packages. Graphviz DOT is
  emitted as plain text (no library needed). The `dot` binary is optional and only used to
  rasterize. If a phase wants the `graphviz` Python package for convenience, it must be an
  **optional** extra, never a hard dependency.
- **Testing:** `pytest`. AWS CLI calls are mocked with recorded JSON fixtures so tests run
  offline. `pytest` is a dev dependency only.
- **Style:** type hints everywhere, `dataclasses` for models, `ruff`-clean formatting.
- **Config file format:** TOML, parsed with the stdlib `tomllib` (Python 3.11+, read-only) so
  the account→profile mapping needs **no** third-party dependency. Do not use YAML (that would
  pull in PyYAML). JSON may be accepted as a secondary format since it's also stdlib.

## 2. Suggested project layout

```
CloudBreachGraph/
├── pyproject.toml
├── README.md                     # user-facing, short (docs/ holds the build plan)
├── src/
│   └── cloudbreachgraph/
│       ├── __init__.py
│       ├── __main__.py           # enables `python -m cloudbreachgraph`
│       ├── cli.py                # argparse entrypoint  (Phase 3)
│       ├── config.py             # account -> profile mapping loader/resolver  (Phase 1)
│       ├── aws/
│       │   ├── __init__.py
│       │   ├── runner.py         # subprocess wrapper around `aws ...`  (Phase 1)
│       │   └── collectors.py     # describe_* functions -> normalized dicts  (Phase 1)
│       ├── model/
│       │   ├── __init__.py
│       │   ├── resources.py      # dataclasses: Eni, Ec2Instance, LoadBalancer, Subnet, Vpc  (Phase 2)
│       │   └── graph.py          # Node, Edge, Graph  (Phase 2)
│       ├── mapping/
│       │   ├── __init__.py
│       │   ├── builder.py        # build_graph(collected) -> Graph, relationship rules  (Phase 2)
│       │   ├── routing.py        # RouteResolver: routability of reachability edges (§5.6)
│       │   └── collapse.py       # collapse_security_groups(graph): SG-layer view transform (§5.5)
│       └── output/
│           ├── __init__.py
│           ├── json_export.py    # Graph -> JSON  (Phase 3)
│           └── dot_export.py     # Graph -> Graphviz DOT  (Phase 3)
├── tests/
│   ├── fixtures/                 # recorded AWS CLI JSON responses
│   └── test_*.py
└── docs/                         # this plan (already present)
```

Phases may adjust this layout, but **if they do, they must record the final layout in their
`learnings_phaseX.md`** so the next session isn't surprised.

## 3. AWS CLI commands used

All commands run with `--output json`. Region and profile are threaded through from the CLI
flags. The AWS CLI auto-paginates by default, returning the full result set.

| Resource | Command | Key output path |
|----------|---------|-----------------|
| Network Interfaces | `aws ec2 describe-network-interfaces --region <r>` | `.NetworkInterfaces[]` |
| EC2 Instances | `aws ec2 describe-instances --region <r>` | `.Reservations[].Instances[]` |
| Load Balancers (v2: ALB/NLB) | `aws elbv2 describe-load-balancers --region <r>` | `.LoadBalancers[]` |
| Load Balancers (v1: Classic) | `aws elb describe-load-balancers --region <r>` | `.LoadBalancerDescriptions[]` |
| Subnets | `aws ec2 describe-subnets --region <r>` | `.Subnets[]` |
| VPCs | `aws ec2 describe-vpcs --region <r>` | `.Vpcs[]` |
| Security Groups | `aws ec2 describe-security-groups --region <r>` | `.SecurityGroups[]` |
| Route Tables | `aws ec2 describe-route-tables --region <r>` | `.RouteTables[]` |
| NAT Gateways | `aws ec2 describe-nat-gateways --region <r>` | `.NatGateways[]` |
| VPC Endpoints | `aws ec2 describe-vpc-endpoints --region <r>` | `.VpcEndpoints[]` |
| VPC Flow Logs config (`flow_logs`) | `aws ec2 describe-flow-logs --region <r>` | `.FlowLogs[]` |
| IP-allocation history (`flow_logs`) | `aws cloudtrail lookup-events --lookup-attributes=AttributeKey=EventName,AttributeValue=CreateNetworkInterface` | `.Events[].CloudTrailEvent` |
| Flow-log records (`flow_logs`) | `aws logs filter-log-events --log-group-name=<g> --start-time=<ms>` | `.events[].message` |
| Caller identity (account check) | `aws sts get-caller-identity` | `.Account`, `.Arn` |

The three `flow_logs`-role commands are opt-in (`--flow-logs`, §5.7). They are **read-only**
retrievals — `cloudtrail lookup-events` and `logs filter-log-events` retrieve, never mutate — even
though their verbs aren't the usual `describe`/`list`/`get`/`head` (the read-only guarantee is about
*not mutating*, §9). Value-carrying flags are passed in `--flag=value` form so the cache-key /
`--from-cache` file naming (which keys on the positional sub-command) stays stable.

Notes for the collection layer (Phase 1):

- Add `--no-cli-pager` to avoid the interactive pager blocking a subprocess.
- Respect an optional `--profile <name>` by passing it through to **every** `aws` call. The
  profile is resolved from the account→profile mapping (see §10) or from an explicit override.
- Treat a non-zero exit code as a hard error with the captured stderr surfaced to the user
  (common causes: expired creds, missing permissions, wrong region).
- `elb`/`elbv2` may be absent or return empty in accounts with no load balancers — handle
  the empty case gracefully; do not treat "no load balancers" as an error.
- Consider a `--cache-dir` option that writes each raw JSON response to disk, so Phase 2/3
  and tests can replay real captures. Optional but recommended.

## 4. Fields we depend on (record any schema surprises in learnings)

**Network Interface** (`.NetworkInterfaces[]`):
- `NetworkInterfaceId` — node id, e.g. `eni-0abc...`
- `SubnetId` — always present → subnet edge
- `VpcId` — present (redundant with subnet's VPC, but useful as a cross-check)
- `InterfaceType` — e.g. `interface`, `network_load_balancer`, `nat_gateway`, `vpc_endpoint`, `lambda`, `gateway_load_balancer`
- `Attachment.InstanceId` — present when attached to an EC2 instance
- `Attachment.InstanceOwnerId` — for service-managed ENIs this is an AWS service principal (e.g. `amazon-elb`, `amazon-aws`)
- `Description` — free-text; **critical** for load balancer attribution (see §5)
- `RequesterId`, `RequesterManaged` — service-managed ENIs (ELB, NAT, RDS, etc.)
- `PrivateIpAddresses[]`, `Groups[]` (security groups) — useful node metadata
- `Association.PublicIp` (interface-level, and per-address under `PrivateIpAddresses[].Association.PublicIp`) — the Elastic/public IP(s) for the ENI, surfaced as `public_ips`

**EC2 Instance** (`.Reservations[].Instances[]`):
- `InstanceId`, `State.Name`, `InstanceType`, `Tags[]` (Name), `VpcId`, `SubnetId`

**ELBv2 Load Balancer** (`.LoadBalancers[]`):
- `LoadBalancerArn` — contains `:loadbalancer/app/<name>/<id>` (ALB) or `.../net/<name>/<id>` (NLB)
- `LoadBalancerName`, `Type` (`application` | `network` | `gateway`), `VpcId`, `DNSName`

**Classic ELB** (`.LoadBalancerDescriptions[]`):
- `LoadBalancerName`, `VPCId`, `DNSName`, `Subnets[]`

**Subnet** (`.Subnets[]`): `SubnetId`, `VpcId`, `CidrBlock`, `AvailabilityZone`, `Tags[]`

**VPC** (`.Vpcs[]`): `VpcId`, `CidrBlock`, `IsDefault`, `Tags[]`

**Security Group** (`.SecurityGroups[]`): `GroupId`, `GroupName`, `VpcId`, `Description`,
`IpPermissions[]` (**ingress** — what we depend on for reachability; egress is dropped). Each
`IpPermissions[]` entry has `IpProtocol` (`"-1"` = all traffic), `FromPort`/`ToPort`, and its
sources: `IpRanges[].CidrIp` (IPv4), `Ipv6Ranges[].CidrIpv6` (IPv6), and `UserIdGroupPairs[].GroupId`
(a referencing security group). An ENI's `Groups[].GroupId` (see above) says which SGs apply to it.

**NAT Gateway** (`.NatGateways[]`): `NatGatewayId`, `VpcId`, `SubnetId`, `State`,
`ConnectivityType` (`public`/`private`), `Tags[]` (Name), and `NatGatewayAddresses[]` — each with
`NetworkInterfaceId` (**the authoritative ENI-ownership signal**, §5.4) and `PublicIp`. A NAT
gateway owns one ENI per address it holds.

**VPC Endpoint** (`.VpcEndpoints[]`): `VpcEndpointId`, `VpcEndpointType`
(`Interface`/`Gateway`/`GatewayLoadBalancer`), `VpcId`, `ServiceName`, `State`, `Tags[]` (Name),
and `NetworkInterfaceIds[]` — the ENIs an **Interface**/**GatewayLoadBalancer** endpoint owns
(§5.4). A **Gateway** endpoint (S3/DynamoDB) owns **no** ENI (it is a route-table target), so its
list is empty.

**Route Table** (`.RouteTables[]`): `RouteTableId`, `VpcId`, `Associations[]` (`SubnetId`, `Main`
— the VPC's implicit fallback RT), `Routes[]` (`DestinationCidrBlock`/`DestinationIpv6CidrBlock`,
the next-hop id in one of `GatewayId` (`local`/`igw-`/`vgw-`), `NatGatewayId`, `TransitGatewayId`,
`VpcPeeringConnectionId`, … — normalized to one `Target` string — and `State`
(`active`/`blackhole`)). Used to decide whether a reachability source is actually **routed** to an
ENI (§5.6).

## 5. Relationship-mapping rules (THE CORE — Phase 2)

For each ENI, resolve **at most one** compute/LB attachment, plus its subnet and VPC.

### 5.1 ENI → Subnet  (always)
Edge `in_subnet` from ENI node to the subnet named by `NetworkInterface.SubnetId`.
Every ENI has a `SubnetId`. If the subnet isn't in the collected set, still create the edge
and mark the subnet node as `synthetic` / `unresolved` (metadata flag) — don't drop it.

### 5.2 Subnet → VPC  (always)
Edge `in_vpc` from subnet node to `Subnet.VpcId`. Same synthetic-node rule if a VPC is
missing from the collected set.

### 5.3 ENI → EC2 Instance
If `Attachment.InstanceId` is present **and** non-empty → edge `attached_to` from ENI to
that EC2 instance node. This is the unambiguous, preferred signal. When it's present, the
ENI is instance-attached and you do **not** also attribute it to a load balancer.

### 5.4 ENI → owner (NAT gateway / VPC endpoint / load balancer)
A service-managed ENI (no `Attachment.InstanceId`) belongs to some **owning resource**. The goal
is that **every** ENI resolves to an owner, not just instance- and LB-fronted ones. Resolve in
this priority order, attaching at most one owner and recording which rule fired in edge metadata
(`match_rule`):

1. **NAT gateway / VPC endpoint via the resource's own ENI list (authoritative).** These owners
   publish exactly which ENIs they hold, so we key on the ENI id directly — no fragile description
   parsing:
   - a NAT gateway's `NatGatewayAddresses[].NetworkInterfaceId` → edge `attached_to`
     (ENI → `nat_gateway` node), `match_rule = "nat_gateway_address"`.
   - a VPC endpoint's `NetworkInterfaceIds[]` → edge `attached_to` (ENI → `vpc_endpoint` node),
     `match_rule = "vpc_endpoint_interface"`.

   NAT gateways and VPC endpoints share the load balancer's **role class** — they move traffic in
   and out of the VPC — so they render into the same visual ring/layer as the load balancers
   (`§7`, `_ring_of`).

2. **ELBv2 (ALB/NLB/GWLB) via Description prefix.** ELBv2-owned ENIs have a `Description`
   shaped like:
   - ALB: `ELB app/<lb-name>/<lb-id>`
   - NLB: `ELB net/<lb-name>/<lb-id>`
   - GWLB: `ELB gwy/<lb-name>/<lb-id>`
   Extract the `app/<name>/<id>` (or `net/`, `gwy/`) token after `ELB `. Match it against the
   suffix of each ELBv2 `LoadBalancerArn` (the ARN ends with `:loadbalancer/app/<name>/<id>`).
   On match → edge `attached_to` (ENI → that load balancer), `match_rule = "elbv2_description"`.

3. **Classic ELB via Description.** Classic-ELB ENIs have `Description = "ELB <lb-name>"`
   (no `app/`/`net/` segment). Match `<lb-name>` against Classic `LoadBalancerName`.
   On match → edge `attached_to`, `match_rule = "classic_elb_description"`.

4. **InterfaceType fallback.** If `InterfaceType == "network_load_balancer"` or
   `"gateway_load_balancer"` but the description didn't resolve to a known LB, still create
   the LB-type attachment to an `unresolved` load balancer node keyed by the parsed name,
   and flag it. Record `match_rule = "interface_type_fallback"`.

If none of these fire, the ENI has **no** resolvable owner yet (e.g. RDS, Lambda, ElastiCache,
EFS mount-target ENIs — service-managed ENIs identified only by description/requester, without a
clean authoritative `describe-*` ENI list). That's expected — leave it attached only to its
subnet/VPC and tag the ENI node with its `InterfaceType` so the map still explains what it is. Do
**not** invent an attachment. These are the follow-up owners to add next (see `docs/05_roadmap.md`).

> **Edge-case guidance:** the NAT-gateway/VPC-endpoint ENI lists and the `ELB ` description format
> are the documented, stable attribution signals; verify against a real capture and note any
> account where they didn't hold. Never attribute an ENI to more than one owner — instance
> attachment (§5.3) always wins over every owner in this section.

### 5.5 ENI reachability — who can connect to it (security-group inbound rules)

Beyond *what an ENI is*, the map records *how each ENI is reachable*, from its security groups'
(`Eni.security_groups` → `SecurityGroup.ingress`) **inbound** rules. Reachability edges carry a
`ports` attribute summarising the protocol/port ranges (e.g. `"tcp/443"`, `"tcp/80, tcp/443"`,
`"all"` for the `-1` protocol). Load-balancer reachability rides this path with no special case: an
ALB / Classic-ELB ENI carries its LB's security groups in its own `Groups[]`, so the LB's inbound
rules flow in as the fronting ENI's sources. The pass is **always on** (independent of
`--include-orphans`). `build_graph(show_security_groups=...)` picks one of two shapes:

**Shown (default, `_map_reachability_via_sgs`).** Security groups are first-class nodes so the
source fan-out collapses through them:

* each ENI links to every SG it carries — edge `secured_by` (ENI → SG);
* each SG's inbound rules add a source per distinct allowance, linked to the SG — edge `can_reach`
  (source → SG). Source kinds (node `type`): **`internet`** (a `0.0.0.0/0`/`::/0` rule → a per-SG
  `internet:<sg-id>` node), **`cidr`** (any other range → a shared `cidr:<cidr>` node), and a
  **referencing security group** (a `UserIdGroupPairs[].GroupId` → that SG's own node, id the raw
  `sg-<id>`, so it is an SG → SG `can_reach` edge).

Routability (§5.6) is **not** represented in this shape — it is a *(source, ENI)* property and an
SG can front ENIs in different subnets.

**Hidden (`--no-security-groups`, `_map_reachability_direct`).** No SG node is emitted; only the
**IPs behind** the SGs are brought forward, connected **directly** to the ENIs, and each edge
carries the routability split (§5.6):

* **`internet`** — a per-ENI `internet:<eni-id>` node (never one shared Internet node — a single
  hub would collect a spoke from every internet-facing ENI, the crossings a per-ENI node avoids);
* **`cidr`** — a shared `cidr:<cidr>` node;
* a **peer-SG reference** is expanded to the **private IPs of that SG's member ENIs** (each a `/32`
  `cidr` node), so the concrete addresses a referencing group lets in are surfaced, not dropped.

An ENI with no security groups (or whose SGs weren't collected) gets no sources either way — never
invent one; shown mode still records its `secured_by` membership. The `cloudbreachgraph-to-html`
converter collapses a *shown* graph to the hidden shape after the fact via
`mapping/collapse.py::collapse_security_groups` (a view transform — it can only remove SG nodes, so
it no-ops on a graph that lacks `secured_by` membership, and its collapsed edges are plain
`can_reach` since a written graph carries no route data).

### 5.6 Routability — is the source actually routed to the ENI?

A security-group rule says a source is **allowed**; §5.6 asks whether a network path actually
**routes** it there. This split applies only in the **hidden** (`--no-security-groups`) shape,
where reachability edges point at ENIs; in the shown shape edges point at shared SG nodes, which
have no single per-ENI verdict. The edge's *relationship* carries the verdict, computed from the
ENI's **route table** (`mapping/routing.py`, `RouteResolver`):

* `routable_can_reach`     — allowed **and** a route exists.
* `not_routable_can_reach` — allowed but **no** route (e.g. a `0.0.0.0/0` rule on an ENI in a
  private subnet, or on one with no public IP).
* `can_reach`              — routability **undetermined**: no route tables were collected, or the
  ENI's subnet resolves to no route table. We keep the plain relationship rather than guess, so an
  old capture / a run without route-table permissions still produces reachability edges.

The ENI's effective route table is the one **explicitly associated** with its subnet, else the
VPC's **main** route table. The model is deliberately simple and documented (not a full route
simulator — NACLs, TGW route propagation, VPN/DX propagation are out of scope):

* **internet** source (`0.0.0.0/0` / `::/0`): routable iff the subnet is *public* (an active
  default route to an internet gateway `igw-`) **and** the ENI has a public/Elastic IP (so it is
  addressable from outside).
* **cidr** source: routable if the CIDR is inside the VPC (a `local` route always covers it), or a
  route explicitly covers it via a connective gateway (`vgw-`/`tgw-`/`pcx-`), or the ENI is
  internet-reachable as above; otherwise not routable. A peer-SG reference has already been
  expanded to its members' `/32` private IPs (§5.5), so it arrives here as an intra-VPC `cidr`
  (routable via the local route). `RouteResolver.classify` keeps a `security_group` branch as a
  defensive default, but the hidden shape no longer feeds it one.

### 5.7 Flow-log analysis — IP history + observed connections (`flow_logs` role)

The `network` rules above map what the topology *is*. The **`flow_logs`** role (opt-in via
`--flow-logs`; collectors in `aws/collectors.py`, mapping in `mapping/flowlogs.py`,
`build_graph(map_flow_logs=True)`) adds what the topology *did* — the traffic actually observed
to/from each ENI — plus where the logs that record it live. It reads three things and folds them
into the already-built graph:

1. **IP history.** `cloudtrail lookup-events` for `CreateNetworkInterface` gives *when* each ENI's
   private IP was allocated. Each ENI node gains an `ip_allocations` attribute
   (`[{ip, allocated_at}]`). The **earliest** allocation time is the per-ENI lower bound for the
   flow-log window: a flow record with a capture-window `start` *before* it is dropped — that
   traffic belonged to a **different interface reusing the address**, not this ENI.

2. **Flow-log configuration** (`ec2 describe-flow-logs`, the "where each VPC stores its logs"
   config). Per flow log: a `flow_log` node (attributes `resource_id`, `destination_type`,
   `traffic_type`, `status`); a **destination** node — `log_group` (CloudWatch Logs) or
   `log_bucket` (S3), keyed by the log-group name / S3 ARN; and edges `logs_to`
   (logged resource → flow_log, added only when the resource node exists so nothing dangles) and
   `delivers_to` (flow_log → destination).

3. **Observed connections** (`logs filter-log-events`, up to `FLOW_LOG_MAX_LOOKBACK_DAYS = 60` days
   of default-format records from each CloudWatch flow-log group). For each record captured on a
   collected ENI `A`, the *peer* end (the address that isn't one of `A`'s private IPs) becomes the
   other node of a directed **`connects_to`** edge — `peer → A` when `A` is the destination (*what
   connected to it*), `A → peer` when `A` is the source (*what it connects to*):
   - if the peer IP belongs to **another collected ENI `B`**, the edge runs **ENI → ENI** directly
     (`B → A` or `A → B`) — the acceptance-criteria "if the connecting IP belongs to another ENI,
     add an edge from one ENI to another";
   - otherwise the peer is an external **`flow_peer`** node (`flow-peer:<ip>`).
   Ports are aggregated per directed edge into a `ports` label (e.g. `tcp/443`), with
   `via = "flow_log"` so a `connects_to` edge is distinguishable from a reachability edge. Records
   with a missing address (`-`, e.g. NODATA/skipped) are dropped; the record's own `interface-id`
   (field 2) identifies the home ENI, and direction is decided by matching `srcaddr`/`dstaddr`
   against that ENI's private IPs.

**Scope & simplifications.** Only the **CloudWatch-Logs** record path is analysed for connections;
an S3 destination is shown as a `log_bucket` node but its objects are not fetched (that would need
per-object `s3api get-object`). All three commands run against the account bound to the `flow_logs`
role (§11) — which defaults to the same account as `network`, so the common single-account case
needs no config. Reading flow-log *records* (not just their config/destination) goes beyond the
original roadmap's "show the destination, don't parse traffic" line — a deliberate extension for
this feature. Determinism holds: allocation times and record timestamps come from the data, and the
60-day bound is applied at the *collection* query (not from wall-clock in the output), so a fixed
capture always yields the same graph.

## 6. Graph data model (Phase 2 defines, Phase 3 consumes)

A minimal, serialization-friendly model:

```
Node:
  id:    str            # eni-..., i-..., subnet-..., vpc-..., LB arn/name, or a reachability
                        #   source: internet:<eni>, cidr:<cidr>, sg-source:<gid>
  type:  str            # "eni" | "ec2_instance" | "load_balancer" | "nat_gateway"
                        #   | "vpc_endpoint" | "subnet" | "vpc"
                        #   | "security_group" | "internet" | "cidr"   (reachability, §5.5)
                        #   | "flow_log" | "log_group" | "log_bucket" | "flow_peer"  (flow logs, §5.7)
  label: str            # human-friendly (Name tag or id)
  attributes: dict      # type-specific metadata (state, cidr, interface_type, synthetic, ...)

Edge:
  source: str           # node id
  target: str           # node id
  relationship: str     # "attached_to" | "in_subnet" | "in_vpc" | "secured_by" (ENI->SG, §5.5)
                        #   | "can_reach" / "routable_can_reach" / "not_routable_can_reach" (§5.5/§5.6)
                        #   | "logs_to" / "delivers_to" / "connects_to" (flow logs, §5.7)
  attributes: dict      # e.g. {"match_rule": "elbv2_description"} or {"ports": "tcp/443"}

Graph:
  nodes: list[Node]     # unique by id
  edges: list[Edge]
  meta:  dict           # account id, region(s), generated_at, tool version
```

Requirements:
- Node ids are unique; adding a node that already exists merges attributes rather than
  duplicating.
- The graph must be deterministic (stable ordering) so JSON/DOT diffs are meaningful and
  tests are stable — sort nodes and edges before export.
- `Graph.to_dict()` returns a plain JSON-serializable structure; this is the **contract**
  Phase 3 depends on.

## 7. Output formats (Phase 3)

- **JSON** (`graph.json`): `Graph.to_dict()`, pretty-printed, stable ordering.
- **Graphviz DOT** (`graph.dot`): nodes grouped/colored by type; edge labels show the
  relationship (and `match_rule` for LB edges, `ports` for `can_reach` edges, when useful).
  Consider `subgraph cluster_*` per VPC so the layout groups subnets/ENIs (and `security_group`,
  `nat_gateway` and `vpc_endpoint` nodes, which are VPC-scoped) inside their VPC visually.
  `nat_gateway`/`vpc_endpoint` share the load balancer's role class, so they get the same
  `component` shape (distinct fills). Reachability (§5.5): with SGs shown each
  ENI has a dashed `secured_by` edge to its SG and the `internet`/`cidr` sources link to the SG;
  hidden, the sources link straight to the ENIs and the edge is colored by **routability** (§5.6):
  `routable_can_reach` solid red, `not_routable_can_reach` grey dashed, plain `can_reach` default.
  A public IP shows on the ENI's own `Public IP:` label line (this replaced the earlier
  public-IP-only shared `Internet` decoration).
- **Optional render:** if the `dot` binary is on PATH, offer `--render png|svg` that shells
  out to `dot -T<fmt>`. Absence of `dot` must degrade gracefully (still write the `.dot`).
- **Interactive HTML** (`graph.html`, opt-in via `--html`, *not* produced by default):
  a single **self-contained** page (`output/html_export.py`) — the graph is inlined as JSON
  and drawn on an HTML5 canvas by a small vanilla-JS force simulation that self-distributes
  the nodes (pairwise repulsion + edge springs + collision separation) so they don't
  overlap; supports drag/zoom/pan. Disconnected components (separate VPCs, orphans) repel
  each other (`CROSS_COMPONENT`) so segregated clusters settle apart. A **Recompute layout**
  button *refines the layout from its current positions* rather than re-solving it: it anchors
  each node to where it is (`n.ax`/`n.ay` + `ANCHOR`), re-anchors spring rest lengths to the
  current edge lengths, drops the centering gravity (`gravityScale = 0`) and applies only a
  gentle reheat (`RECOMPUTE_ALPHA`), so a hand-arranged layout is preserved while overlaps are
  resolved and clusters eased apart (a full reheat re-tangled it — that was a bug).
  **No** third-party runtime dependency and **no** network
  access (stays consistent with §1). The emitted HTML is byte-stable (nodes/edges pre-sorted,
  a seeded PRNG for the layout, no timestamps). Because an in-browser O(n²) force layout only
  stays responsive up to a point, `write_html` enforces a size guard (`MAX_NODES`,
  `MAX_HTML_BYTES`): over budget it writes nothing and returns `None`, and the CLI **warns
  and falls back to the always-written `.dot`** (which Graphviz lays out offline at any
  scale). `--html` accepts the same layout selectors as the converter below: `--optimize-passes N`
  swaps this in-browser force layout for the deterministic **overlap-free** layout
  (`write_optimized_html`), and `--ringed` selects the **ringed** layout (`write_ringed_html`, with
  `--optimize-passes` as its in-ring crossing-reduction budget). `--from-cache` and
  `--all-accounts` go through the same `_write_outputs`, so they get all three.
- **Converting existing output → HTML** (`cloudbreachgraph-to-html`, `convert.py`): an
  auxiliary console entry point that re-loads a previously written `graph.json`/`graph.dot`
  and renders the HTML view without re-collecting from AWS. Loading is the inverse of the
  writers and lives in `graph_io.py`: `load_json`/`graph_from_dict` is a **lossless** inverse
  of `Graph.to_dict()`; `load_dot` is a **best-effort** parser for *this tool's own* DOT
  (recovers node id/type/name, public/synthetic flags, the one display attribute per type,
  and every edge + `match_rule`/`ports`; reachability sources (`internet`/`cidr`/`security_group`,
  §5.5) round-trip as ordinary nodes. (Legacy `.dot` files with the old shared `Internet`
  decoration still fold back into `public_ips`.) The converter reuses the same `write_html` size
  guard and `.dot` fallback. Its `--ringed` flag selects an alternative **concentric-ringed** layout
  (`html_export.write_ringed_html`/`build_ringed_html`): each VPC is a cluster center, ringed
  by its subnets, then its ENIs on a dedicated ring, then everything else under that VPC (EC2
  instances, load balancers, NAT gateways, VPC endpoints), then a **security-group** ring, then a new **outermost** ring of the
  IP sources (`internet`/`cidr`, §5.5). The ENI ring is the angular anchor: each subnet is placed at
  the mean angle of the ENIs it contains (ENIs are grouped by subnet on their ring), each
  EC2/LB at the mean angle of the ENIs attached to it, each security group at the mean angle of the
  ENIs it secures, and each source at the mean angle of the ENIs it can reach (through its SG), so
  all stay radially next to their interfaces; orphan resources collect into a final ring-cluster
  (empty center). With `--no-security-groups` the SG ring is empty and the source ring nests onto
  the ENIs. Ring positions are
  computed deterministically in Python (no in-browser force sim), and the same `MAX_NODES`/
  `MAX_HTML_BYTES` guard and `.dot` fallback apply. The `--optimize-passes N` flag runs up to
  N barycenter passes (`html_export._optimize_cluster`) that move each node toward the mean
  angle of its neighbours, placed via an L2 isotonic min-gap projection (`_place_min_gap`) so
  connected nodes cluster as close as an overlap-free gap allows (not merely reordered), then
  nudge apart any residual overlaps. A geometric cooling schedule shrinks each pass's movement
  so the iteration freezes to a stable layout (otherwise it limit-cycles on dense graphs and the
  bytes would depend on the pass count). A final greedy crossing-reduction local search
  (`_reduce_crossings`) relocates each node to the same-ring slot with the fewest incident edge
  crossings — a monotone minimiser (moving one node only changes crossings on its own edges), it
  clears whole spokes the barycenter passes leave crossing. Rings preserved, output
  deterministic; `N=0` (default) is the exact ENI-aligned layout. Without `--ringed`, the same
  `--optimize-passes N` flag instead selects a third, **overlap-free** layout
  (`html_export.write_optimized_html`/
  `build_optimized_html`, sharing the draw-only template via `_render_static_layout`): it runs up
  to N deterministic *optimisation passes* over four phases (`_optimize_layout`/`_layout_nodes`):
  a cooled force-directed **unfolding** (`_OPT_FORCE_PASSES` cap), hard geometric **projection**
  sweeps that separate the disks/edges, a best-effort **crossing reduction**, and a final **label
  pass** — laying the whole graph out at once, then **rigidly translating** each **connected
  component** (`_connected_components`) into its own cell of a non-overlapping grid
  (`_pack_components`, mirroring the ringed cluster tiling, now sized to include label extents) so
  independent clusters stay visually separated — packing a component as a rigid body preserves its
  internal crossings/overlaps and there are no cross-component edges, so it keeps exactly the
  crossing count the joint layout found (better than optimising each component in isolation). It
  stops the moment the drawing has **zero node-node overlaps**, **zero edge-over-node overlaps**
  (a non-incident node's disk intersecting an edge segment) and **zero label overlaps** (a node's
  label rectangle intersecting another label or another node's disk — `_count_label_overlaps`
  verifies these, `_count_overlaps` the first two). A node's label is drawn just under its disk and
  is usually wider than it, so labels are cleared *after* the disks are laid out and de-tangled, by
  **uniformly inflating** the layout about its centroid — a transform that changes no edge crossing
  — until the label rectangles have room, then projecting them apart (`_separate_overlaps` with
  labels on; the inflation escalates if a projection can't reach zero). Because the labels are
  separated in *world* space, the page scales its label fonts with the view (`SCALE_LABELS` in the
  overlap-free variant of the draw-only template), so the clearance holds on screen at every zoom.
  Real topologies are non-planar (the example graph's largest VPC alone contains a non-planar
  minor), so zero edge *crossings* is impossible; this layout targets the overlaps that hurt
  legibility instead. The **crossing-reduction** phase (`_reduce_crossings_free`,
  `_count_crossings`) greedily relocates each crossing-incident node to the nearby candidate slot
  with the fewest incident crossings (a monotone move) and re-projects the disks; it is a
  *secondary* objective (crossings ~halve on the example graph, 39→18) that never sacrifices the
  overlap guarantees — it runs on the disk-only layout, and the crossings-preserving label
  inflation that follows keeps its result.
  `--optimize-passes` is unified across both layouts (ringed reduction with `--ringed`, overlap-free
  without) and both CLIs. The three-way choice lives in one place — `html_export.write_layout_html`
  (with the shared `RINGED_HELP`/`OPTIMIZE_PASSES_HELP` flag descriptions) — which both
  `cli._write_outputs` and `convert.main` call, so they can't drift; `N=0` (default) keeps the
  force/ringed layout. Same `MAX_NODES`/`MAX_HTML_BYTES` guard and `.dot` fallback. Its
  `--split-by-vpc` flag writes **one HTML per VPC** — `graph-<VPC ID>.html` in the `-o` directory
  (default: the input's directory) — via `html_export.split_by_vpc`, which partitions the graph on
  the same `_vpc_group_of` tracing the ringed layout clusters by: each sub-graph holds the nodes
  that resolve to that VPC plus the edges wholly within it (unassigned nodes and cross-VPC edges are
  dropped). It reuses `write_layout_html` per sub-graph, so the layout flags (`--ringed`/
  `--optimize-passes`/`--no-security-groups`) and the size guard / per-file `.dot` fallback all
  apply to every VPC file.
- **Anonymising existing output** (`cloudbreachgraph-anonymize`, `anonymize.py`): an auxiliary
  console entry point that rewrites a previously written `graph.json` into a scrubbed copy safe
  to share as a debugging/example graph. It **keeps every node and edge** but replaces all
  identifying *values* — resource ids, ARNs, IPv4 addresses/CIDRs, DNS names, 12-digit account
  ids, regions/AZs, hash tokens, and human names/labels — with random, **format-preserving**
  stand-ins (a private IP stays private, a `/24` stays a `/24`, an id keeps its prefix and
  suffix length, an AZ keeps its region-consistent letter). The invariant is **referential
  consistency**: `Anonymizer` scans every string value with an ordered regex battery (CIDR,
  IPv4, resource id, account, AZ, region, hex hash, digit run — overlaps resolved
  longest-first via per-string span consumption), treats any `id`/`label` with *no* pattern
  match as a human name, builds one **injective** source→replacement map (seeded by `--seed`
  for reproducibility), then rewrites every value in a **single left-to-right alternation pass**
  (longest token first) so a freshly-substituted value can never be re-scrambled. Because ARN
  and DNS *components* (account, region, name, hash) are each their own token, an ARN or DNS
  name is anonymised piecewise and stays consistent with the same tokens wherever else they
  appear (edge targets, ENI `Description`). Dict **keys** and non-string scalars are left
  untouched, so structural vocabulary (`type`, `relationship`, attribute keys, `match_rule`)
  survives verbatim. Output round-trips through `graph_from_dict` → `write_json`, so it's the
  same sorted/deterministic shape as every other writer. Read-only and AWS-free (local file
  I/O only). Known limitation: literal-substring replacement can over-match a human name that
  is also a substring of structural text (e.g. a VPC named `network`).

## 8. Regions

- Default: the single region from CLI config or `--region`.
- Stretch (only if cheap): `--all-regions` iterates `aws ec2 describe-regions` and tags each
  node with its region. If not implemented in v1, note it as future work in learnings.

## 9. Error handling & safety

- Read-only: the app must never call a mutating AWS API. Collectors only run `describe-*` and
  the read-only `sts get-caller-identity`.
- Fail loudly on auth/permission errors with the AWS CLI's stderr shown to the user.
- Partial data: if one collector fails but others succeed, prefer building a partial graph
  and clearly flagging what's missing over aborting — but make that behavior explicit and
  documented in learnings.

## 10. Account → profile mapping (how to target an account)

> **The `account` is the atom** (alias → account id + profile + region). This section covers the
> simple, common case: everything in one account. When resources for a single run live in
> **different** accounts — e.g. VPC flow logs in a central logging account, separate from the
> VPCs — see **§11 (resource roles & multi-account targets)**, which builds directly on this.

The operator keeps **one named AWS CLI profile per account**. CloudBreachGraph must let them
say "for account X, use profile Y" so they select an account without memorizing which profile
maps to it. There are two inputs, resolved in this precedence order (first match wins):

1. **`--profile <name>` (explicit CLI override).** Skips the mapping entirely and uses that
   profile directly. Always available as an escape hatch.
2. **`--account <id-or-alias>` resolved against the config file.** Looks up the account in the
   mapping and uses its `profile`.
3. **Neither given:** fall back to the AWS CLI's own default profile/credentials (no
   `--profile` flag passed), so the tool still works for someone with a single default account.

### 10.1 Config file

- **Format:** TOML (parsed with stdlib `tomllib`). Optional JSON support may mirror it.
- **Discovery order** when `--config` is not given: `./cloudbreachgraph.toml`, then
  `$XDG_CONFIG_HOME/cloudbreachgraph/config.toml` (default `~/.config/cloudbreachgraph/config.toml`).
  A missing config file is **not** an error unless `--account` was requested and can't be resolved.
- **Shape:** each account has a human alias (the table key), an `account_id`, a `profile`, and
  an optional default `region`:

```toml
# cloudbreachgraph.toml
default_account = "prod"        # optional: used when --account is omitted but a config exists

[accounts.prod]
account_id = "111111111111"
profile    = "prod-audit"
region     = "us-east-1"        # optional per-account default region

[accounts.staging]
account_id = "222222222222"
profile    = "staging-audit"

[accounts.sandbox]
account_id = "333333333333"
profile    = "sandbox-ro"
```

- `--account` accepts **either** an alias (`prod`) **or** a raw 12-digit account id
  (`111111111111`); resolve by matching either the table key or the `account_id` field.
- A canonical example ships at `docs/examples/cloudbreachgraph.example.toml`.

### 10.2 Resolution API (Phase 1 owns this; Phase 3 CLI consumes it)

`config.py` should expose roughly:

```python
def load_config(path: str | None) -> AccountConfig            # discovery + parse; empty if none
def resolve_profile(cfg: AccountConfig, *, account: str | None,
                    profile_override: str | None) -> Resolved  # -> {profile, account_id, region}
```

Where `Resolved.profile` may be `None` (meaning "use the CLI default"). The resolver applies
the precedence above and raises a clear error if `--account` was given but matches nothing in
the config.

### 10.3 Account verification (recommended)

After resolving a profile, run `aws sts get-caller-identity --profile <Y>` once and compare the
returned `.Account` to the expected `account_id` from the mapping. On mismatch, **stop** with a
clear error ("profile `prod-audit` resolves to account 999… but config says 111…") — this
prevents mapping a graph while unknowingly pointed at the wrong account. Make this a
`--verify-account/--no-verify-account` toggle (default on when an `account_id` is known). Record
the resolved/verified account id in `Graph.meta`.

### 10.4 Optional: map several accounts in one run

Because the operator has many per-account profiles, a `--all-accounts` flag may iterate every
account in the config, running the full collect→build→write pipeline per account and writing
per-account outputs (e.g. `graph.<alias>.json` / `graph.<alias>.dot`). This stays within the
single-account-per-graph model (no merged cross-account graph); it just loops. Treat it as a
Phase 3 stretch goal — if not built in v1, note it as future work in learnings.

## 11. Resource roles & multi-account targets (the flow-logs nuance)

Some data for a single logical environment lives in **different accounts**. The motivating
example: **VPC Flow Logs** are commonly published to a central **log-archive / logging
account** (CloudWatch Logs or an S3 bucket), separate from the workload account that owns the
VPCs, subnets, ENIs, and instances. To collect the full picture the tool must use **profile A**
for the networking resources and **profile B** for the flow logs — in the same run.

To express this the app introduces two concepts on top of §10's accounts:

### 11.1 Resource roles

A **role** is a named group of resources that are always fetched from the same account. Roles
form an extensible registry; new features add new roles without changing the config grammar.

| Role | Resources | Status |
|------|-----------|--------|
| `network` | ENIs, EC2 instances, load balancers, NAT gateways, VPC endpoints, subnets, VPCs, security groups, route tables (everything in §3 today) | **v1** |
| `flow_logs` | VPC Flow Log config + destinations (CloudWatch log groups / S3), IP-allocation history (CloudTrail), and analysed flow records → observed connections (§5.7) | **shipped** (opt-in via `--flow-logs`) |

Additional future roles (e.g. `dns`, `cloudtrail`) plug in the same way. See `05_roadmap.md`.

### 11.2 Targets — bind roles to accounts

A **target** is the thing you point the tool at: a named environment composed of one or more
accounts, one per role. It maps each role to an account alias from §10.

```toml
# accounts are still the atom (see §10)
[accounts.workload_prod]
account_id = "111111111111"
profile    = "prod-audit"
region     = "us-east-1"

[accounts.log_archive]
account_id = "999999999999"
profile    = "log-archive-ro"

# a target binds resource roles to accounts
[targets.prod]
default_account = "workload_prod"   # every role uses this unless overridden below
[targets.prod.roles]
flow_logs = "log_archive"           # ...but flow logs come from the central logging account

# a simple target that is entirely one account needs no role overrides
[targets.sandbox]
default_account = "workload_sandbox"
```

- `default_account` covers the ordinary "one account for everything" case; the `[targets.X.roles]`
  table overrides only the roles that live elsewhere. This keeps simple configs simple.
- A bare `--account <alias|id>` (from §10) is exactly a target whose every role resolves to that
  one account — backward compatible. `--target <name>` selects a multi-account target instead.
- `--profile <name>` still overrides **all** roles to that single profile (escape hatch).

### 11.3 Role-aware resolution API (generalizes §10.2)

`config.py` resolves to a **profile per role**, not a single profile:

```python
def resolve_target(cfg, *, target: str | None, account: str | None,
                   profile_override: str | None) -> ResolvedTarget
#   ResolvedTarget.roles: dict[str, ResolvedAccount]   # role -> {profile, account_id, region}
#   ResolvedAccount.profile may be None -> use the CLI default
```

The single-account `resolve_profile` from §10.2 becomes a thin wrapper: it resolves the
`network` role of a target built from `--account`/`--profile`. Precedence within each role:
`--profile` override → target's role binding / `default_account` → CLI default. The resolver
raises a clear error if a requested `--target`/`--account`/role can't be resolved.

### 11.4 Role-aware collection

The collection layer runs **per role**: for each role needed by the current command, resolve
its account's profile and run that role's collectors with it. The role→collectors binding is the
explicit registry in **§11.6**, and the exact driver loop is **§11.7**. In v1 only the `network`
role is active, so behavior is identical to §3 today — but adding `flow_logs` later is one new
registry entry ("register the role's collectors + let users bind it"), with **no** change to the
CLI grammar or the graph model. Record each role's resolved/verified account id in `Graph.meta`
so the map documents which account each part came from.

### 11.5 Verification with multiple accounts

Run the §10.3 `sts get-caller-identity` check **once per distinct resolved account** in the
target, comparing against each account's expected `account_id`. This catches a mis-bound role
(e.g. a `log_archive` profile that actually points at the workload account).

### 11.6 Role registry — how a role becomes actual `aws` commands

A role name resolves to real AWS CLI calls through an explicit **registry** that binds each role
to its set of collector functions. This is the single seam future roles plug into. Define it in
`aws/collectors.py` (or a small `aws/roles.py`) as data, not scattered logic:

```python
# aws/collectors.py  (Phase 1)

# Each collector is: collect_x(profile: str | None, region: str | None) -> list[dict]
# and internally calls runner.run_aws([...], profile=profile, region=region), which shells out to
#   aws <service> <describe-cmd> --region <r> --profile <p> --output json --no-cli-pager

ROLE_COLLECTORS: dict[str, list[Collector]] = {
    "network": [
        collect_network_interfaces,   # aws ec2   describe-network-interfaces  -> .NetworkInterfaces[]
        collect_ec2_instances,        # aws ec2   describe-instances           -> .Reservations[].Instances[]
        collect_load_balancers_v2,    # aws elbv2 describe-load-balancers      -> .LoadBalancers[]
        collect_load_balancers_classic,  # aws elb describe-load-balancers     -> .LoadBalancerDescriptions[]
        collect_subnets,              # aws ec2   describe-subnets              -> .Subnets[]
        collect_vpcs,                 # aws ec2   describe-vpcs                 -> .Vpcs[]
        collect_security_groups,      # aws ec2   describe-security-groups      -> .SecurityGroups[]
        collect_route_tables,         # aws ec2   describe-route-tables         -> .RouteTables[]
        collect_nat_gateways,         # aws ec2   describe-nat-gateways         -> .NatGateways[]
        collect_vpc_endpoints,        # aws ec2   describe-vpc-endpoints        -> .VpcEndpoints[]
    ],
    # ── future (Phase 4; do NOT implement in v1, see 05_roadmap.md) ───────────────
    # "flow_logs": [
    #     collect_flow_logs,          # aws ec2  describe-flow-logs             -> .FlowLogs[]
    #     collect_log_destinations,   # aws logs describe-log-groups / s3api ...
    # ],
}

# The output key each role writes into the collected bundle (see §11.7).
ROLE_RESULT_KEYS: dict[str, list[str]] = {
    "network": ["network_interfaces", "ec2_instances", "load_balancers_v2",
                "load_balancers_classic", "subnets", "vpcs", "security_groups", "route_tables",
                "nat_gateways", "vpc_endpoints"],
    # "flow_logs": ["flow_logs", "log_destinations"],  # future
}
```

Rules for the registry:

- **Adding a role is data, not control flow.** A new feature adds one entry to `ROLE_COLLECTORS`
  (+ its result keys) and writes the collectors — nothing in the CLI, config grammar, resolver,
  or graph model changes.
- Each collector takes only `(profile, region)` and returns normalized dicts; it must not know
  about roles, targets, or which account it's running against. That knowledge lives one level up.
- The registry is the authoritative list of what `network` (and later `flow_logs`) means —
  §11.1's table is the human summary; this dict is the machine-readable source of truth.

### 11.7 The collection loop (ties §11.3 + §11.6 together)

`collect_all` is the driver. Pseudocode:

```python
def collect_all(resolved: ResolvedTarget, *, roles: list[str] = ["network"]) -> dict:
    bundle = {"meta": {"target": ..., "region": ..., "accounts": {}}}
    for role in roles:                                    # v1: just ["network"]
        acct = resolved.roles[role]                       # {profile, account_id, region} (§11.3)
        collectors = ROLE_COLLECTORS[role]                # role -> collectors            (§11.6)
        keys       = ROLE_RESULT_KEYS[role]               # parallel result-bundle keys
        for collector, key in zip(collectors, keys):
            bundle[key] = collector(acct.profile, acct.region)   # -> aws ... via runner.py (§3)
        bundle["meta"]["accounts"][role] = acct.account_id       # record provenance       (§11.4)
    return bundle
```

So the path is always: **role → `resolved.roles[role]` (profile) + `ROLE_COLLECTORS[role]` (commands)
→ `collector(profile, region)` → `runner.run_aws(...)` → one `aws` subprocess.** In v1 the loop
runs a single iteration (`network`); binding `flow_logs` later just adds a second iteration that
happens to use a different account's profile.
