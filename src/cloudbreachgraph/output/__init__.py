"""Serializers: JSON and Graphviz DOT export (Phase 3).

* :func:`cloudbreachgraph.output.json_export.write_json` — deterministic ``graph.json``.
* :func:`cloudbreachgraph.output.dot_export.write_dot` / ``render`` — Graphviz ``graph.dot``
  and optional rasterization via the system ``dot`` binary.
"""

from .dot_export import dot_available, render, write_dot
from .json_export import write_json

__all__ = ["write_json", "write_dot", "render", "dot_available"]
