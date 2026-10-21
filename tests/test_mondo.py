"""Mondo membership: is a statement already asserted in the Mondo release?

``tests/fixtures/mondo_subset.obo`` holds the real stanzas, from Mondo release
2026-10-06, of every term the packet fixture is about.
"""

from pathlib import Path

import pytest

from mondo_evidence_browser.mondo import MondoRelease, normalise_text

FIXTURE = Path(__file__).parent / "fixtures" / "mondo_subset.obo"
EXACT = "oio:hasExactSynonym"


@pytest.fixture(scope="module")
def mondo() -> MondoRelease:
    return MondoRelease.from_obo(FIXTURE)


def test_release_metadata(mondo: MondoRelease) -> None:
    assert mondo.version == "releases/2026-10-06"
    assert len(mondo.terms) == 14
    term = mondo.terms["MONDO:0008814"]
    assert term.name == "arginase deficiency"
    assert ("hyperargininemia", "EXACT") in term.synonyms
    assert "MONDO:0800153" in term.parents


def test_synonym_with_same_text_and_scope_is_in_mondo(mondo: MondoRelease) -> None:
    assert mondo.contains("MONDO:0008814", EXACT, "hyperargininemia")
    # Case and spacing differences do not make a new synonym.
    assert mondo.contains("MONDO:0008814", EXACT, "Arginase  Deficiency")


def test_different_scope_or_text_is_a_candidate(mondo: MondoRelease) -> None:
    assert not mondo.contains("MONDO:0008814", "oio:hasRelatedSynonym", "hyperargininemia")
    assert not mondo.contains("MONDO:0008814", EXACT, "Arginase 1 deficiency")
    assert not mondo.contains("MONDO:9999999", EXACT, "anything")


def test_label_counts_as_an_exact_synonym(mondo: MondoRelease) -> None:
    name = mondo.terms["MONDO:0008814"].name
    assert name is not None
    assert mondo.contains("MONDO:0008814", EXACT, name)
    assert not mondo.contains("MONDO:0008814", "oio:hasNarrowSynonym", name)


def test_subclass_statements(mondo: MondoRelease) -> None:
    assert mondo.contains("MONDO:0008814", "rdfs:subClassOf", "MONDO:0800153")
    assert not mondo.contains("MONDO:0008814", "rdfs:subClassOf", "MONDO:0000001")


def test_normalise_text() -> None:
    assert normalise_text("  Arginase   Deficiency ") == "arginase deficiency"


def test_membership_says_when_it_could_not_check(mondo: MondoRelease) -> None:
    assert mondo.membership("MONDO:0008814", EXACT, "hyperargininemia") == "IN_MONDO"
    assert mondo.membership("MONDO:0008814", EXACT, "Arginase 1 deficiency") == "CANDIDATE"
    # A claim type the release comparison does not cover is not guessed at.
    assert mondo.membership("MONDO:0008814", "RO:0004003", "HGNC:663") == "NOT_CHECKED"
    assert mondo.membership("MONDO:0008814", "skos:exactMatch", "OMIM:207800") == "NOT_CHECKED"
