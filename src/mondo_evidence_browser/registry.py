"""Resource and agent registries.

Resources carry a quality level (D-060) and the resources they derive from (D-046);
agents carry a role and trust level (D-049, D-052). Levels are dated: a value applies
from its ``since`` date, so a later re-rating does not change a score computed or a
sign-off given earlier.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

ORCID_PATTERN = re.compile(r"(\d{4}-\d{4}-\d{4}-\d{3}[\dX])")


def normalise_orcid(value: str) -> str:
    """Return an ORCID as ``ORCID:0000-0000-0000-0000``.

    >>> normalise_orcid("https://orcid.org/0000-0002-1825-0097")
    'ORCID:0000-0002-1825-0097'
    """
    match = ORCID_PATTERN.search(value)
    if match is None:
        raise ValueError(f"not an ORCID: {value!r}")
    return f"ORCID:{match.group(1)}"


@dataclass(frozen=True)
class DatedLevel:
    """A level (quality or trust) that applies from ``since`` onwards."""

    value: float
    since: date


def _levels(spec: Any) -> tuple[DatedLevel, ...]:
    """Parse ``0.4`` or ``[{value: 0.4, since: 2026-01-01}, ...]``.

    >>> _levels(0.4)
    (DatedLevel(value=0.4, since=datetime.date(1, 1, 1)),)
    """
    if spec is None:
        return ()
    if isinstance(spec, int | float):
        return (DatedLevel(float(spec), date.min),)
    levels = [
        DatedLevel(
            float(entry["value"]),
            entry["since"]
            if isinstance(entry["since"], date)
            else date.fromisoformat(str(entry["since"])),
        )
        for entry in spec
    ]
    return tuple(sorted(levels, key=lambda level: level.since))


def level_as_of(levels: tuple[DatedLevel, ...], when: date) -> float | None:
    """Return the level in force on ``when``, or None before the first one.

    >>> lv = _levels([{"value": 0.5, "since": "2025-01-01"}, {"value": 0.9, "since": "2026-01-01"}])
    >>> level_as_of(lv, date(2025, 6, 1)), level_as_of(lv, date(2026, 6, 1))
    (0.5, 0.9)
    """
    current: float | None = None
    for level in levels:
        if level.since <= when:
            current = level.value
    return current


@dataclass(frozen=True)
class Resource:
    """An information resource such as OMIM or Wikipedia."""

    id: str
    name: str
    quality: tuple[DatedLevel, ...]
    derived_from: tuple[str, ...]
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class Agent:
    """A person (ORCID) or organisation whose judgement can count as evidence."""

    id: str
    name: str
    kind: str
    role: str | None
    trust: tuple[DatedLevel, ...]
    can_sign_off: bool


class Registry:
    """Lookup of resources and agents, see ``config/resources.yaml`` / ``agents.yaml``."""

    def __init__(self, resources: Mapping[str, Resource], agents: Mapping[str, Agent]) -> None:
        """Index resources (including their aliases) and agents."""
        self.resources = dict(resources)
        self.agents = dict(agents)
        self._alias = {alias: r.id for r in resources.values() for alias in r.aliases}

    @classmethod
    def from_dicts(
        cls, resources: Mapping[str, Mapping[str, Any]], agents: Mapping[str, Mapping[str, Any]]
    ) -> Registry:
        """Build a registry from parsed YAML mappings."""
        return cls(
            {
                rid: Resource(
                    id=rid,
                    name=spec.get("name", rid),
                    quality=_levels(spec.get("quality")),
                    derived_from=tuple(spec.get("derived_from", ())),
                    aliases=tuple(spec.get("aliases", ())),
                )
                for rid, spec in resources.items()
            },
            {
                aid: Agent(
                    id=aid,
                    name=spec.get("name", aid),
                    kind=spec.get("kind", "person"),
                    role=spec.get("role"),
                    trust=_levels(spec.get("trust")),
                    can_sign_off=bool(spec.get("can_sign_off", False)),
                )
                for aid, spec in agents.items()
            },
        )

    @classmethod
    def from_dir(cls, config_dir: Path) -> Registry:
        """Load ``resources.yaml`` and ``agents.yaml`` from a config directory."""
        resources = yaml.safe_load((config_dir / "resources.yaml").read_text())
        agents = yaml.safe_load((config_dir / "agents.yaml").read_text())
        return cls.from_dicts(resources["resources"], agents["agents"])

    def resource_id(self, source: str) -> str:
        """Resolve an alias (e.g. a web host) to its resource id; unknown ids pass through."""
        return self._alias.get(source, source)

    def resource_quality(self, source: str, when: date) -> float | None:
        """Quality of a resource on a date, or None if the resource is not registered."""
        resource = self.resources.get(self.resource_id(source))
        return level_as_of(resource.quality, when) if resource else None

    def derived_from(self, source: str) -> set[str]:
        """All resources a resource derives from, transitively."""
        seen: set[str] = set()
        todo = [self.resource_id(source)]
        while todo:
            resource = self.resources.get(todo.pop())
            for parent in resource.derived_from if resource else ():
                if parent not in seen:
                    seen.add(parent)
                    todo.append(parent)
        return seen

    def agent(self, agent_id: str) -> Agent | None:
        """The registered agent with this id, if any."""
        return self.agents.get(agent_id)

    def agent_trust(self, agent_id: str, when: date) -> float | None:
        """Trust of an agent on a date, or None if unregistered / not yet rated."""
        agent = self.agents.get(agent_id)
        return level_as_of(agent.trust, when) if agent else None
