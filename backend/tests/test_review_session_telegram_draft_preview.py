"""Review-row Telegram draft preview removal tests.

The review queue should no longer emit ``source.telegram_draft``. These tests
pin the absence on rows that previously produced preview payloads.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlmodel import Session

from app.db import engine
from app.models import (
    AppUser,
    MatchDecision,
    ReceiptDocument,
    StatementImport,
    StatementTransaction,
)
from app.services.review_sessions import get_or_create_review_session, session_payload
from _pivot_helpers import ensure_expense_report_for_statement


def _seed_matched(
    session: Session,
    *,
    receipt_amount: Decimal = Decimal("12.34"),
    statement_amount: Decimal | None = None,
    business_reason: str | None = "Project meeting",
) -> int:
    user = AppUser(display_name="telegram-draft-removal-test")
    session.add(user)
    session.flush()

    statement = StatementImport(
        source_filename="telegram-draft-removal.xlsx",
        row_count=1,
        uploader_user_id=user.id,
    )
    session.add(statement)
    session.flush()

    tx = StatementTransaction(
        statement_import_id=statement.id,
        transaction_date=date(2026, 4, 30),
        supplier_raw="Smoke Cafe",
        supplier_normalized="SMOKE CAFE",
        local_currency="USD",
        local_amount=statement_amount if statement_amount is not None else receipt_amount,
        usd_amount=statement_amount if statement_amount is not None else receipt_amount,
        source_row_ref="row-1",
    )
    receipt = ReceiptDocument(
        uploader_user_id=user.id,
        source="test",
        status="imported",
        content_type="photo",
        original_file_name="r.jpg",
        extracted_date=date(2026, 4, 30),
        extracted_supplier="Smoke Cafe",
        extracted_local_amount=receipt_amount,
        extracted_currency="USD",
        business_or_personal="Business",
        report_bucket="Hotel/Lodging/Laundry",
        business_reason=business_reason,
        attendees="Hakan",
        needs_clarification=False,
    )
    session.add(tx)
    session.add(receipt)
    session.commit()
    session.refresh(statement)
    session.refresh(tx)
    session.refresh(receipt)

    decision = MatchDecision(
        statement_transaction_id=tx.id,
        receipt_document_id=receipt.id,
        confidence="high",
        match_method="test",
        approved=True,
        reason="telegram draft removal fixture",
    )
    session.add(decision)
    session.commit()
    return statement.id or 0


def _row_payload(session: Session, statement_id: int) -> dict:
    expense_report_id = ensure_expense_report_for_statement(session, statement_id)
    review = get_or_create_review_session(session, expense_report_id=expense_report_id)
    rows = session_payload(session, review)["rows"]
    assert len(rows) == 1
    return rows[0]


def test_amount_mismatch_row_omits_telegram_draft_preview() -> None:
    with Session(engine) as session:
        statement_id = _seed_matched(
            session,
            receipt_amount=Decimal("10.00"),
            statement_amount=Decimal("999.99"),
        )

        row = _row_payload(session, statement_id)

        assert row["source"]["match"].get("receipt_statement_issues")
        assert "telegram_draft" not in row["source"]


def test_missing_business_reason_row_omits_telegram_draft_preview() -> None:
    with Session(engine) as session:
        statement_id = _seed_matched(session, business_reason=None)

        row = _row_payload(session, statement_id)

        assert row["confirmed"]["business_reason"] is None
        assert "telegram_draft" not in row["source"]
