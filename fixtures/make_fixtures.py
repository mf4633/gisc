"""Regenerate the test fixtures.

Everything is designed in EPSG:2264 (NAD83 / North Carolina, US survey feet) --
a realistic civil design CRS -- then exported to the formats a real project
hands you. Run:  python fixtures/make_fixtures.py
"""
from __future__ import annotations

import json
import math
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


# ---------------------------------------------------------------------------
# A spiral-curve-spiral alignment, and one carrying station equations.
#
# The spiral geometry here is computed from the Fresnel integrals by power
# series -- deliberately not by the numerical integration gisc uses to read it
# back, so the fixture is an independent statement of where the curve goes
# rather than a recording of gisc's own answer.

SPIRAL_R = 300.0          # radius of the circular arc between the spirals
SPIRAL_LEN = 150.0        # length of each transition spiral
ARC_DEFLECTION = 45.0     # degrees turned by the circular arc alone
TANGENT = 400.0           # tangent in and out


def fresnel(x: float, terms: int = 40) -> tuple[float, float]:
    """C(x), S(x) by power series. Converges fast for the x a spiral produces."""
    c = s_ = 0.0
    for n in range(terms):
        c += (-1) ** n * (math.pi / 2) ** (2 * n) * x ** (4 * n + 1) / (
            math.factorial(2 * n) * (4 * n + 1)
        )
        s_ += (-1) ** n * (math.pi / 2) ** (2 * n + 1) * x ** (4 * n + 3) / (
            math.factorial(2 * n + 1) * (4 * n + 3)
        )
    return c, s_


def _rot(theta: float, v: tuple[float, float]) -> tuple[float, float]:
    ct, st = math.cos(theta), math.sin(theta)
    return (v[0] * ct - v[1] * st, v[0] * st + v[1] * ct)


def _intersect(p: tuple[float, float], hp: float,
               q: tuple[float, float], hq: float) -> tuple[float, float]:
    """Where the tangent at p and the tangent at q cross: a spiral's PI."""
    dx, dy = q[0] - p[0], q[1] - p[1]
    det = math.cos(hp) * -math.sin(hq) - (-math.cos(hq)) * math.sin(hp)
    t = (dx * -math.sin(hq) + math.cos(hq) * dy) / det
    return (p[0] + t * math.cos(hp), p[1] + t * math.sin(hp))


def scs_geometry():
    """Spiral-curve-spiral, turning left, in local feet from a start at (0, 0).

    Returns every point the LandXML needs plus the exact total length.
    """
    r, ls = SPIRAL_R, SPIRAL_LEN
    d_s = ls / (2.0 * r)                       # deflection of one spiral, radians
    d_c = math.radians(ARC_DEFLECTION)         # deflection of the arc

    # Entry spiral, exactly, in its own frame: origin at TS, heading +x.
    t = math.sqrt(ls / (math.pi * r))
    c, s_ = fresnel(t)
    k = math.sqrt(math.pi * r * ls)
    local_sc = (k * c, k * s_)

    ts = (-TANGENT, 0.0)                       # start of the alignment
    begin_spiral = (0.0, 0.0)                  # TS: tangent -> spiral
    sc = local_sc                              # SC: spiral -> curve
    centre = (sc[0] - r * math.sin(d_s), sc[1] + r * math.cos(d_s))
    # CS: curve -> spiral, by rotating SC about the arc centre.
    v = (sc[0] - centre[0], sc[1] - centre[1])
    cs = (centre[0] + _rot(d_c, v)[0], centre[1] + _rot(d_c, v)[1])
    # Mid-arc, for the <Curve> element's own record.
    mid = (centre[0] + _rot(d_c / 2.0, v)[0], centre[1] + _rot(d_c / 2.0, v)[1])

    theta_end = 2.0 * d_s + d_c
    # An exit spiral read backwards is an entry spiral: same shape, mirrored.
    st = (cs[0] + _rot(theta_end, (local_sc[0], -local_sc[1]))[0],
          cs[1] + _rot(theta_end, (local_sc[0], -local_sc[1]))[1])
    end = (st[0] + TANGENT * math.cos(theta_end), st[1] + TANGENT * math.sin(theta_end))

    return {
        "ts": ts,
        "begin_spiral": begin_spiral,
        "sc": sc,
        "mid": mid,
        "cs": cs,
        "st": st,
        "end": end,
        "centre": centre,
        "pi_in": _intersect(begin_spiral, 0.0, sc, d_s),
        "pi_out": _intersect(cs, d_s + d_c, st, theta_end),
        "theta_spiral_deg": math.degrees(d_s),
        "theta_end": theta_end,
        "length": TANGENT + ls + r * d_c + ls + TANGENT,
    }


def _xy(p):
    """Local feet -> the LandXML ordinate pair, which is northing first."""
    return f"{N0 + p[1]:.4f} {E0 + p[0]:.4f}"


def _landxml(name: str, sta_start: float, coordgeom: str, equations: str = "") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2" version="1.2"
         date="2026-09-10" time="09:00:00">
  <Units>
    <Imperial linearUnit="USSurveyFoot" areaUnit="squareFoot"
              volumeUnit="cubicFeet" temperatureUnit="fahrenheit"
              pressureUnit="inHG" angularUnit="decimal degrees"
              directionUnit="decimal degrees"/>
  </Units>
  <CoordinateSystem epsgCode="2264" horizontalDatum="NAD83"
                    horizontalCoordinateSystemName="NAD83 / North Carolina (ftUS)"/>
  <Alignments name="Roadway">
    <Alignment name="{name}" length="{{length}}" staStart="{sta_start:.4f}">
      <CoordGeom>
{coordgeom}
      </CoordGeom>{equations}
    </Alignment>
  </Alignments>
</LandXML>
"""


def spiral_fixture() -> None:
    """fixtures/alignment_spiral.xml -- tangent, spiral, arc, spiral, tangent."""
    g = scs_geometry()
    coordgeom = "\n".join([
        "        <Line>",
        f"          <Start>{_xy(g['ts'])}</Start>",
        f"          <End>{_xy(g['begin_spiral'])}</End>",
        "        </Line>",
        f'        <Spiral length="{SPIRAL_LEN:.4f}" radiusStart="INF" '
        f'radiusEnd="{SPIRAL_R:.4f}" rot="ccw" spiType="clothoid" '
        f'theta="{g["theta_spiral_deg"]:.6f}">',
        f"          <Start>{_xy(g['begin_spiral'])}</Start>",
        f"          <PI>{_xy(g['pi_in'])}</PI>",
        f"          <End>{_xy(g['sc'])}</End>",
        "        </Spiral>",
        f'        <Curve rot="ccw" radius="{SPIRAL_R:.4f}">',
        f"          <Start>{_xy(g['sc'])}</Start>",
        f"          <Center>{_xy(g['centre'])}</Center>",
        f"          <End>{_xy(g['cs'])}</End>",
        "        </Curve>",
        f'        <Spiral length="{SPIRAL_LEN:.4f}" radiusStart="{SPIRAL_R:.4f}" '
        f'radiusEnd="INF" rot="ccw" spiType="clothoid" '
        f'theta="{g["theta_spiral_deg"]:.6f}">',
        f"          <Start>{_xy(g['cs'])}</Start>",
        f"          <PI>{_xy(g['pi_out'])}</PI>",
        f"          <End>{_xy(g['st'])}</End>",
        "        </Spiral>",
        "        <Line>",
        f"          <Start>{_xy(g['st'])}</Start>",
        f"          <End>{_xy(g['end'])}</End>",
        "        </Line>",
    ])
    xml = _landxml("CL-SPIRAL", STA_START, coordgeom).replace(
        "{length}", f"{g['length']:.4f}"
    )
    (HERE / "alignment_spiral.xml").write_text(xml)

    # The expected answers, so a test never has to re-derive them.
    truth = {
        "note": "spiral-curve-spiral, computed from Fresnel series, EPSG:2264 ftUS",
        "radius": SPIRAL_R,
        "spiral_length": SPIRAL_LEN,
        "arc_deflection_deg": ARC_DEFLECTION,
        "tangent": TANGENT,
        "sta_start": STA_START,
        "length": g["length"],
        "points": {
            k: [E0 + v[0], N0 + v[1]]
            for k, v in g.items()
            if isinstance(v, tuple)
        },
        "total_deflection_deg": math.degrees(g["theta_end"]),
    }
    (HERE / "alignment_spiral_truth.json").write_text(json.dumps(truth, indent=2) + "\n")


def equation_fixture() -> None:
    """fixtures/alignment_equations.xml -- CL-MAIN, re-stationed twice.

    One gap and one overlap, because they fail differently: a gap loses
    stations that never existed, an overlap makes one station name two points.
    """
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
    equations = (
        "\n      <StaEquation staInternal=\"1400.0000\" staBack=\"1400.0000\" "
        "staAhead=\"2000.0000\" desc=\"gap: 14+00 back = 20+00 ahead\"/>"
        "\n      <StaEquation staInternal=\"1800.0000\" staBack=\"2400.0000\" "
        "staAhead=\"2100.0000\" desc=\"overlap: 24+00 back = 21+00 ahead\"/>"
    )
    xml = _landxml(
        "CL-EQUATED", STA_START, "\n".join(segs), equations
    ).replace("{length}", f"{total:.4f}")
    (HERE / "alignment_equations.xml").write_text(xml)


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

    spiral_fixture()
    equation_fixture()

    print("wrote fixtures to", HERE)


if __name__ == "__main__":
    main()
