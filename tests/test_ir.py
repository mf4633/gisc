"""The IR contract and the engine invariants that keep it honest.

The validator is what keeps the op set closed. If a task can smuggle an op
past it, ``plan.json`` stops being a complete statement of what will happen,
which is the only thing gisc really sells.
"""

from __future__ import annotations

import json
import pathlib

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString, Point

from gisc.errors import MissingCRSError, UsageError
from gisc.exec import (
    _side,
    _station_label,
    _station_range,
    claim_out_dir,
    execute,
)
from gisc.ir import OPS, Plan, Source, validate

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"


def line(coords, crs="EPSG:2264", **cols):
    return gpd.GeoDataFrame({**cols} or {"n": [1]}, geometry=[LineString(coords)], crs=crs)


def minimal_plan(**over) -> Plan:
    plan = Plan(task="corridor.conflicts", crs="EPSG:3857", buffer_ft=15.0)
    plan.sources = [Source(id="alignment", kind="geojson", ref="a.geojson")]
    plan.ops = [{"op": "read", "src": "alignment"}]
    for k, v in over.items():
        setattr(plan, k, v)
    return plan


# -- the validator ---------------------------------------------------------


def test_op_set_is_exactly_the_documented_six():
    assert OPS == ("read", "reproject", "buffer", "intersect", "sample", "write")


def test_plan_with_no_sources():
    with pytest.raises(UsageError, match="no sources"):
        validate(Plan(task="t", crs="EPSG:3857", buffer_ft=15.0))


def test_duplicate_source_ids():
    plan = minimal_plan()
    plan.sources.append(Source(id="alignment", kind="gpkg", ref="b.gpkg"))
    with pytest.raises(UsageError, match="duplicate source ids"):
        validate(plan)


@pytest.mark.parametrize(
    "op, needle",
    [
        ({"op": "buffer", "src": "alignment", "out": "c"}, "missing required key 'dist_ft'"),
        ({"op": "reproject", "src": "alignment"}, "missing required key 'to'"),
        ({"op": "intersect", "a": "alignment", "out": "c"}, "missing required key 'b'"),
        ({"op": "sample", "src": "alignment", "out": "c"}, "missing required key 'along'"),
        ({"op": "write", "src": "alignment"}, "missing required key 'to'"),
    ],
)
def test_ops_must_carry_their_required_keys(op, needle):
    plan = minimal_plan()
    plan.ops.append(op)
    with pytest.raises(UsageError, match=needle):
        validate(plan)


@pytest.mark.parametrize("name", ["dissolve", "clip", "union", "buffer_ft", ""])
def test_the_op_set_stays_closed(name):
    plan = minimal_plan()
    plan.ops.append({"op": name, "src": "alignment"})
    with pytest.raises(UsageError, match="unknown op"):
        validate(plan)


def test_an_op_cannot_read_something_that_does_not_exist_yet():
    plan = minimal_plan()
    # intersect before the buffer that produces "corridor"
    plan.ops.append({"op": "intersect", "a": "alignment", "b": "corridor", "out": "x"})
    plan.ops.append({"op": "buffer", "src": "alignment", "dist_ft": 15, "out": "corridor"})
    with pytest.raises(UsageError, match="b='corridor' is not defined yet"):
        validate(plan)


def test_an_out_defines_a_name_for_later_ops():
    plan = minimal_plan()
    plan.ops += [
        {"op": "buffer", "src": "alignment", "dist_ft": 15, "out": "corridor"},
        {"op": "intersect", "a": "alignment", "b": "corridor", "out": "hits"},
        {"op": "write", "src": "hits", "to": "hits.geojson"},
    ]
    validate(plan)  # must not raise


def test_unknown_source_lookup():
    with pytest.raises(UsageError, match="unknown source"):
        minimal_plan().source("nope")


def test_plan_round_trips_through_json():
    plan = minimal_plan(notes=["a note"])
    d = json.loads(plan.to_json())
    assert d["task"] == "corridor.conflicts"
    assert d["notes"] == ["a note"]
    assert d["sources"][0]["id"] == "alignment"


def test_execute_validates_before_touching_data(tmp_path):
    """A bad plan fails on the plan, not halfway through reading files."""
    plan = minimal_plan()
    plan.ops.append({"op": "teleport", "src": "alignment"})
    with pytest.raises(UsageError, match="unknown op"):
        execute(plan, tmp_path)
    assert not any(tmp_path.iterdir())


# -- engine invariants -----------------------------------------------------


def run_ops(tmp_path, env, ops, sources=()):
    """Drive ops against a pre-seeded env.

    execute() always starts from an empty env and reads its way in, so this
    dispatches directly to reach guards that a compiled task cannot produce.
    """
    from gisc import exec as engine

    plan = Plan(task="t", crs="EPSG:3857", buffer_ft=15.0)
    plan.sources = list(sources) or [Source(id="x", kind="geojson", ref="x.geojson")]
    plan.ops = ops
    prov: dict = {"sources": {}, "ops": [], "outputs": {}}
    ctx: dict = {}
    for op in ops:
        engine._DISPATCH[op["op"]](op, plan, env, prov, ctx)
    return env, prov, ctx


def test_intersect_refuses_a_crs_mismatch(tmp_path):
    env = {
        "a": line([(0, 0), (100, 0)], crs="EPSG:2264"),
        "b": line([(0, -10), (0, 10)], crs="EPSG:3857"),
    }
    with pytest.raises(UsageError, match="CRS mismatch"):
        run_ops(tmp_path, env, [{"op": "intersect", "a": "a", "b": "b", "out": "c"}])


def test_ops_refuse_a_frame_that_lost_its_crs(tmp_path):
    env = {"a": line([(0, 0), (100, 0)], crs=None)}
    with pytest.raises(MissingCRSError, match="has no CRS at this point"):
        run_ops(tmp_path, env, [{"op": "buffer", "src": "a", "dist_ft": 15, "out": "c"}])


def test_sample_falls_back_to_the_analysis_crs(tmp_path):
    """Without measure_in='native' the sample happens where the data already is."""
    env = {
        "hits": line([(905_100, 675_008), (905_200, 675_008)], crs="EPSG:2264", name=["p"]),
        "cl": gpd.GeoDataFrame(
            {"sta_start": [1000.0]},
            geometry=[LineString([(905_000, 675_000), (905_500, 675_000)])],
            crs="EPSG:2264",
        ),
    }
    env, prov, _ = run_ops(
        tmp_path, env,
        [{"op": "sample", "src": "hits", "along": "cl", "out": "hits"}],
    )
    out = env["hits"]
    assert out["offset_ft"].iloc[0] == pytest.approx(8.0, abs=0.01)
    assert out["sta_label"].iloc[0] == "11+00.00 - 12+00.00"


def test_sample_leaves_empty_geometry_blank_rather_than_guessing(tmp_path):
    hits = gpd.GeoDataFrame(
        {"name": ["real", "empty"]},
        geometry=[LineString([(905_100, 675_008), (905_200, 675_008)]), None],
        crs="EPSG:2264",
    )
    env = {
        "hits": hits,
        "cl": gpd.GeoDataFrame(
            {"sta_start": [1000.0]},
            geometry=[LineString([(905_000, 675_000), (905_500, 675_000)])],
            crs="EPSG:2264",
        ),
    }
    env, _prov, _ = run_ops(
        tmp_path, env, [{"op": "sample", "src": "hits", "along": "cl", "out": "hits"}]
    )
    out = env["hits"]
    assert pd.notna(out["sta_label"].iloc[0])
    # Blank, not a guessed station. pandas normalises the None to NaN.
    assert pd.isna(out["sta_label"].iloc[1])
    assert pd.isna(out["offset_ft"].iloc[1])
    assert pd.isna(out["sta"].iloc[1])


# -- station formatting ----------------------------------------------------


@pytest.mark.parametrize(
    "value, label",
    [
        (0.0, "0+00.00"),
        (99.99, "0+99.99"),
        (100.0, "1+00.00"),
        (1200.0, "12+00.00"),
        (1199.99999, "12+00.00"),   # the float that used to print 11+100.00
        (1199.994, "11+99.99"),
        (1299.999, "13+00.00"),
        (12345.67, "123+45.67"),
        (-50.0, "-0+50.00"),
        (None, None),
    ],
)
def test_station_labels(value, label):
    assert _station_label(value) == label


def test_no_station_label_can_contain_a_three_digit_remainder():
    """The 11+100.00 class of bug, swept across the range."""
    for i in range(0, 500_000, 997):
        label = _station_label(i / 100.0 + 0.009999)
        assert "+" in label
        assert len(label.split("+")[1].split(".")[0]) == 2, label


def test_station_range_collapses_below_a_foot():
    assert _station_range(1200.0, 1200.4) == "12+00.20"
    assert _station_range(1200.0, 1250.0) == "12+00.00 - 12+50.00"
    assert _station_range(None, 1200.0) is None
    assert _station_range(1200.0, None) is None


def test_side_is_left_right_or_centerline():
    cl = LineString([(0, 0), (100, 0)])  # running east, so up-station is +x
    assert _side(cl, 50.0, Point(50, 0), Point(50, 10)) == "L"
    assert _side(cl, 50.0, Point(50, 0), Point(50, -10)) == "R"
    assert _side(cl, 50.0, Point(50, 0), Point(50, 0)) == "CL"


# -- output folder ownership ----------------------------------------------


def test_claim_creates_a_missing_folder(tmp_path):
    target = tmp_path / "deep" / "run"
    assert claim_out_dir(target) == []
    assert target.is_dir()


def test_claim_accepts_an_existing_empty_folder(tmp_path):
    (tmp_path / "run").mkdir()
    assert claim_out_dir(tmp_path / "run") == []


def test_claim_refuses_a_folder_it_did_not_write(tmp_path):
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "notes.txt").write_text("mine")
    with pytest.raises(UsageError, match="will not delete files it does not own"):
        claim_out_dir(tmp_path / "run")


def test_claim_survives_a_corrupt_provenance_file(tmp_path):
    """A truncated provenance.json still identifies the folder as gisc's."""
    run = tmp_path / "run"
    run.mkdir()
    (run / "provenance.json").write_text("{ truncated")
    (run / "plan.json").write_text("{}")
    (run / "summary.md").write_text("#")
    assert sorted(claim_out_dir(run)) == ["plan.json", "provenance.json", "summary.md"]
    assert not any(run.iterdir())


def test_claim_refuses_when_a_gisc_folder_holds_extras(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "provenance.json").write_text(json.dumps({"outputs": {"conflicts.geojson": {}}}))
    (run / "conflicts.geojson").write_text("{}")
    (run / "hand_edits.geojson").write_text("{}")
    with pytest.raises(UsageError, match="hand_edits.geojson"):
        claim_out_dir(run)
    assert (run / "hand_edits.geojson").exists()


def test_claim_lists_only_the_first_few_strays(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "provenance.json").write_text(json.dumps({"outputs": {}}))
    for i in range(9):
        (run / f"stray{i}.txt").write_text("x")
    with pytest.raises(UsageError, match=r"\.\.\."):
        claim_out_dir(run)
