"""Shared helpers for file-backed adapters."""

from __future__ import annotations

import datetime as _dt
import hashlib
import pathlib
from typing import Any

from gisc.errors import AdapterError


def resolve(ref: str) -> pathlib.Path:
    p = pathlib.Path(ref).expanduser()
    if not p.exists():
        raise AdapterError(f"input not found: {p}")
    return p.resolve()


def file_provenance(path: pathlib.Path) -> dict[str, Any]:
    """Where it came from and what state it was in when we read it."""
    st = path.stat()
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return {
        "ref": str(path),
        "bytes": st.st_size,
        "mtime": _dt.datetime.fromtimestamp(st.st_mtime, _dt.timezone.utc)
        .isoformat(timespec="seconds"),
        "sha256": h.hexdigest(),
    }


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
