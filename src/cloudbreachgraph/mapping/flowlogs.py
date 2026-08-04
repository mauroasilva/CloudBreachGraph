"""Flow-log analysis: IP history, per-VPC flow-log config, and observed connections (§5.7).

This is the mapping half of the ``flow_logs`` role (the collectors are in
``aws/collectors.py``). Given the collected flow-log *configuration*, the per-ENI IP-allocation
events, the **historical ENIs** reconstructed from CloudTrail, and the parsed flow-log *records*,
:func:`map_flow_logs` folds three things into the already-built graph:

1. **IP history** — each *current* ENI node gains an ``ip_history`` attribute: ``{ip: {"start",
   "end"}}`` for every address it has held (from CloudTrail ``CreateNetworkInterface`` events).
   JSON-only; the DOT/HTML views show only the ENI's *current* private IPs.
2. **Flow-log configuration** — *not* separate nodes: each flow log's destination (where the logs
   are stored) is recorded as a ``flow_logs`` **attribute on the VPC** that owns the logged resource
   (a VPC-, subnet- or ENI-scoped flow log all attach to their VPC).
3. **Observed connections** — for every flow record captured on an ENI (current **or** reconstructed
   historical), the *peer* end of the flow becomes a directed ``connects_to`` edge. IP→ENI
   resolution is **time-aware**: the peer IP resolves to whichever ENI held it **at the record's
   timestamp** (a :class:`_Inventory` time-indexed resolver over the combined current ∪ historical
   inventory), so a reused ASG IP is attributed to the ENI that actually owned it then — an
   **ENI → ENI** edge (no ``flow_peer``) when the peer was an ENI in the window. Only when *no* ENI
   held the IP at that time does the peer become an external ``flow_peer`` node — unless the IP is
   known to be internal (some inventory ENI held it at another time), in which case the record is
   dropped rather than inventing a spurious external peer.

The transform is deterministic (sorted iteration, aggregated port labels) and read-only — it only
reshapes an in-memory :class:`~cloudbreachgraph.model.graph.Graph`.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from ..model.graph import Edge, Graph, Node
from ..model.resources import Eni, FlowLog, FlowLogRecord, HistoricalEni, IpAllocation, Vpc

# Protocol numbers we bother to name in a port label; anything else keeps its number.
_PROTO_NAMES = {"1": "icmp", "6": "tcp", "17": "udp", "58": "icmpv6"}

# How many distinct ports a single ``connects_to`` edge lists before it collapses to a range. A busy
# pair (e.g. a peer that fans out over thousands of ephemeral ports) would otherwise produce an
# unreadable label — and one that overflows Graphviz's 16 KB quoted-string limit, so `dot` can't
# render the .dot at all. Above this count the ports are aggregated to a ``<proto>/<min>-<max>``
# range per protocol.
_MAX_PORTS_IN_LABEL = 10


def _port_label(protocol: str | None, port: int | None) -> str:
    """A short ``tcp/443``-style label for a flow's protocol/port (mirrors reachability ports)."""
    if protocol in (None, "", "-", "-1"):
        proto = "all"
    else:
        proto = _PROTO_NAMES.get(str(protocol), str(protocol))
    if port is None:
        return proto
    return f"{proto}/{port}"


def _ports_label(ports: set[str]) -> str:
    """A bounded, deterministic ``ports`` label for a ``connects_to`` edge (§5.7).

    Ten or fewer distinct ports are listed (sorted). More than :data:`_MAX_PORTS_IN_LABEL` — a busy
    pair fanning out over many ephemeral ports — collapse to a ``<proto>/<min>-<max>`` range **per
    protocol**, so the label stays short and never overflows Graphviz's quoted-string limit (an
    unbounded list broke ``dot`` on real captures). Protocol is kept because ``tcp/443`` and
    ``udp/443`` differ materially in a reachability graph. Port-less labels (``all``, ``icmp``) pass
    through as-is."""
    ordered = sorted(ports)
    if len(ordered) <= _MAX_PORTS_IN_LABEL:
        return ", ".join(ordered)
    by_proto: dict[str, list[int]] = {}
    portless: set[str] = set()
    for label in ordered:
        proto, sep, port = label.partition("/")
        if sep and port.isdigit():
            by_proto.setdefault(proto, []).append(int(port))
        else:
            portless.add(label)  # "all", "icmp", a bare protocol — no numeric port to range
    parts: list[str] = []
    for proto in sorted(by_proto):
        nums = by_proto[proto]
        lo, hi = min(nums), max(nums)
        parts.append(f"{proto}/{lo}-{hi}" if lo != hi else f"{proto}/{lo}")
    parts.extend(sorted(portless))
    return ", ".join(parts)


def bound_connects_to_port_labels(graph: Graph) -> None:
    """Re-aggregate the ``ports`` label on every ``connects_to`` edge of ``graph`` (§5.7).

    The build path already bounds these labels (:func:`_ports_label` in :func:`_map_connections`),
    but ``cloudbreachgraph-to-html`` loads a graph a *previous* run wrote — possibly before that
    bound existed — whose ``connects_to`` edges can still carry an unbounded, comma-joined port
    list that overflows Graphviz's quoted-string limit and clutters the HTML. This splits each
    stored label back into port tokens and re-applies :func:`_ports_label`, so the range aggregation
    reaches the convert tool too. Idempotent: an already-ranged (or ≤10-port) label is unchanged."""
    for edge in graph.edges:
        if edge.relationship != "connects_to":
            continue
        ports = edge.attributes.get("ports")
        if not ports:
            continue
        tokens = {tok.strip() for tok in str(ports).split(",") if tok.strip()}
        edge.attributes["ports"] = _ports_label(tokens)


def _epoch(iso: str | None) -> int | None:
    """Parse an ISO-8601 timestamp to epoch seconds (``None`` if absent/unparseable)."""
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Combined ENI inventory + time-indexed IP→ENI resolver (§5.7 Part 3)
# --------------------------------------------------------------------------- #
@dataclass
class _Entry:
    """One ENI in the combined inventory, with the lifetime bounds resolution keys on.

    ``created``/``deleted`` are epoch seconds (either possibly ``None`` — unknown-created means
    "predates the window / held throughout"; unknown-deleted means "still alive"). ``ips`` is every
    private IP the ENI is known to have held (current ∪ reconstructed)."""

    eni_id: str
    ips: set[str] = field(default_factory=set)
    created: int | None = None
    deleted: int | None = None
    historical: bool = False


class _Inventory:
    """The combined current ∪ historical ENI inventory + a time-indexed IP→ENI resolver.

    Subsumes the old ``ip_to_eni`` dict + ``ip_alloc_epoch`` temporal guard: :meth:`resolve`
    answers "which ENI held this IP at time ``t``", disambiguating a reused ASG IP by lifetime.
    """

    def __init__(self) -> None:
        self.entries: dict[str, _Entry] = {}
        self._ip_index: dict[str, list[str]] = {}

    def _entry(self, eni_id: str) -> _Entry:
        entry = self.entries.get(eni_id)
        if entry is None:
            entry = _Entry(eni_id)
            self.entries[eni_id] = entry
        return entry

    def add_current(self, eni_id: str, ips: set[str], created: int | None) -> None:
        entry = self._entry(eni_id)
        entry.ips |= ips
        entry.created = _min_epoch(entry.created, created)
        entry.deleted = None  # a current ENI exists now — never terminated
        entry.historical = entry.historical and False

    def add_historical(
        self, eni_id: str, ips: set[str], created: int | None, deleted: int | None
    ) -> None:
        existing = self.entries.get(eni_id)
        if existing is not None:
            # Also a current ENI (same id): enrich its IPs/created, keep it alive (deleted stays).
            existing.ips |= ips
            existing.created = _min_epoch(existing.created, created)
            return
        self.entries[eni_id] = _Entry(eni_id, set(ips), created, deleted, historical=True)

    def index(self) -> None:
        """Build the IP → [eni ids] index (sorted, so resolution ties break deterministically)."""
        self._ip_index = {}
        for entry in self.entries.values():
            for ip in entry.ips:
                self._ip_index.setdefault(ip, []).append(entry.eni_id)
        for ids in self._ip_index.values():
            ids.sort()

    def ever_internal(self, ip: str) -> bool:
        """Whether *any* inventory ENI (current or historical) ever held ``ip``."""
        return ip in self._ip_index

    @staticmethod
    def alive_at(entry: _Entry, t: int | None) -> bool:
        """Whether ``entry``'s ``[created, deleted]`` lifetime contains time ``t``.

        Unknown ``t`` (a record with no ``start``) can't be clamped, so it always passes."""
        if t is None:
            return True
        if entry.created is not None and t < entry.created:
            return False
        if entry.deleted is not None and t > entry.deleted:
            return False
        return True

    def resolve(self, ip: str, t: int | None) -> str | None:
        """The ENI that held ``ip`` at time ``t`` — the time-aware IP→ENI resolution.

        Among the ENIs that ever held ``ip``, keep those whose lifetime covers ``t``; tie-break to
        the **latest ``created`` ≤ ``t``** (an unknown ``created`` sorts earliest). ``None`` when no
        ENI held ``ip`` at ``t`` (so the caller falls back to a ``flow_peer`` — or drops the record
        if the IP is otherwise internal)."""
        candidates = [
            self.entries[eid]
            for eid in self._ip_index.get(ip, [])
            if ip in self.entries[eid].ips and self.alive_at(self.entries[eid], t)
        ]
        if not candidates and t is None:
            return None
        if not candidates:
            return None
        # Prefer a still-alive (current) ENI when the time is unknown; else latest created ≤ t.
        if t is None:
            current = [e for e in candidates if e.deleted is None]
            candidates = current or candidates
        candidates.sort(key=lambda e: (e.created if e.created is not None else -1, e.eni_id))
        return candidates[-1].eni_id


def _min_epoch(a: int | None, b: int | None) -> int | None:
    vals = [x for x in (a, b) if x is not None]
    return min(vals) if vals else None


def map_flow_logs(
    graph: Graph,
    enis: list[Eni],
    flow_logs: list[FlowLog],
    allocations: list[IpAllocation],
    records: Iterable[dict],
    historical: list[HistoricalEni] | None = None,
    vpcs: list[Vpc] | None = None,
) -> None:
    """Fold IP history, per-VPC flow-log config, and observed connections into ``graph`` (§5.7).

    ``records`` is a **re-iterable** of raw record dicts (a ``list`` or a disk-backed
    :class:`~cloudbreachgraph.aws.collectors.FlowLogRecordStream`) — it is iterated **twice** (once
    to surface unrecognised ENIs, once to map connections), converting each dict to a
    :class:`FlowLogRecord` lazily, so the full record set is never held in memory. An account with
    millions of flow records maps in bounded RAM (only the per-ENI inference counts + per-edge port
    aggregates are kept); the records themselves stream from disk (§5.7 bounded-memory fetch).

    ``historical`` are the CloudTrail-reconstructed ENIs; the caller (``mapping/builder.py``) is
    responsible for having already added their **nodes** (flagged ``historical``/terminated), so
    here they only feed the time-indexed resolver and the connection edges.

    ``vpcs`` supply the VPC CIDRs used to infer an **unrecognised ENI**'s own IP: any flow-record
    ``interface-id`` that is in neither the current nor the historical inventory is surfaced as an
    ``eni`` node flagged ``unrecognised``/``needs_review`` — never silently dropped — with any
    guessed IP recorded (separately, flagged) under ``inferred_private_ips`` for the user to audit.
    """
    historical = historical or []

    alloc_start = _map_ip_history(graph, enis, allocations)
    _attach_flow_log_config_to_vpcs(graph, flow_logs)

    inventory = _build_inventory(enis, alloc_start, historical)
    # Surface every unrecognised ENI (a flow-record home id we don't know) before resolving
    # connections, so its own flows map and its inferred IP can form ENI↔ENI edges with peers.
    _add_unrecognised_enis(graph, records, inventory, _vpc_networks(vpcs or []))
    inventory.index()  # re-index: the unrecognised entries added new IP→ENI mappings
    _map_connections(graph, records, inventory)


def _build_inventory(
    enis: list[Eni], alloc_start: dict[str, int], historical: list[HistoricalEni]
) -> _Inventory:
    """Combine current ENIs (alive; created from CloudTrail) and historical ENIs (w/ lifetimes)."""
    inv = _Inventory()
    for eni in enis:
        if not eni.id:
            continue
        inv.add_current(eni.id, {ip for ip in eni.private_ips if ip}, alloc_start.get(eni.id))
    for h in historical:
        if not h.id:
            continue
        inv.add_historical(
            h.id,
            {ip for ip in h.private_ips if ip},
            _epoch(h.created_at),
            _epoch(h.deleted_at),
        )
    inv.index()
    return inv


def _map_ip_history(
    graph: Graph, enis: list[Eni], allocations: list[IpAllocation]
) -> dict[str, int]:
    """Attach an ``ip_history`` dict to **every** current ENI node; return the earliest alloc epoch.

    ``ip_history`` maps each IP the ENI has held to ``{"start", "end"}`` ISO timestamps: ``start``
    is when it was allocated (from CloudTrail, ``None`` if unknown), ``end`` is ``None`` while the
    ENI still holds the IP (its *current* addresses) else the allocation time of the IP that
    superseded it. JSON-only. The returned earliest-alloc epoch per ENI is the current ENI's
    ``created`` bound for the combined inventory (traffic before its IP existed is a different
    interface's).
    """
    by_eni: dict[str, list[IpAllocation]] = {}
    for alloc in allocations:
        if alloc.eni_id:
            by_eni.setdefault(alloc.eni_id, []).append(alloc)

    earliest: dict[str, int] = {}
    for eni in enis:
        node = graph.get_node(eni.id) if eni.id else None
        if node is None or node.type != "eni":
            continue
        allocs = by_eni.get(eni.id, [])

        # Earliest known allocation start per IP (an IP could appear in several events).
        start_of: dict[str, str | None] = {}
        for a in allocs:
            if not a.private_ip:
                continue
            prev = start_of.get(a.private_ip)
            if prev is None or (a.allocated_at or "") < prev:
                start_of[a.private_ip] = a.allocated_at

        # Allocation starts in chronological order, to find what superseded a released IP.
        ordered = sorted(
            ((_epoch(s), s) for s in start_of.values() if s), key=lambda t: (t[0] or 0, t[1])
        )
        current = {ip for ip in eni.private_ips if ip}

        def _end_for(start_iso: str | None) -> str | None:
            # The next allocation after this IP's start marks when it stopped being on the ENI.
            se = _epoch(start_iso)
            for ep, iso in ordered:  # noqa: B023 - ordered/se are per-iteration by design
                if se is not None and ep is not None and ep > se:
                    return iso
            return None

        history: dict[str, dict[str, str | None]] = {}
        for ip in sorted(current | set(start_of), key=lambda i: (start_of.get(i) or "", i)):
            start = start_of.get(ip)
            end = None if ip in current else _end_for(start)
            history[ip] = {"start": start, "end": end}
        node.attributes["ip_history"] = history

        epochs = [e for e in (_epoch(s) for s in start_of.values()) if e is not None]
        if epochs:
            earliest[eni.id] = min(epochs)
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


# --------------------------------------------------------------------------- #
# Unrecognised ENIs — surface (never drop) a flow-record home id we don't know (§5.7)
# --------------------------------------------------------------------------- #
def _vpc_networks(vpcs: list[Vpc]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """The parsed CIDR networks of the collected VPCs (skipping any without a usable CIDR)."""
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for vpc in vpcs:
        if vpc.cidr:
            try:
                nets.append(ipaddress.ip_network(vpc.cidr, strict=False))
            except ValueError:
                continue
    return nets


def _in_any_network(addr: str, nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> bool:
    """Whether ``addr`` parses to an IP inside any of ``nets`` (a known VPC CIDR)."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip.version == net.version and ip in net for net in nets)


def _infer_own_ip(
    freq: dict[str, int],
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> dict[str, str] | None:
    """Guess an unrecognised ENI's own private IP from its address-frequency map, flagging *how*.

    Every flow captured on the ENI has the ENI's own address on one side and a peer on the other, so
    the ENI's IP is the address that **recurs** across its records — ``freq`` counts each address's
    occurrences (built incrementally by :func:`_add_unrecognised_enis`, so the records themselves
    aren't held). Preferring an address that also falls inside a **known VPC CIDR**
    (``method = "vpc_cidr"``, high confidence) pins the internal side; with no VPC-internal
    candidate we fall back to the most-recurring address overall (``method = "recurring_side"``,
    low confidence). Returns ``{"ip", "method", "confidence"}`` or ``None`` when there is no
    address at all. Deterministic (ties break to the lexically smallest address)."""
    if not freq:
        return None
    in_vpc = [a for a in freq if _in_any_network(a, nets)]
    if in_vpc:
        own = min(in_vpc, key=lambda a: (-freq[a], a))
        return {"ip": own, "method": "vpc_cidr", "confidence": "high"}
    own = min(freq, key=lambda a: (-freq[a], a))
    return {"ip": own, "method": "recurring_side", "confidence": "low"}


def _add_unrecognised_enis(
    graph: Graph,
    records: Iterable[dict],
    inventory: _Inventory,
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> None:
    """Emit an ``eni`` node for every flow-record home id not in the combined inventory (§5.7).

    ENIs created outside CloudTrail retention (long-lived ASG/instance interfaces) can't be
    reconstructed, so their flow records used to be silently dropped. Instead, each such
    ``interface-id`` becomes an ``eni`` node flagged ``unrecognised: true`` / ``origin: "flow_log"``
    / ``needs_review: true``; any inferred IP goes under ``inferred_private_ips`` (with ``method`` +
    ``confidence``) — **never** in ``private_ips`` (kept empty; confirmed addresses only). The
    inferred IP is added to the resolver's inventory (unbounded lifetime) so the ENI's own flows map
    and a peer matching that IP forms an ENI↔ENI edge. Deterministic: processed in sorted id order.

    Streams ``records`` once, keeping only a compact per-unknown-ENI **address-frequency** map
    (never the records themselves) — bounded to O(unknown ENIs × distinct addresses)."""
    freq_by_eni: dict[str, dict[str, int]] = {}
    for raw in records:
        rec = FlowLogRecord.from_collected(raw)
        iface = rec.interface_id
        if not iface or iface in inventory.entries:
            continue  # unknown home ids only — a known ENI already has a (richer) node
        freq = freq_by_eni.setdefault(iface, {})
        for addr in (rec.srcaddr, rec.dstaddr):
            if addr:
                freq[addr] = freq.get(addr, 0) + 1

    for eni_id in sorted(freq_by_eni):
        inferred = _infer_own_ip(freq_by_eni[eni_id], nets)
        inferred_list = [inferred] if inferred is not None else []
        entry = inventory._entry(eni_id)  # created/deleted stay None -> alive at every record time
        if inferred is not None:
            entry.ips.add(inferred["ip"])
        graph.add_node(
            Node(
                id=eni_id,
                type="eni",
                label=eni_id,
                attributes={
                    "unrecognised": True,
                    "origin": "flow_log",
                    "needs_review": True,
                    "private_ips": [],  # confirmed only — a guess never lands here
                    "inferred_private_ips": inferred_list,
                },
            )
        )


def _map_connections(graph: Graph, records: Iterable[dict], inventory: _Inventory) -> None:
    """Turn flow records into ``connects_to`` edges (+ ``flow_peer`` nodes for external peers).

    For each record captured on an ENI ``A`` (current or historical — the *home*, resolved by
    ``interface-id``), the *peer* end (the address that isn't one of ``A``'s) is resolved **at the
    record's time** via :meth:`_Inventory.resolve`:

    * home ``A`` must have been **alive** when the flow was captured (its lifetime covers
      ``rec.start``); else the record belonged to a different interface and is dropped;
    * a peer IP that resolves to an ENI ``B`` (that held it at ``rec.start``) yields a direct
      **ENI → ENI** edge — even when ``B`` is now terminated;
    * otherwise the peer is an external ``flow_peer`` node — unless the IP is otherwise internal
      (held by some inventory ENI at another time), in which case the record is dropped so a reused
      internal address never surfaces as a spurious external peer.

    Ports are aggregated per directed edge so repeated flows collapse to one edge. ``records`` is
    streamed once and each dict converted to a :class:`FlowLogRecord` lazily, so only the bounded
    ``agg`` (one entry per distinct directed edge) is held — not the records."""
    # (source_id, target_id) -> {ports, peer_ip (set only for a flow_peer node)}
    agg: dict[tuple[str, str], dict] = {}

    for raw in records:
        rec = FlowLogRecord.from_collected(raw)
        home = inventory.entries.get(rec.interface_id) if rec.interface_id else None
        if home is None:
            continue  # the home ENI isn't in the combined inventory — nothing to anchor on
        if not inventory.alive_at(home, rec.start):
            continue  # home ENI didn't exist at record time — a different interface's traffic

        if rec.dstaddr in home.ips:
            peer_ip, inbound = rec.srcaddr, True
        elif rec.srcaddr in home.ips:
            peer_ip, inbound = rec.dstaddr, False
        else:
            continue  # record isn't about this ENI's own addresses
        if not peer_ip:
            continue

        peer_eni = inventory.resolve(peer_ip, rec.start)
        if peer_eni == home.eni_id:
            continue  # a flow between this ENI's own addresses — no peer

        if peer_eni is not None:
            src, tgt = (peer_eni, home.eni_id) if inbound else (home.eni_id, peer_eni)
            peer_node_ip = None
        elif inventory.ever_internal(peer_ip):
            # Internal IP, but no ENI held it at record time — historic reuse / lifetime gap. Drop
            # rather than invent an external peer for an address that is really an ENI's.
            continue
        else:
            peer_id = f"flow-peer:{peer_ip}"
            src, tgt = (peer_id, home.eni_id) if inbound else (home.eni_id, peer_id)
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
        ports = _ports_label(agg[(src, tgt)]["ports"])
        graph.add_edge(
            Edge(
                source=src,
                target=tgt,
                relationship="connects_to",
                attributes={"ports": ports, "via": "flow_log"},
            )
        )
