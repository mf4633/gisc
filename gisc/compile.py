"""The frontend: civil intent + inputs -> IR.

Compiling never executes an op. With ``probe=True`` (the default) it reads
source *headers* so the plan can state each input's native CRS; it reads no
geometry and connects to nothing. ``gisc ir`` stops here.
"""

from __future__ import annotations

import pathlib
from typing import Any

from gisc.errors import UsageError
from gisc.ir import Plan, validate
from gisc.tasks import TASKS


def compile_task(task: str, *, probe: bool = True, **kwargs: Any) -> Plan:
    if task not in TASKS:
        raise UsageError(f"unknown task {task!r}; known tasks: {', '.join(sorted(TASKS))}")
    plan = TASKS[task].compile_plan(probe=probe, **kwargs)
    validate(plan)
    return plan


def write_plan(plan: Plan, out_dir: pathlib.Path | str) -> pathlib.Path:
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / "plan.json"
    dest.write_text(plan.to_json(), encoding="utf-8")
    return dest
