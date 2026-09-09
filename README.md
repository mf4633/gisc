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
artifacts, records what it cleared, and refuses any folder it did not write.

**Task:** `corridor.conflicts` — what is inside N feet of an alignment. **Ops:**
`read, reproject, buffer, intersect, sample, write`, and nothing else.
**Exit codes:** 0 ok, 2 usage, 3 missing CRS, 4 adapter failed, 5 adapter stubbed.

**Feet are ground feet.** A projected CRS measures grid distance, so gisc measures
the local scale factor empirically (a geodesic walk on WGS 84) and converts. In
EPSG:3857 near 35.6°N that is 1.230, so a naive 15 ft buffer would cover 12.2 ft of
real ground. Stationing is measured in the alignment's *native design CRS*, because
a station is a grid distance in the system the drawing was made in.

**LandXML reads two subjects:** `<Alignment>` for the centre line, `<PipeNetwork>`
for the utility — so Civil 3D storm goes straight into `--utils` without converting
first. Pipes become lines, structures points. Pipe geometry lives on the structures
a pipe names, so a pipe whose structure is missing from the file is left out and
named in provenance, never straight-lined between guesses. Diameters are reported as
written: real files carry inches under `linearUnit="USSurveyFoot"`.

**Stubbed:** `postgis` and `fema` (NFHL) — both parse their input and fail with the
exact SQL or HTTP request they would issue. Alignments understand `Line` and `Curve`
only; a spiral, a discontinuity, or a missing `<CoordinateSystem>` fails loudly and
names the element.

**Tested:** 236 tests, 99% line coverage, plus two acceptance checks against real
tools. An alignment built in **Autodesk Civil 3D 2023** over COM agrees to 0.0025 ft
in length and 0.0095 ft worst coordinate deviation over 21 stations — inside the
0.01 ft chord tolerance the parser advertises (`validation/README.md`; committed
artifacts mean `pytest -m civil3d` needs no Civil 3D). A genuine `LANDXMLOUT` sheet
export and real `<PipeNetwork>` storm exports both parse, the latter correctly
refusing two pipes whose structures the export omitted.

**Not exercised:** a *curved* alignment from a real export, spirals (refused by
design), and station equations — no LandXML on the machine this was built on has a
curve or spiral in an alignment, so the arc case rests on the Civil 3D check above.

Python ≥3.11 (3.12 is untested here only because this machine has 3.11).
Regenerate fixtures with `python fixtures/make_fixtures.py`.
