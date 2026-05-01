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

from sqlmodel import Session, select  # noqa: E402

from app.db import engine  # noqa: E402
from app.models import ReceiptDocument  # noqa: E402


def backfill_source_tags() -> int:
    """Set *_source = 'legacy_unknown' on rows where any source tag is NULL.

    Returns count of updated rows.
    """
    updated = 0
    with Session(engine) as session:
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
    n = backfill_source_tags()
    print(f"Backfilled source tags on {n} ReceiptDocument rows.")
