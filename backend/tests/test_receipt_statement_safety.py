from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlmodel import Session

from app.db import engine
from app.models import (
    AppUser,
    MatchDecision,
    ReceiptDocument,
    StatementImport,
    StatementTransaction,
)
from app.services.report_validation import validate_report_readiness
from app.services.review_sessions import (
    confirm_review_session,
    get_or_create_review_session,
    review_rows,
    session_payload,
    update_review_row,
)
from app.services.receipt_statement_safety import (
    _normalize_currency,
    receipt_statement_issues,
)
from _pivot_helpers import ensure_expense_report_for_statement


@pytest.mark.parametrize(
    (
        "label",
        "receipt_date",
        "receipt_amount",
        "receipt_currency",
        "statement_date",
        "statement_amount",
        "statement_currency",
        "expected_codes",
    ),
    [
        (
            "receipt_11_missing_date",
            None,
            Decimal("682.65"),
            "TRY",
            date(2025, 11, 21),
            Decimal("682.65"),
            "TRY",
            ["receipt_statement_date_missing"],
        ),
        (
            "receipt_14_wrong_year",
            date(2024, 11, 23),
            Decimal("480.00"),
            "TRY",
            date(2025, 11, 23),
            Decimal("480.00"),
            "TRY",
            ["receipt_statement_date_mismatch"],
        ),
        (
            "receipt_16_missing_date_wrong_amount",
            None,
            Decimal("5.58"),
            "BAM",
            date(2025, 11, 24),
            Decimal("5.50"),
            "BAM",
            ["receipt_statement_amount_mismatch", "receipt_statement_date_missing"],
        ),
        (
            "receipt_32_wrong_year_wrong_amount",
            date(2024, 12, 25),
            Decimal("420.00"),
            "TRY",
            date(2025, 12, 25),
            Decimal("410.00"),
            "TRY",
            ["receipt_statement_amount_mismatch", "receipt_statement_date_mismatch"],
        ),
        (
            "receipt_34_wrong_month_wrong_amount",
            date(2025, 6, 3),
            Decimal("650.00"),
            "TRY",
            date(2025, 12, 25),
            Decimal("500.00"),
            "TRY",
            ["receipt_statement_amount_mismatch", "receipt_statement_date_mismatch"],
        ),
        (
            "receipt_35_wrong_date",
            date(2024, 12, 1),
            Decimal("1429.41"),
            "TRY",
            date(2025, 12, 25),
            Decimal("1429.41"),
            "TRY",
            ["receipt_statement_date_mismatch"],
        ),
        (
            "receipt_42_wrong_amount",
            date(2025, 12, 28),
            Decimal("203.50"),
            "TRY",
            date(2025, 12, 28),
            Decimal("223.00"),
            "TRY",
            ["receipt_statement_amount_mismatch"],
        ),
        (
            "receipt_44_wrong_date",
            date(2025, 11, 29),
            Decimal("240.00"),
            "TRY",
            date(2025, 12, 28),
            Decimal("240.00"),
            "TRY",
            ["receipt_statement_date_mismatch"],
        ),
    ],
)
def test_verified_receipt_amount_date_failures_are_classified_without_images(
    label: str,
    receipt_date: date | None,
    receipt_amount: Decimal,
    receipt_currency: str,
    statement_date: date,
    statement_amount: Decimal,
    statement_currency: str,
    expected_codes: list[str],
) -> None:
    receipt = ReceiptDocument(
        extracted_date=receipt_date,
        extracted_local_amount=receipt_amount,
        extracted_currency=receipt_currency,
        extracted_supplier=f"{label} receipt supplier",
    )
    transaction = StatementTransaction(
        transaction_date=statement_date,
        local_amount=statement_amount,
        local_currency=statement_currency,
        supplier_raw=f"{label} statement supplier",
    )

    assert [
        issue.code for issue in receipt_statement_issues(receipt, transaction)
    ] == expected_codes


def _seed_approved_match(
    session: Session,
    *,
    tx_date: date,
    tx_amount: Decimal,
    tx_currency: str,
    tx_supplier: str = "Statement Supplier",
    receipt_date: date | None,
    receipt_amount: Decimal | None,
    receipt_currency: str | None,
    receipt_supplier: str = "Receipt Supplier",
) -> int:
    user = AppUser(display_name="receipt-statement-safety")
    session.add(user)
    session.flush()

    statement = StatementImport(
        source_filename="synthetic_statement.xlsx",
        row_count=1,
        uploader_user_id=user.id,
    )
    session.add(statement)
    session.flush()

    tx = StatementTransaction(
        statement_import_id=statement.id,
        transaction_date=tx_date,
        supplier_raw=tx_supplier,
        supplier_normalized=tx_supplier.upper(),
        local_amount=tx_amount,
        local_currency=tx_currency,
    )
    receipt = ReceiptDocument(
        source="test",
        status="imported",
        content_type="photo",
        original_file_name="synthetic_receipt.jpg",
        extracted_date=receipt_date,
        extracted_supplier=receipt_supplier,
        extracted_local_amount=receipt_amount,
        extracted_currency=receipt_currency,
        business_or_personal="Business",
        report_bucket="Business",
        business_reason="Synthetic regression fixture",
        needs_clarification=False,
    )
    session.add(tx)
    session.add(receipt)
    session.commit()
    session.refresh(statement)
    session.refresh(tx)
    session.refresh(receipt)

    session.add(
        MatchDecision(
            statement_transaction_id=tx.id,
            receipt_document_id=receipt.id,
            confidence="high",
            match_method="test",
            approved=True,
            reason="synthetic approved match",
        )
    )
    session.commit()
    return statement.id  # type: ignore[return-value]


def test_review_row_blocks_amount_date_currency_receipt_statement_mismatch() -> None:
    with Session(engine) as session:
        statement_id = _seed_approved_match(
            session,
            tx_date=date(2025, 12, 25),
            tx_amount=Decimal("410.00"),
            tx_currency="TRY",
            receipt_date=date(2024, 12, 25),
            receipt_amount=Decimal("420.00"),
            receipt_currency="BAM",
        )
        expense_report_id = ensure_expense_report_for_statement(session, statement_id)

        review = get_or_create_review_session(session, expense_report_id=expense_report_id)
        rows = review_rows(session, review.id or 0)
        assert len(rows) == 1
        row = rows[0]
        payload = session_payload(session, review)["rows"][0]

        assert row.status == "needs_review"
        assert row.attention_required is True
        assert "receipt/statement amount mismatch" in (row.attention_note or "")
        issue_codes = [
            issue["code"]
            for issue in payload["source"]["match"]["receipt_statement_issues"]
        ]
        assert issue_codes == [
            "receipt_statement_amount_mismatch",
            "receipt_statement_currency_mismatch",
            "receipt_statement_date_mismatch",
        ]

        with pytest.raises(ValueError, match="rows marked for attention"):
            confirm_review_session(session, review.id or 0)

        update_review_row(
            session,
            row.id or 0,
            fields={"business_reason": "Reviewed receipt against statement"},
        )
        session.refresh(row)
        assert row.attention_required is True

        update_review_row(
            session,
            row.id or 0,
            attention_required=False,
            attention_note="Reviewer accepted statement values after visual check.",
        )
        session.refresh(row)
        assert row.attention_required is False
        confirm_review_session(session, review.id or 0)
        validation = validate_report_readiness(
            session, expense_report_id=expense_report_id
        )
        warning_codes = [issue.code for issue in validation.issues]
        assert validation.ready is True
        assert "receipt_statement_amount_mismatch" in warning_codes
        assert "receipt_statement_currency_mismatch" in warning_codes
        assert "receipt_statement_date_mismatch" in warning_codes


def test_review_row_blocks_missing_receipt_date_even_when_amount_matches() -> None:
    with Session(engine) as session:
        statement_id = _seed_approved_match(
            session,
            tx_date=date(2025, 11, 21),
            tx_amount=Decimal("682.65"),
            tx_currency="TRY",
            receipt_date=None,
            receipt_amount=Decimal("682.65"),
            receipt_currency="TRY",
        )
        expense_report_id = ensure_expense_report_for_statement(session, statement_id)

        review = get_or_create_review_session(session, expense_report_id=expense_report_id)
        row = review_rows(session, review.id or 0)[0]
        payload = session_payload(session, review)["rows"][0]

        assert row.status == "needs_review"
        assert row.attention_required is True
        assert "receipt date missing" in (row.attention_note or "")
        assert payload["source"]["match"]["receipt_statement_issues"][0]["code"] == (
            "receipt_statement_date_missing"
        )


def test_supplier_mismatch_alone_is_not_a_review_blocker() -> None:
    with Session(engine) as session:
        statement_id = _seed_approved_match(
            session,
            tx_date=date(2025, 12, 28),
            tx_amount=Decimal("240.00"),
            tx_currency="TRY",
            tx_supplier="Statement Merchant",
            receipt_date=date(2025, 12, 28),
            receipt_amount=Decimal("240.00"),
            receipt_currency="TRY",
            receipt_supplier="Different Receipt Header",
        )
        expense_report_id = ensure_expense_report_for_statement(session, statement_id)

        review = get_or_create_review_session(session, expense_report_id=expense_report_id)
        row = review_rows(session, review.id or 0)[0]
        payload = session_payload(session, review)["rows"][0]

        assert row.status == "suggested"
        assert row.attention_required is False
        assert "receipt_statement_issues" not in payload["source"]["match"]
        confirm_review_session(session, review.id or 0)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("TL", "TRY"),
        ("tl", "TRY"),
        ("₺", "TRY"),
        ("TRY", "TRY"),
        ("$", "USD"),
        ("USD", "USD"),
        ("€", "EUR"),
        ("EUR", "EUR"),
        (" eur ", "EUR"),
        ("£", "GBP"),
        ("GBP", "GBP"),
        ("KM", "BAM"),
        ("BAM", "BAM"),
        ("RSD", "RSD"),
    ],
)
def test_normalize_currency_handles_symbols_and_iso_codes(
    raw: str | None, expected: str | None
) -> None:
    assert _normalize_currency(raw) == expected


def test_currency_symbol_matches_iso_does_not_flag_mismatch() -> None:
    receipt = ReceiptDocument(
        extracted_date=date(2025, 6, 1),
        extracted_local_amount=Decimal("100.00"),
        extracted_currency="€",
        extracted_supplier="EU Cafe",
    )
    transaction = StatementTransaction(
        transaction_date=date(2025, 6, 1),
        local_amount=Decimal("100.00"),
        local_currency="EUR",
        supplier_raw="EU Cafe",
    )
    assert receipt_statement_issues(receipt, transaction) == []
