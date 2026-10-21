# Vendored evidence schema

Copied unchanged from mondo-ai (`src/mondo_ai/schema/`), last changed there in commit
`a236706` (2026-08-16):

- `mondo_ai_sieve.yaml`: mondo-ai's root schema (imports `sieve`)
- `sieve.yaml`: the SIEVE curation model
- `sepio_classes.yaml`: the SEPIO subset SIEVE builds on

Do not edit these files. `../browser.yaml` imports them and adds everything the browser
needs. To pick up a new version, run `just vendor-schema` and then `just test`. If the
upstream schema renamed or removed a slot that `browser.yaml` annotates, the build fails
and names the slot.
