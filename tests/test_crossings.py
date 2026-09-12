"""A second task, and what it proves about the IR.

``gisc/ir.py`` claims the IR is "one common plan that every task compiles down
to". With exactly one task that was untested — the IR could have been shaped
entirely around ``corridor.conflicts`` and nobody would have known.

``alignment.crossings`` is the test. It is deliberately a different shape of
question: *where does this cross the centreline*, not *what is near it*. That
means no buffer at all, and an answer that is a point existing in neither
input until the intersection makes it.

Against the same fixtures the two tasks correctly disagree:

    corridor.conflicts   WL-8IN and SS-12IN  — both within 15 ft
    alignment.crossings  WL-8IN only         — SS-12IN runs parallel, 8 ft off,
                                               and never touches the centreline
"""

from __future__ import annotations

import json
import pathlib

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point

from gisc.compile import compile_task
from gisc.errors import UsageError
from gisc.exec import execute
from gisc.ir import OPS, validate
from gisc.tasks import TASKS, crossings

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"
ALIGN_XML = str(FIXTURES / "alignment.xml")
UTILS = str(FIXTURES / "utilities.geojson")
FLOOD = str(FIXTURES / "flood.geojson")

# The design grid the fixtures are laid out on, from fixtures/make_fixtures.py.
E0, N0 = 905_000.0, 675_000.0


def _compile(out_dir, task="alignment.crossings", **over):
    kwargs = dict(alignment=ALIGN_XML, utils=UTILS, crs="EPSG:2264", out_dir=out_dir)
    kwargs.update(over)
    return compile_task(task, **kwargs)


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    out = tmp_path_factory.mktemp("crossings")
    plan = _compile(out)
    return plan, execute(plan, out)


# -- 1. what the second task proves about the IR ---------------------------


def test_the_engine_knows_more_than_one_task():
    assert set(TASKS) == {"corridor.conflicts", "alignment.crossings"}


def test_a_second_task_needed_no_new_ops(run):
    """The op set is the claim. If a genuinely different question had needed a
    seventh op, "read, reproject, buffer, intersect, sample, write, and nothing
    else" would have been a description of one task, not an IR."""
    plan, _ = run
    assert {o["op"] for o in plan.ops} <= set(OPS)
    validate(plan)


def test_a_task_that_never_buffers_emits_no_buffer_op(run):
    """The structural difference. corridor.conflicts cannot answer without a
    buffer; this one cannot use one."""
    plan, _ = run
    assert "buffer" not in {o["op"] for o in plan.ops}


def test_a_task_that_never_buffers_carries_no_buffer_distance(run):
    """buffer_ft used to be a required field of the IR, so a task with no
    corridor still had to name a corridor width."""
    plan, _ = run
    assert plan.buffer_ft is None
    assert "buffer_ft" not in plan.to_dict()


def test_the_corridor_task_still_declares_its_buffer(tmp_path):
    plan = _compile(tmp_path, task="corridor.conflicts", buffer_ft=15.0)
    assert plan.buffer_ft == 15.0
    assert plan.to_dict()["buffer_ft"] == 15.0


def test_provenance_omits_the_buffer_for_a_task_that_has_none(run):
    _plan, result = run
    assert "buffer_ft" not in result.provenance
    assert "buffer" not in result.provenance


# -- 2. the answer ----------------------------------------------------------


def test_a_crossing_is_a_point_not_the_thing_that_crosses(run):
    """The whole reason the task exists. "Where does it cross" is a point that
    is in neither input until the intersection makes it."""
    _plan, result = run
    hits = result.env["utility_crossings"]
    assert len(hits) == 1
    assert hits.geometry.iloc[0].geom_type == "Point"


def test_the_crossing_lands_where_the_geometry_says(run):
    """Outputs are RFC 7946, so the answer comes back in degrees. Put it back on
    the design grid to check it in the unit the design was drawn in."""
    _plan, result = run
    point = result.env["utility_crossings"].geometry.iloc[0]
    on_grid = gpd.GeoSeries([point], crs="EPSG:4326").to_crs("EPSG:2264").iloc[0]
    # WL-8IN runs north-south through local x=200; the alignment is on y=0 there.
    assert on_grid.distance(Point(E0 + 200.0, N0)) < 0.01


def test_the_crossing_is_stationed_and_on_the_centreline(run):
    _plan, result = run
    row = result.env["utility_crossings"].iloc[0]
    assert row["name"] == "WL-8IN"
    assert row["sta_label"] == "12+00.00"
    assert row["offset_ft"] == 0.0
    assert row["side"] == "CL"


def test_the_two_tasks_disagree_correctly_on_the_same_inputs(tmp_path):
    """A parallel utility 8 ft off the centreline is a conflict and not a
    crossing. Both answers are right; they are answers to different questions."""
    corridor_plan = _compile(tmp_path / "a", task="corridor.conflicts", buffer_ft=15.0)
    corridor = execute(corridor_plan, tmp_path / "a")
    crossing_plan = _compile(tmp_path / "b")
    crossing = execute(crossing_plan, tmp_path / "b")

    assert sorted(corridor.env["conflicts"]["name"]) == ["SS-12IN", "WL-8IN"]
    assert sorted(crossing.env["utility_crossings"]["name"]) == ["WL-8IN"]


def test_stations_agree_between_the_two_tasks(tmp_path):
    """Same centreline, same utility, same station -- or one of them is wrong."""
    corridor = execute(
        _compile(tmp_path / "a", task="corridor.conflicts", buffer_ft=15.0), tmp_path / "a"
    )
    crossing = execute(_compile(tmp_path / "b"), tmp_path / "b")

    a = corridor.env["conflicts"].set_index("name").loc["WL-8IN", "sta_label"]
    b = crossing.env["utility_crossings"].set_index("name").loc["WL-8IN", "sta_label"]
    assert a == b == "12+00.00"


def test_a_polygon_crossing_yields_the_line_through_it(tmp_path):
    """A flood zone the alignment runs through is crossed along a length, not
    at a point, and the geometry says so rather than being reduced."""
    plan = _compile(tmp_path, utils=None, flood=FLOOD)
    result = execute(plan, tmp_path)
    hits = result.env["flood_crossings"]
    assert len(hits) == 1
    assert "Line" in hits.geometry.iloc[0].geom_type
    assert hits["zone"].iloc[0] == "AE"


# -- 3. the op that had to grow --------------------------------------------


def test_intersect_still_keeps_source_geometry_by_default():
    """corridor.conflicts depends on this: the answer to "which pipe" is the
    whole pipe, not the 30 ft of it inside the corridor."""
    from gisc import exec as engine
    from gisc.ir import Plan, Source

    line = gpd.GeoDataFrame(
        {"name": ["P"]}, geometry=[LineString([(0, -50), (0, 50)])], crs="EPSG:2264"
    )
    band = gpd.GeoDataFrame(
        {"n": [1]}, geometry=[LineString([(-10, 0), (10, 0)]).buffer(5)], crs="EPSG:2264"
    )
    env = {"a": line, "b": band}
    plan = Plan(task="t", crs="EPSG:2264")
    plan.sources = [Source(id="a", kind="geojson", ref="x")]
    engine._DISPATCH["intersect"](
        {"op": "intersect", "a": "a", "b": "b", "out": "o"},
        plan, env, {"sources": {}, "ops": [], "outputs": {}}, {},
    )
    assert env["o"].geometry.iloc[0].length == pytest.approx(100.0)


def test_an_unknown_geometry_mode_is_refused_not_guessed():
    from gisc import exec as engine
    from gisc.ir import Plan, Source

    g = gpd.GeoDataFrame({"n": [1]}, geometry=[Point(0, 0)], crs="EPSG:2264")
    plan = Plan(task="t", crs="EPSG:2264")
    plan.sources = [Source(id="a", kind="geojson", ref="x")]
    with pytest.raises(UsageError, match="is not something gisc knows"):
        engine._DISPATCH["intersect"](
            {"op": "intersect", "a": "a", "b": "b", "out": "o", "geometry": "clip"},
            plan, {"a": g, "b": g}, {"sources": {}, "ops": [], "outputs": {}}, {},
        )


def test_the_geometry_mode_is_on_the_record(run):
    _plan, result = run
    op = next(o for o in result.provenance["ops"] if o["op"] == "intersect")
    assert op["result"]["geometry"] == "intersection only"
    assert op["result"]["geometry_types_out"] == ["Point"]


# -- 4. the usual refusals --------------------------------------------------


def test_crossings_needs_something_to_cross(tmp_path):
    with pytest.raises(UsageError, match="needs something to cross"):
        compile_task(
            "alignment.crossings", alignment=ALIGN_XML, crs="EPSG:2264", out_dir=tmp_path
        )


def test_an_unknown_task_names_both_of_them(tmp_path):
    with pytest.raises(UsageError, match="alignment.crossings, corridor.conflicts"):
        compile_task("corridor.nonsense", alignment=ALIGN_XML, out_dir=tmp_path)


def test_the_plan_compiles_without_opening_anything(tmp_path):
    plan = _compile(tmp_path, probe=False)
    assert [s.native_crs for s in plan.sources] == [None, None]
    validate(plan)


def test_it_writes_what_its_plan_promised(tmp_path):
    plan = _compile(tmp_path)
    result = execute(plan, tmp_path)
    for name, dest in plan.outputs.items():
        written = pathlib.Path(dest)
        assert written.is_file(), name
        assert json.loads(written.read_text())["type"] == "FeatureCollection"
        assert written.name in result.provenance["outputs"]


# -- 5. the parts a person actually reads -----------------------------------


def test_the_summary_reports_each_crossing_with_its_station(tmp_path):
    plan = _compile(tmp_path, flood=FLOOD)
    result = execute(plan, tmp_path)
    text = crossings.summary(result, tmp_path)

    assert "# alignment.crossings" in text
    assert "utility_crossings.geojson" in text
    assert "flood_crossings.geojson" in text
    assert "| WL-8IN | 12+00.00 | Point |" in text
    assert "| AE |" in text                       # a zone, not a name
    assert "analysis CRS: **EPSG:2264**" in text


def test_the_summary_says_so_when_nothing_crosses(tmp_path):
    """SS-12IN and GAS-4IN run parallel; neither touches the centreline."""
    parallel = tmp_path / "parallel.geojson"
    gpd.GeoDataFrame(
        {"name": ["SS-12IN"]},
        geometry=[LineString([(E0 + 900, N0 + 158), (E0 + 1150, N0 + 158)])],
        crs="EPSG:2264",
    ).to_file(parallel, driver="GeoJSON")

    plan = _compile(tmp_path / "run", utils=str(parallel))
    result = execute(plan, tmp_path / "run")
    text = crossings.summary(result, tmp_path / "run")
    assert "- **0** in `utility_crossings.geojson`" in text


def test_an_alignment_crs_override_reaches_the_read_op(tmp_path):
    """A GeoJSON centreline declares WGS 84 by default. Saying otherwise has to
    survive into the plan, or the override is decoration."""
    plan = _compile(
        tmp_path, alignment=str(FIXTURES / "alignment.geojson"), alignment_crs="EPSG:4326"
    )
    read = next(o for o in plan.ops if o["op"] == "read" and o["src"] == "alignment")
    assert read["crs_override"] == "EPSG:4326"


def test_a_stubbed_source_is_declared_before_anything_runs(tmp_path):
    """The plan says the run will fail, rather than finding out at execute time."""
    plan = _compile(
        tmp_path, utils="postgresql://gis@db.example.org:5432/city?layer=util.water"
    )
    assert any("is a stub" in n for n in plan.notes)


def test_a_source_with_no_crs_is_declared_before_anything_runs(tmp_path):
    plan = _compile(tmp_path, utils=str(FIXTURES / "utilities_nocrs.gpkg"))
    assert any("declares no CRS" in n for n in plan.notes)
