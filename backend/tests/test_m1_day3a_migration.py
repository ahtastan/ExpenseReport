"""Tests for the M1 Day 3a FieldProvenanceEvent migration script.

Builds a synthetic SQLite DB with the production-shape receiptdocument
schema, seeds 5 receipts that exercise the backfill edge cases (NULL
columns, all-tracked-fields-set, money-only, categorical-only), runs
the migration, and asserts the post-state.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from migrations import m1_day3a_field_provenance as migration  # noqa: E402


# Pre-migration receiptdocument schema (post M1 Day 2.5; FieldProvenanceEvent
# does NOT exist yet).
RECEIPTDOCUMENT_DDL = """
CREATE TABLE receiptdocument (
    id INTEGER NOT NULL PRIMARY KEY,
    extracted_local_amount NUMERIC(18,4),
    extracted_currency VARCHAR,
    extracted_date DATE,
    extracted_supplier VARCHAR,
    receipt_type VARCHAR(50),
    business_or_personal VARCHAR,
    report_bucket VARCHAR,
    business_reason VARCHAR,
    attendees VARCHAR,
    created_at DATETIME NOT NULL
)
"""

# Stub appuser table so the foreign key in fieldprovenanceevent is valid.
APPUSER_DDL = """
CREATE TABLE appuser (
    id INTEGER NOT NULL PRIMARY KEY,
    display_name VARCHAR
)
"""

NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")

# 5 receipts exercising the backfill-relevant edge cases:
#   id=1: all 9 tracked fields populated
#   id=2: only money + currency (mid-extraction state)
#   id=3: all NULL (skeleton receipt, no backfill events)
#   id=4: categorical only (no extracted_local_amount)
#   id=5: money only (just extracted_local_amount)
SEED_RECEIPTS = [
    # (id, extracted_local_amount, extracted_currency, extracted_date,
    #  extracted_supplier, receipt_type, business_or_personal,
    #  report_bucket, business_reason, attendees, created_at)
    (1, "419.5800", "TRY", "2026-04-01", "Migros", "itemized", "Business",
     "Meals/Snacks", "Customer dinner", "Alice; Bob", NOW),
    (2, "1250.0000", "TRY", None, None, None, None, None, None, None, NOW),
    (3, None, None, None, None, None, None, None, None, None, NOW),
    (4, None, None, None, "Hotel", "payment_receipt", "Business",
     "Hotel/Lodging/Laundry", None, None, NOW),
    (5, "75.5000", None, None, None, None, None, None, None, None, NOW),
]


def _build_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(APPUSER_DDL)
        conn.execute(RECEIPTDOCUMENT_DDL)
        conn.executemany(
            "INSERT INTO receiptdocument "
            "(id, extracted_local_amount, extracted_currency, extracted_date, "
            "extracted_supplier, receipt_type, business_or_personal, "
            "report_bucket, business_reason, attendees, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            SEED_RECEIPTS,
        )
        conn.commit()


def _expected_event_count() -> int:
    """Count non-NULL tracked-field values across SEED_RECEIPTS."""
    n = 0
    # SEED_RECEIPTS columns 1..9 are the tracked fields (in TRACKED_FIELDS order).
    for row in SEED_RECEIPTS:
        for value in row[1:10]:
            if value is not None:
                n += 1
    return n


@pytest.fixture
def synthetic_db(tmp_path: Path) -> Path:
    db = tmp_path / "m1_day3a_test.db"
    _build_db(db)
    return db


# ---------------------------------------------------------------------------
# DDL + index creation
# ---------------------------------------------------------------------------


def test_apply_creates_table_and_indexes(synthetic_db: Path) -> None:
    migration.migrate(str(synthetic_db), apply=True)

    with sqlite3.connect(synthetic_db) as conn:
        # Table exists with all 14 expected columns + id.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(fieldprovenanceevent)")}
        expected = {
            "id", "entity_type", "entity_id", "field_name", "event_type", "source",
            "value", "value_decimal", "confidence", "decision_group_id",
            "actor_type", "actor_user_id", "actor_label", "metadata_json",
            "created_at",
        }
        assert cols == expected

        # All 8 expected indexes present.
        idx = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='fieldprovenanceevent'"
            )
        }
        for name in migration.EXPECTED_INDEXES:
            assert name in idx, f"missing index {name}"


# ---------------------------------------------------------------------------
# backfill correctness
# ---------------------------------------------------------------------------


def test_apply_backfills_one_event_per_non_null_tracked_field(synthetic_db: Path) -> None:
    result = migration.migrate(str(synthetic_db), apply=True)

    expected = _expected_event_count()
    assert result.backfilled_events == expected

    with sqlite3.connect(synthetic_db) as conn:
        actual = conn.execute(
            "SELECT COUNT(*) FROM fieldprovenanceevent WHERE source = ?",
            (migration.LEGACY_SOURCE,),
        ).fetchone()[0]
        assert actual == expected


def test_apply_does_not_backfill_null_columns(synthetic_db: Path) -> None:
    """Receipt id=3 has all NULL tracked fields → 0 events for that receipt."""
    migration.migrate(str(synthetic_db), apply=True)

    with sqlite3.connect(synthetic_db) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM fieldprovenanceevent WHERE entity_id = 3"
        ).fetchone()[0]
        assert n == 0

        # Receipt id=5 has only extracted_local_amount → exactly 1 event.
        n5 = conn.execute(
            "SELECT COUNT(*) FROM fieldprovenanceevent WHERE entity_id = 5"
        ).fetchone()[0]
        assert n5 == 1


def test_apply_preserves_original_created_at_in_metadata_json(synthetic_db: Path) -> None:
    migration.migrate(str(synthetic_db), apply=True)

    with sqlite3.connect(synthetic_db) as conn:
        row = conn.execute(
            "SELECT metadata_json FROM fieldprovenanceevent WHERE entity_id = 1 LIMIT 1"
        ).fetchone()
        assert row is not None
        meta = json.loads(row[0])
        assert meta["original_created_at"] == NOW
        assert meta["backfill_reason"] == migration.BACKFILL_REASON


def test_apply_uses_legacy_unknown_current_source_and_system_migration_actor(
    synthetic_db: Path,
) -> None:
    migration.migrate(str(synthetic_db), apply=True)

    with sqlite3.connect(synthetic_db) as conn:
        rows = conn.execute(
            "SELECT source, event_type, actor_type, actor_user_id, actor_label "
            "FROM fieldprovenanceevent"
        ).fetchall()
        assert rows, "no events written"
        for source, event_type, actor_type, actor_user_id, actor_label in rows:
            assert source == "legacy_unknown_current"
            assert event_type == "accepted"
            assert actor_type == "system_migration"
            assert actor_user_id is None
            assert actor_label == "system:m1-day3a-backfill"


def test_apply_money_field_populates_value_decimal(synthetic_db: Path) -> None:
    """extracted_local_amount goes to BOTH value (TEXT) and value_decimal."""
    migration.migrate(str(synthetic_db), apply=True)

    with sqlite3.connect(synthetic_db) as conn:
        row = conn.execute(
            "SELECT value, value_decimal FROM fieldprovenanceevent "
            "WHERE entity_id = 1 AND field_name = 'extracted_local_amount'"
        ).fetchone()
        assert row is not None
        value, value_decimal = row
        assert value == "419.5800"
        # SQLite returns NUMERIC as float by default through raw sqlite3;
        # value compares equal numerically.
        assert float(value_decimal) == 419.58


def test_apply_categorical_field_leaves_value_decimal_null(synthetic_db: Path) -> None:
    migration.migrate(str(synthetic_db), apply=True)

    with sqlite3.connect(synthetic_db) as conn:
        row = conn.execute(
            "SELECT value, value_decimal FROM fieldprovenanceevent "
            "WHERE entity_id = 1 AND field_name = 'extracted_supplier'"
        ).fetchone()
        assert row is not None
        value, value_decimal = row
        assert value == "Migros"
        assert value_decimal is None


def test_apply_per_receipt_decision_group_id_is_shared(synthetic_db: Path) -> None:
    """All events for one receipt share one decision_group_id; receipts differ."""
    migration.migrate(str(synthetic_db), apply=True)

    with sqlite3.connect(synthetic_db) as conn:
        groups_per_receipt: dict[int, set[str]] = {}
        for receipt_id, group_id in conn.execute(
            "SELECT entity_id, decision_group_id FROM fieldprovenanceevent ORDER BY entity_id"
        ):
            groups_per_receipt.setdefault(receipt_id, set()).add(group_id)

        # Each receipt has exactly one decision_group_id.
        for receipt_id, groups in groups_per_receipt.items():
            assert len(groups) == 1, (
                f"receipt id={receipt_id} has {len(groups)} decision_group_ids; "
                f"expected 1 shared across all its backfill events"
            )

        # Different receipts have different decision_group_ids.
        all_groups = {g for groups in groups_per_receipt.values() for g in groups}
        assert len(all_groups) == len(groups_per_receipt)


# ---------------------------------------------------------------------------
# idempotency + dry-run + protected paths
# ---------------------------------------------------------------------------


def test_apply_is_idempotent(synthetic_db: Path) -> None:
    migration.migrate(str(synthetic_db), apply=True)
    result = migration.migrate(str(synthetic_db), apply=True)
    assert result.already_migrated is True
    assert result.backfilled_events == _expected_event_count()


def test_dry_run_does_not_mutate_database(synthetic_db: Path) -> None:
    before_bytes = synthetic_db.read_bytes()
    before_mtime = synthetic_db.stat().st_mtime

    result = migration.migrate(str(synthetic_db), apply=False)

    assert result.dry_run is True
    assert result.backup_path is None
    assert result.log_path is None
    assert synthetic_db.read_bytes() == before_bytes
    assert synthetic_db.stat().st_mtime == before_mtime

    with sqlite3.connect(synthetic_db) as conn:
        # Table should NOT exist after dry-run.
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fieldprovenanceevent'"
        ).fetchone()
        assert row is None


def test_dry_run_projects_correct_event_count(synthetic_db: Path) -> None:
    result = migration.migrate(str(synthetic_db), apply=False)
    assert result.backfilled_events == _expected_event_count()
    # 4 receipts have at least one non-NULL field (id=1, 2, 4, 5).
    assert result.receipts_with_events == 4


def test_refuses_protected_path() -> None:
    with pytest.raises(SystemExit) as exc_info:
        migration.migrate("/var/lib/dcexpense/expense_app.db", apply=True)
    assert exc_info.value.code == 2

    with pytest.raises(SystemExit) as exc_info:
        migration.migrate("/opt/dcexpense/some.db", apply=False)
    assert exc_info.value.code == 2


def test_refuses_missing_db(tmp_path: Path) -> None:
    nonexistent = tmp_path / "absent.db"
    with pytest.raises(SystemExit) as exc_info:
        migration.migrate(str(nonexistent), apply=True)
    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# exit invariant
# ---------------------------------------------------------------------------


def test_exit_invariant_holds(synthetic_db: Path) -> None:
    """After backfill, every receipt with a non-NULL tracked field has its
    matching legacy_unknown_current event."""
    migration.migrate(str(synthetic_db), apply=True)

    with sqlite3.connect(synthetic_db) as conn:
        violations = migration._exit_invariant_violations(conn)
        assert violations == [], f"exit invariant violated: {violations}"


def test_money_field_names_match_app_money_fields() -> None:
    """The migration script duplicates MONEY_FIELDS membership locally to
    stay standalone. This test catches drift forever — if either set is
    edited without the other, the test fails with a pointer to both.
    """
    from app.provenance_enums import MONEY_FIELDS
    from migrations.m1_day3a_field_provenance import MONEY_FIELD_NAMES

    app_set = {f.value for f in MONEY_FIELDS}
    assert app_set == MONEY_FIELD_NAMES, (
        f"Migration script MONEY_FIELD_NAMES ({MONEY_FIELD_NAMES}) "
        f"diverged from app.provenance_enums.MONEY_FIELDS ({app_set}). "
        "Update both."
    )


def test_partial_state_refuses_re_run(synthetic_db: Path) -> None:
    """If the table exists but the invariant doesn't hold, refuse with exit 2.

    Simulated by creating the table empty and trying to migrate — the
    pre-state has 18 non-null values but 0 backfill events ⇒ partial state.
    """
    # Apply once successfully.
    migration.migrate(str(synthetic_db), apply=True)

    # Wipe the events but leave the table.
    with sqlite3.connect(synthetic_db) as conn:
        conn.execute("DELETE FROM fieldprovenanceevent")
        conn.commit()

    with pytest.raises(SystemExit) as exc_info:
        migration.migrate(str(synthetic_db), apply=True)
    assert exc_info.value.code == 2
