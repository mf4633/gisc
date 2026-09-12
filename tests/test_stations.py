"""Station equations, and the two bugs that could only be seen in provenance.

A station equation is the quietest way a LandXML file can make gisc wrong. The
geometry parses, the CRS resolves, the offsets are right, and every station
past the equation is off by the size of the equation with nothing on the
record to say so. These tests exist because that failure has no symptom.

``fixtures/alignment_equations.xml`` is CL-MAIN re-stationed twice:

    region 1   raw 1000 -> 1400    displayed 10+00 -> 14+00
    equation   14+00 back = 20+00 ahead          a 600 ft gap
    region 2   raw 1400 -> 1800    displayed 20+00 -> 24+00
    equation   24+00 back = 21+00 ahead          a 300 ft overlap
    region 3   raw 1800 ->         displayed 21+00 ->
"""

from __future__ import annotations

import json
import pathlib

import pytest
from typer.testing import CliRunner

from gisc.adapters import landxml
from gisc.cli import app
from gisc.compile import compile_task
from gisc.errors import AdapterError
from gisc.exec import execute
from gisc.stations import Equation, Stationing

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"
EQUATED = str(FIXTURES / "alignment_equations.xml")
PLAIN = str(FIXTURES / "alignment.xml")
UTILS = str(FIXTURES / "utilities.geojson")
FLOOD = str(FIXTURES / "flood.geojson")

runner = CliRunner()


# -- 1. the mapping itself --------------------------------------------------


def test_with_no_equations_stationing_is_the_identity():
    st = Stationing(1000.0)
    assert not st.equated
    assert st.monotonic
    assert st.raw(200.0) == 1200.0
    assert st.display(1200.0) == 1200.0
    assert st.region_of(1200.0).index == 1
    assert st.notes == []


def test_a_gap_shifts_everything_past_it():
    st = Stationing(1000.0, [Equation(internal=1400.0, back=1400.0, ahead=2000.0)])
    assert st.display(1399.0) == 1399.0
    assert st.display(1400.0) == 2000.0  # the equation point takes the ahead station
    assert st.display(2000.0) == 2600.0
    assert st.monotonic
    assert any("does not exist" in n for n in st.notes)


def test_an_overlap_makes_one_station_name_two_points():
    st = Stationing(1000.0, [Equation(internal=1400.0, back=1400.0, ahead=1100.0)])
    assert st.display(1399.0) == 1399.0
    assert st.display(1400.0) == 1100.0
    assert not st.monotonic
    assert any("occurs twice" in n for n in st.notes)


def test_regions_are_numbered_the_way_a_drawing_numbers_them():
    st = Stationing(1000.0, [
        Equation(internal=1400.0, back=1400.0, ahead=2000.0),
        Equation(internal=1800.0, back=2400.0, ahead=2100.0),
    ])
    assert [r.index for r in st.regions] == [1, 2, 3]
    assert [r.offset for r in st.regions] == [0.0, 600.0, 300.0]
    assert st.region_of(1200.0).index == 1
    assert st.region_of(1500.0).index == 2
    assert st.region_of(1900.0).index == 3
    assert not st.monotonic


def test_a_staback_that_contradicts_the_geometry_is_reported_not_absorbed():
    st = Stationing(1000.0, [Equation(internal=1400.0, back=1450.0, ahead=2000.0)])
    assert any("discrepancy" in n for n in st.notes)
    # staInternal is the one tied to the geometry, so it wins.
    assert st.display(1400.0) == 2000.0


def test_equations_out_of_order_are_sorted_and_the_reordering_is_declared():
    """Station order is the truth, but staInternal may have been derived from
    file order, so a reader has to be told the two disagreed."""
    st = Stationing(1000.0, [
        Equation(internal=1800.0, back=1800.0, ahead=2000.0),
        Equation(internal=1400.0, back=1400.0, ahead=1600.0),
    ])
    assert [e.internal for e in st.equations] == [1400.0, 1800.0]
    assert any("not in station order" in n for n in st.notes)


def test_an_equation_before_the_start_of_the_alignment_is_refused():
    with pytest.raises(AdapterError, match="before the start of the region"):
        Stationing(1000.0, [Equation(internal=900.0, back=900.0, ahead=1500.0)])


def test_equations_survive_the_trip_through_a_geodataframe_column():
    st = Stationing(1000.0, [Equation(internal=1400.0, back=1400.0, ahead=2000.0)])
    back = Stationing.from_json(1000.0, st.to_json())
    assert back.display(2000.0) == 2600.0


@pytest.mark.parametrize("blob", [None, "", "   ", float("nan"), []])
def test_an_absent_equation_column_means_no_equations(blob):
    assert not Stationing.from_json(1000.0, blob).equated


# -- 2. reading them out of LandXML ----------------------------------------


@pytest.fixture(scope="module")
def equated():
    return landxml.read(EQUATED)


def test_the_adapter_reads_both_equations(equated):
    _gdf, prov = equated
    st = prov["stationing"]
    assert len(st["equations"]) == 2
    assert [r["offset"] for r in st["regions"]] == [0.0, 600.0, 300.0]
    assert st["monotonic"] is False


def test_the_alignment_carries_its_equations_on_the_frame(equated):
    gdf, _prov = equated
    assert Stationing.COLUMN in gdf.columns
    assert len(json.loads(gdf[Stationing.COLUMN].iloc[0])) == 2


def test_a_plain_alignment_says_it_has_none():
    _gdf, prov = landxml.read(PLAIN)
    assert prov["stationing"]["equations"] == []
    assert prov["stationing"]["monotonic"] is True


def test_the_gap_and_the_overlap_are_both_called_out(equated):
    _gdf, prov = equated
    notes = " ".join(prov["notes"])
    assert "is a gap" in notes
    assert "is an overlap" in notes
    assert "600 ft of stationing does not exist" in notes


def test_an_equation_that_says_nothing_about_where_it_is_is_refused(tmp_path):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2" version="1.2">
  <CoordinateSystem epsgCode="2264"/>
  <Alignments name="R">
    <Alignment name="A" staStart="1000.0000">
      <CoordGeom>
        <Line>
          <Start>675000.0000 905000.0000</Start>
          <End>675000.0000 905400.0000</End>
        </Line>
      </CoordGeom>
      <StaEquation staAhead="2000.0000"/>
    </Alignment>
  </Alignments>
</LandXML>
"""
    path = tmp_path / "eq.xml"
    path.write_text(xml)
    with pytest.raises(AdapterError, match="neither staInternal nor staBack"):
        landxml.read(str(path))


def test_an_equation_with_only_staback_has_its_raw_station_derived(tmp_path):
    """Some exporters omit staInternal. The raw station follows from the rest."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2" version="1.2">
  <CoordinateSystem epsgCode="2264"/>
  <Alignments name="A" staStart="1000.0000">
    <Alignment name="A" staStart="1000.0000">
      <CoordGeom>
        <Line>
          <Start>675000.0000 905000.0000</Start>
          <End>675000.0000 905400.0000</End>
        </Line>
      </CoordGeom>
      <StaEquation staBack="1200.0000" staAhead="1800.0000"/>
    </Alignment>
  </Alignments>
</LandXML>
"""
    path = tmp_path / "eq.xml"
    path.write_text(xml)
    _gdf, prov = landxml.read(str(path))
    eq = prov["stationing"]["equations"][0]
    assert eq["internal"] == 1200.0
    assert prov["stationing"]["regions"][1]["offset"] == 600.0


# -- 3. end to end: the station a contractor would read ---------------------


@pytest.fixture(scope="module")
def equated_run(tmp_path_factory):
    out = tmp_path_factory.mktemp("equated")
    plan = compile_task(
        "corridor.conflicts", alignment=EQUATED, utils=UTILS, flood=FLOOD,
        buffer_ft=15.0, crs="EPSG:2264", out_dir=out,
    )
    return execute(plan, out)


def test_stations_past_an_equation_are_equated(equated_run):
    """Without the equation SS-12IN reads 19+27.20. Civil 3D says 22+27.20."""
    c = equated_run.env["conflicts"].set_index("name")
    assert c.loc["WL-8IN", "sta_label"] == "12+00.00"          # region 1, unshifted
    assert c.loc["SS-12IN", "sta_label"] == "22+27.20 - 24+77.20"
    assert c.loc["SS-12IN", "sta_raw"] == 1927.2               # the geometry behind it


def test_the_region_travels_with_the_station(equated_run):
    c = equated_run.env["conflicts"].set_index("name")
    assert c.loc["WL-8IN", "sta_region"] == 1
    assert c.loc["SS-12IN", "sta_region"] == 3


def test_a_feature_straddling_an_equation_names_both_regions(equated_run):
    """13+50 to 23+04 is not a 950 ft polygon, and the label must not imply it."""
    label = equated_run.env["flood_hits"]["sta_label"].iloc[0]
    assert label == "13+50.00 (R1) - 23+04.31 (R2)"


def test_an_unequated_alignment_keeps_a_clean_table(tmp_path):
    """The extra columns are the price of equations; nobody else should pay it."""
    plan = compile_task(
        "corridor.conflicts", alignment=PLAIN, utils=UTILS,
        buffer_ft=15.0, crs="EPSG:2264", out_dir=tmp_path,
    )
    result = execute(plan, tmp_path)
    assert "sta_region" not in result.env["conflicts"].columns
    assert "sta_raw" not in result.env["conflicts"].columns


def test_the_stationing_is_on_the_record(equated_run):
    sample = next(o for o in equated_run.provenance["ops"] if o["op"] == "sample")
    st = sample["result"]["stationing"]
    assert len(st["regions"]) == 3
    assert st["monotonic"] is False
    assert sample["result"]["offset_unit"] == "international foot"


# -- 4. two regressions ----------------------------------------------------


def test_the_sample_op_records_which_centreline_it_measured_against(equated_run):
    """This read as a list of vertex distances: leaked loop state, in the one
    file whose whole job is to be trustworthy."""
    for op in equated_run.provenance["ops"]:
        if op["op"] == "sample":
            assert op["result"]["along"] == "alignment"


def test_a_failed_compile_does_not_delete_the_previous_run(tmp_path):
    """The output folder is claimed *after* compiling, not before. Otherwise a
    typo'd input path wipes the answers you already had."""
    out = tmp_path / "run"
    first = runner.invoke(app, ["compile", "corridor.conflicts", "-a", PLAIN,
                                "-u", UTILS, "-o", str(out)])
    assert first.exit_code == 0
    before = (out / "conflicts.geojson").read_bytes()
    prov_before = (out / "provenance.json").read_bytes()

    second = runner.invoke(app, ["compile", "corridor.conflicts",
                                 "-a", str(tmp_path / "does-not-exist.xml"),
                                 "-u", UTILS, "-o", str(out)])
    assert second.exit_code == 4  # adapter failed

    assert (out / "conflicts.geojson").read_bytes() == before
    assert (out / "provenance.json").read_bytes() == prov_before


def test_an_equation_that_does_not_say_what_the_stationing_becomes_is_refused(tmp_path):
    path = tmp_path / "eq.xml"
    path.write_text(_ALIGNMENT_WITH('<StaEquation staInternal="1400.0000"/>'))
    with pytest.raises(AdapterError, match="no staAhead"):
        landxml.read(str(path))


def test_an_equation_with_only_stainternal_has_its_staback_derived(tmp_path):
    path = tmp_path / "eq.xml"
    path.write_text(
        _ALIGNMENT_WITH('<StaEquation staInternal="1200.0000" staAhead="1800.0000"/>')
    )
    _gdf, prov = landxml.read(str(path))
    eq = prov["stationing"]["equations"][0]
    assert eq["back"] == 1200.0
    assert eq["internal"] == 1200.0


def test_a_point_before_the_start_of_the_alignment_falls_in_the_first_region():
    """nearest_points can land behind an alignment's start. It is region 1."""
    st = Stationing(1000.0, [Equation(internal=1400.0, back=1400.0, ahead=2000.0)])
    assert st.region_of(500.0).index == 1
    assert st.display(500.0) == 500.0


def test_an_unreadable_equation_column_is_refused_not_ignored():
    with pytest.raises(AdapterError, match="unreadable station equations"):
        Stationing.from_json(1000.0, "{not json")


def _ALIGNMENT_WITH(equation: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2" version="1.2">
  <CoordinateSystem epsgCode="2264"/>
  <Alignments name="R">
    <Alignment name="A" staStart="1000.0000">
      <CoordGeom>
        <Line>
          <Start>675000.0000 905000.0000</Start>
          <End>675000.0000 905400.0000</End>
        </Line>
      </CoordGeom>
      {equation}
    </Alignment>
  </Alignments>
</LandXML>
"""
