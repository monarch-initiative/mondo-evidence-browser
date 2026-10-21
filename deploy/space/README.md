---
title: Mondo evidence sign-off
emoji: ✅
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
short_description: Sign-off service for the Mondo evidence browser
---

# Mondo evidence sign-off service

The write side of the [Mondo evidence browser](https://github.com/yaseminbridges/mondo-evidence-browser).
The browser (on GitHub Pages) logs curators in with ORCID and sends their sign-offs
here. This service checks ORCID's signature on the login token, checks the curator
registry and the form, and commits the decision to a GitHub repository. It keeps
nothing on disk.

Settings (Space *Settings → Variables and secrets*): see `deploy/space/README.md` in the
GitHub repository.
