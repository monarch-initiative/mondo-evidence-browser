# Deploying, for free

Everything here is free with no payment card: GitHub (Pages, Actions, a repository
for decisions) and Hugging Face (a dataset for the data, a Space for the sign-off
service).

```
GitHub Pages ─────────────── the site (static)
     │ reads lazily over HTTP range requests
Hugging Face dataset ─────── builds/<run>/evidence.db, current/lookup/*.json
     ▲ uploads each build                 │ shards read per sign-off
GitHub Actions (pages.yaml)          Hugging Face Space ── sign-off service (stateless)
     │ reads                              │ commits one file per decision
     └────────── GitHub repo: mondo-evidence-decisions ◄┘
```

- **Site:** GitHub Pages. Each build is pinned to its own data folder, so a rebuild
  never mixes two index versions in one browser session.
- **Data:** a public Hugging Face dataset. It serves files with range requests and CORS,
  uncompressed, with no 1 GB limit, so 600K+ records fit.
- **Sign-off service:** a free Hugging Face Space (Docker). It keeps nothing on disk:
  - records come from the build's lookup shards, about 150 records per shard at 600K;
  - each decision is committed to a GitHub repository as its own file.
- **Decisions:** `decisions/<shard>/<record>/<time>_<id>.json` in that repository. The
  layout is append-only by construction, with full history in git. The Pages build
  reads it to set review statuses.

Free tiers have limits:
- a Space sleeps after about 48 hours unused, and the first request then waits about
  a minute;
- GitHub's API allows 5,000 requests an hour per token;
- Pages and Hugging Face have fair-use bandwidth.

All of that is ample for curation.

Below, `HF_USER` is your Hugging Face username.

## 1. The decisions repository

Create an empty public repository `mondo-evidence-decisions` on GitHub, with a README so
that it has a `main` branch. Public means curators' ORCID iDs and rationales are
visible, which is usual for open curation; it can be private, but then the Pages
build needs a token to read it.

## 2. A GitHub token that can write only there

GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained
tokens** → Generate new token:

- Resource owner: you
- Repository access: **Only select repositories** → `mondo-evidence-decisions`
- Permissions → Repository permissions → **Contents: Read and write**
- Expiration: up to a year (renew it in step 4 when it expires)

Copy the token. You'll paste it into the Space's secrets in step 4. Don't put it
anywhere in this repository.

## 3. Hugging Face

1. Create a free account at <https://huggingface.co/join>.
2. Settings → **Access Tokens** → Create new token → type **Write**. Copy it. GitHub
   Actions uses it to upload data and rebuild the Space.
3. Create a **Space**: New → Space.
   - Name: `mondo-evidence-signoff`
   - SDK: **Docker** → Blank
   - Hardware: **CPU basic (free)**
   - Visibility: Public
4. Add the two files from [`deploy/space/`](deploy/space/) to the Space, either in its
   *Files* tab (add file → upload) or from this repository:

   ```bash
   uvx --from huggingface_hub hf auth login
   uvx --from huggingface_hub hf upload HF_USER/mondo-evidence-signoff deploy/space . --repo-type space
   ```

The data dataset (`HF_USER/mondo-evidence-data`) is created by the first build.

## 4. The Space's settings

Space → Settings → **Variables and secrets**:

| Kind | Name | Value |
|---|---|---|
| Variable | `ORCID_CLIENT_ID` | your ORCID client id (`APP-…`) |
| Variable | `ORCID_BASE_URL` | `https://sandbox.orcid.org` (or `https://orcid.org`) |
| Variable | `MEB_LOOKUP_URL` | `https://huggingface.co/datasets/HF_USER/mondo-evidence-data/resolve/main/current/lookup/` |
| Variable | `MEB_DECISIONS_REPO` | `yaseminbridges/mondo-evidence-decisions` |
| Variable | `MEB_ALLOWED_ORIGINS` | `https://yaseminbridges.github.io` |
| **Secret** | `MEB_GITHUB_TOKEN` | the token from step 2 |

The service's address is `https://HF_USER-mondo-evidence-signoff.hf.space`. Check it at
`/api/health` once the first build (step 6) has published the lookups.

## 5. This repository's settings

GitHub → this repository → Settings → Secrets and variables → **Actions**:

| Kind | Name | Value |
|---|---|---|
| Variable | `MEB_SERVICE_URL` | `https://HF_USER-mondo-evidence-signoff.hf.space` |
| Variable | `ORCID_CLIENT_ID` | your ORCID client id |
| Variable | `ORCID_BASE_URL` | `https://sandbox.orcid.org` (or `https://orcid.org`) |
| Variable | `MEB_DECISIONS_REPO` | `yaseminbridges/mondo-evidence-decisions` |
| Variable | `MEB_DATA_REPO` | `HF_USER/mondo-evidence-data` |
| Variable | `MEB_SPACE` | `HF_USER/mondo-evidence-signoff` |
| **Secret** | `HF_TOKEN` | the Hugging Face token from step 3 |

## 6. ORCID, curators, first build

1. **ORCID:** in your ORCID client's Developer tools, add the redirect URI
   `https://yaseminbridges.github.io/mondo-evidence-browser/auth.html`.
2. **Curators:** add each curator's ORCID iD to
   [`config/agents.yaml`](config/agents.yaml) with `can_sign_off: true`, and push. The
   Space rebuilds from `main`.
3. **Build:** Actions → *Deploy to GitHub Pages* → **Run workflow**. It builds, uploads
   the data to Hugging Face, deploys the site, and rebuilds the Space.

Then open the site, log in with ORCID, and sign off a record. The decision appears as a
commit in `mondo-evidence-decisions`, and its review status shows on the site after the
next build (weekly, on every push, or when run by hand).

## What stays the same without this

If none of the variables are set, the workflow builds a read-only site with its data
on Pages, as before. Each piece can be set up on its own:

- data on Hugging Face (`MEB_DATA_REPO` + `HF_TOKEN`);
- review statuses from the decisions repository (`MEB_DECISIONS_REPO`);
- sign-off (the Space + `MEB_SERVICE_URL` + `ORCID_CLIENT_ID`).
