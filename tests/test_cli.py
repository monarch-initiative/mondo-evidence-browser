"""Command line: build, spec, and the serve command's settings."""

import json
from pathlib import Path

from typer.testing import CliRunner

from mondo_evidence_browser.cli import app

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = Path(__file__).parent.parent / "config"


def test_build(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "build",
            "--store",
            str(FIXTURES),
            "--mondo",
            str(FIXTURES / "mondo_subset.obo"),
            "--config",
            str(CONFIG),
            "--index",
            str(tmp_path / "evidence.db"),
            "--site",
            str(tmp_path / "site"),
            "--decisions",
            str(tmp_path / "decisions.jsonl"),
            "--service-url",
            "",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "23 records" in result.output
    assert "IN_MONDO" in result.output and "CANDIDATE" in result.output
    config = json.loads((tmp_path / "site" / "config.json").read_text())
    assert config["service"] == {"url": ""}


def test_build_without_a_mondo_release_explains_how_to_get_one(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["build", "--store", str(FIXTURES), "--mondo", str(tmp_path / "missing.obo")],
    )
    assert result.exit_code == 1
    assert "just fetch-mondo" in result.output


def test_spec_prints_the_view_spec() -> None:
    result = runner.invoke(app, ["spec"])
    assert result.exit_code == 0
    assert json.loads(result.output)["root"] == "BrowserRecord"


def test_build_reports_duplicate_ids_without_a_traceback(tmp_path: Path) -> None:
    packets = (FIXTURES / "packets_000.jsonl").read_text().splitlines()
    store = tmp_path / "store"
    store.mkdir()
    (store / "packets_000.jsonl").write_text("\n".join([*packets, packets[0]]) + "\n")
    result = runner.invoke(
        app,
        [
            "build",
            "--store",
            str(store),
            "--mondo",
            str(FIXTURES / "mondo_subset.obo"),
            "--index",
            str(tmp_path / "e.db"),
            "--site",
            str(tmp_path / "site"),
        ],
    )
    assert result.exit_code == 1
    assert "appears twice" in result.output
    assert "Traceback" not in result.output
