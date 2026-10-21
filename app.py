"""Vercel entrypoint: the sign-off service as one stateless serverless function.

Vercel imports the top-level ``app`` from this file (see DEPLOY.md). Its disk does not
survive between requests, so records come from the build's lookup shards and each
decision is committed to the decisions repository; it refuses to start otherwise
rather than write sign-offs to files that would vanish. GitHub Pages serves the site.
"""

import os
import sys
from dataclasses import replace
from pathlib import Path

# The package lives in src/; make it importable whether or not Vercel installed it.
sys.path.insert(0, str(Path(__file__).parent / "src"))

from mondo_evidence_browser.service import ServiceSettings, create_service  # noqa: E402

for required in ("MEB_LOOKUP_URL", "MEB_DECISIONS_REPO"):
    if not os.environ.get(required):
        raise RuntimeError(f"{required} must be set for the serverless sign-off service")

app = create_service(replace(ServiceSettings.from_env(), site_dir=None))
