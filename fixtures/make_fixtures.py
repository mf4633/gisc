"""Regenerate the test fixtures.

Everything is designed in EPSG:2264 (NAD83 / North Carolina, US survey feet) --
a realistic civil design CRS -- then exported to the formats a real project
hands you. Run:  python fixtures/make_fixtures.py
"""
from __future__ import annotations

import json
import pathlib

import geopandas as gpd
from pyproj import Transformer
from shapely.geometry import LineString, Polygon, box

HERE = pathlib.Path(__file__).parent
DESIGN_CRS = "EPSG:2264"

# Somewhere west of Asheville, NC. Origin of the local design grid.
E0, N0 = 905_000.0, 675_000.0

# Alignment: three tangents with one bend. Stations run 10+00 -> 22+81.
ALIGNMENT = [(0, 0), (400, 0), (800, 150), (1200, 150)]
STA_START = 1000.0

# name, offset description, vertices (local ft)
UTILITIES = [
    ("WL-8IN", "water, crosses at STA 12+00", [(200, -60), (200, 60)]),
    ("SS-12IN", "sewer, 8 ft off centerline", [(900, 158), (1150, 158)]),
    ("GAS-4IN", "gas, 40 ft off centerline", [(900, 190), (1150, 190)]),
]

FLOOD = [
    ("AE", 2081.4, box(350, -100, 650, 200)),
    ("X", None, box(2000, 500, 2400, 900)),
]


def local(pts):
    return [(E0 + x, N0 + y) for x, y in pts]


def main() -> None:
    to4326 = Transformer.from_crs(DESIGN_CRS, "EPSG:4326", always_xy=True)

    def wgs(pts):
        return [tuple(round(v, 9) for v in to4326.transform(x, y)) for x, y in local(pts)]

    # --- alignment.geojson -------------------------------------------------
    aln_wgs = wgs(ALIGNMENT)
    gj = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "CL-MAIN", "sta_start": STA_START},
                "geometry": {"type": "LineString", "coordinates": [list(c) for c in aln_wgs]},
            }
        ],
    }
    (HERE / "alignment.geojson").write_text(json.dumps(gj, indent=2) + "\n")

    # --- alignment.xml (LandXML, native design CRS) ------------------------
    pts = local(ALIGNMENT)
    total = LineString(pts).length
    segs = []
    for (e1, n1), (e2, n2) in zip(pts, pts[1:]):
        segs.append(
            "        <Line>\n"
            f"          <Start>{n1:.4f} {e1:.4f}</Start>\n"
            f"          <End>{n2:.4f} {e2:.4f}</End>\n"
            "        </Line>"
        )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2" version="1.2"
         date="2026-09-09" time="09:00:00">
  <Units>
    <Imperial linearUnit="USSurveyFoot" areaUnit="squareFoot"
              volumeUnit="cubicFeet" temperatureUnit="fahrenheit"
              pressureUnit="inHG" angularUnit="decimal degrees"
              directionUnit="decimal degrees"/>
  </Units>
  <CoordinateSystem epsgCode="2264" horizontalDatum="NAD83"
                    horizontalCoordinateSystemName="NAD83 / North Carolina (ftUS)"/>
  <Alignments name="Roadway">
    <Alignment name="CL-MAIN" length="{total:.4f}" staStart="{STA_START:.4f}">
      <CoordGeom>
{chr(10).join(segs)}
      </CoordGeom>
    </Alignment>
  </Alignments>
</LandXML>
"""
    (HERE / "alignment.xml").write_text(xml)

    # --- utilities.geojson -------------------------------------------------
    utils = gpd.GeoDataFrame(
        {"name": [u[0] for u in UTILITIES], "note": [u[1] for u in UTILITIES]},
        geometry=[LineString(local(u[2])) for u in UTILITIES],
        crs=DESIGN_CRS,
    ).to_crs("EPSG:4326")
    (HERE / "utilities.geojson").write_text(utils.to_json(indent=2, drop_id=True) + "\n")

    # --- flood.geojson -----------------------------------------------------
    flood = gpd.GeoDataFrame(
        {"zone": [f[0] for f in FLOOD], "bfe_ft": [f[1] for f in FLOOD]},
        geometry=[
            Polygon([(E0 + x, N0 + y) for x, y in g.exterior.coords]) for _, _, g in FLOOD
        ],
        crs=DESIGN_CRS,
    ).to_crs("EPSG:4326")
    (HERE / "flood.geojson").write_text(flood.to_json(indent=2, drop_id=True) + "\n")

    # --- utilities_nocrs.gpkg: a real file with an undefined SRS -----------
    nocrs = gpd.GeoDataFrame(
        {"name": [u[0] for u in UTILITIES]},
        geometry=[LineString(local(u[2])) for u in UTILITIES],
        crs=None,
    )
    out = HERE / "utilities_nocrs.gpkg"
    out.unlink(missing_ok=True)
    nocrs.to_file(out, driver="GPKG", layer="utilities")

    # --- utilities.gpkg: same data, in the design CRS ----------------------
    out = HERE / "utilities.gpkg"
    out.unlink(missing_ok=True)
    gpd.GeoDataFrame(
        {"name": [u[0] for u in UTILITIES], "note": [u[1] for u in UTILITIES]},
        geometry=[LineString(local(u[2])) for u in UTILITIES],
        crs=DESIGN_CRS,
    ).to_file(out, driver="GPKG", layer="utilities")

    print("wrote fixtures to", HERE)


if __name__ == "__main__":
    main()
