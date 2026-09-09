"""``corridor.conflicts`` -- what is inside N feet of this alignment.

Compiles to: read every source, reproject to one analysis CRS, buffer the
alignment, intersect, station the hits, reproject to WGS 84, write.
"""

from __future__ import annotations

import pathlib
from typing import Any

from gisc import adapters
from gisc.errors import UsageError
from gisc.ir import Plan, Source

TASK = "corridor.conflicts"


def compile_plan(
    *,
    alignment: str,
    utils: str | None = None,
    flood: str | None = None,
    row: str | None = None,
    buffer_ft: float = 15.0,
    crs: str = "EPSG:3857",
    out_dir: pathlib.Path | str = "./out",
    layers: dict[str, str] | None = None,
    alignment_crs: str | None = None,
    probe: bool = True,
) -> Plan:
    """Build the IR. With ``probe=False`` no file is opened at all."""
    if not (utils or flood or row):
        raise UsageError(
            "corridor.conflicts needs something to look for: pass --utils, "
            "--flood or --row."
        )
    if buffer_ft <= 0:
        raise UsageError(f"--buffer-ft must be positive, got {buffer_ft}")

    layers = layers or {}
    out_dir = pathlib.Path(out_dir)
    plan = Plan(task=TASK, crs=crs, buffer_ft=float(buffer_ft))
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

    plan.ops.append(
        {"op": "buffer", "src": "alignment", "dist_ft": float(buffer_ft), "out": "corridor"}
    )

    # (result name, source id, output filename)
    pairs = [
        ("conflicts", "utilities", "conflicts.geojson"),
        ("flood_hits", "flood", "flood.geojson"),
        ("row_hits", "row", "row.geojson"),
    ]
    for result, sid, filename in pairs:
        if sid not in ids:
            continue
        plan.ops.append({"op": "intersect", "a": sid, "b": "corridor", "out": result})
        plan.ops.append(
            {"op": "sample", "src": result, "along": "alignment", "out": result,
             "measure_in": "native"}
        )
        plan.ops.append({"op": "reproject", "src": result, "to": "EPSG:4326"})
        # Forward slashes: the IR is portable, even when compiled on Windows.
        dest = (out_dir / filename).as_posix()
        plan.ops.append({"op": "write", "src": result, "to": dest})
        plan.outputs[result] = dest

    plan.notes = notes
    return plan


# --------------------------------------------------------------------------


def _table(gdf, columns: list[tuple[str, str]]) -> list[str]:
    present = [(c, h) for c, h in columns if c in gdf.columns]
    if not present:
        return []
    lines = [
        "| " + " | ".join(h for _, h in present) + " |",
        "|" + "|".join("---" for _ in present) + "|",
    ]
    for _, r in gdf.iterrows():
        cells = []
        for c, _h in present:
            v = r[c]
            cells.append("" if v is None or v != v else str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def summary(result, out_dir: pathlib.Path) -> str:
    """A human-readable account of the run. Counts, CRS, and what was touched."""
    plan, prov = result.plan, result.provenance
    buf = prov.get("buffer") or {}
    lines = [
        f"# {plan.task}",
        "",
        f"- run: `{out_dir.name}`  ({prov['started_at']} -> {prov['finished_at']})",
        f"- analysis CRS: **{prov['crs_analysis']}**   output CRS: {prov['crs_out']}",
        f"- buffer: **{plan.buffer_ft:g} ft** on the ground "
        f"= {buf.get('distance_in_crs_units', float('nan')):.4f} {buf.get('crs_unit', '?')} "
        f"(point scale factor {buf.get('point_scale_factor', float('nan')):.6f})",
        "",
        "## Sources read",
        "",
        "| id | kind | CRS in | CRS source | features | ref |",
        "|---|---|---|---|---|---|",
    ]
    for sid, s in prov["sources"].items():
        lines.append(
            f"| {sid} | {s['kind']} | {s.get('native_crs')} | {s.get('crs_source')} "
            f"| {s.get('features_in')} | `{s['ref']}` |"
        )

    lines += ["", "## Findings", ""]
    labels = {
        "conflicts": "utilities within the corridor",
        "flood_hits": "flood polygons intersecting the corridor",
        "row_hits": "ROW/parcels intersecting the corridor",
    }
    for name, path in plan.outputs.items():
        gdf = result.env.get(name)
        total = result.provenance
        candidates = next(
            (o["result"]["candidates"] for o in total["ops"]
             if o["op"] == "intersect" and o.get("out") == name),
            None,
        )
        n = 0 if gdf is None else len(gdf)
        lines.append(
            f"- **{n} of {candidates} {labels.get(name, name)}** -> "
            f"`{pathlib.Path(path).name}`"
        )
        if n:
            lines.append("")
            lines += _table(
                gdf,
                [
                    ("name", "name"),
                    ("zone", "zone"),
                    ("sta_label", "station"),
                    ("offset_ft", "offset (ft)"),
                    ("side", "side"),
                    ("overlap_ft", "in corridor (ft)"),
                    ("overlap_ac", "in corridor (ac)"),
                ],
            )
            lines.append("")

    lines += [
        "",
        "## What was touched",
        "",
        "gisc stored nothing. It read the sources above and wrote only:",
        "",
    ]
    for fname, o in prov["outputs"].items():
        n = o.get("features")
        if n is None:
            lines.append(f"- `{fname}`")
        else:
            lines.append(f"- `{fname}` -- {n} feature{'' if n == 1 else 's'}, {o['crs']}")
    lines += ["- `plan.json`, `provenance.json`, `summary.md`", ""]

    if plan.notes:
        lines += ["## Notes", ""] + [f"- {n}" for n in plan.notes] + [""]

    aln = prov["sources"].get("alignment", {})
    if aln.get("notes"):
        lines += ["## Alignment notes", ""] + [f"- {n}" for n in aln["notes"]] + [""]

    if str(prov["crs_analysis"]).upper() == "EPSG:3857":
        lines += [
            "> Analysis ran in EPSG:3857, which is not a survey-grade projection. "
            f"gisc corrected the buffer for a point scale factor of "
            f"{buf.get('point_scale_factor', float('nan')):.4f}, so the {plan.buffer_ft:g} ft "
            "is a true ground distance -- but for a deliverable, rerun with your "
            "state plane zone via `--crs`.",
            "",
        ]
    return "\n".join(lines)
