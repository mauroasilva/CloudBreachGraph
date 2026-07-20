"""JSON export — serialize a :class:`~cloudbreachgraph.model.graph.Graph` to disk.

``write_json(graph, path)`` renders ``Graph.to_dict()`` (the Phase 2 → Phase 3 contract,
``docs/02_architecture.md §7``) as pretty, **deterministic** JSON.

Determinism is a hard requirement (``docs/04_conventions.md``): nodes/edges are already
sorted by :class:`Graph`, and this writer adds **no** wall-clock timestamp so byte-for-byte
output is stable across runs and diffable. (Phase 2's learnings explicitly keep
``generated_at`` out of ``to_dict`` for the same reason; we honor that here rather than
stamp one at write time.)
"""

from __future__ import annotations

import json
from pathlib import Path

from ..model.graph import Graph


def write_json(graph: Graph, path: str | Path) -> Path:
    """Write ``graph.to_dict()`` to ``path`` as pretty, deterministic JSON.

    Parent directories are created as needed. Returns the :class:`~pathlib.Path` written.
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(graph.to_dict(), indent=2, ensure_ascii=False, default=str)
    out.write_text(text + "\n", encoding="utf-8")
    return out
