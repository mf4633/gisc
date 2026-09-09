"""gisc command line.

    gisc ir      corridor.conflicts ...   compile and print the plan, run nothing
    gisc compile corridor.conflicts ...   compile the plan, then run it
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import uuid
from typing import Optional

import typer

from gisc import __version__
from gisc.compile import compile_task, write_plan
from gisc.errors import GiscError
from gisc.exec import execute
from gisc.tasks import TASKS

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="A stateless civil/GIS compiler. Reads your files, writes one folder.",
)

# Shared options, so `ir` and `compile` cannot drift apart.
_TASK = typer.Argument(..., help="e.g. corridor.conflicts")
_ALIGNMENT = typer.Option(..., "--alignment", "-a", help="LandXML or GeoJSON LineString")
_UTILS = typer.Option(None, "--utils", "-u", help="GeoPackage / GeoJSON / PostGIS URI")
_FLOOD = typer.Option(None, "--flood", "-f", help="GeoJSON / GPKG polygons, or an nfhl: URI")
_ROW = typer.Option(None, "--row", help="ROW / parcels: GeoJSON or GPKG")
_BUFFER = typer.Option(15.0, "--buffer-ft", "-b", help="Corridor half-width, ground feet")
_CRS = typer.Option("EPSG:3857", "--crs", help="Analysis CRS. Prefer your state plane zone.")
_OUT = typer.Option(None, "--out", "-o", help="Output folder (default ./out/<run_id>)")
_ALN_CRS = typer.Option(None, "--alignment-crs", help="CRS for an alignment that declares none")
_UTILS_LAYER = typer.Option(None, "--utils-layer", help="Layer/table name inside --utils")
_FLOOD_LAYER = typer.Option(None, "--flood-layer", help="Layer name inside --flood")
_ALN_LAYER = typer.Option(None, "--alignment-name", help="<Alignment name=...> to use")


def _run_id() -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def _layers(utils_layer, flood_layer, alignment_name) -> dict[str, str]:
    return {
        k: v
        for k, v in (
            ("utilities", utils_layer),
            ("flood", flood_layer),
            ("alignment", alignment_name),
        )
        if v
    }


def _fail(exc: GiscError) -> None:
    typer.secho(f"gisc: {exc}", fg=typer.colors.RED, err=True)
    raise typer.Exit(exc.exit_code)


@app.command()
def ir(
    task: str = _TASK,
    alignment: str = _ALIGNMENT,
    utils: Optional[str] = _UTILS,
    flood: Optional[str] = _FLOOD,
    row: Optional[str] = _ROW,
    buffer_ft: float = _BUFFER,
    crs: str = _CRS,
    out: Optional[str] = _OUT,
    alignment_crs: Optional[str] = _ALN_CRS,
    utils_layer: Optional[str] = _UTILS_LAYER,
    flood_layer: Optional[str] = _FLOOD_LAYER,
    alignment_name: Optional[str] = _ALN_LAYER,
    probe: bool = typer.Option(
        True, "--probe/--no-probe",
        help="Read source headers for native CRS. --no-probe opens nothing at all.",
    ),
) -> None:
    """Compile the plan, print it, and exit. No adapter is executed."""
    try:
        plan = compile_task(
            task,
            alignment=alignment, utils=utils, flood=flood, row=row,
            buffer_ft=buffer_ft, crs=crs,
            out_dir=pathlib.Path(out or "./out/<run_id>"),
            layers=_layers(utils_layer, flood_layer, alignment_name),
            alignment_crs=alignment_crs,
            probe=probe,
        )
    except GiscError as exc:
        _fail(exc)
    typer.echo(plan.to_json().rstrip())


@app.command(name="compile")
def compile_cmd(
    task: str = _TASK,
    alignment: str = _ALIGNMENT,
    utils: Optional[str] = _UTILS,
    flood: Optional[str] = _FLOOD,
    row: Optional[str] = _ROW,
    buffer_ft: float = _BUFFER,
    crs: str = _CRS,
    out: Optional[str] = _OUT,
    alignment_crs: Optional[str] = _ALN_CRS,
    utils_layer: Optional[str] = _UTILS_LAYER,
    flood_layer: Optional[str] = _FLOOD_LAYER,
    alignment_name: Optional[str] = _ALN_LAYER,
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print only the output folder"),
) -> None:
    """Compile the plan and run it."""
    out_dir = pathlib.Path(out) if out else pathlib.Path("./out") / _run_id()
    try:
        plan = compile_task(
            task,
            alignment=alignment, utils=utils, flood=flood, row=row,
            buffer_ft=buffer_ft, crs=crs, out_dir=out_dir,
            layers=_layers(utils_layer, flood_layer, alignment_name),
            alignment_crs=alignment_crs,
        )
        write_plan(plan, out_dir)
        result = execute(plan, out_dir)
    except GiscError as exc:
        _fail(exc)

    # Rewrite the plan: reading resolved each source's real CRS.
    write_plan(plan, out_dir)

    summary = TASKS[task].summary(result, out_dir)
    (out_dir / "summary.md").write_text(summary, encoding="utf-8")
    result.provenance["outputs"]["summary.md"] = {"path": str((out_dir / "summary.md").resolve())}
    (out_dir / "provenance.json").write_text(
        json.dumps(result.provenance, indent=2, default=str) + "\n", encoding="utf-8"
    )

    if quiet:
        typer.echo(str(out_dir.resolve()))
    else:
        typer.echo(summary)
        typer.echo(f"\nwrote {out_dir.resolve()}")


@app.command()
def version() -> None:
    """Print the gisc version."""
    typer.echo(__version__)


def main() -> None:
    try:
        app()
    except GiscError as exc:  # anything that escaped a command
        typer.secho(f"gisc: {exc}", fg=typer.colors.RED, err=True)
        sys.exit(exc.exit_code)


if __name__ == "__main__":
    main()
