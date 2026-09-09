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
never a guess.

**Task:** `corridor.conflicts` — what is inside N feet of an alignment. **Ops:**
`read, reproject, buffer, intersect, sample, write`, and nothing else.

**Feet are ground feet.** A projected CRS measures grid distance, so gisc measures
the local scale factor empirically (a geodesic walk on WGS 84) and converts. In
EPSG:3857 near 35.6°N that is 1.230, so a naive 15 ft buffer would cover 12.2 ft of
real ground. Stationing is measured in the alignment's *native design CRS*, because
a station is a grid distance in the system the drawing was made in.

**Stubbed:** `postgis` and `fema` (NFHL). Both parse their input and fail with the
exact SQL or HTTP request they would issue — export to GeoPackage/GeoJSON meanwhile.
**LandXML** reads `Line` and `Curve` only; a spiral, a discontinuity, or a missing
`<CoordinateSystem>` fails loudly and names the element. Use a GeoJSON LineString
alignment if your export is uglier than that.

Python ≥3.11 (3.12 is untested here only because this machine has 3.11).
Regenerate fixtures with `python fixtures/make_fixtures.py`.
