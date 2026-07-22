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
then everything else (EC2 instances, load balancers). The ENI ring is the angular anchor: a
subnet is placed at the mean angle of the ENIs it contains, and an EC2/LB at the mean angle
of the ENIs attached to it, so each stays radially next to its interfaces.
Unlike the force layout its positions are computed deterministically in Python, so the page
needs no in-browser relaxation. It shares the same :data:`MAX_NODES` / :data:`MAX_HTML_BYTES`
size guard and the same ``None``-means-fall-back-to-``.dot`` contract.

A third, **overlap-free** layout (:func:`write_optimized_html` / :func:`build_optimized_html`,
exposed by ``cloudbreachgraph-to-html --max-passes N``) also computes positions in Python, but
its objective is legibility of the rendering rather than a fixed shape: it runs up to ``N``
optimisation passes and guarantees that, once it converges, **no two node disks overlap** and
**no edge is drawn across a node it is not connected to** (an "edge overlap"). Real topologies
are non-planar (the example graph's largest VPC alone contains a non-planar minor), so zero
edge *crossings* is impossible — this layout instead removes the two overlaps that actually
hurt readability and, as a **secondary** objective, minimises edge crossings (a bounded greedy
local search, ~halving them on the example graph) without giving up the overlap guarantee. It
shares the same size guard and ``.dot`` fallback as the other two.

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
import random
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

# Drawn node radius per type — kept in step with the page's ``radiusFor`` so the Python-side
# overlap check uses the same disk sizes the browser draws.
_NODE_RADII: dict[str, float] = {
    "vpc": 16.0,
    "load_balancer": 13.0,
    "ec2_instance": 11.0,
    "subnet": 11.0,
}
_DEFAULT_NODE_RADIUS = 9.0


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
# balancers) sits on the **outer ring**. The ENI ring is the **angular anchor**: each subnet
# is placed at the **mean angle of the ENIs it contains** and each EC2/LB at the **mean angle
# of the ENIs attached to it**, and ENIs are ordered by subnet on their ring so a subnet's
# interfaces form one contiguous arc it sits radially inside. Multiple
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

# Per-pass cooling factor for the --optimize-passes refinement (see _optimize_cluster): each
# pass moves nodes only `alpha` of the way to their barycenter, and alpha *= this each pass, so
# movement decays to zero and the layout freezes to a stable state regardless of the pass count.
_OPT_COOLING = 0.9

# After the barycenter passes, a greedy crossing-reduction local search (`_reduce_crossings`)
# relocates each node to the same-ring slot with the fewest incident edge crossings. Bounded so
# the O(ring² · edges) sweep stays cheap; skipped for clusters larger than the node cap (the
# barycenter result stands there).
_RELOC_SWEEPS = 8
_RELOC_MAX_NODES = 260


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


def _place_aligned_to_enis(
    members: list,
    cx: float,
    cy: float,
    radius: float,
    eni_angle: dict[str, float],
    enis_by_member: dict[str, list[str]],
) -> None:
    """Place each member at the (circular-mean) angle of the ENIs associated with it.

    ``enis_by_member`` maps a member id to the ENI ids it should line up with, and
    ``eni_angle`` gives each ENI's angle on its own ring. Used for both neighbours of the ENI
    ring: the **subnet** ring (a subnet aligns to the ENIs it contains) and the **outer** ring
    (an EC2 instance / load balancer aligns to the ENIs attached to it). A single ENI puts the
    member on exactly that spoke; several average out. Members with no associated ENI (e.g.
    orphans surfaced by ``--include-orphans``) fall back to even spacing.
    """
    aligned: list[tuple[dict, float]] = []
    unaligned: list[dict] = []
    for m in members:
        angs = [eni_angle[e] for e in enis_by_member.get(m["id"], []) if e in eni_angle]
        (aligned.append((m, _circular_mean(angs))) if angs else unaligned.append(m))
    for m, angle in aligned:
        _place_at_angle(m, cx, cy, radius, angle)
    count = len(unaligned)
    for i, m in enumerate(unaligned):
        _place_at_angle(m, cx, cy, radius, -math.pi / 2 + (2 * math.pi * i / count if count else 0))


def _ang_diff(a: float, b: float) -> float:
    """Signed smallest angular difference ``a - b`` in ``(-π, π]``."""
    d = (a - b) % (2 * math.pi)
    return d - 2 * math.pi if d > math.pi else d


def _isotonic_l2(y: list[float]) -> list[float]:
    """Least-squares non-decreasing fit of ``y`` (pool-adjacent-violators). Deterministic, O(n)."""
    blocks: list[list[float]] = []  # each: [mean, count, total]
    for yi in y:
        blocks.append([yi, 1.0, yi])
        while len(blocks) >= 2 and blocks[-2][0] > blocks[-1][0]:
            _, c2, t2 = blocks.pop()
            _, c1, t1 = blocks.pop()
            total, count = t1 + t2, c1 + c2
            blocks.append([total / count, count, total])
    out: list[float] = []
    for mean, count, _ in blocks:
        out.extend([mean] * int(count))
    return out


def _place_min_gap(members: list, cx: float, cy: float, radius: float, target: dict) -> None:
    """Place ``members`` near their ``target`` angle, spread just enough to not overlap.

    Unlike even spacing, this lets connected nodes actually sit *close* together (only a
    minimum angular gap apart, sized so their disks don't touch), so e.g. two subnets that share
    a load balancer end up adjacent rather than parked at distant even slots. Implemented as an
    L2 isotonic projection: sort by target, cut the circle at its widest gap (so the unavoidable
    wrap-around slack lands where it's least binding), then find the ordered positions closest to
    the targets subject to the min-gap — a min-gap-shifted isotonic regression. Reorders
    ``members`` in place to match the placement.
    """
    n = len(members)
    if n == 0:
        return
    members.sort(key=lambda m: (target[m["id"]], m["id"]))
    if n == 1:
        _place_at_angle(members[0], cx, cy, radius, target[members[0]["id"]])
        return

    ts = [target[m["id"]] for m in members]  # ascending in (-π, π]
    max_r = max(_node_radius(m) for m in members)
    gap = min((2 * max_r + 8.0) / radius, 2 * math.pi / n)  # never exceed even spacing

    # Cut after the widest gap between consecutive (circular) targets.
    gaps = [(ts[(i + 1) % n] - ts[i]) % (2 * math.pi) for i in range(n)]
    cut = max(range(n), key=lambda i: gaps[i])
    order = [(cut + 1 + k) % n for k in range(n)]

    # Unwrap the targets into a monotonically increasing sequence in this order.
    unwrapped: list[float] = []
    for idx in order:
        a = ts[idx]
        while unwrapped and a < unwrapped[-1] - 1e-12:
            a += 2 * math.pi
        unwrapped.append(a)

    # Positions minimizing Σ(p_i - t_i)² s.t. p_{i+1} - p_i ≥ gap: subtract i·gap, project to
    # non-decreasing, add it back.
    shifted = _isotonic_l2([a - i * gap for i, a in enumerate(unwrapped)])
    reordered = [members[i] for i in order]
    for k, m in enumerate(reordered):
        _place_at_angle(m, cx, cy, radius, shifted[k] + k * gap)
    members[:] = reordered


def _node_radius(node: dict) -> float:
    return _NODE_RADII.get(node["type"], _DEFAULT_NODE_RADIUS)


def _nudge_overlaps(nodes: list, iterations: int = 12, pad: float = 6.0) -> None:
    """Separate any overlapping node disks with small symmetric push-apart steps.

    Even placement already prevents intra-ring overlap and the ring gap prevents cross-ring
    overlap, so on real topologies this early-exits after one clean scan; it's a safety net for
    pathological small-radius clusters. Deterministic: coincident nodes are split along a fixed
    axis. Kept O(n²) per iteration but bounded and early-exiting.
    """
    for _ in range(iterations):
        moved = False
        for i in range(len(nodes)):
            a = nodes[i]
            for j in range(i + 1, len(nodes)):
                b = nodes[j]
                dx, dy = a["x"] - b["x"], a["y"] - b["y"]
                dist = math.hypot(dx, dy)
                mind = _node_radius(a) + _node_radius(b) + pad
                if dist >= mind:
                    continue
                if dist < 1e-9:  # exactly coincident: split along x, deterministically
                    a["x"] += mind / 2
                    b["x"] -= mind / 2
                else:
                    push = (mind - dist) / 2
                    a["x"] += dx / dist * push
                    a["y"] += dy / dist * push
                    b["x"] -= dx / dist * push
                    b["y"] -= dy / dist * push
                moved = True
        if not moved:
            break
    for m in nodes:
        m["x"], m["y"] = round(m["x"], 2), round(m["y"], 2)


def _orient(a: tuple, b: tuple, c: tuple) -> int:
    """Sign of the cross product (a→b)×(a→c): +1 ccw, −1 cw, 0 collinear."""
    v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    return (v > 1e-9) - (v < -1e-9)


def _reduce_crossings(node_by_id: dict, cedges: list, rings: dict, cx: float, cy: float, rs: list):
    """Greedy local search: relocate each node to the same-ring slot with the fewest crossings.

    Moving a single node only changes crossings on that node's own edges (all its edges share it
    as an endpoint, so they never cross each other), so accepting a move that strictly lowers its
    incident-crossing count never raises the global total — the search is a monotone crossing
    minimiser. It complements the barycenter passes, which optimise proximity but get stuck in
    local minima where whole spokes still cross (dominant on the outer ring, where a load
    balancer fans edges to ENIs in several subnets). Candidate slots are the mid-angles of the
    gaps between the other nodes on the ring; ``_nudge_overlaps`` afterwards fixes any tight
    insertion. Deterministic (nodes visited in id order; only strict improvements accepted).
    """

    def pt(nid: str) -> tuple:
        m = node_by_id[nid]
        return (m["x"], m["y"])

    incident: dict[str, list[int]] = {}
    for k, (a, b) in enumerate(cedges):
        incident.setdefault(a, []).append(k)
        incident.setdefault(b, []).append(k)

    def crosses(k: int, j: int) -> bool:
        e, f = cedges[k], cedges[j]
        if e[0] in f or e[1] in f:  # edges sharing a node don't count as a crossing
            return False
        p, q, r, s = pt(e[0]), pt(e[1]), pt(f[0]), pt(f[1])
        return _orient(p, q, r) != _orient(p, q, s) and _orient(r, s, p) != _orient(r, s, q)

    def incident_crossings(nid: str) -> int:
        return sum(
            1 for k in incident.get(nid, ()) for j in range(len(cedges)) if j != k and crosses(k, j)
        )

    for _ in range(_RELOC_SWEEPS):
        moved = False
        for r in (1, 2, 3):
            ids = rings.get(r, [])
            if len(ids) < 3:
                continue
            radius = rs[r - 1]
            for nid in sorted(ids):
                m = node_by_id[nid]
                start_xy = (m["x"], m["y"])
                best_xy, best = start_xy, incident_crossings(nid)
                others = sorted(math.atan2(pt(x)[1] - cy, pt(x)[0] - cx) for x in ids if x != nid)
                for i in range(len(others)):
                    wrap = 2 * math.pi if i + 1 == len(others) else 0
                    mid = (others[i] + others[(i + 1) % len(others)] + wrap) / 2
                    m["x"] = round(cx + radius * math.cos(mid), 2)
                    m["y"] = round(cy + radius * math.sin(mid), 2)
                    c = incident_crossings(nid)
                    if c < best:  # strict improvement only -> deterministic, monotone
                        best, best_xy = c, (m["x"], m["y"])
                m["x"], m["y"] = best_xy
                if best_xy != start_xy:
                    moved = True
        if not moved:
            break


def _optimize_cluster(bucket: dict, cx: float, cy: float, rs: list, adj: dict, passes: int) -> None:
    """Move nodes within each ring toward their neighbours to cut crossings, then de-overlap.

    A barycenter (mean-of-neighbours) heuristic: on each ring every node is aimed at the mean
    angle of its neighbours on the *other* rings and placed there — as close as a minimum,
    overlap-free gap allows (:func:`_place_min_gap`), so connected nodes genuinely cluster rather
    than snapping to distant even slots. The sweep repeats up to ``passes`` times, outward then
    back inward each pass so positions propagate across all four rings. This is what pulls two
    subnets that share a load balancer next to each other, so its edges stop crossing the circle.
    The centre VPC (ring 0) has no angle and is skipped as a neighbour. Stops early once a full
    pass moves every node less than a small epsilon (so extra passes never churn the layout).
    """
    ring_of = {m["id"]: r for r in range(_RING_COUNT) for m in bucket[r]}
    angle = {m["id"]: math.atan2(m["y"] - cy, m["x"] - cx) for r in (1, 2, 3) for m in bucket[r]}

    # Cooling: each node is moved only a fraction `alpha` of the way to its barycenter, and
    # `alpha` decays every pass. Without it the barycenter iteration on a real graph never
    # settles — it drifts around a limit cycle of equal-crossing layouts, so the coordinates
    # (and the emitted bytes) would depend on the exact pass count. Geometric cooling forces the
    # movement to zero, so the layout *freezes* and any large pass count yields the same result.
    alpha = 1.0
    for _ in range(passes):
        max_move = 0.0
        for r in (1, 2, 3, 2):  # outward then back inward, so positions propagate both ways
            members = bucket[r]
            if not members:
                continue
            target = {}
            for m in members:
                neigh = [
                    angle[nid]
                    for nid in adj.get(m["id"], ())
                    if ring_of.get(nid, 0) != 0 and nid in angle
                ]
                cur = angle[m["id"]]
                bary = _circular_mean(neigh) if neigh else cur
                target[m["id"]] = cur + alpha * _ang_diff(bary, cur)  # damped step toward it
            _place_min_gap(members, cx, cy, rs[r - 1], target)
            for m in members:
                a = math.atan2(m["y"] - cy, m["x"] - cx)
                max_move = max(max_move, abs(_ang_diff(a, angle[m["id"]])))
                angle[m["id"]] = a
        alpha *= _OPT_COOLING
        if max_move < 1e-4:  # frozen — further passes would not change anything
            break

    # Greedy crossing-reduction on the settled layout: relocate nodes to lower-crossing slots.
    # The barycenter passes optimise proximity but leave whole spokes crossing (mostly on the
    # outer ring); this directly removes those. Bounded to modest clusters so it stays cheap.
    node_by_id = {m["id"]: m for r in range(_RING_COUNT) for m in bucket[r]}
    if 0 < len(node_by_id) <= _RELOC_MAX_NODES:
        ids_set = set(node_by_id)
        cedges = sorted(
            {
                tuple(sorted((nid, nb)))
                for nid in ids_set
                for nb in adj.get(nid, ())
                if nb in ids_set
            }
        )
        rings = {r: [m["id"] for m in bucket[r]] for r in (1, 2, 3)}
        _reduce_crossings(node_by_id, cedges, rings, cx, cy, rs)

    _nudge_overlaps([m for r in (1, 2, 3) for m in bucket[r]])


def _adjacency(graph: Graph) -> dict[str, list[str]]:
    """Undirected node adjacency from the graph's edges (deterministic order)."""
    adj: dict[str, list[str]] = {}
    for e in graph.edges:
        adj.setdefault(e.source, []).append(e.target)
        adj.setdefault(e.target, []).append(e.source)
    return adj


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


def _ringed_view_data(graph: Graph, passes: int = 0) -> dict:
    """Build the ringed page payload: nodes with fixed x/y, edges, and per-cluster metadata.

    Reuses :func:`_view_data`'s per-node fields (colors, exposure flag, detail line) and adds
    computed ``x``/``y``. ``clusters`` carries each VPC cluster's center, ring radii and label
    — used to place the cluster label and fit the view; the rings themselves are conveyed by
    node position alone (no guide circles are drawn).

    With ``passes > 0`` each cluster is post-processed by :func:`_optimize_cluster`: up to that
    many barycenter passes reorder nodes within their rings to pull connected nodes together
    (fewer crossing edges) and separate any overlaps. ``passes == 0`` (the default) leaves the
    deterministic ENI-aligned placement byte-for-byte unchanged.
    """
    base = _view_data(graph)
    by_id = {n["id"]: n for n in base["nodes"]}
    group_of = _vpc_group_of(graph)

    # ENI adjacency used to angle-align the ENI ring's two neighbours. graph.edges is sorted,
    # so every per-node ENI list is deterministic.
    #  * enis_of_lb: outer-ring node (ec2/lb) -> the ENIs attached to it (via `attached_to`).
    #  * enis_of_subnet: subnet -> the ENIs it contains (via `in_subnet`).
    #  * subnet_of_eni: ENI -> its subnet, used to group ENIs by subnet on ring 2.
    enis_of_lb: dict[str, list[str]] = {}
    enis_of_subnet: dict[str, list[str]] = {}
    subnet_of_eni: dict[str, str] = {}
    for e in graph.edges:
        if e.relationship == "attached_to":
            enis_of_lb.setdefault(e.target, []).append(e.source)
        elif e.relationship == "in_subnet":
            enis_of_subnet.setdefault(e.target, []).append(e.source)
            subnet_of_eni.setdefault(e.source, e.target)

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
    adj = _adjacency(graph) if passes > 0 else {}

    clusters = []
    for i, g in enumerate(order):
        cx = (i % cols) * cell
        cy = (i // cols) * cell
        rs = radii[g]
        _place_on_ring(groups[g][0], cx, cy, 0.0)  # VPC center (0..1 nodes)
        # ring 2: ENIs, evenly spaced but ordered so a subnet's ENIs are contiguous (grouped by
        # subnet id, then ENI id) — this makes each subnet's ENIs a single arc it can center on.
        enis = sorted(groups[g][2], key=lambda m: (subnet_of_eni.get(m["id"], ""), m["id"]))
        eni_angle = _place_on_ring(enis, cx, cy, rs[1])
        # ring 1: subnets aligned to the mean angle of the ENIs they contain, so a subnet sits
        # radially inward from its own block of ENIs (its interfaces stay near it).
        _place_aligned_to_enis(groups[g][1], cx, cy, rs[0], eni_angle, enis_of_subnet)
        # ring 3: EC2/LBs aligned to the mean angle of the ENIs attached to each.
        _place_aligned_to_enis(groups[g][3], cx, cy, rs[2], eni_angle, enis_of_lb)
        # Optional: reorder within rings to cut edge crossings and nudge apart overlaps.
        if passes > 0:
            _optimize_cluster(groups[g], cx, cy, rs, adj, passes)
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


def build_ringed_html(graph: Graph, passes: int = 0) -> str:
    """Return the complete, self-contained ringed HTML document for ``graph`` as a string.

    ``passes`` (default 0) is the max number of crossing-reduction passes; see
    :func:`_ringed_view_data`.
    """
    data_json = json.dumps(_ringed_view_data(graph, passes), ensure_ascii=False, default=str)
    return (
        _render_static_layout(data_json, "ringed", _RINGED_HINT)
        .replace("__NODE_COUNT__", str(len(graph.nodes)))
        .replace("__EDGE_COUNT__", str(len(graph.edges)))
    )


def write_ringed_html(
    graph: Graph,
    path: str | Path,
    *,
    max_nodes: int | None = None,
    max_bytes: int | None = None,
    passes: int = 0,
) -> Path | None:
    """Write ``graph`` to ``path`` as the self-contained **ringed** HTML page.

    Same contract and size guard as :func:`write_html` (VPC-centered concentric rings instead
    of a force layout): returns the :class:`~pathlib.Path` written, or ``None`` when the graph
    is too large (more than ``max_nodes`` nodes, or a rendered page over ``max_bytes`` bytes),
    in which case **no file is written** so the caller can fall back to the ``.dot`` output.
    ``passes`` (default 0) is the max number of crossing-reduction passes; see
    :func:`_ringed_view_data`.
    """
    node_cap = MAX_NODES if max_nodes is None else max_nodes
    byte_cap = MAX_HTML_BYTES if max_bytes is None else max_bytes

    if len(graph.nodes) > node_cap:
        return None

    html = build_ringed_html(graph, passes)
    if len(html.encode("utf-8")) > byte_cap:
        return None

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


# --------------------------------------------------------------------------- #
# Overlap-elimination layout (--max-passes).
#
# A third deterministic, Python-computed layout whose single promise is that the *rendering* is
# legible: after it converges, **no two node disks overlap** and **no edge is drawn across a
# node it is not incident to**. We call the latter an *edge overlap* — the natural counterpart
# of a node overlap in a straight-line node-link drawing. Real AWS topologies are non-planar
# (the example graph's largest VPC contains a non-planar minor), so a drawing with zero edge
# *crossings* cannot exist; this optimiser therefore targets the two overlaps that genuinely
# impair readability rather than an unreachable crossing-free ideal.
#
# It runs up to ``max_passes`` *optimisation passes*, each counting one against the budget, in
# two phases and stops the moment both overlap counts hit zero:
#
#   1. **Unfolding** — a cooled force-directed relaxation (degree-weighted repulsion between all
#      pairs, springs along edges, a weak centering gravity) spreads the nodes into a roomy,
#      low-energy arrangement. Bounded to :data:`_OPT_FORCE_PASSES` passes.
#   2. **Projection** — repeated hard geometric sweeps: first push any overlapping node disks
#      apart, then push any node off an edge that crosses it (to a hair beyond touching). Because
#      each sweep only *moves* when a violation exists, a sweep that moves nothing is a
#      certificate that both overlap counts are exactly zero. With the room phase 1 created this
#      converges quickly (~100 sweeps on the example graph); if the budget runs out first the
#      best arrangement reached is emitted.
#   3. **Crossing reduction** — a bounded greedy local search that relocates each crossing-incident
#      node to the nearby candidate slot with the fewest incident edge crossings (moving one node
#      only changes crossings on its own edges, so an accepted strict improvement never raises the
#      global total). Real topologies are non-planar so zero crossings is unreachable, but this is
#      a *secondary* objective — it typically roughly halves the crossings (39 -> 18 on the example
#      graph) — and a **final projection** afterwards restores the overlap-free guarantee that
#      remains the layout's primary promise.
#
# Determinism (``docs/04_conventions.md``): positions come from a fixed initial spiral, a fixed
# PRNG seed (used only to jitter exactly-coincident nodes), a fixed iteration order over the
# already-sorted nodes/edges, and are rounded to 2 dp — so a given graph and pass count always
# yield byte-identical HTML.
# --------------------------------------------------------------------------- #
_OPT_SEED = 0x1F2E3D4C  # fixed PRNG seed -> byte-stable output (only jitters coincident nodes)
_OPT_MARGIN = 4.0  # clearance beyond bare touching that the projection sweep enforces
_OPT_FORCE_PASSES = 400  # cap on phase-1 unfolding passes (the rest of the budget is projection)
_OPT_PROJECT_MIN = 120  # ensure projection always gets at least this many passes when affordable

# Phase-1 force constants. Tuned so a real cluster unfolds with plenty of room for phase 2 to
# then clear every overlap; deliberately roomy (large repulsion / rest lengths) because empty
# space is what lets the projection sweep converge to zero instead of oscillating.
_OPT_REPULSION = 9000.0
_OPT_SPRING = 0.02
_OPT_DAMPING = 0.9
_OPT_GRAVITY = 0.005
_OPT_COOL = 0.99  # per-pass cooling of the force alpha
_OPT_ALPHA_FLOOR = 0.05

# Phase-3 crossing-reduction constants. Candidate slots for a relocated node are the neighbour
# barycenter plus a ring of probes around it (radii x angles); bounded sweeps keep it cheap.
_OPT_REDUCE_SWEEPS = 8
_OPT_REDUCE_RADII = (25.0, 55.0, 95.0, 150.0)
_OPT_REDUCE_ANGLES = 12
# The relocation can leave a tight spot the projection can only oscillate in; cap the restoring
# projection so it can't burn the whole budget, and fall back to the phase-2 layout if it doesn't
# settle overlap-free within the cap (crossings not reduced, but the primary guarantee is kept).
_OPT_REDUCE_PROJECT_CAP = 400


def _seg_point_dist(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> tuple:
    """Distance from point ``p`` to segment ``ab`` and its closest point, as ``(d, cx, cy)``."""
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 < 1e-12:  # degenerate segment (coincident endpoints) -> distance to the point
        return math.hypot(px - ax, py - ay), ax, ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy), cx, cy


def _count_overlaps(nodes: list, edges: list) -> tuple[int, int]:
    """Count ``(node_node, edge_node)`` overlaps in a laid-out payload.

    A *node overlap* is two node disks intersecting; an *edge overlap* is a non-incident node's
    disk intersecting an edge segment (the edge is drawn across that node). Uses the drawn radii
    (:data:`_NODE_RADII`) so the counts match what the browser paints. Strict (bare touching is
    not an overlap), so the projection sweep's ``+_OPT_MARGIN`` target drives both to exactly 0.
    """
    pos = {n["id"]: (n["x"], n["y"], _node_radius(n)) for n in nodes}
    node_node = 0
    ids = [n["id"] for n in nodes]
    for i in range(len(ids)):
        ax, ay, ar = pos[ids[i]]
        for j in range(i + 1, len(ids)):
            bx, by, br = pos[ids[j]]
            if math.hypot(ax - bx, ay - by) < ar + br - 1e-6:
                node_node += 1
    edge_node = 0
    for e in edges:
        a, b = e["source"], e["target"]
        if a not in pos or b not in pos:
            continue
        ax, ay, _ = pos[a]
        bx, by, _ = pos[b]
        for nid in ids:
            if nid == a or nid == b:
                continue
            px, py, pr = pos[nid]
            d, _, _ = _seg_point_dist(px, py, ax, ay, bx, by)
            if d < pr - 1e-6:
                edge_node += 1
    return node_node, edge_node


def _seg_seg_cross(
    ax: float, ay: float, bx: float, by: float, cx: float, cy: float, dx: float, dy: float
) -> bool:
    """Whether open segments ``ab`` and ``cd`` properly cross (callers exclude shared endpoints)."""
    d1 = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    d2 = (bx - ax) * (dy - ay) - (by - ay) * (dx - ax)
    d3 = (dx - cx) * (ay - cy) - (dy - cy) * (ax - cx)
    d4 = (dx - cx) * (by - cy) - (dy - cy) * (bx - cx)
    s1 = (d1 > 1e-9) - (d1 < -1e-9)
    s2 = (d2 > 1e-9) - (d2 < -1e-9)
    s3 = (d3 > 1e-9) - (d3 < -1e-9)
    s4 = (d4 > 1e-9) - (d4 < -1e-9)
    return s1 != s2 and s3 != s4


def _count_crossings(nodes: list, edges: list) -> int:
    """Number of pairs of edges whose segments cross (shared-endpoint pairs never count)."""
    pos = {n["id"]: (n["x"], n["y"]) for n in nodes}
    ep = [(e["source"], e["target"]) for e in edges if e["source"] in pos and e["target"] in pos]
    total = 0
    for i in range(len(ep)):
        a, b = ep[i]
        ax, ay = pos[a]
        bx, by = pos[b]
        for j in range(i + 1, len(ep)):
            c, d = ep[j]
            if a == c or a == d or b == c or b == d:
                continue
            cx, cy = pos[c]
            dx, dy = pos[d]
            if _seg_seg_cross(ax, ay, bx, by, cx, cy, dx, dy):
                total += 1
    return total


def _separate_overlaps(xs: list, ys: list, epairs: list, radii: list, n: int) -> bool:
    """One hard geometric projection sweep; returns whether it moved anything.

    (a) separates overlapping node disks, then (b) pushes any node off an edge it is drawn across
    (to :data:`_OPT_MARGIN` beyond touching). A sweep that returns ``False`` is a certificate that
    no node-node and no edge-over-node overlap remains.
    """
    moved = False
    for i in range(n):
        for j in range(i + 1, n):
            dx = xs[i] - xs[j]
            dy = ys[i] - ys[j]
            dist = math.hypot(dx, dy)
            mind = radii[i] + radii[j] + _OPT_MARGIN
            if dist < mind:
                if dist < 1e-9:  # exactly coincident: split along a fixed axis
                    dx, dy, dist = 1.0, 0.0, 1.0
                push = (mind - dist) / 2.0
                ux, uy = dx / dist, dy / dist
                xs[i] += ux * push
                ys[i] += uy * push
                xs[j] -= ux * push
                ys[j] -= uy * push
                moved = True
    for a, b in epairs:
        ax, ay, bx, by = xs[a], ys[a], xs[b], ys[b]
        for kidx in range(n):
            if kidx == a or kidx == b:
                continue
            d, cx, cy = _seg_point_dist(xs[kidx], ys[kidx], ax, ay, bx, by)
            need = radii[kidx] + _OPT_MARGIN
            if d < need:
                ddx = xs[kidx] - cx
                ddy = ys[kidx] - cy
                dl = math.hypot(ddx, ddy)
                if dl < 1e-9:  # node sits on the segment: push along the edge normal
                    ddx, ddy = -(by - ay), (bx - ax)
                    dl = math.hypot(ddx, ddy) or 1.0
                push = need - d
                xs[kidx] += ddx / dl * push
                ys[kidx] += ddy / dl * push
                moved = True
    return moved


# Clear gap left between adjacent packed components (px), so independent clusters read as separate.
_OPT_CLUSTER_GAP = 160.0


def _connected_components(nodes: list, edges: list) -> list:
    """Split a payload into connected components as ``[(comp_nodes, comp_edges), ...]``.

    Components are ordered by the first node's position in the (already sorted) ``nodes`` list, and
    each component's nodes/edges keep that sorted order — so the split is deterministic. Edges only
    ever join nodes within one component (an edge across two would merge them), so every edge lands
    in exactly one component and crossings/overlaps are a purely per-component concern.
    """
    by_id = {node["id"]: node for node in nodes}
    adj: dict[str, list[str]] = {node["id"]: [] for node in nodes}
    for e in edges:
        a, b = e["source"], e["target"]
        if a in adj and b in adj and a != b:
            adj[a].append(b)
            adj[b].append(a)

    comp_of: dict[str, int] = {}
    count = 0
    for node in nodes:  # BFS in sorted order -> deterministic component numbering
        if node["id"] in comp_of:
            continue
        queue = [node["id"]]
        comp_of[node["id"]] = count
        for cur in queue:
            for nb in adj[cur]:
                if nb not in comp_of:
                    comp_of[nb] = count
                    queue.append(nb)
        count += 1

    comp_nodes: list = [[] for _ in range(count)]
    for node in nodes:
        comp_nodes[comp_of[node["id"]]].append(node)
    comp_edges: list = [[] for _ in range(count)]
    for e in edges:
        a = e["source"]
        if a in comp_of and e["target"] in by_id:
            comp_edges[comp_of[a]].append(e)
    return list(zip(comp_nodes, comp_edges, strict=True))


def _pack_components(components: list) -> None:
    """Translate each already-laid-out component into its own cell of a grid so none overlap.

    Each component is shifted so its bounding box (node disks included) sits centered in a
    square grid cell sized to the largest component plus :data:`_OPT_CLUSTER_GAP`. This keeps
    independent clusters clearly apart — the reader can tell at a glance which nodes belong
    together and which components are disconnected. Mirrors the ringed layout's cluster tiling.
    Deterministic: components keep :func:`_connected_components`' order; ties in size don't matter.
    """
    if not components:
        return
    boxes = []  # (w, h, cx, cy) of each component's node-disk bounding box
    for comp_nodes, _ in components:
        min_x = min(node["x"] - _node_radius(node) for node in comp_nodes)
        max_x = max(node["x"] + _node_radius(node) for node in comp_nodes)
        min_y = min(node["y"] - _node_radius(node) for node in comp_nodes)
        max_y = max(node["y"] + _node_radius(node) for node in comp_nodes)
        boxes.append((max_x - min_x, max_y - min_y, (min_x + max_x) / 2, (min_y + max_y) / 2))

    cell = max(max(w, h) for w, h, _, _ in boxes) + _OPT_CLUSTER_GAP
    cols = max(1, math.ceil(math.sqrt(len(components))))
    for i, ((comp_nodes, _), (_, _, cx, cy)) in enumerate(zip(components, boxes, strict=True)):
        target_x = (i % cols) * cell
        target_y = (i // cols) * cell
        dx, dy = target_x - cx, target_y - cy
        for node in comp_nodes:
            node["x"] += dx
            node["y"] += dy


def _optimize_layout(nodes: list, edges: list, max_passes: int) -> int:
    """Lay out ``nodes`` (in place, setting ``x``/``y``) overlap-free, keeping components apart.

    Splits the graph into connected components, lays each one out independently
    (:func:`_layout_component` — overlap-free, crossings reduced), then packs the components into a
    non-overlapping grid (:func:`_pack_components`) so independent clusters stay visually separated.
    Returns the largest per-component pass count (the "rounds" the hardest cluster needed); each
    component gets the full ``max_passes`` budget since they optimise independently. Coordinates are
    rounded to 2 dp once, after packing, for byte-stable output.
    """
    for node in nodes:  # ensure coordinates on the trivial paths (empty graph / no budget)
        node.setdefault("x", 0.0)
        node.setdefault("y", 0.0)
    if not nodes or max_passes <= 0:
        return 0

    components = _connected_components(nodes, edges)
    passes = 0
    for comp_nodes, comp_edges in components:
        passes = max(passes, _layout_component(comp_nodes, comp_edges, max_passes))
    _pack_components(components)

    for node in nodes:
        node["x"] = round(node["x"], 2)
        node["y"] = round(node["y"], 2)
    return passes


def _layout_component(nodes: list, edges: list, max_passes: int) -> int:
    """Lay out one connected component (in place, setting raw ``x``/``y``) to remove overlaps.

    ``nodes`` are view-data node dicts (as from :func:`_view_data`) for a single connected
    component; ``edges`` are that component's ``{"source","target",...}`` dicts. Runs up to
    ``max_passes`` passes (see this section's header) and returns the number actually used. On
    convergence every node disk is disjoint from every other and from every non-incident edge, and
    edge crossings are reduced; if the budget is exhausted first the best arrangement reached is
    left in place. Coordinates are left **unrounded** so the caller can translate the component
    (:func:`_pack_components`) before a single final rounding. Deterministic.
    """
    n = len(nodes)
    for node in nodes:  # ensure every node has coordinates even on the trivial paths below
        node.setdefault("x", 0.0)
        node.setdefault("y", 0.0)
    if n == 0 or max_passes <= 0:
        return 0

    idx = {node["id"]: i for i, node in enumerate(nodes)}
    radii = [_node_radius(node) for node in nodes]
    epairs = [
        (idx[e["source"]], idx[e["target"]])
        for e in edges
        if e["source"] in idx and e["target"] in idx and e["source"] != e["target"]
    ]
    deg = [0] * n
    for a, b in epairs:
        deg[a] += 1
        deg[b] += 1
    rest = [110.0 + 11.0 * math.sqrt(max(deg[a], deg[b])) for a, b in epairs]

    rng = random.Random(_OPT_SEED)
    xs = [0.0] * n
    ys = [0.0] * n
    for i in range(n):  # deterministic golden-angle spiral, spaced roomily
        ang = i * 2.399963229728653
        r = 30.0 * math.sqrt(i + 1)
        xs[i] = math.cos(ang) * r
        ys[i] = math.sin(ang) * r
    vx = [0.0] * n
    vy = [0.0] * n

    passes = 0
    # Give projection a guaranteed share of the budget so a modest --max-passes still converges.
    force_budget = min(max_passes, _OPT_FORCE_PASSES)
    if max_passes - force_budget < _OPT_PROJECT_MIN:
        force_budget = max(0, max_passes - _OPT_PROJECT_MIN)

    # Phase 1: cooled force-directed unfolding.
    alpha = 1.0
    while passes < force_budget:
        for i in range(n):
            ax, ay = xs[i], ys[i]
            ci = _OPT_REPULSION * (1.0 + 0.7 * math.sqrt(deg[i]))
            for j in range(i + 1, n):
                dx = ax - xs[j]
                dy = ay - ys[j]
                d2 = dx * dx + dy * dy
                if d2 < 0.01:  # coincident: deterministic-but-seeded jitter to break the tie
                    dx = rng.random() - 0.5
                    dy = rng.random() - 0.5
                    d2 = dx * dx + dy * dy
                dist = math.sqrt(d2)
                rep = math.sqrt(ci * _OPT_REPULSION * (1.0 + 0.7 * math.sqrt(deg[j]))) / d2 * alpha
                fx = dx / dist * rep
                fy = dy / dist * rep
                vx[i] += fx
                vy[i] += fy
                vx[j] -= fx
                vy[j] -= fy
        for k, (a, b) in enumerate(epairs):
            dx = xs[b] - xs[a]
            dy = ys[b] - ys[a]
            dist = math.hypot(dx, dy) or 0.01
            f = _OPT_SPRING * (dist - rest[k]) * alpha
            fx = dx / dist * f
            fy = dy / dist * f
            vx[a] += fx
            vy[a] += fy
            vx[b] -= fx
            vy[b] -= fy
        for i in range(n):
            vx[i] -= xs[i] * _OPT_GRAVITY * alpha
            vy[i] -= ys[i] * _OPT_GRAVITY * alpha
            vx[i] *= _OPT_DAMPING
            vy[i] *= _OPT_DAMPING
            xs[i] += vx[i]
            ys[i] += vy[i]
        alpha = max(alpha * _OPT_COOL, _OPT_ALPHA_FLOOR)
        passes += 1

    # Phase 2: hard geometric projection until the layout is overlap-free or the budget is spent.
    # Stop as soon as it is *strictly* overlap-free: `_separate_overlaps` keeps a small margin, so
    # it can keep reporting movement (cycling within that band) long after the real overlaps clear.
    while passes < max_passes:
        moved = _separate_overlaps(xs, ys, epairs, radii, n)
        passes += 1
        if not moved or not _has_overlap(xs, ys, epairs, radii, n):
            break

    # Phase 3: reduce edge crossings (best effort), then re-project to keep the overlap-free
    # guarantee. Crossings can't be zeroed (non-planar), so this is a bounded greedy local search.
    # Phase 2 already produced an overlap-free layout; keep it as a safe fallback so the primary
    # guarantee survives even if relocation lands somewhere the projection can't fully clear.
    if passes < max_passes and len(epairs) > 1:
        safe_xs, safe_ys = xs[:], ys[:]
        passes = _reduce_crossings_free(xs, ys, epairs, radii, n, max_passes, passes)
        if _has_overlap(xs, ys, epairs, radii, n):
            xs[:], ys[:] = safe_xs, safe_ys

    for i, node in enumerate(nodes):  # raw floats; _optimize_layout rounds after packing
        node["x"] = xs[i]
        node["y"] = ys[i]
    return passes


def _reduce_crossings_free(
    xs: list, ys: list, epairs: list, radii: list, n: int, max_passes: int, passes: int
) -> int:
    """Greedy crossing-reduction on free positions, then a final overlap projection. Returns passes.

    Relocates each crossing-incident node to the nearby candidate slot (neighbour barycenter, or a
    ring of probes around it) with the fewest *incident* crossings — a monotone move, since a
    node's edges all share it and so never cross one another, an accepted strict improvement can't
    raise the global crossing count. Bounded to :data:`_OPT_REDUCE_SWEEPS` sweeps. A final
    :func:`_separate_overlaps` loop restores the zero-overlap property the relocation may perturb.
    Deterministic: nodes visited most-crossed-first with the index breaking ties.
    """
    nbr: list = [[] for _ in range(n)]
    inc: list = [[] for _ in range(n)]
    for k, (a, b) in enumerate(epairs):
        nbr[a].append(b)
        nbr[b].append(a)
        inc[a].append(k)
        inc[b].append(k)
    m_edges = len(epairs)

    def incident_crossings(v: int) -> int:
        total = 0
        for k in inc[v]:
            a, b = epairs[k]
            ax, ay, bx, by = xs[a], ys[a], xs[b], ys[b]
            for j in range(m_edges):
                if j == k:
                    continue
                c, d = epairs[j]
                if a == c or a == d or b == c or b == d:
                    continue
                if _seg_seg_cross(ax, ay, bx, by, xs[c], ys[c], xs[d], ys[d]):
                    total += 1
        return total

    for _ in range(_OPT_REDUCE_SWEEPS):
        if passes >= max_passes:
            break
        current = [incident_crossings(v) for v in range(n)]
        # Visit most-crossed nodes first; the index breaks ties so the sweep is deterministic.
        order = sorted(range(n), key=lambda v: (-current[v], v))
        improved = False
        for v in order:
            if current[v] == 0 or not nbr[v]:
                continue
            bx = math.fsum(xs[u] for u in nbr[v]) / len(nbr[v])
            by = math.fsum(ys[u] for u in nbr[v]) / len(nbr[v])
            base = incident_crossings(v)
            best = base
            best_x, best_y = xs[v], ys[v]
            candidates = [(bx, by)]  # the barycenter itself pulls a node's spokes together
            for rr in _OPT_REDUCE_RADII:
                for a in range(_OPT_REDUCE_ANGLES):
                    ang = 2.0 * math.pi * a / _OPT_REDUCE_ANGLES
                    candidates.append((bx + math.cos(ang) * rr, by + math.sin(ang) * rr))
            for qx, qy in candidates:
                xs[v], ys[v] = qx, qy
                c = incident_crossings(v)
                if c < best:  # strict improvement only -> monotone, deterministic
                    best, best_x, best_y = c, qx, qy
            xs[v], ys[v] = best_x, best_y
            if best < base:
                improved = True
        passes += 1
        if not improved:
            break

    # Restore the overlap-free guarantee after relocation. Stop as soon as the layout is strictly
    # overlap-free (`_separate_overlaps` keeps a small margin, so it can report movement while the
    # drawing already has no real overlap); the cap bounds a genuinely tight spot that only cycles.
    cap = passes + _OPT_REDUCE_PROJECT_CAP
    while passes < max_passes and passes < cap:
        moved = _separate_overlaps(xs, ys, epairs, radii, n)
        passes += 1
        if not moved or not _has_overlap(xs, ys, epairs, radii, n):
            break
    return passes


def _has_overlap(xs: list, ys: list, epairs: list, radii: list, n: int) -> bool:
    """Whether any node-node or edge-over-node overlap remains (strict; matches _count_overlaps)."""
    for i in range(n):
        for j in range(i + 1, n):
            if math.hypot(xs[i] - xs[j], ys[i] - ys[j]) < radii[i] + radii[j] - 1e-6:
                return True
    for a, b in epairs:
        ax, ay, bx, by = xs[a], ys[a], xs[b], ys[b]
        for kidx in range(n):
            if kidx == a or kidx == b:
                continue
            d, _, _ = _seg_point_dist(xs[kidx], ys[kidx], ax, ay, bx, by)
            if d < radii[kidx] - 1e-6:
                return True
    return False


def _optimized_view_data(graph: Graph, max_passes: int) -> dict:
    """Build the overlap-free page payload: nodes with optimised x/y, edges, empty clusters.

    Reuses :func:`_view_data`'s per-node fields and adds ``x``/``y`` from :func:`_optimize_layout`.
    ``clusters`` is empty (this layout has no VPC-centered rings); the shared draw-only template
    handles that by simply drawing no cluster labels and fitting the view to the node bounds.
    """
    base = _view_data(graph)
    _optimize_layout(base["nodes"], base["edges"], max_passes)
    base["clusters"] = []
    return base


def build_optimized_html(graph: Graph, max_passes: int) -> str:
    """Return the complete, self-contained overlap-free HTML document for ``graph`` as a string.

    ``max_passes`` bounds the optimisation passes (see the overlap-elimination section header).
    """
    data_json = json.dumps(_optimized_view_data(graph, max_passes), ensure_ascii=False, default=str)
    return (
        _render_static_layout(data_json, "overlap-free", _OPTIMIZED_HINT)
        .replace("__NODE_COUNT__", str(len(graph.nodes)))
        .replace("__EDGE_COUNT__", str(len(graph.edges)))
    )


def write_optimized_html(
    graph: Graph,
    path: str | Path,
    *,
    max_nodes: int | None = None,
    max_bytes: int | None = None,
    max_passes: int = 0,
) -> Path | None:
    """Write ``graph`` to ``path`` as the self-contained **overlap-free** HTML page.

    Same contract and size guard as :func:`write_html`: returns the :class:`~pathlib.Path`
    written, or ``None`` when the graph is too large (more than ``max_nodes`` nodes, or a rendered
    page over ``max_bytes`` bytes), in which case **no file is written** so the caller can fall
    back to the ``.dot`` output. ``max_passes`` bounds the optimisation passes.
    """
    node_cap = MAX_NODES if max_nodes is None else max_nodes
    byte_cap = MAX_HTML_BYTES if max_bytes is None else max_bytes

    if len(graph.nodes) > node_cap:
        return None

    html = build_optimized_html(graph, max_passes)
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


# The one-line hint shown in each Python-computed layout's HUD (injected as __HINT__).
_RINGED_HINT = (
    "rings from each center: VPC · subnets · ENIs · everything else · "
    "drag a node · scroll to zoom (or lock it and use + / −) · drag to pan"
)
_OPTIMIZED_HINT = (
    "overlap-free layout: no node covers another and no edge crosses a node · "
    "drag a node · scroll to zoom (or lock it and use + / −) · drag to pan"
)


def _render_static_layout(data_json: str, variant: str, hint: str) -> str:
    """Fill the shared draw-only template with a graph payload, variant name, and hint line.

    Injects via ``str.replace`` (not ``%``/``format``) so the template's CSS/JS braces need no
    escaping. ``__NODE_COUNT__``/``__EDGE_COUNT__`` are left for the caller to fill.
    """
    return (
        _STATIC_TEMPLATE.replace("__GRAPH_DATA__", data_json)
        .replace("__VARIANT__", variant)
        .replace("__HINT__", hint)
    )


# --------------------------------------------------------------------------- #
# Shared draw-only template for the Python-computed layouts (ringed and overlap-free).
# Positions are precomputed in Python (GRAPH.nodes have x/y; GRAPH.clusters, when present, carry
# each cluster's center/label), so there is no force simulation — the JS only draws and handles
# pan/zoom/drag. The variant name (page title + HUD badge) and the hint line are injected via
# __VARIANT__ / __HINT__ by :func:`_render_static_layout`. Self-contained: inline CSS + JS.
# --------------------------------------------------------------------------- #
_STATIC_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CloudBreachGraph — __VARIANT__</title>
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
  <h1>CloudBreachGraph <span class="muted">· __VARIANT__</span></h1>
  <div class="muted"><span id="ncount">__NODE_COUNT__</span> nodes ·
    <span id="ecount">__EDGE_COUNT__</span> edges</div>
  <div id="legend"></div>
  <div id="controls">
    <button id="zoomIn" class="zoom" title="Zoom in">+</button>
    <button id="zoomOut" class="zoom" title="Zoom out">−</button>
    <label title="When on, the mouse wheel no longer zooms — use the + / − buttons instead.">
      <input type="checkbox" id="noscroll"> lock scroll-zoom</label>
  </div>
  <div id="hint">__HINT__</div>
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

  // Cluster labels (the VPC name above each cluster), drawn first so nodes/edges sit on top.
  // The rings themselves are conveyed purely by node position — no guide circles are drawn.
  ctx.fillStyle = dark ? "#9aa0a6" : "#607d8b";
  ctx.textAlign = "center";
  ctx.font = "12px Helvetica, Arial, sans-serif";
  if (scale > 0.4) {
    for (const c of clusters) {
      const p = toScreen({ x: c.cx, y: c.cy });
      const top = Math.max(0, ...c.rings, 40) * scale;
      ctx.fillText(c.label, p.x, p.y - top - 6);
    }
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
