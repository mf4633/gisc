"""Clothoid spirals: the geometry, and what gisc refuses to guess at.

The reference here is the Fresnel integral by power series, computed in this
file. gisc reads a spiral by numerically integrating the heading; a series
expansion is a genuinely different method, so agreement between them is
evidence rather than a restatement.
"""

from __future__ import annotations

import json
import math
import pathlib

import pytest
from shapely.geometry import Point

from gisc.adapters import landxml
from gisc.adapters.landxml import _clothoid, _radius
from gisc.errors import AdapterError

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"
SPIRAL_XML = FIXTURES / "alignment_spiral.xml"
TRUTH = json.loads((FIXTURES / "alignment_spiral_truth.json").read_text())


def fresnel(x: float, terms: int = 40) -> tuple[float, float]:
    """C(x), S(x) by power series -- the independent reference."""
    c = s = 0.0
    for n in range(terms):
        c += (-1) ** n * (math.pi / 2) ** (2 * n) * x ** (4 * n + 1) / (
            math.factorial(2 * n) * (4 * n + 1)
        )
        s += (-1) ** n * (math.pi / 2) ** (2 * n + 1) * x ** (4 * n + 3) / (
            math.factorial(2 * n + 1) * (4 * n + 3)
        )
    return c, s


def clothoid_point(radius: float, length: float, s: float) -> tuple[float, float]:
    """Exact point at distance ``s`` along an INF -> ``radius`` clothoid."""
    k = math.sqrt(math.pi * radius * length)
    c, sn = fresnel(s / k)
    return k * c, k * sn


# -- 1. the integrator ------------------------------------------------------


@pytest.mark.parametrize("radius, length", [(300.0, 150.0), (1000.0, 300.0), (150.0, 60.0)])
def test_clothoid_matches_the_fresnel_series(radius, length):
    pts, heading = _clothoid((0.0, 0.0), 0.0, 0.0, 1.0 / radius, length, 1.0, 64)
    assert math.dist(pts[-1], clothoid_point(radius, length, length)) < 1e-9
    # The heading is closed-form, not integrated, so it is exact.
    assert heading == pytest.approx(length / (2.0 * radius), abs=1e-15)


def test_clothoid_is_accurate_at_the_coarsest_chord_count_it_will_ever_use():
    """Even 8 chords is quadrature-exact; the error that remains is chording."""
    pts, _ = _clothoid((0.0, 0.0), 0.0, 0.0, 1.0 / 300.0, 150.0, 1.0, 8)
    assert math.dist(pts[-1], clothoid_point(300.0, 150.0, 150.0)) < 1e-6


def test_a_right_hand_spiral_mirrors_a_left_hand_one():
    left, _ = _clothoid((0.0, 0.0), 0.0, 0.0, 1.0 / 300.0, 150.0, 1.0, 32)
    right, _ = _clothoid((0.0, 0.0), 0.0, 0.0, 1.0 / 300.0, 150.0, -1.0, 32)
    for (lx, ly), (rx, ry) in zip(left, right):
        assert lx == pytest.approx(rx, abs=1e-12)
        assert ly == pytest.approx(-ry, abs=1e-12)


# -- 2. a spiral-curve-spiral alignment, end to end -------------------------


@pytest.fixture(scope="module")
def spiral_alignment():
    return landxml.read(str(SPIRAL_XML))


def test_spiral_curve_spiral_parses(spiral_alignment):
    gdf, prov = spiral_alignment
    assert len(gdf) == 1
    assert prov["native_crs"] == "EPSG:2264"
    assert prov["sta_start"] == TRUTH["sta_start"]


def test_length_comes_in_short_never_long(spiral_alignment):
    """Chords cut corners. A length that came in long would mean a real error."""
    _gdf, prov = spiral_alignment
    error = prov["length_computed"] - TRUTH["length"]
    assert -0.02 < error < 0.0, f"length error {error:+.4f} ft"


def test_every_design_point_lies_on_the_flattened_centreline(spiral_alignment):
    """Element endpoints are vertices, so they are exact. Mid-arc is not: it is
    the point a chord cuts furthest from, and it may sit a whole tolerance out."""
    gdf, _prov = spiral_alignment
    line = gdf.geometry.iloc[0]
    endpoints = ("ts", "begin_spiral", "sc", "cs", "st", "end")
    for name in endpoints + ("mid",):  # "centre" is the arc centre, not on the line
        x, y = TRUTH["points"][name]
        limit = 0.001 if name in endpoints else landxml.ARC_TOLERANCE
        assert line.distance(Point(x, y)) <= limit, name


def test_the_flattened_spiral_stays_inside_the_advertised_tolerance(spiral_alignment):
    """Walk the true clothoid and check gisc's polyline never strays past it."""
    gdf, _prov = spiral_alignment
    line = gdf.geometry.iloc[0]
    ts_x, ts_y = TRUTH["points"]["begin_spiral"]
    worst = 0.0
    for i in range(1, 501):
        s = TRUTH["spiral_length"] * i / 500.0
        dx, dy = clothoid_point(TRUTH["radius"], TRUTH["spiral_length"], s)
        worst = max(worst, line.distance(Point(ts_x + dx, ts_y + dy)))
    assert worst <= landxml.ARC_TOLERANCE, f"strayed {worst:.6f} ft"


def test_the_chord_count_follows_the_tight_end_not_the_average(spiral_alignment):
    """The bug this pins: averaging curvature under-chords a spiral by ~2x."""
    _gdf, prov = spiral_alignment
    flattened = [n for n in prov["notes"] if "flattened a <Spiral>" in n]
    assert len(flattened) == 2
    for note in flattened:
        chords = int(note.split(" to ")[1].split(" chords")[0])
        # deflection/step would give 16; max-curvature/step gives 31.
        assert chords >= 30, note


# -- 3. what it refuses -----------------------------------------------------


def _one_spiral(tmp_path, attrs, start="675000.0000 905000.0000",
                pi="675000.0000 905100.3294", end="675012.4443 905149.0652"):
    """One spiral running east from (905000, 675000), turning left to R=300.

    LandXML ordinates are northing first, so a point (x, y) is written "y x".
    """
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2" version="1.2">
  <CoordinateSystem epsgCode="2264"/>
  <Alignments name="R">
    <Alignment name="A" staStart="0.0000">
      <CoordGeom>
        <Spiral {attrs}>
          <Start>{start}</Start>
          <PI>{pi}</PI>
          <End>{end}</End>
        </Spiral>
      </CoordGeom>
    </Alignment>
  </Alignments>
</LandXML>
"""
    path = tmp_path / "spiral.xml"
    path.write_text(xml)
    return str(path)


def test_a_non_clothoid_spiral_is_refused_by_name(tmp_path):
    ref = _one_spiral(tmp_path, 'length="150" radiusStart="INF" radiusEnd="300" '
                                'rot="ccw" spiType="bloss"')
    with pytest.raises(AdapterError, match="not a clothoid"):
        landxml.read(ref)


def test_a_spiral_that_does_not_close_is_refused(tmp_path):
    """The declared End is 5 ft from where the declared geometry actually goes."""
    ref = _one_spiral(
        tmp_path,
        'length="150" radiusStart="INF" radiusEnd="300" rot="ccw" spiType="clothoid"',
        end="675012.4443 905154.0652",
    )
    with pytest.raises(AdapterError, match="does not close"):
        landxml.read(ref)


def test_a_backwards_rotation_is_named_as_the_likely_cause(tmp_path):
    """rot='cw' on a curve that plainly goes left. Say so, do not just fail."""
    ref = _one_spiral(
        tmp_path,
        'length="150" radiusStart="INF" radiusEnd="300" rot="cw" spiType="clothoid"',
    )
    with pytest.raises(AdapterError, match="wrong way round"):
        landxml.read(ref)


def test_a_spiral_needs_a_rotation(tmp_path):
    ref = _one_spiral(tmp_path, 'length="150" radiusStart="INF" radiusEnd="300"')
    with pytest.raises(AdapterError, match="rot="):
        landxml.read(ref)


def test_a_spiral_needs_a_positive_length(tmp_path):
    ref = _one_spiral(tmp_path, 'length="0" radiusStart="INF" radiusEnd="300" rot="ccw"')
    with pytest.raises(AdapterError, match="positive length"):
        landxml.read(ref)


def test_infinite_at_both_ends_is_a_line_not_a_spiral(tmp_path):
    ref = _one_spiral(tmp_path, 'length="150" radiusStart="INF" radiusEnd="INF" rot="ccw"')
    with pytest.raises(AdapterError, match="line, not a spiral"):
        landxml.read(ref)


def test_a_spiral_with_no_pi_and_nothing_before_it_cannot_be_oriented(tmp_path):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2" version="1.2">
  <CoordinateSystem epsgCode="2264"/>
  <Alignments name="R">
    <Alignment name="A" staStart="0.0000">
      <CoordGeom>
        <Spiral length="150" radiusStart="INF" radiusEnd="300" rot="ccw">
          <Start>905000.0000 675000.0000</Start>
          <End>905149.0652 675012.4443</End>
        </Spiral>
      </CoordGeom>
    </Alignment>
  </Alignments>
</LandXML>
"""
    path = tmp_path / "nopi.xml"
    path.write_text(xml)
    with pytest.raises(AdapterError, match="cannot orient"):
        landxml.read(str(path))


def test_a_preceding_tangent_can_orient_a_spiral_with_no_pi(tmp_path):
    """No <PI> is survivable: the element before it fixes the tangent."""
    x, y = clothoid_point(300.0, 150.0, 150.0)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2" version="1.2">
  <CoordinateSystem epsgCode="2264"/>
  <Alignments name="R">
    <Alignment name="A" staStart="0.0000">
      <CoordGeom>
        <Line>
          <Start>675000.0000 904900.0000</Start>
          <End>675000.0000 905000.0000</End>
        </Line>
        <Spiral length="150" radiusStart="INF" radiusEnd="300" rot="ccw">
          <Start>675000.0000 905000.0000</Start>
          <End>{675000.0 + y:.4f} {905000.0 + x:.4f}</End>
        </Spiral>
      </CoordGeom>
    </Alignment>
  </Alignments>
</LandXML>
"""
    path = tmp_path / "prev.xml"
    path.write_text(xml)
    _gdf, prov = landxml.read(str(path))
    assert any("oriented from its previous element" in n for n in prov["notes"])


# -- 4. radii ---------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "INF", "INFINITE", "  inf  "])
def test_a_tangent_end_reads_as_an_infinite_radius(value):
    r, notes = _radius(value, "f.xml", "radiusStart")
    assert math.isinf(r)
    assert notes == []


def test_a_zero_radius_is_read_as_infinite_and_said_out_loud():
    r, notes = _radius("0", "f.xml", "radiusEnd")
    assert math.isinf(r)
    assert notes and "infinite radius" in notes[0]


def test_a_radius_that_is_not_a_number_is_refused():
    with pytest.raises(AdapterError, match="neither a number nor INF"):
        _radius("wide", "f.xml", "radiusStart")


def test_a_negative_radius_is_read_by_magnitude():
    """rot carries the direction; a signed radius must not fight it."""
    r, _ = _radius("-300", "f.xml", "radiusStart")
    assert r == 300.0


# -- 5. the notes: things gisc did that a reader has to know about ----------


def test_a_small_closure_error_is_snapped_and_recorded(tmp_path):
    """Under the refusal threshold gisc snaps to <End>, but says that it did."""
    x, y = clothoid_point(300.0, 150.0, 150.0)
    ref = _one_spiral(
        tmp_path,
        'length="150" radiusStart="INF" radiusEnd="300" rot="ccw" spiType="clothoid"',
        end=f"{675000.0 + y:.4f} {905000.0 + x + 0.01:.4f}",
    )
    _gdf, prov = landxml.read(ref)
    assert any("snapped the last point to <End>" in n for n in prov["notes"])


def test_a_theta_written_in_radians_is_recognised_not_believed(tmp_path):
    ref = _one_spiral(
        tmp_path,
        'length="150" radiusStart="INF" radiusEnd="300" rot="ccw" '
        'spiType="clothoid" theta="0.25"',
    )
    _gdf, prov = landxml.read(ref)
    assert any("is in radians" in n for n in prov["notes"])


def test_a_theta_that_matches_nothing_is_reported_and_ignored(tmp_path):
    ref = _one_spiral(
        tmp_path,
        'length="150" radiusStart="INF" radiusEnd="300" rot="ccw" '
        'spiType="clothoid" theta="20"',
    )
    _gdf, prov = landxml.read(ref)
    assert any("disagrees with the" in n for n in prov["notes"])


def test_a_spiral_that_does_not_meet_the_element_before_it_tangentially(tmp_path):
    """Legal, but a kink in a centreline is worth one line of provenance."""
    off = math.radians(0.5)
    x, y = clothoid_point(300.0, 150.0, 150.0)
    ex = 905000.0 + x * math.cos(off) - y * math.sin(off)
    ey = 675000.0 + x * math.sin(off) + y * math.cos(off)
    px = 905000.0 + 100.3294 * math.cos(off)
    py = 675000.0 + 100.3294 * math.sin(off)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2" version="1.2">
  <CoordinateSystem epsgCode="2264"/>
  <Alignments name="R">
    <Alignment name="A" staStart="0.0000">
      <CoordGeom>
        <Line>
          <Start>675000.0000 904900.0000</Start>
          <End>675000.0000 905000.0000</End>
        </Line>
        <Spiral length="150" radiusStart="INF" radiusEnd="300" rot="ccw"
                spiType="clothoid">
          <Start>675000.0000 905000.0000</Start>
          <PI>{py:.4f} {px:.4f}</PI>
          <End>{ey:.4f} {ex:.4f}</End>
        </Spiral>
      </CoordGeom>
    </Alignment>
  </Alignments>
</LandXML>
"""
    path = tmp_path / "kink.xml"
    path.write_text(xml)
    _gdf, prov = landxml.read(str(path))
    assert any("off the tangent of the element before it" in n for n in prov["notes"])
