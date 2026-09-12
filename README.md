# gisc

A stateless civil/GIS compiler. Civil intent → IR → adapters → view + provenance.

```
pip install -e ".[dev]" && pytest && gisc compile corridor.conflicts \
  --alignment fixtures/alignment.xml \
  --utils fixtures/utilities.geojson \
  --flood fixtures/flood.geojson \
  --buffer-ft 15 --crs EPSG:3857 --out ./out/demo
```

`gisc ir <same args>` prints the plan and stops without executing any op.

**Stateless** means gisc has no database. It reads the files and services you already
own, in place, read-only, and writes exactly one folder: `plan.json` (the IR it
compiled), `conflicts.geojson`, `flood.geojson`, `summary.md`, and `provenance.json`
— source path, mtime, sha256, CRS in and out, filter, and feature counts for every
input. Every emitted feature carries the source it came from; if gisc cannot say
where a geometry came from, it does not emit it. A missing CRS is a non-zero exit,
never a guess. A run folder belongs to one run: gisc clears a previous run's
artifacts, records what it cleared, and refuses any folder it did not write. The
clearing is undone if anything goes wrong — the previous run is set aside, not
deleted, and put back on any failure — so a run either replaces the last one
completely or leaves it exactly as it was.

**Tasks:** `corridor.conflicts` — what is inside N feet of an alignment.
`alignment.crossings` — where something crosses the centreline, as the point the
intersection makes, with its station. **Ops:**
`read, reproject, buffer, intersect, sample, write`, and nothing else.
**Exit codes:** 0 ok, 2 usage, 3 missing CRS, 4 adapter failed, 5 adapter stubbed.

**Feet are ground feet.** A projected CRS measures grid distance, so gisc measures
the local scale factor empirically (a geodesic walk on WGS 84) and converts. In
EPSG:3857 near 35.6°N that is 1.230, so a naive 15 ft buffer would cover 12.2 ft of
real ground. Two things quietly give that back, so gisc handles both: a buffer is a
polygon *inscribed* in the true circle, and at the usual 8 segments per quadrant a
15 ft corridor is 14.93 ft at the ends, so gisc picks the segment count from the
tolerance and records the arc error it bought; and the scale factor is a property of
a *point*, so over a long alignment one buffer distance cannot be right everywhere —
gisc measures the drift along the extent and says what it costs in feet.

**A station is not a distance.** Stationing is measured in the alignment's *native
design CRS*, because a station is a grid distance in the system the drawing was made
in. And if the alignment carries `<StaEquation>` the stationing jumps: gisc applies
the equations, numbers the regions, and puts the raw stations beside them. Ignoring
one is the quietest way this format can make a tool wrong — the geometry stays
perfect, the offsets stay right, and every station past the equation is off by the
size of the equation. A gap and an overlap are both called out, and a feature that
straddles an equation is labelled with both regions, because across one the
displayed stations are no longer a length.

**LandXML reads two subjects:** `<Alignment>` for the centre line, `<PipeNetwork>`
for the utility — so Civil 3D storm goes straight into `--utils` without converting
first. Pipes become lines, structures points. Pipe geometry lives on the structures
a pipe names, so a pipe whose structure is missing from the file is left out and
named in provenance, never straight-lined between guesses. Diameters are reported as
written: real files carry inches under `linearUnit="USSurveyFoot"`.

**Alignments understand `Line`, `Curve` and `Spiral`.** The spiral is the clothoid,
which is what a roadway transition is: gisc integrates the heading — exact in closed
form — and quadratures its sine and cosine, so one expression covers an entry spiral,
an exit spiral and a compound spiral between two finite radii. It then checks the
result against the `<End>` the file declares and refuses a spiral that does not land
on it, naming the rotation as the likely cause when reversing it would close. A
non-clothoid transition, a discontinuity, or a missing `<CoordinateSystem>` fails
loudly and names the element.

**Stubbed:** `postgis` and `fema` (NFHL) — both parse their input and fail with the
exact SQL or HTTP request they would issue.

**Tested:** 363 tests, 100% line coverage, enforced in CI. Four guards no input can
reach are marked `pragma: no cover` with the reason, rather than mocked into a
number. Plus acceptance checks against real tools.
Two alignments built in **Autodesk Civil 3D 2023** over COM:

| | Civil 3D vs gisc |
|---|---|
| tangent–arc–tangent, length | **−0.0025 ft** over 1035 ft |
| tangent–arc–tangent, XY over 21 stations | worst **0.0095 ft** |
| spiral–curve–spiral, length | **−0.0020 ft** over 985 ft |
| spiral–curve–spiral, XY over 40 stations | worst **0.0091 ft** |
| clothoid `TotalX` / `TotalY` | **~7e-11 ft** |
| offset, 8 probes | worst **0.0073 ft** |
| **station across a station equation**, 8 probes | worst **0.0300 ft** |

Both sit inside the 0.01 ft chord tolerance the parser advertises. The clothoid
figures are the ones that say something: Civil 3D carries the spiral analytically
and publishes its own `TotalX`/`TotalY`, gisc integrates it from nothing but the
length and the two radii, and they agree to a ten-billionth of a foot — so what
error remains is chording, not the maths. See `validation/README.md`; committed
artifacts mean `pytest -m civil3d` needs no Civil 3D. A genuine `LANDXMLOUT` sheet
export and real `<PipeNetwork>` storm exports both parse, the latter correctly
refusing two pipes whose structures the export omitted.

**Not exercised:** a genuine `LANDXMLOUT` file containing a spiral or a station
equation. Both are now checked against Civil 3D's own geometry and its own equated
station strings, but the LandXML in that check was written *by* gisc from what Civil
3D reported over COM, because Civil 3D's LandXML export is a modal dialog with no COM
entry point. Element ordering and attribute spelling in a real export are covered
only by the `LANDXMLOUT` sheet export in the suite, which contains neither. Spiral
types other than the clothoid — bloss, sinusoidal, cosine, the cubics — are refused
by name rather than approximated.

Python ≥3.11, tested in CI on Linux and Windows against 3.11 and 3.12.
Regenerate fixtures with `python fixtures/make_fixtures.py`.
