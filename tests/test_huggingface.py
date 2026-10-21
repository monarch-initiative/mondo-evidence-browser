"""Publishing data to a Hugging Face dataset: per-build folders, pruning, URLs."""

from mondo_evidence_browser.huggingface import builds_to_prune, data_url


def test_each_build_gets_its_own_immutable_folder() -> None:
    assert data_url("yaseminbridges/mondo-evidence-data", "1234") == (
        "https://huggingface.co/datasets/yaseminbridges/mondo-evidence-data/resolve/main/builds/1234/"
    )


def test_only_the_newest_builds_are_kept() -> None:
    paths = [
        "builds/100/evidence.db",
        "builds/100/lookup/abc.json",
        "builds/250/evidence.db",
        "builds/99/evidence.db",
        "builds/300/evidence.db",
        "current/lookup/abc.json",
        "README.md",
    ]
    # Numeric order, not text order: 99 is older than 100.
    assert builds_to_prune(paths, keep=2, current="300") == ["builds/100", "builds/99"]
    assert builds_to_prune(paths, keep=5, current="300") == []
    # The build being published is never pruned, however many are kept.
    assert "builds/300" not in builds_to_prune(paths, keep=0, current="300")
