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

## The second alignment: spirals and a station equation

The arc above bounded chord flattening. It left two things gisc had no
evidence for at all, both of which the README used to list as *not exercised*:
a **clothoid spiral**, and a **station equation**.

A second alignment was built the same way, in Civil 3D 2023 over COM:

    500 ft tangent, entry clothoid Ls = 150, arc R = 300, exit clothoid
    Ls = 150, 500 ft tangent, fitted to a 45 deg PI on CSCode NC83F,
    stationed from 10+00, with one station equation: 14+00 back = 20+00 ahead

| quantity | Civil 3D | gisc | difference |
|---|---|---|---|
| total length | 984.820206 ft | 984.818223 ft | **-0.0020 ft** |
| centre-line XY, 40 stations | — | — | worst **0.0091 ft** |
| spiral `TotalX` | 149.065208764 | 149.065208764 | **6.3e-11 ft** |
| spiral `TotalY` | 12.444307281 | 12.444307281 | **7.7e-11 ft** |
| offset, 8 probes | — | — | worst **0.0073 ft** |
| **equated station, 8 probes** | — | — | worst **0.0300 ft** |

The spiral figures are the interesting ones. Civil 3D carries the clothoid
analytically and publishes its own `TotalX`/`TotalY`; gisc integrates the
heading numerically from nothing but the length and the two radii. They agree
to about a ten-billionth of a foot, which says the integration is not where any
error lives. What is left is chording, and that shows up exactly where the arc
validation said it would: 0.0091 ft worst deviation against an advertised
0.01 ft tolerance.

The station equation is the one that would have hurt. Civil 3D prints
**20+47.88** at the point whose raw station is 14+47.88. Before this work gisc
printed 14+47.88 — right geometry, right offset, wrong station, and nothing
anywhere in the output saying so.

### Two things the COM API will do to you

`AddFreeSCSGroup1(PreviousElementId, NextElementId, Spiral1Length, Radius,
Spiral2Length, GreaterThan180, spiralDefinition)`

* **`GreaterThan180`** picks between the two curves tangent to both lines.
  Passing `1` succeeds, returns a valid object, and fits the **315 degree
  reflex curve** instead of the 45 degree one. Nothing complains; the only
  symptom is a 2900 ft alignment where 985 ft was expected. `capture_civil3d_spiral.py`
  now checks the fitted length against a hand computation before trusting it.
* **`spiralDefinition`** must be `1` for the clothoid. `0` raises.

`StationEquations.Add(RawStationBack, StationBack, StationAhead,
StationEquationType)` takes four arguments, and type `1` is increasing.

The parameter names above came out of the type library at runtime, not from
documentation — `c3d_api_dump.json` holds the dump, so the next person does not
have to start Civil 3D to read a signature.

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

python validation/capture_civil3d_spiral.py              # the spiral + equation alignment
python validation/write_spiral_landxml.py                # its LandXML, from Civil 3D's geometry
cp validation/alignment_spiral_c3d.xml validation/c3d_spiral_truth.json fixtures/civil3d/
```

Run them with the **system** Python: pywin32 lives there, not in the venv.
Civil 3D must already have a drawing open before COM will answer at all — a
fresh instance sitting on the Start tab returns a `Documents` collection with
no methods on it, which looks like a permissions problem and is not one.

Both attach to an already-running **Civil 3D 2023** instance, refuse any other
version, work only in a drawing they create themselves, and never save. That
version guard is deliberate: `New-Object`/`GetActiveObject` on a different
ProgID will happily attach to whatever session is open, including one with real
work in it.

## What this does not cover

The LandXML here was written by gisc from Civil 3D's coordinates, because Civil
3D's LandXML export is a modal dialog with no COM entry point. So this validates
gisc's **geometry, stationing and CRS handling** against Civil 3D. It does not
validate the parser against a genuine `LANDXMLOUT` file — the element ordering,
attribute spelling and profile blocks in a real export are still only covered by
the `LANDXMLOUT` sheet export and the storm `PipeNetwork` exports in the main
test suite, neither of which contains a spiral or an equation.
