"""LandXML -> an alignment centerline, or a pipe network.

Two subjects, because a Civil 3D project exports both and a corridor check
needs both: ``<Alignment>`` is the centreline you measure from,
``<PipeNetwork>`` is the buried utility you are looking for.

Alignments understand ``Line``, ``Curve`` (circular arc) and ``Spiral``
(clothoid), which is what an ordinary roadway or utility centreline is made
of. Anything else -- a non-clothoid transition, an irregular line, a file
with no CRS -- fails with a message that names the file and the element,
rather than quietly producing a shape that is almost right.

``<StaEquation>`` is read, because ignoring one is the quietest way to be
wrong that this format offers: the geometry stays perfect and every station
past the equation is off by the size of the equation. See
:mod:`gisc.stations`.

Pipe geometry is not in the ``<Pipe>`` element. A pipe names the structures at
its ends and the coordinates live on those, so a pipe whose structure is
missing from the file has no geometry gisc can vouch for. It is left out and
listed in provenance, never straight-lined between guesses.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from typing import Any

import geopandas as gpd
from pyproj import CRS
from shapely.geometry import LineString, Point

from gisc.adapters._common import file_provenance, now, resolve
from gisc.errors import AdapterError, MissingCRSError
from gisc.stations import Equation, Stationing

# Chord tolerance when flattening an arc or a spiral, in CRS units (ft or m).
ARC_TOLERANCE = 0.01

# How far an integrated clothoid may land from the ``<End>`` the file declares
# before gisc stops believing the file. A correct clothoid closes to well under
# a thousandth of a foot; anything near this means the length, the radii, the
# rotation and the endpoints do not describe the same curve, and gisc would be
# picking which of them to disbelieve.
SPIRAL_CLOSURE_TOLERANCE = 0.05

# Integration panels per emitted chord. The chord count already satisfies
# ARC_TOLERANCE geometrically; these make the quadrature error irrelevant
# beside it.
SPIRAL_SUBSTEPS = 8


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


def _arc(start, center, end, rot: str, path):
    """Flatten a circular arc to chords. Returns the points and the exit heading."""
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
    # Exit heading from a0 + sweep rather than a1: same angle, no branch cut.
    heading = a0 + sweep + (math.pi / 2 if sweep > 0 else -math.pi / 2)
    return pts, heading


def _r(radius: float) -> str:
    return "INF" if math.isinf(radius) else f"{radius:g}"


def _radius(value: str | None, path, which: str) -> tuple[float, list[str]]:
    """A spiral end radius. ``INF``, blank or absent all mean a tangent end."""
    if value is None or not value.strip():
        return math.inf, []
    text = value.strip().upper()
    if text in ("INF", "INFINITE", "INFINITY", "-INF"):
        return math.inf, []
    try:
        r = float(text)
    except ValueError:
        raise AdapterError(
            f"{path}: <Spiral {which}={value!r}> is neither a number nor INF"
        ) from None
    if r == 0:
        # A zero-radius end is infinite curvature, which no spiral has. Every
        # exporter that writes it means the tangent end. Say so rather than
        # dividing by it.
        return math.inf, [
            f"<Spiral {which}> is zero, read as an infinite radius (a tangent "
            "end); a zero radius is not a curve gisc could draw"
        ]
    return abs(r), []


def _spiral_heading(el: ET.Element, start, prev_heading, path):
    """The tangent direction at the spiral's start.

    ``<PI>`` is the intersection of the tangents at the spiral's two ends, so
    Start -> PI is the entry tangent whichever way round the spiral runs.
    """
    notes: list[str] = []
    pi_el = _find(el, "PI")
    if pi_el is not None:
        px, py = _point(pi_el, path, "Spiral/PI")
        dx, dy = px - start[0], py - start[1]
        if math.hypot(dx, dy) > 1e-6:
            heading = math.atan2(dy, dx)
            if prev_heading is not None:
                off = abs((heading - prev_heading + math.pi) % (2 * math.pi) - math.pi)
                if off > math.radians(0.01):
                    notes.append(
                        f"<Spiral> enters {math.degrees(off):.4f} deg off the tangent "
                        "of the element before it; gisc used the spiral's own <PI>"
                    )
            return heading, "PI", notes
    if prev_heading is not None:
        return prev_heading, "previous element", notes
    raise AdapterError(
        f"{path}: <Spiral> has no usable <PI> and nothing before it to take a "
        "tangent from, so gisc cannot orient it. A spiral needs either a <PI> "
        "distinct from its <Start>, or a preceding <Line> or <Curve>."
    )


def _clothoid(start, theta0: float, k1: float, k2: float, length: float,
              sign: float, n: int):
    """Integrate a clothoid: curvature ramps linearly from ``k1`` to ``k2``.

    Heading is exact -- theta(s) = theta0 + sign*(k1*s + (k2-k1)*s^2/2L) -- so
    the only approximation is the quadrature of its sine and cosine, done by
    composite Simpson. One expression covers an entry spiral, an exit spiral
    and a compound spiral running between two finite radii.
    """
    def theta(s: float) -> float:
        return theta0 + sign * (k1 * s + (k2 - k1) * s * s / (2.0 * length))

    panels = n * SPIRAL_SUBSTEPS
    h = length / panels
    x, y = start
    pts: list[tuple[float, float]] = []
    for j in range(panels):
        s0 = j * h
        t0, tm, t1 = theta(s0), theta(s0 + h / 2.0), theta(s0 + h)
        x += h / 6.0 * (math.cos(t0) + 4.0 * math.cos(tm) + math.cos(t1))
        y += h / 6.0 * (math.sin(t0) + 4.0 * math.sin(tm) + math.sin(t1))
        if (j + 1) % SPIRAL_SUBSTEPS == 0:
            pts.append((x, y))
    return pts, theta(length)


def _spiral(el: ET.Element, path, prev_heading: float | None):
    """Flatten a ``<Spiral>`` to chords. Returns points, exit heading, notes."""
    spi_type = (el.get("spiType") or "clothoid").strip().lower()
    if spi_type != "clothoid":
        raise AdapterError(
            f"{path}: <Spiral spiType={el.get('spiType')!r}> is not a clothoid. "
            "gisc flattens clothoid spirals only -- bloss, sinusoidal, cosine and "
            "the cubic families are not implemented. Re-export the alignment as "
            "chords, or hand gisc a GeoJSON LineString instead."
        )

    start = _point(_find(el, "Start"), path, "Spiral/Start")
    end = _point(_find(el, "End"), path, "Spiral/End")
    length = _maybe_float(el.get("length"))
    if not length or length <= 0:
        raise AdapterError(
            f"{path}: <Spiral> needs a positive length, got {el.get('length')!r}"
        )
    rot = (el.get("rot") or "").lower()
    if rot not in ("cw", "ccw"):
        raise AdapterError(f"{path}: <Spiral> needs rot='cw' or rot='ccw', got {rot!r}")

    r1, notes = _radius(el.get("radiusStart"), path, "radiusStart")
    r2, more = _radius(el.get("radiusEnd"), path, "radiusEnd")
    notes = list(notes) + more
    if math.isinf(r1) and math.isinf(r2):
        raise AdapterError(
            f"{path}: <Spiral> is infinite-radius at both ends, which is a line, "
            "not a spiral"
        )
    k1 = 0.0 if math.isinf(r1) else 1.0 / r1
    k2 = 0.0 if math.isinf(r2) else 1.0 / r2

    theta0, how, heading_notes = _spiral_heading(el, start, prev_heading, path)
    notes += heading_notes

    # Chord count from the tightest radius, the same rule the arc uses.
    r_min = 1.0 / max(k1, k2)
    step = 2 * math.acos(max(-1.0, min(1.0, 1 - ARC_TOLERANCE / r_min)))
    deflection = abs(length * (k1 + k2) / 2.0)
    # Curvature is not constant along a spiral, so the chord count follows the
    # tightest end, not the average: the chord nearest the small-radius end
    # turns about twice as far as the mean chord does, and it is the one that
    # has to satisfy the tolerance.
    worst_chord_turn = max(k1, k2) * length
    n = max(2, min(20_000, math.ceil(worst_chord_turn / max(step, 1e-9))))

    sign = 1.0 if rot == "ccw" else -1.0
    pts, heading = _clothoid(start, theta0, k1, k2, length, sign, n)
    closure = math.dist(pts[-1], end)

    if closure > SPIRAL_CLOSURE_TOLERANCE:
        # Before blaming the file, check the one mistake that produces exactly
        # this: a rotation that disagrees with the geometry.
        other, _ = _clothoid(start, theta0, k1, k2, length, -sign, n)
        hint = ""
        if math.dist(other[-1], end) < closure / 10.0:
            hint = (
                f" It closes to {math.dist(other[-1], end):.4f} with "
                f"rot={'cw' if rot == 'ccw' else 'ccw'!r}, so the declared rotation "
                "is probably the wrong way round."
            )
        raise AdapterError(
            f"{path}: <Spiral> does not close. Integrating length={length:g} from "
            f"radiusStart={_r(r1)} to radiusEnd={_r(r2)} rot={rot!r}, starting on "
            f"the tangent from its {how}, lands {closure:.4f} CRS units from the "
            f"declared <End>. gisc will not stretch a curve onto an endpoint it "
            f"does not reach.{hint}"
        )
    if closure > 0.001:
        notes.append(
            f"<Spiral> integration closed {closure:.4f} CRS units from the declared "
            "<End>; gisc snapped the last point to <End>"
        )
    pts[-1] = end

    declared_theta = _maybe_float(el.get("theta"))
    if declared_theta is not None:
        degrees = math.degrees(deflection)
        if abs(declared_theta - degrees) > 0.01:
            if abs(declared_theta - deflection) <= 1e-4:
                notes.append(
                    "<Spiral theta> is in radians, not the file's declared angular "
                    f"unit; gisc used its own computed deflection of {degrees:.6f} deg"
                )
            else:
                notes.append(
                    f"<Spiral theta={declared_theta:g}> disagrees with the "
                    f"{degrees:.6f} deg gisc computes from length and radii; gisc "
                    "used the computed value"
                )

    notes.append(
        f"flattened a <Spiral> ({_r(r1)} -> {_r(r2)}, {length:g} long, "
        f"{math.degrees(deflection):.4f} deg) to {n} chords at {ARC_TOLERANCE} "
        f"CRS units, oriented from its {how}"
    )
    return [start, *pts], heading, notes


def _centerline(alignment: ET.Element, path) -> tuple[LineString, list[str]]:
    geom = _find(alignment, "CoordGeom")
    if geom is None:
        raise AdapterError(f"{path}: <Alignment> has no <CoordGeom>")

    coords: list[tuple[float, float]] = []
    notes: list[str] = []
    # The tangent leaving the last element, so a spiral with no <PI> can be
    # oriented. None until the first element has been read.
    heading: float | None = None

    for el in geom:
        kind = _tag(el)
        if kind == "Line":
            start = _point(_find(el, "Start"), path, "Line/Start")
            end = _point(_find(el, "End"), path, "Line/End")
            seg = [start, end]
            heading = math.atan2(end[1] - start[1], end[0] - start[0])
        elif kind == "Curve":
            start = _point(_find(el, "Start"), path, "Curve/Start")
            center = _point(_find(el, "Center"), path, "Curve/Center")
            end = _point(_find(el, "End"), path, "Curve/End")
            arc, heading = _arc(start, center, end, (el.get("rot") or "").lower(), path)
            seg = [start, *arc]
            notes.append(
                f"flattened a <Curve> to {len(seg)} chords at {ARC_TOLERANCE} CRS units"
            )
        elif kind == "Spiral":
            seg, heading, spiral_notes = _spiral(el, path, heading)
            notes += spiral_notes
        else:
            raise AdapterError(
                f"{path}: <CoordGeom> contains <{kind}>, which gisc does not parse. "
                "Only <Line>, <Curve> and <Spiral> (clothoid) are supported; "
                "re-export the alignment as chords, or hand gisc a GeoJSON "
                "LineString instead."
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


def _label(el: ET.Element) -> str:
    return el.get("name") or "(unnamed)"


def _pick_subject(root: ET.Element, path, name: str | None) -> tuple[str, ET.Element]:
    """Work out whether ``name`` means an alignment or a pipe network."""
    alignments = _findall(root, "Alignment")
    networks = _findall(root, "PipeNetwork")

    if name is not None:
        for a in alignments:
            if a.get("name") == name:
                return "alignment", a
        for n in networks:
            if n.get("name") == name:
                return "pipenetwork", n
        available = ", ".join(
            [f"alignment {_label(a)!r}" for a in alignments]
            + [f"network {_label(n)!r}" for n in networks]
        ) or "nothing readable"
        raise AdapterError(f"{path}: no <Alignment name={name!r}>; available: {available}")

    if not alignments and not networks:
        raise AdapterError(
            f"{path}: no <Alignment> and no <PipeNetwork>. gisc reads alignments and "
            "pipe networks; surfaces, parcels and point groups are not implemented."
        )
    if alignments and not networks:
        if len(alignments) > 1:
            names = ", ".join(_label(a) for a in alignments)
            raise AdapterError(
                f"{path}: has {len(alignments)} alignments ({names}); "
                "name one with layer=<name>"
            )
        return "alignment", alignments[0]
    if networks and not alignments:
        if len(networks) > 1:
            names = ", ".join(_label(n) for n in networks)
            raise AdapterError(
                f"{path}: has {len(networks)} pipe networks ({names}); "
                "name one with layer=<name>"
            )
        return "pipenetwork", networks[0]

    both = ", ".join(
        [f"alignment {_label(a)!r}" for a in alignments]
        + [f"network {_label(n)!r}" for n in networks]
    )
    raise AdapterError(
        f"{path}: holds both alignments and pipe networks ({both}); "
        "name one with layer=<name>"
    )


def _pick_alignment(root: ET.Element, path, name: str | None) -> ET.Element:
    subject, el = _pick_subject(root, path, name)
    if subject != "alignment":
        raise AdapterError(f"{path}: {name!r} is a pipe network, not an alignment")
    return el


def _maybe_float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _structures(network: ET.Element, path) -> dict[str, dict[str, Any]]:
    """Structures carry the coordinates; pipes only reference them by name."""
    out: dict[str, dict[str, Any]] = {}
    for s in network.iter():
        if _tag(s) != "Struct":
            continue
        centre = _find(s, "Center")
        if centre is None:
            continue
        name = s.get("name") or f"struct{len(out)}"
        out[name] = {
            "name": name,
            "desc": s.get("desc"),
            "elev_rim": _maybe_float(s.get("elevRim")),
            "elev_sump": _maybe_float(s.get("elevSump")),
            "xy": _point(centre, path, "Struct/Center"),
        }
    return out


def _network_features(network: ET.Element, path):
    """Pipes as lines and structures as points, plus what had to be left out."""
    structures = _structures(network, path)
    rows: list[dict[str, Any]] = []
    geoms: list[Any] = []
    skipped: list[dict[str, str]] = []
    length_gap = 0.0

    for pipe in network.iter():
        if _tag(pipe) != "Pipe":
            continue
        name = pipe.get("name") or f"pipe{len(rows)}"
        start, end = pipe.get("refStart"), pipe.get("refEnd")
        a, b = structures.get(start or ""), structures.get(end or "")
        if a is None or b is None:
            missing = [n for n, s in ((start, a), (end, b)) if s is None]
            skipped.append({
                "pipe": name,
                "reason": "structure(s) not in this file: "
                          + ", ".join(repr(m) for m in missing),
            })
            continue
        if a["xy"] == b["xy"]:
            skipped.append({"pipe": name, "reason": "both structures at the same point"})
            continue

        circ = _find(pipe, "CircPipe")
        declared = _maybe_float(pipe.get("length"))
        computed = math.dist(a["xy"], b["xy"])
        if declared:
            length_gap = max(length_gap, abs(computed - declared))
        rows.append({
            "kind": "pipe",
            "name": name,
            "desc": pipe.get("desc"),
            "from_struct": start,
            "to_struct": end,
            "diameter": _maybe_float(circ.get("diameter")) if circ is not None else None,
            "material": circ.get("material") if circ is not None else None,
            "slope": _maybe_float(pipe.get("slope")),
            "length_declared": declared,
        })
        geoms.append(LineString([a["xy"], b["xy"]]))

    for s in structures.values():
        rows.append({
            "kind": "struct",
            "name": s["name"],
            "desc": s["desc"],
            "elev_rim": s["elev_rim"],
            "elev_sump": s["elev_sump"],
        })
        geoms.append(Point(s["xy"]))

    return rows, geoms, skipped, structures, length_gap


def _equations(alignment: ET.Element, path) -> list[Equation]:
    """Read ``<StaEquation>`` children into the form :mod:`gisc.stations` wants.

    ``staInternal`` is the raw station, the only one of the three attributes
    tied to the geometry. Exporters that omit it give ``staBack`` instead, and
    the raw station follows from the equations already applied.
    """
    equations: list[Equation] = []
    offset = 0.0
    for el in _findall(alignment, "StaEquation"):
        ahead = _maybe_float(el.get("staAhead"))
        back = _maybe_float(el.get("staBack"))
        internal = _maybe_float(el.get("staInternal"))
        if ahead is None:
            raise AdapterError(
                f"{path}: <StaEquation> has no staAhead, so it does not say what "
                "the stationing becomes"
            )
        if internal is None and back is None:
            raise AdapterError(
                f"{path}: <StaEquation staAhead={ahead:g}> gives neither "
                "staInternal nor staBack, so gisc cannot place it on the alignment"
            )
        if internal is None:
            internal = back - offset
        if back is None:
            back = internal + offset
        equations.append(
            Equation(internal=internal, back=back, ahead=ahead, desc=el.get("desc"))
        )
        offset = ahead - internal
    return equations


def describe(ref: str, layer: str | None = None, crs_override: str | None = None) -> dict[str, Any]:
    path = resolve(ref)
    root = _root(path)
    crs, source = _read_crs(root, path, crs_override)
    subject, el = _pick_subject(root, path, layer)
    units = _find(root, "Units")
    declared_unit = None
    if units is not None and len(units):
        declared_unit = units[0].get("linearUnit")

    info = {
        "kind": "landxml",
        "subject": subject,
        "ref": str(path),
        "layer": el.get("name"),
        "native_crs": crs,
        "crs_source": source,
        "landxml_linear_unit": declared_unit,
    }
    if subject == "alignment":
        info["sta_start"] = float(el.get("staStart") or 0.0)
        info["length_declared"] = float(el.get("length") or 0.0)
        info["sta_equations_declared"] = sum(
            1 for x in el.iter() if _tag(x) == "StaEquation"
        )
    else:
        info["pipe_net_type"] = el.get("pipeNetType")
        info["alignment_ref"] = el.get("alignmentRef")
        info["pipes_declared"] = sum(1 for x in el.iter() if _tag(x) == "Pipe")
        info["structs_declared"] = sum(1 for x in el.iter() if _tag(x) == "Struct")
    return info


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


def _read_network(info, path, root, layer, crs):
    """A pipe network: pipes as lines, structures as points."""
    _subject, network = _pick_subject(root, path, layer)
    rows, geoms, skipped, structures, length_gap = _network_features(network, path)
    if not rows:
        raise AdapterError(
            f"{path}: pipe network {info['layer']!r} yielded no geometry "
            f"({info['pipes_declared']} pipes, {info['structs_declared']} structures "
            "declared, but no structure carried a <Center>)"
        )

    notes = []
    if skipped:
        notes.append(
            f"left out {len(skipped)} of {info['pipes_declared']} pipes with no "
            "usable geometry; see skipped in provenance"
        )
    if length_gap > 0.01:
        notes.append(
            f"pipe lengths differ from the declared <Pipe length> by up to "
            f"{length_gap:.4f}; gisc measures structure centre to structure centre "
            "in 2D, Civil 3D measures along the pipe"
        )
    diameters = sorted({r["diameter"] for r in rows if r.get("diameter")})
    if diameters and min(diameters) >= 4:
        notes.append(
            f"<CircPipe diameter> values {diameters} are reported as written; the "
            f"file declares linearUnit={info.get('landxml_linear_unit')!r} but these "
            "read as inches, so gisc does not convert them"
        )

    gdf = gpd.GeoDataFrame(rows, geometry=geoms, crs=crs)
    prov = {
        **info,
        **file_provenance(path),
        "read_at": now(),
        "features_in": int(len(gdf)),
        "pipes": int(sum(1 for r in rows if r["kind"] == "pipe")),
        "structures": int(sum(1 for r in rows if r["kind"] == "struct")),
        "skipped": skipped,
        "geometry_types": sorted({t for t in gdf.geom_type.dropna().unique()}),
        "filter": f"PipeNetwork[name={info['layer']!r}]/Pipes+Structs",
        "notes": notes,
    }
    return gdf, prov


def read(ref: str, layer: str | None = None, crs_override: str | None = None):
    info = describe(ref, layer=layer, crs_override=crs_override)
    path = resolve(ref)
    root = _root(path)
    crs = CRS.from_user_input(info["native_crs"])
    if info["subject"] == "pipenetwork":
        return _read_network(info, path, root, layer, crs)

    alignment = _pick_alignment(root, path, layer)
    line, notes = _centerline(alignment, path)
    stationing = Stationing(info["sta_start"], _equations(alignment, path))
    if stationing.equated:
        notes.append(
            f"alignment carries {len(stationing.equations)} station equation(s); "
            "the stations gisc reports are equated, and the regions and the raw "
            "stations behind them are in provenance"
        )
    notes += stationing.notes

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
            Stationing.COLUMN: [stationing.to_json()],
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
        "stationing": stationing.to_dict(),
        "geometry_types": ["LineString"],
        "filter": f"Alignment[name={info['layer']!r}]/CoordGeom",
        "notes": notes,
    }
    return gdf, prov
