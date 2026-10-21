## Project recipes, imported by the main justfile.

store := "../mondo-ai/packet_store_merged"
port := "8000"

# Download the current Mondo release, used to split records into the two tabs
[group('data')]
fetch-mondo:
    mkdir -p data
    curl -sSL --fail -o data/mondo.obo https://purl.obolibrary.org/obo/mondo.obo
    grep -m1 '^data-version:' data/mondo.obo

# Build the index and the static site; the local service serves it (same origin)
[group('build')]
build store_dir=store:
    uv run mondo-evidence-browser build --store {{store_dir}} --service-url ""

# Build a read-only static site, as GitHub Pages gets it without a sign-off service
[group('build')]
build-static store_dir=store:
    uv run mondo-evidence-browser build --store {{store_dir}}

# Print the view spec derived from the schema
[group('build')]
spec:
    uv run mondo-evidence-browser spec

# Serve the built site and the sign-off service on http://127.0.0.1:8000
[group('app')]
serve:
    uv run mondo-evidence-browser serve --port {{port}}

# Unit tests only (no browser)
[group('checks')]
test-unit:
    uv run pytest -m "not browser"

# Browser smoke tests only (Playwright, Chromium)
[group('checks')]
test-browser:
    uv run pytest -m browser

# Re-vendor the evidence schema from a mondo-ai checkout, then run `just test`
[group('build')]
vendor-schema mondo_ai="../mondo-ai":
    cp {{mondo_ai}}/src/mondo_ai/schema/mondo_ai_sieve.yaml {{mondo_ai}}/src/mondo_ai/schema/sieve.yaml {{mondo_ai}}/src/mondo_ai/schema/sepio_classes.yaml src/mondo_evidence_browser/schema/vendor/
    git -C {{mondo_ai}} log -1 --format='Vendored mondo-ai %h (%cs)' -- src/mondo_ai/schema/
