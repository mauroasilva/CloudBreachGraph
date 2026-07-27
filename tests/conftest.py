"""Shared test helpers. Tests run fully offline: the AWS CLI is never invoked."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cloudbreachgraph.aws import collectors

FIXTURES = Path(__file__).parent / "fixtures"
# Repo-root docs example, parsed by a test so the shipped sample never drifts.
EXAMPLE_TOML = (
    Path(__file__).resolve().parents[1] / "docs" / "examples" / "cloudbreachgraph.example.toml"
)


def load_fixture(name: str) -> dict:
    """Load a recorded AWS CLI JSON response from ``tests/fixtures/``."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _reset_collector_state():
    """Reset the collectors' module-level knobs (flow-log window, historical toggle) around every
    test, so a test that sets ``--flow-log-days``/``--no-historical-enis`` via the CLI can't leak
    that state into another test (they mirror the ``configure_cache`` global pattern)."""
    collectors.set_flow_log_window(collectors.FLOW_LOG_MAX_LOOKBACK_DAYS)
    collectors.set_historical_enis(True)
    yield
    collectors.set_flow_log_window(collectors.FLOW_LOG_MAX_LOOKBACK_DAYS)
    collectors.set_historical_enis(True)
