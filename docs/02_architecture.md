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
| IP-allocation history (`flow_logs`) | `aws cloudtrail lookup-events --lookup-attributes=AttributeKey=EventName,AttributeValue=CreateNetworkInterface --start-time=<now-90d>` | `.Events[].CloudTrailEvent` |
| Historical ENIs — reconstruction (`flow_logs`) | `aws cloudtrail lookup-events --lookup-attributes=AttributeKey=EventName,AttributeValue=<EventName> --start-time=<now-90d>` — one query each for `CreateNetworkInterface`, `RunInstances`, `DeleteNetworkInterface`, `TerminateInstances` | `.Events[].CloudTrailEvent` |
| Flow-log records — CloudWatch (`flow_logs`) | `aws logs filter-log-events --log-group-name=<g> --start-time=<ms>` | `.events[].message` |
| Flow-log records — S3 list (`flow_logs`) | `aws s3api list-objects-v2 --bucket=<b> --prefix=<p>` | `.Contents[].{Key,LastModified}` |
| Flow-log records — S3 object (`flow_logs`) | `aws s3api get-object --bucket=<b> --key=<k> <file>` | gzip body → lines |
| Caller identity (account check) | `aws sts get-caller-identity` | `.Account`, `.Arn` |

The `flow_logs`-role commands are opt-in (`--flow-logs`, §5.7). They are **read-only** retrievals —
`cloudtrail lookup-events` and `logs filter-log-events` retrieve, never mutate — even though their
verbs aren't the usual `describe`/`list`/`get`/`head` (the read-only guarantee is about *not
mutating*, §9). Value-carrying flags are passed in `--flag=value` form so the cache-key /
`--from-cache` file naming (which keys on the positional sub-command) stays stable. The
historical-ENI reconstruction issues one `cloudtrail lookup-events` **per EventName**, so the
`--from-cache` reader disambiguates them by the `EventName` in `--lookup-attributes`, serving
`cloudtrail_lookup-events.<eventname>.json` when present (else falling back to the un-suffixed
file). The flow-log-record window is configurable (`--flow-log-days N`, default 60); the CloudTrail
history window is always the full 90-day retention (never shorter than the record window), so a
flow captured on a now-terminated ENI can still be resolved to whichever ENI held its IP then.

The **only** non-read command the tool can issue is `aws sso login --profile <p>`, run **strictly in
reaction to an expired-token error** to refresh local credentials before retrying the run once
(§5.7, §9). It never mutates AWS resources and runs via a dedicated *interactive* runner entry
(`runner.sso_login`, stdio inherited, not captured).

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
   private IP was allocated. The lookback is bounded **explicitly** by `--start-time = now −
   FLOW_LOG_MAX_LOOKBACK_DAYS` (60 days), so the IP-history window matches the flow-log-record
   window rather than relying on CloudTrail's 90-day Event-history default. **Every** ENI node gains
   an `ip_history` attribute — a dict keyed by
   each IP the ENI has held, valued `{start, end}` (ISO timestamps): `start` is the allocation time
   (`None` if unknown), `end` is `None` while the ENI still holds the IP (its *current* addresses)
   else the allocation time of the IP that superseded it. The **earliest** known allocation is the
   per-ENI lower bound for the flow-log window: a flow record with a capture-window `start` *before*
   it is dropped — that traffic belonged to a **different interface reusing the address**, not this
   ENI. `ip_history` is a **JSON-only** field; the DOT/HTML views show only the ENI's current IPs.

2. **Flow-log configuration** (`ec2 describe-flow-logs`, the "where each VPC stores its logs"
   config). This is **not** modelled as separate nodes — it is a **`flow_logs` attribute on the
   owning VPC node**: a list of `{flow_log_id, resource_id, destination_type, destination,
   traffic_type, status}`. A flow log's `ResourceId` (VPC-, subnet-, or ENI-scoped) resolves *up to
   its VPC* (subnet via `in_vpc`, ENI via `in_subnet` then `in_vpc`), so all of a VPC's flow logs
   collect on that one VPC. A flow log whose VPC isn't in the (ENI-anchored) graph is skipped. Like
   `ip_history`, this config is **JSON-only** — it is never drawn in the DOT or HTML output.

3. **Observed connections** (up to `FLOW_LOG_MAX_LOOKBACK_DAYS = 60` days of records). `describe-flow-logs`
   says *where* each flow log delivers (`LogDestinationType`), and the collector **dispatches to the
   reader for that type** (`FLOW_LOG_READERS`) so it always pulls from the right source:
   - **CloudWatch Logs** (`cloud-watch-logs`): `logs filter-log-events` per log group.
   - **S3** (`s3`): `s3api list-objects-v2` under the destination ARN's bucket/prefix (filtered to
     `.gz` objects modified within the window), then `s3api get-object` on each, gunzipped and parsed.
   A destination type with **no implemented reader** (e.g. `kinesis-data-firehose`) raises
   `FlowLogDestinationError` — the run fails loudly rather than silently omitting those flows.
   Records are parsed by **field position derived from the format** — a CloudWatch group's own
   `LogFormat`, or (for S3) the **header row** each flow-log object carries; an absent/unrecognised
   format falls back to the default v2 layout, and a format missing a required field
   (`interface-id`/`srcaddr`/`dstaddr`) is skipped rather than misread. The collector prints a
   one-line **stderr diagnostic** — config counts by destination, items fetched per source, records
   parsed — so an empty result is explainable (usual remaining cause: a scope/permission mismatch).
   For each record captured on a
   collected ENI `A`, the *peer* end (the address that isn't one of `A`'s private IPs) becomes the
   other node of a directed **`connects_to`** edge — `peer → A` when `A` is the destination (*what
   connected to it*), `A → peer` when `A` is the source (*what it connects to*):
   - if the peer IP belongs to **another collected ENI `B`**, the edge runs **ENI → ENI** directly
     (`B → A` or `A → B`), with **no** new node — the acceptance-criteria "if the connecting IP
     belongs to another ENI, add an edge from one ENI to another". This is subject to a **temporal
     guard**: `B` must have already held that IP when the flow was captured. If `B`'s allocation of
     the IP (from CloudTrail) is *after* the record's `start`, the IP was a **different interface's**
     at the time (historic reuse) and the record is **dropped** — never linking a current ENI
     through a stale address. An *unknown* peer-allocation time (`B` predates the analysis window, so
     it has held the IP throughout) is treated as valid.
   - otherwise the peer is an external **`flow_peer`** node (`flow-peer:<ip>`).
   Ports are aggregated per directed edge into a `ports` label (e.g. `tcp/443`), with
   `via = "flow_log"` so a `connects_to` edge is distinguishable from a reachability edge. Records
   with a missing address (`-`, e.g. NODATA/skipped) are dropped; the record's own `interface-id`
   (field 2) identifies the home ENI, and direction is decided by matching `srcaddr`/`dstaddr`
   against that ENI's *current* private IPs (so a record whose home-side address the ENI no longer
   holds is naturally skipped too).

The one remaining flow-log node type, **`flow_peer`**, is an external IP source, so in the ringed
HTML layout it sits on the **outermost IP-source ring** alongside `internet`/`cidr` and is clustered
into the VPC of the ENI it exchanges flows with (traced via its `connects_to` edge). It has a
distinct fill colour from the other IP-source nodes in every HTML/DOT view.

**Resilient fetch (best-effort, both sources).** An account can hold thousands of flow-log
objects/groups, so a single failed AWS call must not abort the whole run. **Both** record readers
(`_read_s3_records` per S3 object, `_read_cloudwatch_records` per CloudWatch group) route every
failed **unit** through one shared classifier + retry wrapper in `aws/collectors.py`
(`_classify_aws_error` → `_ErrorTier`, `_run_unit`):

- **`RequestTimeTooSkewed` — clock vs network.** Fetch a **trusted external time** (stdlib only: an
  HTTPS `Date` header via `urllib`, falling back to an SNTP/NTP UDP query; `_trusted_time_offset`,
  ~5s timeout) and compare to the local UTC clock. If the offset exceeds the SigV4 tolerance
  (`_CLOCK_SKEW_TOLERANCE_S` = 900s) it's a **genuine clock problem** → abort with an actionable
  "sync your clock (macOS: System Settings → General → Date & Time)" message, no retry. Within
  tolerance, **or if the trusted time can't be fetched**, it's a likely **network stall** → retry.
- **Exponential backoff** (`_RETRY_BACKOFF`) for the skew-network case and for other **transient**
  errors (`RequestTimeout`/`SlowDown`/`Throttling`/5xx/`ServiceUnavailable`/connection resets): up
  to **3 retries, 30s → 60s → 120s**, each re-invoking a fresh `aws` subprocess so it re-signs with a
  current timestamp. Exhausted → warn + skip the unit.
- **`ExpiredToken`/`InvalidToken`/`TokenRefreshRequired`** → raise `CredentialsExpiredError`, which
  propagates to `cli.main`; the CLI (which holds the config) runs **`aws sso login --profile <p>`
  for every distinct profile** in the loaded `AccountConfig` (tolerating a non-SSO profile's error)
  and **retries the whole run once**. Still expired → exit non-zero with an "authenticate and re-run"
  message. `cli.is_expired_error` also converts an expired-token `AwsCliError` raised **anywhere**
  (e.g. a network collector) into the same re-login flow.
- **Systemic** (`AccessDenied`/`Forbidden`/`AuthorizationHeaderMalformed`, `SignatureDoesNotMatch`,
  a real clock skew) → raise `FlowLogFetchError` with a **source-aware** message (S3: needs
  `s3:ListBucket` + `s3:GetObject` on the bucket; CloudWatch: `logs:FilterLogEvents` on the group)
  and exit non-zero.
- **Skippable** (S3 `NoSuchKey`, CloudWatch `ResourceNotFoundException`, corrupt gzip, anything
  unclassified) → warn (naming the object/group) and skip.
- **Failure-rate safeguard** (`_FailureTracker`, both sources) tracks attempted vs failed units and
  aborts (`FlowLogFetchError`) if the first `_FAILURE_STREAK_ABORT` in a row fail or > 50% of a
  large-enough sample fail — so the tool never returns a silent near-empty graph. Skipped counts are
  reported in the stderr diagnostic.

Because skipping unreachable units makes the output depend on reachability, best-effort fetch is a
documented determinism caveat (consistent with §9's partial-graph stance). All timing
(`time.sleep`, the backoff, the trusted-time fetch) is mockable so the tests are fast and offline;
retries/backoff/re-login and all logging are **off** the graph path, so JSON/DOT/HTML stay
deterministic. `--verbose` (`runner.set_verbose`, mirroring the `configure_cache` pattern) echoes
every `aws` command actually run — including every `get-object`, each retry, and the `aws sso login`
calls — to **stderr** with a short OK/NOT OK, keeping stdout and the graph files clean.

**Scope & simplifications.** Both the **CloudWatch-Logs** and **S3** record paths are analysed
(destination type dispatched, above); a `kinesis-data-firehose` destination isn't implemented and
raises. All flow-log commands run against the account bound to the `flow_logs` role (§11) — which
defaults to the same account as `network`, so the common single-account case needs no config. The S3
reader lists under the destination prefix and filters by `LastModified`; for a very large bucket
that listing/download can be heavy (a future optimisation could narrow to date-partitioned prefixes).
Required read-only IAM adds `s3:ListBucket` + `s3:GetObject` on the destination bucket. Reading flow-log *records* (not just their config/destination)
goes beyond the original roadmap's "show the destination, don't parse traffic" line — a deliberate
extension for this feature. Determinism holds: allocation times and record timestamps come from the
data, and the 60-day bound is applied at the *collection* query (not from wall-clock in the output),
so a fixed capture always yields the same graph.

### 5.7.1 Historical ENIs + time-aware resolution (configurable window, ASG collapse)

`--flow-logs` only knows the ENIs that exist **right now**. In an Auto Scaling group, instances and
their ENIs are constantly replaced, so days of flow logs are full of records captured on
**terminated** ENIs and traffic to/from **reused** IPs. Four connected pieces fix this:

1. **Configurable flow-log window.** `--flow-log-days N` (default `FLOW_LOG_MAX_LOOKBACK_DAYS = 60`)
   sets how many days of *records* are read. A module-level setter (`collectors.set_flow_log_window`,
   mirroring `configure_cache`) threads it in without breaking the `collect_x(profile, region)`
   contract. `graph.meta` records both `flow_log_window_days` (configured) and `cloudtrail_window_days`.

2. **90-day CloudTrail history.** The historical-ENI reconstruction and IP-allocation history query
   the full `CLOUDTRAIL_MAX_LOOKBACK_DAYS = 90` (`min(90, max(days, 90))` — always 90, never shorter
   than the record window), independent of the record window, so a flow on a terminated ENI resolves
   even when `--flow-log-days` is small.

3. **Historical ENIs + time-aware resolution.** `collect_historical_enis` reconstructs the ENIs that
   existed in the window from CloudTrail — one `lookup-events` per EventName:
   `CreateNetworkInterface` (a standalone ENI), **`RunInstances`** (each instance's ENIs *and* its
   `aws:autoscaling:groupName` tag — most instance ENIs have no standalone create event),
   `DeleteNetworkInterface` / `TerminateInstances` (which set `deleted_at`, a terminated instance's
   deletion cascading to its ENIs). These merge by ENI id into a `HistoricalEni` with a lifetime
   `{created_at, deleted_at}`, `asg_name` and `instance_id`. The mapping builds a **combined ENI
   inventory** (current ∪ historical, keyed by ENI id) and a **time-indexed resolver**: an
   `(ip, record_epoch)` pair resolves to the ENI whose `[created_at, deleted_at]` lifetime contains
   the record's time *and* held the IP (tie-break: latest `created_at ≤ record_epoch`). This subsumes
   the old `ip_to_eni` dict + `ip_alloc_epoch` guard and disambiguates reused ASG IPs. A record's
   **home** is resolved by `interface-id` against the combined inventory (so a terminated home gets
   analysed, not dropped); the **peer** resolves through the time-indexed resolver → an ENI↔ENI edge
   to whichever ENI held the IP then (current or historical), falling back to `flow_peer` only when
   *no* ENI held it — and dropping the record (not inventing a peer) when the IP is otherwise
   internal. Referenced historical ENIs become graph nodes (`type: "eni"`, `historical: true`,
   `status: "terminated"`, `terminated_at`), placed in their subnet/VPC and styled **dashed/greyed**
   in DOT/HTML. Reconstruction is **on** with `--flow-logs` (`--no-historical-enis` opts out; point
   users at `--collapse-asgs` to keep the graph readable).

4. **ASG collapse (`--collapse-asgs`).** A graph **view transform** (`mapping/collapse.py`, same
   shape as `collapse_security_groups`), applied after the graph is built. It folds each Auto Scaling
   group's **members** — every EC2 instance in the group *and* every ENI attached to them, current
   **and** historical — into one `autoscaling_group` node (id `asg:<group-name>`). Membership is the
   `aws:autoscaling:groupName` tag (current instances via `describe-instances`, current ENIs via
   their instance, historical via the `RunInstances` `tagSet`). Edges re-point: an edge with one
   member endpoint moves that endpoint to the ASG node (direction preserved) then de-dups/merges
   parallels (`connects_to` unions `ports`; `in_subnet` keeps one edge per distinct member subnet, so
   the fleet nests in its VPC across AZs); an edge with both endpoints in the same group is a
   self-loop and is **dropped** (intra-fleet `connects_to`, every ENI→instance `attached_to`); a flow
   between two different groups becomes one ASG→ASG edge. Subnets, VPCs, security groups, reachability
   sources, `flow_peer`s and any non-ASG ENI/instance are untouched. The ASG node carries current vs
   historical member counts (instances and ENIs), the member subnets + VPC, the union of member
   private IPs, and a sample of instance ids. Deterministic and idempotent; a graph built without the
   flag is byte-for-byte unchanged.

## 6. Graph data model (Phase 2 defines, Phase 3 consumes)

A minimal, serialization-friendly model:

```
Node:
  id:    str            # eni-..., i-..., subnet-..., vpc-..., LB arn/name, or a reachability
                        #   source: internet:<eni>, cidr:<cidr>, sg-source:<gid>
  type:  str            # "eni" | "ec2_instance" | "load_balancer" | "nat_gateway"
                        #   | "vpc_endpoint" | "subnet" | "vpc"
                        #   | "security_group" | "internet" | "cidr"   (reachability, §5.5)
                        #   | "flow_peer"  (external peer seen in flow logs, §5.7; flow-log config
                        #                   is a `flow_logs` attribute on the vpc node, not a node)
                        #   | "autoscaling_group"  (a collapsed ASG fleet, --collapse-asgs, §5.7.1)
  label: str            # human-friendly (Name tag or id)
  attributes: dict      # type-specific metadata (state, cidr, interface_type, synthetic, ...);
                        #   a reconstructed, now-terminated ENI/instance carries `historical: true`
                        #   + `terminated_at` (§5.7.1), styled dashed/greyed in DOT/HTML

Edge:
  source: str           # node id
  target: str           # node id
  relationship: str     # "attached_to" | "in_subnet" | "in_vpc" | "secured_by" (ENI->SG, §5.5)
                        #   | "can_reach" / "routable_can_reach" / "not_routable_can_reach" (§5.5/§5.6)
                        #   | "connects_to" (observed flow-log connection, §5.7)
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
  (`write_optimized_html`), `--ringed` selects the **ringed** layout (`write_ringed_html`, with
  `--optimize-passes` as its in-ring crossing-reduction budget), and `--hierarchical` selects the
  **hierarchical** layout (`write_hierarchical_html`; takes precedence over `--ringed`).
  `--from-cache` and `--all-accounts` go through the same `_write_outputs`, so they get all four.
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
  connected nodes cluster as close as an overlap-free gap allows (not merely reordered). A geometric
  cooling schedule shrinks each pass's movement so the iteration freezes to a stable layout
  (otherwise it limit-cycles on dense graphs and the bytes would depend on the pass count). A greedy
  crossing-reduction local search (`_reduce_crossings`) relocates each node to the same-ring slot
  with the fewest incident edge crossings — a monotone minimiser (moving one node only changes
  crossings on its own edges), it clears whole spokes the barycenter passes leave crossing. When
  optimising, the layout is also made **fully overlap-free**: the rings are sized for the nodes'
  whole **labels** (`_label_ring_radii`), the barycenter min-gap is label-aware, and a final
  per-cluster **inflation about the centre + projection** (`_clear_cluster_overlaps`, a
  similarity transform that preserves crossings and the ring shape) drives node-node,
  edge-over-node, label-label and disk-over-label overlaps all to zero; the grown clusters are then
  tiled into a grid whose cells reserve room for the rings and labels. Because labels are then
  separated in world space, the ringed variant sets `SCALE_LABELS` on (scaling label fonts with the
  view) when `N > 0`. Rings preserved, output deterministic; `N=0` (default) is the exact
  ENI-aligned layout (disk-sized rings, fixed-size labels), byte-for-byte unchanged. Its
  `--hierarchical` flag selects a fourth, **hierarchical** layout
  (`html_export.write_hierarchical_html`/`build_hierarchical_html`, also on the shared draw-only
  template) that follows the ringed layout's rules but "unrolls" the concentric rings into
  **left/right columns**: a layer maps to a signed x-distance (the column, via `_hier_column_x`,
  which collapses empty layers as `_ring_radii` does) instead of a radius, and the ENI anchor maps
  to a y-position instead of an angle — ENIs spread down their column (grouped by subnet), every
  other node aligned to the mean y of its ENIs by an L2 isotonic **min-gap** projection
  (`_place_column`, the linear cousin of `_place_min_gap`). Two rules make it a hierarchy: (1)
  **connected nodes share a side** — a cluster's nodes are split into the connected components of
  its VPC-center-removed subgraph (`_partition_sides`) and each component is placed wholly left or
  wholly right, so no edge is ever drawn across the center between sides (only subnet→VPC center
  edges cross, terminating at the center); (2) the two sides are **balanced** by a greedy
  largest-first assignment. It reuses `_eni_anchor_maps` (the ENI-alignment maps, factored out of
  the ringed layout and shared by both) and `_vpc_group_of`; clusters are tiled into a grid whose
  cells reserve room for the columns and labels (`_hier_extent`), with a single half-height "ring"
  in each cluster's metadata so the template floats the VPC label above it. Its `--optimize-passes N`
  (N > 0) refinement mirrors the ringed reduction but exploits the column structure to make the
  guarantees *by construction*: the columns are spaced label-aware (`_hier_column_x_labeled`) and the
  rows label-aware (`_hier_row_gap`) so no two label rectangles can overlap — hence **zero node-node
  and zero label overlap** — and `_optimize_hier_cluster` runs up to N cooled barycenter sweeps (the
  layered-graph crossing-reduction heuristic; each node aimed at the mean y of its neighbours,
  re-placed via `_place_column`, the two sides optimised independently) to **cut edge crossings**,
  frozen by the same `_OPT_COOLING` schedule so a big N converges. When N > 0 the labels are
  separated in world space so `SCALE_LABELS` is set on; N = 0 (default) keeps the disk-sized columns
  and fixed-size labels, byte-for-byte unchanged. Unlike the ringed/overlap-free layouts it does not
  guarantee zero *edge-over-node* overlap (a column-skipping edge can pass over a node). Output
  deterministic. `--hierarchical` takes precedence over `--ringed` in `write_layout_html`. Without
  `--ringed`, the same
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
  `--hierarchical`/`--optimize-passes`/`--no-security-groups`) and the size guard / per-file `.dot`
  fallback all apply to every VPC file.
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

- Read-only: the app must never call a mutating AWS API. Collectors only run `describe-*`/`list-*`/
  `get-*`/`lookup-*`/`filter-*` retrievals and the read-only `sts get-caller-identity`.
  - **The one exception is `aws sso login`** — the *only* non-read command the tool ever issues. It
    runs **strictly in reaction to an expired-token error** (`ExpiredToken`/`InvalidToken`/…), never
    speculatively, for every profile in the loaded config, and then the run is retried once (§5.7).
    It **refreshes local credentials only** — it does not mutate any AWS resource — so the read-only
    guarantee ("never mutate") holds. It runs via a dedicated *interactive* runner entry
    (`runner.sso_login`) that inherits the terminal's stdio (it may print a device code / open a
    browser) rather than capturing it, unlike every other `aws` call.
- Fail loudly on auth/permission errors with the AWS CLI's stderr shown to the user. For flow-log
  fetch these are the **systemic** tier (`AccessDenied`/`SignatureDoesNotMatch`/genuine clock skew):
  abort with a source-aware, actionable message and a non-zero exit (§5.7).
- Partial data: if one collector — or one flow-log **unit** (an S3 object, a CloudWatch group) —
  fails but others succeed, prefer building a partial graph and clearly flagging what's missing
  (a stderr warning naming the object/group, plus skipped counts in the flow-log diagnostic) over
  aborting. This best-effort fetch is mandatory for **both** flow-log record readers and is bounded
  by a **failure-rate safeguard** that aborts rather than emit a silent near-empty graph (§5.7).
  Because skipping unreachable units makes output depend on reachability, this is a documented
  determinism caveat — the retries/backoff/re-login/logging themselves never affect the JSON/DOT/HTML.

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
