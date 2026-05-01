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
    ClarificationQuestion,
    ExpenseReport,
    MatchDecision,
    ReceiptDocument,
    ReviewRow,
    ReviewSession,
    StatementImport,
    StatementTransaction,
)
from app.services.report_validation import (  # noqa: E402
    _is_telecom_row,
    validate_report_readiness,
)


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


def test_personal_row_with_stale_open_clarification_does_not_block(isolated_db):
    with Session(isolated_db) as session:
        report_id, row_id = _seed_confirmed_row(
            session,
            bucket="Taxi",
            business_or_personal="Personal",
            business_reason=None,
            attendees=None,
            amount=Decimal("25.0"),
            supplier="Yellow Taxi",
        )
        row = session.get(ReviewRow, row_id)
        session.add(
            ClarificationQuestion(
                receipt_document_id=row.receipt_document_id,
                question_key="business_reason",
                question_text="Please reply with the business purpose.",
                status="open",
            )
        )
        session.commit()

        validation = validate_report_readiness(session, expense_report_id=report_id)

    codes = [i.code for i in validation.issues]
    assert "open_clarification" not in codes
    assert "missing_business_reason" not in codes
    assert validation.ready is True


def test_telecom_row_ignores_stale_meal_clarification(isolated_db):
    with Session(isolated_db) as session:
        report_id, row_id = _seed_confirmed_row(
            session,
            bucket="Telephone/Internet",
            business_or_personal="Business",
            business_reason=None,
            attendees=None,
            amount=Decimal("1617.25"),
            currency="TRY",
            supplier="ZEYNEP ILETISIM ELEK.BIL.PAZ. VODAFONE",
        )
        row = session.get(ReviewRow, row_id)
        session.add(
            ClarificationQuestion(
                receipt_document_id=row.receipt_document_id,
                question_key="telegram_meal_context",
                question_text="Was this business or personal spending? If business, who was included?",
                status="open",
            )
        )
        session.commit()

        validation = validate_report_readiness(session, expense_report_id=report_id)

    codes = [i.code for i in validation.issues]
    assert "open_clarification" not in codes
    assert "missing_business_reason" not in codes
    assert "missing_attendees_on_meal" not in codes
    assert validation.ready is True


def test_business_meal_with_answered_fields_ignores_stale_open_context_question(isolated_db):
    with Session(isolated_db) as session:
        report_id, row_id = _seed_confirmed_row(
            session,
            bucket="Dinner",
            business_or_personal="Business",
            business_reason="Customer dinner after site visit",
            attendees="Hakan, Ahmet Yilmaz",
            amount=Decimal("52.0"),
            supplier="Bosnak Doner",
        )
        row = session.get(ReviewRow, row_id)
        session.add(
            ClarificationQuestion(
                receipt_document_id=row.receipt_document_id,
                question_key="telegram_meal_context",
                question_text="Was this business or personal spending? If business, who was included?",
                status="open",
            )
        )
        session.commit()

        validation = validate_report_readiness(session, expense_report_id=report_id)

    errors = [i for i in validation.issues if i.severity == "error"]
    assert [i.code for i in errors] == []
    assert validation.ready is True


def test_validation_issue_contains_review_row_locator_context(isolated_db):
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
        row = session.get(ReviewRow, row_id)
        validation = validate_report_readiness(session, expense_report_id=report_id)

    issue = next(i for i in validation.issues if i.code == "missing_attendees_on_meal")
    assert issue.review_row_id == row_id
    assert issue.receipt_id == row.receipt_document_id
    assert issue.statement_transaction_id == row.statement_transaction_id
    assert issue.supplier == "Trattoria"
    assert issue.transaction_date == "2026-04-01"


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


# ─── _is_telecom_row narrowing (PR #83 follow-up) ──────────────────────────
#
# PR #83 introduced `_is_telecom_row` with a substring-based text fallback
# that scanned supplier / category / report_bucket / extracted_supplier /
# original_file_name for telecom-flavoured tokens (vodafone, turkcell,
# internet, gsm, telefon, …). Cross-evaluation found that the fallback
# silently exempted Business rows with non-telecom buckets — e.g. a real
# customer dinner at "Vodafone Park" (a stadium in Istanbul) or any receipt
# whose filename happened to contain "internet" — from the
# `missing_business_reason` hard block. These tests pin the narrowed
# behavior: the text fallback is gated on a small bucket allow-list AND
# only matches strong, unambiguous telecom signals.


class _ReceiptStub:
    """Minimal stand-in for ReceiptDocument used by `_is_telecom_row` unit tests."""

    def __init__(
        self,
        *,
        report_bucket: str | None = None,
        extracted_supplier: str | None = None,
        original_file_name: str | None = None,
    ) -> None:
        self.report_bucket = report_bucket
        self.extracted_supplier = extracted_supplier
        self.original_file_name = original_file_name


def test_is_telecom_row_false_for_other_bucket_with_weak_brand_in_supplier():
    # Vodafone Park is a real stadium in Istanbul — a legitimate non-telecom
    # venue. The narrowed heuristic must not treat the row as telecom just
    # because the supplier string contains "vodafone".
    confirmed = {"report_bucket": "Other", "supplier": "Vodafone Park"}
    assert _is_telecom_row(confirmed, None) is False


def test_is_telecom_row_false_for_meal_bucket_regardless_of_supplier_text():
    # Even if the supplier text mentions "Turkcell", a Dinner-bucketed row
    # is a meal — the bucket itself rules out telecom.
    confirmed = {"report_bucket": "Dinner", "supplier": "Turkcell Müşteri Yemeği"}
    assert _is_telecom_row(confirmed, None) is False


def test_is_telecom_row_false_for_meal_bucket_with_strong_token_in_supplier():
    # Isolates the bucket allow-list guard from the strong-token list:
    # even a strong, unambiguous telecom phrase ("türk telekom") in the
    # supplier must NOT flip a Dinner-bucketed row to telecom. A future
    # regression that drops the bucket short-circuit while keeping the
    # strong-only token list would silently re-open the loophole on meal
    # rows whose name happens to mention a phone-bill phrase — this test
    # would then fail.
    confirmed = {"report_bucket": "Dinner", "supplier": "Türk Telekom corporate cafe lunch"}
    assert _is_telecom_row(confirmed, None) is False


def test_is_telecom_row_false_for_entertainment_bucket_with_internet_in_filename():
    # Filename-based matches are easy to trigger by accident. Entertainment
    # bucket must rule out the heuristic.
    confirmed = {"report_bucket": "Entertainment"}
    receipt = _ReceiptStub(report_bucket="Entertainment", original_file_name="internet_cafe.jpg")
    assert _is_telecom_row(confirmed, receipt) is False


def test_is_telecom_row_true_for_telephone_internet_bucket():
    # Preserve PR #83 behavior: when the operator (or the bucket suggester)
    # has classified the row as Telephone/Internet, the row is telecom and
    # business_reason is implicit in the supplier (the phone bill itself).
    confirmed = {"report_bucket": "Telephone/Internet", "supplier": "Vodafone fatura"}
    assert _is_telecom_row(confirmed, None) is True


def test_is_telecom_row_true_for_unclassified_row_with_strong_telecom_signal():
    # Truly unclassified rows (bucket="") may still leverage the text
    # fallback — but only on strong, unambiguous signals like
    # "fatura tahsilatı" (Turkish for "bill collection") or full brand
    # phrases like "turk telekom".
    confirmed = {"report_bucket": "", "supplier": "Turkcell fatura tahsilatı"}
    assert _is_telecom_row(confirmed, None) is True


def test_is_telecom_row_false_for_unclassified_row_with_weak_brand_only():
    # Negative control for the strong-signal rule: a fresh, unclassified
    # row whose only telecom evidence is the brand name "Vodafone" alone
    # (no "fatura", no "tahsilatı", no full ISP brand phrase) must NOT
    # be auto-classified as telecom.
    confirmed = {"report_bucket": "", "supplier": "Vodafone Park"}
    assert _is_telecom_row(confirmed, None) is False


def test_is_telecom_row_true_for_other_bucket_with_strong_signal():
    # The catch-all "Other" bucket is in the text-fallback allow-list, so
    # a strong, unambiguous signal like "fatura tahsilatı" still flips
    # is_telecom to True there. This pins the surviving exemption surface
    # so a future tightening (e.g. dropping "other" from the allow-list)
    # is a deliberate, test-driven decision rather than an accident.
    confirmed = {"report_bucket": "Other", "supplier": "Turkcell fatura tahsilatı"}
    assert _is_telecom_row(confirmed, None) is True


def test_vodafone_park_business_row_with_other_bucket_blocks_on_missing_business_reason(isolated_db):
    # Integration check: the loophole closed by this PR. A Business row at
    # "Vodafone Park" with bucket="Other" and no business_reason must now
    # surface `missing_business_reason` instead of silently being treated
    # as telecom and skipping the gate.
    with Session(isolated_db) as session:
        report_id, row_id = _seed_confirmed_row(
            session,
            bucket="Other",
            business_or_personal="Business",
            business_reason=None,
            attendees=None,
            amount=Decimal("250.0"),
            supplier="Vodafone Park",
        )
        validation = validate_report_readiness(session, expense_report_id=report_id)

    codes = [i.code for i in validation.issues]
    assert "missing_business_reason" in codes, (
        f"Vodafone Park (a stadium) with bucket=Other and empty business_reason "
        f"must still emit missing_business_reason; got issues={codes}"
    )
    block = next(i for i in validation.issues if i.code == "missing_business_reason")
    assert block.severity == "error"
    assert validation.ready is False


# ─── suggest_bucket narrowing (Codex BLOCK follow-up) ──────────────────────
#
# Codex independent review of PR #84 found that even though the validator's
# `_is_telecom_row` was tightened, `suggest_bucket()` in
# `app/services/merchant_buckets.py` was still auto-classifying any draft
# row whose supplier contained `vodafone | turkcell | internet | gsm |
# fatura` as `Telephone/Internet`. That auto-classification then short-
# circuited `_is_telecom_row` on the bucket, silently bypassing
# `missing_business_reason` for the very loophole PR #84 was supposed to
# fix. These tests pin the suggester now matching the same strong-only
# token list as `TELECOM_TEXT_TOKENS`.


def test_suggest_bucket_vodafone_park_is_not_telecom():
    from app.services.merchant_buckets import suggest_bucket
    # Real Istanbul stadium — must not be auto-classified as telecom.
    assert suggest_bucket("Vodafone Park") != "Telephone/Internet"


def test_suggest_bucket_turkcell_musteri_yemegi_is_not_telecom():
    from app.services.merchant_buckets import suggest_bucket
    # "Turkcell Customer Meal" — sponsored event meal, not a phone bill.
    assert suggest_bucket("Turkcell Müşteri Yemeği") != "Telephone/Internet"


def test_suggest_bucket_vodafone_fatura_tahsilati_is_telecom():
    from app.services.merchant_buckets import suggest_bucket
    # "Vodafone bill collection" — strong, unambiguous phone-bill phrase.
    assert suggest_bucket("Vodafone fatura tahsilatı") == "Telephone/Internet"


def test_suggest_bucket_turk_telekom_is_telecom():
    from app.services.merchant_buckets import suggest_bucket
    # Full ISP brand name — strong, unambiguous telecom signal.
    assert suggest_bucket("Türk Telekom") == "Telephone/Internet"


# ─── End-to-end via the real review-session sync path ──────────────────────
#
# The unit-level tests above pin the suggester and the validator each in
# isolation. This e2e test pins the chain — suggester → review-session sync
# → validator — by going through `get_or_create_review_session`. A future
# regression where a unit test passes but the real initializer still leaks
# (the exact failure mode Codex caught on PR #84) would now surface here.


def test_vodafone_park_through_real_sync_path_blocks_on_missing_business_reason(isolated_db):
    from app.services.review_sessions import (
        get_or_create_review_session,
        review_rows,
    )

    with Session(isolated_db) as session:
        user = AppUser(telegram_user_id=1 + hash(uuid4().hex) % 10_000, display_name="E2E")
        session.add(user)
        session.commit()
        session.refresh(user)

        statement = StatementImport(
            source_filename=f"e2e_{uuid4().hex[:6]}.xlsx",
            row_count=1,
            uploader_user_id=user.id,
        )
        session.add(statement)
        session.commit()
        session.refresh(statement)

        tx = StatementTransaction(
            statement_import_id=statement.id,
            transaction_date=date(2026, 4, 1),
            supplier_raw="Vodafone Park",
            supplier_normalized="VODAFONE PARK",
            local_currency="USD",
            local_amount=Decimal("250.0"),
            usd_amount=Decimal("250.0"),
        )
        receipt = ReceiptDocument(
            source="test",
            status="imported",
            content_type="photo",
            original_file_name="dinner.jpg",
            extracted_date=date(2026, 4, 1),
            extracted_supplier="Vodafone Park",
            extracted_local_amount=Decimal("250.0"),
            extracted_currency="USD",
            business_or_personal="Business",
            # report_bucket left as None so _row_payload calls
            # suggest_bucket() on the supplier — the exact pre-fix path.
            report_bucket=None,
            business_reason=None,
            attendees=None,
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
            match_method="e2e_test",
            approved=True,
            reason="end-to-end sync-path fixture",
        )
        session.add(decision)
        session.commit()

        report = ExpenseReport(
            owner_user_id=user.id,
            report_kind="diners_statement",
            title="E2E Vodafone Park",
            status="draft",
            report_currency="USD",
            statement_import_id=statement.id,
        )
        session.add(report)
        session.commit()
        session.refresh(report)

        # Real sync path. Inside _sync_review_rows → _row_payload, the
        # suggested payload is `receipt.report_bucket or suggest_bucket(...)`.
        # Pre-fix, suggest_bucket("Vodafone Park") returned
        # "Telephone/Internet" via the weak-token rule (vodafone), and the
        # validator silently skipped missing_business_reason from there.
        review = get_or_create_review_session(session, expense_report_id=report.id)
        rows = review_rows(session, review.id)
        assert len(rows) == 1, f"expected exactly one synced row, got {len(rows)}"
        row = rows[0]
        confirmed = json.loads(row.confirmed_json or "{}")

        # Assertion 1: the suggester does NOT pre-classify Vodafone Park
        # as a telecom row. After the fix `suggest_bucket("Vodafone Park")`
        # returns None / a non-telecom bucket; either is acceptable, the
        # only forbidden value is "Telephone/Internet".
        assert confirmed.get("report_bucket") != "Telephone/Internet", (
            "Suggester silently classified Vodafone Park as Telephone/Internet "
            "via the real review-session sync path — the loophole Codex flagged "
            "is still open. Got bucket="
            f"{confirmed.get('report_bucket')!r}."
        )

        # Assertion 2: validate_report_readiness emits missing_business_reason
        # for this row. validate_report_readiness reads the row's
        # confirmed_json directly (it does not require a confirmed snapshot
        # for the business-row checks; the snapshot is only required for the
        # air-travel block), so this exercises the full chain.
        validation = validate_report_readiness(session, expense_report_id=report.id)

    codes = [i.code for i in validation.issues]
    assert "missing_business_reason" in codes, (
        "missing_business_reason must fire end-to-end for a Business + Vodafone Park "
        "row built through the real sync path with empty business_reason. Without "
        "this, a future regression of the suggester would silently let the row pass. "
        f"Got codes={codes}."
    )


# ─── Whitespace edge case in `_is_telecom_row` ─────────────────────────────
#
# The `or` chain in `_is_telecom_row` previously treated the string "   "
# as truthy, so a row whose confirmed bucket had been cleared to whitespace
# would NEVER fall back to `receipt.report_bucket`. The whitespace string
# would normalize to "" downstream, putting the row in the text-fallback
# allow-list with no real bucket — different code path than the equivalent
# None/"" cases. These tests pin both the non-telecom and telecom fallback
# directions so the normalization is intentional, not incidental.


def test_is_telecom_row_whitespace_confirmed_bucket_falls_back_to_non_telecom_receipt():
    # The operator cleared the bucket to whitespace; the receipt-side
    # bucket is a real non-telecom value (Dinner). After normalization the
    # row should be treated as Dinner (a meal bucket) — telecom=False so
    # missing_business_reason and missing_attendees_on_meal still fire.
    confirmed = {"report_bucket": "   ", "supplier": "Vodafone Park"}
    receipt = _ReceiptStub(report_bucket="Dinner")
    assert _is_telecom_row(confirmed, receipt) is False


def test_is_telecom_row_whitespace_confirmed_bucket_falls_back_to_telecom_receipt():
    # Symmetric case: the operator cleared the bucket to whitespace, but
    # the receipt-side bucket is genuinely Telephone/Internet. After
    # normalization the row IS telecom — the supplier-IS-the-reason
    # exemption applies and missing_business_reason is correctly skipped.
    confirmed = {"report_bucket": "   ", "supplier": "Vodafone fatura"}
    receipt = _ReceiptStub(report_bucket="Telephone/Internet")
    assert _is_telecom_row(confirmed, receipt) is True
