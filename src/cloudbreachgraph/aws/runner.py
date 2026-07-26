"""Subprocess wrapper around the AWS CLI.

Every AWS call in CloudBreachGraph goes through :func:`run_aws`. It shells out to::

    aws <args...> --output json --no-cli-pager [--region <r>] [--profile <p>]

parses the JSON on stdout, and raises :class:`AwsCliError` (surfacing stderr) on a
non-zero exit. This is the single mock boundary for the test suite — collectors are
tested by patching :func:`run_aws`, so no test ever touches the network.

The runner is read-only by construction: it does not add any mutating verbs, but
callers are responsible for only passing ``describe-*`` / ``get-*`` / ``list-*``
subcommands (see ``docs/02_architecture.md §9``).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Optional cache directory for raw-JSON dumps (see ``configure_cache``). When set,
# every ``run_aws`` response is also written verbatim to disk so Phase 2/3 and tests
# can replay real captures. ``None`` disables caching.
_cache_dir: Path | None = None

# Verbose command echo (see ``set_verbose``). When on, every ``aws`` invocation actually
# run (incl. every ``get-object``) is echoed to **stderr** with a short OK/NOT OK result
# and elapsed time, so an operator can see exactly which commands ran without polluting
# stdout or the graph files. Mirrors the ``configure_cache``/``_cache_dir`` module pattern.
_verbose: bool = False


class AwsCliError(RuntimeError):
    """Raised when an ``aws`` invocation exits non-zero or returns unparseable JSON.

    The AWS CLI's stderr is preserved on :attr:`stderr` and included in the message so
    the operator sees the real cause (expired creds, missing permission, wrong region).
    """

    def __init__(self, args: list[str], returncode: int, stderr: str) -> None:
        self.args_run = args
        self.returncode = returncode
        self.stderr = stderr
        pretty = "aws " + " ".join(args)
        super().__init__(f"AWS CLI command failed (exit {returncode}): {pretty}\n{stderr.strip()}")


def configure_cache(path: str | Path | None) -> None:
    """Enable (or disable, with ``None``) raw-JSON caching of every AWS response.

    When enabled, each response is written to ``<path>/<cache-key>.json``. Intended to
    back a ``--cache-dir`` flag in Phase 3's CLI.
    """
    global _cache_dir
    _cache_dir = Path(path) if path is not None else None


def set_verbose(value: bool) -> None:
    """Enable (or disable) echoing every ``aws`` command run to stderr (``--verbose``).

    Mirrors :func:`configure_cache`: a module-level flag toggled once by the CLI. Echoing is
    stderr-only, so stdout and the written graph files stay clean and deterministic.
    """
    global _verbose
    _verbose = value


def is_verbose() -> bool:
    """Whether verbose command echo is enabled (so callers e.g. collectors can echo retries)."""
    return _verbose


def _echo(line: str) -> None:
    """Print one verbose line to stderr, prefixed like the tool's other diagnostics."""
    if _verbose:
        print(f"cloudbreachgraph: {line}", file=sys.stderr)


def _cache_key(args: list[str]) -> str:
    """Derive a filesystem-safe cache filename from the aws sub-arguments."""
    safe = [a.replace("/", "_").replace(" ", "_") for a in args if not a.startswith("-")]
    return "-".join(safe) or "aws"


def run_aws(
    args: list[str],
    *,
    profile: str | None = None,
    region: str | None = None,
    cache_dir: str | Path | None = None,
) -> Any:
    """Run ``aws <args>`` with JSON output and return the parsed response.

    Parameters
    ----------
    args:
        The AWS CLI sub-arguments, e.g. ``["ec2", "describe-network-interfaces"]``.
        ``--output json`` and ``--no-cli-pager`` are appended automatically.
    profile:
        Optional named profile, threaded through as ``--profile``. ``None`` omits the
        flag entirely so the AWS CLI default credentials are used.
    region:
        Optional region, threaded through as ``--region``. ``None`` omits the flag so
        the CLI's configured default region applies.
    cache_dir:
        Optional per-call override of the module-level cache directory.

    Returns
    -------
    The JSON-decoded stdout (a ``dict`` for every command used here).

    Raises
    ------
    AwsCliError
        On a non-zero exit (stderr surfaced) or if stdout is not valid JSON.
    """
    cmd = ["aws", *args, "--output", "json", "--no-cli-pager"]
    if region:
        cmd += ["--region", region]
    if profile:
        cmd += ["--profile", profile]

    _echo("+ " + " ".join(cmd))
    started = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.monotonic() - started
    if proc.returncode != 0:
        _echo(f"  NOT OK (exit {proc.returncode}, {elapsed:.2f}s)")
        raise AwsCliError(args, proc.returncode, proc.stderr)
    _echo(f"  OK ({elapsed:.2f}s)")

    try:
        data = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError as exc:
        raise AwsCliError(args, proc.returncode, f"could not parse JSON output: {exc}") from exc

    target_dir = Path(cache_dir) if cache_dir is not None else _cache_dir
    if target_dir is not None:
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / f"{_cache_key(args)}.json").write_text(proc.stdout, encoding="utf-8")

    return data


def download_object(
    args: list[str],
    dest: str | Path,
    *,
    profile: str | None = None,
    region: str | None = None,
) -> Path:
    """Run a **read-only** ``aws`` command that writes a binary body to ``dest``.

    Used for ``s3api get-object`` (VPC flow-log objects are gzipped, so their body can't go
    through the JSON-parsing :func:`run_aws`). ``dest`` is appended as the output-file positional
    argument; stdout (the object metadata) is ignored. Raises :class:`AwsCliError` on a non-zero
    exit — with the **full** command (``dest`` included) so the message names the exact invocation.
    This is the same mock boundary as :func:`run_aws` — tests patch it, so no network.
    """
    full_args = [*args, str(dest)]
    cmd = ["aws", *full_args, "--no-cli-pager"]
    if region:
        cmd += ["--region", region]
    if profile:
        cmd += ["--profile", profile]

    _echo("+ " + " ".join(cmd))
    started = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.monotonic() - started
    if proc.returncode != 0:
        _echo(f"  NOT OK (exit {proc.returncode}, {elapsed:.2f}s)")
        raise AwsCliError(full_args, proc.returncode, proc.stderr)
    _echo(f"  OK ({elapsed:.2f}s)")
    return Path(dest)


def sso_login(profile: str) -> None:
    """Run the interactive ``aws sso login --profile <p>`` (the **only** non-read command).

    Unlike :func:`run_aws`/:func:`download_object` this does **not** capture stdio: SSO login is
    interactive (it may print a device code and open a browser), so the child inherits the
    terminal's stdin/stdout/stderr. It refreshes local credentials only — it never mutates AWS
    resources — and is invoked **strictly in reaction to an expired-token error** (never
    speculatively), gated by :mod:`cloudbreachgraph.cli` (see ``docs/02_architecture.md §9``).
    Raises :class:`AwsCliError` on a non-zero exit so the caller can tolerate a per-profile failure
    (e.g. a non-SSO profile) and continue.
    """
    args = ["sso", "login", "--profile", profile]
    _echo("+ aws " + " ".join(args))
    proc = subprocess.run(["aws", *args])  # inherit stdio: interactive, do NOT capture
    if proc.returncode != 0:
        _echo(f"  NOT OK (exit {proc.returncode})")
        raise AwsCliError(args, proc.returncode, "")
    _echo("  OK")
