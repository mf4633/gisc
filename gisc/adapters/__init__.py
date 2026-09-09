"""Adapters read what already exists. None of them write to their source."""

from __future__ import annotations

import pathlib
from typing import Any

from gisc.adapters import fema, geojson, gpkg, landxml, postgis
from gisc.errors import AdapterError, UsageError

_BY_KIND = {
    "geojson": geojson,
    "gpkg": gpkg,
    "landxml": landxml,
    "postgis": postgis,
    "fema": fema,
}

_BY_SUFFIX = {
    ".geojson": "geojson",
    ".json": "geojson",
    ".gpkg": "gpkg",
    ".xml": "landxml",
    ".landxml": "landxml",
}


def detect_kind(ref: str) -> str:
    """Work out which adapter owns ``ref`` from its URI scheme or extension."""
    low = ref.lower()
    if low.startswith(("postgresql://", "postgres://")):
        return "postgis"
    if low.startswith(("nfhl:", "fema:")) or "hazards.fema.gov" in low:
        return "fema"
    suffix = pathlib.PurePath(ref).suffix.lower()
    if suffix in _BY_SUFFIX:
        return _BY_SUFFIX[suffix]
    raise UsageError(
        f"no adapter for {ref!r}: unrecognised extension {suffix or '(none)'}. "
        f"Supported: {', '.join(sorted(set(_BY_SUFFIX.values())))}, postgis URI, fema/nfhl URI."
    )


def get(kind: str):
    try:
        return _BY_KIND[kind]
    except KeyError:
        raise AdapterError(f"unknown adapter kind {kind!r}") from None


def describe(ref: str, kind: str | None = None, layer: str | None = None,
             crs_override: str | None = None) -> dict[str, Any]:
    """Header-only probe: CRS, layer, feature count. Reads no geometry."""
    kind = kind or detect_kind(ref)
    return get(kind).describe(ref, layer=layer, crs_override=crs_override)


def read(ref: str, kind: str | None = None, layer: str | None = None,
         crs_override: str | None = None):
    """Return ``(GeoDataFrame, provenance dict)``."""
    kind = kind or detect_kind(ref)
    return get(kind).read(ref, layer=layer, crs_override=crs_override)


__all__ = ["detect_kind", "describe", "read", "get", "geojson", "gpkg", "landxml",
           "postgis", "fema"]
