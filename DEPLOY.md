# Deploying, for free

Everything here is free with no payment card: GitHub (Pages, Actions, a repository
for decisions), Vercel's Hobby plan (the sign-off service) and, past ~600K records,
Hugging Face (a dataset for the data).

```
GitHub Pages ─────────────── the site (static), and the data up to ~600K records
     │ reads lazily over HTTP range requests
Hugging Face dataset ─────── builds/<run>/evidence.db, current/lookup/*.json.gz (optional)
     ▲ uploads each build                 │ shards read per sign-off
GitHub Actions (pages.yaml)          Vercel function (app.py) ── sign-off service (stateless)
     │ reads                              │ commits one file per decision
     └────────── GitHub repo: mondo-evidence-decisions ◄┘
```

- **Site:** GitHub Pages. Each build is pinned to its own data, so a rebuild never
  mixes two index versions in one browser session.
- **Data:** on Pages itself (1 GB per site, fine to ~600K records), or a public Hugging
  Face dataset, which serves files with range requests and CORS, uncompressed, with no
  1 GB limit.
- **Sign-off service:** [`app.py`](app.py), one Python function on Vercel. Vercel
  deploys it from this repository on every push to `main`. It keeps nothing on disk
  (and refuses to start unless configured not to):
  - records come from the build's lookup shards, about 150 records per shard at 600K;
  - each decision is committed to a GitHub repository as its own file.
- **Decisions:** `decisions/<shard>/<record>/<time>_<id>.json` in that repository. The
  layout is append-only by construction, with full history in git. The Pages build
  reads it to set review statuses.

Free tiers have limits, all ample for curation:
- Vercel Hobby is for non-commercial use, with 1M function calls and 4 CPU-hours a
  month; the first call after a quiet spell takes a second or two longer;
- GitHub's API allows 5,000 requests an hour per token;
- Pages and Hugging Face have fair-use bandwidth.

Below, `LOOKUP_URL` is where the build's lookup shards are:
- data on Pages: `https://yaseminbridges.github.io/mondo-evidence-browser/data/lookup/`
- data on Hugging Face (`HF_USER` is your username): `https://huggingface.co/datasets/HF_USER/mondo-evidence-data/resolve/main/current/lookup/`

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
- Expiration: up to a year (renew it in Vercel when it expires, then redeploy)

Copy the token. You'll paste it into Vercel's environment variables in step 3. Don't put it
anywhere in this repository.

## 3. Vercel

1. Sign up at <https://vercel.com/signup> with **Continue with GitHub**, and pick the
   **Hobby** plan. No card is asked for.
2. **Add New → Project →** import `yaseminbridges/mondo-evidence-browser`. Vercel
   reads [`vercel.json`](vercel.json): the framework preset is **FastAPI** and the
   entrypoint is `app.py`. Leave the build settings as they are.
3. Before deploying, open **Environment Variables** and add:

   | Name | Value |
   |---|---|
   | `ORCID_CLIENT_ID` | your ORCID client id (`APP-…`) |
   | `ORCID_BASE_URL` | `https://sandbox.orcid.org` (or `https://orcid.org`) |
   | `MEB_LOOKUP_URL` | `LOOKUP_URL` (above) |
   | `MEB_DECISIONS_REPO` | `yaseminbridges/mondo-evidence-decisions` |
   | `MEB_ALLOWED_ORIGINS` | `https://yaseminbridges.github.io` |
   | `MEB_GITHUB_TOKEN` | the token from step 2 (tick **Sensitive**) |

4. **Deploy.** The service's address is shown as the production domain, e.g.
   `https://mondo-evidence-browser.vercel.app`. Check it at `/api/health`: it reports
   the record count of the build its lookups come from.

From then on Vercel redeploys on every push to `main`. A changed environment variable
takes effect on the next deployment (Deployments → ⋯ → Redeploy).

## 4. Hugging Face (optional, for 600K+ records)

1. Create a free account at <https://huggingface.co/join>.
2. Settings → **Access Tokens** → Create new token → type **Write**. Copy it; GitHub
   Actions uses it to upload data.

The dataset (`HF_USER/mondo-evidence-data`) is created by the first build. Then set
`MEB_LOOKUP_URL` on Vercel to the Hugging Face `LOOKUP_URL` and redeploy.

## 5. This repository's settings

GitHub → this repository → Settings → Secrets and variables → **Actions**:

| Kind | Name | Value |
|---|---|---|
| Variable | `MEB_SERVICE_URL` | the Vercel address from step 3 |
| Variable | `ORCID_CLIENT_ID` | your ORCID client id |
| Variable | `ORCID_BASE_URL` | `https://sandbox.orcid.org` (or `https://orcid.org`) |
| Variable | `MEB_DECISIONS_REPO` | `yaseminbridges/mondo-evidence-decisions` (already set) |
| Variable | `MEB_DATA_REPO` | `HF_USER/mondo-evidence-data` (only with step 4) |
| **Secret** | `HF_TOKEN` | the Hugging Face token (only with step 4) |

## 6. ORCID, curators, first build

1. **ORCID:** in your ORCID client's Developer tools, add the redirect URI
   `https://yaseminbridges.github.io/mondo-evidence-browser/auth.html`.
2. **Curators:** add each curator's ORCID iD to
   [`config/agents.yaml`](config/agents.yaml) with `can_sign_off: true`, and push. Vercel
   redeploys from `main`.
3. **Build:** Actions → *Deploy to GitHub Pages* → **Run workflow**. It builds, uploads
   the data (to Hugging Face, if set up) and deploys the site.

Then open the site, log in with ORCID, and sign off a record. The decision appears as a
commit in `mondo-evidence-decisions`, and its review status shows on the site after the
next build (weekly, on every push, or when run by hand).

## What stays the same without this

If none of the variables are set, the workflow builds a read-only site with its data
on Pages, as before. Each piece can be set up on its own:

- data on Hugging Face (`MEB_DATA_REPO` + `HF_TOKEN`);
- review statuses from the decisions repository (`MEB_DECISIONS_REPO`);
- sign-off (the Vercel project + `MEB_SERVICE_URL` + `ORCID_CLIENT_ID`).
