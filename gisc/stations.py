"""Station equations: the distance along an alignment is not its station.

An alignment is stationed from ``staStart``, and for most alignments the
station of a point is just ``staStart`` plus how far along it is. A re-stationed
alignment is not: a station equation declares that at one point the stationing
jumps, so the drawing reads ``14+00 Back = 20+00 Ahead`` and everything past it
is 600 ft further along than the geometry alone would say.

This is worth its own module because getting it wrong is invisible. The
geometry is right, the CRS is right, the offset is right, and the station --
the one number a contractor works from -- is off by the size of the equation.
gisc reads the equations, applies them, and puts the regions on the record.

Two things a station equation can do that a reader must be told about:

* a **gap** (``ahead > back``) skips a range of stations; no point on the
  alignment has one.
* an **overlap** (``ahead < back``) repeats a range; two different points on
  the alignment share a station, so a station alone stops being an address and
  the region has to be named with it.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from gisc.errors import AdapterError

# How far a declared staBack may sit from the station the preceding region
# actually produces before gisc stops believing the file. Well inside anything
# that would matter on a drawing, well outside export rounding.
BACK_TOLERANCE = 0.01


@dataclasses.dataclass(frozen=True)
class Equation:
    """One ``<StaEquation>``: at ``internal``, ``back`` becomes ``ahead``.

    ``internal`` is the raw, un-equated station -- ``staStart`` plus distance
    along -- which is the only one of the three that is tied to the geometry.
    """

    internal: float
    back: float
    ahead: float
    desc: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {"internal": self.internal, "back": self.back, "ahead": self.ahead}
        if self.desc:
            d["desc"] = self.desc
        return d


@dataclasses.dataclass(frozen=True)
class Region:
    """A run of alignment over which stationing is continuous."""

    index: int  # 1-based, as a drawing numbers them
    raw_from: float
    raw_to: float | None  # None on the last region: it runs to the end
    offset: float  # displayed = raw + offset

    def contains(self, raw: float) -> bool:
        if raw < self.raw_from:
            return False
        return self.raw_to is None or raw < self.raw_to

    def to_dict(self) -> dict[str, Any]:
        return {
            "region": self.index,
            "raw_from": round(self.raw_from, 4),
            "raw_to": None if self.raw_to is None else round(self.raw_to, 4),
            "offset": round(self.offset, 4),
            "display_from": round(self.raw_from + self.offset, 4),
            "display_to": (
                None if self.raw_to is None else round(self.raw_to + self.offset, 4)
            ),
        }


class Stationing:
    """Raw station -> the station printed on the drawing.

    With no equations this is the identity, which is the common case and costs
    nothing. ``notes`` is what gisc has to tell the reader about the equations
    it found; it is never empty when the stationing is ambiguous.
    """

    def __init__(self, sta_start: float = 0.0, equations: list[Equation] | None = None):
        self.sta_start = float(sta_start)
        given = list(equations or [])
        self.equations = sorted(given, key=lambda e: e.internal)
        self.notes: list[str] = []
        if [e.internal for e in given] != [e.internal for e in self.equations]:
            # Sorting is the right thing to do -- an equation's place on the
            # alignment is its staInternal, not its place in the file -- but an
            # exporter that writes them out of order may also have derived one
            # from staBack, which does depend on order. Say that it happened.
            self.notes.append(
                "station equations were not in station order in the file; gisc "
                "sorted them by staInternal. Check any equation that gave a "
                "staBack but no staInternal."
            )
        self.regions = self._build()

    # -- construction ------------------------------------------------------

    def _build(self) -> list[Region]:
        regions = [Region(index=1, raw_from=self.sta_start, raw_to=None, offset=0.0)]
        if not self.equations:
            return regions

        for i, eq in enumerate(self.equations, start=1):
            previous = regions[-1]
            if eq.internal < previous.raw_from:
                raise AdapterError(
                    f"station equation {i} is at internal station {eq.internal:g}, "
                    f"before the start of the region it equates ({previous.raw_from:g}). "
                    "An equation off the start of the alignment does not describe "
                    "any point on it."
                )
            expected_back = eq.internal + previous.offset
            if abs(eq.back - expected_back) > BACK_TOLERANCE:
                self.notes.append(
                    f"station equation {i} declares staBack={eq.back:g}, but the "
                    f"stationing ahead of it reaches {expected_back:g} at that point "
                    f"(a {eq.back - expected_back:+g} discrepancy); gisc stationed from "
                    "staInternal, which is the one tied to the geometry"
                )
            offset = eq.ahead - eq.internal
            jump = eq.ahead - expected_back
            if jump > 0:
                self.notes.append(
                    f"station equation {i} at {_label(expected_back)} is a gap: "
                    f"{_label(eq.back)} back = {_label(eq.ahead)} ahead, so {jump:g} ft "
                    "of stationing does not exist on this alignment"
                )
            elif jump < 0:
                self.notes.append(
                    f"station equation {i} at {_label(expected_back)} is an overlap: "
                    f"{_label(eq.back)} back = {_label(eq.ahead)} ahead, so {-jump:g} ft "
                    "of stationing occurs twice -- a station in that range names two "
                    "points and needs its region with it"
                )
            regions[-1] = dataclasses.replace(previous, raw_to=eq.internal)
            regions.append(
                Region(index=i + 1, raw_from=eq.internal, raw_to=None, offset=offset)
            )
        return regions

    # -- use ---------------------------------------------------------------

    @property
    def equated(self) -> bool:
        return bool(self.equations)

    @property
    def monotonic(self) -> bool:
        """False when some station names more than one point on the alignment."""
        return all(
            b.offset >= a.offset for a, b in zip(self.regions, self.regions[1:])
        )

    def raw(self, distance: float) -> float:
        """Distance along the alignment -> raw, un-equated station."""
        return self.sta_start + distance

    def region_of(self, raw: float) -> Region:
        for region in self.regions:
            if region.contains(raw):
                return region
        # Only reachable below the first region: a point off the start of the
        # alignment, which nearest_points can produce for a feature behind it.
        return self.regions[0]

    def display(self, raw: float | None) -> float | None:
        """Raw station -> the station on the drawing."""
        if raw is None:
            return None
        return raw + self.region_of(raw).offset

    def to_dict(self) -> dict[str, Any]:
        return {
            "sta_start": self.sta_start,
            "equations": [e.to_dict() for e in self.equations],
            "regions": [r.to_dict() for r in self.regions],
            "monotonic": self.monotonic,
            "notes": self.notes,
        }

    # -- travelling with a GeoDataFrame ------------------------------------
    #
    # The equations are read by the LandXML adapter and used by the sample op,
    # with a reproject and a copy in between. A JSON string in a column
    # survives all of that unambiguously, and stays readable to anyone who
    # dumps the frame.

    COLUMN = "sta_equations"

    def to_json(self) -> str:
        return json.dumps([e.to_dict() for e in self.equations])

    @classmethod
    def from_json(cls, sta_start: float, blob: Any) -> "Stationing":
        if blob is None or blob != blob or not str(blob).strip():  # NaN-safe
            return cls(sta_start)
        try:
            raw = json.loads(blob) if isinstance(blob, str) else blob
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"unreadable station equations: {blob!r}") from exc
        return cls(
            sta_start,
            [
                Equation(
                    internal=float(d["internal"]),
                    back=float(d["back"]),
                    ahead=float(d["ahead"]),
                    desc=d.get("desc"),
                )
                for d in raw
            ],
        )


def _label(sta: float) -> str:
    """12+00.00, for messages. The canonical formatter lives in exec."""
    sign = "-" if sta < 0 else ""
    whole, rem = divmod(int(round(abs(sta) * 100)), 10_000)
    return f"{sign}{whole}+{rem / 100:05.2f}"
