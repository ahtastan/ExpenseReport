"""F-AI-0b-2 Review Queue AI display contract tests.

These tests assert that the public ``source.ai_review`` block on a review-row
payload matches the documented advisory shape:

  * absent  -> key omitted entirely
  * pass    -> {status: pass, risk_level: pass, ...}
  * warn    -> includes non-empty differences
  * block   -> advisory only; never sets attention_required
  * stale   -> canonical_snapshot_hash mismatch
  * malformed -> latest run failed / completed-without-comparison

All fixtures are synthetic. No live model calls. No real receipt files.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

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
from app.services.review_sessions import (
    get_or_create_review_session,
    review_rows,
    session_payload,
)
from _pivot_helpers import ensure_expense_report_for_statement


def _seed_matched_receipt(
    session: Session,
    *,
    receipt_amount: Decimal = Decimal("12.34"),
    receipt_currency: str = "USD",
    receipt_date_value: date = date(2026, 4, 1),
    receipt_supplier: str = "Privacy Cafe",
    business_or_personal: str | None = "Business",
    business_reason: str | None = "Demo trip",
    attendees: str | None = "Demo Team",
) -> tuple[int, ReceiptDocument]:
    """Seed user/statement/tx/receipt/match for one matched review row.

    Returns ``(statement_import_id, receipt)``. The caller can then attach
    AgentDB rows to that receipt via the ``_seed_agent_*`` helpers.
    """
    user = AppUser(display_name="ai-display-test")
    session.add(user)
    session.flush()

    statement = StatementImport(
        source_filename="ai_display_statement.xlsx",
        row_count=1,
        uploader_user_id=user.id,
    )
    session.add(statement)
    session.flush()

    tx = StatementTransaction(
        statement_import_id=statement.id,
        transaction_date=receipt_date_value,
        supplier_raw=receipt_supplier,
        supplier_normalized=receipt_supplier.upper(),
        local_currency=receipt_currency,
        local_amount=receipt_amount,
        usd_amount=receipt_amount,
        source_row_ref="row-1",
    )
    receipt = ReceiptDocument(
        uploader_user_id=user.id,
        source="test",
        status="imported",
        content_type="photo",
        original_file_name="synthetic_receipt.jpg",
        extracted_date=receipt_date_value,
        extracted_supplier=receipt_supplier,
        extracted_local_amount=receipt_amount,
        extracted_currency=receipt_currency,
        business_or_personal=business_or_personal,
        report_bucket="Meals/Snacks",
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
        reason="ai-display-test fixture",
    )
    session.add(decision)
    session.commit()
    return statement.id, receipt


def _seed_unmatched_statement(session: Session) -> int:
    """Seed a statement with one transaction and no matched receipt."""
    user = AppUser(display_name="ai-display-test-unmatched")
    session.add(user)
    session.flush()

    statement = StatementImport(
        source_filename="ai_display_unmatched.xlsx",
        row_count=1,
        uploader_user_id=user.id,
    )
    session.add(statement)
    session.flush()

    tx = StatementTransaction(
        statement_import_id=statement.id,
        transaction_date=date(2026, 4, 2),
        supplier_raw="Unmatched Vendor",
        supplier_normalized="UNMATCHED VENDOR",
        local_currency="USD",
        local_amount=Decimal("9.99"),
        usd_amount=Decimal("9.99"),
        source_row_ref="row-1",
    )
    session.add(tx)
    session.commit()
    session.refresh(statement)
    return statement.id


def _seed_agent_completed(
    session: Session,
    *,
    receipt: ReceiptDocument,
    risk_level: str,
    differences: list[str],
    suggested_user_message: str | None,
    canonical_snapshot_hash_override: str | None = None,
    completed_at: datetime | None = None,
    recommended_action: str = "accept",
    agent_amount: str | None = None,
    agent_currency: str | None = None,
    agent_date: date | None = None,
    agent_supplier: str | None = None,
) -> AgentReceiptComparison:
    snapshot = build_canonical_receipt_snapshot(receipt)
    snapshot_json = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
    snapshot_hash = canonical_snapshot_hash_override or canonical_receipt_snapshot_hash(snapshot)
    completed_at = completed_at or datetime.now(timezone.utc)
    run = AgentReceiptReviewRun(
        receipt_document_id=receipt.id or 0,
        run_source="test",
        run_kind="receipt_second_read",
        status="completed",
        schema_version="0a",
        prompt_version="agent_receipt_review_prompt_0a",
        prompt_hash="x" * 64,
        model_provider=None,
        model_name="local_mock",
        comparator_version="agent_receipt_comparator_0a",
        canonical_snapshot_json=snapshot_json,
        input_hash="y" * 64,
        raw_model_json=None,
        raw_model_json_redacted=True,
        prompt_text=None,
        started_at=completed_at,
        completed_at=completed_at,
    )
    session.add(run)
    session.flush()

    read_row = AgentReceiptRead(
        run_id=run.id or 0,
        receipt_document_id=receipt.id or 0,
        read_schema_version="0a",
        read_json=json.dumps({}, sort_keys=True),
        extracted_date=agent_date,
        extracted_supplier=agent_supplier,
        amount_text=None,
        local_amount_decimal=agent_amount,
        local_amount_minor=None,
        amount_scale=None,
        currency=agent_currency,
        receipt_type=None,
        business_or_personal=None,
        business_reason=None,
        attendees_json=None,
        confidence_json=None,
        evidence_json=None,
        warnings_json=None,
    )
    session.add(read_row)
    session.flush()

    comparison = AgentReceiptComparison(
        run_id=run.id or 0,
        agent_receipt_read_id=read_row.id or 0,
        receipt_document_id=receipt.id or 0,
        comparator_version="agent_receipt_comparator_0a",
        risk_level=risk_level,
        recommended_action=recommended_action,
        attention_required=risk_level != "pass",
        amount_status="match" if "amount" not in {d.split("_")[0] for d in differences} else "mismatch",
        date_status=None,
        currency_status=None,
        supplier_status=None,
        business_context_status=None,
        differences_json=json.dumps(differences, sort_keys=True),
        suggested_user_message=suggested_user_message,
        ai_review_note=None,
        canonical_snapshot_hash=snapshot_hash,
        agent_read_hash="z" * 64,
    )
    session.add(comparison)
    session.commit()
    session.refresh(comparison)
    return comparison


def _seed_agent_failed_only(session: Session, *, receipt: ReceiptDocument) -> AgentReceiptReviewRun:
    completed_at = datetime.now(timezone.utc)
    run = AgentReceiptReviewRun(
        receipt_document_id=receipt.id or 0,
        run_source="test",
        run_kind="receipt_second_read",
        status="failed",
        schema_version="0a",
        prompt_version="agent_receipt_review_prompt_0a",
        prompt_hash="f" * 64,
        comparator_version="agent_receipt_comparator_0a",
        canonical_snapshot_json="{}",
        raw_model_json_redacted=True,
        error_code="agent_review_failed",
        error_message="synthetic failure",
        started_at=completed_at,
        completed_at=completed_at,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def _row_payload(session: Session, statement_id: int) -> dict:
    expense_report_id = ensure_expense_report_for_statement(session, statement_id)
    review = get_or_create_review_session(session, expense_report_id=expense_report_id)
    rows = session_payload(session, review)["rows"]
    assert len(rows) == 1
    return rows[0]


def test_no_ai_review_means_ai_review_key_is_omitted() -> None:
    with Session(engine) as session:
        statement_id, _ = _seed_matched_receipt(session)
        payload = _row_payload(session, statement_id)
        assert "ai_review" not in payload["source"]


def test_pass_review_omits_differences_and_summary() -> None:
    with Session(engine) as session:
        statement_id, receipt = _seed_matched_receipt(session)
        _seed_agent_completed(
            session,
            receipt=receipt,
            risk_level="pass",
            differences=[],
            suggested_user_message=None,
            recommended_action="accept",
        )
        payload = _row_payload(session, statement_id)
        ai = payload["source"]["ai_review"]
        assert ai["status"] == "pass"
        assert ai["label"] == "AI second read"
        assert ai["risk_level"] == "pass"
        assert ai["recommended_action"] == "accept"
        assert "differences" not in ai
        assert "summary" not in ai


def test_warn_review_includes_non_empty_differences_with_field_and_severity() -> None:
    with Session(engine) as session:
        statement_id, receipt = _seed_matched_receipt(session)
        _seed_agent_completed(
            session,
            receipt=receipt,
            risk_level="warn",
            differences=["date_mismatch", "supplier_mismatch"],
            suggested_user_message="Check the date and supplier name.",
            recommended_action="manual_review",
            agent_amount=str(receipt.extracted_local_amount),
            agent_currency=receipt.extracted_currency,
            agent_date=date(2099, 1, 1),
            agent_supplier="Different Cafe Header",
        )
        payload = _row_payload(session, statement_id)
        ai = payload["source"]["ai_review"]
        assert ai["status"] == "warn"
        assert ai["risk_level"] == "warn"
        # manual_review -> public "review"
        assert ai["recommended_action"] == "review"
        assert ai["summary"] == "Check the date and supplier name."
        codes = [d["code"] for d in ai["differences"]]
        assert codes == ["date_mismatch", "supplier_mismatch"]
        date_diff = next(d for d in ai["differences"] if d["code"] == "date_mismatch")
        assert date_diff["field"] == "date"
        assert date_diff["severity"] == "warn"
        assert date_diff["agent_value"] == "2099-01-01"
        assert date_diff["canonical_value"] == receipt.extracted_date.isoformat()


def test_block_review_is_advisory_and_exposes_block_severity() -> None:
    with Session(engine) as session:
        statement_id, receipt = _seed_matched_receipt(session)
        _seed_agent_completed(
            session,
            receipt=receipt,
            risk_level="block",
            differences=["amount_mismatch"],
            suggested_user_message="Receipt amount does not match canonical OCR.",
            recommended_action="block_report",
            agent_amount="999.99",
            agent_currency=receipt.extracted_currency,
        )
        payload = _row_payload(session, statement_id)
        ai = payload["source"]["ai_review"]
        assert ai["status"] == "block"
        assert ai["risk_level"] == "block"
        assert ai["recommended_action"] == "block_report"
        assert ai["summary"] == "Receipt amount does not match canonical OCR."
        amount_diff = ai["differences"][0]
        assert amount_diff["code"] == "amount_mismatch"
        assert amount_diff["field"] == "amount"
        assert amount_diff["severity"] == "block"
        assert amount_diff["agent_value"] == "999.99"
        assert amount_diff["canonical_value"] == str(receipt.extracted_local_amount)


def test_stale_review_when_receipt_edited_after_run() -> None:
    with Session(engine) as session:
        statement_id, receipt = _seed_matched_receipt(session)
        # Seed a completed comparison with a hash that does NOT match the
        # current canonical snapshot.
        _seed_agent_completed(
            session,
            receipt=receipt,
            risk_level="pass",
            differences=[],
            suggested_user_message=None,
            canonical_snapshot_hash_override="0" * 64,
        )
        payload = _row_payload(session, statement_id)
        ai = payload["source"]["ai_review"]
        assert ai["status"] == "stale"
        assert ai["label"] == "AI second read"
        # Stale rows must NOT expose risk_level / differences / summary,
        # because the comparison no longer reflects current canonical fields.
        assert "risk_level" not in ai
        assert "differences" not in ai
        assert "summary" not in ai
        assert "agent_read" not in ai
        assert "canonical" not in ai


def test_malformed_when_only_failed_run_exists() -> None:
    with Session(engine) as session:
        statement_id, receipt = _seed_matched_receipt(session)
        _seed_agent_failed_only(session, receipt=receipt)
        payload = _row_payload(session, statement_id)
        ai = payload["source"]["ai_review"]
        assert ai["status"] == "malformed"
        assert ai["label"] == "AI second read"
        assert "risk_level" not in ai
        assert "differences" not in ai
        assert "summary" not in ai


def test_malformed_when_unknown_risk_level_in_completed_row() -> None:
    with Session(engine) as session:
        statement_id, receipt = _seed_matched_receipt(session)
        _seed_agent_completed(
            session,
            receipt=receipt,
            risk_level="catastrophe",  # not in pass/warn/block whitelist
            differences=[],
            suggested_user_message=None,
        )
        payload = _row_payload(session, statement_id)
        ai = payload["source"]["ai_review"]
        assert ai["status"] == "malformed"


def test_unmatched_statement_row_has_no_ai_review_key() -> None:
    with Session(engine) as session:
        statement_id = _seed_unmatched_statement(session)
        expense_report_id = ensure_expense_report_for_statement(session, statement_id)
        review = get_or_create_review_session(session, expense_report_id=expense_report_id)
        rows = session_payload(session, review)["rows"]
        assert len(rows) == 1
        assert "ai_review" not in rows[0]["source"]


def test_latest_completed_comparison_wins_when_multiple_runs_exist() -> None:
    with Session(engine) as session:
        statement_id, receipt = _seed_matched_receipt(session)
        # Earlier completed run: pass.
        _seed_agent_completed(
            session,
            receipt=receipt,
            risk_level="pass",
            differences=[],
            suggested_user_message=None,
            completed_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        # Later completed run: warn. Should win.
        _seed_agent_completed(
            session,
            receipt=receipt,
            risk_level="warn",
            differences=["date_mismatch"],
            suggested_user_message="Date drift.",
            recommended_action="manual_review",
            completed_at=datetime.now(timezone.utc),
            agent_date=date(2099, 12, 31),
        )
        # An even later run that's failed must NOT replace the latest completed.
        _seed_agent_failed_only(session, receipt=receipt)
        payload = _row_payload(session, statement_id)
        ai = payload["source"]["ai_review"]
        assert ai["status"] == "warn"
        assert ai["summary"] == "Date drift."


def test_suggested_user_message_omitted_when_empty_string() -> None:
    with Session(engine) as session:
        statement_id, receipt = _seed_matched_receipt(session)
        _seed_agent_completed(
            session,
            receipt=receipt,
            risk_level="warn",
            differences=["date_mismatch"],
            suggested_user_message="   ",  # whitespace only -> treat as empty
            recommended_action="manual_review",
            agent_date=date(2099, 1, 1),
        )
        payload = _row_payload(session, statement_id)
        ai = payload["source"]["ai_review"]
        assert "summary" not in ai


def test_review_payload_never_leaks_internal_ai_fields() -> None:
    """Spot-check: source.ai_review must not contain any of the audit-only
    fields stored on AgentDB rows (prompt text, raw model JSON, hashes, etc.)."""
    with Session(engine) as session:
        statement_id, receipt = _seed_matched_receipt(session)
        _seed_agent_completed(
            session,
            receipt=receipt,
            risk_level="warn",
            differences=["supplier_mismatch"],
            suggested_user_message="Supplier text differs.",
            recommended_action="manual_review",
            agent_supplier="Some Other Cafe",
        )
        payload = _row_payload(session, statement_id)
        ai = payload["source"]["ai_review"]
        forbidden = {
            "prompt_text",
            "raw_model_json",
            "canonical_snapshot_hash",
            "agent_read_hash",
            "prompt_hash",
            "input_hash",
            "model_response_json",
            "model_debug_json",
            "evidence_json",
            "warnings_json",
            "confidence_json",
            "merchant_address",
            "raw_text_summary",
            "line_items",
            "ai_review_note",
            "comparison_id",
            "run_id",
            "app_git_sha",
            "model_name",
            "model_provider",
        }
        assert forbidden.isdisjoint(ai.keys())
        if "agent_read" in ai:
            assert forbidden.isdisjoint(ai["agent_read"].keys())
        if "canonical" in ai:
            assert forbidden.isdisjoint(ai["canonical"].keys())


