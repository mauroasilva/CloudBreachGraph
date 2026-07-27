"""Reconstruct ENIs from CloudTrail events — the one shared parser (``docs/02_architecture.md``).

The live ``flow_logs`` collector (``aws/collectors.py::collect_historical_enis``) and the read-only
``cloudbreachgraph-merge`` auxiliary tool (``merge.py``) both need to rebuild the ENIs that existed
in a CloudTrail window from ``CreateNetworkInterface`` / ``RunInstances`` /
``DeleteNetworkInterface`` / ``TerminateInstances`` events. This module is that single **pure**
parser so there is exactly one implementation:

    enis_from_events(events: list[dict]) -> list[dict]

``events`` are raw CloudTrail ``lookup-events`` ``Events[]`` entries (each carrying a
``CloudTrailEvent`` JSON *string* — or an already-parsed dict). Each event is dispatched by its own
``eventName`` (so a flat list mixing all four event types is handled correctly), merged by ENI id,
and returned as a sorted list of normalized dicts::

    {NetworkInterfaceId, PrivateIpAddresses[], SubnetId, VpcId, Groups[], Description,
     InterfaceType, RequesterId, InstanceId, AsgName, Name, CreatedAt, DeletedAt}

Pure and deterministic (sorted by ENI id); no AWS, no I/O.
"""

from __future__ import annotations

import json
from typing import Any

# The normalized historical-ENI dict keys, so a merged record always has every field.
_HISTORICAL_DEFAULTS: dict[str, Any] = {
    "PrivateIpAddresses": None,
    "SubnetId": None,
    "VpcId": None,
    "Groups": None,
    "Description": None,
    "InterfaceType": None,
    "RequesterId": None,
    "InstanceId": None,
    "AsgName": None,
    "Name": None,
    "CreatedAt": None,
    "DeletedAt": None,
}

# The CloudTrail event names the reconstruction understands (one query each in the live collector).
EVENT_NAMES = (
    "CreateNetworkInterface",
    "RunInstances",
    "DeleteNetworkInterface",
    "TerminateInstances",
)


def cloudtrail_detail(raw: dict) -> dict:
    """The parsed ``CloudTrailEvent`` JSON object (the interesting fields live in the *string*)."""
    detail = raw.get("CloudTrailEvent")
    if isinstance(detail, str):
        try:
            return json.loads(detail)
        except ValueError:
            return {}
    return detail if isinstance(detail, dict) else {}


def _earliest(*isos: str | None) -> str | None:
    """The earliest of some ISO-8601 timestamps (lexical order works for same-offset ISO)."""
    vals = [x for x in isos if x]
    return min(vals) if vals else None


def _iface_ips(iface: dict) -> list[str]:
    """The private IPs on a CloudTrail ``networkInterface`` element (primary + secondary set)."""
    ips: list[str] = []
    for candidate in (
        iface.get("privateIpAddress"),
        *(
            item.get("privateIpAddress")
            for item in (iface.get("privateIpAddressesSet") or {}).get("items", [])
        ),
    ):
        if candidate and candidate not in ips:
            ips.append(candidate)
    return ips


def _iface_groups(iface: dict) -> list[str]:
    """The security-group ids on a CloudTrail ``networkInterface`` element's ``groupSet``."""
    return [
        g.get("groupId") for g in (iface.get("groupSet") or {}).get("items", []) if g.get("groupId")
    ]


def _tag_items(tagset: Any) -> dict[str, str]:
    """A ``{key: value}`` map from a CloudTrail ``tagSet`` (``.items[]`` of ``{key, value}``)."""
    items = (tagset or {}).get("items", []) if isinstance(tagset, dict) else (tagset or [])
    out: dict[str, str] = {}
    for tag in items:
        key = tag.get("key", tag.get("Key"))
        if key is not None:
            out[key] = tag.get("value", tag.get("Value"))
    return out


def _merge_historical(by_eni: dict[str, dict], eni_id: str, **fields: Any) -> None:
    """Merge one event's contribution into the reconstructed record for ``eni_id``.

    First non-null wins for scalar fields; list fields (``PrivateIpAddresses``/``Groups``) union
    preserving first-seen order; ``CreatedAt`` keeps the **earliest** across event sources."""
    rec = by_eni.get(eni_id)
    if rec is None:
        rec = {"NetworkInterfaceId": eni_id, **_HISTORICAL_DEFAULTS}
        by_eni[eni_id] = rec
    for key, value in fields.items():
        if value in (None, "", []):
            continue
        if key == "CreatedAt":
            rec["CreatedAt"] = _earliest(rec.get("CreatedAt"), value)
        elif key in ("PrivateIpAddresses", "Groups"):
            existing = rec.get(key) or []
            for item in value:
                if item and item not in existing:
                    existing.append(item)
            rec[key] = existing
        elif rec.get(key) is None:
            rec[key] = value


def _absorb_create_network_interface(
    detail: dict, when: str | None, by_eni: dict[str, dict]
) -> None:
    """A ``CreateNetworkInterface`` event → a (usually standalone) reconstructed ENI."""
    iface = ((detail.get("responseElements") or {}).get("networkInterface")) or {}
    eni_id = iface.get("networkInterfaceId")
    if not eni_id:
        return
    _merge_historical(
        by_eni,
        eni_id,
        PrivateIpAddresses=_iface_ips(iface),
        SubnetId=iface.get("subnetId"),
        VpcId=iface.get("vpcId"),
        Groups=_iface_groups(iface),
        Description=iface.get("description"),
        InterfaceType=iface.get("interfaceType"),
        RequesterId=iface.get("requesterId"),
        CreatedAt=when,
    )


def _absorb_run_instances(detail: dict, when: str | None, by_eni: dict[str, dict]) -> None:
    """A ``RunInstances`` event → each instance's ENIs, tagged with the instance + its ASG name.

    Essential: most instance ENIs have **no** standalone ``CreateNetworkInterface`` event, so this
    is where an ASG fleet's ENIs (and their ``aws:autoscaling:groupName`` tag) come from."""
    items = ((detail.get("responseElements") or {}).get("instancesSet") or {}).get("items", [])
    for inst in items:
        instance_id = inst.get("instanceId")
        tags = _tag_items(inst.get("tagSet"))
        asg_name = tags.get("aws:autoscaling:groupName")
        name = tags.get("Name")
        for nif in (inst.get("networkInterfaceSet") or {}).get("items", []):
            eni_id = nif.get("networkInterfaceId")
            if not eni_id:
                continue
            _merge_historical(
                by_eni,
                eni_id,
                PrivateIpAddresses=_iface_ips(nif),
                SubnetId=nif.get("subnetId"),
                VpcId=nif.get("vpcId"),
                Groups=_iface_groups(nif),
                InstanceId=instance_id,
                AsgName=asg_name,
                Name=name,
                CreatedAt=when,
            )


def _terminated_instance_ids(detail: dict) -> list[str]:
    """The instance ids in a ``TerminateInstances`` event (request or response ``instancesSet``)."""
    out: list[str] = []
    for section in ("requestParameters", "responseElements"):
        items = ((detail.get(section) or {}).get("instancesSet") or {}).get("items", [])
        for item in items:
            iid = item.get("instanceId")
            if iid and iid not in out:
                out.append(iid)
    return out


def enis_from_events(events: list[dict]) -> list[dict]:
    """Reconstruct ENIs from a flat list of CloudTrail ``Events[]`` (§5.7 Part 2).

    Each event is dispatched by its **own** ``eventName`` (so a single list mixing
    ``CreateNetworkInterface`` / ``RunInstances`` / ``DeleteNetworkInterface`` /
    ``TerminateInstances`` is handled — and duplicates merge idempotently). The delete events set
    ``DeletedAt`` (a terminated instance's deletion cascades to its ENIs). Returns the records
    sorted by ENI id; events that carry no ENI simply contribute nothing.
    """
    by_eni: dict[str, dict] = {}
    eni_deleted: dict[str, str] = {}
    instance_deleted: dict[str, str] = {}

    for ev in events:
        detail = cloudtrail_detail(ev)
        name = detail.get("eventName")
        when = detail.get("eventTime") or ev.get("EventTime")
        if name == "CreateNetworkInterface":
            _absorb_create_network_interface(detail, when, by_eni)
        elif name == "RunInstances":
            _absorb_run_instances(detail, when, by_eni)
        elif name == "DeleteNetworkInterface":
            eni_id = (detail.get("requestParameters") or {}).get("networkInterfaceId")
            if eni_id:
                eni_deleted[eni_id] = _earliest(eni_deleted.get(eni_id), when)
        elif name == "TerminateInstances":
            for iid in _terminated_instance_ids(detail):
                instance_deleted[iid] = _earliest(instance_deleted.get(iid), when)

    # Fold deletions in: a direct DeleteNetworkInterface, or a TerminateInstances on the ENI's host.
    for eni_id, rec in by_eni.items():
        deleted = eni_deleted.get(eni_id)
        instance_id = rec.get("InstanceId")
        if instance_id and instance_deleted.get(instance_id):
            deleted = _earliest(deleted, instance_deleted[instance_id])
        rec["DeletedAt"] = deleted

    return sorted(by_eni.values(), key=lambda r: r["NetworkInterfaceId"] or "")
