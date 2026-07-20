"""Resource collectors, the role registry, and the collection driver.

Each ``collect_x(profile, region) -> list[dict]`` runs exactly one ``aws`` command via
:mod:`cloudbreachgraph.aws.runner` and normalizes the response into a list of plain
dicts, preserving the fields Phase 2 maps on (``docs/02_architecture.md §4``). Collectors
are **role-agnostic**: they know nothing about targets, accounts or roles — only
``(profile, region)``. The knowledge of "which account a role runs against" lives one
level up, in :func:`collect_all`.

The role registry (:data:`ROLE_COLLECTORS` / :data:`ROLE_RESULT_KEYS`, §11.6) is the
single seam future roles extend: adding a role is a new registry entry plus its
collectors — no change to the driver loop, the config grammar, or the CLI.
"""

from __future__ import annotations

from collections.abc import Callable

from . import runner

# A collector's contract: given an optional profile and region, return normalized dicts.
Collector = Callable[[str | None, str | None], list[dict]]


# --------------------------------------------------------------------------- #
# Normalization helpers — keep original AWS key names for the fields we depend on,
# so Phase 2 reads e.g. ``eni["Attachment"]["InstanceId"]`` exactly as documented.
# --------------------------------------------------------------------------- #
def _normalize_eni(raw: dict) -> dict:
    attachment = raw.get("Attachment") or {}
    return {
        "NetworkInterfaceId": raw.get("NetworkInterfaceId"),
        "SubnetId": raw.get("SubnetId"),
        "VpcId": raw.get("VpcId"),
        "InterfaceType": raw.get("InterfaceType"),
        "Description": raw.get("Description", ""),
        "Status": raw.get("Status"),
        "AvailabilityZone": raw.get("AvailabilityZone"),
        "RequesterId": raw.get("RequesterId"),
        "RequesterManaged": raw.get("RequesterManaged"),
        "Attachment": {
            "AttachmentId": attachment.get("AttachmentId"),
            "InstanceId": attachment.get("InstanceId"),
            "InstanceOwnerId": attachment.get("InstanceOwnerId"),
            "DeviceIndex": attachment.get("DeviceIndex"),
            "Status": attachment.get("Status"),
        },
        "PrivateIpAddresses": raw.get("PrivateIpAddresses", []),
        "Groups": raw.get("Groups", []),
    }


def _normalize_instance(raw: dict) -> dict:
    return {
        "InstanceId": raw.get("InstanceId"),
        "State": {"Name": (raw.get("State") or {}).get("Name")},
        "InstanceType": raw.get("InstanceType"),
        "VpcId": raw.get("VpcId"),
        "SubnetId": raw.get("SubnetId"),
        "Tags": raw.get("Tags", []),
    }


def _normalize_elbv2(raw: dict) -> dict:
    return {
        "LoadBalancerArn": raw.get("LoadBalancerArn"),
        "LoadBalancerName": raw.get("LoadBalancerName"),
        "Type": raw.get("Type"),
        "Scheme": raw.get("Scheme"),
        "VpcId": raw.get("VpcId"),
        "DNSName": raw.get("DNSName"),
        "State": raw.get("State", {}),
    }


def _normalize_classic_elb(raw: dict) -> dict:
    return {
        "LoadBalancerName": raw.get("LoadBalancerName"),
        # Classic ELB spells the key "VPCId" (capital PC), unlike every other resource.
        "VPCId": raw.get("VPCId"),
        "DNSName": raw.get("DNSName"),
        "Scheme": raw.get("Scheme"),
        "Subnets": raw.get("Subnets", []),
        "SecurityGroups": raw.get("SecurityGroups", []),
    }


def _normalize_subnet(raw: dict) -> dict:
    return {
        "SubnetId": raw.get("SubnetId"),
        "VpcId": raw.get("VpcId"),
        "CidrBlock": raw.get("CidrBlock"),
        "AvailabilityZone": raw.get("AvailabilityZone"),
        "Tags": raw.get("Tags", []),
    }


def _normalize_vpc(raw: dict) -> dict:
    return {
        "VpcId": raw.get("VpcId"),
        "CidrBlock": raw.get("CidrBlock"),
        "IsDefault": raw.get("IsDefault"),
        "Tags": raw.get("Tags", []),
    }


# --------------------------------------------------------------------------- #
# Collectors — one AWS command each (network role)
# --------------------------------------------------------------------------- #
def collect_network_interfaces(profile: str | None, region: str | None) -> list[dict]:
    """``aws ec2 describe-network-interfaces`` -> normalized ``.NetworkInterfaces[]``."""
    data = runner.run_aws(["ec2", "describe-network-interfaces"], profile=profile, region=region)
    return [_normalize_eni(x) for x in data.get("NetworkInterfaces", [])]


def collect_ec2_instances(profile: str | None, region: str | None) -> list[dict]:
    """``aws ec2 describe-instances`` -> normalized instances, flattened out of
    ``.Reservations[].Instances[]`` into a single flat list."""
    data = runner.run_aws(["ec2", "describe-instances"], profile=profile, region=region)
    instances: list[dict] = []
    for reservation in data.get("Reservations", []):
        for inst in reservation.get("Instances", []):
            instances.append(_normalize_instance(inst))
    return instances


def collect_load_balancers_v2(profile: str | None, region: str | None) -> list[dict]:
    """``aws elbv2 describe-load-balancers`` -> normalized ``.LoadBalancers[]`` (ALB/NLB/GWLB)."""
    data = runner.run_aws(["elbv2", "describe-load-balancers"], profile=profile, region=region)
    return [_normalize_elbv2(x) for x in data.get("LoadBalancers", [])]


def collect_load_balancers_classic(profile: str | None, region: str | None) -> list[dict]:
    """``aws elb describe-load-balancers`` -> normalized ``.LoadBalancerDescriptions[]``.

    Accounts with no Classic ELBs return an empty list — handled gracefully (never an
    error) via the ``.get`` default."""
    data = runner.run_aws(["elb", "describe-load-balancers"], profile=profile, region=region)
    return [_normalize_classic_elb(x) for x in data.get("LoadBalancerDescriptions", [])]


def collect_subnets(profile: str | None, region: str | None) -> list[dict]:
    """``aws ec2 describe-subnets`` -> normalized ``.Subnets[]``."""
    data = runner.run_aws(["ec2", "describe-subnets"], profile=profile, region=region)
    return [_normalize_subnet(x) for x in data.get("Subnets", [])]


def collect_vpcs(profile: str | None, region: str | None) -> list[dict]:
    """``aws ec2 describe-vpcs`` -> normalized ``.Vpcs[]``."""
    data = runner.run_aws(["ec2", "describe-vpcs"], profile=profile, region=region)
    return [_normalize_vpc(x) for x in data.get("Vpcs", [])]


# --------------------------------------------------------------------------- #
# Role registry (§11.6) — the seam future roles extend
# --------------------------------------------------------------------------- #
ROLE_COLLECTORS: dict[str, list[Collector]] = {
    "network": [
        collect_network_interfaces,
        collect_ec2_instances,
        collect_load_balancers_v2,
        collect_load_balancers_classic,
        collect_subnets,
        collect_vpcs,
    ],
    # ── future (see docs/05_roadmap.md); do NOT implement in v1 ──────────────
    # "flow_logs": [
    #     collect_flow_logs,          # aws ec2  describe-flow-logs   -> .FlowLogs[]
    #     collect_log_destinations,   # aws logs describe-log-groups / s3api ...
    # ],
}

# Parallel to ROLE_COLLECTORS: the bundle key each collector's result is stored under.
# ``ROLE_RESULT_KEYS[role][i]`` names the output of ``ROLE_COLLECTORS[role][i]``.
ROLE_RESULT_KEYS: dict[str, list[str]] = {
    "network": [
        "network_interfaces",
        "ec2_instances",
        "load_balancers_v2",
        "load_balancers_classic",
        "subnets",
        "vpcs",
    ],
    # "flow_logs": ["flow_logs", "log_destinations"],  # future
}


# --------------------------------------------------------------------------- #
# Driver loop (§11.7)
# --------------------------------------------------------------------------- #
def collect_all(
    resolved,
    *,
    roles: tuple[str, ...] | list[str] = ("network",),
    cache_dir: str | None = None,
) -> dict:
    """Run every collector for each requested role and bundle the results (§11.7).

    ``resolved`` is a :class:`cloudbreachgraph.config.ResolvedTarget`; each role is run
    with its own resolved ``profile``/``region``, so a multi-account target can pull
    different roles from different accounts in one call. Per-role account provenance is
    recorded under ``meta.accounts``.

    Returns the exact Phase 1 interface contract (``docs/03_phase_plan.md``)::

        {
          "meta": {"target": str | None, "region": str | None,
                   "accounts": {role: account_id | None, ...}},
          "network_interfaces": [...], "ec2_instances": [...],
          "load_balancers_v2": [...], "load_balancers_classic": [...],
          "subnets": [...], "vpcs": [...],
        }
    """
    roles = tuple(roles)
    if cache_dir is not None:
        runner.configure_cache(cache_dir)

    # meta.region reflects the first requested role's region (network in v1).
    first_region = resolved.roles[roles[0]].region if roles else None
    bundle: dict = {
        "meta": {
            "target": resolved.target,
            "region": first_region,
            "accounts": {},
        }
    }

    for role in roles:
        acct = resolved.roles[role]
        collectors = ROLE_COLLECTORS[role]
        keys = ROLE_RESULT_KEYS[role]
        for collector, key in zip(collectors, keys, strict=True):
            bundle[key] = collector(acct.profile, acct.region)
        bundle["meta"]["accounts"][role] = acct.account_id

    return bundle
