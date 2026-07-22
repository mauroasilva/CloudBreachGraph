"""End-to-end tests for the Phase 3 CLI (``cloudbreachgraph.cli:main``).

Fully offline. Two boundaries are exercised:

* ``--from-cache`` reads the recorded ``tests/fixtures`` JSON straight off disk (no mock
  needed — that path makes zero live AWS calls by design).
* live runs mock ``runner.run_aws`` so we can assert *which profile* each collector ran
  with (the "for account X use profile Y" contract) without touching the network.
"""

from __future__ import annotations

import json

import pytest
from conftest import FIXTURES, load_fixture

from cloudbreachgraph import cli
from cloudbreachgraph.aws import runner
from cloudbreachgraph.output import dot_export, html_export

_COMMAND_FIXTURES = {
    ("ec2", "describe-network-interfaces"): "ec2_describe-network-interfaces.json",
    ("ec2", "describe-instances"): "ec2_describe-instances.json",
    ("elbv2", "describe-load-balancers"): "elbv2_describe-load-balancers.json",
    ("elb", "describe-load-balancers"): "elb_describe-load-balancers.json",
    ("ec2", "describe-subnets"): "ec2_describe-subnets.json",
    ("ec2", "describe-vpcs"): "ec2_describe-vpcs.json",
}

_CONFIG = """
[accounts.workload_prod]
account_id = "111111111111"
profile    = "prod-audit"
region     = "us-east-1"

[accounts.workload_sandbox]
account_id = "333333333333"
profile    = "sandbox-ro"
"""


@pytest.fixture
def fake_aws(monkeypatch):
    """Serve fixtures for every describe-*, record (args, profile, region), stub sts."""
    calls: list[dict] = []
    state = {"sts_account": "111111111111"}

    def _run(args, *, profile=None, region=None, cache_dir=None):
        calls.append({"args": list(args), "profile": profile, "region": region})
        key = tuple(args[:2])
        if key == ("sts", "get-caller-identity"):
            return {"Account": state["sts_account"], "Arn": "arn:aws:iam::x:user/y"}
        return load_fixture(_COMMAND_FIXTURES[key])

    monkeypatch.setattr(runner, "run_aws", _run)
    fake = type("Fake", (), {"calls": calls, "state": state})
    return fake


def _write_config(tmp_path) -> str:
    cfg = tmp_path / "cloudbreachgraph.toml"
    cfg.write_text(_CONFIG, encoding="utf-8")
    return str(cfg)


def _profiles_for_describe(calls: list[dict]) -> set[str | None]:
    """The profiles used by the network describe-* collectors (excludes sts)."""
    return {c["profile"] for c in calls if tuple(c["args"][:2]) != ("sts", "get-caller-identity")}


# --------------------------------------------------------------------------- #
# --from-cache (offline, no mock)
# --------------------------------------------------------------------------- #
def test_from_cache_produces_wellformed_outputs(tmp_path):
    out = tmp_path / "out"
    rc = cli.main(["--from-cache", str(FIXTURES), "--output-dir", str(out)])
    assert rc == 0

    json_path = out / "graph.json"
    dot_path = out / "graph.dot"
    assert json_path.is_file() and dot_path.is_file()

    data = json.loads(json_path.read_text())
    assert set(data) == {"meta", "nodes", "edges"}
    assert any(n["type"] == "eni" for n in data["nodes"])
    assert dot_path.read_text().startswith("digraph cloudbreachgraph {")


def test_from_cache_makes_no_live_calls(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("live AWS call made under --from-cache")

    monkeypatch.setattr(runner, "run_aws", _boom)
    rc = cli.main(["--from-cache", str(FIXTURES), "--output-dir", str(tmp_path)])
    assert rc == 0  # the swapped-in cache reader is used, not the live runner


# --------------------------------------------------------------------------- #
# Profile resolution: --account maps to a profile, --profile overrides it
# --------------------------------------------------------------------------- #
def test_account_resolves_mapped_profile(tmp_path, fake_aws):
    cfg = _write_config(tmp_path)
    rc = cli.main(
        [
            "--account",
            "workload_prod",
            "--config",
            cfg,
            "--no-verify-account",
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )
    assert rc == 0
    assert _profiles_for_describe(fake_aws.calls) == {"prod-audit"}


def test_account_by_id_resolves_mapped_profile(tmp_path, fake_aws):
    cfg = _write_config(tmp_path)
    rc = cli.main(
        [
            "--account",
            "333333333333",
            "--config",
            cfg,
            "--no-verify-account",
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )
    assert rc == 0
    assert _profiles_for_describe(fake_aws.calls) == {"sandbox-ro"}


def test_profile_overrides_mapping(tmp_path, fake_aws):
    cfg = _write_config(tmp_path)
    rc = cli.main(
        [
            "--account",
            "workload_prod",
            "--profile",
            "override-prof",
            "--config",
            cfg,
            "--no-verify-account",
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )
    assert rc == 0
    # The mapping ("prod-audit") is bypassed entirely by the --profile escape hatch.
    assert _profiles_for_describe(fake_aws.calls) == {"override-prof"}


# --------------------------------------------------------------------------- #
# Account verification
# --------------------------------------------------------------------------- #
def test_verify_on_by_default_catches_mismatch(tmp_path, fake_aws, capsys):
    cfg = _write_config(tmp_path)
    fake_aws.state["sts_account"] = "999999999999"  # profile points at the wrong account
    rc = cli.main(["--account", "workload_prod", "--config", cfg, "--output-dir", str(tmp_path)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "account mismatch" in err


def test_verify_default_runs_sts_when_account_known(tmp_path, fake_aws):
    cfg = _write_config(tmp_path)
    rc = cli.main(["--account", "workload_prod", "--config", cfg, "--output-dir", str(tmp_path)])
    assert rc == 0
    assert any(tuple(c["args"][:2]) == ("sts", "get-caller-identity") for c in fake_aws.calls)


# --------------------------------------------------------------------------- #
# Render degradation & --all-accounts
# --------------------------------------------------------------------------- #
def test_render_without_dot_degrades_gracefully(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dot_export.shutil, "which", lambda _: None)
    out = tmp_path / "out"
    rc = cli.main(["--from-cache", str(FIXTURES), "--output-dir", str(out), "--render", "png"])
    assert rc == 0
    assert (out / "graph.dot").is_file()
    assert not (out / "graph.png").exists()
    assert "`dot` not found" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# --html (opt-in interactive output) + size fallback to .dot
# --------------------------------------------------------------------------- #
def test_html_not_written_by_default(tmp_path):
    out = tmp_path / "out"
    rc = cli.main(["--from-cache", str(FIXTURES), "--output-dir", str(out)])
    assert rc == 0
    assert not (out / "graph.html").exists()  # opt-in only


def test_html_flag_writes_self_contained_page(tmp_path):
    out = tmp_path / "out"
    rc = cli.main(["--from-cache", str(FIXTURES), "--output-dir", str(out), "--html"])
    assert rc == 0
    html = out / "graph.html"
    assert html.is_file()
    text = html.read_text()
    assert text.startswith("<!DOCTYPE html>")
    assert "const GRAPH =" in text
    # The defaults (json + dot) are still produced alongside the HTML.
    assert (out / "graph.json").is_file()
    assert (out / "graph.dot").is_file()


def test_html_falls_back_to_dot_when_too_large(tmp_path, monkeypatch, capsys):
    # Force the "too big" branch by capping the node budget to zero.
    monkeypatch.setattr(html_export, "MAX_NODES", 0)
    out = tmp_path / "out"
    rc = cli.main(["--from-cache", str(FIXTURES), "--output-dir", str(out), "--html"])
    assert rc == 0
    assert not (out / "graph.html").exists()  # skipped
    assert (out / "graph.dot").is_file()  # fallback is the always-written .dot
    err = capsys.readouterr().err
    assert "too large" in err and "graph.dot" in err


def test_html_optimize_passes_writes_overlap_free_layout(tmp_path):
    # --html --optimize-passes N renders the deterministic overlap-free layout, not the force one.
    out = tmp_path / "out"
    rc = cli.main(
        [
            "--from-cache",
            str(FIXTURES),
            "--output-dir",
            str(out),
            "--html",
            "--optimize-passes",
            "500",
        ]
    )
    assert rc == 0
    text = (out / "graph.html").read_text()
    assert "overlap-free" in text  # variant marker, absent from the force layout
    assert "const GRAPH =" in text


def test_html_ringed_writes_ringed_layout(tmp_path):
    # --html --ringed renders the concentric-ringed layout, not the force or overlap-free one.
    out = tmp_path / "out"
    rc = cli.main(["--from-cache", str(FIXTURES), "--output-dir", str(out), "--html", "--ringed"])
    assert rc == 0
    text = (out / "graph.html").read_text()
    assert "· ringed" in text  # ringed HUD badge
    assert "overlap-free" not in text


def test_optimize_passes_without_html_warns_and_is_ignored(tmp_path, capsys):
    out = tmp_path / "out"
    rc = cli.main(
        ["--from-cache", str(FIXTURES), "--output-dir", str(out), "--optimize-passes", "50"]
    )
    assert rc == 0
    assert not (out / "graph.html").exists()  # --html not given, so no HTML at all
    assert "only affects --html" in capsys.readouterr().err


def test_ringed_without_html_warns_and_is_ignored(tmp_path, capsys):
    out = tmp_path / "out"
    rc = cli.main(["--from-cache", str(FIXTURES), "--output-dir", str(out), "--ringed"])
    assert rc == 0
    assert not (out / "graph.html").exists()
    assert "--ringed only affects --html" in capsys.readouterr().err


def test_optimize_passes_negative_errors(tmp_path):
    out = tmp_path / "out"
    rc = cli.main(
        ["--from-cache", str(FIXTURES), "--output-dir", str(out), "--optimize-passes", "-1"]
    )
    assert rc == 2


def test_all_accounts_writes_one_graph_each(tmp_path, fake_aws):
    cfg = _write_config(tmp_path)
    out = tmp_path / "out"
    rc = cli.main(
        ["--all-accounts", "--config", cfg, "--no-verify-account", "--output-dir", str(out)]
    )
    assert rc == 0
    assert (out / "graph.workload_prod.json").is_file()
    assert (out / "graph.workload_prod.dot").is_file()
    assert (out / "graph.workload_sandbox.json").is_file()
    assert (out / "graph.workload_sandbox.dot").is_file()
