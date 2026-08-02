"""Command-line entrypoint — wire the pipeline end to end.

``cloudbreachgraph`` resolves *which account/profile to use* (``config``), *collects*
network resources via the AWS CLI (``aws.collectors``), *builds* the topology graph
(``mapping.builder``), and *writes* ``graph.json`` + ``graph.dot`` (``output``).

The flow, per ``docs/02_architecture.md §10–§11`` and ``docs/03_phase_plan.md`` Phase 3::

    load_config → resolve_target → verify_target → collect_all → build_graph → write_json/write_dot

Targeting flags surface the operator's "for account X use profile Y" requirement:

* ``--target <name>``   — a config target that binds each resource *role* to an account.
  v1 only runs the ``network`` role, but resolution goes through ``resolve_target`` so
  binding ``flow_logs`` to another account later needs **no CLI change** (``§11``).
* ``--account <alias|id>`` — shorthand: a target whose every role is that one account.
* ``--profile <name>``   — direct override that bypasses the mapping (all roles).
* ``--from-cache <dir>`` — build from previously cached AWS JSON with **no** live calls.

Everything here is read-only: the only AWS calls made are the collectors' ``describe-*``
and the optional ``sts get-caller-identity`` verification (``docs/02_architecture.md §9``).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .aws import collectors, runner
from .config import (
    AccountConfig,
    ConfigError,
    ResolvedAccount,
    ResolvedTarget,
    load_config,
    resolve_target,
    verify_target,
)
from .mapping.builder import build_graph
from .mapping.collapse import collapse_autoscaling_groups
from .output import dot_export, html_export, json_export

# The roles a run activates. ``network`` is always on; ``--flow-logs`` adds ``flow_logs`` (§5.7).
# The rest of the pipeline is role-aware, so this is the only place the active set is chosen.
_ROLES: tuple[str, ...] = ("network",)


def _active_roles(args: argparse.Namespace) -> tuple[str, ...]:
    """The roles this run collects: always ``network``, plus ``flow_logs`` under ``--flow-logs``."""
    return (*_ROLES, "flow_logs") if getattr(args, "flow_logs", False) else _ROLES


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cloudbreachgraph",
        description="Map an AWS account's network topology (ENIs -> EC2/LB -> subnets -> VPCs) "
        "using the AWS CLI. Read-only.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="echo every aws command actually run (incl. every get-object, retries and any "
        "aws sso login) to stderr with a short OK/NOT OK result; stdout and the graph files "
        "stay clean. With --from-cache, echoes which cached response each command is served from",
    )

    # Targeting (precedence: --profile > --target > --account > config default > CLI default).
    tgt = p.add_argument_group("targeting")
    tgt.add_argument("--target", metavar="NAME", help="config target binding roles to accounts")
    tgt.add_argument("--account", metavar="ALIAS|ID", help="account alias or 12-digit id")
    tgt.add_argument("--profile", metavar="NAME", help="AWS CLI profile override (all roles)")
    tgt.add_argument("--config", metavar="PATH", help="path to the TOML config file")
    tgt.add_argument(
        "--verify-account",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="verify the profile maps to the expected account via sts get-caller-identity "
        "(default: on when the account id is known)",
    )
    tgt.add_argument(
        "--all-accounts",
        action="store_true",
        help="loop over every configured account, writing one graph each (graph.<alias>.json/.dot)",
    )

    # Collection / offline.
    col = p.add_argument_group("collection")
    col.add_argument(
        "--region", metavar="REGION", help="AWS region (overrides per-account default)"
    )
    col.add_argument(
        "--cache-dir",
        metavar="DIR",
        help="also write raw AWS JSON responses here, and cache downloaded S3 flow-log objects "
        "(reused for 30 days, so a re-run doesn't re-download them)",
    )
    col.add_argument(
        "--from-cache",
        metavar="DIR",
        help="build from previously cached AWS JSON in DIR with no live AWS calls",
    )
    col.add_argument(
        "--include-orphans",
        action="store_true",
        help="also emit collected resources that no ENI references (subnets, VPCs, EC2, LBs)",
    )
    col.add_argument(
        "--flow-logs",
        action="store_true",
        help="also gather IP-allocation history and analyse VPC flow logs (last 60 days): record "
        "flow-log config as a VPC attribute and, for each observed connection, add a connects_to "
        "edge to the peer (ENI->ENI when the peer IP is another collected ENI that already held it "
        "at the time, else a flow_peer node). Reads records from CloudWatch Logs or S3 per the "
        "flow log's destination type. Needs extra read-only IAM: ec2:DescribeFlowLogs, "
        "cloudtrail:LookupEvents, logs:FilterLogEvents, and s3:ListBucket + s3:GetObject for S3 "
        "destinations",
    )
    # The record window is either a day count (--flow-log-days) or an explicit start[/end] range
    # (--flow-log-start[/--flow-log-end]); the first two are mutually exclusive.
    window = col.add_mutually_exclusive_group()
    window.add_argument(
        "--flow-log-days",
        type=int,
        default=collectors.FLOW_LOG_MAX_LOOKBACK_DAYS,
        metavar="N",
        help=f"how many days of flow-log records to analyse (default "
        f"{collectors.FLOW_LOG_MAX_LOOKBACK_DAYS}; only with --flow-logs). The 90-day CloudTrail "
        f"history used to reconstruct historical ENIs is unaffected (always the full retention, "
        f"never shorter than this window)",
    )
    window.add_argument(
        "--flow-log-start",
        metavar="TIMESTAMP",
        help="analyse flow-log records from this timestamp instead of --flow-log-days (ISO-8601 "
        "like 2026-05-01 or 2026-05-01T12:00:00Z, or epoch seconds; only with --flow-logs). "
        "Records are read from here up to now, or to --flow-log-end if given",
    )
    col.add_argument(
        "--flow-log-end",
        metavar="TIMESTAMP",
        help="with --flow-log-start, only analyse records up to this timestamp (default: now); "
        "same timestamp formats as --flow-log-start",
    )
    col.add_argument(
        "--historical-enis",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="with --flow-logs, reconstruct ENIs that existed in the last 90 days from CloudTrail "
        "(on by default) so a flow captured on a now-terminated ASG ENI is analysed and its peers "
        "resolve to the ENI that held the IP at the time. --no-historical-enis turns it off; "
        "consider --collapse-asgs to keep the graph readable",
    )
    col.add_argument(
        "--collapse-asgs",
        action="store_true",
        help="collapse every Auto Scaling group's instances and ENIs (current + historical) into a "
        "single autoscaling_group node, merging their edges — tames the fan-out of a churning ASG",
    )
    col.add_argument(
        "--security-groups",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="show security groups as nodes between each ENI and its reachability sources "
        "(default: on). --no-security-groups hides them and connects the source IPs directly "
        "to the ENIs, with the routable/not-routable split",
    )

    # Output.
    out = p.add_argument_group("output")
    out.add_argument(
        "--output-dir", metavar="DIR", default=".", help="where to write outputs (default: .)"
    )
    out.add_argument(
        "--render",
        choices=("png", "svg"),
        help="also rasterize the .dot with Graphviz (requires the `dot` binary on PATH)",
    )
    out.add_argument(
        "--html",
        action="store_true",
        help="also write an interactive, self-contained HTML view (graph.html); by default its "
        "nodes self-distribute via an in-browser force layout. Falls back to the .dot when the "
        "graph is too large to render responsibly in a browser",
    )
    out.add_argument(
        "--ringed",
        action="store_true",
        help=f"with --html, {html_export.RINGED_HELP}",
    )
    out.add_argument(
        "--hierarchical",
        action="store_true",
        help=f"with --html, {html_export.HIERARCHICAL_HELP}",
    )
    out.add_argument(
        "--optimize-passes",
        type=int,
        default=0,
        metavar="N",
        help=f"with --html, {html_export.OPTIMIZE_PASSES_HELP}",
    )
    return p


# --------------------------------------------------------------------------- #
# Offline cache reader (--from-cache)
# --------------------------------------------------------------------------- #
def _cache_variant(args: list[str]) -> str | None:
    """A cache-file variant suffix from a value-carrying lookup flag, or ``None``.

    Currently only ``cloudtrail lookup-events`` needs one: its ``--lookup-attributes`` carries the
    ``EventName`` the query is scoped to, so ``--from-cache`` can serve a distinct fixture per event
    (the historical-ENI reconstruction issues one query per EventName)."""
    for a in args:
        if a.startswith("--lookup-attributes=") and "AttributeValue=" in a:
            return a.split("AttributeValue=", 1)[1].split(",")[0].strip().lower()
    return None


def _make_cache_reader(cache_dir: str | Path):
    """A drop-in replacement for ``runner.run_aws`` that reads cached JSON off disk.

    It maps an ``aws`` sub-argument list (e.g. ``["ec2", "describe-network-interfaces"]``)
    to a file in ``cache_dir``, trying both the tool's own ``--cache-dir`` naming
    (``ec2-describe-network-interfaces.json``) and the ``tests/fixtures`` naming
    (``ec2_describe-network-interfaces.json``). A missing file yields ``{}`` so empty
    resources (e.g. no load balancers) flow through gracefully, exactly as a live empty
    response would.
    """
    base = Path(cache_dir)

    def _reader(args: list[str], *, profile=None, region=None, cache_dir=None):
        positional = [a for a in args if not a.startswith("-")]
        # A per-EventName variant for the repeated ``cloudtrail lookup-events`` queries (§5.7): the
        # historical-ENI reconstruction runs one per EventName, so the fixture is disambiguated by
        # ``<service>_<command>.<eventname>.json`` and only falls back to the un-suffixed file.
        variant = _cache_variant(args)
        candidates: list[str] = []
        if variant and positional:
            candidates.append(positional[0] + "_" + "-".join(positional[1:]) + f".{variant}.json")
            candidates.append("-".join(positional) + f".{variant}.json")
        candidates.append("-".join(positional) + ".json")  # runner cache-key format
        if positional:
            candidates.append(positional[0] + "_" + "-".join(positional[1:]) + ".json")  # fixtures
        for name in candidates:
            fp = base / name
            if fp.is_file():
                if runner.is_verbose():
                    print(
                        f"cloudbreachgraph: + [cache] aws {' '.join(positional)} -> {fp}",
                        file=sys.stderr,
                    )
                return json.loads(fp.read_text(encoding="utf-8"))
        print(
            f"cloudbreachgraph: warning: no cached response for 'aws {' '.join(positional)}' "
            f"in {base} — treating as empty",
            file=sys.stderr,
        )
        return {}

    return _reader


def _collect_from_cache(cache_dir: str, region: str | None, roles: tuple[str, ...]) -> dict:
    """Run the collectors against cached JSON by temporarily swapping ``runner.run_aws``."""
    resolved = ResolvedTarget(
        target=None,
        roles={
            role: ResolvedAccount(profile=None, account_id=None, region=region) for role in roles
        },
    )
    original = runner.run_aws
    runner.run_aws = _make_cache_reader(cache_dir)  # type: ignore[assignment]
    try:
        return collectors.collect_all(resolved, roles=roles)
    finally:
        runner.run_aws = original  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Live collection
# --------------------------------------------------------------------------- #
def _collect_live(
    resolved: ResolvedTarget, args: argparse.Namespace, roles: tuple[str, ...]
) -> dict:
    # Verification defaults ON only when at least one role has a known expected account id.
    if args.verify_account is None:
        verify_enabled = any(acct.account_id for acct in resolved.roles.values())
    else:
        verify_enabled = args.verify_account
    if verify_enabled:
        # Resolve run_aws at call time so --from-cache/tests can swap the boundary.
        verify_target(resolved, enabled=True, run_aws=runner.run_aws)
    return collectors.collect_all(resolved, roles=roles, cache_dir=args.cache_dir)


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def _write_outputs(collected: dict, out_dir: Path, stem: str, args: argparse.Namespace) -> None:
    graph = build_graph(
        collected,
        include_orphans=args.include_orphans,
        show_security_groups=args.security_groups,
        map_flow_logs=args.flow_logs,
    )
    if args.collapse_asgs:
        # A post-build view transform (§5.7 Part 4): fold each ASG's members into one node.
        graph = collapse_autoscaling_groups(graph)
    json_path = json_export.write_json(graph, out_dir / f"{stem}.json")
    dot_path = dot_export.write_dot(graph, out_dir / f"{stem}.dot")
    print(f"wrote {json_path}")
    print(f"wrote {dot_path}")

    if args.html:
        html_path = html_export.write_layout_html(
            graph,
            out_dir / f"{stem}.html",
            ringed=args.ringed,
            hierarchical=args.hierarchical,
            optimize_passes=args.optimize_passes,
        )
        if html_path is None:
            # Too large to render responsibly in a browser: fall back to the .dot, which
            # Graphviz can lay out offline at any scale (docs/02_architecture.md §7).
            print(
                f"cloudbreachgraph: warning: graph too large for an interactive HTML view "
                f"(> {html_export.MAX_NODES} nodes); skipped {stem}.html — use {dot_path} "
                f"with Graphviz instead.",
                file=sys.stderr,
            )
        else:
            print(f"wrote {html_path}")

    if args.render:
        rendered = dot_export.render(dot_path, args.render)
        if rendered is None:
            print(
                "cloudbreachgraph: warning: `dot` not found on PATH; wrote .dot only "
                "(install Graphviz to render). See docs/02_architecture.md §7.",
                file=sys.stderr,
            )
        else:
            print(f"wrote {rendered}")


# --------------------------------------------------------------------------- #
# SSO re-login (the sole error-gated non-read command) + retry-once
# --------------------------------------------------------------------------- #
def _config_profiles(cfg: AccountConfig) -> list[str]:
    """Every distinct named profile in the loaded config (the profiles to re-login)."""
    return sorted({acct.profile for acct in cfg.accounts.values() if acct.profile})


def _sso_login_all(cfg: AccountConfig) -> None:
    """Run ``aws sso login`` for **every** distinct profile in the config, tolerating per-profile
    failures (e.g. a non-SSO profile errors — warn and continue). This is the tool's only non-read
    command; it runs strictly in reaction to an expired-token error (``docs/02_architecture.md``
    §9)."""
    profiles = _config_profiles(cfg)
    if not profiles:
        print(
            "cloudbreachgraph: warning: credentials expired but the config names no profiles to "
            "`aws sso login` — authenticate manually and re-run.",
            file=sys.stderr,
        )
        return
    for profile in profiles:
        try:
            runner.sso_login(profile)
        except runner.AwsCliError as exc:
            print(
                f"cloudbreachgraph: warning: `aws sso login --profile {profile}` failed "
                f"(a non-SSO profile is expected to); continuing. ({exc})",
                file=sys.stderr,
            )


def _run_live(
    cfg: AccountConfig, args: argparse.Namespace, out_dir: Path, roles: tuple[str, ...]
) -> int:
    """Resolve the target and run collect → build → write for the live (non-cache) path."""
    if args.all_accounts:
        return _run_all_accounts(cfg, out_dir, args)
    resolved = resolve_target(
        cfg,
        target=args.target,
        account=args.account,
        profile_override=args.profile,
        region=args.region,
        roles=roles,
    )
    collected = _collect_live(resolved, args, roles)
    _write_outputs(collected, out_dir, "graph", args)
    return 0


def _run_live_with_sso_retry(
    cfg: AccountConfig, args: argparse.Namespace, out_dir: Path, roles: tuple[str, ...]
) -> int:
    """Run the live pipeline; on an expired-token error, `aws sso login` for every configured
    profile and retry the whole run **once** (``docs/02_architecture.md §9``)."""
    try:
        return _run_live(cfg, args, out_dir, roles)
    except (collectors.CredentialsExpiredError, runner.AwsCliError) as exc:
        if not collectors.is_expired_error(exc):
            raise  # not an expiry — let main()'s handlers classify it
        print(
            "cloudbreachgraph: credentials expired; running `aws sso login` for every configured "
            "profile, then retrying the run once...",
            file=sys.stderr,
        )
        _sso_login_all(cfg)
        try:
            return _run_live(cfg, args, out_dir, roles)
        except (collectors.CredentialsExpiredError, runner.AwsCliError) as exc2:
            if collectors.is_expired_error(exc2):
                print(
                    "cloudbreachgraph: credentials are still expired after `aws sso login`. "
                    "Authenticate and re-run.",
                    file=sys.stderr,
                )
                return 1
            raise


# --------------------------------------------------------------------------- #
# --all-accounts
# --------------------------------------------------------------------------- #
def _run_all_accounts(cfg: AccountConfig, out_dir: Path, args: argparse.Namespace) -> int:
    if cfg.is_empty or not cfg.accounts:
        print("cloudbreachgraph: --all-accounts needs a config with [accounts.*]", file=sys.stderr)
        return 2
    roles = _active_roles(args)
    for alias in sorted(cfg.accounts):
        resolved = resolve_target(cfg, account=alias, region=args.region, roles=roles)
        print(f"== account {alias} ==")
        collected = _collect_live(resolved, args, roles)
        _write_outputs(collected, out_dir, f"graph.{alias}", args)
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _parse_timestamp(value: str) -> float:
    """Parse an ISO-8601 timestamp or epoch seconds into epoch seconds (UTC-assumed if naive).

    Accepts ``2026-05-01``, ``2026-05-01T12:00:00``, ``...Z``, ``...+00:00`` or a bare epoch-seconds
    integer. Raises :class:`ValueError` with an actionable message on anything else."""
    v = value.strip()
    if v.isdigit():
        return float(v)
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"invalid timestamp {value!r} (use ISO-8601 like 2026-05-01T12:00:00Z or epoch seconds)"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _configure_flow_log_window(args: argparse.Namespace) -> str | None:
    """Apply the flow-log-record window to the collectors from the CLI args.

    Uses the explicit ``--flow-log-start``/``--flow-log-end`` range when given, else the
    ``--flow-log-days`` count. Returns an error message (for exit 2) or ``None`` on success.
    ``--flow-log-days`` and ``--flow-log-start`` are already mutually exclusive via argparse."""
    collectors.set_flow_log_range(None)  # reset; default is the days-based window
    if args.flow_log_start is not None:
        try:
            start = _parse_timestamp(args.flow_log_start)
            end = _parse_timestamp(args.flow_log_end) if args.flow_log_end else None
        except ValueError as exc:
            return str(exc)
        if end is not None and end <= start:
            return "--flow-log-end must be after --flow-log-start"
        collectors.set_flow_log_range(start, end)
        return None
    if args.flow_log_end is not None:
        return "--flow-log-end requires --flow-log-start"
    if args.flow_log_days <= 0:
        return "--flow-log-days must be > 0"
    collectors.set_flow_log_window(args.flow_log_days)
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out_dir = Path(args.output_dir)
    runner.set_verbose(bool(args.verbose))

    # Thread the flow-log window + historical-ENI toggle into the collectors (module-level knobs,
    # mirroring configure_cache), so the collect_x(profile, region) contract is untouched (§5.7).
    window_error = _configure_flow_log_window(args)
    if window_error is not None:
        print(f"cloudbreachgraph: {window_error}", file=sys.stderr)
        return 2
    collectors.set_historical_enis(args.flow_logs and args.historical_enis)

    if args.optimize_passes < 0:
        print("cloudbreachgraph: --optimize-passes must be >= 0", file=sys.stderr)
        return 2
    if not args.html:
        if args.ringed:
            print(
                "cloudbreachgraph: warning: --ringed only affects --html; ignoring it.",
                file=sys.stderr,
            )
        if args.hierarchical:
            print(
                "cloudbreachgraph: warning: --hierarchical only affects --html; ignoring it.",
                file=sys.stderr,
            )
        if args.optimize_passes:
            print(
                "cloudbreachgraph: warning: --optimize-passes only affects --html; ignoring it.",
                file=sys.stderr,
            )

    try:
        roles = _active_roles(args)

        # Offline: build from cached JSON, no config/credentials needed (so no SSO path here).
        if args.from_cache:
            collected = _collect_from_cache(args.from_cache, args.region, roles)
            _write_outputs(collected, out_dir, "graph", args)
            return 0

        cfg = load_config(args.config)
        return _run_live_with_sso_retry(cfg, args, out_dir, roles)

    except ConfigError as exc:
        print(f"cloudbreachgraph: config error: {exc}", file=sys.stderr)
        return 2
    except collectors.FlowLogDestinationError as exc:
        print(f"cloudbreachgraph: {exc}", file=sys.stderr)
        return 1
    except collectors.FlowLogFetchError as exc:
        print(f"cloudbreachgraph: {exc}", file=sys.stderr)
        return 1
    except runner.AwsCliError as exc:
        print(f"cloudbreachgraph: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
