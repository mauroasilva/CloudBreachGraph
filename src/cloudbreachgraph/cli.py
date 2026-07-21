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
from .output import dot_export, html_export, json_export

# The roles a run activates. v1 = network only; binding another role in config later means
# extending this default (or a future --roles flag) — the rest of the pipeline is role-aware.
_ROLES: tuple[str, ...] = ("network",)


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
    col.add_argument("--cache-dir", metavar="DIR", help="also write raw AWS JSON responses here")
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
        help="also write an interactive, self-contained HTML view (graph.html) whose nodes "
        "self-distribute via a force layout; falls back to the .dot when the graph is too "
        "large to render responsibly in a browser",
    )
    return p


# --------------------------------------------------------------------------- #
# Offline cache reader (--from-cache)
# --------------------------------------------------------------------------- #
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
        candidates = [
            "-".join(positional) + ".json",  # runner cache-key format
        ]
        if positional:
            candidates.append(positional[0] + "_" + "-".join(positional[1:]) + ".json")  # fixtures
        for name in candidates:
            fp = base / name
            if fp.is_file():
                return json.loads(fp.read_text(encoding="utf-8"))
        print(
            f"cloudbreachgraph: warning: no cached response for 'aws {' '.join(positional)}' "
            f"in {base} — treating as empty",
            file=sys.stderr,
        )
        return {}

    return _reader


def _collect_from_cache(cache_dir: str, region: str | None) -> dict:
    """Run the collectors against cached JSON by temporarily swapping ``runner.run_aws``."""
    resolved = ResolvedTarget(
        target=None,
        roles={"network": ResolvedAccount(profile=None, account_id=None, region=region)},
    )
    original = runner.run_aws
    runner.run_aws = _make_cache_reader(cache_dir)  # type: ignore[assignment]
    try:
        return collectors.collect_all(resolved, roles=_ROLES)
    finally:
        runner.run_aws = original  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Live collection
# --------------------------------------------------------------------------- #
def _collect_live(resolved: ResolvedTarget, args: argparse.Namespace) -> dict:
    # Verification defaults ON only when at least one role has a known expected account id.
    if args.verify_account is None:
        verify_enabled = any(acct.account_id for acct in resolved.roles.values())
    else:
        verify_enabled = args.verify_account
    if verify_enabled:
        # Resolve run_aws at call time so --from-cache/tests can swap the boundary.
        verify_target(resolved, enabled=True, run_aws=runner.run_aws)
    return collectors.collect_all(resolved, roles=_ROLES, cache_dir=args.cache_dir)


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def _write_outputs(collected: dict, out_dir: Path, stem: str, args: argparse.Namespace) -> None:
    graph = build_graph(collected, include_orphans=args.include_orphans)
    json_path = json_export.write_json(graph, out_dir / f"{stem}.json")
    dot_path = dot_export.write_dot(graph, out_dir / f"{stem}.dot")
    print(f"wrote {json_path}")
    print(f"wrote {dot_path}")

    if args.html:
        html_path = html_export.write_html(graph, out_dir / f"{stem}.html")
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
# --all-accounts
# --------------------------------------------------------------------------- #
def _run_all_accounts(cfg: AccountConfig, out_dir: Path, args: argparse.Namespace) -> int:
    if cfg.is_empty or not cfg.accounts:
        print("cloudbreachgraph: --all-accounts needs a config with [accounts.*]", file=sys.stderr)
        return 2
    for alias in sorted(cfg.accounts):
        resolved = resolve_target(cfg, account=alias, region=args.region, roles=_ROLES)
        print(f"== account {alias} ==")
        collected = _collect_live(resolved, args)
        _write_outputs(collected, out_dir, f"graph.{alias}", args)
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out_dir = Path(args.output_dir)

    try:
        # Offline: build from cached JSON, no config/credentials needed.
        if args.from_cache:
            collected = _collect_from_cache(args.from_cache, args.region)
            _write_outputs(collected, out_dir, "graph", args)
            return 0

        cfg = load_config(args.config)

        if args.all_accounts:
            return _run_all_accounts(cfg, out_dir, args)

        resolved = resolve_target(
            cfg,
            target=args.target,
            account=args.account,
            profile_override=args.profile,
            region=args.region,
            roles=_ROLES,
        )
        collected = _collect_live(resolved, args)
        _write_outputs(collected, out_dir, "graph", args)
        return 0

    except ConfigError as exc:
        print(f"cloudbreachgraph: config error: {exc}", file=sys.stderr)
        return 2
    except runner.AwsCliError as exc:
        print(f"cloudbreachgraph: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
