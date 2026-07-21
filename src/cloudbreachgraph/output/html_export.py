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
  #hint { margin-top: 8px; color: #607d8b; }
  @media (prefers-color-scheme: dark) {
    body { background: #202124; color: #e8eaed; }
    #hud { background: rgba(40,42,45,0.92); border-color: #3c4043; }
    #hud .muted, #hint { color: #9aa0a6; }
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
  <div id="hint">drag a node to pin · scroll to zoom · drag background to pan</div>
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
let alpha = 1.0;             // cooling factor (1 -> 0)

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
      const f = (Math.sqrt(a.charge * b.charge) / d2) * alpha;
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
  // Gravity + integrate.
  for (const n of nodes) {
    if (n.fixed) { n.vx = 0; n.vy = 0; continue; }
    n.vx -= n.x * GRAVITY * alpha; n.vy -= n.y * GRAVITY * alpha;
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
  } else if (dragging) {
    panX += ev.clientX - lastX; panY += ev.clientY - lastY;
    lastX = ev.clientX; lastY = ev.clientY; centered = true;
  }
});
window.addEventListener("mouseup", () => { dragNode = null; dragging = false; });
canvas.addEventListener("wheel", (ev) => {
  ev.preventDefault();
  const factor = ev.deltaY < 0 ? 1.1 : 1 / 1.1;
  const w = fromScreen(ev.clientX, ev.clientY);
  scale = Math.max(0.1, Math.min(6, scale * factor));
  panX = ev.clientX - w.x * scale; panY = ev.clientY - w.y * scale; centered = true;
}, { passive: false });

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
