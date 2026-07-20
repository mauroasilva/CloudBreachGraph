"""Shared test helpers. Tests run fully offline: the AWS CLI is never invoked."""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
# Repo-root docs example, parsed by a test so the shipped sample never drifts.
EXAMPLE_TOML = (
    Path(__file__).resolve().parents[1] / "docs" / "examples" / "cloudbreachgraph.example.toml"
)


def load_fixture(name: str) -> dict:
    """Load a recorded AWS CLI JSON response from ``tests/fixtures/``."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))
