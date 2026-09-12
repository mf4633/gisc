"""The IR: one common plan that every task compiles down to.

Keep this small and stable. The whole point of gisc is that ``plan.json`` is a
complete, readable statement of what will happen before anything happens.

That claim was untested while there was one task, so a second one was written
to try to break it -- ``alignment.crossings``, deliberately a different shape
of question: nothing to buffer, and an answer that is a point existing in
neither input until the intersection makes it. The op set survived unchanged,
which is the part worth believing. Two things did not:

* ``buffer_ft`` was a **required** field here, so a task with no corridor had
  to name a corridor width. It is optional now, and the buffer op's own
  ``dist_ft`` is the real home for it.
* ``intersect`` always returned source geometry -- right for "which pipe is
  near me", useless for "where does it cross". It takes a ``geometry`` mode.

Neither needed a seventh op, but neither was free either. Read "every task"
as a claim with two data points behind it, not a law.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from gisc.errors import UsageError

# The complete op set. Do not add to this until every one of these works.
OPS = ("read", "reproject", "buffer", "intersect", "sample", "write")

# Required keys per op, beyond "op" itself.
_OP_KEYS: dict[str, tuple[str, ...]] = {
    "read": ("src",),
    "reproject": ("src", "to"),
    "buffer": ("src", "dist_ft", "out"),
    "intersect": ("a", "b", "out"),
    "sample": ("src", "along", "out"),
    "write": ("src", "to"),
}


@dataclasses.dataclass
class Source:
    """One thing gisc will read. It is never copied into a store."""

    id: str
    kind: str  # geojson | gpkg | landxml | postgis | fema
    ref: str  # path or URI, exactly as the user gave it
    native_crs: str | None = None
    crs_source: str | None = None  # declared | rfc7946_default | user_override | unknown
    layer: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {"id": self.id, "kind": self.kind, "ref": self.ref, "native_crs": self.native_crs}
        if self.crs_source:
            d["crs_source"] = self.crs_source
        if self.layer:
            d["layer"] = self.layer
        return d


@dataclasses.dataclass
class Plan:
    """The compiled program. ``gisc ir`` prints this and stops."""

    task: str
    crs: str
    # Only tasks that buffer carry this. It stays on the Plan because the
    # summary reads it, but the buffer op already carries dist_ft, so a task
    # that never buffers -- alignment.crossings -- simply leaves it unset
    # rather than inventing a distance it does not use.
    buffer_ft: float | None = None
    sources: list[Source] = dataclasses.field(default_factory=list)
    ops: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    outputs: dict[str, str] = dataclasses.field(default_factory=dict)
    notes: list[str] = dataclasses.field(default_factory=list)

    def source(self, sid: str) -> Source:
        for s in self.sources:
            if s.id == sid:
                return s
        raise UsageError(f"plan references unknown source {sid!r}")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "task": self.task,
            "crs": self.crs,
            "sources": [s.to_dict() for s in self.sources],
            "ops": self.ops,
        }
        if self.buffer_ft is not None:
            d["buffer_ft"] = self.buffer_ft
        if self.outputs:
            d["outputs"] = self.outputs
        if self.notes:
            d["notes"] = self.notes
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent) + "\n"


def validate(plan: Plan) -> None:
    """Reject a plan that could not be executed, before touching any data."""
    if not plan.sources:
        raise UsageError("plan has no sources")

    ids = [s.id for s in plan.sources]
    if len(set(ids)) != len(ids):
        raise UsageError(f"duplicate source ids: {ids}")

    # Every name an op reads must already exist: a source, or an earlier "out".
    defined = set(ids)
    for i, op in enumerate(plan.ops):
        name = op.get("op")
        if name not in OPS:
            raise UsageError(f"ops[{i}]: unknown op {name!r}; allowed: {', '.join(OPS)}")
        for key in _OP_KEYS[name]:
            if key not in op:
                raise UsageError(f"ops[{i}] ({name}): missing required key {key!r}")
        for key in ("src", "a", "b", "along"):
            ref = op.get(key)
            if ref is not None and ref not in defined:
                raise UsageError(f"ops[{i}] ({name}): {key}={ref!r} is not defined yet")
        if "out" in op:
            defined.add(op["out"])
