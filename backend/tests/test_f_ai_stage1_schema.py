from __future__ import annotations

import importlib
from datetime import datetime

from sqlmodel import Session, select

from app.db import engine
from app.models import (
    AgentReceiptRead,
    AgentReceiptReviewRun,
    AgentReceiptUserResponse,
    ReceiptDocument,
)

source_tag_backfill = importlib.import_module(
    "migrations.001_f_ai_stage1_backfill_source_tags"
)


def _create_receipt(session: Session) -> ReceiptDocument:
    receipt = ReceiptDocument(source="test", status="extracted", content_type="photo")
    session.add(receipt)
    session.commit()
    session.refresh(receipt)
    return receipt


def _create_run(session: Session, receipt: ReceiptDocument) -> AgentReceiptReviewRun:
    run = AgentReceiptReviewRun(
        receipt_document_id=receipt.id or 0,
        run_source="test",
        run_kind="receipt_inline_keyboard",
        status="completed",
        schema_version="stage1",
        prompt_version="stage1_prompt",
        comparator_version="stage1_comparator",
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def _create_read(
    session: Session,
    receipt: ReceiptDocument,
    run: AgentReceiptReviewRun,
) -> AgentReceiptRead:
    read = AgentReceiptRead(
        run_id=run.id or 0,
        receipt_document_id=receipt.id or 0,
        read_schema_version="stage1",
    )
    session.add(read)
    session.commit()
    session.refresh(read)
    return read


def test_agent_receipt_read_has_suggestion_columns() -> None:
    with Session(engine) as session:
        receipt = _create_receipt(session)
        run = _create_run(session, receipt)
        read = AgentReceiptRead(
            run_id=run.id or 0,
            receipt_document_id=receipt.id or 0,
            read_schema_version="stage1",
            suggested_business_or_personal="Business",
            suggested_report_bucket="Meals/Snacks",
            suggested_attendees_json='["Hakan", "Customer"]',
            suggested_customer="Acme",
            suggested_business_reason="Customer meeting",
            suggested_confidence_overall=0.87,
        )
        session.add(read)
        session.commit()
        session.refresh(read)

        reloaded = session.get(AgentReceiptRead, read.id)

        assert reloaded is not None
        assert reloaded.suggested_business_or_personal == "Business"
        assert reloaded.suggested_report_bucket == "Meals/Snacks"
        assert reloaded.suggested_attendees_json == '["Hakan", "Customer"]'
        assert reloaded.suggested_customer == "Acme"
        assert reloaded.suggested_business_reason == "Customer meeting"
        assert reloaded.suggested_confidence_overall == 0.87


def test_agent_receipt_review_run_has_context_window_column() -> None:
    with Session(engine) as session:
        receipt = _create_receipt(session)
        run = AgentReceiptReviewRun(
            receipt_document_id=receipt.id or 0,
            run_source="test",
            run_kind="receipt_inline_keyboard",
            status="completed",
            schema_version="stage1",
            prompt_version="stage1_prompt",
            comparator_version="stage1_comparator",
            context_window_json='{"recent_receipts": []}',
        )
        session.add(run)
        session.commit()
        session.refresh(run)

        reloaded = session.get(AgentReceiptReviewRun, run.id)

        assert reloaded is not None
        assert reloaded.context_window_json == '{"recent_receipts": []}'


def test_agent_receipt_user_response_table_exists() -> None:
    with Session(engine) as session:
        receipt = _create_receipt(session)
        run = _create_run(session, receipt)
        read = _create_read(session, receipt, run)
        action_at = datetime(2026, 5, 2, 12, 30)
        response = AgentReceiptUserResponse(
            receipt_document_id=receipt.id or 0,
            agent_receipt_review_run_id=run.id or 0,
            agent_receipt_read_id=read.id or 0,
            telegram_user_id=123456,
            keyboard_message_id=789,
            user_action="edited",
            user_action_at=action_at,
            free_text_reply="Business: customer meeting",
            canonical_write_json='{"business_reason": "customer meeting"}',
        )
        session.add(response)
        session.commit()
        session.refresh(response)

        reloaded = session.get(AgentReceiptUserResponse, response.id)

        assert reloaded is not None
        assert reloaded.receipt_document_id == receipt.id
        assert reloaded.agent_receipt_review_run_id == run.id
        assert reloaded.agent_receipt_read_id == read.id
        assert reloaded.telegram_user_id == 123456
        assert reloaded.keyboard_message_id == 789
        assert reloaded.user_action == "edited"
        assert reloaded.user_action_at == action_at
        assert reloaded.free_text_reply == "Business: customer meeting"
        assert reloaded.canonical_write_json == '{"business_reason": "customer meeting"}'


def test_agent_receipt_user_response_action_values() -> None:
    actions = (
        "pending",
        "confirmed",
        "edited",
        "cancelled",
        "auto_confirmed_timeout",
        "auto_confirmed_supersede",
    )
    with Session(engine) as session:
        receipt = _create_receipt(session)
        run = _create_run(session, receipt)
        read = _create_read(session, receipt, run)
        for action in actions:
            session.add(
                AgentReceiptUserResponse(
                    receipt_document_id=receipt.id or 0,
                    agent_receipt_review_run_id=run.id or 0,
                    agent_receipt_read_id=read.id or 0,
                    user_action=action,
                )
            )
        session.commit()

        stored = session.exec(select(AgentReceiptUserResponse)).all()

        assert {row.user_action for row in stored} == set(actions)


def test_receipt_document_has_source_tag_columns() -> None:
    with Session(engine) as session:
        receipt = ReceiptDocument(
            source="test",
            status="extracted",
            content_type="photo",
            category_source="ai_advisory",
            bucket_source="auto_suggester",
            business_reason_source="telegram_user",
            attendees_source="user",
        )
        session.add(receipt)
        session.commit()
        session.refresh(receipt)

        reloaded = session.get(ReceiptDocument, receipt.id)

        assert reloaded is not None
        assert reloaded.category_source == "ai_advisory"
        assert reloaded.bucket_source == "auto_suggester"
        assert reloaded.business_reason_source == "telegram_user"
        assert reloaded.attendees_source == "user"


def test_backfill_source_tags_legacy_unknown() -> None:
    with Session(engine) as session:
        session.add(ReceiptDocument(source="test", status="extracted", content_type="photo"))
        session.add(ReceiptDocument(source="test", status="extracted", content_type="photo"))
        session.commit()

    first_count = source_tag_backfill.backfill_source_tags()
    second_count = source_tag_backfill.backfill_source_tags()

    with Session(engine) as session:
        receipts = session.exec(select(ReceiptDocument)).all()

    assert first_count == 2
    assert second_count == 0
    assert len(receipts) == 2
    for receipt in receipts:
        assert receipt.category_source == "legacy_unknown"
        assert receipt.bucket_source == "legacy_unknown"
        assert receipt.business_reason_source == "legacy_unknown"
        assert receipt.attendees_source == "legacy_unknown"
