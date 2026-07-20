"""Tests for the graph data model (``model/graph.py``): merge, dedup, ordering, to_dict."""

from __future__ import annotations

from cloudbreachgraph.model.graph import Edge, Graph, Node


def test_add_node_merges_attributes_on_duplicate_id():
    g = Graph()
    g.add_node(Node("vpc-1", "vpc", "vpc-1", {"synthetic": True, "unresolved": True}))
    # A later, richer add for the same id upgrades the placeholder label and merges attrs.
    g.add_node(Node("vpc-1", "vpc", "primary-vpc", {"cidr": "10.0.0.0/16"}))

    node = g.get_node("vpc-1")
    assert node.label == "primary-vpc"
    assert node.attributes == {"synthetic": True, "unresolved": True, "cidr": "10.0.0.0/16"}
    assert len(g.nodes) == 1


def test_add_edge_dedupes_on_source_target_relationship():
    g = Graph()
    g.add_edge(Edge("eni-1", "subnet-1", "in_subnet"))
    g.add_edge(Edge("eni-1", "subnet-1", "in_subnet"))
    assert len(g.edges) == 1


def test_nodes_sorted_by_type_then_id():
    g = Graph()
    g.add_node(Node("subnet-2", "subnet", "s2"))
    g.add_node(Node("eni-2", "eni", "e2"))
    g.add_node(Node("eni-1", "eni", "e1"))
    g.add_node(Node("vpc-1", "vpc", "v1"))
    assert [(n.type, n.id) for n in g.nodes] == [
        ("eni", "eni-1"),
        ("eni", "eni-2"),
        ("subnet", "subnet-2"),
        ("vpc", "vpc-1"),
    ]


def test_edges_sorted_by_source_target_relationship():
    g = Graph()
    g.add_edge(Edge("eni-2", "subnet-1", "in_subnet"))
    g.add_edge(Edge("eni-1", "i-1", "attached_to"))
    g.add_edge(Edge("eni-1", "subnet-1", "in_subnet"))
    assert [(e.source, e.target, e.relationship) for e in g.edges] == [
        ("eni-1", "i-1", "attached_to"),
        ("eni-1", "subnet-1", "in_subnet"),
        ("eni-2", "subnet-1", "in_subnet"),
    ]


def test_to_dict_shape_is_the_phase3_contract():
    g = Graph(meta={"region": "us-east-1"})
    g.add_node(Node("eni-1", "eni", "eni-1", {"interface_type": "interface"}))
    g.add_edge(Edge("eni-1", "subnet-1", "in_subnet", {"k": "v"}))

    d = g.to_dict()
    assert set(d) == {"meta", "nodes", "edges"}
    assert d["meta"] == {"region": "us-east-1"}
    assert d["nodes"] == [
        {
            "id": "eni-1",
            "type": "eni",
            "label": "eni-1",
            "attributes": {"interface_type": "interface"},
        }
    ]
    assert d["edges"] == [
        {
            "source": "eni-1",
            "target": "subnet-1",
            "relationship": "in_subnet",
            "attributes": {"k": "v"},
        }
    ]
