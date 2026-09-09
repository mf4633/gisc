"""corridor.conflicts, end to end.

The fixtures are laid out in EPSG:2264 (NC state plane, ftUS) so the expected
answers are arithmetic, not eyeballed:

    alignment  CL-MAIN, three tangents, staStart 10+00
    WL-8IN     crosses the alignment at STA 12+00          -> inside 15 ft
    SS-12IN    parallel, 8 ft off centerline               -> inside 15 ft
    GAS-4IN    parallel, 40 ft off centerline              -> outside 15 ft
    flood AE   overlaps the corridor                       -> hit
    flood X    2000 ft away                                -> miss
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import geopandas as gpd
import pytest
from shapely.geometry import LineString

from gisc import units
from gisc.compile import compile_task
from gisc.errors import MissingCRSError, StubError, UsageError
from gisc.exec import execute
from gisc.ir import OPS, validate

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"
ALIGN_XML = str(FIXTURES / "alignment.xml")
ALIGN_GJ = str(FIXTURES / "alignment.geojson")
UTILS = str(FIXTURES / "utilities.geojson")
UTILS_GPKG = str(FIXTURES / "utilities.gpkg")
UTILS_NOCRS = str(FIXTURES / "utilities_nocrs.gpkg")
FLOOD = str(FIXTURES / "flood.geojson")


def _compile(out_dir, **over):
    kwargs = dict(
        alignment=ALIGN_XML, utils=UTILS, flood=FLOOD,
        buffer_ft=15.0, crs="EPSG:3857", out_dir=out_dir,
    )
    kwargs.update(over)
    return compile_task("corridor.conflicts", **kwargs)


# -- 1. compile the IR without executing -----------------------------------


def test_compiles_ir_without_executing(tmp_path):
    plan = _compile(tmp_path)

    assert plan.task == "corridor.conflicts"
    assert plan.crs == "EPSG:3857"
    assert plan.buffer_ft == 15.0

    sources = {s.id: s for s in plan.sources}
    assert set(sources) == {"alignment", "utilities", "flood"}
    assert sources["alignment"].kind == "landxml"
    assert sources["alignment"].native_crs == "EPSG:2264"  # from the header probe
    assert sources["utilities"].kind == "geojson"

    ops = [o["op"] for o in plan.ops]
    assert set(ops) <= set(OPS), "the IR grew an op that is not in the stable set"
    assert ops.count("buffer") == 1
    assert ops.count("intersect") == 2  # utilities and flood
    validate(plan)

    # Compiling must not have produced anything.
    assert list(tmp_path.iterdir()) == []


def test_no_probe_opens_nothing(tmp_path):
    plan = compile_task(
        "corridor.conflicts",
        alignment="does/not/exist.xml", utils="also/missing.geojson",
        buffer_ft=15.0, crs="EPSG:3857", out_dir=tmp_path, probe=False,
    )
    assert plan.source("alignment").native_crs is None


def test_ir_rejects_unknown_op(tmp_path):
    plan = _compile(tmp_path)
    plan.ops.append({"op": "dissolve", "src": "conflicts", "out": "x"})
    with pytest.raises(UsageError, match="unknown op"):
        validate(plan)


def test_ir_rejects_undefined_reference(tmp_path):
    plan = _compile(tmp_path)
    plan.ops.append({"op": "write", "src": "nope", "to": "x.geojson"})
    with pytest.raises(UsageError, match="not defined yet"):
        validate(plan)


# -- 2. execute on the fixtures --------------------------------------------


def test_two_of_three_utilities_conflict(tmp_path):
    result = execute(_compile(tmp_path), tmp_path)

    assert result.count("utilities") == 3
    conflicts = result.frame("conflicts")
    assert len(conflicts) == 2
    assert sorted(conflicts["name"]) == ["SS-12IN", "WL-8IN"]
    assert "GAS-4IN" not in set(conflicts["name"])


def test_one_of_two_flood_polygons_intersects(tmp_path):
    result = execute(_compile(tmp_path), tmp_path)
    hits = result.frame("flood_hits")
    assert len(hits) == 1
    assert hits["zone"].iloc[0] == "AE"


def test_conflicts_are_stationed(tmp_path):
    result = execute(_compile(tmp_path), tmp_path)
    conflicts = result.frame("conflicts").set_index("name")

    # WL-8IN crosses at 200 ft past staStart 1000 -> STA 12+00, zero offset.
    assert conflicts.loc["WL-8IN", "sta"] == pytest.approx(1200.0, abs=0.1)
    assert conflicts.loc["WL-8IN", "sta_label"] == "12+00.00"
    assert conflicts.loc["WL-8IN", "offset_ft"] == pytest.approx(0.0, abs=0.05)

    # SS-12IN sits 8 ft off the centerline.
    assert conflicts.loc["SS-12IN", "offset_ft"] == pytest.approx(8.0, abs=0.05)
    assert conflicts.loc["SS-12IN", "side"] == "L"


def test_outputs_and_provenance_are_written(tmp_path):
    from gisc.compile import write_plan

    plan = _compile(tmp_path)
    write_plan(plan, tmp_path)
    result = execute(plan, tmp_path)

    conflicts = tmp_path / "conflicts.geojson"
    flood = tmp_path / "flood.geojson"
    assert conflicts.exists() and flood.exists()

    fc = json.loads(conflicts.read_text())
    assert len(fc["features"]) == 2
    # Every emitted feature says where it came from.
    for feat in fc["features"]:
        assert feat["properties"]["gisc_src"] == "utilities"
        assert feat["properties"]["gisc_fid"] is not None

    prov = result.provenance
    assert prov["crs_analysis"] == "EPSG:3857"
    assert prov["crs_out"] == "EPSG:4326"
    assert set(prov["sources"]) == {"alignment", "utilities", "flood"}
    for src in prov["sources"].values():
        assert src["ref"] and src["mtime"] and src["sha256"]
        assert src["native_crs"]
    assert prov["outputs"]["conflicts.geojson"]["features"] == 2
    assert prov["outputs"]["conflicts.geojson"]["derived_from"] == ["utilities"]
    assert prov["buffer"]["dist_ft"] == 15.0


def test_summary_reports_counts_and_crs(tmp_path):
    from gisc.tasks import corridor

    result = execute(_compile(tmp_path), tmp_path)
    text = corridor.summary(result, tmp_path)
    assert "**2 of 3 utilities within the corridor**" in text
    assert "**1 of 2 flood polygons intersecting the corridor**" in text
    assert "EPSG:3857" in text and "15 ft" in text
    assert "12+00.00" in text


def test_gpkg_source_gives_the_same_answer(tmp_path):
    result = execute(_compile(tmp_path, utils=UTILS_GPKG), tmp_path)
    assert sorted(result.frame("conflicts")["name"]) == ["SS-12IN", "WL-8IN"]


def test_buffer_size_actually_decides(tmp_path):
    tight = execute(_compile(tmp_path / "a", buffer_ft=5.0), tmp_path / "a")
    assert sorted(tight.frame("conflicts")["name"]) == ["WL-8IN"]  # 8 ft sewer drops out
    wide = execute(_compile(tmp_path / "b", buffer_ft=50.0), tmp_path / "b")
    assert len(wide.frame("conflicts")) == 3


# -- 3. missing CRS is refused, and the message names the file -------------


def test_missing_crs_is_refused_and_names_the_file(tmp_path):
    plan = _compile(tmp_path, utils=UTILS_NOCRS)
    with pytest.raises(MissingCRSError) as exc:
        execute(plan, tmp_path)
    assert "utilities_nocrs.gpkg" in str(exc.value)
    assert "will not guess" in str(exc.value)


def test_missing_crs_exits_non_zero(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "gisc.cli", "compile", "corridor.conflicts",
         "--alignment", ALIGN_XML, "--utils", UTILS_NOCRS,
         "--out", str(tmp_path / "run")],
        capture_output=True, text=True,
    )
    assert proc.returncode == MissingCRSError.exit_code
    assert "utilities_nocrs.gpkg" in proc.stderr


def test_landxml_crs_by_name_is_a_declaration(tmp_path):
    """No epsgCode, but a resolvable name is still a declaration, not a guess."""
    from gisc.adapters import landxml as lx

    named = tmp_path / "named.xml"
    named.write_text(
        pathlib.Path(ALIGN_XML).read_text().replace('epsgCode="2264"', 'epsgCode=""')
    )
    info = lx.describe(str(named))
    assert info["native_crs"] == "EPSG:2264"
    assert info["crs_source"] == "declared_by_horizontalCoordinateSystemName"


def test_landxml_without_crs_is_refused(tmp_path):
    ugly = tmp_path / "nocrs.xml"
    ugly.write_text(
        pathlib.Path(ALIGN_XML).read_text()
        .replace('epsgCode="2264"', 'epsgCode=""')
        .replace('horizontalCoordinateSystemName="NAD83 / North Carolina (ftUS)"', "")
    )
    with pytest.raises(MissingCRSError, match="nocrs.xml"):
        compile_task(
            "corridor.conflicts", alignment=str(ugly), utils=UTILS,
            buffer_ft=15.0, crs="EPSG:3857", out_dir=tmp_path,
        )
    # ...and an explicit CRS is accepted, because that is a declaration.
    plan = compile_task(
        "corridor.conflicts", alignment=str(ugly), utils=UTILS,
        buffer_ft=15.0, crs="EPSG:3857", out_dir=tmp_path, alignment_crs="EPSG:2264",
    )
    assert plan.source("alignment").crs_source == "user_override"
    assert len(execute(plan, tmp_path).frame("conflicts")) == 2


def test_geographic_analysis_crs_is_refused(tmp_path):
    with pytest.raises(UsageError, match="degrees"):
        execute(_compile(tmp_path, crs="EPSG:4326"), tmp_path)


# -- 4. LandXML and GeoJSON alignments agree -------------------------------


def test_landxml_matches_geojson_alignment(tmp_path):
    from gisc.adapters import geojson as gj_adapter
    from gisc.adapters import landxml as lx_adapter

    lx, _ = lx_adapter.read(ALIGN_XML)
    gj, _ = gj_adapter.read(ALIGN_GJ)
    assert lx.crs.to_string() == "EPSG:2264"
    assert gj.crs.to_string() == "EPSG:4326"

    a = lx.to_crs("EPSG:3857").geometry.iloc[0]
    b = gj.to_crs("EPSG:3857").geometry.iloc[0]
    assert len(a.coords) == len(b.coords)
    for (ax, ay), (bx, by) in zip(a.coords, b.coords):
        assert ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5 < 0.01  # metres
    assert a.hausdorff_distance(b) < 0.01


def test_landxml_and_geojson_alignments_give_the_same_conflicts(tmp_path):
    from_xml = execute(_compile(tmp_path / "x", alignment=ALIGN_XML), tmp_path / "x")
    from_gj = execute(_compile(tmp_path / "g", alignment=ALIGN_GJ), tmp_path / "g")
    assert sorted(from_xml.frame("conflicts")["name"]) == sorted(from_gj.frame("conflicts")["name"])


def test_landxml_reads_a_curve():
    """A circular arc flattens to chords within tolerance of the true radius."""
    from gisc.adapters import landxml as lx

    xml = """<?xml version="1.0"?>
<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2">
  <CoordinateSystem epsgCode="2264"/>
  <Alignments name="A"><Alignment name="C1" staStart="0">
    <CoordGeom>
      <Curve rot="ccw">
        <Start>0.0 100.0</Start><Center>0.0 0.0</Center><End>100.0 0.0</End>
      </Curve>
    </CoordGeom>
  </Alignment></Alignments>
</LandXML>"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "curve.xml"
        p.write_text(xml)
        gdf, prov = lx.read(str(p))
    line = gdf.geometry.iloc[0]
    # Quarter circle of radius 100 ft: arc length pi/2 * 100 = 157.08 ft.
    assert line.length == pytest.approx(157.08, abs=0.05)
    for x, y in line.coords:
        assert (x * x + y * y) ** 0.5 == pytest.approx(100.0, abs=lx.ARC_TOLERANCE)
    assert any("flattened" in n for n in prov["notes"])


def test_landxml_rejects_a_spiral():
    from gisc.adapters import landxml as lx
    from gisc.errors import AdapterError

    xml = """<?xml version="1.0"?>
<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2">
  <CoordinateSystem epsgCode="2264"/>
  <Alignments name="A"><Alignment name="S1" staStart="0">
    <CoordGeom><Spiral spiType="clothoid"/></CoordGeom>
  </Alignment></Alignments>
</LandXML>"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "spiral.xml"
        p.write_text(xml)
        with pytest.raises(AdapterError, match="Spiral"):
            lx.read(str(p))


# -- units -----------------------------------------------------------------


def test_buffer_corrects_for_point_scale_factor():
    """15 ground feet is ~5.63 m in EPSG:3857 at 35.6 N, not 4.572 m."""
    web = units.buffer_distance("EPSG:3857", 15.0, (-9_189_000.0, 4_241_000.0))
    assert web["point_scale_factor"] == pytest.approx(1.2307, abs=1e-3)
    assert web["distance_in_crs_units"] == pytest.approx(5.627, abs=2e-3)
    # The naive answer -- 15 ft straight into metres -- is 23% short.
    assert web["distance_in_crs_units"] > 15 * units.FT_TO_M * 1.2

    # State plane in US survey feet lands ~15, the other side of unity.
    sp = units.buffer_distance("EPSG:2264", 15.0, (905_000.0, 675_000.0))
    assert sp["crs_unit_to_m"] == pytest.approx(0.3048006096)
    assert sp["distance_in_crs_units"] == pytest.approx(14.998, abs=1e-3)

    for conv in (web, sp):
        assert units.to_ft(conv["distance_in_crs_units"], conv["crs"],
                           conv["point_scale_factor"]) == pytest.approx(15.0)


def test_web_mercator_anisotropy_is_reported_not_hidden():
    """EPSG:3857 is not conformal on the ellipsoid. Say so, do not average it away."""
    web = units.scale_factor("EPSG:3857", (-9_189_000.0, 4_241_000.0))
    lo, hi = web["scale_factor_range"]
    assert hi - lo > 0.004  # ~0.45% N-S vs E-W at this latitude
    assert web["anisotropy_pct"] > 0.3

    # A real conformal projection has none of it.
    sp = units.scale_factor("EPSG:2264", (905_000.0, 675_000.0))
    assert sp["anisotropy_pct"] < 0.001


def test_analysis_crs_does_not_change_the_answer(tmp_path):
    """The same 15 ft in Web Mercator and in state plane finds the same pipes."""
    web = execute(_compile(tmp_path / "w", crs="EPSG:3857"), tmp_path / "w")
    sp = execute(_compile(tmp_path / "s", crs="EPSG:2264"), tmp_path / "s")
    assert sorted(web.frame("conflicts")["name"]) == sorted(sp.frame("conflicts")["name"])
    assert (
        web.frame("conflicts").set_index("name").loc["SS-12IN", "offset_ft"]
        == pytest.approx(sp.frame("conflicts").set_index("name").loc["SS-12IN", "offset_ft"],
                         abs=0.02)
    )


# -- stubs fail honestly ---------------------------------------------------


def test_postgis_stub_states_the_sql_it_would_run(tmp_path):
    from gisc.adapters import postgis

    uri = "postgresql://gis@db.example.org:5432/city?layer=util.water&geom=shape"
    parsed = postgis.parse_uri(uri)
    assert parsed["schema"] == "util" and parsed["table"] == "water"
    assert parsed["geom_column"] == "shape"

    plan = _compile(tmp_path, utils=uri)
    assert "stub" in " ".join(plan.notes)
    with pytest.raises(StubError) as exc:
        execute(plan, tmp_path)
    msg = str(exc.value)
    assert 'SELECT *, ST_AsBinary("shape")' in msg
    assert '"util"."water"' in msg
    assert "not implemented" in msg


def test_fema_stub_states_the_request_it_would_make(tmp_path):
    from gisc.adapters import fema

    req = fema.would_request("nfhl:?where=DFIRM_ID%3D%2737021C%27")
    assert req["url"].startswith("https://hazards.fema.gov/")
    with pytest.raises(StubError, match="hazards.fema.gov"):
        fema.read("nfhl:")


def test_unknown_extension_is_refused(tmp_path):
    with pytest.raises(UsageError, match="no adapter"):
        compile_task(
            "corridor.conflicts", alignment=ALIGN_XML, utils="pipes.dwg",
            buffer_ft=15.0, crs="EPSG:3857", out_dir=tmp_path,
        )


def test_unknown_task_is_refused(tmp_path):
    with pytest.raises(UsageError, match="unknown task"):
        compile_task("plans.takeoff", alignment=ALIGN_XML, out_dir=tmp_path)


def test_task_with_nothing_to_look_for_is_refused(tmp_path):
    with pytest.raises(UsageError, match="--utils"):
        compile_task("corridor.conflicts", alignment=ALIGN_XML, out_dir=tmp_path)


# -- the CLI ---------------------------------------------------------------


def test_cli_compile_writes_the_four_artifacts(tmp_path):
    out = tmp_path / "demo"
    proc = subprocess.run(
        [sys.executable, "-m", "gisc.cli", "compile", "corridor.conflicts",
         "--alignment", ALIGN_XML, "--utils", UTILS, "--flood", FLOOD,
         "--buffer-ft", "15", "--crs", "EPSG:3857", "--out", str(out)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    for name in ("plan.json", "conflicts.geojson", "flood.geojson",
                 "summary.md", "provenance.json"):
        assert (out / name).exists(), f"{name} missing"

    plan = json.loads((out / "plan.json").read_text())
    assert plan["task"] == "corridor.conflicts"
    assert plan["buffer_ft"] == 15
    assert [s["id"] for s in plan["sources"]] == ["alignment", "utilities", "flood"]

    gdf = gpd.read_file(out / "conflicts.geojson")
    assert len(gdf) == 2
    assert gdf.crs.to_string() == "EPSG:4326"


def test_cli_ir_prints_a_plan_and_writes_nothing(tmp_path):
    out = tmp_path / "nothing"
    proc = subprocess.run(
        [sys.executable, "-m", "gisc.cli", "ir", "corridor.conflicts",
         "--alignment", ALIGN_XML, "--utils", UTILS, "--out", str(out)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    plan = json.loads(proc.stdout)
    assert plan["task"] == "corridor.conflicts"
    assert [o["op"] for o in plan["ops"]][0] == "read"
    assert not out.exists()


def test_empty_result_still_writes_valid_geojson(tmp_path):
    """A corridor that hits nothing produces an empty FC, not a crash."""
    lonely = tmp_path / "far.geojson"
    gpd.GeoDataFrame(
        {"name": ["ELSEWHERE"]},
        geometry=[LineString([(1_000_000, 800_000), (1_000_100, 800_000)])],
        crs="EPSG:2264",
    ).to_file(lonely, driver="GeoJSON")

    out = tmp_path / "run"
    result = execute(_compile(out, utils=str(lonely), flood=None), out)
    assert len(result.frame("conflicts")) == 0
    fc = json.loads((out / "conflicts.geojson").read_text())
    assert fc["type"] == "FeatureCollection" and fc["features"] == []


# -- reporting only numbers that mean something ----------------------------


def test_polygons_get_an_area_and_no_bogus_perimeter(tmp_path):
    """The perimeter of a clipped flood polygon is not a number to act on."""
    result = execute(_compile(tmp_path), tmp_path)
    flood = result.frame("flood_hits")
    assert "overlap_ac" in flood.columns
    assert "overlap_ft" not in flood.columns
    assert flood["overlap_ac"].iloc[0] == pytest.approx(0.218, abs=0.005)

    conflicts = result.frame("conflicts")
    assert "overlap_ft" in conflicts.columns
    assert "overlap_ac" not in conflicts.columns


def test_station_is_a_point_for_a_crossing_and_a_range_for_an_extent(tmp_path):
    result = execute(_compile(tmp_path), tmp_path)
    c = result.frame("conflicts").set_index("name")

    # A perpendicular crossing has no extent along the alignment.
    assert c.loc["WL-8IN", "sta_label"] == "12+00.00"
    assert c.loc["WL-8IN", "side"] == "CL"  # on the centerline, not unknown

    # A 250 ft parallel run covers 250 ft of station.
    assert c.loc["SS-12IN", "sta_label"] == "19+27.20 - 21+77.20"
    assert c.loc["SS-12IN", "sta_end"] - c.loc["SS-12IN", "sta_begin"] == pytest.approx(
        250.0, abs=0.1
    )

    flood = result.frame("flood_hits")
    assert " - " in flood["sta_label"].iloc[0]


def test_stationing_is_measured_in_the_design_crs(tmp_path):
    """A station is a grid distance in the design system, not a ground distance."""
    result = execute(_compile(tmp_path), tmp_path)
    sample = next(o for o in result.provenance["ops"] if o["op"] == "sample")
    assert sample["result"]["measured_in"] == "EPSG:2264"  # not the EPSG:3857 analysis CRS
    assert sample["result"]["measured_in_source"] == "native"
    assert sample["result"]["sta_unit"] == "US survey foot"


def test_plan_paths_are_portable(tmp_path):
    """The IR is a document, not a Windows path dump."""
    plan = _compile(tmp_path).to_dict()
    for op in plan["ops"]:
        if op["op"] == "write":
            assert "\\" not in op["to"], op["to"]
    assert all("\\" not in v for v in plan["outputs"].values())


def test_station_label_rounds_before_splitting():
    """1199.9999 must print 12+00.00, never 11+100.00."""
    from gisc.exec import _station_label

    assert _station_label(1200.0) == "12+00.00"
    assert _station_label(1199.99999) == "12+00.00"
    assert _station_label(1199.994) == "11+99.99"
    assert _station_label(0.0) == "0+00.00"
    assert _station_label(1234.56) == "12+34.56"
    assert _station_label(-50.0) == "-0+50.00"
    assert _station_label(None) is None


def test_overlap_is_geodesic_so_the_analysis_crs_does_not_move_it(tmp_path):
    """How much pipe is in the corridor is a ground fact, not a projection artefact."""
    web = execute(_compile(tmp_path / "w", crs="EPSG:3857"), tmp_path / "w")
    sp = execute(_compile(tmp_path / "s", crs="EPSG:2264"), tmp_path / "s")

    w = web.frame("conflicts").set_index("name")
    s = sp.frame("conflicts").set_index("name")

    # The 250 ft parallel run measures the same either way, and matches design.
    assert w.loc["SS-12IN", "overlap_ft"] == pytest.approx(250.03, abs=0.05)
    assert w.loc["SS-12IN", "overlap_ft"] == pytest.approx(s.loc["SS-12IN", "overlap_ft"],
                                                           abs=0.05)
    # Stationing is identical, not merely close.
    assert list(w["sta_label"]) == list(s["sta_label"])


# -- the scale factor guard ------------------------------------------------


@pytest.mark.parametrize(
    "crs, xy",
    [
        ("EPSG:3857", (0.0, 3e7)),          # near the pole, k ~ 55
        ("EPSG:3857", (2e7, 2e7)),          # off the edge of the world
        ("EPSG:2264", (1e9, 1e9)),          # NC state plane, but nowhere near NC
    ],
)
def test_implausible_scale_factor_is_refused(crs, xy):
    """A buffer is only meaningful if the data is where the CRS says it is."""
    with pytest.raises(UsageError, match="implausible"):
        units.buffer_distance(crs, 15.0, xy)


def test_geographic_crs_cannot_measure_feet():
    with pytest.raises(UsageError, match="degrees"):
        units.scale_factor("EPSG:4326", (-82.5, 35.6))


# -- summary rendering -----------------------------------------------------


def test_summary_renders_plan_notes_and_alignment_notes(tmp_path):
    """A stub source and a flattened curve both have to reach the reader."""
    from gisc.tasks import corridor

    curved = tmp_path / "curve.xml"
    curved.write_text("""<?xml version="1.0"?>
<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2">
  <CoordinateSystem epsgCode="2264"/>
  <Alignments name="A"><Alignment name="C1" staStart="1000">
    <CoordGeom><Curve rot="ccw">
      <Start>675000.0 905100.0</Start><Center>675000.0 905000.0</Center>
      <End>675100.0 905000.0</End>
    </Curve></CoordGeom>
  </Alignment></Alignments>
</LandXML>""")

    plan = compile_task(
        "corridor.conflicts", alignment=str(curved), utils=UTILS,
        buffer_ft=15.0, crs="EPSG:2264", out_dir=tmp_path / "run",
    )
    result = execute(plan, tmp_path / "run")
    plan.notes = ["a compile-time note"]
    text = corridor.summary(result, tmp_path / "run")

    assert "## Notes" in text and "a compile-time note" in text
    assert "## Alignment notes" in text and "flattened a <Curve>" in text


def test_summary_handles_an_output_with_no_feature_count(tmp_path):
    """plan.json and summary.md are listed by path, with no count to show."""
    from gisc.tasks import corridor

    result = execute(_compile(tmp_path), tmp_path)
    result.provenance["outputs"]["plan.json"] = {"path": str(tmp_path / "plan.json")}
    text = corridor.summary(result, tmp_path)
    assert "- `plan.json`" in text
    assert "- `conflicts.geojson` -- 2 features" in text


def test_summary_table_skips_a_frame_with_nothing_to_show(tmp_path):
    from gisc.tasks import corridor

    bare = gpd.GeoDataFrame({"unrelated": [1]},
                            geometry=[LineString([(0, 0), (1, 1)])], crs="EPSG:2264")
    assert corridor._table(bare, [("name", "name"), ("sta_label", "station")]) == []


def test_summary_singularises_one_feature(tmp_path):
    from gisc.tasks import corridor

    result = execute(_compile(tmp_path), tmp_path)
    text = corridor.summary(result, tmp_path)
    assert "1 feature," in text      # flood.geojson has exactly one
    assert "1 features," not in text
