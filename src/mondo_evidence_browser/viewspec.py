"""Derive the browser's view spec from the LinkML schema.

The view spec is the only thing the front end and the index builder read: tabs, table
columns (with their sort and filter hints), the default order, the shape of every
class for the generic detail view, the enums, and the sign-off form. It is computed
from ``schema/browser.yaml`` and its ``browser_*`` annotations (documented at the top
of that file), so a schema change means regenerating, never editing UI code.

Mistakes in the annotations raise :class:`SpecError` instead of silently dropping a
column: a misspelled ``browser_*`` key, a column on a class-valued slot without
``browser_field``, a tab slot that is not an enum, two default sorts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jsonasobj2 import as_dict
from linkml_runtime import SchemaView
from linkml_runtime.linkml_model.meta import ClassDefinition, SlotDefinition

SCHEMA_PATH = Path(__file__).parent / "schema" / "browser.yaml"

SLOT_ANNOTATIONS = {
    "browser_key",
    "browser_tab",
    "browser_column",
    "browser_preview",
    "browser_sortable",
    "browser_default_sort",
    "browser_search",
    "browser_filter",
    "browser_field",
    "browser_format",
    "browser_layout",
    "browser_widget",
    "browser_status",
}
CLASS_ANNOTATIONS = {
    "browser_root",
    "browser_form",
    "browser_history",
    "browser_explain",
    "browser_title",
}

NUMBER_TYPES = {"float", "double", "decimal"}
CURIE_TYPES = {"uriorcurie", "uri", "curie"}
DATE_TYPES = {"date", "datetime"}


def _default_title(name: str) -> str:
    """A readable title for a slot the schema gives no title.

    >>> _default_title("has_evidence_items")
    'Has evidence items'
    """
    return name.replace("_", " ").capitalize()


class SpecError(ValueError):
    """The schema's browser annotations are inconsistent."""


def _annotations(element: SlotDefinition | ClassDefinition, allowed: set[str]) -> dict[str, Any]:
    """The ``browser_*`` annotations of a schema element, checked for typos."""
    found = {key: value["value"] for key, value in as_dict(element.annotations).items()}
    unknown = {k for k in found if k.startswith("browser_") and k not in allowed}
    if unknown:
        raise SpecError(
            f"{element.name}: unknown annotation(s) {', '.join(sorted(unknown))}; "
            f"expected one of {', '.join(sorted(allowed))}"
        )
    return {k: v for k, v in found.items() if k.startswith("browser_")}


class _Builder:
    def __init__(self, sv: SchemaView) -> None:
        self.sv = sv
        self.enums = set(sv.all_enums())
        self.classes = set(sv.all_classes())

    def kind(self, slot: SlotDefinition) -> str:
        """What kind of value a slot holds: class, enum, number, integer, ..."""
        rng = slot.range or self.sv.schema.default_range or "string"
        if rng in self.classes:
            return "class"
        if rng in self.enums:
            return "enum"
        if rng in self.sv.all_types():
            base = self.sv.type_ancestors(rng)
            names = set(base) | {rng}
            if names & NUMBER_TYPES:
                return "number"
            if "integer" in names:
                return "integer"
            if "boolean" in names:
                return "boolean"
            if names & DATE_TYPES:
                return "date"
            if names & CURIE_TYPES:
                return "curie"
        return "string"

    def slot_entry(self, slot: SlotDefinition) -> dict[str, Any]:
        ann = _annotations(slot, SLOT_ANNOTATIONS)
        kind = self.kind(slot)
        return {
            "name": slot.name,
            "title": slot.title or _default_title(slot.name),
            "description": slot.description,
            "kind": kind,
            "range": slot.range,
            "class": slot.range if kind == "class" else None,
            "enum": slot.range if kind == "enum" else None,
            "multivalued": bool(slot.multivalued),
            "required": bool(slot.required),
            "layout": ann.get("browser_layout"),
            "format": ann.get("browser_format"),
        }

    def class_entry(self, name: str) -> dict[str, Any]:
        cls = self.sv.get_class(name)
        _annotations(cls, CLASS_ANNOTATIONS)
        return {
            "name": name,
            "title": cls.title or name,
            "description": cls.description,
            "ancestors": list(self.sv.class_ancestors(name))[1:],
            "slots": [self.slot_entry(s) for s in self.sv.class_induced_slots(name)],
        }

    def columns(self, cls: str, path: list[str], seen: tuple[str, ...]) -> list[dict[str, Any]]:
        """Annotated columns reachable through single-valued, class-valued slots."""
        out: list[dict[str, Any]] = []
        for slot in self.sv.class_induced_slots(cls):
            ann = _annotations(slot, SLOT_ANNOTATIONS)
            kind = self.kind(slot)
            here = [*path, slot.name]
            if "browser_column" in ann:
                out.append(self.column(slot, ann, kind, here))
            elif kind == "class" and not slot.multivalued and slot.range not in seen:
                out.extend(self.columns(slot.range, here, (*seen, slot.range)))
        return out

    def previews(self, cls: str, path: list[str], seen: tuple[str, ...]) -> list[dict[str, Any]]:
        """Slots annotated browser_preview, found like columns; shown on row expand."""
        out: list[dict[str, Any]] = []
        for slot in self.sv.class_induced_slots(cls):
            ann = _annotations(slot, SLOT_ANNOTATIONS)
            here = [*path, slot.name]
            if "browser_preview" in ann:
                entry = self.slot_entry(slot)
                out.append(
                    {
                        **entry,
                        "name": "__".join(here),
                        "path": here,
                        "order": float(ann["browser_preview"]),
                    }
                )
            elif self.kind(slot) == "class" and not slot.multivalued and slot.range not in seen:
                out.extend(self.previews(slot.range, here, (*seen, slot.range)))
        return out

    def column(
        self, slot: SlotDefinition, ann: dict[str, Any], kind: str, path: list[str]
    ) -> dict[str, Any]:
        enum = slot.range if kind == "enum" else None
        if kind == "class":
            field = ann.get("browser_field")
            if field is None:
                raise SpecError(
                    f"column {'.'.join(path)} has class range {slot.range}: "
                    "add browser_field to say which of its slots to show"
                )
            inner = {s.name: s for s in self.sv.class_induced_slots(slot.range)}
            if field not in inner:
                raise SpecError(f"browser_field {field!r} is not a slot of {slot.range}")
            kind = self.kind(inner[field])
            enum = inner[field].range if kind == "enum" else None
            path = [*path, field]
        if slot.multivalued and ann.get("browser_sortable"):
            raise SpecError(f"column {'.'.join(path)} is multivalued and cannot be sortable")
        return {
            "name": "__".join(path),
            "path": path,
            "order": float(ann["browser_column"]),
            "title": slot.title or _default_title(slot.name),
            "description": slot.description,
            "kind": kind,
            "enum": enum,
            "multivalued": bool(slot.multivalued),
            "format": ann.get("browser_format"),
            "sortable": bool(ann.get("browser_sortable", False)),
            "search": bool(ann.get("browser_search", False)),
            "filter": bool(ann.get("browser_filter", False)),
            "default_sort": ann.get("browser_default_sort"),
        }


def _marked_classes(sv: SchemaView) -> tuple[str, str | None, str | None, str | None]:
    """The root, form, history and explained classes."""
    marked: dict[str, list[str]] = {
        "browser_root": [],
        "browser_form": [],
        "browser_history": [],
        "browser_explain": [],
    }
    for name in sv.all_classes():
        ann = _annotations(sv.get_class(name), CLASS_ANNOTATIONS)
        for key, names in marked.items():
            if ann.get(key):
                names.append(name)
    roots = marked["browser_root"]
    if len(roots) != 1:
        raise SpecError(f"exactly one class needs browser_root: true, found {roots or 'none'}")
    for key in ("browser_form", "browser_history", "browser_explain"):
        if len(marked[key]) > 1:
            raise SpecError(f"at most one class may have {key}: true, found {marked[key]}")
    form, history, explain = (
        marked[k][0] if marked[k] else None
        for k in ("browser_form", "browser_history", "browser_explain")
    )
    return roots[0], form, history, explain


def _check_slot_usage(sv: SchemaView) -> None:
    """Every slot_usage in the browser schema must refine a slot its parents have.

    Otherwise an upstream rename (say subject_label -> subject_name) would leave
    the annotation on a slot nothing fills, and the column would silently be empty.
    """
    for name in sv.all_classes(imports=False):
        cls = sv.get_class(name)
        parents = [p for p in [cls.is_a, *cls.mixins] if p]
        inherited = {s.name for p in parents for s in sv.class_induced_slots(p)}
        own = set(cls.attributes or {}) | set(cls.slots or [])
        for slot in cls.slot_usage or {}:
            if slot not in inherited and slot not in own:
                raise SpecError(
                    f"{name} refines slot {slot!r}, but {', '.join(parents) or 'it'} has no such "
                    "slot: was it renamed or removed in the evidence schema?"
                )


def _enum_values(sv: SchemaView, name: str) -> list[dict[str, Any]]:
    enum = sv.get_enum(name)
    return [
        {"value": value, "title": pv.title or value, "description": pv.description}
        for value, pv in (enum.permissible_values or {}).items()
    ]


def build_view_spec(schema_path: Path = SCHEMA_PATH) -> dict[str, Any]:
    """Compute the view spec for a browser schema.

    >>> spec = build_view_spec()
    >>> spec["default_sort"]
    {'column': 'inclusion_score__value', 'direction': 'desc'}
    """
    sv = SchemaView(str(schema_path))
    _check_slot_usage(sv)
    builder = _Builder(sv)
    root, form_class, history_class, explain_class = _marked_classes(sv)

    keys, tabs, statuses = [], [], []
    for slot in sv.class_induced_slots(root):
        ann = _annotations(slot, SLOT_ANNOTATIONS)
        if ann.get("browser_key"):
            keys.append(slot.name)
        if ann.get("browser_status"):
            statuses.append(slot.name)
        if ann.get("browser_tab"):
            if builder.kind(slot) != "enum":
                raise SpecError(f"tab slot {slot.name} must have an enum range, not {slot.range}")
            tabs.append(slot)
    if len(keys) != 1:
        raise SpecError(f"exactly one slot of {root} needs browser_key: true, found {keys}")
    if len(tabs) > 1:
        raise SpecError(f"at most one tab slot, found {[t.name for t in tabs]}")
    if len(statuses) > 1:
        raise SpecError(f"at most one browser_status slot, found {statuses}")

    columns = sorted(builder.columns(root, [], (root,)), key=lambda c: c["order"])
    if not columns:
        raise SpecError("no slot has browser_column: the table would be empty")
    sorts = [c for c in columns if c["default_sort"]]
    if len(sorts) > 1:
        raise SpecError(f"only one default sort allowed, found {[c['name'] for c in sorts]}")
    for column in sorts:
        if column["default_sort"] not in ("asc", "desc"):
            raise SpecError(f"browser_default_sort must be asc or desc on {column['name']}")
        if not column["sortable"]:
            raise SpecError(f"default sort column {column['name']} must be browser_sortable")
    default_sort = (
        {"column": sorts[0]["name"], "direction": sorts[0]["default_sort"]} if sorts else None
    )
    # The review status column, which the list updates live from recent sign-offs.
    if statuses and statuses[0] not in [c["name"] for c in columns]:
        raise SpecError(f"browser_status slot {statuses[0]} must also be a browser_column")

    form = None
    if form_class is not None:
        fields = []
        for slot in sv.class_induced_slots(form_class):
            entry = builder.slot_entry(slot)
            ann = _annotations(slot, SLOT_ANNOTATIONS)
            widget = ann.get("browser_widget") or ("select" if entry["kind"] == "enum" else "text")
            fields.append(
                {
                    **entry,
                    "widget": widget,
                    "options": _enum_values(sv, slot.range) if entry["kind"] == "enum" else None,
                }
            )
        form = {"class": form_class, "fields": fields}

    tab = tabs[0] if tabs else None
    return {
        "schema": {
            "id": sv.schema.id,
            "name": sv.schema.name,
            "version": sv.schema.version,
            "title": sv.schema.title,
        },
        "root": root,
        "key": keys[0],
        "title_template": _annotations(sv.get_class(root), CLASS_ANNOTATIONS).get("browser_title"),
        "tabs": {"slot": tab.name, "values": _enum_values(sv, tab.range)} if tab else None,
        "columns": columns,
        "preview": sorted(builder.previews(root, [], (root,)), key=lambda p: p["order"]),
        "default_sort": default_sort,
        "classes": {name: builder.class_entry(name) for name in sorted(sv.all_classes())},
        "enums": {name: _enum_values(sv, name) for name in sorted(sv.all_enums())},
        "form": form,
        "history": history_class,
        "status": statuses[0] if statuses else None,
        "explain": explain_class,
    }
