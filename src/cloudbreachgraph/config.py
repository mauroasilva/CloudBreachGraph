"""Account -> profile mapping and role-aware target resolution.

This is the Phase 1 implementation of ``docs/02_architecture.md §10`` (account/profile
mapping) and ``§11`` (resource roles & multi-account targets). It is deliberately
independent of the collectors: it knows about *accounts*, *targets* and *roles* — never
about which AWS commands a role runs (that lives in ``aws/collectors.py``).

Key concepts
------------
* **account** — the atom: an alias mapped to ``{account_id, profile, region}``.
* **target**  — a named environment binding each resource *role* to an account. A role
  not explicitly bound falls back to the target's ``default_account``.
* **role**    — a named group of resources fetched from one account. v1 activates only
  ``network``; ``flow_logs`` (and others) can be bound in config with no grammar change.

Resolution precedence (per role, first match wins):
``--profile`` override  →  ``--target`` / ``--account`` binding (or ``default``)  →  CLI default.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .aws import runner

# v1 activates only the ``network`` role. Kept as a tuple so the resolver has a stable
# default; future roles are resolved by passing them explicitly to ``resolve_target``.
DEFAULT_ROLES: tuple[str, ...] = ("network",)


class ConfigError(Exception):
    """Raised for unparseable config, an unresolvable target/account/role, or a failed
    account-verification check."""


# --------------------------------------------------------------------------- #
# Parsed-config shapes
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Account:
    """One ``[accounts.<alias>]`` entry."""

    alias: str
    account_id: str | None
    profile: str | None
    region: str | None = None


@dataclass(frozen=True)
class Target:
    """One ``[targets.<name>]`` entry: a role->account-alias binding plus a default."""

    name: str
    default_account: str | None
    roles: dict[str, str] = field(default_factory=dict)


@dataclass
class AccountConfig:
    """The whole parsed config file (empty when no file was found)."""

    accounts: dict[str, Account] = field(default_factory=dict)
    targets: dict[str, Target] = field(default_factory=dict)
    default_target: str | None = None
    default_account: str | None = None
    path: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.accounts and not self.targets


# --------------------------------------------------------------------------- #
# Resolved shapes (the contract Phase 3's CLI consumes)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ResolvedAccount:
    """A single role's resolution result.

    ``profile is None`` means "use the AWS CLI default credentials" (pass no
    ``--profile``). ``account_id is None`` means the expected account is unknown, so
    verification is skipped for it.
    """

    profile: str | None
    account_id: str | None
    region: str | None


@dataclass(frozen=True)
class ResolvedTarget:
    """The full resolution: one :class:`ResolvedAccount` per requested role."""

    target: str | None
    roles: dict[str, ResolvedAccount]

    @property
    def network(self) -> ResolvedAccount:
        """Convenience accessor for the v1 ``network`` role."""
        return self.roles["network"]


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def _discovery_paths() -> list[Path]:
    """Config discovery order when ``--config`` is not given (see §10.1)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    xdg_base = Path(xdg) if xdg else Path.home() / ".config"
    return [
        Path("cloudbreachgraph.toml"),
        xdg_base / "cloudbreachgraph" / "config.toml",
    ]


def load_config(path: str | None) -> AccountConfig:
    """Discover, read and parse the config file.

    Discovery when ``path`` is ``None`` (first existing file wins): ``./cloudbreachgraph.toml``
    then ``$XDG_CONFIG_HOME/cloudbreachgraph/config.toml`` (default
    ``~/.config/cloudbreachgraph/config.toml``). A missing config file is **not** an error —
    an empty :class:`AccountConfig` is returned, so the tool still works with only CLI
    defaults. An explicitly given ``path`` that does not exist **is** an error.
    """
    chosen: Path | None
    if path is not None:
        chosen = Path(path)
        if not chosen.is_file():
            raise ConfigError(f"config file not found: {path}")
    else:
        chosen = next((p for p in _discovery_paths() if p.is_file()), None)
        if chosen is None:
            return AccountConfig()

    try:
        with chosen.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"could not read config {chosen}: {exc}") from exc

    return _parse_config(data, str(chosen))


def _parse_config(data: dict, path: str) -> AccountConfig:
    accounts: dict[str, Account] = {}
    for alias, raw in (data.get("accounts") or {}).items():
        accounts[alias] = Account(
            alias=alias,
            account_id=raw.get("account_id"),
            profile=raw.get("profile"),
            region=raw.get("region"),
        )

    targets: dict[str, Target] = {}
    for name, raw in (data.get("targets") or {}).items():
        targets[name] = Target(
            name=name,
            default_account=raw.get("default_account"),
            roles=dict(raw.get("roles") or {}),
        )

    return AccountConfig(
        accounts=accounts,
        targets=targets,
        default_target=data.get("default_target"),
        default_account=data.get("default_account"),
        path=path,
    )


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def _find_account(cfg: AccountConfig, key: str) -> Account:
    """Resolve an account by its alias (table key) or its 12-digit ``account_id``."""
    if key in cfg.accounts:
        return cfg.accounts[key]
    for acct in cfg.accounts.values():
        if acct.account_id == key:
            return acct
    raise ConfigError(
        f"account {key!r} not found in config (known aliases: {sorted(cfg.accounts) or 'none'})"
    )


def _resolve_from_account(
    acct: Account, roles: tuple[str, ...], region: str | None
) -> dict[str, ResolvedAccount]:
    """Every role resolves to the same single account (the ``--account`` case)."""
    return {
        role: ResolvedAccount(
            profile=acct.profile,
            account_id=acct.account_id,
            region=region or acct.region,
        )
        for role in roles
    }


def resolve_target(
    cfg: AccountConfig,
    *,
    target: str | None = None,
    account: str | None = None,
    profile_override: str | None = None,
    region: str | None = None,
    roles: tuple[str, ...] = DEFAULT_ROLES,
) -> ResolvedTarget:
    """Resolve each requested role to a ``{profile, account_id, region}`` (§11.3).

    Precedence (per role, first match wins):

    1. ``profile_override`` — applies to **every** role; ``account_id`` becomes ``None``
       (verification skipped), matching the "escape hatch" semantics of ``--profile``.
    2. ``target`` — each role uses its ``[targets.<name>.roles]`` binding, else the
       target's ``default_account``.
    3. ``account`` — a target whose every role is that one account (alias or id).
    4. none of the above — the config's ``default_target``, else ``default_account``,
       else the AWS CLI default (``profile=None`` for every role).

    Raises :class:`ConfigError` if a requested ``target``/``account``/role cannot be
    resolved.
    """
    roles = tuple(roles)

    # 1. --profile override: one profile for all roles, no expected account id.
    if profile_override is not None:
        resolved = {
            role: ResolvedAccount(profile=profile_override, account_id=None, region=region)
            for role in roles
        }
        return ResolvedTarget(target=None, roles=resolved)

    # 2. --target
    if target is not None:
        return ResolvedTarget(
            target=target, roles=_resolve_target_binding(cfg, target, roles, region)
        )

    # 3. --account
    if account is not None:
        acct = _find_account(cfg, account)
        return ResolvedTarget(target=None, roles=_resolve_from_account(acct, roles, region))

    # 4. defaults
    if cfg.default_target is not None:
        return ResolvedTarget(
            target=cfg.default_target,
            roles=_resolve_target_binding(cfg, cfg.default_target, roles, region),
        )
    if cfg.default_account is not None:
        acct = _find_account(cfg, cfg.default_account)
        return ResolvedTarget(target=None, roles=_resolve_from_account(acct, roles, region))

    # No config selection at all: fall back to the AWS CLI default credentials.
    resolved = {
        role: ResolvedAccount(profile=None, account_id=None, region=region) for role in roles
    }
    return ResolvedTarget(target=None, roles=resolved)


def _resolve_target_binding(
    cfg: AccountConfig, target: str, roles: tuple[str, ...], region: str | None
) -> dict[str, ResolvedAccount]:
    tgt = cfg.targets.get(target)
    if tgt is None:
        raise ConfigError(
            f"target {target!r} not found in config (known targets: "
            f"{sorted(cfg.targets) or 'none'})"
        )
    resolved: dict[str, ResolvedAccount] = {}
    for role in roles:
        alias = tgt.roles.get(role, tgt.default_account)
        if alias is None:
            raise ConfigError(
                f"target {target!r} does not bind role {role!r} and has no default_account"
            )
        acct = _find_account(cfg, alias)
        resolved[role] = ResolvedAccount(
            profile=acct.profile,
            account_id=acct.account_id,
            region=region or acct.region,
        )
    return resolved


def resolve_profile(
    cfg: AccountConfig,
    *,
    account: str | None = None,
    profile_override: str | None = None,
    region: str | None = None,
) -> ResolvedAccount:
    """Thin single-account wrapper: resolve just the ``network`` role (§10.2).

    Kept for the common one-account case and backward compatibility with the §10 API.
    Equivalent to ``resolve_target(..., roles=("network",)).network``.
    """
    resolved = resolve_target(
        cfg,
        account=account,
        profile_override=profile_override,
        region=region,
        roles=("network",),
    )
    return resolved.network


# --------------------------------------------------------------------------- #
# Account verification
# --------------------------------------------------------------------------- #
def verify_target(
    resolved: ResolvedTarget,
    *,
    enabled: bool = True,
    run_aws=runner.run_aws,
) -> dict[str | None, str | None]:
    """Verify each distinct resolved account with ``sts get-caller-identity`` (§11.5).

    The check runs **once per distinct resolved profile** (credentials determine the
    account, so the same profile is queried once). For every role whose
    :class:`ResolvedAccount` carries an expected ``account_id``, the returned
    ``.Account`` must match, otherwise :class:`ConfigError` is raised — catching a
    mis-bound role or a profile that points at the wrong account.

    Returns a ``{profile: actual_account_id}`` map for the accounts it checked. When
    ``enabled`` is ``False`` it is a no-op returning ``{}``. ``run_aws`` is injectable so
    tests never touch the network.
    """
    if not enabled:
        return {}

    seen: dict[str | None, str | None] = {}
    for role, acct in resolved.roles.items():
        if acct.profile in seen:
            actual = seen[acct.profile]
        else:
            identity = run_aws(
                ["sts", "get-caller-identity"], profile=acct.profile, region=acct.region
            )
            actual = identity.get("Account")
            seen[acct.profile] = actual
        if acct.account_id and actual != acct.account_id:
            raise ConfigError(
                f"account mismatch for role {role!r}: profile {acct.profile!r} resolves to "
                f"account {actual} but config expects {acct.account_id}"
            )
    return seen
