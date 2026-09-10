"""The buffer: polygonisation, and a scale factor that will not hold still.

gisc goes to some trouble to make ``--buffer-ft 15`` mean 15 ft of ground.
Two things can quietly give that back:

* **Polygonisation.** A buffer is drawn as a polygon inscribed in the true
  circle, so every arc falls *inside* it. At shapely's default 8 segments per
  quadrant a 15 ft corridor is 14.93 ft at the ends.
* **Drift.** The scale factor is a property of a *point*. Over a long
  alignment it changes, and one buffer distance cannot be right at both ends.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from pyproj import Transformer

from gisc import units
from gisc.compile import compile_task
from gisc.exec import execute
from gisc.tasks import corridor

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"
UTILS = str(FIXTURES / "utilities.geojson")

_TO3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)


# -- polygonisation ---------------------------------------------------------


def test_quad_segs_buys_the_tolerance_it_is_asked_for():
    for dist, tol in [(5.6, 0.0003), (100.0, 0.01), (3.0, 0.001)]:
        q = units.quad_segs(dist, tol)
        import math
        sagitta = dist * (1.0 - math.cos(math.pi / (4.0 * q)))
        assert sagitta <= tol, (dist, tol, q, sagitta)


def test_a_default_buffer_no_longer_cuts_its_ends_off():
    """8 segments/quadrant costs 0.072 ft on a 15 ft buffer. This is the fix."""
    conv = units.buffer_distance("EPSG:3857", 15.0, (-9_180_000.0, 4_250_000.0))
    assert conv["quad_segs"] > 8
    assert conv["chord_error_ft"] < 0.01


@pytest.mark.parametrize("dist, tol", [(0.0, 0.001), (-1.0, 0.001), (5.0, 0.0)])
def test_a_nonsense_request_falls_back_to_the_library_default(dist, tol):
    assert units.quad_segs(dist, tol) == 8


def test_an_unreachably_tight_tolerance_is_capped_not_chased():
    """tol/dist underflows to nothing; gisc caps rather than looping forever."""
    assert units.quad_segs(1e20, 0.001) == 512


# -- drift ------------------------------------------------------------------


def test_a_short_alignment_says_nothing_about_drift():
    """The common case must not grow a warning it does not need."""
    at = _TO3857.transform(-82.5, 35.6)
    ends = [_TO3857.transform(-82.5, 35.6), _TO3857.transform(-82.498, 35.602)]
    conv = units.buffer_distance("EPSG:3857", 15.0, at, extent=ends)
    assert conv["extent_error_ft"] < 0.01
    assert conv["notes"] == []


def test_a_long_alignment_is_told_its_buffer_cannot_be_right_everywhere():
    at = _TO3857.transform(-82.5, 35.7)
    ends = [_TO3857.transform(-82.5, 35.2), _TO3857.transform(-82.5, 36.2)]
    conv = units.buffer_distance("EPSG:3857", 15.0, at, extent=ends)

    lo, hi = conv["scale_factor_over_extent"]
    assert hi - lo > 0.01, "a degree of latitude in 3857 must move the factor"
    # ~0.09 ft on a 15 ft buffer: small, but it is the whole of what the scale
    # factor correction bought, given back at the ends.
    assert conv["extent_error_ft"] > 0.05
    assert any("cannot be exact along all of it" in n for n in conv["notes"])


def test_the_warning_reaches_the_summary_a_person_reads(tmp_path):
    """A note nobody sees is not provenance. It has to land in summary.md."""
    aln = tmp_path / "long.geojson"
    aln.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"name": "CL-LONG"},
            "geometry": {"type": "LineString",
                         "coordinates": [[-82.5, 35.2], [-82.5, 36.2]]},
        }],
    }))
    out = tmp_path / "run"
    plan = compile_task(
        "corridor.conflicts", alignment=str(aln), utils=UTILS,
        buffer_ft=15.0, crs="EPSG:3857", out_dir=out,
    )
    result = execute(plan, out)
    text = corridor.summary(result, out)

    assert "cannot be exact along all of it" in text
    assert result.provenance["buffer"]["extent_error_ft"] > 0.1


def test_the_buffer_line_states_its_own_polygonisation(tmp_path):
    out = tmp_path / "run"
    plan = compile_task(
        "corridor.conflicts", alignment=str(FIXTURES / "alignment.xml"),
        utils=UTILS, buffer_ft=15.0, crs="EPSG:2264", out_dir=out,
    )
    result = execute(plan, out)
    text = corridor.summary(result, out)
    assert "segments/quadrant" in text
    assert "of arc error" in text
