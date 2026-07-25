"""Flow-log analysis: IP history, flow-log destinations, and observed connections (§5.7).

This is the mapping half of the ``flow_logs`` role (the collectors are in
``aws/collectors.py``). Given the collected flow-log *configuration*, the per-ENI IP-allocation
events, and the parsed flow-log *records*, :func:`map_flow_logs` folds three things into the
already-built graph:

1. **IP history** — each ENI node gains an ``ip_allocations`` attribute: *when* each of its private
   IPs was allocated (from CloudTrail ``CreateNetworkInterface`` events).
2. **Flow-log configuration** — a ``flow_log`` node per configured flow log, a ``log_group`` /
   ``log_bucket`` node for its destination, and ``logs_to`` (resource → flow_log) / ``delivers_to``
   (flow_log → destination) edges, so the map shows *where each VPC stores its logs*.
3. **Observed connections** — for every flow record captured on a collected ENI, from the moment its
   IP was allocated onward (clamped to at most 60 days, ``collectors.FLOW_LOG_MAX_LOOKBACK_DAYS``),
   the *peer* end of the flow becomes a node and a directed ``connects_to`` edge. When the peer IP
   belongs to **another collected ENI**, the edge runs **ENI → ENI** directly; otherwise the peer is
   a ``flow_peer`` node (an external/unmapped address).

The transform is deterministic (sorted iteration, aggregated port labels) and read-only — it only
reshapes an in-memory :class:`~cloudbreachgraph.model.graph.Graph`.
"""

from __future__ import annotations

from datetime import datetime

from ..model.graph import Edge, Graph, Node
from ..model.resources import Eni, FlowLog, FlowLogRecord, IpAllocation

# Protocol numbers we bother to name in a port label; anything else keeps its number.
_PROTO_NAMES = {"1": "icmp", "6": "tcp", "17": "udp", "58": "icmpv6"}


def _port_label(protocol: str | None, port: int | None) -> str:
    """A short ``tcp/443``-style label for a flow's protocol/port (mirrors reachability ports)."""
    if protocol in (None, "", "-", "-1"):
        proto = "all"
    else:
        proto = _PROTO_NAMES.get(str(protocol), str(protocol))
    if port is None:
        return proto
    return f"{proto}/{port}"


def _epoch(iso: str | None) -> int | None:
    """Parse an ISO-8601 timestamp to epoch seconds (``None`` if absent/unparseable)."""
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except ValueError:
        return None


def map_flow_logs(
    graph: Graph,
    enis: list[Eni],
    flow_logs: list[FlowLog],
    allocations: list[IpAllocation],
    records: list[FlowLogRecord],
) -> None:
    """Fold IP history, flow-log config, and observed connections into ``graph`` (§5.7)."""
    ip_to_eni: dict[str, str] = {}
    eni_ips: dict[str, set[str]] = {}
    for eni in enis:
        if not eni.id:
            continue
        ips = {ip for ip in eni.private_ips if ip}
        eni_ips[eni.id] = ips
        for ip in ips:
            ip_to_eni.setdefault(ip, eni.id)

    alloc_start = _map_ip_history(graph, allocations)
    _map_flow_log_config(graph, flow_logs)
    _map_connections(graph, records, ip_to_eni, eni_ips, alloc_start)


def _map_ip_history(graph: Graph, allocations: list[IpAllocation]) -> dict[str, int]:
    """Attach ``ip_allocations`` to each ENI node; return the earliest alloc epoch per ENI.

    The earliest epoch bounds how far back that ENI's flow records are analysed — traffic seen
    before its IP was allocated is a *different* interface reusing the address and is dropped.
    """
    by_eni: dict[str, list[IpAllocation]] = {}
    for alloc in allocations:
        if alloc.eni_id:
            by_eni.setdefault(alloc.eni_id, []).append(alloc)

    earliest: dict[str, int] = {}
    for eni_id, allocs in by_eni.items():
        node = graph.get_node(eni_id)
        if node is None or node.type != "eni":
            continue
        entries = sorted(
            ({"ip": a.private_ip, "allocated_at": a.allocated_at} for a in allocs),
            key=lambda e: (e["allocated_at"] or "", e["ip"] or ""),
        )
        node.attributes["ip_allocations"] = entries
        epochs = [e for e in (_epoch(a.allocated_at) for a in allocs) if e is not None]
        if epochs:
            earliest[eni_id] = min(epochs)
    return earliest


def _map_flow_log_config(graph: Graph, flow_logs: list[FlowLog]) -> None:
    """Add ``flow_log`` + destination (``log_group``/``log_bucket``) nodes and their edges."""
    for fl in sorted(flow_logs, key=lambda f: f.id or ""):
        if not fl.id:
            continue
        graph.add_node(
            Node(
                id=fl.id,
                type="flow_log",
                label=fl.id,
                attributes={
                    "resource_id": fl.resource_id,
                    "destination_type": fl.destination_type,
                    "traffic_type": fl.traffic_type,
                    "status": fl.status,
                },
            )
        )
        # resource -> flow_log, only when the logged resource is already in the graph so no edge
        # dangles (a flow log can target a VPC/subnet/ENI that no ENI referenced).
        if fl.resource_id and graph.get_node(fl.resource_id) is not None:
            graph.add_edge(Edge(source=fl.resource_id, target=fl.id, relationship="logs_to"))

        dest_id = fl.destination_id
        if dest_id:
            graph.add_node(
                Node(
                    id=dest_id,
                    type=fl.destination_node_type,
                    label=dest_id,
                    attributes={"destination_type": fl.destination_type},
                )
            )
            graph.add_edge(
                Edge(
                    source=fl.id,
                    target=dest_id,
                    relationship="delivers_to",
                    attributes={"destination_type": fl.destination_type},
                )
            )


def _map_connections(
    graph: Graph,
    records: list[FlowLogRecord],
    ip_to_eni: dict[str, str],
    eni_ips: dict[str, set[str]],
    alloc_start: dict[str, int],
) -> None:
    """Turn flow records into ``connects_to`` edges (+ ``flow_peer`` nodes for external peers).

    For each record captured on a collected ENI ``A``, the *peer* end (the address that is not
    ``A``'s) becomes the other node. A peer IP that belongs to another collected ENI ``B`` yields a
    direct **ENI → ENI** edge; otherwise the peer is an external ``flow_peer`` node. Ports are
    aggregated per directed edge so repeated flows collapse to one edge with a merged port label.
    """
    # (source_id, target_id) -> {ports, peer_ip (for a flow_peer node), peer_is_eni}
    agg: dict[tuple[str, str], dict] = {}

    for rec in records:
        home = rec.interface_id
        if not home or home not in eni_ips:
            continue
        ips = eni_ips[home]

        if rec.dstaddr in ips:
            peer_ip, inbound = rec.srcaddr, True
        elif rec.srcaddr in ips:
            peer_ip, inbound = rec.dstaddr, False
        else:
            continue  # record isn't about this ENI's own addresses
        if not peer_ip:
            continue

        # Clamp to the IP-allocation window: drop traffic seen before the ENI's IP was allocated.
        start_bound = alloc_start.get(home)
        if start_bound is not None and rec.start is not None and rec.start < start_bound:
            continue

        peer_eni = ip_to_eni.get(peer_ip)
        if peer_eni == home:
            continue  # a flow between this ENI's own addresses — no peer

        if peer_eni is not None:
            src, tgt = (peer_eni, home) if inbound else (home, peer_eni)
            peer_node_ip = None
        else:
            peer_id = f"flow-peer:{peer_ip}"
            src, tgt = (peer_id, home) if inbound else (home, peer_id)
            peer_node_ip = peer_ip

        entry = agg.setdefault((src, tgt), {"ports": set(), "peer_ip": peer_node_ip})
        entry["ports"].add(_port_label(rec.protocol, rec.dstport))

    for peer_ip in sorted({e["peer_ip"] for e in agg.values() if e["peer_ip"]}):
        graph.add_node(
            Node(
                id=f"flow-peer:{peer_ip}",
                type="flow_peer",
                label=peer_ip,
                attributes={"ip": peer_ip},
            )
        )

    for src, tgt in sorted(agg):
        ports = ", ".join(sorted(agg[(src, tgt)]["ports"]))
        graph.add_edge(
            Edge(
                source=src,
                target=tgt,
                relationship="connects_to",
                attributes={"ports": ports, "via": "flow_log"},
            )
        )
