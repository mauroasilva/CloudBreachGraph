"""Flow-log analysis: IP history, per-VPC flow-log config, and observed connections (§5.7).

This is the mapping half of the ``flow_logs`` role (the collectors are in
``aws/collectors.py``). Given the collected flow-log *configuration*, the per-ENI IP-allocation
events, and the parsed flow-log *records*, :func:`map_flow_logs` folds three things into the
already-built graph:

1. **IP history** — each ENI node gains an ``ip_allocations`` attribute: *when* each of its private
   IPs was allocated (from CloudTrail ``CreateNetworkInterface`` events).
2. **Flow-log configuration** — *not* separate nodes: each flow log's destination (where the logs
   are stored) is recorded as a ``flow_logs`` **attribute on the VPC** that owns the logged resource
   (a VPC-, subnet- or ENI-scoped flow log all attach to their VPC). So the map answers "where does
   this VPC store its logs?" on the VPC itself.
3. **Observed connections** — for every flow record captured on a collected ENI, from the moment its
   IP was allocated onward (clamped to at most 60 days, ``collectors.FLOW_LOG_MAX_LOOKBACK_DAYS``),
   the *peer* end of the flow becomes a directed ``connects_to`` edge. When the peer IP belongs to
   **another collected ENI** — *and that ENI already held the IP at the record's time* — the edge
   runs **ENI → ENI** directly (no new node); otherwise the peer is an external ``flow_peer`` node.
   A record whose peer IP currently belongs to an ENI but was allocated to it *after* the record was
   captured is a **historic-IP reuse** and is dropped, so the map never links a current ENI through
   an address it didn't own at the time.

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
    """Fold IP history, per-VPC flow-log config, and observed connections into ``graph`` (§5.7)."""
    ip_to_eni: dict[str, str] = {}
    eni_ips: dict[str, set[str]] = {}
    for eni in enis:
        if not eni.id:
            continue
        ips = {ip for ip in eni.private_ips if ip}
        eni_ips[eni.id] = ips
        for ip in ips:
            ip_to_eni.setdefault(ip, eni.id)

    # When each *current* ENI IP was allocated (keyed by the IP itself — an IP maps to one ENI).
    # Used to reject a flow whose peer IP belonged to that ENI only *after* the flow was captured.
    ip_alloc_epoch: dict[str, int] = {}
    for alloc in allocations:
        ep = _epoch(alloc.allocated_at)
        if alloc.private_ip and ep is not None:
            # If an IP appears in several events, keep the earliest allocation.
            ip_alloc_epoch[alloc.private_ip] = min(ep, ip_alloc_epoch.get(alloc.private_ip, ep))

    alloc_start = _map_ip_history(graph, allocations)
    _attach_flow_log_config_to_vpcs(graph, flow_logs)
    _map_connections(graph, records, ip_to_eni, eni_ips, alloc_start, ip_alloc_epoch)


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


def _attach_flow_log_config_to_vpcs(graph: Graph, flow_logs: list[FlowLog]) -> None:
    """Record each flow log's destination as a ``flow_logs`` attribute on the owning VPC node.

    A flow log's ``ResourceId`` is a VPC, subnet, or ENI; all three resolve up to a VPC (subnet via
    its ``in_vpc`` edge, ENI via ``in_subnet`` then ``in_vpc``). The config is stored on the VPC
    itself — "where does this VPC store its logs?" — rather than as separate ``flow_log``/
    destination nodes. A flow log whose VPC isn't in the (ENI-anchored) graph is skipped.
    """
    eni_subnet = {e.source: e.target for e in graph.edges if e.relationship == "in_subnet"}
    subnet_vpc = {e.source: e.target for e in graph.edges if e.relationship == "in_vpc"}

    def _vpc_of(resource_id: str | None) -> str | None:
        if not resource_id:
            return None
        if resource_id.startswith("vpc-"):
            return resource_id
        if resource_id.startswith("subnet-"):
            return subnet_vpc.get(resource_id)
        if resource_id.startswith("eni-"):
            return subnet_vpc.get(eni_subnet.get(resource_id, ""))
        return None

    by_vpc: dict[str, list[dict]] = {}
    for fl in flow_logs:
        if not fl.id:
            continue
        vpc_id = _vpc_of(fl.resource_id)
        if vpc_id is None or graph.get_node(vpc_id) is None:
            continue
        by_vpc.setdefault(vpc_id, []).append(
            {
                "flow_log_id": fl.id,
                "resource_id": fl.resource_id,
                "destination_type": fl.destination_type,
                "destination": fl.destination,
                "traffic_type": fl.traffic_type,
                "status": fl.status,
            }
        )

    for vpc_id, entries in by_vpc.items():
        graph.get_node(vpc_id).attributes["flow_logs"] = sorted(
            entries, key=lambda e: e["flow_log_id"] or ""
        )


def _map_connections(
    graph: Graph,
    records: list[FlowLogRecord],
    ip_to_eni: dict[str, str],
    eni_ips: dict[str, set[str]],
    alloc_start: dict[str, int],
    ip_alloc_epoch: dict[str, int],
) -> None:
    """Turn flow records into ``connects_to`` edges (+ ``flow_peer`` nodes for external peers).

    For each record captured on a collected ENI ``A``, the *peer* end (the address that is not
    ``A``'s) becomes the other node. A peer IP that belongs to another collected ENI ``B`` yields a
    direct **ENI → ENI** edge — but only if ``B`` already held that IP when the flow was captured
    (else the record is a historic-IP reuse and is dropped). Otherwise the peer is an external
    ``flow_peer`` node. Ports are aggregated per directed edge so repeated flows collapse to one
    edge with a merged port label.
    """
    # (source_id, target_id) -> {ports, peer_ip (set only for a flow_peer node)}
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

        # Clamp to the home ENI's IP-allocation window: drop traffic seen before its IP existed.
        start_bound = alloc_start.get(home)
        if start_bound is not None and rec.start is not None and rec.start < start_bound:
            continue

        peer_eni = ip_to_eni.get(peer_ip)
        if peer_eni == home:
            continue  # a flow between this ENI's own addresses — no peer

        if peer_eni is not None:
            # Temporal guard: only link to the peer ENI if it already held this IP at record time.
            # A known allocation *after* the flow means the IP was a different interface's then.
            peer_alloc = ip_alloc_epoch.get(peer_ip)
            if peer_alloc is not None and rec.start is not None and rec.start < peer_alloc:
                continue  # historic-IP reuse — don't link the current ENI through a stale address
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
