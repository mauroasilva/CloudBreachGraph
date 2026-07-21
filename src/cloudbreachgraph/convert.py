"""``cloudbreachgraph-to-html`` — convert an existing graph file to the interactive HTML.

An auxiliary CLI (separate console entry point from the main ``cloudbreachgraph``) that
takes a graph already written by a previous run — ``graph.json`` (lossless) or ``graph.dot``
(best-effort; this tool's own DOT) — and renders the self-contained, force-directed HTML
view (``output/html_export.py``). Handy when you ran without ``--html`` (or from
``--from-cache``) and now want the interactive page without re-collecting from AWS.

By default it renders the force-directed layout; with ``--ringed`` it instead renders the
concentric-**ringed** layout (each VPC at a cluster center, then rings of subnets, ENIs, and
everything else — ``output/html_export.write_ringed_html``).

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
        "--ringed",
        action="store_true",
        help="render the ringed layout instead of the force-directed one: each VPC is the "
        "center of a cluster, ringed by its subnets, then its ENIs, then everything else "
        "(same size guard / .dot fallback)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    in_path = Path(args.input)
    out_path = Path(args.output) if args.output else in_path.with_suffix(".html")

    try:
        graph = load_graph(in_path, fmt=args.format)
    except GraphLoadError as exc:
        print(f"cloudbreachgraph-to-html: {exc}", file=sys.stderr)
        return 2

    writer = html_export.write_ringed_html if args.ringed else html_export.write_html
    result = writer(graph, out_path)
    if result is not None:
        print(f"wrote {result}")
        return 0

    # Too large for an interactive browser layout: fall back to a Graphviz .dot, exactly as
    # the main CLI does. Don't clobber the input if it *is* that .dot already.
    dot_path = out_path.with_suffix(".dot")
    if dot_path.resolve() == in_path.resolve():
        print(
            f"cloudbreachgraph-to-html: warning: graph too large for an interactive HTML view "
            f"(> {html_export.MAX_NODES} nodes); skipped {out_path.name} — lay out {in_path} "
            f"with Graphviz instead (dot -Tsvg).",
            file=sys.stderr,
        )
        return 0
    dot_export.write_dot(graph, dot_path)
    print(
        f"cloudbreachgraph-to-html: warning: graph too large for an interactive HTML view "
        f"(> {html_export.MAX_NODES} nodes); wrote {dot_path} instead — lay it out with "
        f"Graphviz (dot -Tsvg {dot_path}).",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
