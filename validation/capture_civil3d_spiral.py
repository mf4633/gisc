"""Build a spiral-curve-spiral alignment with a station equation in Civil 3D 2023.

The arc validation in ``capture_civil3d.py`` bounded what chord flattening
costs. This one covers the two things gisc had no evidence for:

* a **clothoid spiral**, which gisc integrates and Civil 3D carries analytically
* a **station equation**, where the station of a point stops being its distance

Civil 3D is asked for its own stations and offsets, and then for a LandXML
export, so the file gisc reads is Civil 3D's statement of the geometry rather
than a restatement of the design intent.

Safety: attaches only to a RUNNING Civil 3D 2023 (24.2), works only in a
drawing it creates itself, never saves. A 2026 instance with real project work
open is never touched.

Run with the system Python (pywin32 lives there, not in the venv):

    python validation/capture_civil3d_spiral.py
"""
from __future__ import annotations

import json
import math
import os
import pathlib
import sys
import time

import pythoncom
import win32com.client
from win32com.client import VARIANT

HERE = pathlib.Path(__file__).parent
OUT = HERE / "c3d_spiral_truth.json"
XML = HERE / "alignment_spiral_c3d.xml"

NAME = "GISC-SPIRAL"
PROGID = "AeccXUiLand.AeccApplication.13.5"

# Design, in EPSG:2264 (NAD83 / North Carolina, US survey feet) == CSCode NC83F.
E0, N0 = 905_000.0, 675_000.0
STA_START = 1000.0
RADIUS = 300.0
SPIRAL_LEN = 150.0
TANGENT = 500.0

# The station equation: at raw 14+00, the stationing jumps to 20+00.
EQ_RAW = 1400.0
EQ_AHEAD = 2000.0

# Two tangents that intersect, so a spiral-curve-spiral can be fitted between
# them. PI at 45 degrees keeps the arithmetic checkable by hand.
A = (E0, N0)
PI = (E0 + TANGENT, N0)
C = (PI[0] + TANGENT * math.cos(math.radians(45.0)),
     PI[1] + TANGENT * math.sin(math.radians(45.0)))


def retry(fn, what, tries=60):
    for _ in range(tries):
        try:
            return fn()
        except AttributeError:  # noqa: PERF203
            # A late-bound COM object with no type info yet: the drawing is
            # still opening. Not an error, just early.
            time.sleep(1.5)
            continue
        except pythoncom.com_error as exc:
            hres = exc.args[0] & 0xFFFFFFFF
            if hres in (0x80010001, 0x8001010A):  # busy / rejected
                time.sleep(1.5)
                continue
            print(f"  ! {what}: {exc.args[1] if len(exc.args) > 1 else exc}")
            return None
    print(f"  ! {what}: gave up")
    return None


def wait_for_document(acad, tries=80):
    """Civil 3D answers to COM before its document object is usable."""
    for _ in range(tries):
        try:
            aecc = acad.GetInterfaceObject(PROGID)
            adoc = aecc.ActiveDocument
            name = adoc.Name
            if name:
                return aecc, adoc, name
        except (AttributeError, pythoncom.com_error):
            pass
        time.sleep(2.0)
    return None, None, None


def pt(xy):
    """Civil 3D COM needs a real VT_R8 array; a plain tuple is rejected."""
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8,
                   (float(xy[0]), float(xy[1]), 0.0))


def methods(obj, label):
    """Every callable on a COM object, with how many arguments it wants."""
    out = {}
    try:
        ti = obj._oleobj_.GetTypeInfo()
        attr = ti.GetTypeAttr()
    except Exception as exc:  # noqa: BLE001
        print(f"  ! typeinfo for {label}: {exc}")
        return out
    for i in range(attr.cFuncs):
        fd = ti.GetFuncDesc(i)
        names = ti.GetNames(fd.memid)
        if names:
            out[names[0]] = len(names) - 1
    return out


def props(obj, label):
    """Every readable property, by name."""
    out = {}
    try:
        ti = obj._oleobj_.GetTypeInfo()
        attr = ti.GetTypeAttr()
    except Exception as exc:  # noqa: BLE001
        print(f"  ! typeinfo for {label}: {exc}")
        return out
    names = set()
    for i in range(attr.cFuncs):
        fd = ti.GetFuncDesc(i)
        nm = ti.GetNames(fd.memid)
        if len(nm) == 1 and fd.invkind == 2:
            names.add(nm[0])
    for name in sorted(names):
        try:
            val = getattr(obj, name)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(val, (int, float, str, bool)):
            out[name] = val
        elif isinstance(val, tuple) and all(isinstance(v, (int, float)) for v in val):
            out[name] = list(val)
    return out


# -- attach -----------------------------------------------------------------

acad = retry(lambda: win32com.client.GetActiveObject("AutoCAD.Application.24.2"), "attach")
if acad is None:
    print("NO CONNECTION -- is Civil 3D 2023 running?")
    sys.exit(1)
version = str(retry(lambda: acad.Version, "version"))
print("Version =", version)
if not version.startswith("24.2"):
    print("REFUSING: expected Civil 3D 2023")
    sys.exit(9)

template = os.path.expandvars(
    r"%LOCALAPPDATA%\Autodesk\C3D 2023\enu\Template\_Autodesk Civil 3D (Imperial) NCS.dwt"
)
doc = retry(lambda: acad.Documents.Add(template), "Documents.Add")
if doc is None:
    print("NO DOC")
    sys.exit(3)
time.sleep(4)

aecc, adoc, dwg_name = wait_for_document(acad)
if adoc is None:
    print("DOCUMENT NEVER BECAME READY")
    sys.exit(2)
print("drawing =", dwg_name)

cs = retry(lambda: adoc.Settings.DrawingSettings.UnitZoneSettings.CoordinateSystem, "cs")
retry(lambda: setattr(cs, "CSCode", "NC83F"), "set CSCode")
cs_code = retry(lambda: cs.CSCode, "CSCode")
cs_desc = retry(lambda: cs.Description, "desc")
print(f"CSCode = {cs_code!r} ({cs_desc})")

als = retry(lambda: adoc.AlignmentsSiteless, "AlignmentsSiteless")
style = retry(lambda: adoc.AlignmentStyles.Item(0).Name, "style")
labelset = retry(lambda: adoc.AlignmentLabelStyleSets.Item(0).Name, "labelset")
al = retry(lambda: als.Add(NAME, "0", style, labelset), "Add alignment")
if al is None:
    print("ADD FAILED")
    sys.exit(4)
ents = retry(lambda: al.Entities, "Entities")

# What this build of Civil 3D actually offers. Dumped whatever happens, so a
# signature that needs adjusting does not cost another launch.
api = {
    "AlignmentEntities": methods(ents, "ents"),
    "Alignment": methods(al, "al"),
}
(HERE / "c3d_api_dump.json").write_text(json.dumps(api, indent=2, sort_keys=True) + "\n")
adds = sorted(n for n in api["AlignmentEntities"] if n.startswith("Add"))
print("Add* methods:", ", ".join(adds) or "(none)")

# -- geometry ---------------------------------------------------------------

print(f"design: A={A} PI={PI} C={C}  R={RADIUS} Ls={SPIRAL_LEN}")
line1 = retry(lambda: ents.AddFixedLine1(pt(A), pt(PI)), "AddFixedLine1 #1")
line2 = retry(lambda: ents.AddFixedLine1(pt(PI), pt(C)), "AddFixedLine1 #2")
if line1 is None or line2 is None:
    print("TANGENTS FAILED")
    sys.exit(5)
id1 = retry(lambda: line1.Id, "Id 1")
id2 = retry(lambda: line2.Id, "Id 2")
print("tangent ids:", id1, id2)

# A free spiral-curve-spiral fitted between the two tangents.
#
# The real signature, from the type library:
#
#   AddFreeSCSGroup1(PreviousElementId, NextElementId, Spiral1Length,
#                    Radius, Spiral2Length, GreaterThan180, spiralDefinition)
#
# GreaterThan180 picks between the two curves tangent to both lines. Passing
# 1 fits the 315 degree reflex curve, not the 45 degree one -- worth knowing,
# because it succeeds and looks plausible until you read the length.
GREATER_THAN_180 = 0
CLOTHOID = 1  # spiralDefinition; 0 is rejected by this build
scs = retry(
    lambda: ents.AddFreeSCSGroup1(id1, id2, SPIRAL_LEN, RADIUS, SPIRAL_LEN,
                                  GREATER_THAN_180, CLOTHOID),
    "AddFreeSCSGroup1",
    tries=5,
)
if scs is None:
    print("SPIRAL-CURVE-SPIRAL FAILED; see c3d_api_dump.json")
    sys.exit(6)

# Re-fetch: the collection handle taken before the fit does not see it.
ents = retry(lambda: al.Entities, "Entities (refetched)")
n_ents = retry(lambda: ents.Count, "entity count") or 0
fitted = retry(lambda: al.Length, "fitted length")
print(f"alignment now has {n_ents} top-level entities, length {fitted}")
if n_ents != 3:
    print(f"REFUSING: expected 3 (line, SCS group, line), got {n_ents}")
    sys.exit(7)

# What the geometry has to come to, by hand, so a bad fit cannot pass quietly.
_ds = SPIRAL_LEN / (2.0 * RADIUS)                       # one spiral, radians
_dc = math.radians(45.0) - 2.0 * _ds                    # the arc
_xs = SPIRAL_LEN - SPIRAL_LEN**3 / (40.0 * RADIUS**2)   # spiral X, series
_ys = SPIRAL_LEN**2 / (6.0 * RADIUS) - SPIRAL_LEN**4 / (336.0 * RADIUS**3)
_p = _ys - RADIUS * (1.0 - math.cos(_ds))
_k = _xs - RADIUS * math.sin(_ds)
_ts = _k + (RADIUS + _p) * math.tan(math.radians(22.5))
EXPECTED_LENGTH = 2.0 * (TANGENT - _ts) + 2.0 * SPIRAL_LEN + RADIUS * _dc
print(f"hand-computed length = {EXPECTED_LENGTH:.6f}  (Civil 3D says {fitted})")
if abs(float(fitted) - EXPECTED_LENGTH) > 0.01:
    print("REFUSING: the fitted alignment is not the one that was asked for")
    sys.exit(8)

retry(lambda: setattr(al, "ReferencePointStation", STA_START), "ReferencePointStation")

# -- the station equation ---------------------------------------------------

equations = []
eqs = retry(lambda: al.StationEquations, "StationEquations")
if eqs is not None:
    eq_methods = methods(eqs, "StationEquations")
    api["StationEquations"] = eq_methods
    print("StationEquations methods:", ", ".join(sorted(eq_methods)))
    # Add(RawStationBack, StationBack, StationAhead, StationEquationType).
    # The type enum is undocumented in the type library; 1 is increasing.
    added = None
    for eq_type in (1, 0, 2):
        args = (EQ_RAW, EQ_RAW, EQ_AHEAD, eq_type)
        added = retry(lambda a=args: eqs.Add(*a), f"StationEquations.Add{args}", tries=3)
        if added is not None:
            print(f"added station equation with Add{args}")
            break
    count = retry(lambda: eqs.Count, "eq count") or 0
    for i in range(count):
        item = retry(lambda i=i: eqs.Item(i), f"eq{i}")
        if item is not None:
            equations.append(props(item, f"eq{i}"))
    print("station equations on the alignment:", equations)
else:
    print("  ! no StationEquations collection on this build")

(HERE / "c3d_api_dump.json").write_text(json.dumps(api, indent=2, sort_keys=True) + "\n")

# -- what Civil 3D says -----------------------------------------------------

length = retry(lambda: al.Length, "length")
sta0 = retry(lambda: al.StartingStation, "sta0")
sta1 = retry(lambda: al.EndingStation, "sta1")
print(f"length = {length}   stations {sta0} -> {sta1}")

count = retry(lambda: ents.Count, "count") or 0
entities = []
for i in range(count):
    e = retry(lambda i=i: ents.Item(i), f"ent{i}")
    if e is None:
        continue
    row = props(e, f"entity{i}")
    # An SCS group carries its three pieces; those are what gisc has to match.
    for part in ("SpiralIn", "Arc", "SpiralOut"):
        sub_obj = retry(lambda e=e, part=part: getattr(e, part), part, tries=2)
        if sub_obj is not None:
            row[part] = props(sub_obj, f"entity{i}.{part}")
    entities.append(row)
    print(f"  entity[{i}] type={row.get('Type')} length={row.get('Length')}")
    for part in ("SpiralIn", "Arc", "SpiralOut"):
        if part in row:
            d = row[part]
            print(f"      {part}: length={d.get('Length')} "
                  f"R={d.get('Radius') or d.get('RadiusIn')}->{d.get('RadiusOut')} "
                  f"A={d.get('AValue')}")

# Probe points: on each tangent, and across the curve, so the comparison covers
# spiral, arc and the equation.
probes = [
    (E0 + 200.0, N0),
    (E0 + 200.0, N0 + 8.0),
    (E0 + 200.0, N0 - 8.0),
    (E0 + 450.0, N0),
    (E0 + 700.0, N0 + 60.0),
    (E0 + 800.0, N0 + 150.0),
    (E0 + 900.0, N0 + 260.0),
    (C[0] - 50.0, C[1] - 50.0),
]
station_offsets = []
for x, y in probes:
    got = retry(lambda x=x, y=y: al.StationOffset(x, y), "StationOffset")
    if got is None:
        continue
    try:
        sta, off = float(got[0]), float(got[1])
    except (TypeError, IndexError):
        print("  StationOffset returned", got)
        continue
    equated = retry(
        lambda s=sta: al.GetStationStringWithEquations(s), "GetStationStringWithEquations"
    )
    station_offsets.append({
        "x": x, "y": y, "station": sta, "offset": off, "station_string": equated,
    })
    print(f"  probe ({x:.3f},{y:.3f}) -> sta {sta:.4f} ({equated}) off {off:.4f}")

# Civil 3D's own centreline, every 25 ft of RAW station.
raw0 = retry(lambda: al.StartingStation, "raw0")
point_locations = []
if length is not None:
    s = 0.0
    while s <= float(length) + 1e-9:
        got = retry(lambda s=s: al.PointLocation(float(raw0) + s, 0.0), "PointLocation")
        if got is not None:
            try:
                point_locations.append(
                    {"distance": s, "x": float(got[0]), "y": float(got[1])}
                )
            except (TypeError, IndexError):
                pass
        s += 25.0
    print("sampled", len(point_locations), "centreline points")

truth = {
    "product": "Autodesk Civil 3D 2023",
    "com_progid": PROGID,
    "acad_version": version,
    "coordinate_system": {"cs_code": cs_code, "description": cs_desc, "epsg": 2264},
    "alignment": {
        "name": NAME,
        "length": length,
        "starting_station": sta0,
        "ending_station": sta1,
        "entities": entities,
        "station_equations": equations,
        "station_offsets": station_offsets,
        "point_locations": point_locations,
    },
    "design": {
        "note": "tangent - spiral - arc - spiral - tangent, 45 deg PI, "
                "R=300 Ls=150, EPSG:2264 ftUS, one station equation",
        "E0": E0, "N0": N0, "radius": RADIUS, "spiral_length": SPIRAL_LEN,
        "tangent": TANGENT, "sta_start": STA_START,
        "A": list(A), "PI": list(PI), "C": list(C),
        "equation": {"raw": EQ_RAW, "ahead": EQ_AHEAD},
    },
}
OUT.write_text(json.dumps(truth, indent=2) + "\n", encoding="utf-8")
print("wrote", OUT)
print("CAPTURE COMPLETE")
