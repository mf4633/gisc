"""Read the alignment entity geometry back out of Civil 3D and write the LandXML.

The LandXML carries Civil 3D's own coordinates for the PC, PT, arc centre and
tangent endpoints -- not the numbers that were fed in -- so gisc is parsing
Civil 3D's geometry, not a restatement of the design intent.
"""
from __future__ import annotations

import json
import pathlib
import sys

import win32com.client

HERE = pathlib.Path(__file__).parent
TRUTH = HERE / "c3d_truth.json"
XML = HERE / "alignment_c3d.xml"

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
    if cand.Name == "GISC-VALIDATION":
        al = cand
        break
if al is None:
    print("alignment not found")
    sys.exit(4)

ents = al.Entities  # re-fetched, so Count is current
print("entity count =", ents.Count)


def readable(obj):
    ti = obj._oleobj_.GetTypeInfo()
    attr = ti.GetTypeAttr()
    out = {}
    for i in range(attr.cFuncs):
        fd = ti.GetFuncDesc(i)
        nm = ti.GetNames(fd.memid)
        if len(nm) != 1 or fd.invkind != 2:
            continue
        try:
            val = getattr(obj, nm[0])
        except Exception:  # noqa: BLE001
            continue
        if isinstance(val, (int, float, str, bool)):
            out[nm[0]] = val
    return out


entities = []
for i in range(ents.Count):
    e = ents.Item(i)
    row = readable(e)
    entities.append(row)
    print(f"  entity[{i}] Type={row.get('Type')} Length={row.get('Length')} "
          f"sta {row.get('StartingStation')}->{row.get('EndingStation')}")
    for k in ("StartEasting", "StartNorthing", "MidEasting", "MidNorthing",
              "EndEasting", "EndNorthing", "CenterEasting", "CenterNorthing",
              "Radius", "Clockwise", "Delta"):
        if k in row:
            print(f"      {k} = {row[k]}")

truth = json.loads(TRUTH.read_text())
truth["alignment"]["entities"] = entities
TRUTH.write_text(json.dumps(truth, indent=2) + "\n", encoding="utf-8")
print("updated", TRUTH)

# ------------------------------------------------------------------ LandXML
sta_start = truth["alignment"]["starting_station"]
length = truth["alignment"]["length"]
parts = []
for row in entities:
    kind = str(row.get("Type", ""))
    se, sn = row.get("StartEasting"), row.get("StartNorthing")
    ee, en = row.get("EndEasting"), row.get("EndNorthing")
    if se is None or ee is None:
        print("  ! entity lacks endpoints; skipping")
        continue
    if "Radius" in row and row.get("Radius"):
        ce, cn = row.get("CenterEasting"), row.get("CenterNorthing")
        rot = "cw" if row.get("Clockwise") else "ccw"
        parts.append(
            f'        <Curve rot="{rot}" radius="{row["Radius"]:.10f}">\n'
            f"          <Start>{sn:.10f} {se:.10f}</Start>\n"
            f"          <Center>{cn:.10f} {ce:.10f}</Center>\n"
            f"          <End>{en:.10f} {ee:.10f}</End>\n"
            "        </Curve>"
        )
    else:
        parts.append(
            "        <Line>\n"
            f"          <Start>{sn:.10f} {se:.10f}</Start>\n"
            f"          <End>{en:.10f} {ee:.10f}</End>\n"
            "        </Line>"
        )

xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- Geometry read out of Autodesk Civil 3D 2023 over COM. Coordinates are
     Civil 3D's, to 10 decimal places. Written by c3d_entities.py. -->
<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2" version="1.2">
  <Units>
    <Imperial linearUnit="USSurveyFoot" areaUnit="squareFoot"
              volumeUnit="cubicFeet" temperatureUnit="fahrenheit"
              pressureUnit="inHG" angularUnit="decimal degrees"
              directionUnit="decimal degrees"/>
  </Units>
  <CoordinateSystem epsgCode="2264" horizontalDatum="NAD83"
                    horizontalCoordinateSystemName="NAD83 / North Carolina (ftUS)"
                    desc="{truth['coordinate_system']['description']}"/>
  <Alignments name="GISC">
    <Alignment name="GISC-VALIDATION" length="{length:.10f}" staStart="{sta_start:.10f}">
      <CoordGeom>
{chr(10).join(parts)}
      </CoordGeom>
    </Alignment>
  </Alignments>
</LandXML>
"""
XML.write_text(xml, encoding="utf-8")
print("wrote", XML)
print("ENTITIES COMPLETE")
