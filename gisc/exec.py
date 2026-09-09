"""Execute a compiled plan.

The engine is generic over the op set in :mod:`gisc.ir`. That is deliberate:
``plan.json`` is the program, not a description of one. Nothing here is
persisted anywhere except the output folder the caller named.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
from typing import Any

import geopandas as gpd
import pandas as pd
import shapely
from shapely.geometry import Point
from shapely.ops import nearest_points

from gisc import __version__, adapters, units
from gisc.adapters._common import now
from gisc.errors import MissingCRSError, UsageError
from gisc.ir import Plan, validate

OUT_CRS = "EPSG:4326"  # RFC 7946. Analysis CRS is recorded in provenance.

# Every source is also kept as it was read, before any reproject, under this
# prefix. Stationing is measured there: a station is a grid distance in the
# design coordinate system, which is what the plan sheet says.
NATIVE = "__native__"


@dataclasses.dataclass
class Result:
    plan: Plan
    env: dict[str, gpd.GeoDataFrame]
    provenance: dict[str, Any]

    def frame(self, name: str) -> gpd.GeoDataFrame:
        return self.env[name]

    def count(self, name: str) -> int:
        return int(len(self.env[name])) if name in self.env else 0


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _require_crs(gdf: gpd.GeoDataFrame, name: str) -> None:
    if gdf.crs is None:
        raise MissingCRSError(f"{name!r} has no CRS at this point in the plan")


# --------------------------------------------------------------------------
# ops


def _op_read(op, plan, env, prov, ctx):
    sid = op["src"]
    source = plan.source(sid)
    gdf, sprov = adapters.read(
        source.ref, kind=source.kind, layer=source.layer,
        crs_override=op.get("crs_override"),
    )
    _require_crs(gdf, sid)
    gdf = gdf.copy()
    gdf["gisc_src"] = sid
    gdf["gisc_fid"] = range(len(gdf))
    env[sid] = gdf
    env[NATIVE + sid] = gdf
    # The plan was compiled from a header probe; record what we actually got.
    source.native_crs = gdf.crs.to_string()
    source.crs_source = sprov.get("crs_source", source.crs_source)
    prov["sources"][sid] = sprov
    return {"features": int(len(gdf)), "crs": gdf.crs.to_string()}


def _op_reproject(op, plan, env, prov, ctx):
    src, to = op["src"], op["to"]
    gdf = env[src]
    _require_crs(gdf, src)
    before = gdf.crs.to_string()
    env[op.get("out", src)] = gdf.to_crs(to)
    return {"from": before, "to": to}


def _op_buffer(op, plan, env, prov, ctx):
    src, out = op["src"], op["out"]
    gdf = env[src]
    _require_crs(gdf, src)
    merged = gdf.geometry.union_all()
    rep = merged.representative_point()
    conv = units.buffer_distance(gdf.crs, float(op["dist_ft"]), (rep.x, rep.y))
    ctx["buffer"] = conv
    poly = merged.buffer(conv["distance_in_crs_units"])
    env[out] = gpd.GeoDataFrame(
        {"gisc_src": [f"buffer({src})"], "gisc_fid": [0], "dist_ft": [conv["dist_ft"]]},
        geometry=[poly],
        crs=gdf.crs,
    )
    return conv


def _op_intersect(op, plan, env, prov, ctx):
    a, b, out = op["a"], op["b"], op["out"]
    left, right = env[a], env[b]
    _require_crs(left, a)
    _require_crs(right, b)
    if left.crs != right.crs:
        raise UsageError(
            f"intersect({a}, {b}): CRS mismatch {left.crs.to_string()} vs "
            f"{right.crs.to_string()}; the plan is missing a reproject"
        )
    mask = right.geometry.union_all()
    hits = left[left.geometry.intersects(mask)].copy()

    # Geometry is the source geometry, unclipped: the answer to "which pipe"
    # is the whole pipe. How much of it is inside is an attribute.
    if len(hits):
        # Measured geodesically on WGS 84, so how much of a pipe sits in the
        # corridor does not depend on which analysis CRS was chosen. Doing it
        # in EPSG:3857 costs ~0.2% on an east-west run, because Web Mercator
        # is anisotropic on the ellipsoid.
        inside = hits.geometry.intersection(mask).to_crs(OUT_CRS)
        # A length for linework, an area for polygons. The perimeter of a
        # clipped polygon is not a number anyone should act on, so it is not
        # reported at all.
        if hits.geom_type.isin(["Polygon", "MultiPolygon"]).any():
            hits["overlap_ac"] = [round(units.geodesic_acres(g), 4) for g in inside]
        else:
            hits["overlap_ft"] = [round(units.geodesic_ft(g), 2) for g in inside]
    env[out] = hits
    return {
        "predicate": "intersects",
        "geometry": "source (unclipped)",
        "candidates": int(len(left)),
        "hits": int(len(hits)),
    }


def _op_sample(op, plan, env, prov, ctx):
    """Station and offset of each feature along a reference centerline.

    ``measure_in: "native"`` measures in the centerline's own design CRS, so
    the stations gisc reports are the stations on the drawing.
    """
    src, along, out = op["src"], op["along"], op["out"]
    where = op.get("measure_in", "analysis")

    if where == "native" and NATIVE + along in env:
        ref = env[NATIVE + along]
    else:
        ref = env[along]
        where = "analysis"
    _require_crs(ref, along)

    target = env[src]
    _require_crs(target, src)
    if target.crs != ref.crs:
        target = target.to_crs(ref.crs)

    line = ref.geometry.union_all()
    sta_start = float(ref["sta_start"].iloc[0]) if "sta_start" in ref.columns else 0.0
    unit = ref.crs.axis_info[0].unit_name

    stations, begins, ends, offsets, sides = [], [], [], [], []
    for geom in target.geometry:
        if geom is None or geom.is_empty:
            for col in (stations, begins, ends, offsets, sides):
                col.append(None)
            continue
        pa, pg = nearest_points(line, geom)
        d_along = line.project(pa)
        # Stations stay in the design unit; converting them would contradict
        # the drawing. Offsets are reported in feet.
        stations.append(sta_start + d_along)
        offsets.append(units.grid_to_ft(pa.distance(pg), ref.crs))
        sides.append(_side(line, d_along, pa, pg))

        # How far along the alignment the feature reaches, so a polygon is not
        # reduced to one misleading station.
        along = [line.project(Point(xy)) for xy in shapely.get_coordinates(geom)]
        begins.append(sta_start + min(along))
        ends.append(sta_start + max(along))

    hit = env[src].copy()  # keep the analysis-CRS geometry
    hit["sta"] = [None if v is None else round(v, 2) for v in stations]
    hit["sta_begin"] = [None if v is None else round(v, 2) for v in begins]
    hit["sta_end"] = [None if v is None else round(v, 2) for v in ends]
    hit["sta_label"] = [_station_range(b, e) for b, e in zip(begins, ends)]
    hit["offset_ft"] = [None if o is None else round(o, 2) for o in offsets]
    hit["side"] = sides
    env[out] = hit
    return {
        "along": along,
        "measured_in": ref.crs.to_string(),
        "measured_in_source": where,
        "sta_unit": unit,
        "sta_start": sta_start,
        "sampled": int(len(hit)),
    }


def _side(line, d_along: float, pa: Point, pg: Point) -> str | None:
    """L or R looking up-station. Returns None when the point is on the line."""
    eps = max(line.length * 1e-6, 1e-6)
    p0 = line.interpolate(max(0.0, d_along - eps))
    p1 = line.interpolate(min(line.length, d_along + eps))
    tx, ty = p1.x - p0.x, p1.y - p0.y
    vx, vy = pg.x - pa.x, pg.y - pa.y
    cross = tx * vy - ty * vx
    if abs(cross) < 1e-12:
        return "CL"  # on the centerline, not missing
    return "L" if cross > 0 else "R"


def _station_range(begin: float | None, end: float | None) -> str | None:
    """A single station for a crossing, a range for anything with extent."""
    if begin is None or end is None:
        return None
    if end - begin < 1.0:
        return _station_label((begin + end) / 2.0)
    return f"{_station_label(begin)} - {_station_label(end)}"


def _station_label(sta: float | None) -> str | None:
    """1200.0 -> "12+00.00". Round before splitting, or 1199.9999 prints 11+100.00."""
    if sta is None:
        return None
    sign = "-" if sta < 0 else ""
    whole, rem = divmod(round(abs(sta), 2), 100.0)
    if round(rem, 2) >= 100.0:  # the rounding carried
        whole, rem = whole + 1, 0.0
    return f"{sign}{int(whole)}+{rem:05.2f}"


def _op_write(op, plan, env, prov, ctx):
    src = op["src"]
    dest = pathlib.Path(op["to"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    gdf = env[src]
    _require_crs(gdf, src)

    if len(gdf):
        gdf.to_file(dest, driver="GeoJSON")
    else:
        # geopandas cannot write a typed empty layer; emit a valid empty FC.
        dest.write_text(
            json.dumps({"type": "FeatureCollection", "features": []}, indent=2) + "\n"
        )

    entry = {
        "path": str(dest.resolve()),
        "crs": gdf.crs.to_string(),
        "features": int(len(gdf)),
        "sha256": _sha256(dest),
        "derived_from": sorted({str(v) for v in gdf.get("gisc_src", pd.Series(dtype=str))}),
    }
    prov["outputs"][dest.name] = entry
    return {"path": entry["path"], "features": entry["features"]}


_DISPATCH = {
    "read": _op_read,
    "reproject": _op_reproject,
    "buffer": _op_buffer,
    "intersect": _op_intersect,
    "sample": _op_sample,
    "write": _op_write,
}


# --------------------------------------------------------------------------


def execute(plan: Plan, out_dir: pathlib.Path | str) -> Result:
    """Run every op in order. Any failure propagates; nothing is swallowed."""
    validate(plan)
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    prov: dict[str, Any] = {
        "gisc_version": __version__,
        "task": plan.task,
        "started_at": now(),
        "crs_analysis": plan.crs,
        "crs_out": OUT_CRS,
        "buffer_ft": plan.buffer_ft,
        "stateless": "gisc read the sources below and wrote only this folder",
        "sources": {},
        "ops": [],
        "outputs": {},
    }
    env: dict[str, gpd.GeoDataFrame] = {}
    ctx: dict[str, Any] = {}

    for i, op in enumerate(plan.ops):
        detail = _DISPATCH[op["op"]](op, plan, env, prov, ctx)
        prov["ops"].append({"i": i, **op, "result": detail})

    prov["buffer"] = ctx.get("buffer")
    prov["finished_at"] = now()
    return Result(plan=plan, env=env, provenance=prov)
