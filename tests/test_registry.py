"""Resource and agent registries: dated quality/trust levels and dependence."""

from datetime import date
from pathlib import Path

from mondo_evidence_browser.registry import Registry, normalise_orcid


def test_shipped_registry_loads(config_dir: Path) -> None:
    registry = Registry.from_dir(config_dir)
    assert registry.resource_quality("omim", date(2026, 10, 20)) == 0.95
    assert registry.resource_quality("not-a-resource", date(2026, 10, 20)) is None
    assert "omim" in registry.derived_from("doid")


def test_levels_are_taken_as_of_a_date() -> None:
    registry = Registry.from_dicts(
        resources={},
        agents={
            "ORCID:0000-0002-1825-0097": {
                "name": "Josiah Carberry",
                "kind": "person",
                "can_sign_off": True,
                "trust": [
                    {"value": 0.5, "since": "2025-01-01"},
                    {"value": 0.85, "since": "2026-06-01"},
                ],
            }
        },
    )
    orcid = "ORCID:0000-0002-1825-0097"
    assert registry.agent_trust(orcid, date(2024, 12, 31)) is None
    assert registry.agent_trust(orcid, date(2026, 5, 31)) == 0.5
    assert registry.agent_trust(orcid, date(2026, 6, 1)) == 0.85
    agent = registry.agent(orcid)
    assert agent is not None and agent.can_sign_off and agent.name == "Josiah Carberry"


def test_scalar_level_applies_from_the_start() -> None:
    registry = Registry.from_dicts(resources={"x": {"name": "X", "quality": 0.4}}, agents={})
    assert registry.resource_quality("x", date(1900, 1, 1)) == 0.4


def test_aliases_resolve_to_the_resource() -> None:
    registry = Registry.from_dicts(
        resources={"wikipedia": {"quality": 0.4, "aliases": ["en.wikipedia.org"]}}, agents={}
    )
    assert registry.resource_quality("en.wikipedia.org", date(2026, 1, 1)) == 0.4
    assert registry.resource_id("en.wikipedia.org") == "wikipedia"
    assert registry.resource_id("example.org") == "example.org"


def test_dependence_is_transitive() -> None:
    registry = Registry.from_dicts(
        resources={
            "a": {"quality": 1.0},
            "b": {"quality": 1.0, "derived_from": ["a"]},
            "c": {"quality": 1.0, "derived_from": ["b"]},
        },
        agents={},
    )
    assert registry.derived_from("c") == {"a", "b"}


def test_normalise_orcid() -> None:
    assert normalise_orcid("0000-0002-1825-0097") == "ORCID:0000-0002-1825-0097"
    assert normalise_orcid("https://orcid.org/0000-0002-1825-0097") == "ORCID:0000-0002-1825-0097"
    assert normalise_orcid("ORCID:0000-0002-1825-0097") == "ORCID:0000-0002-1825-0097"
