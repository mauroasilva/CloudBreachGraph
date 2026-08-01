# CloudBreachGraph

A read-only command-line tool that maps an AWS account's network topology — Network
Interfaces (ENIs) → their owner (EC2 instance, load balancer, NAT gateway or VPC endpoint) →
subnets → VPCs — using the **AWS CLI** (not boto3) as its data source. Output is a graph you
can serialize to JSON and render with Graphviz.

CloudBreachGraph is **read-only by construction**: it only ever runs AWS `describe-*`
calls plus the read-only `sts get-caller-identity` check. It never mutates your account.

## Install

```bash
pip install -e .
```

No required third-party runtime dependencies (stdlib `subprocess` + `tomllib`). The AWS
CLI v2 must be installed and on `PATH`, with a read-only profile per account. To rasterize
the graph to PNG/SVG you also need [Graphviz](https://graphviz.org/) (`dot`) on `PATH` —
this is optional; without it the tool still writes the `.dot` file.

### Updating to the latest version

After pulling new commits, reinstall so any new or changed console scripts (e.g.
`cloudbreachgraph-to-html`) and metadata are picked up:

```bash
git pull
pip install -e .          # or: pip install -e '.[dev]' to also refresh dev tools
```

Because the package is installed in editable mode (`-e`), day-to-day code changes take
effect without reinstalling — but a reinstall is required whenever `pyproject.toml` changes
(new entry points, dependencies, or version), which a `git pull` may include. When in doubt,
just rerun the command above; it's cheap and idempotent.

## Quick start

Build a map of an account and write `graph.json` + `graph.dot` into `out/`:

```bash
cloudbreachgraph --account prod --output-dir out/
```

Then render it (optional; needs Graphviz):

```bash
cloudbreachgraph --account prod --output-dir out/ --render svg   # writes out/graph.svg
# or, by hand:
dot -Tsvg out/graph.dot -o out/graph.svg
```

## Configuration

CloudBreachGraph maps each AWS account to one named AWS CLI profile, so you say **"for
account X, use profile Y"** without memorizing which profile is which. Copy the shipped
example and edit it:

```bash
cp docs/examples/cloudbreachgraph.example.toml ./cloudbreachgraph.toml
```

Discovery order when `--config` is not given: `./cloudbreachgraph.toml`, then
`$XDG_CONFIG_HOME/cloudbreachgraph/config.toml` (default
`~/.config/cloudbreachgraph/config.toml`).

```toml
# cloudbreachgraph.toml
default_target = "prod"          # used when neither --target nor --account is given

# accounts are the atom: alias -> { account_id, profile, region? }
[accounts.workload_prod]
account_id = "111111111111"
profile    = "prod-audit"
region     = "us-east-1"

[accounts.log_archive]           # a central logging account (for future flow_logs)
account_id = "999999999999"
profile    = "log-archive-ro"

# a target binds resource roles to accounts (a named environment)
[targets.prod]
default_account = "workload_prod"   # every role uses this...
[targets.prod.roles]
flow_logs = "log_archive"           # ...except flow logs, from the logging account (future role)
```

## Selecting an account (precedence)

First match wins (`docs/02_architecture.md §10–§11`):

| Flag | Meaning |
|------|---------|
| `--profile <name>` | **Escape hatch.** Use this AWS CLI profile directly for every role, ignoring the config mapping. |
| `--target <name>` | Select a config target that binds each resource *role* to an account (may span multiple accounts). |
| `--account <alias\|id>` | Shorthand: a target whose every role is that one account (alias **or** 12-digit id). |
| *(none)* | Fall back to `default_target` / `default_account` in the config, else the AWS CLI default credentials. |

```bash
cloudbreachgraph --target prod                 # multi-account target (roles bound per config)
cloudbreachgraph --account workload_prod       # one account, by alias
cloudbreachgraph --account 111111111111        # one account, by id
cloudbreachgraph --account workload_prod --profile some-other-profile   # --profile wins
```

After resolving, the tool runs `sts get-caller-identity` once per distinct profile and
checks the returned account against the config (`--verify-account`, default **on** when the
account id is known). It stops with a clear error on a profile/account mismatch. Disable
with `--no-verify-account`; note verification is a no-op under `--profile` (no expected id).

## Offline: build from cached JSON

`--cache-dir DIR` writes each raw AWS JSON response to disk during a live run, and (under
`--flow-logs`) **caches the downloaded S3 flow-log objects** there too — they're immutable, so a
cached body under 30 days old is reused with no re-download, making repeat runs far cheaper.
`--from-cache DIR` then rebuilds the graph from those files with **no** live AWS calls —
handy for iterating on output, diffing over time, or working from a colleague's capture:

```bash
cloudbreachgraph --account prod --cache-dir captures/       # live run, also caches raw JSON
cloudbreachgraph --from-cache captures/ --output-dir out/   # offline rebuild, no AWS calls
```

`--from-cache` also reads the repo's recorded fixtures directly, so you can try it with no
AWS account at all:

```bash
cloudbreachgraph --from-cache tests/fixtures --output-dir out/
```

## All flags

```
--target NAME              config target binding roles to accounts
--account ALIAS|ID         account alias or 12-digit id (shorthand target)
--profile NAME             AWS CLI profile override (applies to all roles)
--config PATH              path to the TOML config file
--verify-account /         toggle the sts get-caller-identity check
  --no-verify-account        (default: on when the account id is known)
--all-accounts             loop over every configured account, one graph each
--region REGION            AWS region (overrides the per-account default)
--cache-dir DIR            also write raw AWS JSON responses here, and cache downloaded
                             S3 flow-log objects (reused for 30 days, so a re-run won't
                             re-download them)
--from-cache DIR           build from cached AWS JSON in DIR, no live calls
--include-orphans          also emit collected resources no ENI references
--flow-logs                also gather IP-allocation history and analyse VPC flow
                             logs (last 60 days), reading records from CloudWatch
                             Logs or S3 per the flow log's destination type: record
                             flow-log config on the VPC and, per observed connection,
                             add a connects_to edge to the peer (ENI->ENI when the
                             peer IP is another ENI that held it at the time, else a
                             flow_peer node). Needs extra read-only IAM
                             (ec2:DescribeFlowLogs, cloudtrail:LookupEvents,
                             logs:FilterLogEvents, s3:ListBucket + s3:GetObject)
--flow-log-days N          how many days of flow-log records to analyse (default 60;
                             only with --flow-logs). The 90-day CloudTrail history used
                             to reconstruct historical ENIs is unaffected (always the
                             full retention, never shorter than this window)
--historical-enis /        with --flow-logs, reconstruct ENIs that existed in the last
  --no-historical-enis       90 days from CloudTrail so a flow on a now-terminated ASG
                             ENI is analysed and its peers resolve to the ENI that held
                             the IP at the time (default: on; --no-historical-enis off)
--collapse-asgs            collapse each Auto Scaling group's instances and ENIs
                             (current + historical) into one autoscaling_group node,
                             merging their edges (tames a churning fleet's fan-out)
--security-groups /        show security groups as nodes between ENIs and their
  --no-security-groups       sources (default: on); --no-security-groups connects the
                             source IPs directly to the ENIs with routability
--verbose / -v             echo every aws command actually run (incl. every get-object,
                             retries, and any aws sso login) to stderr with a short
                             OK/NOT OK result; stdout and the graph files stay clean
--output-dir DIR           where to write outputs (default: .)
--render {png,svg}         also rasterize the .dot with Graphviz (needs `dot`)
--html                     also write an interactive, self-contained HTML view
                             (falls back to .dot when the graph is too large)
--ringed                   with --html, render the concentric-ringed layout
--hierarchical             with --html, render the hierarchical layout (VPC-centered
                             columns fanning left/right; connected nodes share a side,
                             sides balanced). Takes precedence over --ringed
--optimize-passes N        with --html, run up to N optimisation passes: overlap-free
                             layout (no node/edge/label overlap), or ringed /
                             hierarchical crossing-reduction with --ringed /
                             --hierarchical (default 0 = base force/ringed/
                             hierarchical layout)
```

## Outputs

- `graph.json` — the full graph (`meta`, `nodes`, `edges`), pretty-printed and
  deterministic (stable ordering, no timestamps) so diffs are meaningful.
- `graph.dot` — Graphviz DOT: nodes colored/shaped by type, subnets and ENIs grouped
  inside their VPC via `subgraph cluster_*`, edges labeled by relationship (load-balancer
  attachment edges also show the `match_rule` that resolved them; reachability edges show the
  `ports` that reach the ENI). ENI labels include their `Private IP` and `Public IP` (when the
  ENI has an Elastic/public IP). **Reachability** source nodes (see below) — `Internet`, source
  CIDRs, and referencing security groups — link to the ENIs they can connect to, colored by
  **routability**: routable edges solid red, not-routable ones grey/dashed.
- `graph.<fmt>` — only with `--render`; requires `dot`. If `dot` is absent the tool warns
  and still writes the `.dot`.
- `graph.html` — **only with `--html`** (never produced by default). A single,
  **self-contained** HTML page (no CDN, no external assets) that draws the graph on an
  HTML5 canvas with a small vanilla-JavaScript **force layout**: nodes self-distribute
  (repulsion + edge springs + collision separation) so they don't sit on top of each other,
  and disconnected clusters (e.g. separate VPCs) repel each other so they settle apart rather
  than mingling. Drag a node to pin it, scroll to zoom, drag the background to pan. **Zoom
  In / Zoom Out** buttons zoom about the center, and a **lock scroll-zoom** toggle disables
  wheel zoom so only those buttons change the zoom. A
  **Recompute layout** button *gently tidies the layout from wherever the nodes are now* — it
  anchors each node to its current position and only relieves local crowding (resolving
  overlaps, easing clusters apart), so a layout you arranged by hand is preserved rather than
  re-solved into a fresh tangle; click again to tidy further. Nodes are colored by type and
  ENIs with a public IP get a red "exposed" outline. Add **`--optimize-passes N`** (see below)
  to write the deterministic **overlap-free** layout instead of this in-browser force layout —
  positions are computed in Python so the page opens already settled, with no overlapping
  nodes, no edge crossing a node, no label overlapping another label or node, fewer edge
  crossings, and independent clusters kept apart. Or pass **`--ringed`** for a VPC-centered
  concentric-ring layout, or **`--hierarchical`** for a VPC-centered left/right-column tree
  (both described below).
  For very large graphs an
  in-browser force layout stops being responsive, so if the graph exceeds the render budget
  the tool **warns and skips the HTML**, pointing you at the always-written `.dot` (which
  Graphviz can lay out offline at any scale). Just open the file in any browser — it works
  fully offline.

With `--all-accounts` the files are named per account: `graph.<alias>.json` / `.dot`
(and `.html` with `--html`).

## ENI ownership (who owns each interface)

Every ENI is attributed to the resource that **owns** it, so the map has no ownerless interfaces.
Each ENI attaches to **at most one** owner, resolved in priority order (the rule that matched is
recorded on the edge as `match_rule`):

| Owner | Node type | How it's attributed |
|-------|-----------|---------------------|
| EC2 instance | `ec2_instance` | `Attachment.InstanceId` (wins over everything) |
| NAT gateway | `nat_gateway` | `aws ec2 describe-nat-gateways` → `NatGatewayAddresses[].NetworkInterfaceId` |
| VPC endpoint (Interface / GWLB) | `vpc_endpoint` | `aws ec2 describe-vpc-endpoints` → `NetworkInterfaceIds[]` |
| Load balancer (ALB/NLB/GWLB/Classic) | `load_balancer` | ENI `Description` (`ELB app/…`, `ELB <name>`) |

NAT gateways and VPC endpoints are attributed from each resource's **own authoritative ENI list**
(no fragile description parsing), and — because they move traffic in and out of the VPC, a role
much like a load balancer's — they render into the **same visual ring/layer as the load
balancers**. A handful of service ENIs (RDS, Lambda, EFS, …) still have no owner because AWS
offers no clean ENI-ownership list for them yet; they stay attached to just their subnet/VPC,
tagged with their `InterfaceType`. See `docs/05_roadmap.md`.

## ENI reachability (who can connect)

Beyond mapping what each ENI *is*, CloudBreachGraph maps **how each ENI is reachable**. It reads
every ENI's security-group **inbound** rules (pulled with `aws ec2 describe-security-groups`) and
turns the sources they allow into graph nodes. Because an ALB / Classic-ELB ENI carries its load
balancer's security groups, **load-balancer** exposure is captured through this same path
automatically. Edges are labelled with the protocol/port ranges that reach the target (e.g.
`tcp/443`, `all`).

### Security groups shown (default)

By default security groups are **nodes** sitting between each ENI and the sources that can reach
it — so the fan-out collapses through them (a CIDR that reaches 20 ENIs sharing one SG is a single
spoke to that SG, not 20 spokes):

- each ENI links to every SG it carries — a `secured_by` edge (ENI → SG);
- each SG's inbound rules add sources linked to the SG — a `can_reach` edge (source → SG):
  - **`Internet`** — a rule open to `0.0.0.0/0` / `::/0`, one node **per SG** (`internet:<sg-id>`);
  - **`cidr`** — any other source range (`cidr:<cidr>`), shared across SGs;
  - a **peer security group** — that SG's own node, giving an SG → SG `can_reach` edge.

### Security groups hidden (`--no-security-groups`)

Pass `--no-security-groups` to drop the SG nodes and bring **only the IPs behind them** forward,
connected **directly** to the ENIs. Here the reachability edge also carries a **routability**
verdict, computed from each ENI's **route table** (`aws ec2 describe-route-tables`):

- **`routable_can_reach`** — allowed **and** a route exists (in DOT: solid **red**);
- **`not_routable_can_reach`** — allowed but **no** route, e.g. a `0.0.0.0/0` rule on an ENI in a
  private subnet or with no public IP (in DOT: **grey dashed**) — the classic "the security group
  looks wide open, but the ENI isn't actually reachable" case;
- **`can_reach`** — routability **undetermined** (no route tables collected).

A **peer-SG reference** is expanded to the private IPs of that SG's member ENIs (each a `/32`
`cidr`), so the actual addresses a referencing group lets in are surfaced rather than dropped.
Routability lives only in this view: it is a per-ENI property, so a shared SG node (which can front
ENIs in different subnets) has no single verdict.

The verdict uses a deliberately simple, documented model (not a full route simulator): an
`internet` source is routable only from a **public** subnet (a default route to an internet
gateway) *and* an ENI with a public IP; an in-VPC CIDR or a peer security group is routable via the
local route; an external CIDR is routable over a VPN / transit-gateway / peering route or the
internet path. See [`docs/02_architecture.md §5.6`](docs/02_architecture.md).

These nodes/edges appear in **all** outputs (JSON, DOT, HTML). In the
[ringed layout](#ringed-layout---ringed) security groups form a ring just outside the ENIs and the
IP sources form the **outermost ring**. `cloudbreachgraph-to-html --no-security-groups` collapses
the SG layer of an existing graph too (it can only remove SG nodes, not re-collect them).

## Flow-log analysis (`--flow-logs`)

Beyond the *static* topology, CloudBreachGraph can map the **observed traffic** to and from each
ENI by reading the account's **VPC Flow Logs**. Pass `--flow-logs` (off by default — it needs extra
read-only permissions and reads recent log data) to add, all read-only:

- **IP history.** For each ENI it looks up (via CloudTrail `CreateNetworkInterface` events) **when
  its private IP was allocated**, and records the full history on the ENI node as an **`ip_history`**
  attribute — `{ip: {start, end}}`, where `end` is `null` while the ENI still holds the IP, else the
  time it was superseded. That allocation time bounds how far back that ENI's flow logs are analysed
  (traffic seen *before* the IP was allocated belonged to a different interface reusing the address,
  so it's dropped). `ip_history` is written to **`graph.json` only** — the DOT/HTML views show just
  the ENI's *current* private IP.
- **Where the logs live.** From `ec2 describe-flow-logs` it records *where each VPC stores its logs*
  as a **`flow_logs` attribute on the VPC node** (destination log group / S3 bucket, traffic type,
  status) — VPC-, subnet- and ENI-scoped flow logs all roll up to their VPC. There are no separate
  flow-log nodes, and this config is written to **`graph.json` only** — it is not drawn in the DOT or
  HTML views.
- **What connected to what.** It reads up to **60 days** of flow records (configurable with
  `--flow-log-days N`) from wherever each flow log delivers — **CloudWatch Logs** (`logs filter-log-events`) or **S3** (`s3api list-objects-v2` +
  `get-object` on the gzipped objects), dispatched by the flow log's `LogDestinationType`. A
  destination type with no reader implemented (e.g. `kinesis-data-firehose`) raises an error rather
  than silently skipping. For every observed connection it adds a directed **`connects_to`** edge to
  the peer:
  - if the peer IP belongs to **another ENI in the account**, the edge runs **ENI → ENI** directly
    (no new node — so the map shows which interfaces actually talk to each other). This only happens
    when that ENI **already held the IP at the time of the flow**: if the IP was allocated to it
    *later*, the record is a historic-IP reuse and is dropped, so a current ENI is never linked
    through an address it didn't own then;
  - otherwise the peer is an external **`flow_peer`** node (`flow-peer:<ip>`), which shares the
    outermost IP-source ring with the reachability sources in the ringed layout.
  Edges are labelled with the protocol/port ranges observed (e.g. `tcp/443`), and direction
  encodes meaning: `peer → ENI` is *what connected to it*, `ENI → peer` is *what it connects to*.

```bash
cloudbreachgraph --account prod --flow-logs --output-dir out/
cloudbreachgraph --from-cache tests/fixtures --flow-logs --output-dir out/   # offline demo
```

Both **CloudWatch-Logs** and **S3** flow-record paths are read (chosen per flow log's destination
type). All flow-log data is read from the account bound to the `flow_logs` role — which defaults to
the same account as `network`, so in the common single-account case `--flow-logs` "just works" (see
`docs/02_architecture.md §5.7`). Reading S3 records needs `s3:ListBucket` + `s3:GetObject` on the
destination bucket, in addition to `ec2:DescribeFlowLogs`, `cloudtrail:LookupEvents` and
`logs:FilterLogEvents`.

### Historical ENIs & ASG collapse

`--flow-logs` only knows the ENIs that exist **right now**. In an Auto Scaling group, instances and
their ENIs are churned constantly, so days of flow logs are full of records captured on
**terminated** ENIs and traffic to/from **reused** IPs. To handle this:

- **90-day CloudTrail reconstruction (on by default with `--flow-logs`).** CloudBreachGraph rebuilds
  the ENIs that existed in the last 90 days from CloudTrail (`RunInstances` — which also carries each
  instance's `aws:autoscaling:groupName` tag — plus `CreateNetworkInterface` /
  `DeleteNetworkInterface` / `TerminateInstances`). These merge with the current ENIs into one
  inventory with per-ENI lifetimes. A terminated ENI becomes a graph node flagged `historical` /
  `terminated` (drawn **dashed and greyed**). Turn it off with `--no-historical-enis`.
- **Time-aware IP → ENI resolution.** Each flow record's peer IP resolves to whichever ENI held that
  IP **at the record's timestamp** — so a flow captured on a now-terminated ASG ENI is analysed (not
  dropped), and a reused ASG IP is attributed to the ENI that actually owned it then, giving an
  **ENI ↔ ENI** edge rather than a `flow_peer` whenever the peer was an ENI in the window.
- **Configurable window.** `--flow-log-days N` (default 60) changes how many days of *records* are
  read; the CloudTrail history always reaches its 90-day max (never shorter than the record window).
  Both windows are recorded in `graph.meta` (`flow_log_window_days`, `cloudtrail_window_days`).
- **`--collapse-asgs`.** A churning fleet produces a lot of historical ENIs. This flag collapses each
  Auto Scaling group's instances **and** ENIs (current + historical) into a single
  `autoscaling_group` node (`asg:<name>`), re-pointing and merging their edges: external edges move
  onto the ASG node (ports unioned; one `in_subnet` per AZ subnet the fleet used), intra-fleet edges
  (including every ENI→instance attachment) drop, and a flow between two ASGs becomes a single
  ASG→ASG edge. The node carries current-vs-historical member counts, the member subnets/VPC, and the
  union of member private IPs. Deterministic; a graph built without the flag is byte-for-byte
  unchanged.

```bash
# 45 days of records, historical ENIs reconstructed, fleets collapsed (offline demo):
cloudbreachgraph --from-cache tests/fixtures --flow-logs --flow-log-days 45 --collapse-asgs --output-dir out/
```

### Resilient fetch (best-effort, with smart recovery)

Because an account can hold **thousands** of flow-log objects/groups, a single failed AWS call must
not abort the whole run. Both record readers (S3 per object, CloudWatch per log group) route every
failed **unit** through one shared policy (`docs/02_architecture.md §5.7`, §9):

- **Corrupt / missing / absent unit** (S3 `NoSuchKey` or a bad gzip; CloudWatch
  `ResourceNotFoundException`) → **warned on stderr and skipped**; the run finishes and builds the
  graph from the rest.
- **Clock-skew (`RequestTimeTooSkewed`)** → the tool asks a **trusted external clock** (an HTTPS
  `Date` header, falling back to an SNTP/NTP query — both stdlib) whether your clock is really off.
  Beyond ~15 min it's a **genuine clock problem**: it aborts with an actionable "sync your clock"
  message (no retry). Within tolerance, or if the trusted time can't be fetched, it's treated as a
  **network stall** and **retried with exponential backoff** (up to 3 retries: 30s → 60s → 120s,
  each re-signing a fresh request); still failing → skip that unit.
- **Other transient errors** (`RequestTimeout`, `SlowDown`, `Throttling`, 5xx / `ServiceUnavailable`,
  connection resets) → the same **3× exponential backoff**, then skip on exhaustion.
- **Expired credentials** (`ExpiredToken` / `InvalidToken` / …) → the tool runs **`aws sso login`
  for every profile in your config** (the *only* non-read command it ever issues, gated strictly on
  an expired-token error; it refreshes local credentials, never touches AWS resources) and then
  **retries the whole run once**. A non-SSO profile whose login errors is tolerated (warned).
- **Systemic errors** (`AccessDenied`, `SignatureDoesNotMatch`, a real clock skew) → **abort** with
  a clear, source-aware message (e.g. "the profile needs `s3:ListBucket` + `s3:GetObject` on bucket
  X" / "`logs:FilterLogEvents` on group Y") and a non-zero exit.
- **Failure-rate safeguard** — if too many units fail (the first several in a row, or a majority
  overall), the run **aborts** rather than silently produce a near-empty graph. Skip counts are
  reported in the flow-log stderr diagnostic.

Skipping unreachable units makes the output depend on reachability — inherent to best-effort, and
consistent with the tool's "partial graph over aborting" stance (`docs/02_architecture.md §9`). All
retry/backoff, re-login and stderr logging happen **off** the graph path, so JSON/DOT/HTML output
stays byte-for-byte deterministic. Add **`--verbose`** to watch every command, retry and login as it
happens.

## Converting an existing graph to HTML

Already have a `graph.json` or `graph.dot` from an earlier run (e.g. from `--from-cache`, a
colleague's capture, or a run without `--html`)? The auxiliary `cloudbreachgraph-to-html`
tool renders the same interactive HTML view from it — **no AWS calls, purely local**:

```bash
cloudbreachgraph-to-html out/graph.json                 # writes out/graph.html
cloudbreachgraph-to-html out/graph.dot -o topology.html # explicit output path
cloudbreachgraph-to-html capture.data --format json     # force the input format
cloudbreachgraph-to-html out/graph.json --ringed        # concentric-ringed layout
cloudbreachgraph-to-html out/graph.json --optimize-passes 10000  # overlap-free layout
cloudbreachgraph-to-html out/graph.json --no-security-groups  # collapse the SG layer
cloudbreachgraph-to-html out/graph.json --collapse-asgs       # collapse ASG fleets
cloudbreachgraph-to-html out/graph.json --split-by-vpc -o out/  # one HTML per VPC
```

`--no-security-groups` collapses the security-group layer of the loaded graph, bringing the source
IPs forward to connect directly to the ENIs. It can only **remove** SG nodes already present in the
input (it never re-collects from AWS), so on a graph that was built with `--no-security-groups` it
is a no-op.

`--collapse-asgs` folds each Auto Scaling group's instances and ENIs (current + historical) into a
single `autoscaling_group` node, re-pointing and merging their edges — the same view transform the
main tool's `--collapse-asgs` applies, but on an already-written graph. Like `--no-security-groups`
it only reshapes what's already in the input, so it needs a graph the main tool built with
`--flow-logs` (which records the `aws:autoscaling:groupName` membership); on a graph with no ASG
membership it is a no-op. Both transforms compose, and both apply to every per-VPC file under
`--split-by-vpc`.

No capture of your own? A shipped, fully **anonymised** example graph (a real-shaped
multi-VPC topology — 4 VPCs, 28 subnets, 60 ENIs, 19 load balancers; all names, IDs and IPs
randomised) lets you try the tool with no AWS account at all:

```bash
cloudbreachgraph-to-html docs/examples/example-graph.json --ringed -o example.html
```

- **From JSON** the conversion is **lossless** — it reproduces exactly the page `--html`
  would have written.
- **From DOT** it's **best-effort** (DOT is a lossy rendering, and only *this tool's own*
  `.dot` is understood): node ids/types/names, the public-exposure and unresolved flags, the
  per-type detail (interface/LB type, CIDR, instance state), and every edge are recovered.
- The same size guard applies: if the graph is too large for a browser force layout, it warns
  and writes a `.dot` fallback instead (skipping it if the input already is that `.dot`).

#### Splitting per VPC (`--split-by-vpc`)

Large multi-VPC accounts are easier to read one VPC at a time. Pass `--split-by-vpc` to write
**one HTML per VPC** — `graph-<VPC ID>.html` — instead of a single combined page:

```bash
cloudbreachgraph-to-html out/graph.json --split-by-vpc -o out/
# → out/graph-vpc-0a31....html, out/graph-vpc-6a60....html, one per VPC
```

Each file is a stand-alone view of just that VPC: its subnets, ENIs, EC2/LB, security groups and
reachability sources, plus the edges placed in it. A node that legitimately relates to **several**
VPCs — a CIDR block, an `Internet` / flow-peer source, **or a security group referenced across a
VPC peering** — appears in **every** VPC it reaches, together with the `can_reach` / `connects_to`
edge into that VPC, so cross-VPC exposure is never hidden. A `can_reach` edge lands only in the VPC
of the resource it reaches (a target VPC's file never gains another VPC's own reach sources as
targets); an **observed flow between two ENIs in different VPCs** (`connects_to`, via flow logs)
is a connection between two real resources, so it appears in **both** their files, each pulling in
the other side's ENI. Resources that trace to no VPC are omitted. Files land in the `-o`
**directory** (default: the input file's directory). The other layout flags compose with it —
`--ringed`, `--optimize-passes` and `--no-security-groups` all apply to **every** per-VPC file —
and each file is subject to the same size guard and per-file `.dot` fallback (`graph-<VPC ID>.dot`).

### Ringed layout (`--ringed`)

Pass `--ringed` to render a **concentric-ringed** view instead of the force-directed one:
each **VPC** sits at the center of its own cluster, ringed by its **subnets**, then its
**ENIs** on their own dedicated ring, then **everything else** under that VPC (EC2 instances,
load balancers, and the **NAT gateways / VPC endpoints** that share the load balancer's role
class), then a **security-group** ring, then a final **outermost ring** of the **IP
sources** (`Internet` / CIDR nodes). With `--no-security-groups` the security-group ring is empty
and the source ring nests straight onto the ENIs. The ENI ring is the **angular anchor**: each
**subnet** is placed at the **mean angle of the ENIs it contains** (and ENIs are grouped by subnet
on their ring), each EC2/LB node at the **mean angle of the ENIs attached to it**, each security
group at the **mean angle of the ENIs it secures**, and each source at the **mean angle of the ENIs
it can reach** (through its SG) — so a subnet keeps its interfaces clustered right next to it, an
EC2 instance or load balancer lines up radially with its interface(s), and a source sits just
outside what it exposes (a single ENI puts it on exactly that spoke; several average out). Multiple VPCs are tiled in a grid so their rings don't overlap; any resource
that resolves to no VPC (an orphan) collects into a final ring-cluster with an empty center.
The ring structure is conveyed by node position alone (no guide circles are drawn); a
per-cluster VPC label sits above each cluster. Unlike the
force view, positions are computed deterministically (no in-browser relaxation, no Recompute
button); you can still drag a node, scroll to zoom, and drag the background to pan, plus the
same **Zoom In / Zoom Out** buttons and **lock scroll-zoom** toggle as the default view. It obeys
the **same size guard** — if the graph is too large it warns and writes the `.dot` fallback,
exactly like the default HTML mode. `--ringed` also works on the main `cloudbreachgraph` command
(`cloudbreachgraph --html --ringed`), straight from a collection run.

#### Reducing crossings (`--optimize-passes N`)

By default a subnet/EC2/LB sits at the *mean* angle of its ENIs, which can push a resource
into an awkward spot when its ENIs span subnets on opposite sides of the ring (long, crossing
edges). Add `--optimize-passes N` (with `--ringed`) to run up to **N passes** that move each
node toward the mean angle of its neighbours — placing it there **as close as an overlap-free
minimum gap allows**, so two subnets that share a load balancer are pulled right next to each
other (not just reordered into distant even slots) and its edges stop crossing the circle. A
**greedy crossing-reduction local search** then relocates each node to the same-ring slot with the
fewest edge crossings — this clears whole spokes that the proximity passes leave crossing
(typically a load balancer fanning edges to ENIs in several subnets).

Optimising also makes the picture **fully overlap-free**, like the [overlap-free
layout](#overlap-free-layout---optimize-passes): the rings are sized so a node's whole **label**
(not just its disk) fits around them, connected nodes are packed no tighter than their labels, and
a final per-cluster **inflation** (a uniform grow-about-the-centre, which changes no crossing)
plus projection clears every residual overlap — node-on-node, edge-across-node, **label-on-label
and disk-on-label** all reach **zero**. Because the labels are now separated in world space the
page **scales the label fonts with the view** (zoom out to see the shape, zoom in to read the
labels — like the overlap-free layout); the plain `--optimize-passes 0` ringed layout keeps
fixed-size labels instead (zoom in to pull two overlapping ring labels apart). Throughout, the
**concentric-ring shape is preserved** — nodes keep their VPC/subnet/ENI/outer ring and essentially
their angle; only the ring radii grow to make room. A **cooling schedule** shrinks the per-pass
movement so the layout **freezes** to a stable state (a big `N` converges to the same result rather
than drifting), and `--optimize-passes 0` (the default) keeps the exact ENI-aligned placement.
Output stays deterministic.

On a real 124-node/4-VPC capture, `--optimize-passes 200` cut edge crossings to **25** and
reached **zero** node, edge and label overlaps — whole-graph and per-VPC (`--split-by-vpc`) alike.
The residual crossings are structural — e.g. a multi-AZ network load balancer whose interfaces
genuinely sit in three different subnets must fan its spokes across the ring.

```bash
cloudbreachgraph-to-html out/graph.json --ringed --optimize-passes 20
```

### Hierarchical layout (`--hierarchical`)

Pass `--hierarchical` for a **tree-like** view that follows the **same rules** as the ringed
layout but replaces the concentric circles with **columns fanning left and right**. Each **VPC**
still sits at the center of its own cluster, and the same layers radiate outward — **subnets**,
then **ENIs**, then **everything else** (EC2, load balancers, NAT gateways, VPC endpoints), then a
**security-group** column, then the **IP sources** — except each layer is a pair of vertical
**columns**, one stepping out to the **left** of the VPC and one to the **right**. Just as in the
ringed layout the ENI layer is the anchor: ENIs are spread down their column (grouped by subnet so
a subnet's interfaces stay contiguous), and every other node lines up at the **mean height of the
ENIs associated with it** (a subnet with the ENIs it contains, an EC2/LB with the ENIs attached to
it, a source with the ENIs it can reach) — the ringed layout's angle-anchoring, done vertically.

Two extra rules make it a genuine hierarchy:

- **Connected nodes share a side.** A subnet and everything that hangs off it — its ENIs, the
  EC2/LB/NAT/endpoint fronting them, their security groups and reachability sources, plus any other
  subnets a shared load balancer / security group / source stitches to it — form one **side-group**
  (a connected component of the VPC's graph once the VPC center is removed), placed **entirely** on
  the left or **entirely** on the right. So **no edge is ever drawn across the center** from one
  side to the other (the only center-crossing edges are subnet → VPC, which end at the center
  itself).
- **The two sides are balanced.** Side-groups are dealt out largest-first, each to the side with
  fewer nodes so far, so roughly the same number of nodes sits on each side of the VPC center.

Multiple VPCs are tiled in a grid, orphans collect into a final cluster with an empty center, and a
per-cluster VPC label sits above each cluster — exactly like the ringed layout. Positions are
computed deterministically in Python (no in-browser relaxation); you can drag a node, scroll to
zoom, and drag the background to pan, with the same **Zoom In / Zoom Out** buttons and **lock
scroll-zoom** toggle. It obeys the **same size guard** — too large and it warns and writes the
`.dot` fallback. `--hierarchical` works on the main `cloudbreachgraph` command
(`cloudbreachgraph --html --hierarchical`) and on `cloudbreachgraph-to-html`, composes with
`--split-by-vpc` and `--no-security-groups`, and **takes precedence over `--ringed`** when both are
given.

```bash
cloudbreachgraph --from-cache tests/fixtures --html --hierarchical --output-dir out/
cloudbreachgraph-to-html out/graph.json --hierarchical -o topology.html
```

#### Reducing crossings (`--optimize-passes N`)

By default each node sits at the *mean height* of its ENIs, which already keeps things tidy, but
an instance or load balancer whose ENIs span subnets at very different heights can leave edges
slanting across a column. Add `--optimize-passes N` (with `--hierarchical`) to run up to **N
barycenter sweeps** that repeatedly move each node toward the mean height of its neighbours — the
standard layered-graph crossing-reduction heuristic — pulling connected nodes to the same row so
their edges straighten out and stop crossing. A **cooling schedule** freezes the layout so a large
`N` converges to the same result rather than drifting, and `--optimize-passes 0` (the default)
keeps the base placement byte-for-byte unchanged.

Optimising also makes the picture **free of node overlap and label overlap** — *by construction*,
not by iterative nudging: the columns are spaced for the nodes' whole **labels** (not just their
disks) and the rows likewise, so no two label rectangles can overlap horizontally or vertically,
which means no two node disks overlap and no disk ever sits on another node's label either. Because
the labels are then separated in *world* space, the page **scales the label fonts with the view**
(zoom out to see the shape, zoom in to read the labels — like the overlap-free layout). Throughout,
the left/right-column shape and the connected-nodes-share-a-side / balanced rules are preserved —
only the row order and the spacing change. (Unlike the ringed and overlap-free layouts it does not
also guarantee zero *edge-over-node* overlap: an edge that skips a column — e.g. a source reaching
an ENI directly with `--no-security-groups` — can still pass over a node in the skipped column.)

On the shipped 124-node/4-VPC example graph, `--optimize-passes 200` cuts edge crossings from
**45** to **27** and reaches **zero** node and label overlap — whole-graph and per-VPC
(`--split-by-vpc`) alike.

```bash
cloudbreachgraph-to-html out/graph.json --hierarchical --optimize-passes 200
cloudbreachgraph --from-cache tests/fixtures --html --hierarchical --optimize-passes 200
```

### Overlap-free layout (`--optimize-passes N`)

Both the force and ringed layouts trade off overlaps against structure. When the priority is a
clean, uncluttered picture, pass **`--optimize-passes N`** (without `--ringed`) to render the
**overlap-free** layout: it runs up to **N graph-optimisation passes** and stops as soon as the
drawing has **no two node disks overlapping**, **no edge drawn across a node** it isn't connected
to (an *edge overlap* — the natural counterpart of a node overlap), and **no node's label
overlapping another label or another node's disk** (a *label overlap*). Positions are computed
deterministically in Python (no in-browser relaxation); you keep drag, zoom and pan. The very
same flag works on the main `cloudbreachgraph` command too — `cloudbreachgraph --html
--optimize-passes N` writes this layout straight from a collection run.

Each pass runs one of four phases: a force-directed **unfolding** that spreads the nodes into a
roomy arrangement, a hard geometric **projection** that separates overlapping disks and pushes
any node off an edge that crosses it, a best-effort **crossing reduction** that relocates each
crossing-heavy node to the nearby slot with the fewest incident crossings, and finally a
**label pass** that clears the labels. A node's label is drawn just under its disk and is often
wider than the disk itself, so the label pass makes room by **uniformly inflating** the
finished, de-tangled layout — a transform that cannot change any edge crossing — until the
labels have space, then separates them. Because the labels are separated in *world* space, the
page **scales its label fonts with the view** (exactly as it already scales the disks), so the
same clearance holds on screen at every zoom; on a dense VPC you simply zoom in to read, and the
labels never sit on top of one another. Finally, each **connected component** (an independent
cluster — e.g. one VPC and everything under it) is **translated into its own cell of a
non-overlapping grid** (label extents included) with a clear gap between them, so it stays
obvious which nodes belong together and which clusters are disconnected. Moving a component as a
rigid body doesn't change its internal crossings or overlaps, and there are no edges between
components, so the separated drawing keeps exactly the crossing count the joint layout achieved.
A real capture is **non-planar** (a single VPC can contain a non-planar minor), so a drawing
with zero edge *crossings* cannot exist; crossings are therefore a *secondary* goal (minimised,
not zeroed) behind the primary no-overlap guarantees. A larger `N` only raises the ceiling on
passes; the layout stops early once it converges, so the result is stable. Combined with
`--ringed`, `--optimize-passes` instead drives the ringed crossing-reduction described above; on
its own it selects this overlap-free layout, and `--optimize-passes 0` (the default) keeps the
plain force / ringed layout.

On the shipped 124-node/4-VPC example graph, `--optimize-passes 10000` reaches **0 node, 0 edge
and 0 label overlap** and roughly **halves the edge crossings** (39 → 18); run per-VPC with
`--split-by-vpc`, every VPC likewise reaches **zero label overlap** while keeping its crossing
count (e.g. the 57-node VPC stays at 15):

```bash
cloudbreachgraph-to-html docs/examples/example-graph.json --optimize-passes 10000 -o example.html
# or straight from a collection run:
cloudbreachgraph --from-cache tests/fixtures --html --optimize-passes 10000
```

## Anonymising a graph for sharing

Need to share a `graph.json` for a bug report or as an example, without leaking real account
ids, IPs, DNS names or resource ids? The auxiliary `cloudbreachgraph-anonymize` tool rewrites
a graph into a **scrubbed copy that keeps every node and relationship** but replaces all
identifying values with random, format-preserving stand-ins — **no AWS calls, purely local**:

```bash
cloudbreachgraph-anonymize out/graph.json                    # writes out/anonymised_graph.json
cloudbreachgraph-anonymize out/graph.json -o example.json    # explicit output path
cloudbreachgraph-anonymize out/graph.json --seed 42          # reproducible randomisation
```

What it randomises: resource ids (`vpc-…`, `subnet-…`, `eni-…`, `i-…`, `sg-…`, `nat-…`, …),
ARNs, private/public IPv4 addresses and CIDRs, DNS names, 12-digit account ids, regions and
AZs, opaque hash tokens, and human names/labels (subnet/VPC/instance/load-balancer names).

The one guarantee is **referential consistency**: a value maps to exactly one replacement
*everywhere it appears*, including where it's embedded inside another string. If `10.1.2.3`
becomes `10.44.9.7` then every reference changes; if the ALB name `my-alb` becomes
`brisk-otter-7` then its ARN node id, the edge that targets it, its DNS name, and the fronting
ENI's `Description` token (`ELB app/my-alb/…`) all change together. Format is preserved so the
result still looks real (a private IP stays private, a `/24` stays a `/24`, an id keeps its
prefix and suffix length). Structural vocabulary — node `type`, edge `relationship`, attribute
keys, the `match_rule` set — is left untouched, and the mapping is injective so two distinct
nodes never collapse into one. With `--seed` the output is byte-for-byte reproducible;
without it a fresh random mapping is used each run. The output is the same deterministic,
sorted `graph.json` shape, so you can feed it straight back into `cloudbreachgraph-to-html`.

**Caveat:** replacement is literal-substring based, so a human name that is also a common
substring of structural text (e.g. a VPC literally named `network`) could over-match; give
resources ordinary names if you plan to anonymise.

## Roles (network, flow logs)

The `network` role (ENIs → EC2/LB → subnets → VPCs, security groups, route tables) is always
active. The **`flow_logs`** role — IP-allocation history + VPC Flow Log analysis, above — is opt-in
via `--flow-logs`; it reads its data from whichever account the `flow_logs` role is bound to in the
config (default: the same account as `network`). Additional roles (e.g. `dns`, `cloudtrail`) plug
into the same registry. See [`docs/05_roadmap.md`](docs/05_roadmap.md) and
`docs/02_architecture.md §5.7, §11`.

## Development

```bash
pip install -e '.[dev]'
pytest        # runs fully offline against tests/fixtures/
ruff check . && ruff format --check .
```

See `docs/` for the full build plan and the per-phase `docs/learnings/` notes.
