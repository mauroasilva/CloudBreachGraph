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
from pathlib import Path
from typing import Any

# Optional cache directory for raw-JSON dumps (see ``configure_cache``). When set,
# every ``run_aws`` response is also written verbatim to disk so Phase 2/3 and tests
# can replay real captures. ``None`` disables caching.
_cache_dir: Path | None = None


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

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AwsCliError(args, proc.returncode, proc.stderr)

    try:
        data = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError as exc:
        raise AwsCliError(args, proc.returncode, f"could not parse JSON output: {exc}") from exc

    target_dir = Path(cache_dir) if cache_dir is not None else _cache_dir
    if target_dir is not None:
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / f"{_cache_key(args)}.json").write_text(proc.stdout, encoding="utf-8")

    return data
