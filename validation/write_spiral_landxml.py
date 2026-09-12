"""Write the LandXML for the spiral alignment out of Civil 3D's own geometry.

Every ordinate here is read back from Civil 3D, not from the design that was
fed in, so what gisc parses is Civil 3D's statement of where the curve goes.
The arc centre comes from the entry spiral's ``RadialEasting``/
``RadialNorthing``, which is Civil 3D's own radial point, and each spiral's
``SPIEasting``/``SPINorthing`` is its tangent intersection -- the ``<PI>``
that LandXML wants.

Run after ``capture_civil3d_spiral.py``, against the same live drawing:

    python validation/write_spiral_landxml.py
"""
from __future__ import annotations

import json
import math
import pathlib
import sys

import win32com.client

HERE = pathlib.Path(__file__).parent
TRUTH = HERE / "c3d_spiral_truth.json"
XML = HERE / "alignment_spiral_c3d.xml"
NAME = "GISC-SPIRAL"

NL = "\n"

acad = win32com.client.GetActiveObject("AutoCAD.Application.24.2")
if not str(acad.Version).startswith("24.2"):
    print("REFUSING: expected Civil 3D 2023")
    sys.exit(9)
aecc = acad.GetInterfaceObject("AeccXUiLand.AeccApplication.13.5")
adoc = aecc.ActiveDocument
print("drawing =", adoc.Name)

al = None
for i in range(adoc.AlignmentsSiteless.Count):
    cand = adoc.AlignmentsSiteless.Item(i)
    if cand.Name == NAME:
        al = cand
        break
if al is None:
    print(f"alignment {NAME} not found -- run capture_civil3d_spiral.py first")
    sys.exit(4)

ents = al.Entities
print("entity count =", ents.Count)


def xy(e, n):
    """LandXML writes northing first."""
    return f"{float(n):.9f} {float(e):.9f}"


def rot_of(start_dir: float, end_dir: float) -> str:
    """Civil 3D directions are azimuths. A falling azimuth turns left."""
    turn = (end_dir - start_dir + math.pi) % (2 * math.pi) - math.pi
    return "cw" if turn > 0 else "ccw"


def num(v) -> str:
    return "INF" if math.isinf(float(v)) else f"{float(v):.9f}"


# Top-level entities come back in id order -- line, line, then the SCS group --
# so each piece is collected with its starting station and sorted at the end.
# A CoordGeom out of order is not a centreline.
pieces: list[tuple[float, str, dict]] = []

for i in range(ents.Count):
    e = ents.Item(i)
    kind = int(e.Type)

    if kind == 1:  # line
        pieces.append((
            float(e.StartingStation),
            "        <Line>" + NL
            + f"          <Start>{xy(e.StartEasting, e.StartNorthing)}</Start>" + NL
            + f"          <End>{xy(e.EndEasting, e.EndNorthing)}</End>" + NL
            + "        </Line>",
            {"type": "line", "length": float(e.Length)},
        ))
        continue

    if kind != 4:  # not a spiral-curve-spiral group
        print(f"  ! entity {i} has unexpected type {kind}")
        continue

    radial_e = float(e.SpiralIn.RadialEasting)
    radial_n = float(e.SpiralIn.RadialNorthing)

    for part in ("SpiralIn", "Arc", "SpiralOut"):
        p = getattr(e, part)
        rot = rot_of(float(p.StartDirection), float(p.EndDirection))

        if part == "Arc":
            pieces.append((
                float(p.StartingStation),
                f'        <Curve rot="{rot}" radius="{float(p.Radius):.9f}">' + NL
                + f"          <Start>{xy(p.StartEasting, p.StartNorthing)}</Start>" + NL
                + f"          <Center>{xy(radial_e, radial_n)}</Center>" + NL
                + f"          <End>{xy(p.EndEasting, p.EndNorthing)}</End>" + NL
                + "        </Curve>",
                {"type": "arc", "length": float(p.Length),
                 "radius": float(p.Radius), "delta": float(p.Delta),
                 "centre": [radial_e, radial_n]},
            ))
            continue

        r_in, r_out = float(p.RadiusIn), float(p.RadiusOut)
        pieces.append((
            float(p.StartingStation),
            f'        <Spiral length="{float(p.Length):.9f}" '
            f'radiusStart="{num(r_in)}" radiusEnd="{num(r_out)}" '
            f'rot="{rot}" spiType="clothoid" '
            f'theta="{math.degrees(float(p.Delta)):.9f}" '
            f'totalX="{float(p.TotalX):.9f}" totalY="{float(p.TotalY):.9f}">' + NL
            + f"          <Start>{xy(p.StartEasting, p.StartNorthing)}</Start>" + NL
            + f"          <PI>{xy(p.SPIEasting, p.SPINorthing)}</PI>" + NL
            + f"          <End>{xy(p.EndEasting, p.EndNorthing)}</End>" + NL
            + "        </Spiral>",
            {"type": "spiral", "length": float(p.Length),
             "radius_in": None if math.isinf(r_in) else r_in,
             "radius_out": None if math.isinf(r_out) else r_out,
             "delta": float(p.Delta), "total_x": float(p.TotalX),
             "total_y": float(p.TotalY), "a_value": float(p.A),
             "spiral_type": int(p.SpiralType)},
        ))

pieces.sort(key=lambda t: t[0])
elements = [x for _s, x, _r in pieces]
records = [r for _s, _x, r in pieces]

# The station equations, as Civil 3D holds them.
equations = []
eqs = al.StationEquations
for i in range(eqs.Count):
    item = eqs.Item(i)
    equations.append({
        "raw": float(item.RawStationBack),
        "back": float(item.StationBack),
        "ahead": float(item.StationAhead),
        "type": int(item.Type),
    })
eq_xml = "".join(
    NL + f'      <StaEquation staInternal="{q["raw"]:.9f}" '
    f'staBack="{q["back"]:.9f}" staAhead="{q["ahead"]:.9f}" '
    f'desc="from Civil 3D 2023"/>'
    for q in equations
)

header = f"""<?xml version="1.0" encoding="UTF-8"?>
<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2" version="1.2">
  <Units>
    <Imperial linearUnit="USSurveyFoot" areaUnit="squareFoot"
              volumeUnit="cubicFeet" temperatureUnit="fahrenheit"
              pressureUnit="inHG" angularUnit="decimal degrees"
              directionUnit="decimal degrees"/>
  </Units>
  <CoordinateSystem epsgCode="2264" horizontalDatum="NAD83"
                    horizontalCoordinateSystemName="NAD83 / North Carolina (ftUS)"
                    desc="NAD83 North Carolina State Planes, US Foot (CSCode NC83F)"/>
  <Alignments name="Roadway">
    <Alignment name="{NAME}" length="{float(al.Length):.9f}" """
header += f'staStart="{float(al.StartingStation):.9f}">' + NL
header += "      <CoordGeom>" + NL

footer = NL + "      </CoordGeom>" + eq_xml + NL
footer += "    </Alignment>" + NL + "  </Alignments>" + NL + "</LandXML>" + NL

XML.write_text(header + NL.join(elements) + footer, encoding="utf-8")
print("wrote", XML)

truth = json.loads(TRUTH.read_text())
truth["landxml"] = {"path": XML.name, "elements": records, "equations": equations}
TRUTH.write_text(json.dumps(truth, indent=2) + "\n", encoding="utf-8")
print("updated", TRUTH)
for r in records:
    print("  ", r["type"], "length", round(r["length"], 6))
print("EXPORT COMPLETE")
