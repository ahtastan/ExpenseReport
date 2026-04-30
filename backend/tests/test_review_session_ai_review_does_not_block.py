"""F-AI-0b-2 invariant: AI second-read is advisory only.

These tests assert that an AI ``risk_level=block`` row cannot:
  * set ``attention_required`` on the review row,
  * cause ``confirm_review_session()`` to raise,
  * add any entry to ``validate_report_readiness().issues``.

Deterministic safety blockers from PR #55 (receipt-vs-statement mismatches)
remain the only thing that gates confirmation and report readiness.
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
    update_review_row,
)
from _pivot_helpers import ensure_expense_report_for_statement


def _seed_matched_receipt(
    session: Session,
    *,
    canonical_amount: Decimal,
    canonical_currency: str,
    canonical_date: date,
    statement_amount: Decimal | None = None,
    statement_currency: str | None = None,
    statement_date: date | None = None,
    business_or_personal: str = "Business",
    business_reason: str | None = "Demo trip",
    attendees: str | None = "Demo Team",
) -> tuple[int, ReceiptDocument]:
    user = AppUser(display_name="ai-block-test")
    session.add(user)
    session.flush()

    statement = StatementImport(
        source_filename="ai_block_statement.xlsx",
        row_count=1,
        uploader_user_id=user.id,
    )
    session.add(statement)
    session.flush()

    tx = StatementTransaction(
        statement_import_id=statement.id,
        transaction_date=statement_date or canonical_date,
        supplier_raw="Block Cafe",
        supplier_normalized="BLOCK CAFE",
        local_currency=statement_currency or canonical_currency,
        local_amount=statement_amount if statement_amount is not None else canonical_amount,
        usd_amount=statement_amount if statement_amount is not None else canonical_amount,
        source_row_ref="row-1",
    )
    receipt = ReceiptDocument(
        uploader_user_id=user.id,
        source="test",
        status="imported",
        content_type="photo",
        original_file_name="synthetic_block.jpg",
        extracted_date=canonical_date,
        extracted_supplier="Block Cafe",
        extracted_local_amount=canonical_amount,
        extracted_currency=canonical_currency,
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
        reason="ai-block-test fixture",
    )
    session.add(decision)
    session.commit()
    return statement.id, receipt


def _seed_agent_block(session: Session, *, receipt: ReceiptDocument) -> AgentReceiptComparison:
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
        prompt_hash="b" * 64,
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
        local_amount_decimal="999.99",
        currency=receipt.extracted_currency,
    )
    session.add(read_row)
    session.flush()

    comparison = AgentReceiptComparison(
        run_id=run.id or 0,
        agent_receipt_read_id=read_row.id or 0,
        receipt_document_id=receipt.id or 0,
        comparator_version="agent_receipt_comparator_0a",
        risk_level="block",
        recommended_action="block_report",
        attention_required=True,
        differences_json=json.dumps(["amount_mismatch"]),
        suggested_user_message="The shadow reviewer found a critical amount issue.",
        canonical_snapshot_hash=snapshot_hash,
    )
    session.add(comparison)
    session.commit()
    session.refresh(comparison)
    return comparison


def test_ai_block_alone_does_not_set_attention_required() -> None:
    with Session(engine) as session:
        statement_id, receipt = _seed_matched_receipt(
            session,
            canonical_amount=Decimal("12.34"),
            canonical_currency="USD",
            canonical_date=date(2026, 4, 1),
        )
        _seed_agent_block(session, receipt=receipt)
        expense_report_id = ensure_expense_report_for_statement(session, statement_id)
        review = get_or_create_review_session(session, expense_report_id=expense_report_id)
        rows = review_rows(session, review.id or 0)
        assert len(rows) == 1
        row = rows[0]
        assert row.attention_required is False
        # Even though AI says "block", the row may still confirm cleanly.
        confirm_review_session(session, review.id or 0)


def test_ai_block_alone_allows_report_validation_to_be_ready() -> None:
    with Session(engine) as session:
        statement_id, receipt = _seed_matched_receipt(
            session,
            canonical_amount=Decimal("12.34"),
            canonical_currency="USD",
            canonical_date=date(2026, 4, 1),
        )
        _seed_agent_block(session, receipt=receipt)
        expense_report_id = ensure_expense_report_for_statement(session, statement_id)
        review = get_or_create_review_session(session, expense_report_id=expense_report_id)
        confirm_review_session(session, review.id or 0)
        validation = validate_report_readiness(session, expense_report_id=expense_report_id)
        codes = [issue.code for issue in validation.issues]
        # No AI-derived codes leak into the readiness issue list.
        ai_codes = {
            "ai_amount_mismatch",
            "amount_mismatch",
            "ai_review_block",
            "ai_review_warn",
        }
        assert ai_codes.isdisjoint(set(codes))
        assert validation.ready is True


def test_ai_supplier_only_difference_does_not_change_confirmation() -> None:
    with Session(engine) as session:
        statement_id, receipt = _seed_matched_receipt(
            session,
            canonical_amount=Decimal("50.00"),
            canonical_currency="USD",
            canonical_date=date(2026, 4, 5),
        )
        # Seed an AI warn whose only difference is supplier_mismatch.
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
            extracted_supplier="Different Cafe Header",
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

        expense_report_id = ensure_expense_report_for_statement(session, statement_id)
        review = get_or_create_review_session(session, expense_report_id=expense_report_id)
        rows = review_rows(session, review.id or 0)
        assert rows[0].attention_required is False
        confirm_review_session(session, review.id or 0)
        validation = validate_report_readiness(session, expense_report_id=expense_report_id)
        assert validation.ready is True


def test_deterministic_safety_still_blocks_even_when_ai_says_pass() -> None:
    """If a deterministic receipt-vs-statement mismatch exists, the row must
    still be flagged regardless of what AI second-read says."""
    with Session(engine) as session:
        # Receipt and statement disagree on amount (deterministic mismatch).
        statement_id, receipt = _seed_matched_receipt(
            session,
            canonical_amount=Decimal("100.00"),
            canonical_currency="USD",
            canonical_date=date(2026, 4, 10),
            statement_amount=Decimal("999.99"),
        )
        # AI says everything is great anyway.
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
            risk_level="pass",
            recommended_action="accept",
            attention_required=False,
            differences_json="[]",
            canonical_snapshot_hash=snapshot_hash,
        )
        session.add(comparison)
        session.commit()

        expense_report_id = ensure_expense_report_for_statement(session, statement_id)
        review = get_or_create_review_session(session, expense_report_id=expense_report_id)
        rows = review_rows(session, review.id or 0)
        # Deterministic safety must still block.
        assert rows[0].attention_required is True
        with pytest.raises(ValueError, match="rows marked for attention"):
            confirm_review_session(session, review.id or 0)
