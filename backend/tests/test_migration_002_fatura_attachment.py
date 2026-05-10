"""F-AI-Stage1 sub-PR 8 phase 1: migration script behavior tests.

Pin the idempotency contract on
``backend/migrations/002_f_ai_stage1_phase8_fatura_attachment.py``: a
forward-only run-once migration must remain a no-op on subsequent runs
even if the schema is partially advanced (column already added, table
already exists, etc.). Also pin the backfill rule that pre-existing
Hotel/Lodging/Laundry receipts get ``fatura_status='pending'`` while
non-hotel rows stay NULL.
"""
from __future__ import annotations

import importlib.util
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlmodel import Session, SQLModel, select

from app.models import ReceiptAttachment, ReceiptDocument


def _load_migration():
    """Import the migration script as a module without executing
    ``__main__`` against the live engine."""
    here = Path(__file__).resolve().parent.parent / "migrations"
    spec = importlib.util.spec_from_file_location(
        "m002_fatura", here / "002_f_ai_stage1_phase8_fatura_attachment.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fresh_engine():
    """In-memory SQLite engine with the full SQLModel.metadata applied —
    mirrors the test isolated_db fixture."""
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    return engine


def _seed_receipt(
    session: Session,
    *,
    bucket: str | None,
    fatura_status: str | None = None,
    supplier: str = "Test Supplier",
) -> int:
    receipt = ReceiptDocument(
        source="telegram",
        status="received",
        content_type="photo",
        extracted_supplier=supplier,
        extracted_date=date(2026, 5, 1),
        extracted_local_amount=Decimal("100.00"),
        extracted_currency="TRY",
        report_bucket=bucket,
        fatura_status=fatura_status,
    )
    session.add(receipt)
    session.commit()
    session.refresh(receipt)
    return receipt.id  # type: ignore[return-value]


def test_migration_creates_receiptattachment_table():
    """SQLModel.metadata.create_all is what creates the new table — the
    migration's create_new_tables wraps that. After a run, the table
    exists and accepts an insert."""
    engine = create_engine("sqlite:///:memory:")
    # Start from a bare SQLite — no tables.
    m = _load_migration()
    m.create_new_tables(engine)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='receiptattachment'"
            )
        ).fetchone()
    assert rows is not None


def test_migration_add_fatura_status_column_idempotent():
    """The ALTER TABLE ADD COLUMN must be guarded so a re-run is a no-op."""
    engine = _fresh_engine()
    # The model already declares fatura_status, so SQLModel.metadata.create_all
    # has it. Drop the column to simulate a pre-PR-8 schema, then re-add via
    # the migration helper.
    m = _load_migration()
    # First run: column already exists from create_all → migration returns False.
    assert m.add_fatura_status_column(engine) is False
    # Re-run: still False (idempotent).
    assert m.add_fatura_status_column(engine) is False


def test_migration_add_fatura_status_column_on_pre_pr8_schema():
    """Simulate a pre-PR-8 receiptdocument (no fatura_status column) and
    confirm the migration adds it on first run, no-ops on second."""
    engine = create_engine("sqlite:///:memory:")
    # Build a stripped receiptdocument table that lacks fatura_status.
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE receiptdocument (
                id INTEGER PRIMARY KEY,
                source VARCHAR,
                status VARCHAR,
                content_type VARCHAR,
                report_bucket VARCHAR
            )
        """))
    m = _load_migration()
    assert m.add_fatura_status_column(engine) is True
    # Now the column exists — re-run is a no-op.
    assert m.add_fatura_status_column(engine) is False
    with engine.connect() as conn:
        cols = conn.execute(text("PRAGMA table_info(receiptdocument)")).fetchall()
    column_names = {row[1] for row in cols}
    assert "fatura_status" in column_names


def test_migration_backfill_marks_existing_hotel_receipts_pending():
    """Existing rows with bucket='Hotel/Lodging/Laundry' AND
    fatura_status NULL get 'pending'. Non-hotel rows untouched."""
    engine = _fresh_engine()
    with Session(engine) as session:
        hotel_id = _seed_receipt(session, bucket="Hotel/Lodging/Laundry")
        taxi_id = _seed_receipt(session, bucket="Taxi/Parking/Tolls/Uber")
        meal_id = _seed_receipt(session, bucket="Meals/Snacks")

    m = _load_migration()
    n = m.backfill_fatura_status_pending_on_hotel_receipts(engine)
    assert n == 1

    with Session(engine) as session:
        assert session.get(ReceiptDocument, hotel_id).fatura_status == "pending"
        assert session.get(ReceiptDocument, taxi_id).fatura_status is None
        assert session.get(ReceiptDocument, meal_id).fatura_status is None


def test_migration_backfill_skips_already_resolved_hotel():
    """If a hotel receipt already carries a non-NULL fatura_status (e.g.
    a manual 'attached' entry), the backfill must not overwrite it."""
    engine = _fresh_engine()
    with Session(engine) as session:
        attached_id = _seed_receipt(
            session, bucket="Hotel/Lodging/Laundry", fatura_status="attached"
        )
        notavail_id = _seed_receipt(
            session, bucket="Hotel/Lodging/Laundry", fatura_status="not_available"
        )
        fresh_id = _seed_receipt(
            session, bucket="Hotel/Lodging/Laundry", fatura_status=None
        )

    m = _load_migration()
    n = m.backfill_fatura_status_pending_on_hotel_receipts(engine)
    assert n == 1  # only the fresh row was touched

    with Session(engine) as session:
        assert session.get(ReceiptDocument, attached_id).fatura_status == "attached"
        assert session.get(ReceiptDocument, notavail_id).fatura_status == "not_available"
        assert session.get(ReceiptDocument, fresh_id).fatura_status == "pending"


def test_migration_backfill_idempotent_second_run_is_noop():
    engine = _fresh_engine()
    with Session(engine) as session:
        _seed_receipt(session, bucket="Hotel/Lodging/Laundry")

    m = _load_migration()
    first = m.backfill_fatura_status_pending_on_hotel_receipts(engine)
    second = m.backfill_fatura_status_pending_on_hotel_receipts(engine)
    assert first == 1
    assert second == 0


def test_migration_full_run_end_to_end_idempotent():
    """create + alter + backfill all in sequence, then re-run end-to-end
    and confirm nothing changes on the second pass."""
    engine = _fresh_engine()
    with Session(engine) as session:
        _seed_receipt(session, bucket="Hotel/Lodging/Laundry")
        _seed_receipt(session, bucket="Auto Gasoline")

    m = _load_migration()
    m.create_new_tables(engine)
    m.add_fatura_status_column(engine)
    n1 = m.backfill_fatura_status_pending_on_hotel_receipts(engine)
    assert n1 == 1

    # Insert a fatura attachment so we can verify the receiptattachment
    # table survived the second run.
    with Session(engine) as session:
        receipt = session.exec(
            select(ReceiptDocument).where(
                ReceiptDocument.report_bucket == "Hotel/Lodging/Laundry"
            )
        ).first()
        attachment = ReceiptAttachment(
            receipt_document_id=receipt.id,
            kind="fatura",
            source="telegram_photo",
            storage_path="/tmp/x.jpg",
        )
        session.add(attachment)
        session.commit()
        attachment_id = attachment.id

    # Second full run — must be a no-op end-to-end.
    m.create_new_tables(engine)
    m.add_fatura_status_column(engine)
    n2 = m.backfill_fatura_status_pending_on_hotel_receipts(engine)
    assert n2 == 0

    with Session(engine) as session:
        survivor = session.get(ReceiptAttachment, attachment_id)
    assert survivor is not None
