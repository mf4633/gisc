"""The command line, exercised the way a person actually uses it.

These run in-process through typer's CliRunner so every branch is measured.
``test_corridor.py`` keeps a few subprocess tests as well, because only those
prove the installed ``gisc`` entry point and ``python -m gisc.cli`` work.
"""

from __future__ import annotations

import json
import pathlib

import geopandas as gpd
import pytest
from typer.testing import CliRunner

from gisc import __version__
from gisc.cli import app
from gisc.errors import AdapterError, MissingCRSError, StubError, UsageError

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"
ALIGN_XML = str(FIXTURES / "alignment.xml")
ALIGN_GJ = str(FIXTURES / "alignment.geojson")
UTILS = str(FIXTURES / "utilities.geojson")
UTILS_GPKG = str(FIXTURES / "utilities.gpkg")
UTILS_NOCRS = str(FIXTURES / "utilities_nocrs.gpkg")
FLOOD = str(FIXTURES / "flood.geojson")

runner = CliRunner()


def run(*args: str):
    return runner.invoke(app, list(args))


def compile_args(out, *extra: str) -> list[str]:
    return ["compile", "corridor.conflicts", "-a", ALIGN_XML, "-u", UTILS,
            "-o", str(out), *extra]


# -- basics ----------------------------------------------------------------


def test_version_flag_and_command_agree():
    flag = run("--version")
    cmd = run("version")
    assert flag.exit_code == 0 and cmd.exit_code == 0
    assert flag.output.strip() == cmd.output.strip() == __version__


def test_help_lists_the_commands_and_the_exit_codes():
    result = run("--help")
    assert result.exit_code == 0
    for word in ("compile", "ir", "version", "Exit codes"):
        assert word in result.output


def test_bare_invocation_shows_help_not_a_traceback():
    result = run()
    assert "Usage" in result.output
    assert "Traceback" not in result.output


# -- the happy path --------------------------------------------------------


def test_compile_writes_every_artifact(tmp_path):
    out = tmp_path / "run"
    result = run(*compile_args(out, "-f", FLOOD, "-b", "15", "--crs", "EPSG:3857"))
    assert result.exit_code == 0, result.output

    written = sorted(p.name for p in out.iterdir())
    assert written == ["conflicts.geojson", "flood.geojson", "plan.json",
                       "provenance.json", "summary.md"]

    # The summary is what a person reads; it must carry the headline numbers.
    assert "2 of 3 utilities" in result.output
    assert "1 of 2 flood polygons" in result.output

    prov = json.loads((out / "provenance.json").read_text())
    # Everything in the folder is accounted for in provenance.
    assert set(prov["outputs"]) == set(written)


def test_quiet_prints_only_the_folder(tmp_path):
    out = tmp_path / "run"
    result = run(*compile_args(out, "-q"))
    assert result.exit_code == 0
    assert result.output.strip() == str(out.resolve())


def test_default_out_folder_is_a_timestamped_run_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["compile", "corridor.conflicts", "-a", ALIGN_XML,
                                 "-u", UTILS, "-q"])
    assert result.exit_code == 0, result.output
    runs = list((tmp_path / "out").iterdir())
    assert len(runs) == 1
    assert runs[0].name.endswith(tuple("0123456789abcdef"))
    assert (runs[0] / "provenance.json").exists()


def test_row_input_produces_a_row_layer(tmp_path):
    out = tmp_path / "run"
    result = runner.invoke(app, ["compile", "corridor.conflicts", "-a", ALIGN_XML,
                                 "--row", FLOOD, "-o", str(out), "-q"])
    assert result.exit_code == 0, result.output
    assert (out / "row.geojson").exists()
    assert not (out / "conflicts.geojson").exists()  # no --utils was given


def test_geojson_alignment_works_without_landxml(tmp_path):
    out = tmp_path / "run"
    result = runner.invoke(app, ["compile", "corridor.conflicts", "-a", ALIGN_GJ,
                                 "-u", UTILS, "-o", str(out), "-q"])
    assert result.exit_code == 0, result.output
    assert len(gpd.read_file(out / "conflicts.geojson")) == 2


def test_output_path_with_a_space(tmp_path):
    out = tmp_path / "a folder with spaces" / "run 1"
    assert run(*compile_args(out, "-q")).exit_code == 0
    assert (out / "conflicts.geojson").exists()


def test_state_plane_crs(tmp_path):
    out = tmp_path / "run"
    result = run(*compile_args(out, "--crs", "EPSG:2264"))
    assert result.exit_code == 0, result.output
    assert "EPSG:2264" in result.output
    # The Web Mercator warning is only for Web Mercator.
    assert "not a survey-grade projection" not in result.output


def test_web_mercator_run_warns_about_web_mercator(tmp_path):
    result = run(*compile_args(tmp_path / "run", "--crs", "EPSG:3857"))
    assert "not a survey-grade projection" in result.output


# -- ir --------------------------------------------------------------------


def test_ir_prints_a_plan_and_writes_nothing(tmp_path):
    out = tmp_path / "never"
    result = run("ir", "corridor.conflicts", "-a", ALIGN_XML, "-u", UTILS, "-o", str(out))
    assert result.exit_code == 0
    plan = json.loads(result.output)
    assert plan["task"] == "corridor.conflicts"
    assert not out.exists()


def test_ir_no_probe_does_not_open_the_files(tmp_path):
    result = run("ir", "corridor.conflicts", "-a", "missing.xml", "-u", "gone.geojson",
                 "--no-probe")
    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    assert all(s["native_crs"] is None for s in plan["sources"])


def test_ir_probing_a_missing_file_fails(tmp_path):
    result = run("ir", "corridor.conflicts", "-a", "missing.xml", "-u", UTILS)
    assert result.exit_code == AdapterError.exit_code
    assert "missing.xml" in result.output


# -- exit codes ------------------------------------------------------------


@pytest.mark.parametrize(
    "args, code, needle",
    [
        (["compile", "nope.task", "-a", ALIGN_XML, "-u", UTILS],
         UsageError.exit_code, "unknown task"),
        (["compile", "corridor.conflicts", "-a", ALIGN_XML, "-u", UTILS, "-b", "-5"],
         UsageError.exit_code, "must be positive"),
        (["compile", "corridor.conflicts", "-a", ALIGN_XML],
         UsageError.exit_code, "needs something to look for"),
        (["compile", "corridor.conflicts", "-a", ALIGN_XML, "-u", "pipes.dwg"],
         UsageError.exit_code, "no adapter"),
        (["compile", "corridor.conflicts", "-a", ALIGN_XML, "-u", UTILS,
          "--crs", "EPSG:4326"], UsageError.exit_code, "degrees"),
        (["compile", "corridor.conflicts", "-a", "nope.xml", "-u", UTILS],
         AdapterError.exit_code, "not found"),
        (["compile", "corridor.conflicts", "-a", ALIGN_XML, "-u", UTILS_NOCRS],
         MissingCRSError.exit_code, "utilities_nocrs.gpkg"),
        (["compile", "corridor.conflicts", "-a", ALIGN_XML,
          "-u", "postgresql://gis@db.example.org:5432/city?layer=util.water"],
         StubError.exit_code, "not implemented"),
    ],
)
def test_exit_codes(tmp_path, args, code, needle):
    result = runner.invoke(app, [*args, "-o", str(tmp_path / "run")])
    assert result.exit_code == code, result.output
    assert needle in result.output
    assert "Traceback" not in result.output


def test_stub_says_what_it_would_have_run(tmp_path):
    result = runner.invoke(app, [
        "compile", "corridor.conflicts", "-a", ALIGN_XML,
        "-u", "postgresql://gis@db.example.org:5432/city?layer=util.water&geom=shape",
        "-o", str(tmp_path / "run")])
    assert result.exit_code == StubError.exit_code
    assert 'SELECT *, ST_AsBinary("shape")' in result.output
    assert '"util"."water"' in result.output


# -- the output folder is owned by the run ---------------------------------


def test_second_run_clears_the_first_runs_artifacts(tmp_path):
    """A run folder describes exactly one run, or provenance is a lie."""
    out = tmp_path / "run"
    assert run(*compile_args(out, "-f", FLOOD, "-q")).exit_code == 0
    assert (out / "flood.geojson").exists()

    # Same folder, no --flood this time.
    assert run(*compile_args(out, "-q")).exit_code == 0
    assert not (out / "flood.geojson").exists(), "stale output survived a rerun"

    prov = json.loads((out / "provenance.json").read_text())
    assert "flood.geojson" in prov["cleared_from_previous_run"]
    assert set(prov["outputs"]) == {p.name for p in out.iterdir()}


def test_refuses_a_folder_it_does_not_own(tmp_path):
    out = tmp_path / "someones_work"
    out.mkdir()
    keep = out / "report.docx"
    keep.write_text("months of work")

    result = run(*compile_args(out, "-q"))
    assert result.exit_code == UsageError.exit_code
    assert "will not delete files it does not own" in result.output
    assert keep.read_text() == "months of work"


def test_refuses_a_gisc_folder_holding_unaccounted_files(tmp_path):
    out = tmp_path / "run"
    assert run(*compile_args(out, "-q")).exit_code == 0
    (out / "notes.txt").write_text("mine")

    result = run(*compile_args(out, "-q"))
    assert result.exit_code == UsageError.exit_code
    assert "notes.txt" in result.output
    assert (out / "notes.txt").exists()


def test_reruns_are_byte_identical(tmp_path):
    """Same inputs, same bytes -- so a diff means the data changed."""
    a, b = tmp_path / "a", tmp_path / "b"
    assert run(*compile_args(a, "-f", FLOOD, "-q")).exit_code == 0
    assert run(*compile_args(b, "-f", FLOOD, "-q")).exit_code == 0
    for name in ("conflicts.geojson", "flood.geojson"):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name


# -- layer selection -------------------------------------------------------


@pytest.fixture
def multi_layer_gpkg(tmp_path):
    src = gpd.read_file(UTILS_GPKG)
    path = tmp_path / "multi.gpkg"
    src.to_file(path, layer="water", driver="GPKG")
    src.iloc[:1].to_file(path, layer="sewer", driver="GPKG")
    return str(path)


def test_multi_layer_gpkg_needs_a_named_layer(tmp_path, multi_layer_gpkg):
    result = runner.invoke(app, ["compile", "corridor.conflicts", "-a", ALIGN_XML,
                                 "-u", multi_layer_gpkg, "-o", str(tmp_path / "r"), "-q"])
    assert result.exit_code == AdapterError.exit_code
    assert "water" in result.output and "sewer" in result.output


def test_utils_layer_selects_the_layer(tmp_path, multi_layer_gpkg):
    out = tmp_path / "run"
    result = runner.invoke(app, ["compile", "corridor.conflicts", "-a", ALIGN_XML,
                                 "-u", multi_layer_gpkg, "--utils-layer", "sewer",
                                 "-o", str(out), "-q"])
    assert result.exit_code == 0, result.output
    plan = json.loads((out / "plan.json").read_text())
    assert plan["sources"][1]["layer"] == "sewer"
    # The sewer layer holds only the crossing water line.
    assert len(gpd.read_file(out / "conflicts.geojson")) == 1


def test_wrong_layer_name_lists_the_real_ones(tmp_path, multi_layer_gpkg):
    result = runner.invoke(app, ["compile", "corridor.conflicts", "-a", ALIGN_XML,
                                 "-u", multi_layer_gpkg, "--utils-layer", "gas",
                                 "-o", str(tmp_path / "r"), "-q"])
    assert result.exit_code == AdapterError.exit_code
    assert "no layer 'gas'" in result.output
    assert "water" in result.output


def test_alignment_crs_override_reaches_the_plan(tmp_path):
    out = tmp_path / "run"
    result = runner.invoke(app, ["compile", "corridor.conflicts", "-a", ALIGN_GJ,
                                 "-u", UTILS, "--alignment-crs", "EPSG:4326",
                                 "-o", str(out), "-q"])
    assert result.exit_code == 0, result.output
    plan = json.loads((out / "plan.json").read_text())
    assert plan["sources"][0]["crs_source"] == "user_override"
    read_op = plan["ops"][0]
    assert read_op["op"] == "read" and read_op["src"] == "alignment"
    assert read_op["crs_override"] == "EPSG:4326"
    assert read_op["expect"] == "one_feature"  # a centre line is one feature


def test_alignment_name_picks_one_of_several(tmp_path):
    """A LandXML with two alignments must be disambiguated, not guessed at."""
    two = tmp_path / "two.xml"
    text = pathlib.Path(ALIGN_XML).read_text()
    block = text[text.index("    <Alignment"):text.index("</Alignment>") + len("</Alignment>")]
    two.write_text(text.replace(block, block + "\n" + block.replace('"CL-MAIN"', '"CL-ALT"')))

    common = ["compile", "corridor.conflicts", "-a", str(two), "-u", UTILS, "-q"]
    ambiguous = runner.invoke(app, [*common, "-o", str(tmp_path / "r1")])
    assert ambiguous.exit_code == AdapterError.exit_code
    assert "CL-MAIN" in ambiguous.output and "CL-ALT" in ambiguous.output

    out = tmp_path / "r2"
    named = runner.invoke(app, [*common, "--alignment-name", "CL-ALT", "-o", str(out)])
    assert named.exit_code == 0, named.output
    plan = json.loads((out / "plan.json").read_text())
    assert plan["sources"][0]["layer"] == "CL-ALT"
