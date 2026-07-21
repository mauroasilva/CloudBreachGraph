"""Interactive HTML export — a self-contained, force-directed view of a graph.

``write_html(graph, path)`` renders a single, **fully self-contained** HTML file (no CDN,
no third-party runtime dependency — ``docs/04_conventions.md``): the graph is inlined as
JSON and drawn on an HTML5 canvas by a small vanilla-JavaScript force simulation that
**self-distributes the nodes** (repulsion between every pair + springs along edges +
collision separation) so they settle without sitting on top of one another. To also keep
the picture readable, the layout actively **reduces edge crossings**: repulsion is scaled
by node degree so hubs fan their spokes out, an angular-resolution force spreads a hub's
edges toward even spacing, and the sim is warmed up before the first paint so it opens at a
low-energy, less-tangled state (globally minimizing crossings is NP-hard, so these are
heuristics — they cut crossings sharply on typical topologies but can't guarantee zero).
The page supports drag, wheel-zoom, and background pan.

This is an **opt-in** output (the CLI ``--html`` flag) — it is never produced by default;
``graph.json`` and ``graph.dot`` remain the defaults.

An alternative **ringed** layout (:func:`write_ringed_html` / :func:`build_ringed_html`,
exposed by the ``cloudbreachgraph-to-html --ringed`` auxiliary flag) draws each VPC at the
center of its own cluster and successive concentric rings around it — subnets, then ENIs,
then everything else (EC2 instances, load balancers).
Unlike the force layout its positions are computed deterministically in Python, so the page
needs no in-browser relaxation. It shares the same :data:`MAX_NODES` / :data:`MAX_HTML_BYTES`
size guard and the same ``None``-means-fall-back-to-``.dot`` contract.

**Size guard / graceful fallback (``docs/02_architecture.md §7``).** An O(n²) force layout
in the browser only stays responsive up to a point, and the inlined JSON grows with the
graph. When a graph would exceed :data:`MAX_NODES` nodes or the rendered page would exceed
:data:`MAX_HTML_BYTES`, :func:`write_html` writes **nothing** and returns ``None`` so the
caller can warn the user and fall back to the always-written ``.dot`` (which Graphviz can
lay out offline at any scale).

Determinism (``docs/04_conventions.md``): the emitted HTML is byte-stable — nodes/edges are
already sorted by :class:`~cloudbreachgraph.model.graph.Graph`, the JSON is dumped with a
stable key order, and the in-browser layout is seeded from a fixed PRNG so a given graph
always relaxes the same way. No timestamps are written.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from ..model.graph import Graph

# Beyond these, an in-browser force layout stops being "reasonably loadable": the O(n²)
# simulation stutters and the inlined JSON bloats the page. The caller falls back to `.dot`.
# Node count is the primary gate (it governs layout cost); the byte cap catches pathological
# attribute payloads. Both are resolved at call time so tests/callers can override them.
MAX_NODES: int = 1500
MAX_HTML_BYTES: int = 8 * 1024 * 1024  # 8 MiB of self-contained HTML

# Fill color per node type — kept in step with dot_export._TYPE_STYLE so the two views match.
_TYPE_COLORS: dict[str, str] = {
    "vpc": "#C5CAE9",
    "subnet": "#90CAF9",
    "eni": "#A5D6A7",
    "ec2_instance": "#FFCC80",
    "load_balancer": "#CE93D8",
}
_DEFAULT_COLOR = "#CFD8DC"


def _view_data(graph: Graph) -> dict:
    """The minimal, already-sorted node/edge payload the page's JS needs to lay out.

    We ship only what the renderer uses (id/type/label/a couple of display attributes and
    the public-IP exposure flag) rather than the full ``to_dict()`` so the inlined JSON —
    and therefore the page — stays as small as the size guard allows.
    """
    nodes = [
        {
            "id": n.id,
            "type": n.type,
            "label": n.label,
            "color": _TYPE_COLORS.get(n.type, _DEFAULT_COLOR),
            "synthetic": bool(n.attributes.get("synthetic")),
            "public": bool(n.attributes.get("public_ips")),
            "detail": _detail_line(n.type, n.attributes),
        }
        for n in graph.nodes
    ]
    edges = [{"source": e.source, "target": e.target, "rel": e.relationship} for e in graph.edges]
    return {"meta": graph.meta, "nodes": nodes, "edges": edges}


def _detail_line(node_type: str, attrs: dict) -> str:
    """A short, human-readable secondary line shown under a node's label."""
    if node_type == "eni":
        bits = []
        if attrs.get("private_ips"):
            bits.append("priv " + ", ".join(attrs["private_ips"]))
        if attrs.get("public_ips"):
            bits.append("pub " + ", ".join(attrs["public_ips"]))
        return " · ".join(bits)
    if node_type == "load_balancer" and attrs.get("lb_type"):
        return str(attrs["lb_type"])
    if node_type in ("subnet", "vpc") and attrs.get("cidr"):
        return str(attrs["cidr"])
    if node_type == "ec2_instance" and attrs.get("state"):
        return str(attrs["state"])
    return ""


def build_html(graph: Graph) -> str:
    """Return the complete, self-contained HTML document for ``graph`` as a string."""
    data_json = json.dumps(_view_data(graph), ensure_ascii=False, default=str)
    node_count = len(graph.nodes)
    edge_count = len(graph.edges)
    # Inject via replace (not str.format/%) so the CSS/JS braces need no escaping.
    return (
        _TEMPLATE.replace("__GRAPH_DATA__", data_json)
        .replace("__NODE_COUNT__", str(node_count))
        .replace("__EDGE_COUNT__", str(edge_count))
    )


def write_html(
    graph: Graph,
    path: str | Path,
    *,
    max_nodes: int | None = None,
    max_bytes: int | None = None,
) -> Path | None:
    """Write ``graph`` to ``path`` as a self-contained interactive HTML page.

    Returns the :class:`~pathlib.Path` written, or ``None`` when the graph is too large to
    render responsibly in a browser (more than ``max_nodes`` nodes, or a rendered page over
    ``max_bytes`` bytes). On ``None`` **no file is written** so the caller can fall back to
    the ``.dot`` output. ``max_nodes``/``max_bytes`` default to :data:`MAX_NODES` /
    :data:`MAX_HTML_BYTES` (resolved here so they can be monkeypatched in tests).
    """
    node_cap = MAX_NODES if max_nodes is None else max_nodes
    byte_cap = MAX_HTML_BYTES if max_bytes is None else max_bytes

    if len(graph.nodes) > node_cap:
        return None

    html = build_html(graph)
    if len(html.encode("utf-8")) > byte_cap:
        return None

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


# --------------------------------------------------------------------------- #
# Ringed layout — concentric rings centered on each VPC.
#
# An alternative, deterministic layout (no force simulation): every VPC is the *center* of
# its own cluster, its subnets sit on the **first ring**, its **ENIs** on their own dedicated
# **second ring**, and everything else that lives under that VPC (EC2 instances, load
# balancers) sits on the **outer ring**, placed at the **mean angle of the ENIs attached to
# it** so it lines up radially with its interface(s). Multiple
# VPC clusters are tiled in a grid so they don't overlap; resources that resolve to no VPC
# (orphans) collect into one final ring-cluster with an empty center. Positions are computed
# here in Python from the already-sorted nodes/edges, so the page is byte-stable and needs no
# in-browser relaxation — the JS only draws, pans, zooms and lets you drag a node.
# --------------------------------------------------------------------------- #
_UNASSIGNED = "__unassigned__"  # pseudo-group key for resources that resolve to no VPC

# Ring geometry (pixels). Arc is the minimum spacing between adjacent nodes on a ring, so a
# ring's radius grows with how many nodes sit on it (keeping them from colliding).
_RING_ARC = 92.0
_RING1_MIN = 150.0  # the first (innermost) ring never smaller than this
_RING_GAP = 150.0  # radial gap between one ring and the next
_CLUSTER_MARGIN = 80.0  # empty room around a cluster's outermost ring
_CLUSTER_PAD = 130.0  # extra gap between adjacent clusters in the grid

# Concentric rings, from the center out: VPC (center), then subnets, then ENIs, then
# everything else (EC2 instances, load balancers, …). ENIs get their own dedicated ring so
# the interfaces read as a distinct layer between their subnets and the compute/LBs they front.
_RING_COUNT = 4  # ring 0 is the center; rings 1..3 are the concentric outer rings


def _ring_of(node_type: str) -> int:
    """Ring index for a node type: VPC center (0), subnet (1), ENI (2), everything else (3)."""
    if node_type == "vpc":
        return 0
    if node_type == "subnet":
        return 1
    if node_type == "eni":
        return 2
    return 3


def _ring_radii(counts: list[int]) -> list[float]:
    """Radius of each successive ring, sized so its nodes fit without colliding.

    ``counts[i]`` is how many nodes sit on ring ``i + 1`` (ring 0 is the center, radius 0). A
    ring's radius is the larger of a fixed step out from the previous non-empty ring and the
    radius its own node count needs. An **empty** ring collapses to radius 0 and consumes no
    radial space, so later rings still nest tightly (e.g. a cluster with no subnets puts its
    ENIs on the innermost ring).
    """
    radii: list[float] = []
    prev = 0.0
    for c in counts:
        if not c:
            radii.append(0.0)
            continue
        base = (prev + _RING_GAP) if prev else _RING1_MIN
        r = max(base, c * _RING_ARC / (2 * math.pi))
        radii.append(r)
        prev = r
    return radii


def _place_at_angle(member: dict, cx: float, cy: float, radius: float, angle: float) -> None:
    """Assign ``x``/``y`` to a single member at ``angle`` on a circle of ``radius`` about center."""
    member["x"] = round(cx + radius * math.cos(angle), 2)
    member["y"] = round(cy + radius * math.sin(angle), 2)


def _place_on_ring(members: list, cx: float, cy: float, radius: float) -> dict[str, float]:
    """Place each member evenly around a circle of ``radius`` about (cx, cy).

    Returns a map of member id -> the angle (radians) it was placed at, so a subsequent ring
    can align its nodes with these (e.g. the outer ring aligning to its ENIs).
    """
    count = len(members)
    angles: dict[str, float] = {}
    for i, m in enumerate(members):
        angle = -math.pi / 2 + (2 * math.pi * i / count if count else 0.0)
        _place_at_angle(m, cx, cy, radius, angle)
        angles[m["id"]] = angle
    return angles


def _circular_mean(angles: list[float]) -> float:
    """Mean of ``angles`` on the circle (averages unit vectors), robust to the −π/π wraparound."""
    sin_sum = math.fsum(math.sin(a) for a in angles)
    cos_sum = math.fsum(math.cos(a) for a in angles)
    return math.atan2(sin_sum, cos_sum)


def _place_outer_ring(
    members: list,
    cx: float,
    cy: float,
    radius: float,
    eni_angle: dict[str, float],
    enis_of: dict[str, list[str]],
) -> None:
    """Place outer-ring nodes at the angle of the ENI(s) attached to them.

    Each EC2 instance / load balancer sits at the **circular mean** of the angles of the ENIs
    that reference it (via ``attached_to``), so it lines up radially with its interface — a
    single ENI puts it on exactly that spoke, several average out. Nodes with no attached ENI
    (e.g. orphans surfaced by ``--include-orphans``) fall back to even spacing.
    """
    aligned: list[tuple[dict, float]] = []
    unaligned: list[dict] = []
    for m in members:
        angs = [eni_angle[e] for e in enis_of.get(m["id"], []) if e in eni_angle]
        (aligned.append((m, _circular_mean(angs))) if angs else unaligned.append(m))
    for m, angle in aligned:
        _place_at_angle(m, cx, cy, radius, angle)
    count = len(unaligned)
    for i, m in enumerate(unaligned):
        _place_at_angle(m, cx, cy, radius, -math.pi / 2 + (2 * math.pi * i / count if count else 0))


def _vpc_group_of(graph: Graph) -> dict[str, str]:
    """Map each node id to the id of the VPC it belongs to (or ``_UNASSIGNED``).

    Traces the model's edges: subnet →(in_vpc)→ vpc, eni →(in_subnet)→ subnet, and
    ec2/lb ←(attached_to)← eni. A node that can't be traced to a VPC lands in ``_UNASSIGNED``.
    """
    eni_subnet: dict[str, str] = {}
    subnet_vpc: dict[str, str] = {}
    node_eni: dict[str, str] = {}  # ec2/lb (attach target) -> the ENI attached to it
    for e in graph.edges:
        if e.relationship == "in_subnet":
            eni_subnet.setdefault(e.source, e.target)
        elif e.relationship == "in_vpc":
            subnet_vpc.setdefault(e.source, e.target)
        elif e.relationship == "attached_to":
            node_eni.setdefault(e.target, e.source)

    group: dict[str, str] = {}
    for n in graph.nodes:
        if n.type == "vpc":
            g: str | None = n.id
        elif n.type == "subnet":
            g = subnet_vpc.get(n.id)
        elif n.type == "eni":
            g = subnet_vpc.get(eni_subnet.get(n.id, ""))
        else:
            eni = node_eni.get(n.id)
            g = subnet_vpc.get(eni_subnet.get(eni, "")) if eni else None
        group[n.id] = g or _UNASSIGNED
    return group


def _ringed_view_data(graph: Graph) -> dict:
    """Build the ringed page payload: nodes with fixed x/y, edges, and per-cluster ring guides.

    Reuses :func:`_view_data`'s per-node fields (colors, exposure flag, detail line) and adds
    computed ``x``/``y``. ``clusters`` carries each VPC cluster's center, ring radii and label
    so the page can draw the guide circles that make the rings legible.
    """
    base = _view_data(graph)
    by_id = {n["id"]: n for n in base["nodes"]}
    group_of = _vpc_group_of(graph)

    # Which ENIs attach to each outer-ring node (ec2/lb), so it can sit at their mean angle.
    # graph.edges is sorted, so the per-node ENI list is deterministic.
    enis_of: dict[str, list[str]] = {}
    for e in graph.edges:
        if e.relationship == "attached_to":
            enis_of.setdefault(e.target, []).append(e.source)

    # Bucket nodes into groups, and within a group into rings, preserving the deterministic
    # (type, id) order the Graph already sorted them into.
    groups: dict[str, dict[int, list]] = {}
    labels: dict[str, str] = {}
    for n in graph.nodes:
        g = group_of[n.id]
        bucket = groups.setdefault(g, {r: [] for r in range(_RING_COUNT)})
        bucket[_ring_of(n.type)].append(by_id[n.id])
        if n.type == "vpc":
            labels[g] = n.label

    # Deterministic group order: real VPC clusters by id, the orphan cluster (if any) last.
    order = sorted(k for k in groups if k != _UNASSIGNED)
    if _UNASSIGNED in groups:
        order.append(_UNASSIGNED)

    # radii[g] holds the radius of each outer ring (rings 1..3); ring 0 is the center at 0.
    radii = {g: _ring_radii([len(groups[g][r]) for r in range(1, _RING_COUNT)]) for g in order}
    cluster_r = {g: max([*radii[g], 60.0]) + _CLUSTER_MARGIN for g in order}
    cell = 2 * (max(cluster_r.values(), default=0.0)) + _CLUSTER_PAD
    cols = max(1, math.ceil(math.sqrt(len(order))))

    clusters = []
    for i, g in enumerate(order):
        cx = (i % cols) * cell
        cy = (i // cols) * cell
        rs = radii[g]
        _place_on_ring(groups[g][0], cx, cy, 0.0)  # VPC center (0..1 nodes)
        _place_on_ring(groups[g][1], cx, cy, rs[0])  # ring 1: subnets, evenly spaced
        eni_angle = _place_on_ring(groups[g][2], cx, cy, rs[1])  # ring 2: ENIs, evenly spaced
        # ring 3: EC2/LBs aligned to the mean angle of the ENIs attached to each.
        _place_outer_ring(groups[g][3], cx, cy, rs[2], eni_angle, enis_of)
        clusters.append(
            {
                "cx": round(cx, 2),
                "cy": round(cy, 2),
                "rings": [round(r, 2) for r in rs],
                "label": labels.get(g, "unassigned"),
            }
        )

    base["clusters"] = clusters
    return base


def build_ringed_html(graph: Graph) -> str:
    """Return the complete, self-contained ringed HTML document for ``graph`` as a string."""
    data_json = json.dumps(_ringed_view_data(graph), ensure_ascii=False, default=str)
    return (
        _RINGED_TEMPLATE.replace("__GRAPH_DATA__", data_json)
        .replace("__NODE_COUNT__", str(len(graph.nodes)))
        .replace("__EDGE_COUNT__", str(len(graph.edges)))
    )


def write_ringed_html(
    graph: Graph,
    path: str | Path,
    *,
    max_nodes: int | None = None,
    max_bytes: int | None = None,
) -> Path | None:
    """Write ``graph`` to ``path`` as the self-contained **ringed** HTML page.

    Same contract and size guard as :func:`write_html` (VPC-centered concentric rings instead
    of a force layout): returns the :class:`~pathlib.Path` written, or ``None`` when the graph
    is too large (more than ``max_nodes`` nodes, or a rendered page over ``max_bytes`` bytes),
    in which case **no file is written** so the caller can fall back to the ``.dot`` output.
    """
    node_cap = MAX_NODES if max_nodes is None else max_nodes
    byte_cap = MAX_HTML_BYTES if max_bytes is None else max_bytes

    if len(graph.nodes) > node_cap:
        return None

    html = build_ringed_html(graph)
    if len(html.encode("utf-8")) > byte_cap:
        return None

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


# --------------------------------------------------------------------------- #
# The page template. Data is injected into __GRAPH_DATA__ (a JSON object literal).
# Self-contained: inline CSS + JS, no network access, no external assets.
# --------------------------------------------------------------------------- #
_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CloudBreachGraph</title>
<style>
  :root { color-scheme: light dark; }
  html, body { margin: 0; height: 100%; font-family: Helvetica, Arial, sans-serif; }
  body { background: #fafafa; color: #263238; }
  #canvas { display: block; width: 100vw; height: 100vh; cursor: grab; }
  #canvas:active { cursor: grabbing; }
  #hud {
    position: fixed; top: 12px; left: 12px; background: rgba(255,255,255,0.92);
    border: 1px solid #cfd8dc; border-radius: 8px; padding: 10px 12px; font-size: 12px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.12); max-width: 260px;
  }
  #hud h1 { font-size: 14px; margin: 0 0 4px; }
  #hud .muted { color: #607d8b; }
  #legend { margin-top: 8px; display: grid; grid-template-columns: auto 1fr; gap: 3px 6px; }
  #legend .swatch { width: 12px; height: 12px; border-radius: 3px; align-self: center; }
  #legend .exposed { border: 2px solid #e53935; }
  #controls { margin-top: 10px; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  #controls button {
    font: inherit; font-size: 12px; cursor: pointer; padding: 5px 10px;
    border: 1px solid #b0bec5; border-radius: 6px; background: #eceff1; color: #263238;
  }
  #controls button.zoom { padding: 5px 9px; font-weight: 600; }
  #controls button:hover { background: #cfd8dc; }
  #controls button:active { transform: translateY(1px); }
  #controls label { font-size: 12px; cursor: pointer; display: inline-flex;
    align-items: center; gap: 4px; }
  #hint { margin-top: 8px; color: #607d8b; }
  @media (prefers-color-scheme: dark) {
    body { background: #202124; color: #e8eaed; }
    #hud { background: rgba(40,42,45,0.92); border-color: #3c4043; }
    #hud .muted, #hint { color: #9aa0a6; }
    #controls button { background: #3c4043; border-color: #5f6368; color: #e8eaed; }
    #controls button:hover { background: #4a4d51; }
  }
</style>
</head>
<body>
<canvas id="canvas"></canvas>
<div id="hud">
  <h1>CloudBreachGraph</h1>
  <div class="muted"><span id="ncount">__NODE_COUNT__</span> nodes ·
    <span id="ecount">__EDGE_COUNT__</span> edges</div>
  <div id="legend"></div>
  <div id="controls">
    <button id="zoomIn" class="zoom" title="Zoom in">+</button>
    <button id="zoomOut" class="zoom" title="Zoom out">−</button>
    <button id="recompute" title="Tidy the layout from its current positions: release pins
and gently settle, resolving overlaps and easing clusters apart — your arrangement is kept,
not re-solved. Click again to refine further.">↻ Recompute layout</button>
    <label title="When on, the mouse wheel no longer zooms — use the + / − buttons instead.">
      <input type="checkbox" id="noscroll"> lock scroll-zoom</label>
  </div>
  <div id="hint">drag a node to pin · scroll to zoom (or lock it and use + / −) ·
    drag background to pan · Recompute gently tidies from where nodes are now</div>
</div>
<script>
"use strict";
const GRAPH = __GRAPH_DATA__;

// Deterministic PRNG (mulberry32) so a given graph always relaxes the same way.
function mulberry32(a) {
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const rand = mulberry32(0x9E3779B9);

const canvas = document.getElementById("canvas");
const ctx = canvas.getContext("2d");
let W = 0, H = 0, dpr = 1;

function resize() {
  dpr = window.devicePixelRatio || 1;
  W = window.innerWidth; H = window.innerHeight;
  canvas.width = Math.round(W * dpr);
  canvas.height = Math.round(H * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
window.addEventListener("resize", () => { resize(); });
resize();

// Node radius by type (LBs/VPCs read as slightly larger anchors).
function radiusFor(t) {
  if (t === "vpc") return 16;
  if (t === "load_balancer") return 13;
  if (t === "ec2_instance" || t === "subnet") return 11;
  return 9;
}

// Build node objects with deterministic initial positions on a spiral around center.
const nodes = GRAPH.nodes.map((n, i) => {
  const a = i * 2.399963229728653;            // golden angle
  const r = 12 * Math.sqrt(i + 1);
  return {
    ...n, r: radiusFor(n.type),
    x: Math.cos(a) * r + (rand() - 0.5) * 8,
    y: Math.sin(a) * r + (rand() - 0.5) * 8,
    vx: 0, vy: 0, fixed: false,
  };
});
const index = new Map(nodes.map((n) => [n.id, n]));
const edges = GRAPH.edges
  .map((e) => ({ s: index.get(e.source), t: index.get(e.target), rel: e.rel }))
  .filter((e) => e.s && e.t);

// --- force simulation ------------------------------------------------------
const REPULSION = 4200;      // base Coulomb-like node-node repulsion
const SPRING = 0.03;         // edge spring stiffness
const GRAVITY = 0.012;       // pull toward the layout center (keeps it on screen)
const DAMPING = 0.86;        // velocity damping per tick
const ANGULAR = 22;          // hub spoke-spreading strength (angular resolution)
const CROSS_COMPONENT = 4;   // extra repulsion between disconnected clusters (keep them apart)
const RECOMPUTE_ALPHA = 0.1; // gentle reheat for the Recompute button: refine from current
                             // positions (nearest minimum), never a full high-energy re-solve
const ANCHOR = 0.08;         // Recompute tethers each node to its current spot with this
                             // stiffness, so it moves only as far as decluttering requires
let alpha = 1.0;             // cooling factor (1 -> 0)
let gravityScale = 1;        // 1 during the initial layout; the Recompute button drops it to 0
                             // so a hand-made arrangement isn't dragged back toward center
let anchored = false;        // true after Recompute: nodes are pulled toward n.ax/n.ay anchors

// Degree-weighted charge + adjacency: high-degree hubs repel harder, so their neighbors
// fan out evenly instead of bunching on one side — this unfolds star clusters (a subnet
// with many ENIs) and is the single biggest lever for reducing edge crossings here. The
// per-node neighbor list is built once so the angular pass below stays O(edges)/tick.
for (const n of nodes) { n.deg = 0; n.adj = []; }
for (const e of edges) { e.s.deg++; e.t.deg++; e.s.adj.push(e.t); e.t.adj.push(e.s); }
for (const n of nodes) n.charge = REPULSION * (1 + 0.7 * Math.sqrt(n.deg));

// Give each edge a rest length that grows with its endpoints' degree, so hub spokes get
// the room they need to spread rather than being pulled back into a tangle.
for (const e of edges) e.len = 70 + 9 * Math.sqrt(Math.max(e.s.deg, e.t.deg));

// Connected components: each maximal set of nodes joined by edges is one "cluster" (e.g. a
// VPC and everything under it; a stand-alone orphan). Nodes in *different* components repel
// CROSS_COMPONENT× harder (see step()), so segregated clusters drift apart instead of
// mingling — which is what the Recompute button leans on. Computed once via BFS over adj.
let componentCount = 0;
for (const n of nodes) n.comp = -1;
for (const start of nodes) {
  if (start.comp !== -1) continue;
  start.comp = componentCount;
  const queue = [start];
  for (let qi = 0; qi < queue.length; qi++) {
    for (const nb of queue[qi].adj) {
      if (nb.comp === -1) { nb.comp = componentCount; queue.push(nb); }
    }
  }
  componentCount++;
}

function step() {
  if (alpha > 0.005) alpha *= 0.992;
  // Pairwise repulsion + collision separation (self-distribution).
  for (let i = 0; i < nodes.length; i++) {
    const a = nodes[i];
    for (let j = i + 1; j < nodes.length; j++) {
      const b = nodes[j];
      let dx = a.x - b.x, dy = a.y - b.y;
      let d2 = dx * dx + dy * dy;
      if (d2 < 0.01) { dx = (rand() - 0.5); dy = (rand() - 0.5); d2 = dx * dx + dy * dy; }
      const dist = Math.sqrt(d2);
      let rep = Math.sqrt(a.charge * b.charge);
      if (a.comp !== b.comp) rep *= CROSS_COMPONENT;   // shove disconnected clusters apart
      const f = (rep / d2) * alpha;
      const fx = (dx / dist) * f, fy = (dy / dist) * f;
      a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
      // Hard collision: never let two node disks overlap.
      const mind = a.r + b.r + 4;
      if (dist < mind) {
        const push = (mind - dist) / 2;
        const px = (dx / dist) * push, py = (dy / dist) * push;
        a.x += px; a.y += py; b.x -= px; b.y -= py;
      }
    }
  }
  // Springs along edges (per-edge rest length).
  for (const e of edges) {
    let dx = e.t.x - e.s.x, dy = e.t.y - e.s.y;
    const dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
    const f = SPRING * (dist - e.len) * alpha;
    const fx = (dx / dist) * f, fy = (dy / dist) * f;
    e.s.vx += fx; e.s.vy += fy; e.t.vx -= fx; e.t.vy -= fy;
  }
  // Angular resolution: fan a hub's spokes out toward an even radial spacing. For each
  // adjacent pair (by angle) that sits closer than the ideal gap, nudge the two neighbors
  // apart *tangentially* (perpendicular to their radius from the hub) so they rotate around
  // it rather than fold across each other. Cheap: only nodes with degree >= 3.
  for (const h of nodes) {
    if (h.deg < 3) continue;
    const spokes = h.adj.slice();
    spokes.sort((p, q) => Math.atan2(p.y - h.y, p.x - h.x) - Math.atan2(q.y - h.y, q.x - h.x));
    const want = (2 * Math.PI) / spokes.length;
    for (let k = 0; k < spokes.length; k++) {
      const a = spokes[k], b = spokes[(k + 1) % spokes.length];
      let gap = Math.atan2(b.y - h.y, b.x - h.x) - Math.atan2(a.y - h.y, a.x - h.x);
      while (gap <= 0) gap += 2 * Math.PI;
      if (gap >= want) continue;                       // already spread enough
      const push = ANGULAR * (want - gap) * alpha;     // >0, separate the pair
      for (const [node, sign] of [[a, -1], [b, 1]]) {  // a rotates back, b rotates forward
        const dx = node.x - h.x, dy = node.y - h.y;
        const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
        node.vx += sign * push * (-dy / d);            // unit tangent = (-dy, dx)/d
        node.vy += sign * push * (dx / d);
      }
    }
  }
  // Gravity + anchors + integrate. After a Recompute, gravityScale is 0 (no re-centering) and
  // each node is pulled back toward its anchor (n.ax/n.ay) — this tether is NOT alpha-scaled,
  // so as the sim cools nodes return to where they were except where decluttering moved them.
  for (const n of nodes) {
    if (n.fixed) { n.vx = 0; n.vy = 0; continue; }
    n.vx -= n.x * GRAVITY * gravityScale * alpha; n.vy -= n.y * GRAVITY * gravityScale * alpha;
    if (anchored) { n.vx += (n.ax - n.x) * ANCHOR; n.vy += (n.ay - n.y) * ANCHOR; }
    n.vx *= DAMPING; n.vy *= DAMPING;
    n.x += n.vx; n.y += n.vy;
  }
}

// Warm up synchronously so the page opens already relaxed at a low-energy (fewer-crossing)
// state instead of visibly untangling from the initial placement. The iteration count is
// bounded by a work budget so even a near-MAX_NODES graph loads without a long stall.
const WARMUP = Math.max(60, Math.min(500, Math.floor(3.0e7 / (nodes.length * nodes.length + 1))));
for (let i = 0; i < WARMUP; i++) step();

// --- view transform (pan/zoom) --------------------------------------------
let scale = 1, panX = 0, panY = 0, centered = false;
function toScreen(n) { return { x: n.x * scale + panX, y: n.y * scale + panY }; }
function fromScreen(sx, sy) { return { x: (sx - panX) / scale, y: (sy - panY) / scale }; }

function autoCenter() {
  if (centered || nodes.length === 0) return;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const n of nodes) {
    minX = Math.min(minX, n.x); minY = Math.min(minY, n.y);
    maxX = Math.max(maxX, n.x); maxY = Math.max(maxY, n.y);
  }
  const gw = Math.max(maxX - minX, 1), gh = Math.max(maxY - minY, 1);
  scale = Math.min(2, 0.85 * Math.min(W / gw, H / gh));
  panX = W / 2 - ((minX + maxX) / 2) * scale;
  panY = H / 2 - ((minY + maxY) / 2) * scale;
  if (alpha < 0.05) centered = true;
}

// --- rendering -------------------------------------------------------------
function draw() {
  ctx.clearRect(0, 0, W, H);
  ctx.lineWidth = 1;
  ctx.strokeStyle = getComputedStyle(document.body).color === "rgb(232, 234, 237)"
    ? "rgba(200,200,200,0.35)" : "rgba(96,125,139,0.35)";
  for (const e of edges) {
    const s = toScreen(e.s), t = toScreen(e.t);
    ctx.beginPath(); ctx.moveTo(s.x, s.y); ctx.lineTo(t.x, t.y); ctx.stroke();
  }
  const showLabels = scale > 0.55;
  for (const n of nodes) {
    const p = toScreen(n);
    const r = n.r * scale;
    ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
    ctx.fillStyle = n.color; ctx.fill();
    if (n.public) { ctx.lineWidth = 2.5; ctx.strokeStyle = "#e53935"; ctx.stroke(); }
    ctx.lineWidth = n.synthetic ? 1.5 : 1;
    ctx.setLineDash(n.synthetic ? [3, 3] : []);
    ctx.strokeStyle = "#37474f"; ctx.stroke(); ctx.setLineDash([]);
    if (showLabels) {
      ctx.fillStyle = getComputedStyle(document.body).color;
      ctx.font = "11px Helvetica, Arial, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(n.label || n.id, p.x, p.y + r + 12);
      if (n.detail) {
        ctx.fillStyle = "#78909c"; ctx.font = "9px Helvetica, Arial, sans-serif";
        ctx.fillText(n.detail, p.x, p.y + r + 23);
      }
    }
  }
}

function frame() { step(); autoCenter(); draw(); requestAnimationFrame(frame); }
requestAnimationFrame(frame);

// --- interaction -----------------------------------------------------------
let dragNode = null, dragging = false, lastX = 0, lastY = 0;
function nodeAt(sx, sy) {
  const w = fromScreen(sx, sy);
  for (let i = nodes.length - 1; i >= 0; i--) {
    const n = nodes[i];
    const dx = n.x - w.x, dy = n.y - w.y;
    if (dx * dx + dy * dy <= (n.r + 3) * (n.r + 3)) return n;
  }
  return null;
}
canvas.addEventListener("mousedown", (ev) => {
  lastX = ev.clientX; lastY = ev.clientY;
  dragNode = nodeAt(ev.clientX, ev.clientY);
  if (dragNode) { dragNode.fixed = true; centered = true; alpha = Math.max(alpha, 0.4); }
  else { dragging = true; }
});
window.addEventListener("mousemove", (ev) => {
  if (dragNode) {
    const w = fromScreen(ev.clientX, ev.clientY);
    dragNode.x = w.x; dragNode.y = w.y; dragNode.vx = 0; dragNode.vy = 0;
    dragNode.ax = w.x; dragNode.ay = w.y;   // if anchored (post-Recompute), the anchor follows
                                            // the drag so the node stays where you drop it
  } else if (dragging) {
    panX += ev.clientX - lastX; panY += ev.clientY - lastY;
    lastX = ev.clientX; lastY = ev.clientY; centered = true;
  }
});
window.addEventListener("mouseup", () => { dragNode = null; dragging = false; });

// Zoom about a screen point, clamped, keeping that point stationary. Shared by the wheel and
// the +/- buttons (which zoom about the viewport center).
function zoomAround(px, py, factor) {
  const w = fromScreen(px, py);
  scale = Math.max(0.1, Math.min(6, scale * factor));
  panX = px - w.x * scale; panY = py - w.y * scale; centered = true;
}
let scrollZoomEnabled = true;   // the "lock scroll-zoom" toggle flips this off
canvas.addEventListener("wheel", (ev) => {
  if (!scrollZoomEnabled) return;               // locked: only the +/- buttons zoom
  ev.preventDefault();
  zoomAround(ev.clientX, ev.clientY, ev.deltaY < 0 ? 1.1 : 1 / 1.1);
}, { passive: false });
document.getElementById("zoomIn")
  .addEventListener("click", () => zoomAround(W / 2, H / 2, 1.2));
document.getElementById("zoomOut")
  .addEventListener("click", () => zoomAround(W / 2, H / 2, 1 / 1.2));
document.getElementById("noscroll").addEventListener("change", (ev) => {
  scrollZoomEnabled = !ev.target.checked;
});

// --- recompute layout ------------------------------------------------------
// Tidy the layout *from wherever the nodes are now* — NOT a global re-solve. The old full
// reheat (alpha = 1) gave nodes large velocities that overshot into a far-away, chaotic
// minimum, throwing away the arrangement you'd made by hand and re-tangling it. Instead we
// make the *current* arrangement the equilibrium and only relieve local crowding:
//   * anchor every node to its current position (n.ax/n.ay) so it's tethered where it is;
//   * re-anchor every spring's rest length to its current length, so edges are NOT contracted
//     back toward the default layout (your shape and edge lengths are preserved);
//   * switch off the centering gravity (gravityScale = 0), so nodes aren't dragged to center;
//   * apply a small amount of energy (RECOMPUTE_ALPHA) so only repulsion, collision, angular
//     spread and cross-component separation act — resolving overlaps and easing clusters apart.
// The viewport is left untouched so the picture doesn't jump. Click again to tidy further.
function recompute() {
  for (const e of edges) {
    const dx = e.t.x - e.s.x, dy = e.t.y - e.s.y;
    e.len = Math.sqrt(dx * dx + dy * dy) || e.len;   // current geometry becomes the rest state
  }
  for (const n of nodes) { n.fixed = false; n.ax = n.x; n.ay = n.y; n.vx = 0; n.vy = 0; }
  anchored = true;           // tether each node to where it is now
  gravityScale = 0;          // keep the arrangement in place; don't re-center it
  alpha = RECOMPUTE_ALPHA;   // gentle nudge from current positions, not a full reheat
}
document.getElementById("recompute").addEventListener("click", recompute);

// --- legend ----------------------------------------------------------------
(function legend() {
  const el = document.getElementById("legend");
  const seen = new Map();
  for (const n of GRAPH.nodes) if (!seen.has(n.type)) seen.set(n.type, n.color);
  for (const [type, color] of seen) {
    const sw = document.createElement("span");
    sw.className = "swatch"; sw.style.background = color;
    const lb = document.createElement("span"); lb.textContent = type;
    el.appendChild(sw); el.appendChild(lb);
  }
  if (GRAPH.nodes.some((n) => n.public)) {
    const sw = document.createElement("span");
    sw.className = "swatch exposed"; sw.style.background = "transparent";
    const lb = document.createElement("span"); lb.textContent = "public IP (exposed)";
    el.appendChild(sw); el.appendChild(lb);
  }
})();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Ringed page template. Positions are precomputed in Python (GRAPH.nodes have x/y and
# GRAPH.clusters carry the ring guides), so there is no force simulation — the JS only draws
# and handles pan/zoom/drag. Self-contained: inline CSS + JS, no external assets.
# --------------------------------------------------------------------------- #
_RINGED_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CloudBreachGraph — ringed</title>
<style>
  :root { color-scheme: light dark; }
  html, body { margin: 0; height: 100%; font-family: Helvetica, Arial, sans-serif; }
  body { background: #fafafa; color: #263238; }
  #canvas { display: block; width: 100vw; height: 100vh; cursor: grab; }
  #canvas:active { cursor: grabbing; }
  #hud {
    position: fixed; top: 12px; left: 12px; background: rgba(255,255,255,0.92);
    border: 1px solid #cfd8dc; border-radius: 8px; padding: 10px 12px; font-size: 12px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.12); max-width: 260px;
  }
  #hud h1 { font-size: 14px; margin: 0 0 4px; }
  #hud .muted { color: #607d8b; }
  #legend { margin-top: 8px; display: grid; grid-template-columns: auto 1fr; gap: 3px 6px; }
  #legend .swatch { width: 12px; height: 12px; border-radius: 3px; align-self: center; }
  #legend .exposed { border: 2px solid #e53935; }
  #controls { margin-top: 10px; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  #controls button {
    font: inherit; font-size: 12px; cursor: pointer; padding: 5px 10px;
    border: 1px solid #b0bec5; border-radius: 6px; background: #eceff1; color: #263238;
  }
  #controls button.zoom { padding: 5px 9px; font-weight: 600; }
  #controls button:hover { background: #cfd8dc; }
  #controls button:active { transform: translateY(1px); }
  #controls label { font-size: 12px; cursor: pointer; display: inline-flex;
    align-items: center; gap: 4px; }
  #hint { margin-top: 8px; color: #607d8b; }
  @media (prefers-color-scheme: dark) {
    body { background: #202124; color: #e8eaed; }
    #hud { background: rgba(40,42,45,0.92); border-color: #3c4043; }
    #hud .muted, #hint { color: #9aa0a6; }
    #controls button { background: #3c4043; border-color: #5f6368; color: #e8eaed; }
    #controls button:hover { background: #4a4d51; }
  }
</style>
</head>
<body>
<canvas id="canvas"></canvas>
<div id="hud">
  <h1>CloudBreachGraph <span class="muted">· ringed</span></h1>
  <div class="muted"><span id="ncount">__NODE_COUNT__</span> nodes ·
    <span id="ecount">__EDGE_COUNT__</span> edges</div>
  <div id="legend"></div>
  <div id="controls">
    <button id="zoomIn" class="zoom" title="Zoom in">+</button>
    <button id="zoomOut" class="zoom" title="Zoom out">−</button>
    <label title="When on, the mouse wheel no longer zooms — use the + / − buttons instead.">
      <input type="checkbox" id="noscroll"> lock scroll-zoom</label>
  </div>
  <div id="hint">rings from each center: VPC · subnets · ENIs · everything else ·
    drag a node · scroll to zoom (or lock it and use + / −) · drag to pan</div>
</div>
<script>
"use strict";
const GRAPH = __GRAPH_DATA__;

const canvas = document.getElementById("canvas");
const ctx = canvas.getContext("2d");
let W = 0, H = 0, dpr = 1;

function resize() {
  dpr = window.devicePixelRatio || 1;
  W = window.innerWidth; H = window.innerHeight;
  canvas.width = Math.round(W * dpr);
  canvas.height = Math.round(H * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  draw();
}
window.addEventListener("resize", resize);

function radiusFor(t) {
  if (t === "vpc") return 16;
  if (t === "load_balancer") return 13;
  if (t === "ec2_instance" || t === "subnet") return 11;
  return 9;
}

// Nodes already carry precomputed x/y from the Python layout; positions are authoritative.
const nodes = GRAPH.nodes.map((n) => ({ ...n, r: radiusFor(n.type) }));
const index = new Map(nodes.map((n) => [n.id, n]));
const edges = GRAPH.edges
  .map((e) => ({ s: index.get(e.source), t: index.get(e.target) }))
  .filter((e) => e.s && e.t);
const clusters = GRAPH.clusters || [];

// --- view transform (pan/zoom), fit to the whole layout on first paint ------
let scale = 1, panX = 0, panY = 0, centered = false;
function toScreen(n) { return { x: n.x * scale + panX, y: n.y * scale + panY }; }
function fromScreen(sx, sy) { return { x: (sx - panX) / scale, y: (sy - panY) / scale }; }

function autoCenter() {
  if (centered || nodes.length === 0) return;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const c of clusters) {
    const r = Math.max(0, ...c.rings, 40) + 30;
    minX = Math.min(minX, c.cx - r); minY = Math.min(minY, c.cy - r);
    maxX = Math.max(maxX, c.cx + r); maxY = Math.max(maxY, c.cy + r);
  }
  for (const n of nodes) {
    minX = Math.min(minX, n.x); minY = Math.min(minY, n.y);
    maxX = Math.max(maxX, n.x); maxY = Math.max(maxY, n.y);
  }
  const gw = Math.max(maxX - minX, 1), gh = Math.max(maxY - minY, 1);
  scale = Math.min(2, 0.9 * Math.min(W / gw, H / gh));
  panX = W / 2 - ((minX + maxX) / 2) * scale;
  panY = H / 2 - ((minY + maxY) / 2) * scale;
  centered = true;
}

// --- rendering -------------------------------------------------------------
function draw() {
  autoCenter();
  ctx.clearRect(0, 0, W, H);
  const dark = getComputedStyle(document.body).color === "rgb(232, 234, 237)";

  // Ring guide circles + cluster labels first, so nodes/edges sit on top.
  ctx.strokeStyle = dark ? "rgba(160,170,180,0.30)" : "rgba(96,125,139,0.28)";
  ctx.fillStyle = dark ? "#9aa0a6" : "#607d8b";
  ctx.textAlign = "center";
  ctx.font = "12px Helvetica, Arial, sans-serif";
  for (const c of clusters) {
    const p = toScreen({ x: c.cx, y: c.cy });
    for (const rr of c.rings) {
      if (rr <= 0) continue;
      ctx.beginPath(); ctx.arc(p.x, p.y, rr * scale, 0, Math.PI * 2); ctx.stroke();
    }
    const top = Math.max(0, ...c.rings, 40) * scale;
    if (scale > 0.4) ctx.fillText(c.label, p.x, p.y - top - 6);
  }

  ctx.lineWidth = 1;
  ctx.strokeStyle = dark ? "rgba(200,200,200,0.35)" : "rgba(96,125,139,0.35)";
  for (const e of edges) {
    const s = toScreen(e.s), t = toScreen(e.t);
    ctx.beginPath(); ctx.moveTo(s.x, s.y); ctx.lineTo(t.x, t.y); ctx.stroke();
  }

  const showLabels = scale > 0.55;
  for (const n of nodes) {
    const p = toScreen(n);
    const r = n.r * scale;
    ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
    ctx.fillStyle = n.color; ctx.fill();
    if (n.public) { ctx.lineWidth = 2.5; ctx.strokeStyle = "#e53935"; ctx.stroke(); }
    ctx.lineWidth = n.synthetic ? 1.5 : 1;
    ctx.setLineDash(n.synthetic ? [3, 3] : []);
    ctx.strokeStyle = "#37474f"; ctx.stroke(); ctx.setLineDash([]);
    if (showLabels) {
      ctx.fillStyle = getComputedStyle(document.body).color;
      ctx.font = "11px Helvetica, Arial, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(n.label || n.id, p.x, p.y + r + 12);
      if (n.detail) {
        ctx.fillStyle = "#78909c"; ctx.font = "9px Helvetica, Arial, sans-serif";
        ctx.fillText(n.detail, p.x, p.y + r + 23);
      }
    }
  }
}

// --- interaction (drag a node, drag background to pan, wheel to zoom) -------
let dragNode = null, dragging = false, lastX = 0, lastY = 0;
function nodeAt(sx, sy) {
  const w = fromScreen(sx, sy);
  for (let i = nodes.length - 1; i >= 0; i--) {
    const n = nodes[i];
    const dx = n.x - w.x, dy = n.y - w.y;
    if (dx * dx + dy * dy <= (n.r + 3) * (n.r + 3)) return n;
  }
  return null;
}
canvas.addEventListener("mousedown", (ev) => {
  lastX = ev.clientX; lastY = ev.clientY;
  dragNode = nodeAt(ev.clientX, ev.clientY);
  if (!dragNode) dragging = true;
});
window.addEventListener("mousemove", (ev) => {
  if (dragNode) {
    const w = fromScreen(ev.clientX, ev.clientY);
    dragNode.x = w.x; dragNode.y = w.y; draw();
  } else if (dragging) {
    panX += ev.clientX - lastX; panY += ev.clientY - lastY;
    lastX = ev.clientX; lastY = ev.clientY; draw();
  }
});
window.addEventListener("mouseup", () => { dragNode = null; dragging = false; });

// Zoom about a screen point, clamped, keeping that point stationary, then repaint. Shared by
// the wheel and the +/- buttons (which zoom about the viewport center).
function zoomAround(px, py, factor) {
  const w = fromScreen(px, py);
  scale = Math.max(0.1, Math.min(6, scale * factor));
  panX = px - w.x * scale; panY = py - w.y * scale; draw();
}
let scrollZoomEnabled = true;   // the "lock scroll-zoom" toggle flips this off
canvas.addEventListener("wheel", (ev) => {
  if (!scrollZoomEnabled) return;               // locked: only the +/- buttons zoom
  ev.preventDefault();
  zoomAround(ev.clientX, ev.clientY, ev.deltaY < 0 ? 1.1 : 1 / 1.1);
}, { passive: false });
document.getElementById("zoomIn")
  .addEventListener("click", () => zoomAround(W / 2, H / 2, 1.2));
document.getElementById("zoomOut")
  .addEventListener("click", () => zoomAround(W / 2, H / 2, 1 / 1.2));
document.getElementById("noscroll").addEventListener("change", (ev) => {
  scrollZoomEnabled = !ev.target.checked;
});

// --- legend ----------------------------------------------------------------
(function legend() {
  const el = document.getElementById("legend");
  const seen = new Map();
  for (const n of GRAPH.nodes) if (!seen.has(n.type)) seen.set(n.type, n.color);
  for (const [type, color] of seen) {
    const sw = document.createElement("span");
    sw.className = "swatch"; sw.style.background = color;
    const lb = document.createElement("span"); lb.textContent = type;
    el.appendChild(sw); el.appendChild(lb);
  }
  if (GRAPH.nodes.some((n) => n.public)) {
    const sw = document.createElement("span");
    sw.className = "swatch exposed"; sw.style.background = "transparent";
    const lb = document.createElement("span"); lb.textContent = "public IP (exposed)";
    el.appendChild(sw); el.appendChild(lb);
  }
})();

resize();
</script>
</body>
</html>
"""
