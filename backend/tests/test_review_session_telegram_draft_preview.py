"""F-AI-TG-2 review-row Telegram draft preview integration tests.

Pin the contract that ``source.telegram_draft`` is emitted on a review-row
payload exactly when the F-AI-TG-0 draft engine yields one, and that the
new field never affects gating logic (``attention_required``,
``confirm_review_session``, ``validate_report_readiness``).

All fixtures synthetic. No live model calls. No Telegram client. No prod DB.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlmodel import Session

from app.db import engine
from app.models import (
    AgentReceiptComparison,
    AgentReceiptRead,
    AgentReceiptReviewRun,
    AppUser,
    MatchDecision,
    ReceiptDocument,
    StatementImport,
    StatementTransaction,
)
from app.services.agent_receipt_review_persistence import (
    build_canonical_receipt_snapshot,
    canonical_receipt_snapshot_hash,
)
from app.services.report_validation import validate_report_readiness
from app.services.review_sessions import (
    confirm_review_session,
    get_or_create_review_session,
    review_rows,
    session_payload,
)
from _pivot_helpers import ensure_expense_report_for_statement


def _seed_matched(
    session: Session,
    *,
    receipt_amount: Decimal = Decimal("12.34"),
    receipt_currency: str = "USD",
    receipt_date_value: date = date(2026, 4, 30),
    statement_amount: Decimal | None = None,
    statement_date: date | None = None,
    business_or_personal: str = "Business",
    business_reason: str | None = "Project meeting",
    attendees: str | None = "Hakan",
    report_bucket: str = "Hotel/Lodging/Laundry",
) -> tuple[int, ReceiptDocument]:
    user = AppUser(display_name="tg2-test")
    session.add(user)
    session.flush()

    statement = StatementImport(
        source_filename="tg2.xlsx",
        row_count=1,
        uploader_user_id=user.id,
    )
    session.add(statement)
    session.flush()

    tx = StatementTransaction(
        statement_import_id=statement.id,
        transaction_date=statement_date or receipt_date_value,
        supplier_raw="Smoke Cafe",
        supplier_normalized="SMOKE CAFE",
        local_currency=receipt_currency,
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
        extracted_date=receipt_date_value,
        extracted_supplier="Smoke Cafe",
        extracted_local_amount=receipt_amount,
        extracted_currency=receipt_currency,
        business_or_personal=business_or_personal,
        report_bucket=report_bucket,
        business_reason=business_reason,
        attendees=attendees,
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
        reason="tg2 fixture",
    )
    session.add(decision)
    session.commit()
    return statement.id, receipt


def _seed_agent_warn(session: Session, *, receipt: ReceiptDocument) -> AgentReceiptComparison:
    snapshot = build_canonical_receipt_snapshot(receipt)
    snapshot_json = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
    snapshot_hash = canonical_receipt_snapshot_hash(snapshot)
    now = datetime.now(timezone.utc)
    run = AgentReceiptReviewRun(
        receipt_document_id=receipt.id or 0,
        run_source="test",
        run_kind="receipt_second_read",
        status="completed",
        schema_version="0a",
        prompt_version="agent_receipt_review_prompt_0a",
        comparator_version="agent_receipt_comparator_0a",
        canonical_snapshot_json=snapshot_json,
        raw_model_json_redacted=True,
        started_at=now,
        completed_at=now,
    )
    session.add(run)
    session.flush()
    read_row = AgentReceiptRead(
        run_id=run.id or 0,
        receipt_document_id=receipt.id or 0,
        read_schema_version="0a",
        read_json="{}",
    )
    session.add(read_row)
    session.flush()
    comparison = AgentReceiptComparison(
        run_id=run.id or 0,
        agent_receipt_read_id=read_row.id or 0,
        receipt_document_id=receipt.id or 0,
        comparator_version="agent_receipt_comparator_0a",
        risk_level="warn",
        recommended_action="manual_review",
        attention_required=True,
        differences_json=json.dumps(["supplier_mismatch"]),
        suggested_user_message=None,
        canonical_snapshot_hash=snapshot_hash,
    )
    session.add(comparison)
    session.commit()
    return comparison


def _row_payload(session: Session, statement_id: int) -> dict:
    expense_report_id = ensure_expense_report_for_statement(session, statement_id)
    review = get_or_create_review_session(session, expense_report_id=expense_report_id)
    rows = session_payload(session, review)["rows"]
    assert len(rows) == 1
    return rows[0]


# ---------------------------------------------------------------------------
# happy-path: each kind of draft surfaces under source.telegram_draft
# ---------------------------------------------------------------------------


def test_amount_mismatch_row_includes_blocker_telegram_draft() -> None:
    with Session(engine) as session:
        statement_id, _ = _seed_matched(
            session,
            receipt_amount=Decimal("10.00"),
            statement_amount=Decimal("999.99"),
        )
        row = _row_payload(session, statement_id)
        draft = row["source"].get("telegram_draft")
        assert draft is not None
        assert draft["kind"] == "amount_mismatch"
        assert draft["severity"] == "blocker"
        assert draft["send_allowed"] is False
        assert "review queue" in draft["text"].lower()


def test_date_mismatch_row_includes_warning_telegram_draft() -> None:
    with Session(engine) as session:
        statement_id, _ = _seed_matched(
            session,
            receipt_date_value=date(2026, 1, 1),
            statement_date=date(2026, 12, 1),  # > 3 day tolerance window
        )
        row = _row_payload(session, statement_id)
        draft = row["source"].get("telegram_draft")
        assert draft is not None
        assert draft["kind"] == "date_mismatch"
        assert draft["severity"] == "warning"
        assert draft["send_allowed"] is False


def test_ai_advisory_warning_row_includes_info_telegram_draft() -> None:
    with Session(engine) as session:
        statement_id, receipt = _seed_matched(session)
        _seed_agent_warn(session, receipt=receipt)
        row = _row_payload(session, statement_id)
        draft = row["source"].get("telegram_draft")
        assert draft is not None
        assert draft["kind"] == "ai_advisory_warning"
        assert draft["severity"] == "info"
        assert draft["send_allowed"] is False


def test_missing_business_reason_row_includes_warning_telegram_draft() -> None:
    with Session(engine) as session:
        statement_id, _ = _seed_matched(
            session,
            business_or_personal="Business",
            business_reason=None,
        )
        row = _row_payload(session, statement_id)
        draft = row["source"].get("telegram_draft")
        assert draft is not None
        assert draft["kind"] == "missing_business_reason"
        assert draft["severity"] == "warning"
        assert draft["send_allowed"] is False


# ---------------------------------------------------------------------------
# clean rows omit the field
# ---------------------------------------------------------------------------


def test_clean_business_row_with_no_issue_omits_telegram_draft() -> None:
    with Session(engine) as session:
        statement_id, _ = _seed_matched(
            session,
            business_or_personal="Business",
            business_reason="Project meeting",
            attendees="Hakan",
            report_bucket="Hotel/Lodging/Laundry",
        )
        row = _row_payload(session, statement_id)
        assert "telegram_draft" not in row["source"]


def test_clean_personal_row_omits_telegram_draft() -> None:
    with Session(engine) as session:
        statement_id, _ = _seed_matched(
            session,
            business_or_personal="Personal",
            business_reason=None,
            attendees=None,
        )
        row = _row_payload(session, statement_id)
        assert "telegram_draft" not in row["source"]


# ---------------------------------------------------------------------------
# safety contract: send_allowed, forbidden phrases, leak guard
# ---------------------------------------------------------------------------


def test_every_emitted_telegram_draft_has_send_allowed_false() -> None:
    cases = [
        dict(receipt_amount=Decimal("10.00"), statement_amount=Decimal("999.99")),
        dict(receipt_date_value=date(2026, 1, 1), statement_date=date(2026, 12, 1)),
        dict(business_reason=None),
        dict(report_bucket="Lunch", attendees=None),
    ]
    for kwargs in cases:
        with Session(engine) as session:
            statement_id, _ = _seed_matched(session, **kwargs)
            row = _row_payload(session, statement_id)
            draft = row["source"].get("telegram_draft")
            if draft is not None:
                assert draft["send_allowed"] is False


def test_telegram_draft_text_has_no_forbidden_phrases() -> None:
    forbidden = (
        "AI approved",
        "AI rejected",
        "report blocked by AI",
        "sent to Telegram",
        "Send Telegram",
        "Send to Telegram",
    )
    cases = [
        dict(receipt_amount=Decimal("10.00"), statement_amount=Decimal("999.99")),
        dict(business_reason=None),
        dict(report_bucket="Lunch", attendees=None),
    ]
    for kwargs in cases:
        with Session(engine) as session:
            statement_id, _ = _seed_matched(session, **kwargs)
            row = _row_payload(session, statement_id)
            draft = row["source"].get("telegram_draft")
            if draft is None:
                continue
            text_lower = draft["text"].lower()
            for phrase in forbidden:
                assert phrase.lower() not in text_lower, (
                    f"forbidden phrase {phrase!r} appeared in draft kind {draft['kind']!r}"
                )


def test_telegram_draft_payload_does_not_leak_internal_fields() -> None:
    forbidden_keys = {
        "storage_path",
        "receipt_path",
        "prompt_text",
        "raw_model_json",
        "model_response_json",
        "model_debug_json",
        "canonical_snapshot_hash",
        "agent_read_hash",
    }
    with Session(engine) as session:
        statement_id, receipt = _seed_matched(session)
        _seed_agent_warn(session, receipt=receipt)
        row = _row_payload(session, statement_id)
        draft = row["source"].get("telegram_draft")
        assert draft is not None
        assert forbidden_keys.isdisjoint(draft.keys())
        text = draft["text"]
        for needle in (
            "/var/lib/dcexpense",
            "/opt/dcexpense",
            "C:\\",
            "OPENAI_API_KEY",
            "prompt_text",
            "raw_model_json",
        ):
            assert needle not in text


# ---------------------------------------------------------------------------
# invariants: confirm + validate behaviour unchanged
# ---------------------------------------------------------------------------


def test_telegram_draft_does_not_change_confirm_review_session_behavior() -> None:
    """A row that emits an AI-advisory Telegram draft (info severity) must
    still be confirmable, since the draft is purely advisory."""
    with Session(engine) as session:
        statement_id, receipt = _seed_matched(session)
        _seed_agent_warn(session, receipt=receipt)
        row = _row_payload(session, statement_id)
        assert row["source"].get("telegram_draft") is not None
        assert row["attention_required"] is False
        expense_report_id = ensure_expense_report_for_statement(session, statement_id)
        review = get_or_create_review_session(session, expense_report_id=expense_report_id)
        confirm_review_session(session, review.id or 0)


def test_telegram_draft_does_not_add_validation_issues() -> None:
    """A row that emits an info-severity Telegram draft (AI advisory only)
    must not contribute any new entries to validate_report_readiness."""
    with Session(engine) as session:
        statement_id, receipt = _seed_matched(session)
        _seed_agent_warn(session, receipt=receipt)
        row = _row_payload(session, statement_id)
        assert row["source"].get("telegram_draft") is not None
        expense_report_id = ensure_expense_report_for_statement(session, statement_id)
        review = get_or_create_review_session(session, expense_report_id=expense_report_id)
        confirm_review_session(session, review.id or 0)
        validation = validate_report_readiness(session, expense_report_id=expense_report_id)
        codes = {issue.code for issue in validation.issues}
        for forbidden_code in ("telegram_draft", "ai_advisory_warning", "telegram_send"):
            assert forbidden_code not in codes
        assert validation.ready is True


def test_telegram_draft_blocker_does_not_become_validation_blocker() -> None:
    """An amount-mismatch row already has a deterministic safety blocker
    from PR #55. The new Telegram draft (kind=amount_mismatch, severity=
    blocker) must NOT add a separate entry to validation issues."""
    with Session(engine) as session:
        statement_id, _ = _seed_matched(
            session,
            receipt_amount=Decimal("10.00"),
            statement_amount=Decimal("999.99"),
        )
        row = _row_payload(session, statement_id)
        draft = row["source"].get("telegram_draft")
        assert draft is not None
        assert draft["kind"] == "amount_mismatch"
        # PR #55 deterministic safety still drives attention.
        assert row["attention_required"] is True
        expense_report_id = ensure_expense_report_for_statement(session, statement_id)
        validation = validate_report_readiness(session, expense_report_id=expense_report_id)
        codes = {issue.code for issue in validation.issues}
        # Validation should still surface the deterministic safety code,
        # not a Telegram-draft-specific code.
        assert "receipt_statement_amount_mismatch" in codes
        for forbidden_code in ("telegram_draft", "telegram_amount_mismatch"):
            assert forbidden_code not in codes
