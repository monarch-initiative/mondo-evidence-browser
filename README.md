# mondo-evidence-browser

A schema-driven browser over Mondo's evidence records. Mondo is moving to
evidence-backed statements: a computed inclusion score ranks the curation queue, and a
human sign-off is what puts a claim into Mondo (requirements D-044, D-048). Curators
use the browser to look at the evidence and sign off. A link from inside Mondo ("see
the full evidence record") resolves to the record's page here.

The requirements (*Mondo evidence, provenance and confidence requirements*) and the
schemas are still moving. Everything the browser shows comes from one LinkML schema, so
a schema change means rebuilding, not editing UI code.

## Quick start

```bash
just install          # uv, npm (sql.js-httpvfs), Playwright's Chromium, pre-commit hooks
cp .env.example .env  # then set ORCID_CLIENT_ID to enable login (see "ORCID client")
just fetch-mondo      # the current Mondo release, for the two tabs
just build            # index + static site from ../mondo-ai/packet_store_merged
just serve            # http://127.0.0.1:8000 (site + sign-off service)
```

`just test` runs everything: unit tests, doctests, the browser smoke tests, mypy and
ruff. `just test-unit` and `just test-browser` run each half on its own.

## What it does

- **Two tabs:** evidence for statements already in Mondo, and for candidate
  statements not in Mondo yet.
  - Membership is checked against the Mondo release used for the build. A synonym
    counts as in Mondo when the term has the same text with the same scope, ignoring
    case and spacing. A subclass counts when the term has that `is_a`.
  - Claim types the build can't compare with a release yet (anything but synonyms and
    subclass placement) are marked **Not checked** rather than guessed. They get a
    third tab, which only appears when it has records.
  - On the real mondo-ai store (6,495 records) against Mondo 2026-10-06: 3,898 are in
    Mondo, 2,597 are candidates, none unchecked.
- **A lazy, sortable table**, sorted by inclusion score by default. Columns, sortable
  columns, filters and search all come from the schema.
  - For every sortable column the index stores each row's rank within its tab, with
    its own index. Any page of any sort order is a range read of that index, whatever
    its depth, and a direct link to page 10,000 costs the same as page 1.
  - Filtered views continue from the page shown. For a common filter value the browser
    walks the rank index; for a rare one it sorts the few matching rows. The counts it
    decides with are stored at build time.
  - The browser fetches only the database pages it needs. Tested on a 6,900-record
    index: opening page 100 directly reads under 10% of the file, and loading,
    re-sorting and paging under 20%.
- **The score with its components:**
  - the score S;
  - its band;
  - the rule and rule version;
  - the claim prior L0, the scaling k and the log-odds L;
  - one row per evidence line: d, s, q and c with their bases, w, independence
    cluster, whether the line counted, and why not.
- **A stable URL per record:** `record.html?id=<record id>`. It works as a direct link
  on GitHub Pages too. The record id is the packet id, which mondo-ai derives from
  the statement itself (`pkt_` + the first 12 hex digits of
  `sha256("{subject}|{predicate}|{object}")`), so Mondo can reference it.
- **ORCID login and sign-off:**
  - Curators log in with ORCID using the OpenID Connect implicit flow. That works
    from a static page, so there's no client secret in the browser.
  - The sign-off form is generated from the schema's `SignOff` class.
  - A small sign-off service checks ORCID's signature on the token, checks the curator
    registry and the form, and appends the decision to a log that is never edited or
    deleted.
  - Each sign-off records the record version, score and rule version on screen
    (D-048, D-051). A later change to any of them sends the record back to the queue
    as "signed off on an older version".

## Scale

Measured on 600,000 records: the 6,495 real packets copied under new ids, with
statements unchanged, so the tab split stays realistic. Measured in Chromium against
a local server.

| Action | Read from the 906 MB index | HTTP requests |
|---|---|---|
| Open the list (default order) | 44 KB | 12 |
| Re-sort by another column | 76 KB | 18 |
| Open page 3,597 by link | 44 KB | 12 |
| Common filter, then Next twice | 80 KB | 15 |
| Two filters at once | 192 KB | 43 |
| Search a rare word | 244 KB | 53 |
| Search a common word ("syndrome") | 3.8 MB | 85 |
| Open a record by link | 52 KB | 10 |
| Expand a row | 72 KB | 18 |

The build takes about 4 minutes, and its peak memory stays near 235 MB whatever the
size. The index grows by about 1.5 KB per record. The service's lookup shards come to
36 MB, about 9 KB fetched per sign-off.

What makes this work:

- **Default order on disk.** Rows are stored in the default order, so a page is a run
  of neighbouring rows.
- **Indexed sort ranks.** Every sortable column has an indexed rank, so any page of any
  order is a range read.
- **The search index answers on its own.** It holds exact tokens for the tab and every
  filter value, so searches and filters, and their counts, never read the rows.
- **Stored counts.** Counts for every combination of filter values are stored at build
  time.

A search with more than 500 matches can only be shown in the default order. The page
says so; sorting it by another column would mean reading every matching row.

## How it is built

```
schema/browser.yaml ──► view-spec.json ──┬─► front end (tabs, columns, sort, detail, form)
  imports vendor/*.yaml                  └─► SQLite layout (columns, indexes, search)
packets + mondo.obo + decision log ──► BrowserRecords ──► index ──► static site
```

- **[`schema/browser.yaml`](src/mondo_evidence_browser/schema/browser.yaml)**
  imports the vendored mondo-ai SIEVE schema and specialises its packet into a
  `BrowserRecord`. The record adds the computed slots: Mondo membership, inclusion
  score with components, review status, and record version. It also defines the
  `SignOff` form and the `Decision` log record.
- **Display hints are slot annotations**, documented at the top of that file:
  `browser_column`, `browser_sortable`, `browser_default_sort`, `browser_tab`,
  `browser_search`, `browser_filter`, `browser_format`, `browser_layout`,
  `browser_widget`, and a few class annotations.
- **[`viewspec.py`](src/mondo_evidence_browser/viewspec.py)** turns the schema into
  `view-spec.json`. Mistakes fail the build instead of quietly dropping a column:
  - a misspelled `browser_*` key;
  - a class-valued column without `browser_field`;
  - a tab slot that isn't an enum;
  - two default sorts;
  - a `slot_usage` on a slot the evidence schema no longer has (an upstream rename).
- **The front end** ([`frontend/`](src/mondo_evidence_browser/frontend/)) is plain
  ES modules with no build step.
  - It renders tables, records, forms and history only from the spec.
  - Polymorphic evidence items render as their class (the `type` designator).
  - Fields the schema doesn't know are shown raw, never dropped.
- **The CI schema-swap test**
  ([`tests/browser/test_schema_swap.py`](tests/browser/test_schema_swap.py)) builds
  with a modified schema and checks the index, the API and the rendered app. The
  modifications are a new sortable, searchable column, a retitled column, a removed
  column, renamed tabs and a new form field.

To see what the app will show, run `just spec`.

## Changing things

Requirements, schemas and data will keep changing. Each kind of change has one place,
and the build fails loudly when something no longer fits, instead of quietly showing
wrong data.

| When this changes… | …change only | What guards it |
|---|---|---|
| What is shown: columns, sort, filters, tabs, row preview, form, labels, explanations | [`schema/browser.yaml`](src/mondo_evidence_browser/schema/browser.yaml) annotations | Mistakes fail the build; the CI schema-swap test rebuilds with a modified schema |
| The upstream evidence schema | `just vendor-schema`, then fix what the build names | A renamed or removed slot that `browser.yaml` refines fails the build |
| The source packet format | one adapter in [`adapters.py`](src/mondo_evidence_browser/adapters.py) | Unrecognised packets are counted and reported |
| Score numbers: strengths, priors, k, floor | [`config/rules/inclusion-score.yaml`](config/rules/inclusion-score.yaml), with a new `version` | Scores and sign-offs record the version; older sign-offs become "signed off on an older version" |
| Source quality, dependence, agent trust, who may sign off | [`config/resources.yaml`](config/resources.yaml), [`config/agents.yaml`](config/agents.yaml) | Levels are dated, so re-rating does not rewrite past scores |
| The D-063 formula itself | [`score.py`](src/mondo_evidence_browser/score.py) | `tests/test_score.py`, including the requirements' worked examples |
| Which claim types are checked against Mondo | `CHECKED_PREDICATES` and `contains` in [`mondo.py`](src/mondo_evidence_browser/mondo.py) | Unsupported claim types show as "Not checked" |
| Record ids | upstream (mondo-ai) | Two statements with one id, or one packet twice, fail the build |
| Where the data is hosted | `--data-url` / `--data-dir` (or `MEB_DATA_URL`) | A browser test reads the data from another origin |
| How the browser reads the data | [`db.js`](src/mondo_evidence_browser/frontend/assets/db.js) only | The browser smoke tests |
| The index layout (columns, ranks, indexes) | [`index.py`](src/mondo_evidence_browser/index.py), derived from the spec | It writes its naming into the index metadata, which the front end reads |

## Deploying

Everything is free with no payment card. [**DEPLOY.md**](DEPLOY.md) is the
step-by-step setup.

| Part | Where | Notes |
|---|---|---|
| Site | GitHub Pages | Static; content-hashed asset paths, so a redeploy is never served stale |
| Data (index + lookup shards) | Pages, or a Hugging Face dataset for 600K+ records | Range requests and CORS, uncompressed; pinned per build |
| Sign-off service | A free Hugging Face Space (Docker) | Stateless: per-record lookup shards, decisions committed to GitHub |
| Decisions | A GitHub repository, one file per decision | Append-only, versioned; the build reads it for review statuses |
| Builds | GitHub Actions ([`pages.yaml`](.github/workflows/pages.yaml)) | On pushes to main, weekly, and on demand |

Notes:

- **Data on Pages.** Without a Hugging Face dataset the index is published on Pages
  as `evidence.sqlite.png`. Pages gzips most content types, which breaks range reads,
  but leaves images alone. Pages allows 1 GB per site, roughly 600K records.
- **Data on Hugging Face.** Each build gets its own folder, so a rebuild never mixes
  two index versions in one browser session.
- **The SQLite-over-HTTP library.** sql.js-httpvfs 0.8.12 is unmaintained, and its
  chunked mode loses pages when a read crosses a chunk boundary, so the index isn't
  split. Only `db.js` uses it, so it can be swapped (for example for DuckDB-WASM)
  without touching the rest.
- **Local runs** (`just serve`) use a local index and decision log instead; every
  setting is documented in [`service.py`](src/mondo_evidence_browser/service.py).

**ORCID client.** Register a public API client (the sandbox for testing). Its redirect
URI is the site's `auth.html`, for example
`https://yaseminbridges.github.io/mondo-evidence-browser/auth.html`. The browser
asks for the `openid` scope; the service only needs the client id.

- **Locally:** put the client id in `.env` (gitignored; template in `.env.example`).
  `just build` and `just serve` load it.
- **Redirect URIs are matched exactly, host included.** Open the browser at the
  address you registered (for example `http://127.0.0.1:8000`, not `localhost:8000`).
- **Sandbox accounts:** sandbox.orcid.org only sends email to `@mailinator.com`
  addresses. Use one of those to register a test account, and never reuse a real
  password there.
- **Who can sign off:** add your ORCID iD to `config/agents.yaml` with
  `can_sign_off: true`. Without it, login works but sign-off is refused.
- **The client secret is never needed;** keep it out of the project.

## Inclusion score (D-063)

```
w_i = d_i · s_i · q_i · c_i                         per evidence line
L   = L0 + k · Σ over independence clusters of w      (strongest line per cluster, D-046)
S   = tanh(L / 2) ∈ (−1, 1)
```

The rule is in [`config/rules/inclusion-score.yaml`](config/rules/inclusion-score.yaml)
(version `0.1.0-uncalibrated`), using [`config/resources.yaml`](config/resources.yaml)
for source quality and dependence, and [`config/agents.yaml`](config/agents.yaml) for
trust and sign-off rights.

**Every number in the rule is a placeholder:** calibration is a design task in D-063.
The values reproduce the D-063 Q2 worked example in `tests/test_score.py` (0.62 for a
community contributor, 0.92 for a ClinGen panel). Claim types without a rule show as
unscored rather than 0.

## Data and repository

- **Packets:** the real mondo-ai packet store. The test fixtures are 23 of those
  packets plus the real Mondo stanzas of their terms. The browser tests' 6,900-record
  laziness check replicates those packets under new ids; nothing synthetic ships.
- **Repository:** generated from
  [monarch-project-copier](https://github.com/monarch-initiative/monarch-project-copier)
  (`.copier-answers.yml`, update with `just _update-template`), then modernised. CI
  ([`main.yaml`](.github/workflows/main.yaml)) has three jobs:
  - `lint`: pre-commit hooks and mypy;
  - `test`: Python 3.11–3.13;
  - `smoke`: Playwright in Chromium, keeping traces when a test fails.

## Not done yet

- **Settle the record id contract with mondo-ai.** Ids are 12 hex digits of a
  SHA-256 (48 bits); at 600K records the chance of a collision is about 1 in 1,500.
  The build fails if one happens, but a longer id upstream would prevent it.
- **Pages for records that disappear.** A record that drops out of the store leaves
  its Mondo link showing "No evidence record". D-053 keeps rejected claims on record,
  so removed records need a page that says what happened.
- **A custom domain** for the Pages site, so Mondo's links don't depend on this repo's
  name and org.

- **Invalidating an evidence line** (D-052), sign-off as an evidence line (D-049), and
  collective sign-offs (D-046).
- **Calibrated strengths and priors** (D-063), and rules for more claim types.
- **A hosting choice for the sign-off service**, with a decision export so the Pages
  build can read the live log.
