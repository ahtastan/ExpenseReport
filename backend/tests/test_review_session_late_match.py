"""Regression tests for ``_sync_review_rows`` late-match upgrade path.

When a ``ReviewSession`` is materialized before any ``MatchDecision`` rows
have been approved, every ``ReviewRow`` is initially unmatched. A subsequent
matcher run that approves decisions should, on the next re-sync, upgrade
those untouched rows in place rather than leave them permanently orphaned.

These tests pin that behaviour and guard against accidentally clobbering
rows the user has already edited or confirmed.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from datetime import date
from pathlib import Path
from uuid import uuid4

VERIFY_ROOT = Path.cwd() / ".verify_data"
VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{VERIFY_ROOT / f'review_late_match_{uuid4().hex}.db'}"
os.environ["EXPENSE_STORAGE_ROOT"] = str(VERIFY_ROOT)
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from sqlmodel import Session  # noqa: E402

from app.db import create_db_and_tables, engine  # noqa: E402
from app.models import (  # noqa: E402
    AppUser,
    MatchDecision,
    ReceiptDocument,
    ReviewSession,
    StatementImport,
    StatementTransaction,
)
from app.services.review_sessions import (  # noqa: E402
    get_or_create_review_session,
    review_rows,
    update_review_row,
)
from _pivot_helpers import ensure_expense_report_for_statement  # noqa: E402


create_db_and_tables()


def _seed_import_and_receipt(session: Session) -> tuple[int, int, int]:
    """Create the minimum fixture: one user, one import + txn, one receipt.

    Returns (statement_import_id, transaction_id, receipt_id).
    """
    user = AppUser(
        telegram_user_id=1001,
        display_name="Test Reviewer",
    )
    session.add(user)
    session.flush()
    statement = StatementImport(
        source_filename=f"late_match_{uuid4().hex[:8]}.xlsx",
        row_count=1,
        uploader_user_id=user.id,
    )
    session.add(statement)
    session.commit()
    session.refresh(statement)

    tx = StatementTransaction(
        statement_import_id=statement.id,
        transaction_date=date(2026, 3, 15),
        supplier_raw="Migros",
        supplier_normalized="MIGROS",
        local_currency="TRY",
        local_amount=Decimal("250.00"),
    )
    receipt = ReceiptDocument(
        source="test",
        status="imported",
        content_type="photo",
        original_file_name="migros.jpg",
        extracted_date=date(2026, 3, 15),
        extracted_supplier="Migros",
        extracted_local_amount=Decimal("250.00"),
        extracted_currency="TRY",
        business_or_personal="Business",
        report_bucket="Meals/Snacks",
        needs_clarification=False,
    )
    session.add(tx)
    session.add(receipt)
    session.commit()
    session.refresh(tx)
    session.refresh(receipt)
    return statement.id, tx.id, receipt.id


def _approve_match(session: Session, tx_id: int, receipt_id: int) -> int:
    decision = MatchDecision(
        statement_transaction_id=tx_id,
        receipt_document_id=receipt_id,
        confidence="high",
        match_method="test_late_match",
        approved=True,
        reason="regression: approved after session build",
    )
    session.add(decision)
    session.commit()
    session.refresh(decision)
    return decision.id


def test_review_session_picks_up_late_approved_match() -> None:
    """After approving a match post-build, the next sync must upgrade the row."""
    with Session(engine) as session:
        statement_id, tx_id, receipt_id = _seed_import_and_receipt(session)

        # First sync: no decisions yet, the single row must be unmatched.
        review = get_or_create_review_session(
            session,
            expense_report_id=ensure_expense_report_for_statement(session, statement_id),
        )
        rows = review_rows(session, review.id)
        assert len(rows) == 1, "expected a single review row for the single txn"
        row_before = rows[0]
        assert row_before.receipt_document_id is None
        assert row_before.match_decision_id is None
        assert row_before.status in {"needs_review", "suggested"}

        # Approve a match AFTER the session+rows already exist.
        decision_id = _approve_match(session, tx_id, receipt_id)

        # Second sync: same session (get_or_create must return the existing
        # draft and re-sync rather than create a new one).
        review_again = get_or_create_review_session(
            session,
            expense_report_id=ensure_expense_report_for_statement(session, statement_id),
        )
        assert review_again.id == review.id, "must reuse the draft session"

        rows_after = review_rows(session, review_again.id)
        assert len(rows_after) == 1, "no duplicate row created"
        row_after = rows_after[0]
        assert row_after.id == row_before.id, "row primary key must be stable"
        assert row_after.receipt_document_id == receipt_id
        assert row_after.match_decision_id == decision_id

        import json

        suggested = json.loads(row_after.suggested_json)
        assert suggested.get("review_status") == "suggested"
        assert suggested.get("receipt_id") == receipt_id


def test_edited_row_is_not_overwritten_by_late_match() -> None:
    """A row the user has already touched must survive the re-sync untouched."""
    with Session(engine) as session:
        statement_id, tx_id, receipt_id = _seed_import_and_receipt(session)

        review = get_or_create_review_session(
            session,
            expense_report_id=ensure_expense_report_for_statement(session, statement_id),
        )
        rows = review_rows(session, review.id)
        assert len(rows) == 1
        row = rows[0]
        assert row.receipt_document_id is None

        # User edits the row. We must satisfy every REQUIRED_FIELDS slot so
        # update_review_row does not downgrade the status back to
        # "needs_review" — the assertion below pins that the row reached the
        # "edited" state the upgrade gate checks for. The Migros supplier row
        # gets report_bucket="Other" from suggest_bucket, so the only missing
        # required field is business_or_personal.
        update_review_row(
            session,
            row_id=row.id,
            fields={
                "business_or_personal": "Business",
                "business_reason": "user-supplied reason",
            },
        )
        session.refresh(row)
        edited_status = row.status
        assert edited_status == "edited", (
            f"fixture setup failed: expected status='edited' after user edit, "
            f"got {edited_status!r}"
        )

        # Match is approved AFTER the user's edit.
        _approve_match(session, tx_id, receipt_id)

        # Re-sync: must NOT clobber the user's edit. Row stays edited and
        # unlinked to a receipt (the user chose to leave it that way).
        review_again = get_or_create_review_session(
            session,
            expense_report_id=ensure_expense_report_for_statement(session, statement_id),
        )
        assert review_again.id == review.id

        rows_after = review_rows(session, review_again.id)
        assert len(rows_after) == 1
        row_after = rows_after[0]
        assert row_after.id == row.id
        assert row_after.status == "edited", (
            "edited rows must NOT be overwritten by a late match re-sync"
        )
        assert row_after.receipt_document_id is None, (
            "edited rows must keep their existing linkage (None here)"
        )


def test_matched_row_falls_back_to_suggest_bucket_when_receipt_has_no_bucket() -> None:
    """B3 regression: a matched receipt with null report_bucket must get a
    suggested bucket derived from the statement supplier name, not leave the
    review row with report_bucket=None."""
    from app.services.merchant_buckets import suggest_bucket as _suggest

    with Session(engine) as session:
        user = AppUser(telegram_user_id=2002, display_name="B3 Tester")
        session.add(user)
        session.flush()
        statement = StatementImport(
            source_filename=f"b3_bucket_{uuid4().hex[:8]}.xlsx",
            row_count=1,
            uploader_user_id=user.id,
        )
        session.add(statement)
        session.commit()
        session.refresh(statement)

        tx = StatementTransaction(
            statement_import_id=statement.id,
            transaction_date=date(2026, 3, 15),
            supplier_raw="SHELL PETROL",
            supplier_normalized="SHELL PETROL",
            local_currency="TRY",
            local_amount=Decimal("500.00"),
        )
        receipt = ReceiptDocument(
            source="test",
            status="imported",
            content_type="photo",
            original_file_name="shell.jpg",
            extracted_date=date(2026, 3, 15),
            extracted_supplier="Shell",
            extracted_local_amount=Decimal("500.00"),
            extracted_currency="TRY",
            business_or_personal="Business",
            report_bucket=None,  # ← the B3 case: receipt has no stored bucket
            needs_clarification=False,
        )
        session.add(tx)
        session.add(receipt)
        session.commit()
        session.refresh(tx)
        session.refresh(receipt)

        _approve_match(session, tx.id, receipt.id)

        review = get_or_create_review_session(
            session,
            expense_report_id=ensure_expense_report_for_statement(session, statement.id),
        )
        rows = review_rows(session, review.id)
        assert len(rows) == 1
        row = rows[0]
        assert row.receipt_document_id == receipt.id

        import json as _json

        suggested = _json.loads(row.suggested_json)
        expected_bucket = _suggest("SHELL PETROL")
        assert expected_bucket is not None, "fixture error: supplier must map to a known bucket"
        assert suggested.get("report_bucket") == expected_bucket, (
            f"matched row must fall back to suggest_bucket when receipt.report_bucket is None; "
            f"got {suggested.get('report_bucket')!r}, expected {expected_bucket!r}"
        )


if __name__ == "__main__":
    test_review_session_picks_up_late_approved_match()
    test_edited_row_is_not_overwritten_by_late_match()
    test_matched_row_falls_back_to_suggest_bucket_when_receipt_has_no_bucket()
    print("review_session_late_match_tests=passed")
