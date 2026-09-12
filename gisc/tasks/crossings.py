"""``alignment.crossings`` -- where does each utility cross the centreline.

A second task, written to find out whether the IR meant what it says. The
corridor task asks *what is near the alignment*; this one asks *where does
something cross it*, which is a different shape of question: there is no
corridor to buffer, and the answer is a point that exists in none of the
inputs -- it is made by the intersection itself.

Compiles to: read every source, reproject to one analysis CRS, intersect
against the centreline, station the crossings, reproject to WGS 84, write.
"""

from __future__ import annotations

import pathlib
from typing import Any

from gisc import adapters
from gisc.errors import UsageError
from gisc.ir import Plan, Source

TASK = "alignment.crossings"


def compile_plan(
    *,
    alignment: str,
    utils: str | None = None,
    flood: str | None = None,
    row: str | None = None,
    buffer_ft: float | None = None,  # accepted and ignored: nothing here buffers
    crs: str = "EPSG:3857",
    out_dir: pathlib.Path | str = "./out",
    layers: dict[str, str] | None = None,
    alignment_crs: str | None = None,
    probe: bool = True,
) -> Plan:
    """Build the IR. With ``probe=False`` no file is opened at all."""
    if not (utils or flood or row):
        raise UsageError(
            "alignment.crossings needs something to cross the alignment: pass "
            "--utils, --flood or --row."
        )

    layers = layers or {}
    out_dir = pathlib.Path(out_dir)
    # No buffer_ft: this task never buffers, so it does not carry a distance.
    plan = Plan(task=TASK, crs=crs)
    notes: list[str] = []

    wanted = [("alignment", alignment), ("utilities", utils), ("flood", flood), ("row", row)]
    for sid, ref in wanted:
        if not ref:
            continue
        kind = adapters.detect_kind(ref)
        override = alignment_crs if sid == "alignment" else None
        src = Source(id=sid, kind=kind, ref=ref, layer=layers.get(sid))
        if probe:
            info = adapters.describe(ref, kind=kind, layer=src.layer, crs_override=override)
            src.native_crs = info.get("native_crs")
            src.crs_source = info.get("crs_source")
            src.layer = info.get("layer") or src.layer
            if info.get("stub"):
                notes.append(
                    f"source {sid!r} uses the {kind} adapter, which is a stub; "
                    "executing this plan will fail with the request it would make."
                )
            if info.get("native_crs") is None:
                notes.append(f"source {sid!r} declares no CRS; execution will refuse it.")
        plan.sources.append(src)

    ids = [s.id for s in plan.sources]

    for sid in ids:
        op: dict[str, Any] = {"op": "read", "src": sid}
        if sid == "alignment":
            op["expect"] = "one_feature"
            if alignment_crs:
                op["crs_override"] = alignment_crs
        plan.ops.append(op)
    for sid in ids:
        plan.ops.append({"op": "reproject", "src": sid, "to": crs})

    pairs = [
        ("utility_crossings", "utilities", "utility_crossings.geojson"),
        ("flood_crossings", "flood", "flood_crossings.geojson"),
        ("row_crossings", "row", "row_crossings.geojson"),
    ]
    for result, sid, filename in pairs:
        if sid not in ids:
            continue
        # No buffer: the centreline itself is the thing being crossed, and the
        # answer wanted is the crossing, not the thing that crosses.
        plan.ops.append({
            "op": "intersect", "a": sid, "b": "alignment",
            "out": result, "geometry": "intersection",
        })
        plan.ops.append(
            {"op": "sample", "src": result, "along": "alignment", "out": result,
             "measure_in": "native"}
        )
        plan.ops.append({"op": "reproject", "src": result, "to": "EPSG:4326"})
        dest = (out_dir / filename).as_posix()
        plan.ops.append({"op": "write", "src": result, "to": dest})
        plan.outputs[result] = dest

    plan.notes = notes
    return plan


def summary(result, out_dir: pathlib.Path) -> str:
    plan, prov = result.plan, result.provenance
    lines = [
        f"# {plan.task}",
        "",
        f"- run: `{out_dir.name}`  ({prov['started_at']} -> {prov['finished_at']})",
        f"- analysis CRS: **{prov['crs_analysis']}**   output CRS: {prov['crs_out']}",
        "",
        "## Crossings",
        "",
    ]
    for name, path in plan.outputs.items():
        gdf = result.env.get(name)
        n = 0 if gdf is None else len(gdf)
        lines.append(f"- **{n}** in `{pathlib.Path(path).name}`")
        if n:
            lines += ["", "| name | station | geometry |", "|---|---|---|"]
            for _, r in gdf.iterrows():
                label = r.get("name") or r.get("zone") or ""
                lines.append(
                    f"| {label} | {r.get('sta_label')} | {r.geometry.geom_type} |"
                )
            lines.append("")
    return "\n".join(lines)
