"""Shared fixtures.

``tests/fixtures/packets_000.jsonl`` holds 23 real packets copied from the mondo-ai
merged store (``packet_store_merged``), chosen to cover every predicate/source pair
present there, the multi-source packets, and every packet for MONDO:0008814.
"""

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = Path(__file__).parent.parent / "config"


@pytest.fixture
def store_dir() -> Path:
    """Directory laid out like a mondo-ai JSONL packet store."""
    return FIXTURES


@pytest.fixture
def raw_packets() -> list[dict[str, Any]]:
    """The fixture packets as parsed JSON."""
    with open(FIXTURES / "packets_000.jsonl") as f:
        return [json.loads(line) for line in f]


@pytest.fixture
def raw_by_id(raw_packets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Fixture packets keyed by packet id."""
    return {p["id"]: p for p in raw_packets}


@pytest.fixture
def config_dir() -> Path:
    """The repository's config directory (score rule + registries)."""
    return CONFIG
