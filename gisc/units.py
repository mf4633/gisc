"""Turning a design distance in feet into a distance in the analysis CRS.

This is the part civil people get burned by. A 15 ft buffer is a distance on
the ground. A projected CRS measures grid distance, which is a different
number: EPSG:3857 near latitude 35.6 deg runs ~1.23, so an uncorrected 15 ft
buffer covers only ~12.2 ft of real ground. State plane runs the other way at
~0.9999.

gisc measures the scale factor empirically -- walk a known geodesic distance
on the WGS 84 ellipsoid, then see how far that is in the analysis CRS. That
avoids trusting a projection's analytic scale factor, which is wrong for
EPSG:3857: pyproj reports the *spherical* Mercator factor (1.2293 at 35.6N)
while the true grid/ground ratio is 1.2334 north-south and 1.2279 east-west.
Web Mercator is not conformal on an ellipsoid, and gisc says so out loud
rather than absorbing a 0.4% error.
"""

from __future__ import annotations

import math
from typing import Any

from pyproj import CRS, Geod, Transformer

from gisc.errors import UsageError

# A survey foot and an international foot differ by 2 ppm. Design clearances
# like "15 ft" are international feet.
FT_TO_M = 0.3048
SQFT_PER_ACRE = 43_560.0

_GEOD = Geod(ellps="WGS84")
_PROBE_M = 100.0  # geodesic probe length; long enough to be numerically clean
_AZIMUTHS = (0.0, 45.0, 90.0, 135.0)  # 180 deg apart is the same line

# How far a buffer's polygonised arc may fall inside the true circle, in ground
# feet. Correcting a 1.23 scale factor and then cutting the corners off the ends
# with shapely's default 8 segments per quadrant -- 0.072 ft on a 15 ft buffer --
# would give back most of what the correction just bought.
BUFFER_CHORD_TOLERANCE_FT = 0.001


def _projected(crs: Any) -> CRS:
    crs = CRS.from_user_input(crs)
    if crs.is_geographic:
        raise UsageError(
            f"cannot measure feet in geographic CRS {crs.to_string()}: its units are "
            "degrees. Pass a projected --crs (EPSG:3857, or better, your state plane "
            "zone)."
        )
    return crs


def scale_factor(crs: Any, at_xy: tuple[float, float]) -> dict[str, Any]:
    """Empirical grid/ground scale at ``at_xy``, measured not assumed.

    Returns the mean dimensionless factor plus the per-azimuth spread, so a
    non-conformal CRS cannot hide its anisotropy in an average.
    """
    crs = _projected(crs)
    unit_to_m = crs.axis_info[0].unit_conversion_factor
    to_ll = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    to_xy = Transformer.from_crs("EPSG:4326", crs, always_xy=True)

    lon, lat = to_ll.transform(*at_xy)
    if not (math.isfinite(lon) and math.isfinite(lat)):
        raise UsageError(
            f"point {at_xy} does not transform out of {crs.to_string()}; the data is "
            "probably not where the CRS says it is."
        )

    factors = []
    for az in _AZIMUTHS:
        lon2, lat2, _ = _GEOD.fwd(lon, lat, az, _PROBE_M)
        x2, y2 = to_xy.transform(lon2, lat2)
        grid = math.hypot(x2 - at_xy[0], y2 - at_xy[1]) * unit_to_m
        factors.append(grid / _PROBE_M)

    k = sum(factors) / len(factors)
    if not (0.5 < k < 2.0):
        raise UsageError(
            f"grid/ground scale factor {k:g} at ({lon:.5f}, {lat:.5f}) is implausible "
            f"for {crs.to_string()}; the data is probably not where the CRS says it is."
        )
    return {
        "crs": crs.to_string(),
        "crs_unit": crs.axis_info[0].unit_name,
        "crs_unit_to_m": unit_to_m,
        "point_scale_factor": k,
        "scale_factor_at": [round(lon, 6), round(lat, 6)],
        "scale_factor_range": [min(factors), max(factors)],
        "anisotropy_pct": round(100.0 * (max(factors) - min(factors)) / k, 4),
        "method": (
            f"measured: {_PROBE_M:g} m geodesic on WGS84 at azimuths "
            f"{', '.join(f'{a:g}' for a in _AZIMUTHS)} deg, re-measured in the CRS"
        ),
    }


def quad_segs(dist_crs_units: float, tolerance_crs_units: float) -> int:
    """Segments per quadrant so a buffer's arc stays within ``tolerance``.

    A polygonised buffer is inscribed: every arc falls *inside* the true
    circle, so a 15 ft corridor drawn at shapely's default 8 segments per
    quadrant is a 14.93 ft corridor at the ends, and a utility sitting in that
    0.07 ft is reported as clear when it is not.
    """
    if dist_crs_units <= 0 or tolerance_crs_units <= 0:
        return 8
    ratio = min(1.0, tolerance_crs_units / dist_crs_units)
    half = math.acos(max(-1.0, min(1.0, 1.0 - ratio)))
    if half <= 0:
        return 512
    return int(max(8, min(512, math.ceil(math.pi / (4.0 * half)))))


def buffer_distance(crs: Any, dist_ft: float, at_xy: tuple[float, float],
                    extent: list[tuple[float, float]] | None = None) -> dict[str, Any]:
    """Distance in CRS units that spans ``dist_ft`` of true ground at ``at_xy``.

    ``extent`` is other points on the same feature. The scale factor is a
    *point* property; over a long alignment it drifts, and a single buffer
    distance cannot be exactly right everywhere. gisc measures the drift and
    reports what it costs in feet rather than letting it pass unmentioned.
    """
    sf = scale_factor(crs, at_xy)
    ground_m = dist_ft * FT_TO_M
    k = sf["point_scale_factor"]
    distance = ground_m * k / sf["crs_unit_to_m"]
    tolerance = BUFFER_CHORD_TOLERANCE_FT * FT_TO_M / sf["crs_unit_to_m"]
    segs = quad_segs(distance, tolerance)

    out: dict[str, Any] = {
        "dist_ft": dist_ft,
        "basis": "true ground distance",
        "ground_m": ground_m,
        **sf,
        "distance_in_crs_units": distance,
        "quad_segs": segs,
        "chord_error_ft": round(
            dist_ft * (1.0 - math.cos(math.pi / (4.0 * segs))), 6
        ),
        "notes": [],
    }

    if extent:
        factors = [scale_factor(crs, p)["point_scale_factor"] for p in extent]
        lo, hi = min(factors + [k]), max(factors + [k])
        # The buffer is one distance. Where the true factor is hi and we used
        # k, the ground width delivered is dist_ft * k / hi.
        worst = max(abs(dist_ft * k / lo - dist_ft), abs(dist_ft * k / hi - dist_ft))
        out["scale_factor_over_extent"] = [lo, hi]
        out["scale_factor_samples"] = len(factors) + 1
        out["extent_error_ft"] = round(worst, 6)
        if worst > max(0.01, 0.001 * dist_ft):
            out["notes"].append(
                f"the grid/ground scale factor runs {lo:.6f} to {hi:.6f} over this "
                f"alignment, so one buffer distance cannot be exact along all of it: "
                f"the {dist_ft:g} ft corridor is off by up to {worst:.3f} ft at the "
                "extremes. Use a state plane zone that actually covers this site, or "
                "split the alignment."
            )
    return out


def to_ft(dist_crs_units: float, crs: Any, k: float) -> float:
    """Inverse of :func:`buffer_distance`: grid distance -> ground feet."""
    unit_to_m = CRS.from_user_input(crs).axis_info[0].unit_conversion_factor
    return dist_crs_units * unit_to_m / k / FT_TO_M


def geodesic_ft(geom_wgs84: Any) -> float:
    """True ground length of a WGS 84 geometry, in feet. No projection involved."""
    return _GEOD.geometry_length(geom_wgs84) / FT_TO_M


def geodesic_acres(geom_wgs84: Any) -> float:
    """True ground area of a WGS 84 geometry, in acres. No projection involved."""
    area_m2, _perimeter = _GEOD.geometry_area_perimeter(geom_wgs84)
    return abs(area_m2) / (FT_TO_M**2) / SQFT_PER_ACRE


def grid_to_ft(dist_crs_units: float, crs: Any) -> float:
    """CRS units -> international feet, unit conversion only, no scale factor.

    This is the right conversion for stationing and offsets, which are grid
    distances in the design coordinate system -- that is what the plan sheet
    says and what Civil 3D computes.
    """
    unit_to_m = CRS.from_user_input(crs).axis_info[0].unit_conversion_factor
    return dist_crs_units * unit_to_m / FT_TO_M
