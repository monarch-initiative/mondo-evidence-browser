"""Which statements are already in Mondo, read from a Mondo release (``mondo.obo``).

This decides the browser's two tabs: evidence for statements already in Mondo, and
for candidate statements not in Mondo yet. Supported predicates:

- synonyms (exact, related, narrow, broad): the term has a synonym with the same text
  and scope. Text is compared after case folding and whitespace collapsing; the label
  counts as an exact synonym.
- ``rdfs:subClassOf``: the term has that ``is_a`` parent.

Any other predicate is reported as not checked (``NOT_CHECKED``) rather than guessed:
add it to :data:`CHECKED_PREDICATES` with a comparison when the release can answer it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

SYNONYM = re.compile(r'^synonym: "((?:[^"\\]|\\.)*)" (EXACT|RELATED|NARROW|BROAD)\b')
SCOPES = {
    "oio:hasExactSynonym": "EXACT",
    "oio:hasRelatedSynonym": "RELATED",
    "oio:hasNarrowSynonym": "NARROW",
    "oio:hasBroadSynonym": "BROAD",
}


CHECKED_PREDICATES = frozenset([*SCOPES, "rdfs:subClassOf"])
"""Predicates :meth:`MondoRelease.membership` can check against a release."""


def normalise_text(text: str) -> str:
    """Case-fold and collapse whitespace, for comparing synonym text.

    >>> normalise_text("Hyper  Argininemia")
    'hyper argininemia'
    """
    return " ".join(text.split()).casefold()


@dataclass
class MondoTerm:
    """What the browser needs to know about one Mondo term."""

    id: str
    name: str | None = None
    synonyms: set[tuple[str, str]] = field(default_factory=set)
    """(normalised text, scope) pairs."""
    parents: set[str] = field(default_factory=set)
    obsolete: bool = False


@dataclass
class MondoRelease:
    """The terms of one Mondo release."""

    version: str | None
    terms: dict[str, MondoTerm]

    @classmethod
    def from_obo(cls, path: Path) -> MondoRelease:
        """Parse the term stanzas of an OBO file."""
        version: str | None = None
        terms: dict[str, MondoTerm] = {}
        term: MondoTerm | None = None
        in_term = False
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if line.startswith("["):
                    in_term = line == "[Term]"
                    term = None
                    continue
                if not in_term:
                    if version is None and line.startswith("data-version: "):
                        version = line.removeprefix("data-version: ").strip()
                    continue
                if line.startswith("id: "):
                    term = terms.setdefault(line[4:].strip(), MondoTerm(id=line[4:].strip()))
                elif term is None:
                    continue
                elif line.startswith("name: "):
                    term.name = line[6:].strip()
                elif line.startswith("synonym: "):
                    match = SYNONYM.match(line)
                    if match:
                        text = match.group(1).replace('\\"', '"')
                        term.synonyms.add((normalise_text(text), match.group(2)))
                elif line.startswith("is_a: "):
                    term.parents.add(line[6:].split()[0])
                elif line == "is_obsolete: true":
                    term.obsolete = True
        return cls(version=version, terms=terms)

    def contains(self, subject: str, predicate: str, obj: str) -> bool:
        """Whether the release asserts this statement."""
        term = self.terms.get(subject)
        if term is None:
            return False
        scope = SCOPES.get(predicate)
        if scope is not None:
            text = normalise_text(obj)
            if scope == "EXACT" and term.name and normalise_text(term.name) == text:
                return True
            return (text, scope) in term.synonyms
        if predicate == "rdfs:subClassOf":
            return obj in term.parents
        return False

    def membership(self, subject: str, predicate: str, obj: str) -> str:
        """IN_MONDO, CANDIDATE, or NOT_CHECKED when the release cannot answer it.

        >>> MondoRelease(version=None, terms={}).membership("MONDO:1", "RO:0004003", "x")
        'NOT_CHECKED'
        """
        if predicate not in CHECKED_PREDICATES:
            return "NOT_CHECKED"
        return "IN_MONDO" if self.contains(subject, predicate, obj) else "CANDIDATE"
