"""Build a tangent-arc-tangent alignment in Civil 3D 2023 and record its answers.

Civil 3D computes the length, stationing and offsets; gisc later computes them
again from the same geometry. The arc matters most: Civil 3D carries a true
circular arc, gisc flattens it to chords, so the comparison bounds gisc's
chord tolerance in the units an engineer cares about.

Safety: attaches only to a RUNNING Civil 3D 2023 instance, works only in a
drawing it creates itself, never saves. The 2026 instance, where a real
drawing is open, is never touched.
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
OUT = HERE / "c3d_truth.json"

# Design grid: EPSG:2264 (NAD83 / North Carolina, US survey feet) == CSCode NC83F.
E0, N0 = 905_000.0, 675_000.0
STA_START = 1000.0
RADIUS = 300.0
SWEEP_DEG = 45.0

A = (E0, N0)                      # begin
B = (E0 + 400.0, N0)              # end of first tangent / arc start
CENTER = (B[0], B[1] + RADIUS)    # arc curves to the left
_a0 = math.radians(-90.0)
_a1 = _a0 + math.radians(SWEEP_DEG)
_am = _a0 + math.radians(SWEEP_DEG / 2.0)
M = (CENTER[0] + RADIUS * math.cos(_am), CENTER[1] + RADIUS * math.sin(_am))
T = (CENTER[0] + RADIUS * math.cos(_a1), CENTER[1] + RADIUS * math.sin(_a1))
_hdg = _a1 + math.pi / 2.0        # tangent bearing leaving the arc
C = (T[0] + 400.0 * math.cos(_hdg), T[1] + 400.0 * math.sin(_hdg))

EXPECTED_LENGTH = 400.0 + RADIUS * math.radians(SWEEP_DEG) + 400.0


def retry(fn, what, tries=60):
    for _ in range(tries):
        try:
            return fn()
        except pythoncom.com_error as exc:  # noqa: PERF203
            hres = exc.args[0] & 0xFFFFFFFF
            if hres in (0x80010001, 0x8001010A):
                time.sleep(1.5)
                continue
            print(f"  ! {what}: {exc.args[1] if len(exc.args) > 1 else exc}")
            return None
    print(f"  ! {what}: gave up")
    return None


def pt(xy):
    """Civil 3D COM needs a real VT_R8 array; a plain tuple is rejected."""
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8,
                   (float(xy[0]), float(xy[1]), 0.0))


def props(obj, label):
    """Every readable property of a COM object, by name."""
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
        if len(nm) == 1 and fd.invkind == 2:  # propget, no args
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


acad = retry(lambda: win32com.client.GetActiveObject("AutoCAD.Application.24.2"), "attach")
if acad is None:
    print("NO CONNECTION")
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

aecc = retry(lambda: acad.GetInterfaceObject("AeccXUiLand.AeccApplication.13.5"), "aecc")
adoc = retry(lambda: aecc.ActiveDocument, "adoc")
print("drawing =", retry(lambda: adoc.Name, "name"))

cs = retry(lambda: adoc.Settings.DrawingSettings.UnitZoneSettings.CoordinateSystem, "cs")
retry(lambda: setattr(cs, "CSCode", "NC83F"), "set CSCode")
cs_code = retry(lambda: cs.CSCode, "CSCode")
cs_desc = retry(lambda: cs.Description, "desc")
print(f"CSCode = {cs_code!r} ({cs_desc})")

als = retry(lambda: adoc.AlignmentsSiteless, "AlignmentsSiteless")
style = retry(lambda: adoc.AlignmentStyles.Item(0).Name, "style")
labelset = retry(lambda: adoc.AlignmentLabelStyleSets.Item(0).Name, "labelset")
al = retry(lambda: als.Add("GISC-VALIDATION", "0", style, labelset), "Add")
if al is None:
    print("ADD FAILED")
    sys.exit(4)
ents = retry(lambda: al.Entities, "Entities")

print(f"design: A={A} B={B} M={M} T={T} C={C}")
line1 = retry(lambda: ents.AddFixedLine1(pt(A), pt(B)), "AddFixedLine1 #1")
if line1 is None:
    print("LINE 1 FAILED")
    sys.exit(5)
id1 = retry(lambda: line1.Id, "Id 1")
print("line1 id =", id1, "length =", retry(lambda: line1.Length, "l1"))

curve = retry(lambda: ents.AddFixedCurve1(id1, pt(B), pt(M), pt(T)), "AddFixedCurve1")
if curve is None:
    print("CURVE FAILED")
    sys.exit(6)
id2 = retry(lambda: curve.Id, "Id 2")
print("curve id =", id2, "length =", retry(lambda: curve.Length, "l2"))

line2 = retry(lambda: ents.AddFixedLine3(id2, pt(T), pt(C)), "AddFixedLine3 #2")
if line2 is None:
    line2 = retry(lambda: ents.AddFixedLine1(pt(T), pt(C)), "AddFixedLine1 #2")
if line2 is None:
    print("LINE 2 FAILED")
    sys.exit(7)
print("line2 length =", retry(lambda: line2.Length, "l3"))

# StartingStation is read-only; stationing is shifted via the reference point.
retry(lambda: setattr(al, "ReferencePointStation", STA_START), "ReferencePointStation")

length = retry(lambda: al.Length, "length")
sta0 = retry(lambda: al.StartingStation, "sta0")
sta1 = retry(lambda: al.EndingStation, "sta1")
print(f"length = {length}  (hand-computed {EXPECTED_LENGTH:.6f})")
print(f"stations {sta0} -> {sta1}")

count = retry(lambda: ents.Count, "count") or 0
entities = []
for i in range(count):
    e = retry(lambda i=i: ents.Item(i), f"ent{i}")
    if e is None:
        continue
    entities.append(props(e, f"entity{i}"))
    print(f"  entity[{i}] type={entities[-1].get('Type')} length={entities[-1].get('Length')}")

probes = [
    (E0 + 200.0, N0),            # on the first tangent
    (E0 + 200.0, N0 + 8.0),      # 8 ft left of it
    (E0 + 200.0, N0 - 8.0),      # 8 ft right of it
    (B[0], B[1]),                # the PC
    (M[0], M[1]),                # mid-arc
    (T[0], T[1]),                # the PT
    (M[0], M[1] + 10.0),         # off the arc
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
    station_offsets.append({"x": x, "y": y, "station": sta, "offset": off})
    print(f"  probe ({x:.3f},{y:.3f}) -> sta {sta:.4f} off {off:.4f}")

point_locations = []
if sta0 is not None and sta1 is not None:
    s = float(sta0)
    while s <= float(sta1) + 1e-9:
        got = retry(lambda s=s: al.PointLocation(s, 0.0), "PointLocation")
        if got is not None:
            try:
                point_locations.append({"station": s, "x": float(got[0]), "y": float(got[1])})
            except (TypeError, IndexError):
                pass
        s += 50.0
    print("sampled", len(point_locations), "station points")

truth = {
    "product": "Autodesk Civil 3D 2023",
    "com_progid": "AeccXUiLand.AeccApplication.13.5",
    "acad_version": version,
    "coordinate_system": {"cs_code": cs_code, "description": cs_desc, "epsg": 2264},
    "alignment": {
        "name": "GISC-VALIDATION",
        "length": length,
        "starting_station": sta0,
        "ending_station": sta1,
        "entities": entities,
        "station_offsets": station_offsets,
        "point_locations": point_locations,
    },
    "design": {
        "note": "tangent - 45 deg arc (R=300) - tangent, in EPSG:2264 ftUS",
        "E0": E0, "N0": N0, "radius": RADIUS, "sweep_deg": SWEEP_DEG,
        "A": list(A), "B": list(B), "M": list(M), "T": list(T), "C": list(C),
        "center": list(CENTER), "sta_start": STA_START,
        "hand_computed_length": EXPECTED_LENGTH,
    },
}
OUT.write_text(json.dumps(truth, indent=2) + "\n", encoding="utf-8")
print("wrote", OUT)
print("CAPTURE COMPLETE")
