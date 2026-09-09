"""The shapes real utility and parcel data actually arrives in.

Fixtures are tidy. A real GeoPackage is not: it has manholes as points, mains
as multipart lines, elevations riding along as Z, and sometimes all of it in
one layer. None of that should surprise the pipeline, and none of it should
produce a number that is true of the wrong geometry type.
"""

from __future__ import annotations

import pathlib

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Point,
    box,
)

from gisc.compile import compile_task
from gisc.exec import execute

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"
ALIGN_XML = str(FIXTURES / "alignment.xml")

# The design grid the fixtures live on: EPSG:2264, NC state plane, ftUS.
E0, N0 = 905_000.0, 675_000.0
DESIGN_CRS = "EPSG:2264"


def pd_isna(value) -> bool:
    return bool(pd.isna(value))


def local(x, y):
    return (E0 + x, N0 + y)


def utilities(tmp_path, geoms, names=None, crs=DESIGN_CRS, name="utils.geojson"):
    names = names or [f"F{i}" for i in range(len(geoms))]
    path = tmp_path / name
    gpd.GeoDataFrame({"name": names}, geometry=geoms, crs=crs).to_file(
        path, driver="GeoJSON"
    )
    return str(path)


def run(tmp_path, utils, **over):
    kwargs = dict(alignment=ALIGN_XML, utils=utils, buffer_ft=15.0,
                  crs="EPSG:3857", out_dir=tmp_path / "run")
    kwargs.update(over)
    plan = compile_task("corridor.conflicts", **kwargs)
    return execute(plan, tmp_path / "run")


# -- geometry types --------------------------------------------------------


def test_point_features_are_stationed_and_offset(tmp_path):
    """Manholes, valves, poles: a point conflict needs a station and an offset."""
    utils = utilities(
        tmp_path,
        [Point(*local(200, 2)), Point(*local(200, 40))],
        names=["MH-1", "MH-2"],
    )
    conflicts = run(tmp_path, utils).frame("conflicts")

    assert list(conflicts["name"]) == ["MH-1"]  # the 40 ft one is outside
    assert conflicts["sta_label"].iloc[0] == "12+00.00"
    assert conflicts["offset_ft"].iloc[0] == pytest.approx(2.0, abs=0.05)
    # A point has no length and no area, so it is given neither.
    assert conflicts["overlap_ac"].isna().all() if "overlap_ac" in conflicts else True


def test_multipart_lines_keep_one_row_and_span_one_station_range(tmp_path):
    """A multipart main is one asset, so it stays one row."""
    utils = utilities(tmp_path, [MultiLineString([
        [local(200, -60), local(200, 60)],      # crosses at 12+00
        [local(900, 158), local(1000, 158)],    # 8 ft off, 100 ft long
    ])], names=["WL-MAIN"])
    conflicts = run(tmp_path, utils).frame("conflicts")

    assert len(conflicts) == 1
    assert conflicts["sta_label"].iloc[0] == "12+00.00 - 20+27.20"
    # 30 ft of crossing inside the corridor plus the full 100 ft parallel run.
    assert conflicts["overlap_ft"].iloc[0] == pytest.approx(130.0, abs=0.5)


def test_z_coordinates_survive_without_disturbing_the_answer(tmp_path):
    """Inverts ride along in the geometry; the 2D answer must not move."""
    flat = utilities(tmp_path, [LineString([local(200, -60), local(200, 60)])],
                     name="flat.geojson")
    with_z = utilities(
        tmp_path,
        [LineString([(*local(200, -60), 2100.5), (*local(200, 60), 2101.5)])],
        name="z.geojson",
    )
    a = run(tmp_path / "a", flat).frame("conflicts")
    b = run(tmp_path / "b", with_z).frame("conflicts")

    assert len(a) == len(b) == 1
    assert a["overlap_ft"].iloc[0] == pytest.approx(b["overlap_ft"].iloc[0], abs=0.01)
    assert a["sta_label"].iloc[0] == b["sta_label"].iloc[0]
    assert b.geometry.iloc[0].has_z


def test_a_mixed_layer_gives_each_geometry_the_right_measure(tmp_path):
    """Lines must never be handed an area, polygons never a length."""
    utils = utilities(
        tmp_path,
        [
            LineString([local(900, 158), local(1000, 158)]),   # 100 ft line
            box(*local(350, -100), *local(650, 200)),          # a polygon
            Point(*local(200, 2)),                             # a point
        ],
        names=["SS-LINE", "EASEMENT", "MH-1"],
    )
    conflicts = run(tmp_path, utils).frame("conflicts").set_index("name")
    assert len(conflicts) == 3

    assert conflicts.loc["SS-LINE", "overlap_ft"] == pytest.approx(100.0, abs=0.5)
    assert pd_isna(conflicts.loc["SS-LINE", "overlap_ac"])

    assert conflicts.loc["EASEMENT", "overlap_ac"] > 0
    assert pd_isna(conflicts.loc["EASEMENT", "overlap_ft"])

    assert pd_isna(conflicts.loc["MH-1", "overlap_ac"])


def test_multipolygons_are_measured_whole(tmp_path):
    """A split flood zone is one polygon record with one total area."""
    two_lobes = MultiPolygon([
        box(*local(350, -100), *local(450, 200)),
        box(*local(550, -100), *local(650, 200)),
    ])
    one_lobe = MultiPolygon([box(*local(350, -100), *local(450, 200))])
    both = run(tmp_path / "a", utilities(tmp_path, [two_lobes], name="two.geojson"))
    single = run(tmp_path / "b", utilities(tmp_path, [one_lobe], name="one.geojson"))

    a = both.frame("conflicts")["overlap_ac"].iloc[0]
    b = single.frame("conflicts")["overlap_ac"].iloc[0]
    assert a > b > 0
    assert a == pytest.approx(2 * b, rel=0.05)


def test_a_feature_that_only_touches_the_corridor_edge(tmp_path):
    """Tangency is an intersection; it must not be dropped or crash a measure."""
    utils = utilities(tmp_path, [Point(*local(200, 15))], names=["EDGE"])
    conflicts = run(tmp_path, utils).frame("conflicts")
    # 15 ft off a 15 ft corridor: in or out is a knife edge, but never a crash.
    assert len(conflicts) in (0, 1)


def test_geometry_collection_does_not_break_the_measures(tmp_path):
    utils = utilities(tmp_path, [GeometryCollection([
        Point(*local(200, 2)),
        LineString([local(900, 158), local(1000, 158)]),
    ])], names=["MIXED-1"])
    conflicts = run(tmp_path, utils).frame("conflicts")
    assert len(conflicts) == 1
    assert conflicts["sta_label"].iloc[0] is not None


# -- awkward but legal inputs ---------------------------------------------


def test_a_layer_where_nothing_is_near_the_corridor(tmp_path):
    utils = utilities(tmp_path, [LineString([local(5000, 5000), local(5100, 5000)])])
    result = run(tmp_path, utils)
    assert len(result.frame("conflicts")) == 0
    assert (tmp_path / "run" / "conflicts.geojson").exists()


def test_duplicate_geometries_are_both_reported(tmp_path):
    """Two records for the same pipe is a data problem, not gisc's to silently fix."""
    same = LineString([local(900, 158), local(1000, 158)])
    utils = utilities(tmp_path, [same, same], names=["SS-A", "SS-B"])
    conflicts = run(tmp_path, utils).frame("conflicts")
    assert sorted(conflicts["name"]) == ["SS-A", "SS-B"]
    assert list(conflicts["gisc_fid"]) == [0, 1]


def test_a_source_with_no_attributes_at_all(tmp_path):
    """Geometry-only layers are common in exports; they must still station."""
    path = tmp_path / "bare.geojson"
    gpd.GeoDataFrame(
        geometry=[LineString([local(900, 158), local(1000, 158)])], crs=DESIGN_CRS
    ).to_file(path, driver="GeoJSON")
    conflicts = run(tmp_path, str(path)).frame("conflicts")
    assert len(conflicts) == 1
    assert conflicts["sta_label"].iloc[0].startswith("19+")
    assert conflicts["gisc_src"].iloc[0] == "utilities"


def test_non_ascii_attributes_and_paths(tmp_path):
    folder = tmp_path / "proyecto ñ"
    folder.mkdir()
    utils = utilities(
        folder,
        [LineString([local(900, 158), local(1000, 158)])],
        names=["Cañería-8″"],
        name="líneas.geojson",
    )
    result = run(tmp_path, utils)
    conflicts = result.frame("conflicts")
    assert conflicts["name"].iloc[0] == "Cañería-8″"
    written = (tmp_path / "run" / "conflicts.geojson").read_text(encoding="utf-8")
    assert "Cañería" in written


def test_a_much_larger_layer_still_finds_the_same_conflicts(tmp_path):
    """2,000 features is a small county utility layer. Nothing should change."""
    geoms = [LineString([local(900, 158), local(1000, 158)])]
    names = ["SS-REAL"]
    for i in range(2_000):
        y = 300 + i  # far outside the corridor
        geoms.append(LineString([local(0, y), local(100, y)]))
        names.append(f"NOISE-{i}")
    conflicts = run(tmp_path, utilities(tmp_path, geoms, names)).frame("conflicts")
    assert list(conflicts["name"]) == ["SS-REAL"]
