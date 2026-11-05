"""The view spec is derived from the browser schema's annotations, nothing else."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from mondo_evidence_browser.viewspec import SCHEMA_PATH, SpecError, build_view_spec


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return build_view_spec(SCHEMA_PATH)


def test_schema_identity(spec: dict[str, Any]) -> None:
    assert spec["schema"]["name"] == "mondo_evidence_browser"
    assert spec["schema"]["version"] == "0.1.0"
    assert spec["root"] == "BrowserRecord"
    assert spec["key"] == "id"
    assert spec["title_template"].startswith("{statement.subject_label}")


def test_tabs_come_from_the_tab_enum(spec: dict[str, Any]) -> None:
    assert spec["tabs"]["slot"] == "mondo_membership"
    assert [(t["value"], t["title"]) for t in spec["tabs"]["values"]] == [
        ("IN_MONDO", "In Mondo"),
        ("CANDIDATE", "Candidates"),
        ("NOT_CHECKED", "Not checked"),
    ]


def test_columns_follow_annotation_order(spec: dict[str, Any]) -> None:
    columns = [(c["name"], c["title"]) for c in spec["columns"]]
    assert columns == [
        ("inclusion_score__value", "Score"),
        ("statement__subject", "Mondo term"),
        ("statement__subject_label", "Term label"),
        ("statement__predicate__label", "Predicate"),
        ("statement__object_label", "Value"),
        ("claim_type", "Claim type"),
        ("sources", "Sources"),
        ("review_status", "Review status"),
    ]


def test_column_properties(spec: dict[str, Any]) -> None:
    by_name = {c["name"]: c for c in spec["columns"]}
    score = by_name["inclusion_score__value"]
    assert (score["kind"], score["format"], score["sortable"]) == ("number", "score", True)
    assert score["path"] == ["inclusion_score", "value"]
    predicate = by_name["statement__predicate__label"]
    assert predicate["path"] == ["statement", "predicate", "label"]
    assert predicate["filter"] and not predicate["sortable"]
    status = by_name["review_status"]
    assert status["kind"] == "enum" and status["enum"] == "ReviewStatus"
    assert by_name["sources"]["multivalued"] and by_name["sources"]["kind"] == "string"
    assert by_name["statement__subject"]["format"] == "curie"
    assert {c["name"] for c in spec["columns"] if c["search"]} == {
        "statement__subject",
        "statement__subject_label",
        "statement__object_label",
    }


def test_preview_items_follow_annotation_order(spec: dict[str, Any]) -> None:
    preview = spec["preview"]
    assert [(p["name"], p["title"]) for p in preview] == [
        ("inclusion_score__components", "Score components"),
        ("has_evidence_lines", "Evidence lines"),
    ]
    components = preview[0]
    assert components["path"] == ["inclusion_score", "components"]
    assert components["kind"] == "class" and components["class"] == "ScoreComponent"
    assert components["multivalued"] and components["layout"] == "table"


def test_score_explainer_and_descriptive_component_titles(spec: dict[str, Any]) -> None:
    assert spec["explain"] == "InclusionScore"
    assert "tanh" in spec["classes"]["InclusionScore"]["description"]
    slots = {s["name"]: s for s in spec["classes"]["ScoreComponent"]["slots"]}
    assert slots["direction"]["title"] == "Direction (d)"
    assert slots["strength"]["title"] == "Strength (s)"
    assert slots["source_prior"]["title"] == "Source prior (q)"
    assert slots["confidence"]["title"] == "Confidence (c)"
    assert slots["weight"]["title"] == "Weight (w)"
    assert all(s["description"] for s in slots.values()), "every component is explained"


def test_default_sort(spec: dict[str, Any]) -> None:
    assert spec["default_sort"] == {"column": "inclusion_score__value", "direction": "desc"}


def test_classes_cover_polymorphic_evidence_items(spec: dict[str, Any]) -> None:
    concordance = spec["classes"]["ConcordanceItem"]
    names = [s["name"] for s in concordance["slots"]]
    assert "source_subject" in names and "mapping_justification" in names
    assert "InformationEntity" in concordance["ancestors"]
    components = {s["name"]: s for s in spec["classes"]["InclusionScore"]["slots"]}["components"]
    assert components["kind"] == "class" and components["class"] == "ScoreComponent"
    assert components["multivalued"] and components["layout"] == "table"
    record = {s["name"]: s for s in spec["classes"]["BrowserRecord"]["slots"]}
    assert record["statement"]["class"] == "BrowserStatement"


def test_form_comes_from_the_form_class(spec: dict[str, Any]) -> None:
    form = spec["form"]
    assert form["class"] == "SignOff"
    fields = {f["name"]: f for f in form["fields"]}
    assert list(fields) == ["outcome", "rationale", "guideline", "perspective"]
    outcome = fields["outcome"]
    assert outcome["required"] and outcome["widget"] == "radio"
    assert [o["value"] for o in outcome["options"]] == [
        "accepted",
        "rejected",
        "controversial",
        "deferred",
    ]
    assert fields["rationale"]["widget"] == "textarea"
    assert fields["guideline"]["widget"] == "text" and not fields["guideline"]["required"]


def test_history_class(spec: dict[str, Any]) -> None:
    assert spec["history"] == "Decision"
    decision = spec["classes"]["Decision"]
    assert "SignOff" in decision["ancestors"]
    names = [s["name"] for s in decision["slots"]]
    assert {"outcome", "rationale", "curator", "packet_version", "score"} <= set(names)


def test_enums_are_exported(spec: dict[str, Any]) -> None:
    titles = {v["value"]: v["title"] for v in spec["enums"]["ReviewStatus"]}
    assert titles["stale"] == "Signed off on an older version"


# --- schema errors fail loudly -------------------------------------------------


def _schema_copy(tmp_path: Path) -> Path:
    """A copy of the schema directory, so tests can edit browser.yaml."""
    import shutil

    target = tmp_path / "schema"
    shutil.copytree(SCHEMA_PATH.parent, target)
    return target / "browser.yaml"


def _edit(path: Path, edit) -> Path:  # type: ignore[no-untyped-def]
    data = yaml.safe_load(path.read_text())
    edit(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def test_misspelled_annotation_is_an_error(tmp_path: Path) -> None:
    path = _edit(
        _schema_copy(tmp_path),
        lambda d: d["classes"]["BrowserStatement"]["slot_usage"]["subject"]["annotations"].update(
            {"browser_sortabel": True}
        ),
    )
    with pytest.raises(SpecError, match="browser_sortabel"):
        build_view_spec(path)


def test_class_valued_column_needs_a_field(tmp_path: Path) -> None:
    path = _edit(
        _schema_copy(tmp_path),
        lambda d: d["classes"]["BrowserStatement"]["slot_usage"]["predicate"]["annotations"].pop(
            "browser_field"
        ),
    )
    with pytest.raises(SpecError, match="browser_field"):
        build_view_spec(path)


def test_tab_slot_must_be_an_enum(tmp_path: Path) -> None:
    def edit(d: dict[str, Any]) -> None:
        d["classes"]["BrowserRecord"]["attributes"]["mondo_membership"]["range"] = "string"

    with pytest.raises(SpecError, match="enum"):
        build_view_spec(_edit(_schema_copy(tmp_path), edit))


def test_only_one_default_sort(tmp_path: Path) -> None:
    def edit(d: dict[str, Any]) -> None:
        d["classes"]["BrowserStatement"]["slot_usage"]["subject_label"]["annotations"][
            "browser_default_sort"
        ] = "asc"

    with pytest.raises(SpecError, match="default sort"):
        build_view_spec(_edit(_schema_copy(tmp_path), edit))


def test_status_column_is_marked_in_the_schema(spec: dict[str, Any]) -> None:
    """The column the list updates live from recent sign-offs (browser_status)."""
    assert spec["status"] == "review_status"
    assert spec["status"] in [c["name"] for c in spec["columns"]]


def test_classes_carry_their_title_template(spec: dict[str, Any]) -> None:
    """A history entry is headed from its class's browser_title, like a record."""
    assert spec["classes"]["Decision"]["title_template"] == (
        "Signed off by {curator.name} on {decided_at}"
    )
    assert spec["classes"]["SignOff"]["title_template"] is None


def test_overlay_classes_add_display_hints_to_imported_classes(spec: dict[str, Any]) -> None:
    """The evidence classes come from mondo-ai unedited; overlays say how to show them."""
    assert spec["overlays"]["ConcordanceItem"] == "ConcordanceItemView"
    assert spec["overlays"]["SieveEvidenceLine"] == "EvidenceLineView"
    item = spec["classes"]["ConcordanceItemView"]
    assert item["renderer"] == "mapping"
    slots = {s["name"]: s for s in item["slots"]}
    assert slots["source_subject"]["role"] == "source_term"
    assert slots["source_subject_label"]["role"] == "source_term_label"
    assert slots["id"]["layout"] == "hidden" and slots["type"]["layout"] == "hidden"


def test_the_record_page_is_laid_out_from_the_schema(spec: dict[str, Any]) -> None:
    root = {s["name"]: s for s in spec["classes"][spec["root"]]["slots"]}
    assert root["has_evidence_lines"]["section"] == "Evidence"
    assert root["inclusion_score"]["collapsed"] == "How the score was computed"
    assert root["record_version"]["collapsed"] == "Record details"
    assert root["statement"]["layout"] == "hidden"  # it is the heading
    assert spec["lede"] == ["inclusion_score", "summary"]
    assert spec["roles"]["term"] == ["statement", "subject"]
    assert spec["roles"]["term_label"] == ["statement", "subject_label"]
    assert spec["title_template"].startswith("{statement.subject_label} ({statement.subject})")


def test_source_ids_are_shown_by_name(spec: dict[str, Any]) -> None:
    sources = next(c for c in spec["columns"] if c["name"] == "sources")
    assert sources["labels"] == "resources"


def test_one_overlay_per_class(tmp_path: Path) -> None:
    path = _edit(
        _schema_copy(tmp_path),
        lambda d: d["classes"].update(
            {
                "SecondLineView": {
                    "is_a": "SieveEvidenceLine",
                    "annotations": {"browser_overlay": True},
                }
            }
        ),
    )
    with pytest.raises(SpecError, match="SieveEvidenceLine"):
        build_view_spec(path)


def test_quick_sign_off_outcomes_come_from_the_schema(spec: dict[str, Any]) -> None:
    """Which outcomes get one-click buttons in the table (browser_quick on the form field)."""
    outcome = next(f for f in spec["form"]["fields"] if f["name"] == "outcome")
    assert outcome["quick"] == ["accepted", "rejected"]


def test_quick_outcomes_must_be_options_of_the_field(tmp_path: Path) -> None:
    path = _edit(
        _schema_copy(tmp_path),
        lambda d: d["classes"]["SignOff"]["attributes"]["outcome"]["annotations"].update(
            {"browser_quick": "accepted approved"}
        ),
    )
    with pytest.raises(SpecError, match="approved"):
        build_view_spec(path)


def test_the_score_visuals_find_their_values_by_role(spec: dict[str, Any]) -> None:
    """The gauge, the contribution chart and the evidence map read slots by browser_role."""
    assert spec["classes"]["InclusionScore"]["renderer"] == "score"
    assert spec["roles"]["score"] == ["inclusion_score", "value"]
    assert spec["roles"]["band"] == ["inclusion_score", "band"]
    assert spec["roles"]["components"] == ["inclusion_score", "components"]
    assert spec["roles"]["evidence"] == ["has_evidence_lines"]

    def roles(cls: str) -> dict[str, str]:
        return {s["role"]: s["name"] for s in spec["classes"][cls]["slots"] if s.get("role")}

    assert roles("InclusionScore") == {
        "lede": "summary",
        "score": "value",
        "band": "band",
        "prior": "claim_prior",
        "scaling": "scaling",
        "log_odds": "log_odds",
        "components": "components",
        "rule": "rule_id",
        "rule_version": "rule_version",
    }
    assert roles("ScoreComponent") == {
        "line": "line_id",
        "kind": "kind",
        "source": "source",
        "direction": "direction",
        "weight": "weight",
        "cluster": "cluster",
        "counted": "counted",
        "reason": "reason",
    }


def test_the_statement_text_is_the_headings_fallback(spec: dict[str, Any]) -> None:
    """When the title template cannot be filled, the packet's own sentence heads the page."""
    assert spec["roles"]["statement_text"] == ["statement", "statement_text"]


def test_the_sign_off_note_finds_the_version_and_rule_by_role(spec: dict[str, Any]) -> None:
    """The note above the sign-off button and the sign-off request read slots by role."""
    assert spec["roles"]["version"] == ["record_version"]
    assert spec["roles"]["rule"] == ["inclusion_score", "rule_id"]
    assert spec["roles"]["rule_version"] == ["inclusion_score", "rule_version"]


def test_the_front_end_names_no_record_slot() -> None:
    """No per-field UI code: the front end reads record slots by role, never by name.

    Names shared with a role (statement_text) or the build's meta (rule_id) are left out.
    """
    assets = SCHEMA_PATH.parent.parent / "frontend" / "assets"
    names = [
        "record_version",
        "inclusion_score",
        "has_evidence_lines",
        "subject_label",
        "source_subject",
    ]
    for script in sorted(assets.glob("*.js")):
        text = script.read_text()
        assert [n for n in names if n in text] == [], script.name
