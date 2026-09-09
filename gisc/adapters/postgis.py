"""PostGIS -- stubbed on purpose.

gisc is stateless, so the only thing it would ever do to a PostGIS instance is
SELECT. Rather than pretend, this adapter parses the URI, works out the exact
read-only SQL it would issue, and raises with that SQL in the message. When it
is implemented it will run precisely this and nothing else.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from gisc.errors import StubError, UsageError


def parse_uri(ref: str) -> dict[str, Any]:
    """postgresql://user@host:5432/dbname?layer=schema.table&geom=geom"""
    u = urllib.parse.urlparse(ref)
    if u.scheme not in ("postgresql", "postgres"):
        raise UsageError(f"not a PostGIS URI: {ref!r}")
    if not u.hostname:
        raise UsageError(f"{ref!r}: no host")
    database = (u.path or "").lstrip("/")
    if not database:
        raise UsageError(f"{ref!r}: no database in the path")

    q = urllib.parse.parse_qs(u.query)
    table = (q.get("layer") or q.get("table") or [None])[0]
    if not table:
        raise UsageError(
            f"{ref!r}: name the table with ?layer=schema.table -- gisc will not "
            "enumerate your database looking for one."
        )
    schema, _, name = table.rpartition(".")
    return {
        "host": u.hostname,
        "port": u.port or 5432,
        "user": u.username,
        "database": database,
        "schema": schema or "public",
        "table": name,
        "geom_column": (q.get("geom") or ["geom"])[0],
        "where": (q.get("where") or [None])[0],
    }


def would_run(ref: str) -> dict[str, str]:
    """The read-only statements this adapter would issue, in order."""
    p = parse_uri(ref)
    qualified = f'"{p["schema"]}"."{p["table"]}"'
    geom = f'"{p["geom_column"]}"'
    where = f"\nWHERE {p['where']}" if p["where"] else ""
    return {
        "srid": (
            f"SELECT Find_SRID('{p['schema']}', '{p['table']}', '{p['geom_column']}') AS srid;"
        ),
        "features": (
            f"SELECT *, ST_AsBinary({geom}) AS __wkb\nFROM {qualified}{where};"
        ),
        "count": f"SELECT count(*) FROM {qualified}{where};",
    }


def _not_yet(ref: str) -> StubError:
    p = parse_uri(ref)
    sql = would_run(ref)
    return StubError(
        "postgis adapter is not implemented.\n"
        f"  target : {p['user'] or '(no user)'}@{p['host']}:{p['port']}/{p['database']} "
        f"-> {p['schema']}.{p['table']} ({p['geom_column']})\n"
        "  it would issue exactly these read-only statements:\n"
        + "\n".join(f"    {line}" for q in sql.values() for line in q.splitlines())
        + "\n  Until then: export the layer to GeoPackage and point --utils at it."
    )


def describe(ref: str, layer: str | None = None, crs_override: str | None = None) -> dict[str, Any]:
    """Enough to compile a plan. It does not connect."""
    p = parse_uri(ref)
    return {
        "kind": "postgis",
        "ref": ref,
        "layer": f"{p['schema']}.{p['table']}",
        # No connection, so no SRID. The plan says so rather than guessing.
        "native_crs": crs_override,
        "crs_source": "user_override" if crs_override else "unknown",
        "stub": True,
        "would_run": would_run(ref),
    }


def read(ref: str, layer: str | None = None, crs_override: str | None = None):
    raise _not_yet(ref)
