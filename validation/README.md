# Validation against Autodesk Civil 3D

Every other test in this repo checks gisc against itself: fixtures whose
expected answers were derived by hand from the design geometry. That proves
internal consistency, not agreement with the tool the drawings come from.

This directory holds the acceptance test. An alignment was built in **Autodesk
Civil 3D 2023** over COM and Civil 3D was asked for its own answers. gisc then
computes the same quantities from the same geometry, independently.

## The alignment

A 400 ft tangent, a 45° arc at R = 300 ft, another 400 ft tangent, on
`CSCode NC83F` — NAD83 North Carolina State Planes, US survey foot, which is
EPSG:2264 — stationed from 10+00.

The arc is the point. Civil 3D carries a true circular arc; gisc flattens it to
chords at `landxml.ARC_TOLERANCE` (0.01 ft). Comparing the two puts a number in
feet on what that tolerance costs.

## What was measured, and what gisc got

| quantity | Civil 3D | gisc | difference |
|---|---|---|---|
| total length | 1035.619449 ft | 1035.616927 ft | **−0.0025 ft** |
| station, tangent probes (×4) | — | — | **0.0000 ft** |
| station, on the arc | 1517.8097 / 1635.6194 | 1517.8100 / 1635.6200 | ≤ **0.0006 ft** |
| station, nearest approach to the arc | 1521.7579 | 1521.7800 | **0.0221 ft** |
| offset, 8 ft either side | ∓8.0000 | 8.00 L / 8.00 R | **0.0000 ft** |
| offset, off the arc | −9.2136 | 9.21 L | **0.0036 ft** |
| centre-line XY, 21 stations | — | — | worst **0.0095 ft** |

The length comes in short, never long, because chords cut corners — 1/32 inch
over 1035 ft. The worst coordinate deviation, 0.0095 ft, sits just inside the
0.01 ft chord tolerance the parser advertises, which is the tolerance doing
exactly what it says.

## One thing worth knowing

**Civil 3D signs offsets negative-left, positive-right.** gisc reports an
unsigned `offset_ft` plus a `side` of `L`, `R` or `CL`. The two agree —
`test_side_matches_civil3ds_offset_sign` asserts the mapping — but anyone
diffing a gisc table against a Civil 3D report needs to know the signs are not
directly comparable.

## Reproducing it

`fixtures/civil3d/` holds Civil 3D's answers (`c3d_truth.json`) and a LandXML
written from the geometry read back out of Civil 3D (`alignment_c3d.xml`), so
`pytest -m civil3d` runs anywhere. Civil 3D is only needed to regenerate them:

```
python validation/capture_civil3d.py                     # build it, record the answers
python validation/read_entities_and_write_landxml.py      # read geometry back, write the LandXML
```

Both attach to an already-running **Civil 3D 2023** instance, refuse any other
version, work only in a drawing they create themselves, and never save. That
version guard is deliberate: `New-Object`/`GetActiveObject` on a different
ProgID will happily attach to whatever session is open, including one with real
work in it.

## What this does not cover

The LandXML here was written by gisc from Civil 3D's coordinates, because Civil
3D's LandXML export is a modal dialog with no COM entry point. So this validates
gisc's **geometry, stationing and CRS handling** against Civil 3D. It does not
validate the parser against a genuine `LANDXMLOUT` file — that still wants a
real export, with the spirals, station equations and profile blocks that come
with one.
