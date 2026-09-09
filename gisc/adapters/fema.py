"""FEMA NFHL -- stubbed on purpose.

The National Flood Hazard Layer is a live ArcGIS service. Reading it means a
network call whose result changes without notice, so provenance has to record
the query time and the exact request. This adapter states the request it would
make and refuses to invent an answer.

Point --flood at a downloaded GeoJSON/GPKG of the effective FIRM panels in the
meantime; that is what a submittal wants anyway.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from gisc.errors import StubError, UsageError

# Layer 28 of the NFHL map service is the flood hazard area polygon layer.
SERVICE = (
    "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
)


def parse_ref(ref: str) -> dict[str, Any]:
    """nfhl:?where=...  or a hazards.fema.gov URL."""
    u = urllib.parse.urlparse(ref)
    if u.scheme not in ("nfhl", "fema", "http", "https"):
        raise UsageError(f"not an NFHL reference: {ref!r}")
    q = urllib.parse.parse_qs(u.query)
    return {
        "service": SERVICE,
        "where": (q.get("where") or ["1=1"])[0],
        "layer": (q.get("layer") or ["28"])[0],
    }


def would_request(ref: str, bbox: tuple[float, float, float, float] | None = None,
                  bbox_crs: str = "EPSG:4326") -> dict[str, Any]:
    """The exact HTTP query this adapter would issue."""
    p = parse_ref(ref)
    params = {
        "f": "geojson",
        "where": p["where"],
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": "4326",
    }
    if bbox:
        params["geometry"] = ",".join(f"{v:.8f}" for v in bbox)
        params["geometryType"] = "esriGeometryEnvelope"
        params["inSR"] = bbox_crs.split(":")[-1]
        params["spatialRel"] = "esriSpatialRelIntersects"
    return {"method": "GET", "url": p["service"], "params": params}


def describe(ref: str, layer: str | None = None, crs_override: str | None = None) -> dict[str, Any]:
    p = parse_ref(ref)
    return {
        "kind": "fema",
        "ref": ref,
        "layer": p["layer"],
        "native_crs": crs_override or "EPSG:4326",
        "crs_source": "user_override" if crs_override else "service_declared",
        "stub": True,
        "would_request": would_request(ref),
    }


def read(ref: str, layer: str | None = None, crs_override: str | None = None):
    req = would_request(ref)
    pretty = urllib.parse.urlencode(req["params"])
    raise StubError(
        "fema (NFHL) adapter is not implemented.\n"
        f"  it would issue: {req['method']} {req['url']}?{pretty}\n"
        "  Until then: download the effective flood hazard polygons and point "
        "--flood at that GeoJSON or GeoPackage."
    )
