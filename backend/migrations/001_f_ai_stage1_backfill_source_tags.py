"""F-AI-Stage1: backfill source tags as 'legacy_unknown' on existing ReceiptDocument rows.

Idempotent. Safe to run multiple times. After this script, every ReceiptDocument
row has the four *_source columns populated. New rows will set them explicitly
in sub-PR 4 onwards.

Usage from repo root:
    cd backend && python migrations/001_f_ai_stage1_backfill_source_tags.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

from app.db import engine  # noqa: E402
from app.models import ReceiptDocument  # noqa: E402


NEW_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "receiptdocument": [
        ("category_source", "VARCHAR"),
        ("bucket_source", "VARCHAR"),
        ("business_reason_source", "VARCHAR"),
        ("attendees_source", "VARCHAR"),
    ],
    "agent_receipt_read": [
        ("suggested_business_or_personal", "VARCHAR"),
        ("suggested_report_bucket", "VARCHAR"),
        ("suggested_attendees_json", "TEXT"),
        ("suggested_customer", "VARCHAR"),
        ("suggested_business_reason", "TEXT"),
        ("suggested_confidence_overall", "REAL"),
    ],
    "agent_receipt_review_run": [
        ("context_window_json", "TEXT"),
    ],
}

NEW_INDEXES: list[tuple[str, str, str]] = [
    ("ix_receiptdocument_category_source", "receiptdocument", "category_source"),
    ("ix_receiptdocument_bucket_source", "receiptdocument", "bucket_source"),
    (
        "ix_receiptdocument_business_reason_source",
        "receiptdocument",
        "business_reason_source",
    ),
    ("ix_receiptdocument_attendees_source", "receiptdocument", "attendees_source"),
    (
        "ix_agent_receipt_read_suggested_business_or_personal",
        "agent_receipt_read",
        "suggested_business_or_personal",
    ),
    (
        "ix_agent_receipt_read_suggested_report_bucket",
        "agent_receipt_read",
        "suggested_report_bucket",
    ),
]


def _table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:table_name"),
        {"table_name": table_name},
    ).fetchone()
    return row is not None


def _column_exists(conn, table_name: str, column_name: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    return any(row[1] == column_name for row in rows)


def create_new_tables(target_engine=None) -> None:
    """Create tables that are entirely new to this schema layer."""
    if target_engine is None:
        target_engine = engine
    SQLModel.metadata.create_all(target_engine)


def add_columns_if_missing(target_engine=None) -> list[str]:
    """Add F-AI-Stage1 columns to existing tables if they are missing.

    Returns the fully-qualified column names added during this run.
    """
    if target_engine is None:
        target_engine = engine
    added: list[str] = []
    with target_engine.begin() as conn:
        for table_name, columns in NEW_COLUMNS.items():
            if not _table_exists(conn, table_name):
                continue
            for column_name, declared_type in columns:
                if _column_exists(conn, table_name, column_name):
                    continue
                conn.execute(
                    text(
                        f"ALTER TABLE {table_name} "
                        f"ADD COLUMN {column_name} {declared_type}"
                    )
                )
                added.append(f"{table_name}.{column_name}")

        for index_name, table_name, column_name in NEW_INDEXES:
            if not _table_exists(conn, table_name):
                continue
            if not _column_exists(conn, table_name, column_name):
                continue
            conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {index_name} "
                    f"ON {table_name}({column_name})"
                )
            )
    return added


def backfill_source_tags(target_engine=None) -> int:
    """Set *_source = 'legacy_unknown' on rows where any source tag is NULL.

    Returns count of updated rows.
    """
    if target_engine is None:
        target_engine = engine
    updated = 0
    with Session(target_engine) as session:
        rows = session.exec(
            select(ReceiptDocument).where(
                (ReceiptDocument.category_source.is_(None))
                | (ReceiptDocument.bucket_source.is_(None))
                | (ReceiptDocument.business_reason_source.is_(None))
                | (ReceiptDocument.attendees_source.is_(None))
            )
        ).all()
        for row in rows:
            row.category_source = row.category_source or "legacy_unknown"
            row.bucket_source = row.bucket_source or "legacy_unknown"
            row.business_reason_source = (
                row.business_reason_source or "legacy_unknown"
            )
            row.attendees_source = row.attendees_source or "legacy_unknown"
            session.add(row)
            updated += 1
        session.commit()
    return updated


if __name__ == "__main__":
    create_new_tables()
    add_columns_if_missing()
    n = backfill_source_tags()
    print(f"Backfilled source tags on {n} ReceiptDocument rows.")
