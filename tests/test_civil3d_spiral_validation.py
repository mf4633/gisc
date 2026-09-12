"""gisc against Autodesk Civil 3D, on the two things it had no evidence for.

``test_civil3d_validation.py`` bounded what chord flattening costs on a
circular arc. It left the README's own confession standing: a curved alignment
from a real export, spirals, and station equations were **not exercised**.

This closes it. An alignment was built in Civil 3D 2023 over COM --

    500 ft tangent, entry clothoid Ls=150, arc R=300, exit clothoid Ls=150,
    500 ft tangent, fitted to a 45 deg PI, on CSCode NC83F (EPSG:2264),
    stationed from 10+00, with a station equation 14+00 back = 20+00 ahead

-- and Civil 3D was asked for its own length, its own station and offset at
eight probes, its own coordinates at 40 stations, and its own equated station
strings. The LandXML gisc reads was written from the geometry read back out of
Civil 3D, so gisc is parsing Civil 3D's coordinates rather than the design
that was fed in.

``validation/capture_civil3d_spiral.py`` and ``validation/write_spiral_landxml.py``
regenerate all of it. The artifacts are committed, so these tests run
anywhere; Civil 3D is not needed to check them.

**Civil 3D signs offsets negative-left, positive-right.** gisc reports an
unsigned distance and a side, so the comparison is on magnitude plus side.
"""

from __future__ import annotations

import json
import math
import pathlib

import geopandas as gpd
import pytest
from shapely.geometry import Point

from gisc import exec as engine
from gisc.adapters import landxml
from gisc.adapters.landxml import _clothoid
from gisc.ir import Plan, Source

pytestmark = pytest.mark.civil3d

CIVIL3D = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "civil3d"
LANDXML = CIVIL3D / "alignment_spiral_c3d.xml"
TRUTH = CIVIL3D / "c3d_spiral_truth.json"

TOL_LENGTH_FT = 0.01
TOL_STATION_FT = 0.05
TOL_OFFSET_FT = 0.02
TOL_XY_FT = 0.011  # ARC_TOLERANCE plus rounding


@pytest.fixture(scope="module")
def truth() -> dict:
    return json.loads(TRUTH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def centerline():
    return landxml.read(str(LANDXML))


def label_to_station(text: str) -> float:
    """"20+47.88" -> 2047.88, so Civil 3D's own string can be compared."""
    whole, _, rem = text.partition("+")
    return float(whole) * 100.0 + float(rem)


def sampled(centerline, points):
    """Run gisc's sample op over ``points`` against the Civil 3D centreline."""
    gdf, _ = centerline
    targets = gpd.GeoDataFrame(
        {"i": list(range(len(points)))},
        geometry=[Point(x, y) for x, y in points],
        crs=gdf.crs,
    )
    env = {"p": targets, "cl": gdf.copy()}
    plan = Plan(task="t", crs=gdf.crs.to_string(), buffer_ft=15.0)
    plan.sources = [Source(id="p", kind="geojson", ref="probes")]
    engine._DISPATCH["sample"](
        {"op": "sample", "src": "p", "along": "cl", "out": "p", "measure_in": "native"},
        plan, env, {"sources": {}, "ops": [], "outputs": {}}, {},
    )
    return env["p"]


# -- what Civil 3D built ---------------------------------------------------


def test_civil3d_built_the_alignment_that_was_asked_for(truth):
    kinds = [e["type"] for e in truth["landxml"]["elements"]]
    assert kinds == ["line", "spiral", "arc", "spiral", "line"]
    spirals = [e for e in truth["landxml"]["elements"] if e["type"] == "spiral"]
    assert all(s["length"] == 150.0 for s in spirals)
    assert all(s["spiral_type"] == 1 for s in spirals)  # clothoid
    # None is the tangent end: an infinite radius has no JSON number.
    assert spirals[0]["radius_in"] is None and spirals[0]["radius_out"] == 300.0
    assert spirals[1]["radius_in"] == 300.0 and spirals[1]["radius_out"] is None


def test_crs_matches_what_civil3d_had(centerline, truth):
    gdf, prov = centerline
    assert gdf.crs.to_string() == "EPSG:2264"
    assert gdf.crs.axis_info[0].unit_name == "US survey foot"
    assert truth["coordinate_system"]["cs_code"] == "NC83F"
    assert prov["crs_source"] == "declared"


# -- the spiral ------------------------------------------------------------


def test_gisc_integrates_the_same_clothoid_civil3d_carries(truth):
    """The direct comparison: Civil 3D's own TotalX/TotalY for the spiral,
    against gisc integrating the heading from length and radius alone."""
    spiral = next(e for e in truth["landxml"]["elements"] if e["type"] == "spiral")
    radius, length = spiral["radius_out"], spiral["length"]

    pts, heading = _clothoid((0.0, 0.0), 0.0, 0.0, 1.0 / radius, length, 1.0, 64)
    x, y = pts[-1]

    assert x == pytest.approx(spiral["total_x"], abs=1e-6)
    assert y == pytest.approx(spiral["total_y"], abs=1e-6)
    assert heading == pytest.approx(spiral["delta"], abs=1e-9)


def test_the_export_parses_as_five_elements(centerline):
    _gdf, prov = centerline
    flattened = [n for n in prov["notes"] if "flattened" in n]
    assert sum("<Spiral>" in n for n in flattened) == 2
    assert sum("<Curve>" in n for n in flattened) == 1
    assert prov["vertices"] > 50


def test_length_matches_civil3d(centerline, truth):
    """Chording shortens. The shortfall must stay under 0.01 ft, and be short."""
    gdf, _ = centerline
    delta = gdf.geometry.iloc[0].length - truth["alignment"]["length"]
    assert abs(delta) < TOL_LENGTH_FT, f"off by {delta:+.4f} ft"
    assert delta < 0


def test_centreline_tracks_civil3d_through_both_spirals(centerline, truth):
    """40 points at 25 ft spacing, against Civil 3D's own coordinates."""
    gdf, _ = centerline
    line = gdf.geometry.iloc[0]
    worst = 0.0
    for row in truth["alignment"]["point_locations"]:
        p = Point(row["x"], row["y"])
        worst = max(worst, line.distance(p))
    assert worst <= TOL_XY_FT, f"worst deviation {worst:.6f} ft"


# -- station and offset ----------------------------------------------------


def test_offsets_match_civil3d_in_magnitude_and_side(centerline, truth):
    probes = truth["alignment"]["station_offsets"]
    got = sampled(centerline, [(p["x"], p["y"]) for p in probes])

    for probe, (_i, row) in zip(probes, got.iterrows()):
        assert row["offset_ft"] == pytest.approx(abs(probe["offset"]), abs=TOL_OFFSET_FT)
        if abs(probe["offset"]) > 1e-9:
            # Civil 3D: negative is left. gisc says which side in words.
            assert row["side"] == ("L" if probe["offset"] < 0 else "R")
        else:
            assert row["side"] == "CL"


def test_raw_stations_match_civil3d(centerline, truth):
    """Before the equation is applied, gisc and Civil 3D must agree on distance."""
    probes = truth["alignment"]["station_offsets"]
    got = sampled(centerline, [(p["x"], p["y"]) for p in probes])

    for probe, (_i, row) in zip(probes, got.iterrows()):
        assert row["sta_raw"] == pytest.approx(probe["station"], abs=TOL_STATION_FT), (
            f"raw station at ({probe['x']}, {probe['y']})"
        )


def test_equated_stations_match_civil3ds_own_station_strings(centerline, truth):
    """The one that matters. Civil 3D prints 20+47.88 where the raw station is
    14+47.88; gisc has to print 20+47.88 too, or a contractor digs in the
    wrong place."""
    probes = truth["alignment"]["station_offsets"]
    got = sampled(centerline, [(p["x"], p["y"]) for p in probes])

    for probe, (_i, row) in zip(probes, got.iterrows()):
        expected = label_to_station(probe["station_string"])
        assert row["sta"] == pytest.approx(expected, abs=TOL_STATION_FT), (
            f"Civil 3D says {probe['station_string']}, gisc says {row['sta']}"
        )


def test_the_equation_actually_moved_something(truth):
    """A guard on the fixture: if the equation stopped being exercised, these
    tests would pass while proving nothing."""
    probes = truth["alignment"]["station_offsets"]
    shifted = [p for p in probes
               if abs(label_to_station(p["station_string"]) - p["station"]) > 1.0]
    assert len(shifted) >= 4, "the capture no longer spans the station equation"
    for p in shifted:
        # Civil 3D's station string is rounded to hundredths; the raw station
        # is not, so the shift can only be checked to that rounding.
        shift = label_to_station(p["station_string"]) - p["station"]
        assert shift == pytest.approx(600.0, abs=0.01)


def test_gisc_read_the_equation_civil3d_wrote(centerline, truth):
    _gdf, prov = centerline
    equations = prov["stationing"]["equations"]
    assert len(equations) == len(truth["landxml"]["equations"]) == 1
    assert equations[0]["internal"] == truth["landxml"]["equations"][0]["raw"]
    assert equations[0]["ahead"] == truth["landxml"]["equations"][0]["ahead"]
    assert [r["offset"] for r in prov["stationing"]["regions"]] == [0.0, 600.0]
