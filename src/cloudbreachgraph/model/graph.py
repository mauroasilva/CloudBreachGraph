"""The graph data model: :class:`Node`, :class:`Edge`, :class:`Graph`.

This is the structure Phase 3 consumes. ``Graph.to_dict()`` is the **interface contract**
(``docs/03_phase_plan.md`` Phase 2 → Phase 3):

    {
      "meta":  {...},
      "nodes": [ {"id","type","label","attributes"}, ... ],   # sorted by (type, id)
      "edges": [ {"source","target","relationship","attributes"}, ... ],  # sorted
    }

Two invariants make diffs and tests stable (``docs/02_architecture.md §6``):

* **Unique node ids** — :meth:`Graph.add_node` merges attributes onto an existing node
  rather than duplicating it.
* **Deterministic ordering** — nodes are sorted by ``(type, id)`` and edges by
  ``(source, target, relationship)`` before export.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Node:
    id: str
    type: str  # "eni" | "ec2_instance" | "load_balancer" | "subnet" | "vpc"
    label: str
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class Edge:
    source: str
    target: str
    relationship: str  # "attached_to" | "in_subnet" | "in_vpc"
    attributes: dict[str, Any] = field(default_factory=dict)


class Graph:
    """A collection of unique nodes and edges with deterministic serialization."""

    def __init__(self, meta: dict[str, Any] | None = None) -> None:
        self.meta: dict[str, Any] = dict(meta or {})
        self._nodes: dict[str, Node] = {}
        self._edges: dict[tuple[str, str, str], Edge] = {}

    # -- mutation ---------------------------------------------------------- #
    def add_node(self, node: Node) -> Node:
        """Add a node, merging attributes if one with the same id already exists.

        On a merge the incoming attributes win on key collisions, but a real node never
        clears an existing ``synthetic``/``unresolved`` flag by accident because the
        builder only ever creates a synthetic placeholder for genuinely-missing resources
        (it checks the collected set first). A non-id ``label`` and a non-empty ``type``
        upgrade a placeholder that previously carried only its id.
        """
        existing = self._nodes.get(node.id)
        if existing is None:
            self._nodes[node.id] = node
            return node

        existing.attributes.update(node.attributes)
        if node.type and existing.type != node.type:
            existing.type = node.type
        if node.label and node.label != node.id and existing.label in ("", existing.id):
            existing.label = node.label
        return existing

    def add_edge(self, edge: Edge) -> Edge:
        """Add an edge, de-duplicating on ``(source, target, relationship)`` (first wins)."""
        key = (edge.source, edge.target, edge.relationship)
        existing = self._edges.get(key)
        if existing is None:
            self._edges[key] = edge
            return edge
        return existing

    # -- access ------------------------------------------------------------ #
    @property
    def nodes(self) -> list[Node]:
        """Nodes sorted deterministically by ``(type, id)``."""
        return sorted(self._nodes.values(), key=lambda n: (n.type, n.id))

    @property
    def edges(self) -> list[Edge]:
        """Edges sorted deterministically by ``(source, target, relationship)``."""
        return sorted(self._edges.values(), key=lambda e: (e.source, e.target, e.relationship))

    def get_node(self, node_id: str) -> Node | None:
        return self._nodes.get(node_id)

    # -- serialization ----------------------------------------------------- #
    def to_dict(self) -> dict[str, Any]:
        """Return the plain, JSON-serializable structure Phase 3 renders."""
        return {
            "meta": self.meta,
            "nodes": [
                {
                    "id": n.id,
                    "type": n.type,
                    "label": n.label,
                    "attributes": n.attributes,
                }
                for n in self.nodes
            ],
            "edges": [
                {
                    "source": e.source,
                    "target": e.target,
                    "relationship": e.relationship,
                    "attributes": e.attributes,
                }
                for e in self.edges
            ],
        }
