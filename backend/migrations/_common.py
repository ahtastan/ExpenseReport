"""Shared helpers for production-DB migration scripts.

Extracted from m1_day25_money_decimal.py during M1 Day 3a so subsequent
migrations (M1 Day 3a field-provenance, future M1 Day 7 FX, etc.) reuse
the same protected-path guard, version check, timestamp/artifact-naming,
and SQLite introspection helpers instead of copy-pasting them.

Every migration script in this directory must:
  1. Call ``refuse_protected_path(db_path)`` before any read or write.
  2. Call ``check_sqlite_version()`` before issuing DDL that depends on
     SQLite features newer than the project minimum (3.35.0).
  3. Use ``migration_artifact_paths(db_path, migration_id)`` to derive
     the backup and audit-log paths so the naming pattern stays uniform
     and the rollback procedure documented in each script's docstring
     keeps working.

The introspection helpers (table_exists, column_info, index_exists)
encapsulate the small amount of sqlite_master / PRAGMA querying that
every migration ends up doing.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


PROTECTED_PATH_FRAGMENTS = ("/var/lib/dcexpense", "/opt/dcexpense")

DEFAULT_MIN_SQLITE_VERSION = (3, 35, 0)


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------


def refuse_protected_path(db_path: str) -> None:
    """Hard refusal if the path resolves to a protected production prefix.

    Uses Path.resolve() so symlinks and relative paths can't smuggle a
    protected location past a literal-substring check. Operational rule
    #6 forbids in-place migration of /var/lib/dcexpense or /opt/dcexpense
    paths; the live DB must be copied to /tmp first.
    """
    raw = db_path.replace("\\", "/").lower()
    resolved = str(Path(db_path).resolve()).replace("\\", "/").lower()
    for fragment in PROTECTED_PATH_FRAGMENTS:
        for candidate in (raw, resolved):
            if fragment in candidate:
                print(
                    f"REFUSED: {db_path!r} resolves to a path matching protected "
                    f"fragment {fragment!r}.\n"
                    "  Per operational rule #6, copy the live DB to /tmp first, "
                    "run the migration on the copy, then copy back.\n"
                    "  This script will not migrate the live file in place.",
                    file=sys.stderr,
                )
                raise SystemExit(2)


def check_sqlite_version(min_version: tuple[int, int, int] = DEFAULT_MIN_SQLITE_VERSION) -> None:
    """Refuse to run on a SQLite older than the migration's required version.

    Default minimum is 3.35.0 (DROP COLUMN landed March 2021). Migrations
    that don't need that feature can still pass the default — production
    runs SQLite 3.45.1, well above the floor.
    """
    if sqlite3.sqlite_version_info < min_version:
        have = ".".join(str(x) for x in sqlite3.sqlite_version_info)
        need = ".".join(str(x) for x in min_version)
        print(
            f"REFUSED: SQLite {have} bundled with this Python is too old. "
            f"This migration requires SQLite ≥ {need}.",
            file=sys.stderr,
        )
        raise SystemExit(2)


# ---------------------------------------------------------------------------
# timestamps & artifact paths
# ---------------------------------------------------------------------------


def utc_timestamp_compact() -> str:
    """UTC timestamp in compact format suitable for filenames."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def migration_artifact_paths(
    db_path: str, migration_id: str, ts: str | None = None
) -> tuple[str, str, str]:
    """Return (timestamp, backup_path, log_path) for a migration run.

    The naming pattern matches the rollback procedure documented in each
    migration's docstring: ``<db>.pre-<migration_id>-<ts>.backup`` /
    ``.migration.log``. ``migration_id`` is the short slug like
    "m1-day25" or "m1-day3a".
    """
    if ts is None:
        ts = utc_timestamp_compact()
    backup_path = f"{db_path}.pre-{migration_id}-{ts}.backup"
    log_path = f"{db_path}.pre-{migration_id}-{ts}.migration.log"
    return ts, backup_path, log_path


# ---------------------------------------------------------------------------
# sqlite introspection
# ---------------------------------------------------------------------------


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def column_info(
    conn: sqlite3.Connection, table: str, column: str
) -> tuple[str, bool] | None:
    """Return (declared_type, exists) for table.column, or None if absent."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    for row in rows:
        # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk)
        if row[1] == column:
            return (row[2] or "", True)
    return None


def index_exists(conn: sqlite3.Connection, index_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    ).fetchone()
    return row is not None
