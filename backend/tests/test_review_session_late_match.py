import json
import os
from datetime import date
from pathlib import Path
from uuid import uuid4

VERIFY_ROOT = Path.cwd() / ".verify_data"
VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{VERIFY_ROOT / f'review_late_match_{uuid4().hex}.db'}"
os.environ["EXPENSE_STORAGE_ROOT"] = str(VERIFY_ROOT)
os.environ.pop("ANTHROPIC_API_KEY", None)

from sqlmodel import Session  # noqa: E402

from app.db import create_db_and_tables, engine  # noqa: E402
from app.models import (  # noqa: E402
    MatchDecision,
    ReceiptDocument,
    StatementImport,
    StatementTransaction,
)
from app.services.review_sessions import (  # noqa: E402
    get_or_create_review_session,
    review_rows,
    update_review_row,
)


def _seed(session: Session) -> tuple[int, int, int]:
    statement = StatementImport(source_filename="late-match.xlsx", row_count=1)
    session.add(statement)
    session.commit()
    session.refresh(statement)

    tx = StatementTransaction(
        statement_import_id=statement.id,
        transaction_date=date(2026, 4, 20),
        supplier_raw="Migros",
        supplier_normalized="MIGROS",
        local_currency="TRY",
        local_amount=419.58,
    )
    receipt = ReceiptDocument(
        source="test",
        status="imported",
        content_type="photo",
        original_file_name="migros.jpg",
        extracted_date=date(2026, 4, 20),
        extracted_supplier="Migros",
        extracted_local_amount=419.58,
        extracted_currency="TRY",
        business_or_personal="Business",
        report_bucket="Business",
        needs_clarification=False,
    )
    session.add(tx)
    session.add(receipt)
    session.commit()
    session.refresh(tx)
    session.refresh(receipt)
    return statement.id, tx.id, receipt.id


def test_review_session_picks_up_late_approved_match() -> None:
    create_db_and_tables()
    with Session(engine) as session:
        statement_id, tx_id, receipt_id = _seed(session)

        review = get_or_create_review_session(session, statement_id)
        rows = review_rows(session, review.id)
        assert len(rows) == 1
        row = rows[0]
        assert row.receipt_document_id is None
        assert row.match_decision_id is None
        assert row.status == "needs_review"
        assert row.attention_required is True
        suggested = json.loads(row.suggested_json)
        assert suggested["review_status"] == "unmatched"
        assert suggested["receipt_id"] is None

        decision = MatchDecision(
            statement_transaction_id=tx_id,
            receipt_document_id=receipt_id,
            confidence="high",
            match_method="auto",
            approved=True,
            reason="auto-approved after build",
        )
        session.add(decision)
        session.commit()
        session.refresh(decision)

        review_after = get_or_create_review_session(session, statement_id)
        assert review_after.id == review.id

        rows_after = review_rows(session, review.id)
        assert len(rows_after) == 1
        updated = rows_after[0]
        assert updated.id == row.id
        assert updated.receipt_document_id == receipt_id
        assert updated.match_decision_id == decision.id
        suggested_after = json.loads(updated.suggested_json)
        assert suggested_after["review_status"] == "suggested"
        assert suggested_after["receipt_id"] == receipt_id
        source_after = json.loads(updated.source_json)
        assert source_after["match"]["status"] == "matched"
        assert source_after["match"]["approved"] is True


def test_edited_row_is_not_overwritten_by_late_match() -> None:
    create_db_and_tables()
    with Session(engine) as session:
        statement_id, tx_id, receipt_id = _seed(session)

        review = get_or_create_review_session(session, statement_id)
        row = review_rows(session, review.id)[0]

        update_review_row(
            session,
            row.id,
            fields={"business_or_personal": "Business", "report_bucket": "Business"},
        )

        edited_before = review_rows(session, review.id)[0]
        assert edited_before.status == "edited"
        assert edited_before.receipt_document_id is None

        decision = MatchDecision(
            statement_transaction_id=tx_id,
            receipt_document_id=receipt_id,
            confidence="high",
            match_method="auto",
            approved=True,
            reason="auto-approved after edit",
        )
        session.add(decision)
        session.commit()

        get_or_create_review_session(session, statement_id)
        after = review_rows(session, review.id)[0]
        assert after.status == "edited"
        assert after.receipt_document_id is None
        assert after.match_decision_id is None


def main() -> None:
    test_review_session_picks_up_late_approved_match()
    test_edited_row_is_not_overwritten_by_late_match()
    print("review_session_late_match_tests=passed")


if __name__ == "__main__":
    main()
