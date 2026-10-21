"""Publish a build's data to a Hugging Face dataset repository (free, no card).

GitHub Pages limits a site to 1 GB; at 600K records the index alone is ~900 MB. A
public Hugging Face dataset serves files with range requests and CORS, uncompressed,
so the browser can read the index from there lazily.

Layout in the dataset repository:

- ``builds/<build id>/``: one folder per build, never changed after upload. A site
  points only at its own build's folder, so a later rebuild can never mix pages of two
  index versions inside one browser session. Only the newest few are kept.
- ``current/lookup/``: the latest build's lookup shards, for the sign-off service
  (which refetches a shard whenever it is older than what a curator saw).

Uses ``huggingface_hub`` (the ``deploy`` dependency group) and a token with write
access to the dataset (``HF_TOKEN``).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

HF = "https://huggingface.co"


def data_url(repo: str, build_id: str) -> str:
    """The public URL of one build's data folder."""
    return f"{HF}/datasets/{repo}/resolve/main/builds/{build_id}/"


def lookup_url(repo: str) -> str:
    """The public URL of the latest lookup shards (for ``MEB_LOOKUP_URL``)."""
    return f"{HF}/datasets/{repo}/resolve/main/current/lookup/"


def builds_to_prune(paths: Iterable[str], keep: int, current: str) -> list[str]:
    """Build folders to delete: all but the ``keep`` newest, never ``current``.

    Build ids are numbers (CI run ids), compared numerically.
    """
    builds = {
        parts[1]
        for path in paths
        if len(parts := path.split("/")) >= 3 and parts[0] == "builds" and parts[1].isdigit()
    }
    newest_first = sorted(builds, key=int, reverse=True)
    kept = set(newest_first[:keep]) | {current}
    return sorted(f"builds/{b}" for b in builds if b not in kept)


def publish(data_dir: Path, repo: str, build_id: str, token: str, keep: int = 3) -> str:
    """Upload a build's data folder and its lookups; prune old builds. Returns the data URL."""
    from huggingface_hub import HfApi

    api = HfApi(token=token)
    api.create_repo(repo, repo_type="dataset", exist_ok=True)
    api.upload_folder(
        repo_id=repo,
        repo_type="dataset",
        folder_path=str(data_dir),
        path_in_repo=f"builds/{build_id}",
        commit_message=f"Build {build_id}",
    )
    api.upload_folder(
        repo_id=repo,
        repo_type="dataset",
        folder_path=str(data_dir / "lookup"),
        path_in_repo="current/lookup",
        commit_message=f"Lookups of build {build_id}",
        delete_patterns=["*.json"],
    )
    files = api.list_repo_files(repo, repo_type="dataset")
    for folder in builds_to_prune(files, keep=keep, current=build_id):
        api.delete_folder(
            folder, repo_id=repo, repo_type="dataset", commit_message=f"Prune {folder}"
        )
    return data_url(repo, build_id)
