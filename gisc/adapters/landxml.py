"""LandXML -> one alignment centerline as a LineString.

Deliberately small. It understands ``Line`` and ``Curve`` (circular arc),
which is what an ordinary roadway or utility centerline is made of. Anything
else -- spirals, irregular lines, a file with no CRS -- fails with a message
that names the file and the element, rather than quietly producing a shape
that is almost right.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from typing import Any

import geopandas as gpd
from pyproj import CRS
from shapely.geometry import LineString

from gisc.adapters._common import file_provenance, now, resolve
from gisc.errors import AdapterError, MissingCRSError

# Chord tolerance when flattening an arc, in CRS units (ft or m).
ARC_TOLERANCE = 0.01


def _tag(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _find(parent: ET.Element, name: str) -> ET.Element | None:
    for el in parent:
        if _tag(el) == name:
            return el
    return None


def _findall(parent: ET.Element, name: str) -> list[ET.Element]:
    return [el for el in parent.iter() if _tag(el) == name]


def _root(path) -> ET.Element:
    try:
        return ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise AdapterError(f"{path}: not valid XML: {exc}") from exc


def _point(el: ET.Element | None, path, what: str) -> tuple[float, float]:
    """LandXML point text is ``northing easting [elevation]`` -- N first."""
    if el is None or not (el.text or "").strip():
        raise AdapterError(f"{path}: <{what}> is missing or empty")
    parts = (el.text or "").split()
    if len(parts) < 2:
        raise AdapterError(f"{path}: <{what}> needs at least 2 ordinates, got {el.text!r}")
    try:
        northing, easting = float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise AdapterError(f"{path}: <{what}> is not numeric: {el.text!r}") from exc
    return easting, northing


def _read_crs(root: ET.Element, path, crs_override: str | None) -> tuple[str, str]:
    if crs_override:
        return crs_override, "user_override"
    cs = _find(root, "CoordinateSystem")
    if cs is not None:
        epsg = cs.get("epsgCode")
        if epsg:
            return f"EPSG:{epsg}", "declared"
        for attr in ("ogcWktCode", "horizontalCoordinateSystemName", "desc"):
            val = cs.get(attr)
            if not val:
                continue
            try:
                return CRS.from_user_input(val).to_string(), f"declared_by_{attr}"
            except Exception:  # noqa: BLE001 - a name we cannot resolve is not a CRS
                continue
    raise MissingCRSError(
        f"{path}: LandXML declares no usable CRS (<CoordinateSystem epsgCode=...> is "
        "missing or unrecognised). gisc will not guess. Re-export with a coordinate "
        "system, or pass the EPSG code explicitly."
    )


def _arc(start, center, end, rot: str, path) -> list[tuple[float, float]]:
    (sx, sy), (cx, cy), (ex, ey) = start, center, end
    r = math.hypot(sx - cx, sy - cy)
    if r <= 0:
        raise AdapterError(f"{path}: <Curve> has zero radius")
    a0 = math.atan2(sy - cy, sx - cx)
    a1 = math.atan2(ey - cy, ex - cx)
    if rot not in ("cw", "ccw"):
        raise AdapterError(f"{path}: <Curve> needs rot='cw' or rot='ccw', got {rot!r}")
    if rot == "ccw":
        sweep = (a1 - a0) % (2 * math.pi)
    else:
        sweep = -((a0 - a1) % (2 * math.pi))

    step = 2 * math.acos(max(-1.0, min(1.0, 1 - ARC_TOLERANCE / r)))
    n = max(2, math.ceil(abs(sweep) / max(step, 1e-6)))
    pts = []
    for i in range(1, n + 1):  # the start point is already on the polyline
        a = a0 + sweep * i / n
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    pts[-1] = (ex, ey)  # land exactly on the declared endpoint
    return pts


def _centerline(alignment: ET.Element, path) -> tuple[LineString, list[str]]:
    geom = _find(alignment, "CoordGeom")
    if geom is None:
        raise AdapterError(f"{path}: <Alignment> has no <CoordGeom>")

    coords: list[tuple[float, float]] = []
    notes: list[str] = []

    for el in geom:
        kind = _tag(el)
        if kind == "Line":
            start = _point(_find(el, "Start"), path, "Line/Start")
            end = _point(_find(el, "End"), path, "Line/End")
            seg = [start, end]
        elif kind == "Curve":
            start = _point(_find(el, "Start"), path, "Curve/Start")
            center = _point(_find(el, "Center"), path, "Curve/Center")
            end = _point(_find(el, "End"), path, "Curve/End")
            seg = [start, *_arc(start, center, end, (el.get("rot") or "").lower(), path)]
            notes.append(
                f"flattened a <Curve> to {len(seg)} chords at {ARC_TOLERANCE} CRS units"
            )
        else:
            raise AdapterError(
                f"{path}: <CoordGeom> contains <{kind}>, which gisc does not parse. "
                "Only <Line> and <Curve> are supported; re-export the alignment as "
                "chords, or hand gisc a GeoJSON LineString instead."
            )

        if coords:
            gap = math.dist(coords[-1], seg[0])
            if gap > 0.01:
                raise AdapterError(
                    f"{path}: <CoordGeom> is discontinuous -- <{kind}> starts {gap:.4f} "
                    f"CRS units from the previous element's end at {coords[-1]}. "
                    "gisc will not bridge a gap it was not asked to bridge."
                )
            if gap > 1e-9:
                # Snapping is what a tolerance means, but a discarded position
                # is still a change to the geometry, so it goes on the record.
                notes.append(
                    f"closed a {gap:.4f} CRS-unit gap before <{kind}> by snapping to "
                    "the previous element's end"
                )
            coords.extend(seg[1:])
        else:
            coords.extend(seg)

    if len(coords) < 2:
        raise AdapterError(f"{path}: <CoordGeom> produced fewer than 2 points")
    return LineString(coords), notes


def _pick_alignment(root: ET.Element, path, name: str | None) -> ET.Element:
    found = _findall(root, "Alignment")
    if not found:
        raise AdapterError(f"{path}: no <Alignment> element")
    names = ", ".join(a.get("name", "(unnamed)") for a in found)
    if name is None:
        if len(found) > 1:
            raise AdapterError(
                f"{path}: has {len(found)} alignments ({names}); name one with layer=<name>"
            )
        return found[0]
    for a in found:
        if a.get("name") == name:
            return a
    raise AdapterError(f"{path}: no <Alignment name={name!r}>; available: {names}")


def describe(ref: str, layer: str | None = None, crs_override: str | None = None) -> dict[str, Any]:
    path = resolve(ref)
    root = _root(path)
    crs, source = _read_crs(root, path, crs_override)
    alignment = _pick_alignment(root, path, layer)
    units = _find(root, "Units")
    declared_unit = None
    if units is not None and len(units):
        declared_unit = units[0].get("linearUnit")
    return {
        "kind": "landxml",
        "ref": str(path),
        "layer": alignment.get("name"),
        "native_crs": crs,
        "crs_source": source,
        "sta_start": float(alignment.get("staStart") or 0.0),
        "length_declared": float(alignment.get("length") or 0.0),
        "landxml_linear_unit": declared_unit,
    }


def _unit_mismatch(declared: str | None, unit_name: str) -> bool:
    if not declared:
        return False
    d = declared.lower().replace(" ", "").replace("_", "")
    u = unit_name.lower().replace(" ", "").replace("_", "")
    if "foot" in d or "feet" in d:
        return "foot" not in u and "feet" not in u
    if "met" in d:
        return "met" not in u
    return False


def read(ref: str, layer: str | None = None, crs_override: str | None = None):
    info = describe(ref, layer=layer, crs_override=crs_override)
    path = resolve(ref)
    root = _root(path)
    alignment = _pick_alignment(root, path, layer)
    line, notes = _centerline(alignment, path)

    crs = CRS.from_user_input(info["native_crs"])
    unit = crs.axis_info[0].unit_name
    if _unit_mismatch(info.get("landxml_linear_unit"), unit):
        notes.append(
            f"LandXML declares linearUnit={info['landxml_linear_unit']!r} but "
            f"{crs.to_string()} uses {unit}; gisc used the CRS unit."
        )

    length = line.length
    if info["length_declared"] and abs(length - info["length_declared"]) > 0.1:
        notes.append(
            f"declared length {info['length_declared']:.4f} vs computed {length:.4f} "
            f"({unit}); gisc reports the computed value."
        )

    gdf = gpd.GeoDataFrame(
        {
            "name": [info["layer"]],
            "sta_start": [info["sta_start"]],
            "length": [length],
        },
        geometry=[line],
        crs=crs,
    )
    prov = {
        **info,
        **file_provenance(path),
        "read_at": now(),
        "features_in": 1,
        "vertices": len(line.coords),
        "length_computed": length,
        "geometry_types": ["LineString"],
        "filter": f"Alignment[name={info['layer']!r}]/CoordGeom",
        "notes": notes,
    }
    return gdf, prov
