"""Tests for the account/target config loader and role-aware resolver."""

from __future__ import annotations

import pytest
from conftest import EXAMPLE_TOML

from cloudbreachgraph import config

SAMPLE_TOML = """
default_target = "prod"

[accounts.workload_prod]
account_id = "111111111111"
profile    = "prod-audit"
region     = "us-east-1"

[accounts.log_archive]
account_id = "999999999999"
profile    = "log-archive-ro"
region     = "us-east-1"

[accounts.sandbox]
account_id = "333333333333"
profile    = "sandbox-ro"

[targets.prod]
default_account = "workload_prod"
[targets.prod.roles]
flow_logs = "log_archive"

[targets.sandbox]
default_account = "sandbox"
"""


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "cloudbreachgraph.toml"
    path.write_text(SAMPLE_TOML, encoding="utf-8")
    return config.load_config(str(path))


def test_load_config_parses_accounts_and_targets(cfg):
    assert set(cfg.accounts) == {"workload_prod", "log_archive", "sandbox"}
    assert cfg.default_target == "prod"
    assert cfg.targets["prod"].roles == {"flow_logs": "log_archive"}
    assert cfg.targets["prod"].default_account == "workload_prod"


def test_resolve_by_alias(cfg):
    resolved = config.resolve_profile(cfg, account="workload_prod")
    assert resolved.profile == "prod-audit"
    assert resolved.account_id == "111111111111"
    assert resolved.region == "us-east-1"


def test_resolve_by_account_id(cfg):
    # --account accepts a raw 12-digit id, matched against account_id.
    resolved = config.resolve_profile(cfg, account="333333333333")
    assert resolved.profile == "sandbox-ro"
    assert resolved.account_id == "333333333333"


def test_profile_override_takes_precedence(cfg):
    # --profile wins even when --account is also supplied; account_id is dropped.
    resolved = config.resolve_profile(cfg, account="workload_prod", profile_override="break-glass")
    assert resolved.profile == "break-glass"
    assert resolved.account_id is None


def test_region_override(cfg):
    resolved = config.resolve_profile(cfg, account="workload_prod", region="eu-west-1")
    assert resolved.region == "eu-west-1"


def test_missing_config_falls_back_to_cli_default(tmp_path, monkeypatch):
    # No file anywhere on the discovery path -> empty config, CLI-default resolution.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    empty = config.load_config(None)
    assert empty.is_empty
    resolved = config.resolve_profile(empty)
    assert resolved.profile is None
    assert resolved.account_id is None


def test_explicit_missing_config_is_error(tmp_path):
    with pytest.raises(config.ConfigError):
        config.load_config(str(tmp_path / "nope.toml"))


def test_unresolvable_account_raises(cfg):
    with pytest.raises(config.ConfigError):
        config.resolve_profile(cfg, account="does-not-exist")


def test_unresolvable_target_raises(cfg):
    with pytest.raises(config.ConfigError):
        config.resolve_target(cfg, target="ghost")


def test_multi_account_target_resolves_roles_to_different_accounts(cfg):
    resolved = config.resolve_target(cfg, target="prod", roles=("network", "flow_logs"))
    # network falls back to the target's default_account (workload_prod)...
    assert resolved.roles["network"].profile == "prod-audit"
    assert resolved.roles["network"].account_id == "111111111111"
    # ...while flow_logs is bound to the central logging account.
    assert resolved.roles["flow_logs"].profile == "log-archive-ro"
    assert resolved.roles["flow_logs"].account_id == "999999999999"
    assert resolved.target == "prod"


def test_default_target_used_when_no_selection(cfg):
    # default_target = "prod" -> network resolves to workload_prod.
    resolved = config.resolve_target(cfg)
    assert resolved.target == "prod"
    assert resolved.network.profile == "prod-audit"


def test_example_toml_parses(tmp_path):
    # The shipped docs example must remain valid and resolvable.
    cfg = config.load_config(str(EXAMPLE_TOML))
    assert "workload_prod" in cfg.accounts
    assert cfg.default_target == "prod"
    resolved = config.resolve_target(cfg, target="prod", roles=("network", "flow_logs"))
    assert resolved.roles["network"].account_id == "111111111111"
    assert resolved.roles["flow_logs"].account_id == "999999999999"


# --------------------------------------------------------------------------- #
# Account verification
# --------------------------------------------------------------------------- #
def test_verify_target_ok(cfg):
    resolved = config.resolve_target(cfg, account="workload_prod")

    def fake_run(args, *, profile=None, region=None):
        return {"Account": "111111111111"}

    seen = config.verify_target(resolved, run_aws=fake_run)
    assert seen == {"prod-audit": "111111111111"}


def test_verify_target_mismatch_raises(cfg):
    resolved = config.resolve_target(cfg, account="workload_prod")

    def fake_run(args, *, profile=None, region=None):
        return {"Account": "999999999999"}

    with pytest.raises(config.ConfigError):
        config.verify_target(resolved, run_aws=fake_run)


def test_verify_target_once_per_distinct_account(cfg):
    resolved = config.resolve_target(cfg, target="prod", roles=("network", "flow_logs"))
    calls: list[str | None] = []

    def fake_run(args, *, profile=None, region=None):
        calls.append(profile)
        return {"Account": "111111111111" if profile == "prod-audit" else "999999999999"}

    seen = config.verify_target(resolved, run_aws=fake_run)
    # Two distinct profiles -> exactly two sts calls.
    assert sorted(calls) == ["log-archive-ro", "prod-audit"]
    assert seen == {"prod-audit": "111111111111", "log-archive-ro": "999999999999"}


def test_verify_target_disabled_is_noop(cfg):
    resolved = config.resolve_target(cfg, account="workload_prod")

    def boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("run_aws should not be called when disabled")

    assert config.verify_target(resolved, enabled=False, run_aws=boom) == {}
