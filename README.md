# CloudBreachGraph

A read-only command-line tool that maps an AWS account's network topology — Network
Interfaces (ENIs) → EC2 instances / load balancers → subnets → VPCs — using the **AWS
CLI** (not boto3) as its data source. Output is a graph you can serialize to JSON and
render with Graphviz.

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

`--cache-dir DIR` writes each raw AWS JSON response to disk during a live run.
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
--cache-dir DIR            also write raw AWS JSON responses here
--from-cache DIR           build from cached AWS JSON in DIR, no live calls
--include-orphans          also emit collected resources no ENI references
--output-dir DIR           where to write outputs (default: .)
--render {png,svg}         also rasterize the .dot with Graphviz (needs `dot`)
--html                     also write an interactive, self-contained HTML view
                             (falls back to .dot when the graph is too large)
```

## Outputs

- `graph.json` — the full graph (`meta`, `nodes`, `edges`), pretty-printed and
  deterministic (stable ordering, no timestamps) so diffs are meaningful.
- `graph.dot` — Graphviz DOT: nodes colored/shaped by type, subnets and ENIs grouped
  inside their VPC via `subgraph cluster_*`, edges labeled by relationship (load-balancer
  attachment edges also show the `match_rule` that resolved them). ENI labels include
  their `Private IP` and `Public IP` (when the ENI has an Elastic/public IP); any ENI with
  a public IP is also linked to a generic `Internet` node to highlight internet exposure.
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
  ENIs with a public IP get a red "exposed" outline. For very large graphs an
  in-browser force layout stops being responsive, so if the graph exceeds the render budget
  the tool **warns and skips the HTML**, pointing you at the always-written `.dot` (which
  Graphviz can lay out offline at any scale). Just open the file in any browser — it works
  fully offline.

With `--all-accounts` the files are named per account: `graph.<alias>.json` / `.dot`
(and `.html` with `--html`).

## Converting an existing graph to HTML

Already have a `graph.json` or `graph.dot` from an earlier run (e.g. from `--from-cache`, a
colleague's capture, or a run without `--html`)? The auxiliary `cloudbreachgraph-to-html`
tool renders the same interactive HTML view from it — **no AWS calls, purely local**:

```bash
cloudbreachgraph-to-html out/graph.json                 # writes out/graph.html
cloudbreachgraph-to-html out/graph.dot -o topology.html # explicit output path
cloudbreachgraph-to-html capture.data --format json     # force the input format
cloudbreachgraph-to-html out/graph.json --ringed        # concentric-ringed layout
```

- **From JSON** the conversion is **lossless** — it reproduces exactly the page `--html`
  would have written.
- **From DOT** it's **best-effort** (DOT is a lossy rendering, and only *this tool's own*
  `.dot` is understood): node ids/types/names, the public-exposure and unresolved flags, the
  per-type detail (interface/LB type, CIDR, instance state), and every edge are recovered.
- The same size guard applies: if the graph is too large for a browser force layout, it warns
  and writes a `.dot` fallback instead (skipping it if the input already is that `.dot`).

### Ringed layout (`--ringed`)

Pass `--ringed` to render a **concentric-ringed** view instead of the force-directed one:
each **VPC** sits at the center of its own cluster, ringed by its **subnets**, then its
**ENIs** on their own dedicated ring, then **everything else** under that VPC (EC2 instances,
load balancers) on the outer ring. The ENI ring is the **angular anchor**: each **subnet** is
placed at the **mean angle of the ENIs it contains** (and ENIs are grouped by subnet on their
ring), and each outer-ring node at the **mean angle of the ENIs attached to it** — so a subnet
keeps its interfaces clustered right next to it, and an EC2 instance or load balancer lines up
radially with its interface(s) (a single ENI puts it on exactly that spoke; several average
out). Multiple VPCs are tiled in a grid so their rings don't overlap; any resource
that resolves to no VPC (an orphan) collects into a final ring-cluster with an empty center.
Faint guide circles and a per-cluster VPC label make the ring structure legible. Unlike the
force view, positions are computed deterministically (no in-browser relaxation, no Recompute
button); you can still drag a node, scroll to zoom, and drag the background to pan, plus the
same **Zoom In / Zoom Out** buttons and **lock scroll-zoom** toggle as the default view. It obeys
the **same size guard** — if the graph is too large it warns and writes the `.dot` fallback,
exactly like the default HTML mode.

## Future roles (flow logs, etc.)

v1 maps the **`network`** role. VPC Flow Logs (`flow_logs`) — often published to a separate
central logging account — are a **future role**: the config grammar, targets, and CLI are
already designed for it, so binding `flow_logs = "log_archive"` in a target will "just work"
once the collectors ship, with no CLI change. See
[`docs/05_roadmap.md`](docs/05_roadmap.md) and `docs/02_architecture.md §11`.

## Development

```bash
pip install -e '.[dev]'
pytest        # runs fully offline against tests/fixtures/
ruff check . && ruff format --check .
```

See `docs/` for the full build plan and the per-phase `docs/learnings/` notes.
