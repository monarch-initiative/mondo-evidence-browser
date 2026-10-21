"""The build: packets + Mondo release + decision log -> index + static site.

The site is everything a static host (GitHub Pages) needs: the generic front end, the
view spec derived from the schema, a site config, the vendored SQLite-over-HTTP
runtime, and the index (on the site, or written for another host with ``data_url``).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from mondo_evidence_browser.decisions import DecisionLog
from mondo_evidence_browser.index import EvidenceIndex, build_index
from mondo_evidence_browser.mondo import MondoRelease
from mondo_evidence_browser.records import RecordBuilder
from mondo_evidence_browser.registry import Registry
from mondo_evidence_browser.score import ScoreRule
from mondo_evidence_browser.viewspec import SCHEMA_PATH, build_view_spec

FRONTEND = Path(__file__).parent / "frontend"
NODE_MODULES = Path(__file__).resolve().parents[2] / "node_modules"
VENDOR_FILES = ("index.js", "sqlite.worker.js", "sql-wasm.wasm")
SITE_MARKER = ".meb-site"

# The index is published as one file. sql.js-httpvfs 0.8.12 (unmaintained) has a bug
# in its chunked mode: a read-ahead that crosses a chunk-file boundary loses the
# pages past it ("doXHR failed (bug)!"). One file avoids it; GitHub Pages limits a
# site to 1 GB in total, and larger indexes go to another host with data_url.


class BuildError(ValueError):
    """The input cannot be built into a browser; the message says what to fix."""


class DuplicateRecordError(BuildError):
    """Two packets share a record id. Mondo links by that id, so this is never ignored."""


def _statement_digest(record: dict[str, Any]) -> bytes:
    """A short fingerprint of a record's statement (subject, predicate, object)."""
    statement = record.get("statement") or {}
    predicate = (statement.get("predicate") or {}).get("code")
    triple = f"{statement.get('subject')}|{predicate}|{statement.get('object')}"
    return hashlib.sha256(triple.encode()).digest()[:12]


@dataclass(frozen=True)
class BuildConfig:
    """Inputs and outputs of a build."""

    store_dir: Path
    mondo_obo: Path
    config_dir: Path
    index_path: Path
    site_dir: Path
    decisions_path: Path | None = None
    schema_path: Path = SCHEMA_PATH
    as_of: date = field(default_factory=date.today)
    service_url: str | None = None
    """Sign-off service base URL; "" when the service serves the site; None for none."""
    orcid_client_id: str | None = None
    orcid_base_url: str = "https://sandbox.orcid.org"
    node_modules: Path = NODE_MODULES
    data_url: str | None = None
    """Public URL the data files are served from, when not from the site itself.

    The host must answer HTTP range requests and allow the site's origin (CORS),
    as S3-style buckets can. At 600K+ records the index outgrows GitHub Pages."""
    data_dir: Path | None = None
    """Where to write the data files for ``data_url``; required with it."""
    db_file: str = "evidence.db"
    """File name the index is published under. GitHub Pages gzips most content types,
    and a gzipped response breaks range reads; it leaves images alone, so the Pages
    build publishes it as ``evidence.sqlite.png``. The browser reads raw bytes and
    never treats it as an image."""


@dataclass
class BuildReport:
    """What a build read and wrote."""

    records: int = 0
    skipped: int = 0
    skipped_examples: list[str] = field(default_factory=list)
    tab_counts: dict[str, int] = field(default_factory=dict)
    db_bytes: int = 0


def _packets(store_dir: Path) -> Iterator[tuple[str, int, dict[str, Any]]]:
    files = sorted(store_dir.glob("packets_*.jsonl"))
    if not files:
        raise FileNotFoundError(f"{store_dir} has no packets_*.jsonl files")
    for path in files:
        with open(path) as f:
            for number, line in enumerate(f, start=1):
                if line.strip():
                    yield path.name, number, json.loads(line)


def assets_version(assets: Path) -> str:
    """Content hash of the front-end assets, so a new build is never served stale."""
    digest = hashlib.sha256()
    for path in sorted(p for p in assets.rglob("*") if p.is_file()):
        digest.update(path.relative_to(assets).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def _prepare_site_dir(site_dir: Path) -> None:
    """Copy the front end into an emptied site directory.

    Assets go to ``assets/<content hash>/`` and the pages point there, so browsers and
    static hosts can cache them forever. Only a previous build's output is emptied.
    """
    if site_dir.exists():
        if not (site_dir / SITE_MARKER).exists() and any(site_dir.iterdir()):
            raise FileExistsError(
                f"{site_dir} exists and is not a previous site build; choose another site_dir"
            )
        shutil.rmtree(site_dir)
    site_dir.mkdir(parents=True)
    version = assets_version(FRONTEND / "assets")
    shutil.copytree(FRONTEND / "assets", site_dir / "assets" / version)
    for page in FRONTEND.glob("*.html"):
        html = page.read_text().replace('"assets/', f'"assets/{version}/')
        html = html.replace('"./assets/', f'"./assets/{version}/')
        (site_dir / page.name).write_text(html)
    (site_dir / SITE_MARKER).touch()
    (site_dir / ".nojekyll").touch()


def _vendor(site_dir: Path, node_modules: Path) -> None:
    package = node_modules / "sql.js-httpvfs"
    if not package.exists():
        raise FileNotFoundError(f"{package} is missing; run `npm ci` (or `just install`)")
    target = site_dir / "vendor" / "sql.js-httpvfs"
    target.mkdir(parents=True)
    for name in VENDOR_FILES:
        shutil.copy2(package / "dist" / name, target / name)
    shutil.copy2(package / "LICENSE", target / "LICENSE")


def _publish_db(index_path: Path, data: Path, base_url: str, name: str) -> dict[str, Any]:
    """Copy the index into ``data`` as ``name``, served at ``base_url``.

    Returns the sql.js-httpvfs config. Only the file ``name`` in ``data`` is replaced.
    """
    data.mkdir(parents=True, exist_ok=True)
    shutil.copy2(index_path, data / name)
    return {"serverMode": "full", "url": f"{base_url}{name}", "requestChunkSize": 4096}


def build(cfg: BuildConfig) -> BuildReport:
    """Build the index and the static site."""
    if cfg.data_url and cfg.data_dir is None:
        raise BuildError("data_url is set, so give data_dir: where to write the data files")
    spec = build_view_spec(cfg.schema_path)
    rule = ScoreRule.from_yaml(cfg.config_dir / "rules" / "inclusion-score.yaml")
    mondo = MondoRelease.from_obo(cfg.mondo_obo)
    builder = RecordBuilder(rule, Registry.from_dir(cfg.config_dir), mondo, cfg.as_of)
    latest = DecisionLog(cfg.decisions_path).latest() if cfg.decisions_path else {}
    report = BuildReport()

    files: list[str] = []
    seen: dict[str, tuple[int, int, bytes]] = {}

    def records() -> Iterator[dict[str, Any]]:
        for file_name, number, raw in _packets(cfg.store_dir):
            record = builder.build(raw, latest.get(raw.get("id", "")))
            if record is None:
                report.skipped += 1
                if len(report.skipped_examples) < 20:
                    report.skipped_examples.append(f"{file_name}:{number}")
                continue
            if not files or files[-1] != file_name:
                files.append(file_name)
            key, digest = str(record[spec["key"]]), _statement_digest(record)
            if key in seen:
                first_file, first_line, first_digest = seen[key]
                where = f"{files[first_file]}:{first_line} and {file_name}:{number}"
                if first_digest != digest:
                    raise DuplicateRecordError(
                        f"Record id {key} is used by two different statements ({where}). "
                        "Mondo links by this id, so one link would show the wrong statement: "
                        "the id scheme upstream needs to change (e.g. a longer hash)."
                    )
                raise DuplicateRecordError(
                    f"Record {key} appears twice ({where}): the packet store is not merged, "
                    "and building would drop one packet's evidence. Merge packets for the "
                    "same statement upstream."
                )
            seen[key] = (len(files) - 1, number, digest)
            yield record

    provenance = {
        "mondo_release": mondo.version,
        "rule_id": rule.id,
        "rule_version": rule.version,
        "as_of": cfg.as_of.isoformat(),
        "source": str(cfg.store_dir),
    }
    report.records = build_index(cfg.index_path, records(), spec, provenance)

    _prepare_site_dir(cfg.site_dir)
    _vendor(cfg.site_dir, cfg.node_modules)
    if cfg.data_url and cfg.data_dir is not None:
        data_dir, base_url = cfg.data_dir, cfg.data_url.rstrip("/") + "/"
    else:
        data_dir, base_url = cfg.site_dir / "data", "data/"
    db_config = _publish_db(cfg.index_path, data_dir, base_url, cfg.db_file)
    site_config = {
        "schema_version": spec["schema"]["version"],
        "db": db_config,
        # "" means the sign-off service serves this site itself (same origin).
        "service": {"url": cfg.service_url.rstrip("/")} if cfg.service_url is not None else None,
        "orcid": (
            {"clientId": cfg.orcid_client_id, "baseUrl": cfg.orcid_base_url.rstrip("/")}
            if cfg.orcid_client_id
            else None
        ),
    }
    (cfg.site_dir / "config.json").write_text(json.dumps(site_config, indent=2) + "\n")
    (cfg.site_dir / "view-spec.json").write_text(json.dumps(spec, indent=1) + "\n")

    report.tab_counts = EvidenceIndex(cfg.index_path).meta()["tab_counts"]
    report.db_bytes = cfg.index_path.stat().st_size
    return report
