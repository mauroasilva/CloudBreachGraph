"""``cloudbreachgraph-to-html`` — convert an existing graph file to the interactive HTML.

An auxiliary CLI (separate console entry point from the main ``cloudbreachgraph``) that
takes a graph already written by a previous run — ``graph.json`` (lossless) or ``graph.dot``
(best-effort; this tool's own DOT) — and renders the self-contained, force-directed HTML
view (``output/html_export.py``). Handy when you ran without ``--html`` (or from
``--from-cache``) and now want the interactive page without re-collecting from AWS.

By default it renders the force-directed layout; with ``--ringed`` it instead renders the
concentric-**ringed** layout (each VPC at a cluster center, then rings of subnets, ENIs, and
everything else — ``output/html_export.write_ringed_html``).

With ``--split-by-vpc`` it instead writes **one HTML per VPC** — ``graph-<VPC ID>.html`` in the
output directory (``-o``, default: the input's directory) — each a stand-alone view of that VPC's
nodes and their edges (``output/html_export.split_by_vpc``). The layout / view-transform flags
(``--ringed`` / ``--optimize-passes`` / ``--no-security-groups`` / ``--collapse-asgs``) apply to
every per-VPC file.

Two **view transforms** rewrite the loaded graph before rendering, mirroring the main pipeline:
``--no-security-groups`` collapses the security-group layer (``collapse_security_groups``) and
``--collapse-asgs`` folds each Auto Scaling group's members into one ``autoscaling_group`` node
(``collapse_autoscaling_groups``). Both can only reshape what is already in the input — they never
re-collect from AWS — so ``--collapse-asgs`` needs a graph the main tool built with ``--flow-logs``
(which records the ASG membership); on a graph with no ASG membership it is a no-op.

It reuses the exact same writer and size guard as the main pipeline: if the graph is too
large to render responsibly in a browser, it warns and **falls back to writing a ``.dot``**
(via ``output/dot_export.py``) that Graphviz can lay out offline — mirroring ``cli.py``.
Purely local file I/O; it never touches AWS.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .graph_io import GraphLoadError, load_graph
from .mapping.collapse import collapse_autoscaling_groups, collapse_security_groups
from .mapping.flowlogs import bound_connects_to_port_labels
from .model.graph import Graph
from .output import dot_export, html_export


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cloudbreachgraph-to-html",
        description="Convert an existing CloudBreachGraph graph.json or graph.dot into the "
        "interactive, self-contained HTML view. Local only — no AWS calls.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("input", metavar="FILE", help="path to a graph.json or graph.dot file")
    p.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        help="output HTML path (default: the input path with a .html suffix)",
    )
    p.add_argument(
        "--format",
        choices=("auto", "json", "dot"),
        default="auto",
        help="input format (default: auto — inferred from the .json/.dot extension)",
    )
    p.add_argument(
        "--security-groups",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="keep security-group nodes as-is (default: on). --no-security-groups collapses the "
        "SG layer, bringing the source IPs forward to connect directly to the ENIs. It can only "
        "remove SG nodes present in the input, not add them (no AWS re-collection)",
    )
    p.add_argument(
        "--collapse-asgs",
        action="store_true",
        help="collapse each Auto Scaling group's instances and ENIs (current + historical) into a "
        "single autoscaling_group node, merging their edges. Uses the ASG membership already in "
        "the input (the asg_name attribute the main tool records with --flow-logs) — it re-points "
        "and merges what's there, never re-collects from AWS",
    )
    p.add_argument(
        "--ringed",
        action="store_true",
        help=f"{html_export.RINGED_HELP} (same size guard / .dot fallback)",
    )
    p.add_argument(
        "--hierarchical",
        action="store_true",
        help=f"{html_export.HIERARCHICAL_HELP} (same size guard / .dot fallback)",
    )
    p.add_argument(
        "--split-by-vpc",
        action="store_true",
        help="write one HTML per VPC (graph-<VPC ID>.html) instead of a single file. Output goes "
        "to the -o directory (default: the input's directory); the layout flags (--ringed / "
        "--hierarchical / --optimize-passes / --no-security-groups) apply to every per-VPC file",
    )
    p.add_argument(
        "--optimize-passes",
        type=int,
        default=0,
        metavar="N",
        help=html_export.OPTIMIZE_PASSES_HELP,
    )
    return p


def _emit(
    graph: Graph,
    out_path: Path,
    *,
    ringed: bool,
    hierarchical: bool,
    optimize_passes: int,
    protect: Path | None,
) -> None:
    """Write one HTML for *graph*, or fall back to a ``.dot`` when it is too large to render.

    Mirrors the main CLI: :func:`~html_export.write_layout_html` returns ``None`` (writing
    nothing) for an over-size graph, in which case we write a Graphviz ``.dot`` beside it instead
    — unless that ``.dot`` would clobber ``protect`` (the original input), in which case we only
    warn. Prints what happened to stdout (success) or stderr (fallback).
    """
    result = html_export.write_layout_html(
        graph, out_path, ringed=ringed, hierarchical=hierarchical, optimize_passes=optimize_passes
    )
    if result is not None:
        print(f"wrote {result}")
        return

    dot_path = out_path.with_suffix(".dot")
    if protect is not None and dot_path.resolve() == protect.resolve():
        print(
            f"cloudbreachgraph-to-html: warning: graph too large for an interactive HTML view "
            f"(> {html_export.MAX_NODES} nodes); skipped {out_path.name} — lay out {protect} "
            f"with Graphviz instead (dot -Tsvg).",
            file=sys.stderr,
        )
        return
    dot_export.write_dot(graph, dot_path)
    print(
        f"cloudbreachgraph-to-html: warning: graph too large for an interactive HTML view "
        f"(> {html_export.MAX_NODES} nodes); wrote {dot_path} instead — lay it out with "
        f"Graphviz (dot -Tsvg {dot_path}).",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    in_path = Path(args.input)

    if args.optimize_passes < 0:
        print("cloudbreachgraph-to-html: --optimize-passes must be >= 0", file=sys.stderr)
        return 2

    try:
        graph = load_graph(in_path, fmt=args.format)
    except GraphLoadError as exc:
        print(f"cloudbreachgraph-to-html: {exc}", file=sys.stderr)
        return 2

    # Re-bound flow-log connects_to port labels (§5.7): a graph written by an older run can carry an
    # unbounded port list that overflows Graphviz's quoted-string limit on the .dot fallback and
    # clutters the HTML. Idempotent — a graph built by the current tool is already bounded.
    bound_connects_to_port_labels(graph)

    if not args.security_groups:
        # Collapse the SG layer of the loaded graph (a view transform; can only remove SG nodes).
        graph = collapse_security_groups(graph)

    if args.collapse_asgs:
        # Collapse each ASG's members into one node (a view transform; a no-op when the input has no
        # ASG membership — i.e. it wasn't built with --flow-logs / historical ENIs).
        graph = collapse_autoscaling_groups(graph)

    if args.split_by_vpc:
        # One self-contained HTML per VPC: graph-<VPC ID>.html in the output directory.
        subgraphs = html_export.split_by_vpc(graph)
        if not subgraphs:
            print("cloudbreachgraph-to-html: no VPCs found to split", file=sys.stderr)
            return 2
        out_dir = Path(args.output) if args.output else (in_path.parent or Path("."))
        out_dir.mkdir(parents=True, exist_ok=True)
        for vpc_id, sub in subgraphs.items():
            _emit(
                sub,
                out_dir / f"graph-{vpc_id}.html",
                ringed=args.ringed,
                hierarchical=args.hierarchical,
                optimize_passes=args.optimize_passes,
                protect=None,  # per-VPC names never collide with the input file
            )
        return 0

    out_path = Path(args.output) if args.output else in_path.with_suffix(".html")
    _emit(
        graph,
        out_path,
        ringed=args.ringed,
        hierarchical=args.hierarchical,
        optimize_passes=args.optimize_passes,
        protect=in_path,  # don't clobber the input if the fallback .dot would land on it
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
