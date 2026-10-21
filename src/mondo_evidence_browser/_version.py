"""Package version, set from git tags by uv-dynamic-versioning at build time."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mondo_evidence_browser")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0.0.0"
