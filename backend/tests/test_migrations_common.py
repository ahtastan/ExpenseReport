"""Unit tests for backend/migrations/_common.py.

The Day 2.5 migration test (test_m1_day25_migration.py) already exercises
refuse_protected_path and check_sqlite_version transitively. These tests
pin the standalone behavior of the extracted helpers + the new
migration_artifact_paths so future migrations can rely on them.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from migrations._common import (  # noqa: E402
    PROTECTED_PATH_FRAGMENTS,
    column_info,
    index_exists,
    migration_artifact_paths,
    refuse_protected_path,
    table_exists,
    utc_timestamp_compact,
)


# ---------------------------------------------------------------------------
# refuse_protected_path
# ---------------------------------------------------------------------------


def test_refuse_protected_path_blocks_var_lib():
    with pytest.raises(SystemExit) as exc_info:
        refuse_protected_path("/var/lib/dcexpense/expense_app.db")
    assert exc_info.value.code == 2


def test_refuse_protected_path_blocks_opt_dcexpense():
    with pytest.raises(SystemExit) as exc_info:
        refuse_protected_path("/opt/dcexpense/some.db")
    assert exc_info.value.code == 2


def test_refuse_protected_path_allows_tmp(tmp_path: Path):
    # No exception should be raised for a path well outside protected zones.
    refuse_protected_path(str(tmp_path / "scratch.db"))


def test_protected_path_fragments_are_documented_set():
    # Locks the fragments so a future contributor doesn't silently expand
    # the guard surface (or shrink it).
    assert PROTECTED_PATH_FRAGMENTS == ("/var/lib/dcexpense", "/opt/dcexpense")


# ---------------------------------------------------------------------------
# utc_timestamp_compact + migration_artifact_paths
# ---------------------------------------------------------------------------


def test_utc_timestamp_compact_format():
    ts = utc_timestamp_compact()
    # Format: YYYYMMDDTHHMMSSZ (16 chars, fixed positions for T and Z)
    assert len(ts) == 16
    assert ts[8] == "T"
    assert ts[15] == "Z"


def test_migration_artifact_paths_default_naming():
    ts, backup, log = migration_artifact_paths("/tmp/sample.db", "m1-day3a")
    assert backup == f"/tmp/sample.db.pre-m1-day3a-{ts}.backup"
    assert log == f"/tmp/sample.db.pre-m1-day3a-{ts}.migration.log"


def test_migration_artifact_paths_explicit_timestamp():
    # When ts is provided, both paths use it without re-querying time.
    ts, backup, log = migration_artifact_paths(
        "/tmp/x.db", "m1-day25", ts="20260101T000000Z"
    )
    assert ts == "20260101T000000Z"
    assert backup == "/tmp/x.db.pre-m1-day25-20260101T000000Z.backup"
    assert log == "/tmp/x.db.pre-m1-day25-20260101T000000Z.migration.log"


# ---------------------------------------------------------------------------
# table_exists / column_info / index_exists
# ---------------------------------------------------------------------------


def _seed_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE foo (id INTEGER PRIMARY KEY, amount NUMERIC(18,4))")
        conn.execute("CREATE INDEX ix_foo_amount ON foo (amount)")


def test_table_exists_true_for_existing_table(tmp_path: Path):
    db = tmp_path / "t.db"
    _seed_db(db)
    with sqlite3.connect(db) as conn:
        assert table_exists(conn, "foo") is True


def test_table_exists_false_for_missing_table(tmp_path: Path):
    db = tmp_path / "t.db"
    _seed_db(db)
    with sqlite3.connect(db) as conn:
        assert table_exists(conn, "bar") is False


def test_column_info_returns_declared_type(tmp_path: Path):
    db = tmp_path / "t.db"
    _seed_db(db)
    with sqlite3.connect(db) as conn:
        info = column_info(conn, "foo", "amount")
        assert info is not None
        declared, exists = info
        assert declared == "NUMERIC(18,4)"
        assert exists is True


def test_column_info_returns_none_for_missing_column(tmp_path: Path):
    db = tmp_path / "t.db"
    _seed_db(db)
    with sqlite3.connect(db) as conn:
        assert column_info(conn, "foo", "nonexistent") is None


def test_index_exists_true_when_present(tmp_path: Path):
    db = tmp_path / "t.db"
    _seed_db(db)
    with sqlite3.connect(db) as conn:
        assert index_exists(conn, "ix_foo_amount") is True


def test_index_exists_false_when_absent(tmp_path: Path):
    db = tmp_path / "t.db"
    _seed_db(db)
    with sqlite3.connect(db) as conn:
        assert index_exists(conn, "ix_foo_nope") is False
