## CHANGE REQUEST (bug fix)

**What's broken:**
`cloudbreachgraph-to-html --split-by-vpc` drops any node that reaches multiple VPCs from every
VPC file but one. A single graph node can legitimately relate to several VPCs — a CIDR block, an
`internet` source, a `flow_peer`, OR a security group that is referenced (across a VPC peering)
by rules in other VPCs. Each is one node with several outgoing `can_reach` / `connects_to`
edges. The split assigns it to exactly ONE VPC, so the other VPCs' HTML files are missing the
node AND its edge into them — hiding real exposure, which defeats the tool's purpose.

**Do NOT fix this by node type.** The first instinct is "duplicate CIDR nodes" or "duplicate the
`_REACH_TYPES` set (internet/cidr) plus flow_peer." That is wrong: it leaves cross-VPC
security-group references (a `security_group` node used as a `can_reach` SOURCE) broken. The fix
must follow EDGE SEMANTICS, not a hardcoded list of types.

**Root cause (already located):**
`src/cloudbreachgraph/output/html_export.py`:
- `split_by_vpc(graph)` assigns every node to a SINGLE VPC via `_vpc_group_of(graph)` and keeps
  an edge only when BOTH endpoints resolve to that same VPC.
- `_vpc_group_of` intentionally returns one VPC per node ("a shared source ... is grouped with
  the first"). That is CORRECT for the single-page ringed / hierarchical / overlap-free layouts
  (each node is drawn once) and MUST stay that way. It is wrong only for splitting.

**Reproductions (both fail on current `main`):**
```python
from cloudbreachgraph.model.graph import Graph, Node, Edge
from cloudbreachgraph.output import html_export as h

def base_two_vpcs():
    g = Graph(meta={})
    for v in ("vpc-a", "vpc-b"):
        g.add_node(Node(v, "vpc", v))
        g.add_node(Node(f"subnet-{v}", "subnet", f"subnet-{v}"))
        g.add_node(Node(f"eni-{v}", "eni", f"eni-{v}"))
        g.add_edge(Edge(f"subnet-{v}", v, "in_vpc"))
        g.add_edge(Edge(f"eni-{v}", f"subnet-{v}", "in_subnet"))
    return g

# (1) Shared CIDR reaching an ENI in both VPCs.
g = base_two_vpcs()
g.add_node(Node("cidr:203.0.113.0/24", "cidr", "203.0.113.0/24"))
g.add_edge(Edge("cidr:203.0.113.0/24", "eni-vpc-a", "can_reach"))
g.add_edge(Edge("cidr:203.0.113.0/24", "eni-vpc-b", "can_reach"))
# BUG: vpc-b's sub-graph has no CIDR node and no can_reach edge.

# (2) A security group used as a source (peer-SG reference across peering) reaching both VPCs.
g = base_two_vpcs()
g.add_node(Node("sg-a", "security_group", "sg-a", {"vpc_id": "vpc-a"}))
g.add_node(Node("sg-b", "security_group", "sg-b", {"vpc_id": "vpc-b"}))
g.add_edge(Edge("eni-vpc-a", "sg-a", "secured_by"))
g.add_edge(Edge("eni-vpc-b", "sg-b", "secured_by"))
g.add_node(Node("sg-shared", "security_group", "sg-shared", {"vpc_id": "vpc-a"}))
g.add_edge(Edge("sg-shared", "sg-a", "can_reach", {"ports": "tcp/443"}))
g.add_edge(Edge("sg-shared", "sg-b", "can_reach", {"ports": "tcp/443"}))
# BUG: vpc-b's sub-graph has no sg-shared node and no can_reach edge into sg-b.
