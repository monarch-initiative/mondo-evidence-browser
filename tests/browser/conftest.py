"""Live sites for the browser tests: a real build, served by the real service.

Each fixture builds a site into a temporary directory and serves it with uvicorn on a
free port. Only ORCID's key endpoint is stood in for (see ``tests/orcid.py``).
"""

import copy
import json
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from mondo_evidence_browser.build import BuildConfig, build
from mondo_evidence_browser.index import EvidenceIndex
from mondo_evidence_browser.service import ServiceSettings, create_service
from mondo_evidence_browser.viewspec import SCHEMA_PATH
from tests.orcid import CLIENT_ID, ORCID_BASE, jwks_endpoint

FIXTURES = Path(__file__).parent.parent / "fixtures"
CONFIG = Path(__file__).parent.parent.parent / "config"

pytestmark = pytest.mark.browser


@dataclass
class Site:
    """A built and served site."""

    url: str
    root: Path
    index: EvidenceIndex
    meta: dict[str, Any]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def _start(app: Any, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("test server did not start")
        time.sleep(0.02)
    return server, thread


def _data_host(directory: Path) -> FastAPI:
    """A static host for the data files on another origin, like an S3-style bucket."""
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "HEAD"],
        allow_headers=["Range"],
        expose_headers=["Content-Range", "Content-Length", "Accept-Ranges"],
    )
    app.mount("/", StaticFiles(directory=directory), name="data")
    return app


def _serve(
    root: Path,
    *,
    store_dir: Path = FIXTURES,
    schema_path: Path = SCHEMA_PATH,
    service: bool = True,
    orcid: bool = True,
    external_data: bool = False,
) -> Iterator[Site]:
    data_port = _free_port() if external_data else None
    build(
        BuildConfig(
            store_dir=store_dir,
            mondo_obo=FIXTURES / "mondo_subset.obo",
            config_dir=CONFIG,
            index_path=root / "evidence.db",
            site_dir=root / "site",
            decisions_path=root / "decisions.jsonl",
            schema_path=schema_path,
            as_of=date(2026, 10, 20),
            service_url="" if service else None,
            orcid_client_id=CLIENT_ID if orcid else None,
            orcid_base_url=ORCID_BASE,
            data_url=f"http://127.0.0.1:{data_port}/" if data_port else None,
            data_dir=root / "data-host" if data_port else None,
            # Like the Pages build: a name whose content type hosts do not recompress.
            db_file="evidence.sqlite.png" if data_port else "evidence.db",
        )
    )
    data_server = (
        _start(_data_host(root / "data-host"), data_port) if data_port is not None else None
    )
    settings = ServiceSettings(
        index_path=root / "evidence.db",
        decisions_path=root / "decisions.jsonl",
        config_dir=CONFIG,
        schema_path=schema_path,
        site_dir=root / "site",
        orcid_client_id=CLIENT_ID,
        orcid_base_url=ORCID_BASE,
    )
    port = _free_port()
    server, thread = _start(create_service(settings, orcid_transport=jwks_endpoint()), port)
    index = EvidenceIndex(root / "evidence.db")
    yield Site(url=f"http://127.0.0.1:{port}", root=root, index=index, meta=index.meta())
    for running, runner in [(server, thread), *([data_server] if data_server else [])]:
        running.should_exit = True
        runner.join(timeout=10)


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Site]:
    """The fixture packets, with the sign-off service and ORCID configured."""
    yield from _serve(tmp_path_factory.mktemp("site"))


@pytest.fixture(scope="module")
def static_site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Site]:
    """A read-only deployment: no sign-off service, no ORCID (like a bare Pages site)."""
    yield from _serve(tmp_path_factory.mktemp("static"), service=False, orcid=False)


@pytest.fixture(scope="module")
def big_site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Site]:
    """~7K records (a 10 MB index), to check that reads stay lazy.

    Test-only data: the 23 real fixture packets replicated under distinct ids.
    """
    root = tmp_path_factory.mktemp("big")
    store = root / "store"
    store.mkdir()
    packets = [json.loads(line) for line in (FIXTURES / "packets_000.jsonl").open()]
    with open(store / "packets_000.jsonl", "w") as f:
        for copy_number in range(300):
            for packet in packets:
                p = copy.deepcopy(packet)
                p["id"] = f"{packet['id']}_{copy_number}"
                p["statement"]["object"] = f"{packet['statement']['object']} {copy_number}"
                f.write(json.dumps(p) + "\n")
    yield from _serve(root, store_dir=store)


@pytest.fixture(scope="module")
def external_data_site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Site]:
    """The site on one origin, its data on another, like a bucket."""
    yield from _serve(tmp_path_factory.mktemp("external"), external_data=True)
