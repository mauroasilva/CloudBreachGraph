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


# --------------------------------------------------------------------------- #
# download_object: the AwsCliError now names the full command (dest included)
# --------------------------------------------------------------------------- #
def test_download_object_error_includes_dest(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(1, stderr="err (NoSuchKey)"))
    dest = tmp_path / "obj.gz"
    with pytest.raises(runner.AwsCliError) as excinfo:
        runner.download_object(["s3api", "get-object", "--bucket=b", "--key=k"], dest)
    err = excinfo.value
    # The dest positional is part of the recorded command (the fixed bug), so the message names it.
    assert str(dest) in " ".join(err.args_run)
    assert str(dest) in str(err)


def test_download_object_caches_and_reuses_with_cache_dir(monkeypatch, tmp_path):
    from pathlib import Path

    calls = {"n": 0}

    def fake_run(cmd, *a, **k):
        calls["n"] += 1
        outfile = cmd[cmd.index("--no-cli-pager") - 1]  # the get-object output-file positional
        Path(outfile).write_bytes(b"OBJECT-BODY")
        return _FakeProc(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    args = ["s3api", "get-object", "--bucket=b", "--key=k"]
    try:
        runner.configure_cache(tmp_path / "cache")
        first = tmp_path / "first.gz"
        runner.download_object(args, first)
        assert calls["n"] == 1 and first.read_bytes() == b"OBJECT-BODY"

        # Same bucket/key again -> served from cache, no second aws call.
        second = tmp_path / "second.gz"
        runner.download_object(args, second)
        assert calls["n"] == 1  # unchanged: cache hit
        assert second.read_bytes() == b"OBJECT-BODY"
    finally:
        runner.configure_cache(None)


def test_download_object_no_cache_without_cache_dir(monkeypatch, tmp_path):
    from pathlib import Path

    calls = {"n": 0}

    def fake_run(cmd, *a, **k):
        calls["n"] += 1
        Path(cmd[cmd.index("--no-cli-pager") - 1]).write_bytes(b"BODY")
        return _FakeProc(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner.configure_cache(None)  # no cache dir -> every call downloads
    args = ["s3api", "get-object", "--bucket=b", "--key=k"]
    runner.download_object(args, tmp_path / "a.gz")
    runner.download_object(args, tmp_path / "b.gz")
    assert calls["n"] == 2


# --------------------------------------------------------------------------- #
# --verbose command echo (stderr only; stdout/graph files stay clean)
# --------------------------------------------------------------------------- #
def test_verbose_echoes_command_and_result(monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(0, stdout="{}"))
    monkeypatch.setattr(runner, "_verbose", True)
    runner.run_aws(["ec2", "describe-vpcs"], profile="prod")
    err = capsys.readouterr().err
    assert "+ aws ec2 describe-vpcs" in err
    assert "--profile prod" in err
    assert "OK" in err


def test_not_verbose_is_silent(monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(0, stdout="{}"))
    monkeypatch.setattr(runner, "_verbose", False)
    runner.run_aws(["ec2", "describe-vpcs"])
    assert capsys.readouterr().err == ""


def test_verbose_echoes_not_ok_on_failure(monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(2, stderr="boom"))
    monkeypatch.setattr(runner, "_verbose", True)
    with pytest.raises(runner.AwsCliError):
        runner.run_aws(["ec2", "describe-vpcs"])
    assert "NOT OK" in capsys.readouterr().err


def test_set_verbose_toggles_module_flag():
    runner.set_verbose(True)
    assert runner.is_verbose() is True
    runner.set_verbose(False)
    assert runner.is_verbose() is False


# --------------------------------------------------------------------------- #
# sso_login: interactive (NOT captured), the sole non-read command
# --------------------------------------------------------------------------- #
def test_sso_login_runs_interactive_uncaptured(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner.sso_login("prod-audit")
    assert captured["cmd"] == ["aws", "sso", "login", "--profile", "prod-audit"]
    # Interactive: stdio is inherited, so capture_output/text are NOT passed (browser prompt).
    assert "capture_output" not in captured["kwargs"]


def test_sso_login_raises_on_failure(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **k: _FakeProc(1))
    with pytest.raises(runner.AwsCliError):
        runner.sso_login("not-an-sso-profile")
