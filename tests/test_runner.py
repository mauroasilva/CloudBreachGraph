"""Tests for the AWS CLI subprocess runner. The subprocess boundary is mocked."""

from __future__ import annotations

import json
import subprocess

import pytest

from cloudbreachgraph.aws import runner


class _FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_aws_appends_json_and_pager_flags(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, capture_output, text):
        captured["cmd"] = cmd
        return _FakeProc(0, stdout=json.dumps({"ok": True}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = runner.run_aws(["ec2", "describe-vpcs"])

    assert result == {"ok": True}
    assert captured["cmd"][:3] == ["aws", "ec2", "describe-vpcs"]
    assert "--output" in captured["cmd"] and "json" in captured["cmd"]
    assert "--no-cli-pager" in captured["cmd"]
    # No profile/region requested -> those flags are absent.
    assert "--profile" not in captured["cmd"]
    assert "--region" not in captured["cmd"]


def test_run_aws_threads_profile_and_region(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, capture_output, text):
        captured["cmd"] = cmd
        return _FakeProc(0, stdout="{}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner.run_aws(["ec2", "describe-subnets"], profile="prod-audit", region="eu-west-1")

    cmd = captured["cmd"]
    assert cmd[cmd.index("--region") + 1] == "eu-west-1"
    assert cmd[cmd.index("--profile") + 1] == "prod-audit"


def test_run_aws_raises_and_surfaces_stderr(monkeypatch):
    def fake_run(cmd, capture_output, text):
        return _FakeProc(255, stdout="", stderr="An error occurred (ExpiredToken)")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(runner.AwsCliError) as excinfo:
        runner.run_aws(["ec2", "describe-instances"], profile="stale")

    err = excinfo.value
    assert err.returncode == 255
    assert "ExpiredToken" in str(err)
    assert "ExpiredToken" in err.stderr


def test_run_aws_empty_stdout_returns_empty_dict(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(0, stdout="   "))
    assert runner.run_aws(["elb", "describe-load-balancers"]) == {}


def test_run_aws_bad_json_raises(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(0, stdout="not json{"))
    with pytest.raises(runner.AwsCliError):
        runner.run_aws(["ec2", "describe-vpcs"])


def test_run_aws_writes_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(0, stdout='{"Vpcs": []}'))
    runner.run_aws(["ec2", "describe-vpcs"], cache_dir=tmp_path)
    cached = tmp_path / "ec2-describe-vpcs.json"
    assert cached.is_file()
    assert json.loads(cached.read_text()) == {"Vpcs": []}
