"""Addition A: hard-block / soft-flag justification validation rules.

EDT's reviewer flagged reports shipping without required justifications —
empty business_reason on Business rows, empty attendees on meals, and
Customer Entertainment charges without a COO pre-approval reference.
These tests pin the new blocking and flagging behavior.

Data source: ReviewRow.confirmed_json is canonical (per M1 Day 2 pivot).
All tests construct ReviewSession + ReviewRow fixtures directly so the
validator exercises the new confirmed_json-based checks without going
through the sync-from-transactions path.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

VERIFY_ROOT = Path.cwd() / ".verify_data"
VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{VERIFY_ROOT / f'justification_{uuid4().hex}.db'}"
os.environ["EXPENSE_STORAGE_ROOT"] = str(VERIFY_ROOT)
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import pytest  # noqa: E402

from app.json_utils import DecimalEncoder  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.models import (  # noqa: E402
    AppUser,
    ExpenseReport,
    MatchDecision,
    ReceiptDocument,
    ReviewRow,
    ReviewSession,
    StatementImport,
    StatementTransaction,
)
from app.services.report_validation import validate_report_readiness  # noqa: E402


def _seed_confirmed_row(
    session: Session,
    *,
    bucket: str,
    business_or_personal: str,
    business_reason: str | None,
    attendees: str | None,
    amount: Decimal = Decimal("50.0"),
    currency: str = "USD",
    supplier: str = "Test Supplier",
) -> tuple[int, int]:
    """Seed a fully-wired statement → transaction → receipt → approved match →
    confirmed review row. Returns (expense_report_id, review_row_id)."""
    user = AppUser(telegram_user_id=1 + hash(uuid4().hex) % 10_000, display_name="A Tester")
    session.add(user)
    session.flush()

    statement = StatementImport(
        source_filename=f"jv_{uuid4().hex[:6]}.xlsx",
        row_count=1,
        uploader_user_id=user.id,
    )
    session.add(statement)
    session.commit()
    session.refresh(statement)

    tx = StatementTransaction(
        statement_import_id=statement.id,
        transaction_date=date(2026, 4, 1),
        supplier_raw=supplier,
        supplier_normalized=supplier.upper(),
        local_currency=currency,
        local_amount=amount,
        usd_amount=amount if currency == "USD" else None,
    )
    receipt = ReceiptDocument(
        source="test",
        status="imported",
        content_type="photo",
        original_file_name=f"{supplier.lower().replace(' ', '_')}.jpg",
        extracted_date=date(2026, 4, 1),
        extracted_supplier=supplier,
        extracted_local_amount=amount,
        extracted_currency=currency,
        business_or_personal=business_or_personal,
        report_bucket=bucket,
        business_reason=business_reason,
        attendees=attendees,
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
        match_method="jv_test",
        approved=True,
        reason="Addition A fixture",
    )
    session.add(decision)
    session.commit()

    report = ExpenseReport(
        owner_user_id=user.id,
        report_kind="diners_statement",
        title="JV test report",
        status="draft",
        report_currency="USD",
        statement_import_id=statement.id,
    )
    session.add(report)
    session.commit()
    session.refresh(report)

    review = ReviewSession(
        expense_report_id=report.id,
        statement_import_id=statement.id,
        status="draft",
    )
    session.add(review)
    session.commit()
    session.refresh(review)

    confirmed = {
        "transaction_id": tx.id,
        "receipt_id": receipt.id,
        "transaction_date": "2026-04-01",
        "supplier": supplier,
        "amount": amount,
        "currency": currency,
        "business_or_personal": business_or_personal,
        "report_bucket": bucket,
        "business_reason": business_reason,
        "attendees": attendees,
    }
    row = ReviewRow(
        review_session_id=review.id,
        statement_transaction_id=tx.id,
        receipt_document_id=receipt.id,
        match_decision_id=decision.id,
        status="confirmed",
        attention_required=False,
        attention_note=None,
        source_json=json.dumps({"statement": {}, "receipt": {}, "match": {"status": "matched"}}),
        suggested_json=json.dumps(confirmed, cls=DecimalEncoder),
        confirmed_json=json.dumps(confirmed, cls=DecimalEncoder),
    )
    session.add(row)
    session.commit()
    session.refresh(row)

    review.status = "confirmed"
    review.snapshot_json = json.dumps([{**confirmed, "review_row_id": row.id}], cls=DecimalEncoder)
    session.add(review)
    session.commit()

    return report.id, row.id


# ─── Tests ───────────────────────────────────────────────────────────────────

def test_missing_business_reason_blocks_generation(isolated_db):
    with Session(isolated_db) as session:
        report_id, row_id = _seed_confirmed_row(
            session,
            bucket="Auto Gasoline",
            business_or_personal="Business",
            business_reason=None,
            attendees=None,
            supplier="Shell",
        )
        validation = validate_report_readiness(session, expense_report_id=report_id)

    codes = [i.code for i in validation.issues]
    assert "missing_business_reason" in codes, f"got issues={codes}"
    block = next(i for i in validation.issues if i.code == "missing_business_reason")
    assert block.severity == "error"
    assert str(row_id) in block.message
    assert validation.ready is False


def test_business_reason_present_does_not_block(isolated_db):
    with Session(isolated_db) as session:
        report_id, _ = _seed_confirmed_row(
            session,
            bucket="Auto Gasoline",
            business_or_personal="Business",
            business_reason="Fuel for customer visit in Istanbul",
            attendees=None,  # non-meal bucket: attendees not required
            supplier="Shell",
        )
        validation = validate_report_readiness(session, expense_report_id=report_id)

    codes = [i.code for i in validation.issues]
    assert "missing_business_reason" not in codes


def test_missing_attendees_on_dinner_blocks(isolated_db):
    with Session(isolated_db) as session:
        report_id, row_id = _seed_confirmed_row(
            session,
            bucket="Dinner",
            business_or_personal="Business",
            business_reason="Team dinner after late shift",
            attendees=None,
            amount=Decimal("55.0"),
            supplier="Trattoria",
        )
        validation = validate_report_readiness(session, expense_report_id=report_id)

    codes = [i.code for i in validation.issues]
    assert "missing_attendees_on_meal" in codes, f"got issues={codes}"
    block = next(i for i in validation.issues if i.code == "missing_attendees_on_meal")
    assert block.severity == "error"
    assert str(row_id) in block.message
    assert "Trattoria" in block.message
    assert validation.ready is False


def test_customer_entertainment_needs_preapproval_reference(isolated_db):
    with Session(isolated_db) as session:
        report_id, row_id = _seed_confirmed_row(
            session,
            bucket="Customer Entertainment",
            business_or_personal="Business",
            business_reason="Took the client out for drinks",
            attendees="self, Client X",
            amount=Decimal("180.0"),
            supplier="The Lobby Bar",
        )
        validation = validate_report_readiness(session, expense_report_id=report_id)

    codes = [i.code for i in validation.issues]
    assert "customer_entertainment_no_preapproval" in codes, f"got issues={codes}"
    block = next(i for i in validation.issues if i.code == "customer_entertainment_no_preapproval")
    assert block.severity == "error"
    assert str(row_id) in block.message
    assert validation.ready is False


def test_customer_entertainment_with_coo_reference_passes(isolated_db):
    with Session(isolated_db) as session:
        report_id, _ = _seed_confirmed_row(
            session,
            bucket="Customer Entertainment",
            business_or_personal="Business",
            business_reason="Pre-approved by COO: ref-123; host dinner with Acme CFO",
            attendees="self, Jane Doe (Acme)",
            amount=Decimal("180.0"),
            supplier="The Lobby Bar",
        )
        validation = validate_report_readiness(session, expense_report_id=report_id)

    codes = [i.code for i in validation.issues]
    assert "customer_entertainment_no_preapproval" not in codes
    # No other error code should appear for this otherwise-valid row — the
    # pre-approval gate is the only thing that could have fired.
    errors = [i for i in validation.issues if i.severity == "error"]
    assert errors == [], f"unexpected errors: {[i.code for i in errors]}"
    assert validation.ready is True


def test_dinner_exceeds_cap_soft_flags(isolated_db):
    # $70 / 1 attendee with customer (cap $60) → warning, not block.
    with Session(isolated_db) as session:
        report_id, row_id = _seed_confirmed_row(
            session,
            bucket="Dinner",
            business_or_personal="Business",
            business_reason="Client dinner",
            attendees="self, Acme CFO",
            amount=Decimal("140.0"),  # 2 heads, $70/head, exceeds $60/head with-customer cap
            currency="USD",
            supplier="Steakhouse",
        )
        validation = validate_report_readiness(session, expense_report_id=report_id)

    codes = [i.code for i in validation.issues]
    assert "dinner_exceeds_cap" in codes, f"got issues={codes}"
    warn = next(i for i in validation.issues if i.code == "dinner_exceeds_cap")
    assert warn.severity == "warning"
    assert "70.00" in warn.message
    assert "with customer" in warn.message
    assert "Add justification if warranted" in warn.message
    # Soft flag — ready stays True (no errors from this specific fixture).
    errors = [i for i in validation.issues if i.severity == "error"]
    assert errors == [], f"unexpected errors on soft-flag test: {[i.code for i in errors]}"
    assert validation.ready is True


def test_personal_rows_not_validated(isolated_db):
    # Personal row, empty business_reason, empty attendees — nothing should block.
    with Session(isolated_db) as session:
        report_id, _ = _seed_confirmed_row(
            session,
            bucket="Dinner",
            business_or_personal="Personal",
            business_reason=None,
            attendees=None,
            amount=Decimal("45.0"),
            supplier="Corner Bistro",
        )
        validation = validate_report_readiness(session, expense_report_id=report_id)

    codes = [i.code for i in validation.issues]
    # None of the Addition A hard blocks should fire on a personal row.
    assert "missing_business_reason" not in codes
    assert "missing_attendees_on_meal" not in codes
    assert "customer_entertainment_no_preapproval" not in codes
    assert "dinner_exceeds_cap" not in codes


def test_solo_dinner_cap_stricter(isolated_db):
    # Attendees="self", $32 amount, 1 head, cap $30 solo → warning.
    with Session(isolated_db) as session:
        report_id, row_id = _seed_confirmed_row(
            session,
            bucket="Dinner",
            business_or_personal="Business",
            business_reason="Late-night work dinner",
            attendees="self",
            amount=Decimal("32.0"),
            currency="USD",
            supplier="Diner",
        )
        validation = validate_report_readiness(session, expense_report_id=report_id)

    codes = [i.code for i in validation.issues]
    assert "dinner_exceeds_cap" in codes, (
        f"solo dinner at $32/head should exceed $30 solo cap; got issues={codes}"
    )
    warn = next(i for i in validation.issues if i.code == "dinner_exceeds_cap")
    assert warn.severity == "warning"
    assert "without customer" in warn.message, f"expected solo framing, got: {warn.message!r}"
    assert "32.00" in warn.message
    assert validation.ready is True
