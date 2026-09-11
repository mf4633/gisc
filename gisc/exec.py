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
import shutil
import uuid
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
from gisc.stations import Stationing

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


# Artifacts gisc always writes, whatever the task.
ALWAYS_WRITES = ("plan.json", "provenance.json", "summary.md")


@dataclasses.dataclass
class Claim:
    """Ownership of an output folder, held for the length of one run.

    The previous run is moved aside rather than deleted, so a run that fails
    part way through -- a malformed geometry, an adapter that gives out on the
    third source -- can put it back. Deleting first and writing second means
    any failure after the delete costs the reader the answers they already had,
    and leaves the folder holding a plan.json with no provenance.json beside
    it: a shape gisc refuses to write into, so the next run cannot start
    either.
    """

    out_dir: pathlib.Path
    cleared: list[str]
    aside: pathlib.Path | None = None
    created: bool = False
    settled: bool = False

    def commit(self) -> None:
        """The run finished. Let go of the run it replaced."""
        if self.settled:
            return
        self.settled = True
        if self.aside is not None and self.aside.is_dir():
            shutil.rmtree(self.aside, ignore_errors=True)

    def rollback(self) -> None:
        """The run failed. Put the folder back the way it was found."""
        if self.settled:
            return
        self.settled = True
        if self.out_dir.is_dir():
            for entry in self.out_dir.iterdir():
                if entry.is_file():
                    entry.unlink()
        if self.aside is not None and self.aside.is_dir():
            for entry in self.aside.iterdir():
                entry.replace(self.out_dir / entry.name)
            self.aside.rmdir()
        elif self.created and self.out_dir.is_dir() and not any(self.out_dir.iterdir()):
            # gisc made this folder for a run that never happened.
            self.out_dir.rmdir()


def claim_out_dir(out_dir: pathlib.Path | str) -> Claim:
    """Take ownership of the output folder, setting a previous run aside.

    A run folder must describe exactly one run. Left alone, a second run with
    fewer inputs would leave the first run's ``flood.geojson`` sitting next to
    a ``provenance.json`` that never mentions it -- an output nobody can trace,
    which is the one thing gisc exists to prevent.

    Only files a previous gisc run recorded writing are moved. A folder gisc
    did not write is refused, so a mistyped --out never touches anyone's work.
    """
    out_dir = pathlib.Path(out_dir)
    if not out_dir.exists():
        out_dir.mkdir(parents=True)
        return Claim(out_dir=out_dir, cleared=[], created=True)

    entries = sorted(out_dir.iterdir(), key=lambda p: p.name)
    if not entries:
        return Claim(out_dir=out_dir, cleared=[])

    marker = out_dir / "provenance.json"
    if not marker.is_file():
        raise UsageError(
            f"{out_dir} is not empty and was not written by gisc. Point --out at an "
            "empty folder; gisc will not delete files it does not own."
        )
    try:
        previous = json.loads(marker.read_text(encoding="utf-8"))
        owned = set(previous.get("outputs", {})) | set(ALWAYS_WRITES)
    except (OSError, json.JSONDecodeError):
        owned = set(ALWAYS_WRITES)

    stray = [p.name for p in entries if p.name not in owned]
    if stray:
        shown = ", ".join(stray[:5]) + (" ..." if len(stray) > 5 else "")
        raise UsageError(
            f"{out_dir} holds a previous gisc run plus {len(stray)} file(s) it does "
            f"not account for ({shown}). Point --out at an empty folder."
        )

    # A sibling, so it is on the same filesystem as the folder it came from and
    # a restore is a rename rather than a copy that could itself fail.
    aside = out_dir.parent / f".gisc-rollback-{uuid.uuid4().hex[:8]}"
    try:
        aside.mkdir(parents=True)
    except OSError as exc:
        raise UsageError(
            f"cannot set aside the previous run in {out_dir.parent}: {exc}. gisc "
            "keeps a copy until the new run succeeds, so it needs to write there."
        ) from exc

    moved = []
    for entry in entries:
        if entry.is_file():
            entry.replace(aside / entry.name)
            moved.append(entry.name)
    return Claim(out_dir=out_dir, cleared=moved, aside=aside)


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

    # A centreline is one feature. Without this, pointing --alignment at a pipe
    # network merges 30 pipes into a single "centre line" and stations against it.
    if op.get("expect") == "one_feature" and len(gdf) != 1:
        raise UsageError(
            f"{sid!r} must be a single feature to act as a centreline, but "
            f"{source.ref} yielded {len(gdf)}. Name one with the layer option "
            f"(--alignment-name for LandXML)."
        )

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
    # Corners and centre of the bounding box, so the scale factor's drift over
    # what is being buffered is measured rather than assumed away. The corners
    # need not lie on the geometry: they bound it, so the range comes out at
    # least as wide as the truth, which is the safe direction to be wrong in.
    x0, y0, x1, y1 = merged.bounds
    extent = [(x0, y0), (x1, y1), ((x0 + x1) / 2.0, (y0 + y1) / 2.0)]
    conv = units.buffer_distance(
        gdf.crs, float(op["dist_ft"]), (rep.x, rep.y), extent=extent
    )
    ctx["buffer"] = conv
    poly = merged.buffer(
        conv["distance_in_crs_units"], quad_segs=conv["quad_segs"]
    )
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
        # Decided per feature, not per layer: a length for linework, an area
        # for polygons, neither for a point. A mixed layer must not hand its
        # lines a zero-acre area.
        lengths, areas = [], []
        for geom in inside:
            polygonal = geom is not None and geom.geom_type in (
                "Polygon", "MultiPolygon", "GeometryCollection"
            )
            areas.append(round(units.geodesic_acres(geom), 4) if polygonal else None)
            linear = geom is not None and "Line" in geom.geom_type
            lengths.append(round(units.geodesic_ft(geom), 2) if linear else None)
        if any(v is not None for v in lengths):
            hits["overlap_ft"] = lengths
        if any(v is not None for v in areas):
            hits["overlap_ac"] = areas

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
    # A station is not a distance. If the alignment carries station equations,
    # the drawing's stations jump and gisc has to jump with them.
    stationing = Stationing.from_json(
        sta_start,
        ref[Stationing.COLUMN].iloc[0] if Stationing.COLUMN in ref.columns else None,
    )
    unit = ref.crs.axis_info[0].unit_name

    raws, raw_begins, raw_ends, offsets, sides = [], [], [], [], []
    for geom in target.geometry:
        if geom is None or geom.is_empty:
            for col in (raws, raw_begins, raw_ends, offsets, sides):
                col.append(None)
            continue
        pa, pg = nearest_points(line, geom)
        d_along = line.project(pa)
        # Stations stay in the design unit; converting them would contradict
        # the drawing. Offsets are reported in international feet.
        raws.append(stationing.raw(d_along))
        offsets.append(units.grid_to_ft(pa.distance(pg), ref.crs))
        sides.append(_side(line, d_along, pa, pg))

        # How far along the alignment the feature reaches, so a polygon is not
        # reduced to one misleading station. Named for what it is: this must not
        # shadow ``along``, which is the id of the centreline being measured
        # against and is reported in provenance.
        reach = [line.project(Point(xy)) for xy in shapely.get_coordinates(geom)]
        raw_begins.append(stationing.raw(min(reach)))
        raw_ends.append(stationing.raw(max(reach)))

    stations = [stationing.display(v) for v in raws]
    begins = [stationing.display(v) for v in raw_begins]
    ends = [stationing.display(v) for v in raw_ends]
    regions = [None if v is None else stationing.region_of(v).index for v in raws]
    reg_b = [None if v is None else stationing.region_of(v).index for v in raw_begins]
    reg_e = [None if v is None else stationing.region_of(v).index for v in raw_ends]

    hit = env[src].copy()  # keep the analysis-CRS geometry
    hit["sta"] = [None if v is None else round(v, 2) for v in stations]
    hit["sta_begin"] = [None if v is None else round(v, 2) for v in begins]
    hit["sta_end"] = [None if v is None else round(v, 2) for v in ends]
    hit["sta_label"] = [
        _station_range(b, e, rb, re_, cb, ce)
        for b, e, rb, re_, cb, ce in zip(begins, ends, raw_begins, raw_ends, reg_b, reg_e)
    ]
    hit["offset_ft"] = [None if o is None else round(o, 2) for o in offsets]
    hit["side"] = sides
    if stationing.equated:
        # Only when they exist, so the ordinary case keeps a clean table. With
        # an overlap equation a station names two points, and the region is the
        # rest of the address; the raw station is what ties it back to geometry.
        hit["sta_region"] = regions
        hit["sta_raw"] = [None if v is None else round(v, 2) for v in raws]
    env[out] = hit
    return {
        "along": along,
        "measured_in": ref.crs.to_string(),
        "measured_in_source": where,
        "sta_unit": unit,
        "sta_start": sta_start,
        "offset_unit": "international foot",
        "stationing": stationing.to_dict(),
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


def _station_range(begin: float | None, end: float | None,
                   raw_begin: float | None = None, raw_end: float | None = None,
                   region_begin: int | None = None,
                   region_end: int | None = None) -> str | None:
    """A single station for a crossing, a range for anything with extent.

    Extent is judged on the raw stations when they are known, because across a
    station equation the displayed stations are not a length: a 50 ft pipe can
    read 14+00 to 20+50. When a feature does straddle an equation the label
    names both regions, since neither station alone locates it.
    """
    if begin is None or end is None:
        return None
    extent = (end - begin) if raw_begin is None or raw_end is None else (raw_end - raw_begin)
    if extent < 1.0:
        return _station_label((begin + end) / 2.0)
    if region_begin is not None and region_begin != region_end:
        return (
            f"{_station_label(begin)} (R{region_begin}) - "
            f"{_station_label(end)} (R{region_end})"
        )
    return f"{_station_label(begin)} - {_station_label(end)}"


def _station_label(sta: float | None) -> str | None:
    """1200.0 -> "12+00.00".

    Done in integer hundredths. Splitting the float first lets 1199.99999
    print as "11+100.00", which is not a station.
    """
    if sta is None:
        return None
    sign = "-" if sta < 0 else ""
    whole, rem = divmod(int(round(abs(sta) * 100)), 10_000)
    return f"{sign}{whole}+{rem / 100:05.2f}"


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
