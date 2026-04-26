"""Cross-write invariant: column value == latest event value.

The load-bearing detector for "someone wrote a tracked column without
going through the wrapper." Day 3a runs it post-backfill against a
synthetic 13-receipt DB; Day 3b promotes the same logic to a per-test
fixture so the invariant is enforced across the entire suite once
merge logic starts emitting events.

For each (receipt, tracked_field) pair:
  - If the column is NULL → ``get_current_event(...)`` returns None
    (no backfill event was written for a NULL column).
  - If the column is non-NULL → ``get_current_event(...)`` returns the
    backfill event, and the event's ``.value`` (deserialized to the
    column's native type) equals the column value.

Decimal comparison uses Decimal == Decimal (no float traps). Date
comparison parses the event's ISO-8601 string back to date.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.provenance_enums import EntityType, FieldName  # noqa: E402
from app.services.field_provenance import get_current_event  # noqa: E402
from migrations import m1_day3a_field_provenance as migration  # noqa: E402


# Production-shape receiptdocument schema (post M1 Day 2.5).
APPUSER_DDL = """
CREATE TABLE appuser (
    id INTEGER NOT NULL PRIMARY KEY,
    display_name VARCHAR
)
"""

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

NOW_ISO = datetime.now(timezone.utc).isoformat(timespec="seconds")

# 13 receipts spanning the realistic mix of pipeline stages — some are
# raw extractions (date/supplier/amount/currency only), some are
# operator-confirmed (categorical metadata), some have all 9 tracked
# fields populated. Every tracked field appears non-NULL on at least
# one receipt AND NULL on at least one — so the invariant test exercises
# both branches for every field.
SEED_ROWS = [
    # (id, amount, currency, date, supplier, receipt_type, b_or_p,
    #  report_bucket, business_reason, attendees)
    (1, "419.5800", "TRY", "2026-04-01", "Migros", "itemized", "Business",
     "Meals/Snacks", "Customer dinner", "Alice; Bob"),
    (2, "1250.0000", "TRY", "2026-04-02", "A101", "payment_receipt", "Personal",
     None, None, None),
    (3, "75.5000",   "USD", "2026-04-03", "Starbucks", "itemized", "Business",
     "Meals/Snacks", "Project meeting", "Charlie"),
    (4, None, None, None, None, None, None, None, None, None),  # skeleton
    (5, "3500.0000", "TRY", "2026-04-04", "Hyatt Istanbul", "invoice", "Business",
     "Hotel/Lodging/Laundry", "Customer visit", "—"),
    (6, "89.9900",   "TRY", "2026-04-05", "Sok Market", None, None,
     None, None, None),  # mid-extraction
    (7, "150.0000",  "USD", "2026-04-06", "Uber", "payment_receipt", "Business",
     "Taxi/Parking/Tolls/Uber", "Airport transfer", None),
    (8, None, None, None, "Unknown supplier", None, None, None, None, None),
    (9, "12.5000",   "USD", "2026-04-07", "Wifi at hotel", "confirmation", "Business",
     "Telephone/Internet", "Working remote", None),
    (10, "725.0000", "TRY", None, None, None, None, None, None, None),
    (11, "15.0000",  "USD", "2026-04-08", "Amazon", "invoice", "Business",
     "Admin Supplies", "Office supplies", None),
    (12, "1899.5000", "TRY", "2026-04-09", "Pegasus Airlines", "confirmation",
     "Business", "Airfare/Bus/Ferry/Other", "Trip to Ankara", None),
    (13, "250.0000", "TRY", "2026-04-10", "Petrol station", "payment_receipt",
     "Business", "Auto Gasoline", "Travel between sites", None),
]

assert len(SEED_ROWS) == 13


def _build_db(path: Path) -> None:
    """Build the synthetic DB and seed 13 receipts."""
    with sqlite3.connect(path) as conn:
        conn.execute(APPUSER_DDL)
        conn.execute(RECEIPTDOCUMENT_DDL)
        conn.executemany(
            "INSERT INTO receiptdocument "
            "(id, extracted_local_amount, extracted_currency, extracted_date, "
            "extracted_supplier, receipt_type, business_or_personal, "
            "report_bucket, business_reason, attendees, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(*row, NOW_ISO) for row in SEED_ROWS],
        )
        conn.commit()


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_db(tmp_path: Path) -> Path:
    """Build the synthetic 13-receipt DB and run the Day 3a migration."""
    db = tmp_path / "m1_day3a_invariant.db"
    _build_db(db)
    migration.migrate(str(db), apply=True)
    return db


# ---------------------------------------------------------------------------
# deserialization for cross-type comparison
# ---------------------------------------------------------------------------


def _deserialize_event_value(field_name: str, raw: str) -> Any:
    """Convert a value TEXT to the column's native type for comparison."""
    if field_name == "extracted_local_amount":
        return Decimal(raw)
    if field_name == "extracted_date":
        return date.fromisoformat(raw)
    return raw  # all other tracked fields are strings


def _normalize_column_value(field_name: str, raw: Any) -> Any:
    """Convert a raw sqlite3 column read to the same native type the event
    deserializer produces.

    SQLite returns NUMERIC columns as float, DATE columns as str, and
    everything else as str. This normalizes them so == comparison with
    the event-deserialized value is exact (no float traps for Decimal).
    """
    if raw is None:
        return None
    if field_name == "extracted_local_amount":
        # Raw sqlite3 returns NUMERIC as float (REAL affinity). Round-trip
        # through Decimal-via-str so the comparison is exact at 4 dp
        # (matches the migration's quantize-on-serialize behavior).
        if isinstance(raw, Decimal):
            return raw.quantize(Decimal("0.0001"))
        return Decimal(str(raw)).quantize(Decimal("0.0001"))
    if field_name == "extracted_date":
        if isinstance(raw, date):
            return raw
        return date.fromisoformat(str(raw))
    return str(raw)


# ---------------------------------------------------------------------------
# the load-bearing test
# ---------------------------------------------------------------------------


def test_invariant_column_equals_latest_event(migrated_db: Path) -> None:
    """For every (receipt, tracked field) pair: the column value equals
    get_current_event(...).value (deserialized to native type), or both
    are None.

    Single-pass integration test for Day 3a. Day 3b will promote this to
    a per-test fixture so any code path that writes a tracked column
    outside the wrapper is detected immediately.
    """
    engine = create_engine(f"sqlite:///{migrated_db.as_posix()}")

    # Read all 13 receipts as a baseline. Iterate via raw sqlite3 so the
    # test doesn't depend on SQLModel reading our hand-written DDL.
    cols = ", ".join(c for c, _ in migration.TRACKED_FIELDS)
    with sqlite3.connect(migrated_db) as conn:
        rows = conn.execute(
            f"SELECT id, {cols} FROM receiptdocument ORDER BY id"
        ).fetchall()
    assert len(rows) == 13, f"expected 13 receipts, got {len(rows)}"

    failures: list[str] = []
    null_pairs = 0
    matched_pairs = 0

    with Session(engine) as session:
        for row in rows:
            receipt_id = row[0]
            column_values = row[1:]
            for (column, field_name), raw_value in zip(
                migration.TRACKED_FIELDS, column_values
            ):
                event = get_current_event(
                    session,
                    entity_type=EntityType.RECEIPT,
                    entity_id=receipt_id,
                    field_name=FieldName(field_name),
                )

                if raw_value is None:
                    # NULL column ⇒ no backfill event written for this field
                    if event is not None:
                        failures.append(
                            f"receipt id={receipt_id} field={field_name}: "
                            f"column is NULL but event exists "
                            f"(value={event.value!r})"
                        )
                    else:
                        null_pairs += 1
                    continue

                # Non-NULL column ⇒ event must exist and deserialize to equal value
                if event is None:
                    failures.append(
                        f"receipt id={receipt_id} field={field_name}: "
                        f"column={raw_value!r} but no current event found"
                    )
                    continue

                expected = _normalize_column_value(field_name, raw_value)
                actual = _deserialize_event_value(field_name, event.value)

                if expected != actual:
                    failures.append(
                        f"receipt id={receipt_id} field={field_name}: "
                        f"column={expected!r} != event.value={actual!r} "
                        f"(raw event.value={event.value!r})"
                    )
                else:
                    matched_pairs += 1

    if failures:
        joined = "\n  ".join(failures)
        pytest.fail(
            f"INVARIANT VIOLATIONS: {len(failures)} (receipt, field) pairs "
            f"have column ⇆ event mismatch\n  {joined}"
        )

    # Sanity: every (receipt, tracked-field) pair was visited; every
    # non-NULL pair matched; every NULL pair returned None.
    total = 13 * len(migration.TRACKED_FIELDS)
    assert matched_pairs + null_pairs == total, (
        f"visited {matched_pairs + null_pairs} pairs but expected {total}"
    )
    # Sanity: at least some non-NULL pairs (the test data has them).
    assert matched_pairs > 0, "no non-NULL pairs verified — test data may be wrong"
    assert null_pairs > 0, "no NULL pairs verified — test data may be wrong"


def test_invariant_test_data_exercises_every_tracked_field(migrated_db: Path) -> None:
    """Sanity meta-test: the SEED_ROWS data populates AT LEAST ONE non-NULL
    AND AT LEAST ONE NULL value for every tracked field. Without this, the
    invariant test could miss a field-specific bug because it never saw
    that field in either branch.
    """
    cols = ", ".join(c for c, _ in migration.TRACKED_FIELDS)
    with sqlite3.connect(migrated_db) as conn:
        rows = conn.execute(
            f"SELECT {cols} FROM receiptdocument"
        ).fetchall()

    for idx, (column, field_name) in enumerate(migration.TRACKED_FIELDS):
        values = [row[idx] for row in rows]
        non_null = [v for v in values if v is not None]
        nulls = [v for v in values if v is None]
        assert non_null, f"field {field_name} has zero non-NULL values in SEED_ROWS"
        assert nulls, f"field {field_name} has zero NULL values in SEED_ROWS"
