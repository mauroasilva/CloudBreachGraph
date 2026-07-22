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
nodes and their edges (``output/html_export.split_by_vpc``). The layout flags (``--ringed`` /
``--optimize-passes`` / ``--no-security-groups``) apply to every per-VPC file.

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
from .mapping.collapse import collapse_security_groups
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
        "--ringed",
        action="store_true",
        help=f"{html_export.RINGED_HELP} (same size guard / .dot fallback)",
    )
    p.add_argument(
        "--split-by-vpc",
        action="store_true",
        help="write one HTML per VPC (graph-<VPC ID>.html) instead of a single file. Output goes "
        "to the -o directory (default: the input's directory); the layout flags (--ringed / "
        "--optimize-passes / --no-security-groups) apply to every per-VPC file",
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
    graph: Graph, out_path: Path, *, ringed: bool, optimize_passes: int, protect: Path | None
) -> None:
    """Write one HTML for *graph*, or fall back to a ``.dot`` when it is too large to render.

    Mirrors the main CLI: :func:`~html_export.write_layout_html` returns ``None`` (writing
    nothing) for an over-size graph, in which case we write a Graphviz ``.dot`` beside it instead
    — unless that ``.dot`` would clobber ``protect`` (the original input), in which case we only
    warn. Prints what happened to stdout (success) or stderr (fallback).
    """
    result = html_export.write_layout_html(
        graph, out_path, ringed=ringed, optimize_passes=optimize_passes
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

    if not args.security_groups:
        # Collapse the SG layer of the loaded graph (a view transform; can only remove SG nodes).
        graph = collapse_security_groups(graph)

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
                optimize_passes=args.optimize_passes,
                protect=None,  # per-VPC names never collide with the input file
            )
        return 0

    out_path = Path(args.output) if args.output else in_path.with_suffix(".html")
    _emit(
        graph,
        out_path,
        ringed=args.ringed,
        optimize_passes=args.optimize_passes,
        protect=in_path,  # don't clobber the input if the fallback .dot would land on it
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
