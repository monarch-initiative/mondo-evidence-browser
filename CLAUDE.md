# CLAUDE.md for mondo-evidence-browser

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Schema-driven browser and sign-off tool for Mondo evidence records. Curators review
the evidence for a statement, see its computed inclusion score (D-063) and sign off
(D-048). Links from inside Mondo resolve to a record's stable URL.

The project uses `uv` for dependency management and `just` as the command runner.
Generated from monarch-project-copier (see `.copier-answers.yml`).

## IMPORTANT INSTRUCTIONS

- we use test driven development, write tests first before implementing a feature
- do not try and 'cheat' by making mock tests (unless asked). The exception is ORCID,
  an external system: tests stand in for its key endpoint at the HTTP transport
  (`tests/orcid.py`); token verification itself runs for real
- if functionality does not work, keep trying, do not relax the test just to get poor code in
- always run tests (`just test`, which includes the Playwright browser tests)
- use docstrings; doctests double as docs and tests

In general AVOID try/except blocks, except when these are truly called for, for example
when interfacing with external systems (ORCID, the network, untrusted tokens).

## Design rules

- **The LinkML schema is the single source of truth** for what the browser shows:
  `src/mondo_evidence_browser/schema/browser.yaml`.
  - Never write per-field UI code. Tabs, columns, sort keys, filters, the detail view
    and the sign-off form all come from `view-spec.json`, which `viewspec.py` derives
    from the schema's `browser_*` annotations.
  - A new display need means a new annotation, handled generically in `viewspec.py`
    and the front end.
- **`schema/vendor/` is a verbatim copy** of mondo-ai's schema. Don't edit it; refine
  slots in `browser.yaml` via `is_a` + `slot_usage`.
- **Computed slots** (Mondo membership, inclusion score, review status, record version)
  are produced in `records.py` under their schema slot names. Tests validate the
  records against the schema.
- **The index layout comes from the spec** (`index.py`): filter columns are indexed on
  `(tab, column)`, and each sortable column gets a rank column indexed on `(tab, rank)`.
  Its naming is written into the index metadata for the front end. Queries must stay
  index-ordered (no OFFSET in normal paging) so the static site reads lazily over HTTP
  range requests. The index is published as one file: sql.js-httpvfs's chunked mode is
  broken.
- **The build refuses bad data:** duplicate or colliding record ids fail it, and claim
  types the Mondo comparison can't answer are marked NOT_CHECKED, never guessed.
- **README "Changing things"** lists where each kind of change goes; keep it current.
- **Decisions are append-only** and each one records the record version and score
  rule version it was given on. Locally they go to a JSONL log (`decisions.py`); when
  deployed, the stateless service commits one file per decision to a GitHub repo
  (`decision_store.py`) and checks records against per-record lookup shards
  (`lookup.py`), never the whole index. Deployment is free and documented in DEPLOY.md.
- **The score rule is versioned** (`config/rules/*.yaml`). Bump the version whenever
  a number changes.
- **Requirement IDs (D-0NN)** refer to the Mondo evidence, provenance and confidence
  requirements, which are still a draft.

## Commands

- `just test`: pytest (unit, doctests, browser smoke tests), mypy, ruff
- `just test-unit` / `just test-browser`
- `just fetch-mondo`: download the Mondo release used for the two tabs
- `just build`: index + static site (`var/evidence.db`, `site/`)
- `just serve`: site + sign-off service on http://127.0.0.1:8000
- `just spec`: print the view spec
- `just vendor-schema`: re-copy the evidence schema from ../mondo-ai
- `just lint-all`: every pre-commit hook on every file
