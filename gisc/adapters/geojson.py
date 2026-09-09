"""GeoJSON.

RFC 7946 says a GeoJSON file with no ``crs`` member is WGS 84. That is a
default, not a declaration, so provenance records which of the two it was.
"""

from __future__ import annotations

import json
from typing import Any

import geopandas as gpd

from gisc.adapters._common import file_provenance, now, resolve
from gisc.errors import AdapterError, MissingCRSError


def _crs_member(path) -> str | None:
    """Return a CRS named by a (pre-2016) ``crs`` member, if there is one."""
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError(f"{path}: not readable as JSON: {exc}") from exc
    crs = doc.get("crs")
    if not isinstance(crs, dict):
        return None
    name = (crs.get("properties") or {}).get("name")
    return name if isinstance(name, str) else None


def describe(ref: str, layer: str | None = None, crs_override: str | None = None) -> dict[str, Any]:
    path = resolve(ref)
    declared = _crs_member(path)
    if crs_override:
        crs, source = crs_override, "user_override"
    elif declared:
        crs, source = declared, "declared"
    else:
        crs, source = "EPSG:4326", "rfc7946_default"
    return {
        "kind": "geojson",
        "ref": str(path),
        "layer": None,
        "native_crs": crs,
        "crs_source": source,
    }


def read(ref: str, layer: str | None = None, crs_override: str | None = None):
    info = describe(ref, layer=layer, crs_override=crs_override)
    path = resolve(ref)
    try:
        gdf = gpd.read_file(path)
    except Exception as exc:  # noqa: BLE001 - the driver's message is the useful part
        raise AdapterError(f"{path}: GeoJSON read failed: {exc}") from exc

    if crs_override:
        gdf = gdf.set_crs(crs_override, allow_override=True)
    if gdf.crs is None:
        raise MissingCRSError(f"{path}: no CRS. Pass an explicit CRS or fix the file.")

    prov = {
        **info,
        **file_provenance(path),
        "read_at": now(),
        "features_in": int(len(gdf)),
        "geometry_types": sorted({t for t in gdf.geom_type.dropna().unique()}),
        "filter": None,
    }
    return gdf, prov
