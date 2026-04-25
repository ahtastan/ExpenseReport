"""Tests for the M1 Day 2.5 money/rate Decimal column migration.

Builds an old-shape SQLite database (FLOAT columns, with the production
schema's index on ``statementtransaction.local_amount``), seeds rows that
exercise the verification edge cases (NULL, zero, 4-dp precision, 8-dp
rate), runs the migration, and asserts the post-state.
"""

from __future__ import annotations

import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from migrations import m1_day25_money_decimal as migration  # noqa: E402


# Old-shape DDL: matches the pre-migration production schema (FLOAT columns).
RECEIPTDOCUMENT_DDL_OLD = """
CREATE TABLE receiptdocument (
    id INTEGER NOT NULL PRIMARY KEY,
    extracted_local_amount FLOAT
)
"""

STATEMENTTRANSACTION_DDL_OLD = """
CREATE TABLE statementtransaction (
    id INTEGER NOT NULL PRIMARY KEY,
    local_amount FLOAT,
    usd_amount FLOAT
)
"""

FXRATE_DDL_OLD = """
CREATE TABLE fxrate (
    id INTEGER NOT NULL PRIMARY KEY,
    rate FLOAT NOT NULL
)
"""


def _build_old_shape_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(RECEIPTDOCUMENT_DDL_OLD)
        conn.execute(STATEMENTTRANSACTION_DDL_OLD)
        conn.execute(FXRATE_DDL_OLD)
        # The index this migration must preserve.
        conn.execute(
            "CREATE INDEX ix_statementtransaction_local_amount "
            "ON statementtransaction (local_amount)"
        )

        # 5 receipts: NULL, zero, large-realistic, 4-dp precision, large-with-fraction.
        conn.executemany(
            "INSERT INTO receiptdocument (id, extracted_local_amount) VALUES (?, ?)",
            [
                (1, None),
                (2, 0.0),
                (3, 12345.6789),
                (4, 419.5800),
                (5, 99999.9999),
            ],
        )

        # 5 transactions (mirror of the above for local_amount + usd_amount).
        conn.executemany(
            "INSERT INTO statementtransaction (id, local_amount, usd_amount) VALUES (?, ?, ?)",
            [
                (1, None, None),
                (2, 0.0, 0.0),
                (3, 12345.6789, 4.1234),
                (4, 419.5800, 50.0),
                (5, 99999.9999, 9999.9999),
            ],
        )

        # 3 fx rates with 8-dp precision edge cases.
        conn.executemany(
            "INSERT INTO fxrate (id, rate) VALUES (?, ?)",
            [
                (1, 0.00000001),
                (2, 1.23456789),
                (3, 12345678.12345678),
            ],
        )
        conn.commit()


def _column_declared_type(conn: sqlite3.Connection, table: str, column: str) -> str:
    for row in conn.execute(f"PRAGMA table_info({table})"):
        if row[1] == column:
            return row[2]
    raise AssertionError(f"column {table}.{column} not found")


def _index_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


@pytest.fixture
def old_shape_db(tmp_path: Path) -> Path:
    db = tmp_path / "m1_day25_test.db"
    _build_old_shape_db(db)
    return db


def test_apply_migrates_columns_to_numeric(old_shape_db: Path) -> None:
    migration.migrate(str(old_shape_db), apply=True)

    with sqlite3.connect(old_shape_db) as conn:
        # Declared types now NUMERIC(...). PRAGMA preserves the declared type.
        assert _column_declared_type(conn, "receiptdocument", "extracted_local_amount") == "NUMERIC(18,4)"
        assert _column_declared_type(conn, "statementtransaction", "local_amount") == "NUMERIC(18,4)"
        assert _column_declared_type(conn, "statementtransaction", "usd_amount") == "NUMERIC(18,4)"
        assert _column_declared_type(conn, "fxrate", "rate") == "NUMERIC(18,8)"


def test_apply_preserves_index_on_local_amount(old_shape_db: Path) -> None:
    migration.migrate(str(old_shape_db), apply=True)

    with sqlite3.connect(old_shape_db) as conn:
        assert _index_exists(conn, "ix_statementtransaction_local_amount")
        # And the index actually points at the renamed column.
        info = list(conn.execute("PRAGMA index_info(ix_statementtransaction_local_amount)"))
        assert len(info) == 1
        # PRAGMA index_info: (seqno, cid, name)
        assert info[0][2] == "local_amount"


def test_apply_preserves_null_and_value_round_trip(old_shape_db: Path) -> None:
    migration.migrate(str(old_shape_db), apply=True)

    with sqlite3.connect(old_shape_db) as conn:
        rows = dict(conn.execute(
            "SELECT id, extracted_local_amount FROM receiptdocument ORDER BY id"
        ))
        assert rows[1] is None
        assert rows[2] == 0
        # Values should round to 4 dp and read back as numeric (REAL via affinity).
        assert abs(rows[3] - 12345.6789) < 1e-6
        assert abs(rows[4] - 419.58) < 1e-6
        assert abs(rows[5] - 99999.9999) < 1e-6

        rate_rows = dict(conn.execute("SELECT id, rate FROM fxrate ORDER BY id"))
        assert abs(rate_rows[1] - 0.00000001) < 1e-12
        assert abs(rate_rows[2] - 1.23456789) < 1e-12
        # Large rate near float64 precision boundary.
        assert abs(rate_rows[3] - 12345678.12345678) < 1e-6


def test_dry_run_does_not_mutate_database(old_shape_db: Path) -> None:
    before_bytes = old_shape_db.read_bytes()
    before_mtime = old_shape_db.stat().st_mtime

    result = migration.migrate(str(old_shape_db), apply=False)

    assert result.dry_run is True
    assert result.backup_path is None
    assert result.log_path is None

    # Byte-for-byte unchanged.
    assert old_shape_db.read_bytes() == before_bytes
    assert old_shape_db.stat().st_mtime == before_mtime

    with sqlite3.connect(old_shape_db) as conn:
        # Column still FLOAT.
        assert _column_declared_type(
            conn, "receiptdocument", "extracted_local_amount"
        ) == "FLOAT"


def test_apply_is_idempotent(old_shape_db: Path) -> None:
    migration.migrate(str(old_shape_db), apply=True)
    # Second run should detect already-migrated state and skip everything.
    result = migration.migrate(str(old_shape_db), apply=True)
    assert result.already_migrated is True
    for col in result.columns:
        assert col.skipped, f"{col.table}.{col.column} should have been skipped"


def test_aggregate_sums_match_within_tolerance(old_shape_db: Path) -> None:
    result = migration.migrate(str(old_shape_db), apply=True)

    by_col = {(c.table, c.column): c for c in result.columns}

    rd = by_col[("receiptdocument", "extracted_local_amount")]
    # NULL + 0 + 12345.6789 + 419.58 + 99999.9999 = 112764.8588
    assert rd.sum_before is not None
    assert rd.sum_after is not None
    assert abs(rd.sum_before - rd.sum_after) < Decimal("0.0005")

    fx = by_col[("fxrate", "rate")]
    # 0.00000001 + 1.23456789 + 12345678.12345678
    assert fx.sum_before is not None
    assert fx.sum_after is not None
    assert abs(fx.sum_before - fx.sum_after) < Decimal("0.0001")


def test_refuses_protected_path(tmp_path: Path) -> None:
    # We don't actually have /var/lib/dcexpense in the test environment, but
    # the protected-path check happens before any filesystem read, so a path
    # string under the protected fragment is rejected even if it doesn't exist.
    with pytest.raises(SystemExit) as exc_info:
        migration.migrate("/var/lib/dcexpense/expense_app.db", apply=True)
    assert exc_info.value.code == 2

    with pytest.raises(SystemExit) as exc_info:
        migration.migrate("/opt/dcexpense/some_other.db", apply=False)
    assert exc_info.value.code == 2


def test_refuses_missing_db(tmp_path: Path) -> None:
    nonexistent = tmp_path / "definitely_not_here.db"
    with pytest.raises(SystemExit) as exc_info:
        migration.migrate(str(nonexistent), apply=True)
    assert exc_info.value.code == 2


def test_per_row_verification_catches_drift(tmp_path: Path, monkeypatch) -> None:
    """If ROUND produced drift > epsilon, the migration must abort and roll back.

    Seeds a value with extra precision (123.456789) so that ROUND(x, 4) =
    123.4568 — a real drift of ~1.1e-5. With a tightened epsilon below that
    value, the per-row check must fire.
    """
    db = tmp_path / "drift_test.db"
    _build_old_shape_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO receiptdocument (id, extracted_local_amount) VALUES (?, ?)",
            (99, 123.456789),
        )
        conn.commit()

    monkeypatch.setattr(
        migration,
        "COLUMNS",
        [("receiptdocument", "extracted_local_amount", "NUMERIC(18,4)", 4, "0.000001")],
    )

    with pytest.raises(SystemExit) as exc_info:
        migration.migrate(str(db), apply=True)
    assert exc_info.value.code == 3
    # Original column preserved (transaction rolled back).
    with sqlite3.connect(db) as conn:
        assert _column_declared_type(
            conn, "receiptdocument", "extracted_local_amount"
        ) == "FLOAT"
