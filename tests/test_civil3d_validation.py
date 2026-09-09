"""gisc against Autodesk Civil 3D.

Everything else in this suite checks gisc against itself. This checks it
against the tool the drawings actually come from.

An alignment was built in Civil 3D 2023 over COM -- a 400 ft tangent, a 45 deg
arc at R=300, another 400 ft tangent, on NC state plane (CSCode NC83F, which
is EPSG:2264), stationed from 10+00. Civil 3D was then asked for its own
length, its own station and offset at seven probe points, and its own
coordinates at 21 stations. Its answers are in ``fixtures/civil3d/``, together
with a LandXML written from the geometry read back out of Civil 3D -- so gisc
is parsing Civil 3D's coordinates, not a restatement of the design intent.

``validation/capture_civil3d.py`` regenerates all of it. The artifacts are
committed, so these tests run anywhere; Civil 3D is not needed to check them.

The interesting comparison is the arc. Civil 3D carries a true circular arc.
gisc flattens it to chords at ``landxml.ARC_TOLERANCE``. These tests bound the
resulting disagreement in feet, which is the only unit that settles whether
the tolerance is fit for the job.
"""

from __future__ import annotations

import json
import pathlib

import geopandas as gpd
import pytest
from shapely.geometry import Point

from gisc import exec as engine
from gisc.adapters import landxml
from gisc.ir import Plan, Source

pytestmark = pytest.mark.civil3d

CIVIL3D = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "civil3d"
LANDXML = CIVIL3D / "alignment_c3d.xml"
TRUTH = CIVIL3D / "c3d_truth.json"

# What a survey crew would call agreement. 0.01 ft is about an eighth of an inch.
TOL_LENGTH_FT = 0.01
TOL_STATION_FT = 0.05
TOL_OFFSET_FT = 0.02
TOL_XY_FT = 0.011  # ARC_TOLERANCE plus rounding


@pytest.fixture(scope="module")
def truth() -> dict:
    return json.loads(TRUTH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def centerline():
    gdf, prov = landxml.read(str(LANDXML))
    return gdf, prov


def sampled(centerline, truth, points):
    """Run gisc's sample op over ``points`` against the Civil 3D centerline."""
    gdf, _ = centerline
    ref = gdf.copy()
    ref["sta_start"] = truth["alignment"]["starting_station"]
    targets = gpd.GeoDataFrame(
        {"i": list(range(len(points)))},
        geometry=[Point(x, y) for x, y in points],
        crs=gdf.crs,
    )
    env = {"p": targets, "cl": ref}
    plan = Plan(task="t", crs=gdf.crs.to_string(), buffer_ft=15.0)
    plan.sources = [Source(id="p", kind="geojson", ref="probes")]
    engine._DISPATCH["sample"](
        {"op": "sample", "src": "p", "along": "cl", "out": "p"},
        plan, env, {"sources": {}, "ops": [], "outputs": {}}, {},
    )
    return env["p"]


# -- the coordinate system -------------------------------------------------


def test_crs_matches_what_civil3d_had(centerline, truth):
    """Civil 3D's CSCode NC83F and the LandXML's epsgCode must be one system."""
    gdf, prov = centerline
    assert gdf.crs.to_string() == "EPSG:2264"
    assert gdf.crs.axis_info[0].unit_name == "US survey foot"
    assert truth["coordinate_system"]["cs_code"] == "NC83F"
    assert "US Foot" in truth["coordinate_system"]["description"]
    assert "North Carolina" in truth["coordinate_system"]["description"]
    assert prov["crs_source"] == "declared"


# -- geometry --------------------------------------------------------------


def test_landxml_parses_to_three_civil3d_entities(centerline):
    gdf, prov = centerline
    assert len(gdf) == 1
    assert prov["vertices"] > 10  # the arc was flattened, so more than 4
    assert any("flattened" in n for n in prov["notes"])


def test_length_matches_civil3d(centerline, truth):
    """Chording an arc shortens it. The shortfall must stay under 0.01 ft."""
    gdf, _ = centerline
    gisc_length = gdf.geometry.iloc[0].length
    c3d_length = truth["alignment"]["length"]
    delta = gisc_length - c3d_length

    assert abs(delta) < TOL_LENGTH_FT, f"gisc {gisc_length} vs Civil 3D {c3d_length}"
    # Chords cut corners, so gisc must come in short, never long.
    assert delta < 0


def test_centerline_tracks_civil3d_at_every_sampled_station(centerline, truth):
    """21 stations along the alignment, compared to Civil 3D's own coordinates."""
    gdf, _ = centerline
    line = gdf.geometry.iloc[0]
    sta_start = truth["alignment"]["starting_station"]

    worst, worst_station = 0.0, None
    for row in truth["alignment"]["point_locations"]:
        here = line.interpolate(row["station"] - sta_start)
        error = ((here.x - row["x"]) ** 2 + (here.y - row["y"]) ** 2) ** 0.5
        if error > worst:
            worst, worst_station = error, row["station"]

    assert worst < TOL_XY_FT, f"worst deviation {worst:.6f} ft at station {worst_station}"
    # The chord tolerance is a promise; hold it to it.
    assert worst <= landxml.ARC_TOLERANCE + 0.002


# -- stationing ------------------------------------------------------------


def test_stations_and_offsets_match_civil3d(centerline, truth):
    probes = truth["alignment"]["station_offsets"]
    out = sampled(centerline, truth, [(p["x"], p["y"]) for p in probes])

    for probe, (_, got) in zip(probes, out.iterrows()):
        where = f"({probe['x']:.2f}, {probe['y']:.2f})"
        assert abs(got["sta"] - probe["station"]) < TOL_STATION_FT, (
            f"station at {where}: gisc {got['sta']} vs Civil 3D {probe['station']}"
        )
        # Civil 3D signs offsets negative-left, positive-right; gisc reports a
        # magnitude plus a side. Compare the magnitudes.
        assert abs(got["offset_ft"] - abs(probe["offset"])) < TOL_OFFSET_FT, (
            f"offset at {where}: gisc {got['offset_ft']} vs Civil 3D {probe['offset']}"
        )


def test_stations_on_tangents_are_exact(centerline, truth):
    """Off the arc there is no flattening, so agreement should be near-perfect."""
    probes = [p for p in truth["alignment"]["station_offsets"]
              if p["station"] <= 1400.0]
    assert len(probes) >= 4
    out = sampled(centerline, truth, [(p["x"], p["y"]) for p in probes])
    for probe, (_, got) in zip(probes, out.iterrows()):
        assert abs(got["sta"] - probe["station"]) < 0.001


def test_side_matches_civil3ds_offset_sign(centerline, truth):
    """L/R must agree with Civil 3D's sign, or the report reads backwards."""
    probes = [p for p in truth["alignment"]["station_offsets"] if abs(p["offset"]) > 1.0]
    assert len(probes) >= 3, "need probes clearly off the centerline"
    out = sampled(centerline, truth, [(p["x"], p["y"]) for p in probes])

    for probe, (_, got) in zip(probes, out.iterrows()):
        expected = "L" if probe["offset"] < 0 else "R"
        assert got["side"] == expected, (
            f"at ({probe['x']:.2f}, {probe['y']:.2f}) Civil 3D offset "
            f"{probe['offset']} means {expected}, gisc said {got['side']}"
        )


def test_a_point_off_the_arc_still_lands_within_tolerance(centerline, truth):
    """The hardest case: nearest approach to a curve, not a straight."""
    probe = max(truth["alignment"]["station_offsets"],
                key=lambda p: p["station"] if abs(p["offset"]) > 1.0 else -1)
    assert probe["station"] > 1400.0, "expected a probe off the arc"

    out = sampled(centerline, truth, [(probe["x"], probe["y"])])
    got = out.iloc[0]
    assert abs(got["sta"] - probe["station"]) < TOL_STATION_FT
    assert abs(got["offset_ft"] - abs(probe["offset"])) < TOL_OFFSET_FT


# -- the corridor task, end to end, on Civil 3D geometry -------------------


def test_corridor_conflicts_runs_on_a_civil3d_alignment(tmp_path, truth):
    """The whole point: a real Civil 3D alignment drives the actual task."""
    from gisc.compile import compile_task
    from gisc.exec import execute
    from shapely.geometry import LineString

    design = truth["design"]
    e0, n0 = design["E0"], design["N0"]
    utils = tmp_path / "utils.geojson"
    gpd.GeoDataFrame(
        {"name": ["WL-CROSSING", "SS-PARALLEL", "GAS-CLEAR"]},
        geometry=[
            LineString([(e0 + 200, n0 - 50), (e0 + 200, n0 + 50)]),   # crosses at 12+00
            LineString([(e0 + 100, n0 + 8), (e0 + 300, n0 + 8)]),     # 8 ft off
            LineString([(e0 + 100, n0 + 60), (e0 + 300, n0 + 60)]),   # 60 ft off
        ],
        crs="EPSG:2264",
    ).to_file(utils, driver="GeoJSON")

    out = tmp_path / "run"
    plan = compile_task(
        "corridor.conflicts", alignment=str(LANDXML), utils=str(utils),
        buffer_ft=15.0, crs="EPSG:2264", out_dir=out,
    )
    result = execute(plan, out)

    conflicts = result.frame("conflicts").set_index("name")
    assert sorted(conflicts.index) == ["SS-PARALLEL", "WL-CROSSING"]
    assert conflicts.loc["WL-CROSSING", "sta_label"] == "12+00.00"
    assert conflicts.loc["SS-PARALLEL", "offset_ft"] == pytest.approx(8.0, abs=0.02)
    assert conflicts.loc["SS-PARALLEL", "side"] == "L"
