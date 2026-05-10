"""F-AI-Stage1 sub-PR 8 phase 1: receipt attachment table + fatura_status column.

Adds the supporting-attachment surface used by the hotel-fatura follow-up:

  1. CREATE TABLE receiptattachment (FK → receiptdocument.id ON DELETE CASCADE)
     plus indexes used by the photo-handler dedup query.
  2. ALTER TABLE receiptdocument ADD COLUMN fatura_status (NULL on non-hotel
     receipts; 'pending' / 'attached' / 'not_available' on hotel receipts).
  3. Backfill: existing Hotel/Lodging/Laundry receipts that don't have a
     fatura_status get 'pending' so report-gen will warn until the user
     attaches or marks them not_available.

Forward-only and idempotent. A second run is a no-op:
  - ``SQLModel.metadata.create_all`` skips an existing table.
  - The ALTER guards on ``column_exists``.
  - The CREATE INDEX uses ``IF NOT EXISTS``.
  - The backfill UPDATE only touches NULL ``fatura_status`` rows.

Usage from repo root:
    cd backend && python migrations/002_f_ai_stage1_phase8_fatura_attachment.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

from app.db import engine  # noqa: E402
from app.models import ReceiptDocument  # noqa: E402  (table import for create_all metadata)
from app.models import ReceiptAttachment  # noqa: E402, F401  (registers the table)


HOTEL_BUCKET = "Hotel/Lodging/Laundry"


def _table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:t"),
        {"t": table_name},
    ).fetchone()
    return row is not None


def _column_exists(conn, table_name: str, column_name: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    return any(row[1] == column_name for row in rows)


def create_new_tables(target_engine=None) -> None:
    """SQLModel.metadata.create_all is idempotent — existing tables are
    skipped, the new ``receiptattachment`` is created."""
    if target_engine is None:
        target_engine = engine
    SQLModel.metadata.create_all(target_engine)


def add_fatura_status_column(target_engine=None) -> bool:
    """Returns True iff the column was added on this run."""
    if target_engine is None:
        target_engine = engine
    with target_engine.begin() as conn:
        if not _table_exists(conn, "receiptdocument"):
            return False
        if _column_exists(conn, "receiptdocument", "fatura_status"):
            return False
        conn.execute(
            text("ALTER TABLE receiptdocument ADD COLUMN fatura_status VARCHAR")
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_receiptdocument_fatura_status "
                "ON receiptdocument(fatura_status)"
            )
        )
    return True


def backfill_fatura_status_pending_on_hotel_receipts(target_engine=None) -> int:
    """Mark every existing Hotel/Lodging/Laundry receipt with
    ``fatura_status='pending'`` so report-gen surfaces a warning until
    the user resolves it. Non-hotel rows stay NULL.

    Returns count of updated rows.
    """
    if target_engine is None:
        target_engine = engine
    updated = 0
    with Session(target_engine) as session:
        rows = session.exec(
            select(ReceiptDocument).where(
                ReceiptDocument.report_bucket == HOTEL_BUCKET,
                ReceiptDocument.fatura_status.is_(None),  # type: ignore[union-attr]
            )
        ).all()
        for row in rows:
            row.fatura_status = "pending"
            session.add(row)
            updated += 1
        session.commit()
    return updated


if __name__ == "__main__":
    create_new_tables()
    column_added = add_fatura_status_column()
    backfilled = backfill_fatura_status_pending_on_hotel_receipts()
    print(
        "F-AI-Stage1 sub-PR 8 phase 1 migration: "
        f"column_added={column_added}, backfilled_hotel_receipts={backfilled}"
    )
