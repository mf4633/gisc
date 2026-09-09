"""GeoPackage. Read-only: gisc never writes back to the owner's .gpkg."""

from __future__ import annotations

from typing import Any

import geopandas as gpd
import pyogrio

from gisc.adapters._common import file_provenance, now, resolve
from gisc.errors import AdapterError, MissingCRSError


def _layers(path) -> list[str]:
    try:
        return [str(row[0]) for row in pyogrio.list_layers(path)]
    except Exception as exc:  # noqa: BLE001
        raise AdapterError(f"{path}: cannot list GeoPackage layers: {exc}") from exc


def _pick_layer(path, layer: str | None) -> str:
    names = _layers(path)
    if not names:
        raise AdapterError(f"{path}: GeoPackage has no vector layers")
    if layer is None:
        if len(names) > 1:
            raise AdapterError(
                f"{path}: has {len(names)} layers ({', '.join(names)}); "
                "name one with layer=<name>"
            )
        return names[0]
    if layer not in names:
        raise AdapterError(f"{path}: no layer {layer!r}; available: {', '.join(names)}")
    return layer


def describe(ref: str, layer: str | None = None, crs_override: str | None = None) -> dict[str, Any]:
    path = resolve(ref)
    name = _pick_layer(path, layer)
    info = pyogrio.read_info(path, layer=name)
    native = info.get("crs")
    if crs_override:
        native, source = crs_override, "user_override"
    else:
        source = "declared" if native else "unknown"
    return {
        "kind": "gpkg",
        "ref": str(path),
        "layer": name,
        "native_crs": native,
        "crs_source": source,
        "features_declared": int(info.get("features", -1)),
    }


def read(ref: str, layer: str | None = None, crs_override: str | None = None):
    info = describe(ref, layer=layer, crs_override=crs_override)
    path = resolve(ref)
    try:
        gdf = gpd.read_file(path, layer=info["layer"])
    except Exception as exc:  # noqa: BLE001
        raise AdapterError(f"{path}: GeoPackage read failed: {exc}") from exc

    if crs_override:
        gdf = gdf.set_crs(crs_override, allow_override=True)
    if gdf.crs is None:
        raise MissingCRSError(
            f"{path} (layer {info['layer']!r}): the GeoPackage declares no SRS. "
            "gisc will not guess one -- set the layer's SRS, or pass an explicit CRS."
        )

    prov = {
        **info,
        **file_provenance(path),
        "read_at": now(),
        "features_in": int(len(gdf)),
        "geometry_types": sorted({t for t in gdf.geom_type.dropna().unique()}),
        "filter": None,
    }
    return gdf, prov
