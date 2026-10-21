"""Command line: build the index and static site, print the view spec, serve."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from mondo_evidence_browser.build import BuildConfig, BuildError
from mondo_evidence_browser.build import build as run_build
from mondo_evidence_browser.viewspec import SCHEMA_PATH, build_view_spec

app = typer.Typer(no_args_is_help=True, help="Mondo evidence browser.")


@app.command()
def build(
    store: Annotated[Path, typer.Option(help="Directory with packets_*.jsonl")] = Path(
        "../mondo-ai/packet_store_merged"
    ),
    mondo: Annotated[Path, typer.Option(help="Mondo release (mondo.obo)")] = Path("data/mondo.obo"),
    config: Annotated[Path, typer.Option(help="Score rule and registries")] = Path("config"),
    index: Annotated[Path, typer.Option(help="SQLite index to write")] = Path("var/evidence.db"),
    site: Annotated[Path, typer.Option(help="Static site to write")] = Path("site"),
    decisions: Annotated[Path, typer.Option(help="Decision log")] = Path("var/decisions.jsonl"),
    schema: Annotated[Path, typer.Option(help="Browser schema")] = SCHEMA_PATH,
    service_url: Annotated[
        str | None,
        typer.Option(
            envvar="MEB_SERVICE_URL",
            help='Sign-off service URL; "" when the service serves the site itself',
        ),
    ] = None,
    orcid_client_id: Annotated[str | None, typer.Option(envvar="ORCID_CLIENT_ID")] = None,
    data_url: Annotated[
        str | None,
        typer.Option(envvar="MEB_DATA_URL", help="Serve the data files from this URL"),
    ] = None,
    data_dir: Annotated[
        Path | None, typer.Option(help="Where to write the data files for --data-url")
    ] = None,
    db_file: Annotated[
        str,
        typer.Option(
            envvar="MEB_DB_FILE",
            help="Published database file name (GitHub Pages: evidence.sqlite.png)",
        ),
    ] = "evidence.db",
    orcid_base_url: Annotated[str, typer.Option(envvar="ORCID_BASE_URL")] = (
        "https://sandbox.orcid.org"
    ),
) -> None:
    """Build the index and the static site from a packet store and a Mondo release."""
    if not mondo.exists():
        typer.echo(f"No Mondo release at {mondo}. Run `just fetch-mondo` first.", err=True)
        raise typer.Exit(1)
    try:  # a data problem is reported as a message, not a traceback
        report = run_build(
            BuildConfig(
                store_dir=store,
                mondo_obo=mondo,
                config_dir=config,
                index_path=index,
                site_dir=site,
                decisions_path=decisions,
                schema_path=schema,
                service_url=service_url,
                orcid_client_id=orcid_client_id,
                orcid_base_url=orcid_base_url,
                data_url=data_url,
                data_dir=data_dir,
                db_file=db_file,
            )
        )
    except BuildError as error:
        typer.echo(f"Build failed: {error}", err=True)
        raise typer.Exit(1) from error
    tabs = ", ".join(f"{tab} {n:,}" for tab, n in sorted(report.tab_counts.items()))
    typer.echo(
        f"Built {report.records:,} records ({tabs}); skipped {report.skipped:,} unrecognised. "
        f"Index {report.db_bytes / 1e6:.1f} MB. Site: {site}"
    )
    for example in report.skipped_examples:
        typer.echo(f"  skipped: {example}")


@app.command()
def spec(schema: Annotated[Path, typer.Option(help="Browser schema")] = SCHEMA_PATH) -> None:
    """Print the view spec derived from the schema."""
    typer.echo(json.dumps(build_view_spec(schema), indent=2))


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the sign-off service, serving the built site (settings from MEB_* / ORCID_*)."""
    import uvicorn

    from mondo_evidence_browser.service import ServiceSettings, create_service

    settings = ServiceSettings.from_env()
    if settings.orcid_client_id is None:
        typer.echo("ORCID_CLIENT_ID is not set: browsing works, signing off does not.")
    uvicorn.run(create_service(settings), host=host, port=port)


@app.command(name="publish-hf")
def publish_hf(
    data_dir: Annotated[Path, typer.Option(help="The build's --data-dir")],
    repo: Annotated[str, typer.Option(envvar="MEB_DATA_REPO", help="Dataset repo owner/name")],
    build_id: Annotated[str, typer.Option(help="Build id, e.g. the CI run id")],
    token: Annotated[str, typer.Option(envvar="HF_TOKEN", help="Hugging Face write token")],
    keep: Annotated[int, typer.Option(help="Builds to keep")] = 3,
) -> None:
    """Upload a build's data to a Hugging Face dataset (needs the deploy group)."""
    from mondo_evidence_browser.huggingface import lookup_url, publish

    url = publish(data_dir, repo, build_id, token, keep=keep)
    typer.echo(f"Published {data_dir} to {url}")
    typer.echo(f"Lookups for the sign-off service (MEB_LOOKUP_URL): {lookup_url(repo)}")


@app.command(name="restart-space")
def restart_space_command(
    space: Annotated[str, typer.Option(envvar="MEB_SPACE", help="Space owner/name")],
    token: Annotated[str, typer.Option(envvar="HF_TOKEN", help="Hugging Face write token")],
) -> None:
    """Rebuild the sign-off service's Space so it runs the latest code."""
    from mondo_evidence_browser.huggingface import restart_space

    restart_space(space, token)
    typer.echo(f"Restarting {space}")
