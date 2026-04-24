"""B5 regression: ReviewRow.confirmed_json is canonical at report-generation and
validation time. Operator corrections in confirmed_json must win over the
original ReceiptDocument columns (which remain as initial-suggestion scaffolding
only).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path
from uuid import uuid4

VERIFY_ROOT = Path.cwd() / ".verify_data"
VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{VERIFY_ROOT / f'b5_canonical_{uuid4().hex}.db'}"
os.environ["EXPENSE_STORAGE_ROOT"] = str(VERIFY_ROOT)
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlmodel import Session  # noqa: E402

from app.db import create_db_and_tables, engine  # noqa: E402
from app.models import (  # noqa: E402
    AppUser,
    MatchDecision,
    ReceiptDocument,
    ReviewRow,
    ReviewSession,
    StatementImport,
    StatementTransaction,
)
from app.services.report_generator import _confirmed_lines  # noqa: E402
from app.services.report_validation import validate_report_readiness  # noqa: E402
from app.services.review_sessions import (  # noqa: E402
    confirm_review_session,
    get_or_create_review_session,
    review_rows,
    update_review_row,
)


create_db_and_tables()


def _seed(session: Session) -> tuple[int, int, int]:
    """Seed one statement + one transaction + one receipt whose ORIGINAL
    extraction says report_bucket='Other' and business_or_personal='Business'."""
    user = AppUser(telegram_user_id=5005, display_name="B5 Tester")
    statement = StatementImport(
        source_filename=f"b5_{uuid4().hex[:8]}.xlsx",
        row_count=1,
    )
    session.add(user)
    session.add(statement)
    session.commit()
    session.refresh(statement)

    tx = StatementTransaction(
        statement_import_id=statement.id,
        transaction_date=date(2026, 3, 15),
        supplier_raw="MIGROS",
        supplier_normalized="MIGROS",
        local_currency="TRY",
        local_amount=250.00,
    )
    receipt = ReceiptDocument(
        source="test",
        status="imported",
        content_type="photo",
        original_file_name="migros.jpg",
        extracted_date=date(2026, 3, 15),
        extracted_supplier="Migros",
        extracted_local_amount=250.00,
        extracted_currency="TRY",
        business_or_personal="Business",  # ← ORIGINAL extraction (will be overridden)
        report_bucket="Other",             # ← ORIGINAL extraction (will be overridden)
        business_reason="placeholder reason",
        needs_clarification=False,
    )
    session.add(tx)
    session.add(receipt)
    session.commit()
    session.refresh(tx)
    session.refresh(receipt)

    decision = MatchDecision(
        statement_transaction_id=tx.id,
        receipt_document_id=receipt.id,
        confidence="high",
        match_method="test_b5",
        approved=True,
        reason="B5 regression",
    )
    session.add(decision)
    session.commit()
    return statement.id, tx.id, receipt.id


def test_report_generator_uses_confirmed_json_over_receipt_columns() -> None:
    """Operator corrects bucket + bp in confirmed_json. Report generator and
    validation MUST honor confirmed_json, not the receipt columns."""
    with Session(engine) as session:
        statement_id, tx_id, receipt_id = _seed(session)

        review = get_or_create_review_session(session, statement_id)
        rows = review_rows(session, review.id)
        assert len(rows) == 1
        row = rows[0]
        assert row.receipt_document_id == receipt_id

        # Operator corrects the row: change bucket to Meals/Snacks and
        # reclassify as Personal.
        update_review_row(
            session,
            row_id=row.id,
            fields={
                "report_bucket": "Meals/Snacks",
                "business_or_personal": "Personal",
            },
        )
        session.refresh(row)

        confirmed = json.loads(row.confirmed_json)
        assert confirmed["report_bucket"] == "Meals/Snacks"
        assert confirmed["business_or_personal"] == "Personal"

        # Assert the RECEIPT COLUMNS were NOT mutated — they still hold the
        # original extraction. This pins that confirmed_json, not the receipt
        # column, is the divergence point.
        session.refresh(session.get(ReceiptDocument, receipt_id))
        receipt = session.get(ReceiptDocument, receipt_id)
        assert receipt.report_bucket == "Other", "receipt column should be untouched (scaffolding only)"
        assert receipt.business_or_personal == "Business", "receipt column should be untouched"

        # Confirm the session so _confirmed_lines can run (it requires a
        # confirmed snapshot).
        confirm_review_session(session, review.id)

        # Report-generation pipeline: lines must reflect confirmed_json.
        lines = _confirmed_lines(session, statement_id)
        assert len(lines) == 1
        line = lines[0]
        assert line.report_bucket == "Meals/Snacks", (
            f"report line must use confirmed_json bucket; got {line.report_bucket!r}"
        )
        assert line.business_or_personal == "Personal", (
            f"report line must use confirmed_json bp; got {line.business_or_personal!r}"
        )

        # Validation pipeline: bp='Personal' on confirmed → personal_receipts
        # count must be 1 (counted from confirmed_by_receipt_id, NOT from the
        # receipt.business_or_personal='Business' column).
        validation = validate_report_readiness(session, statement_id)
        assert validation.personal_receipts == 1, (
            f"validation must count bp from confirmed_json; got personal_receipts={validation.personal_receipts}"
        )
        assert validation.business_receipts == 0, (
            f"receipt column bp='Business' must NOT leak into counts; got business_receipts={validation.business_receipts}"
        )


def test_validate_report_readiness_errors_when_receipt_has_no_review_row() -> None:
    """If an approved receipt has no corresponding ReviewRow, the validator
    must raise a structured missing_review_row error rather than falling back
    to the receipt columns."""
    with Session(engine) as session:
        user = AppUser(telegram_user_id=5006, display_name="B5 Orphan Tester")
        statement = StatementImport(
            source_filename=f"b5_orphan_{uuid4().hex[:8]}.xlsx",
            row_count=1,
        )
        session.add(user)
        session.add(statement)
        session.commit()
        session.refresh(statement)

        tx = StatementTransaction(
            statement_import_id=statement.id,
            transaction_date=date(2026, 3, 15),
            supplier_raw="SHELL",
            supplier_normalized="SHELL",
            local_currency="TRY",
            local_amount=500.00,
        )
        receipt = ReceiptDocument(
            source="test",
            status="imported",
            content_type="photo",
            original_file_name="shell.jpg",
            extracted_date=date(2026, 3, 15),
            extracted_supplier="Shell",
            extracted_local_amount=500.00,
            extracted_currency="TRY",
            business_or_personal="Business",
            report_bucket="Auto Gasoline",
            needs_clarification=False,
        )
        session.add(tx)
        session.add(receipt)
        session.commit()
        session.refresh(tx)
        session.refresh(receipt)

        decision = MatchDecision(
            statement_transaction_id=tx.id,
            receipt_document_id=receipt.id,
            confidence="high",
            match_method="test_b5_orphan",
            approved=True,
            reason="approved without a review session",
        )
        session.add(decision)
        session.commit()

        # Deliberately do NOT build a ReviewSession.
        validation = validate_report_readiness(session, statement.id)
        codes = [issue.code for issue in validation.issues]
        assert "missing_review_row" in codes, (
            f"expected missing_review_row error when no ReviewRow exists; issue codes={codes}"
        )
        missing_issue = next(i for i in validation.issues if i.code == "missing_review_row")
        assert missing_issue.severity == "error"
        assert missing_issue.receipt_id == receipt.id


if __name__ == "__main__":
    test_report_generator_uses_confirmed_json_over_receipt_columns()
    test_validate_report_readiness_errors_when_receipt_has_no_review_row()
    print("b5_canonical_tests=passed")
