"""F-AI-Stage1 sub-PR 3: dispatch + flag-gating + supersede + timeout."""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from sqlmodel import Session

from app.models import (
    AgentReceiptRead,
    AgentReceiptReviewRun,
    AgentReceiptUserResponse,
    AppUser,
    ClarificationQuestion,
    ReceiptDocument,
)


class _FakeClient:
    """Records calls; no network."""

    enabled = True

    def __init__(self) -> None:
        self.send_messages: list[tuple[int, str]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def send_message(self, chat_id: int, text: str) -> None:
        self.send_messages.append((chat_id, text))

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, payload))
        return {"ok": True, "result": {"message_id": 999}}

    def download_file(self, file_id, user_id, fallback_name):
        # Receipt-upload tests in this file don't go through download.
        return None


def _patch_telegram_client(client: _FakeClient):
    import app.services.telegram as telegram_module

    original = telegram_module.TelegramClient
    telegram_module.TelegramClient = lambda *_args, **_kwargs: client  # type: ignore[assignment]
    return telegram_module, original


def _photo_message(*, telegram_user_id: int, file_unique_id: str = "abc123") -> dict[str, Any]:
    return {
        "message": {
            "message_id": 1001,
            "from": {"id": telegram_user_id, "first_name": "Hakan"},
            "chat": {"id": 42, "type": "private"},
            "photo": [
                {
                    "file_id": "AgACA-test",
                    "file_unique_id": file_unique_id,
                    "file_size": 12345,
                    "width": 800,
                    "height": 1200,
                }
            ],
        }
    }


def _seed_user(session: Session, *, telegram_user_id: int = 8038997793) -> int:
    user = AppUser(telegram_user_id=telegram_user_id, display_name="Hakan")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user.id


def _enable_flag_env(monkeypatch, *, keyboard: bool, allowlist: str = "8038997793"):
    """Set env vars and clear the cached settings so flags take effect."""
    monkeypatch.setenv("AI_TELEGRAM_REPLY_ENABLED", "true")
    monkeypatch.setenv("AI_TELEGRAM_LIVE_MODEL_ENABLED", "true")
    monkeypatch.setenv("AI_TELEGRAM_REPLY_ALLOWLIST", allowlist)
    monkeypatch.setenv(
        "AI_TELEGRAM_INLINE_KEYBOARD_ENABLED", "true" if keyboard else "false"
    )
    from app.config import get_settings

    get_settings.cache_clear()


def test_flag_off_uses_legacy_path(isolated_db, monkeypatch):
    """Flag off → no AgentReceiptUserResponse row, legacy clarification flow runs."""
    _enable_flag_env(monkeypatch, keyboard=False)
    user_id = None
    with Session(isolated_db) as session:
        user_id = _seed_user(session)

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)

    # Stub maybe_send_telegram_receipt_reply so we don't try to call OpenAI.
    import app.services.telegram_receipt_reply as reply_module

    original_maybe_send = reply_module.maybe_send_telegram_receipt_reply
    reply_module.maybe_send_telegram_receipt_reply = lambda *a, **k: False
    original_maybe_create = reply_module.maybe_create_telegram_receipt_ai_review
    reply_module.maybe_create_telegram_receipt_ai_review = lambda *a, **k: None
    # Prevent OCR routing from calling anything live.
    import app.services.receipt_extraction as extraction_module

    class _ExtractionStub:
        confidence = 0.5

    original_extract = extraction_module.apply_receipt_extraction
    extraction_module.apply_receipt_extraction = lambda *a, **k: _ExtractionStub()

    try:
        with Session(isolated_db) as session:
            result = handle_update_local(session, _photo_message(telegram_user_id=8038997793))
    finally:
        telegram_module.TelegramClient = original
        reply_module.maybe_send_telegram_receipt_reply = original_maybe_send
        reply_module.maybe_create_telegram_receipt_ai_review = original_maybe_create
        extraction_module.apply_receipt_extraction = original_extract

    assert result["action"] == "receipt_captured"
    with Session(isolated_db) as session:
        assert session.exec(_select_first(AgentReceiptUserResponse)).first() is None
        # Clarification questions DO get seeded on the legacy path.
        questions = session.exec(_select_first(ClarificationQuestion)).all()
        assert len(questions) > 0


def test_flag_on_non_allowlisted_user_uses_legacy(isolated_db, monkeypatch):
    """Flag on but user not in allowlist → legacy flow."""
    _enable_flag_env(monkeypatch, keyboard=True, allowlist="9999")  # different id
    with Session(isolated_db) as session:
        _seed_user(session, telegram_user_id=8038997793)

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    import app.services.telegram_receipt_reply as reply_module

    orig_send = reply_module.maybe_send_telegram_receipt_reply
    orig_create = reply_module.maybe_create_telegram_receipt_ai_review
    reply_module.maybe_send_telegram_receipt_reply = lambda *a, **k: False
    reply_module.maybe_create_telegram_receipt_ai_review = lambda *a, **k: None
    import app.services.receipt_extraction as extraction_module

    class _ExtractionStub:
        confidence = 0.5

    orig_extract = extraction_module.apply_receipt_extraction
    extraction_module.apply_receipt_extraction = lambda *a, **k: _ExtractionStub()

    try:
        with Session(isolated_db) as session:
            handle_update_local(session, _photo_message(telegram_user_id=8038997793))
    finally:
        telegram_module.TelegramClient = original
        reply_module.maybe_send_telegram_receipt_reply = orig_send
        reply_module.maybe_create_telegram_receipt_ai_review = orig_create
        extraction_module.apply_receipt_extraction = orig_extract

    with Session(isolated_db) as session:
        assert session.exec(_select_first(AgentReceiptUserResponse)).first() is None


def test_flag_on_allowlisted_user_uses_keyboard(isolated_db, monkeypatch):
    """Flag on + user in allowlist → keyboard sent, response row pending,
    no clarification questions seeded."""
    _enable_flag_env(monkeypatch, keyboard=True)
    with Session(isolated_db) as session:
        _seed_user(session, telegram_user_id=8038997793)

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    import app.services.telegram_receipt_reply as reply_module

    # Stub the inline-keyboard send so it returns True without calling OpenAI.
    def _stub_send_inline_keyboard(session, _client, **kwargs):
        receipt = kwargs["receipt"]
        # Build a complete agent_run + agent_read + user_response inline.
        run = AgentReceiptReviewRun(
            receipt_document_id=receipt.id,
            run_source="telegram_receipt_inline_keyboard",
            run_kind="receipt_inline_keyboard",
            status="completed",
            schema_version="stage1",
            prompt_version="agent_receipt_inline_keyboard_prompt_stage1_v1",
            comparator_version="agent_receipt_comparator_0a",
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        read = AgentReceiptRead(
            run_id=run.id,
            receipt_document_id=receipt.id,
            read_schema_version="stage1",
            read_json="{}",
            suggested_business_or_personal="Business",
            suggested_report_bucket="Meals/Snacks",
            suggested_attendees_json=json.dumps(["Hakan"]),
            suggested_business_reason="Lunch",
            suggested_confidence_overall=0.8,
        )
        session.add(read)
        session.commit()
        session.refresh(read)
        response = AgentReceiptUserResponse(
            receipt_document_id=receipt.id,
            agent_receipt_review_run_id=run.id,
            agent_receipt_read_id=read.id,
            telegram_user_id=kwargs["telegram_user_id"],
            keyboard_message_id=999,
            user_action="pending",
        )
        session.add(response)
        session.commit()
        return True

    orig_send_kb = reply_module.send_inline_keyboard_proposal
    reply_module.send_inline_keyboard_proposal = _stub_send_inline_keyboard
    # Need to re-import the symbol in services.telegram too because it
    # was imported at module load time.
    telegram_module.send_inline_keyboard_proposal = _stub_send_inline_keyboard

    import app.services.receipt_extraction as extraction_module

    class _ExtractionStub:
        confidence = 0.5

    orig_extract = extraction_module.apply_receipt_extraction
    extraction_module.apply_receipt_extraction = lambda *a, **k: _ExtractionStub()

    try:
        with Session(isolated_db) as session:
            result = handle_update_local(session, _photo_message(telegram_user_id=8038997793))
    finally:
        telegram_module.TelegramClient = original
        reply_module.send_inline_keyboard_proposal = orig_send_kb
        telegram_module.send_inline_keyboard_proposal = orig_send_kb
        extraction_module.apply_receipt_extraction = orig_extract

    assert result["action"] == "receipt_keyboard_sent"
    with Session(isolated_db) as session:
        responses = session.exec(_select_first(AgentReceiptUserResponse)).all()
        assert len(responses) == 1
        assert responses[0].user_action == "pending"
        questions = session.exec(_select_first(ClarificationQuestion)).all()
        assert len(questions) == 0  # keyboard replaces clarification flow


def test_supersede_flow(isolated_db, monkeypatch):
    """Pending row + new receipt → previous flips to auto_confirmed_supersede,
    canonical written with auto_confirmed_default."""
    _enable_flag_env(monkeypatch, keyboard=True)
    with Session(isolated_db) as session:
        user_id = _seed_user(session, telegram_user_id=8038997793)
        # Pre-existing pending row from an earlier receipt.
        prior_receipt = ReceiptDocument(
            uploader_user_id=user_id,
            source="telegram",
            status="received",
            content_type="photo",
            telegram_chat_id=42,
            extracted_supplier="Prior Cafe",
            extracted_date=date(2026, 5, 1),
            extracted_local_amount=Decimal("20.00"),
            extracted_currency="TRY",
        )
        session.add(prior_receipt)
        session.commit()
        session.refresh(prior_receipt)
        run = AgentReceiptReviewRun(
            receipt_document_id=prior_receipt.id,
            run_source="telegram_receipt_inline_keyboard",
            run_kind="receipt_inline_keyboard",
            status="completed",
            schema_version="stage1",
            prompt_version="agent_receipt_inline_keyboard_prompt_stage1_v1",
            comparator_version="agent_receipt_comparator_0a",
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        read = AgentReceiptRead(
            run_id=run.id,
            receipt_document_id=prior_receipt.id,
            read_schema_version="stage1",
            read_json="{}",
            suggested_business_or_personal="Business",
            suggested_report_bucket="Meals/Snacks",
            suggested_attendees_json=json.dumps(["Hakan"]),
            suggested_business_reason="Earlier lunch",
        )
        session.add(read)
        session.commit()
        session.refresh(read)
        prior_response = AgentReceiptUserResponse(
            receipt_document_id=prior_receipt.id,
            agent_receipt_review_run_id=run.id,
            agent_receipt_read_id=read.id,
            telegram_user_id=8038997793,
            keyboard_message_id=555,
            user_action="pending",
        )
        session.add(prior_response)
        session.commit()
        session.refresh(prior_response)
        prior_receipt_id = prior_receipt.id
        prior_response_id = prior_response.id

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    import app.services.telegram_receipt_reply as reply_module

    # Stub the new receipt's keyboard send to a no-op success so the
    # test focuses on the supersede behavior.
    def _stub(session, _client, **kwargs):
        return True

    orig_send_kb = reply_module.send_inline_keyboard_proposal
    reply_module.send_inline_keyboard_proposal = _stub
    telegram_module.send_inline_keyboard_proposal = _stub
    import app.services.receipt_extraction as extraction_module

    class _ExtractionStub:
        confidence = 0.5

    orig_extract = extraction_module.apply_receipt_extraction
    extraction_module.apply_receipt_extraction = lambda *a, **k: _ExtractionStub()

    try:
        with Session(isolated_db) as session:
            handle_update_local(
                session,
                _photo_message(telegram_user_id=8038997793, file_unique_id="new123"),
            )
    finally:
        telegram_module.TelegramClient = original
        reply_module.send_inline_keyboard_proposal = orig_send_kb
        telegram_module.send_inline_keyboard_proposal = orig_send_kb
        extraction_module.apply_receipt_extraction = orig_extract

    with Session(isolated_db) as session:
        prior = session.get(AgentReceiptUserResponse, prior_response_id)
        assert prior.user_action == "auto_confirmed_supersede"
        prior_receipt_refreshed = session.get(ReceiptDocument, prior_receipt_id)
        assert prior_receipt_refreshed.business_or_personal == "Business"
        assert prior_receipt_refreshed.category_source == "auto_confirmed_default"
        assert prior_receipt_refreshed.bucket_source == "auto_confirmed_default"
        assert prior_receipt_refreshed.attendees_source == "auto_confirmed_default"
        assert prior_receipt_refreshed.business_reason_source == "auto_confirmed_default"


def test_timeout_flow(isolated_db, monkeypatch):
    """Pending row >24h → flips to auto_confirmed_timeout on next webhook event."""
    _enable_flag_env(monkeypatch, keyboard=True)
    old_time = datetime.now(timezone.utc) - timedelta(hours=25)
    with Session(isolated_db) as session:
        user_id = _seed_user(session, telegram_user_id=8038997793)
        prior_receipt = ReceiptDocument(
            uploader_user_id=user_id,
            source="telegram",
            status="received",
            content_type="photo",
            telegram_chat_id=42,
            extracted_supplier="Old Cafe",
            extracted_date=date(2026, 4, 28),
            extracted_local_amount=Decimal("50.0"),
            extracted_currency="TRY",
        )
        session.add(prior_receipt)
        session.commit()
        session.refresh(prior_receipt)
        run = AgentReceiptReviewRun(
            receipt_document_id=prior_receipt.id,
            run_source="telegram_receipt_inline_keyboard",
            run_kind="receipt_inline_keyboard",
            status="completed",
            schema_version="stage1",
            prompt_version="agent_receipt_inline_keyboard_prompt_stage1_v1",
            comparator_version="agent_receipt_comparator_0a",
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        read = AgentReceiptRead(
            run_id=run.id,
            receipt_document_id=prior_receipt.id,
            read_schema_version="stage1",
            read_json="{}",
            suggested_business_or_personal="Personal",
            suggested_report_bucket="Other",
            suggested_attendees_json=None,
            suggested_business_reason=None,
        )
        session.add(read)
        session.commit()
        session.refresh(read)
        old_response = AgentReceiptUserResponse(
            receipt_document_id=prior_receipt.id,
            agent_receipt_review_run_id=run.id,
            agent_receipt_read_id=read.id,
            telegram_user_id=8038997793,
            keyboard_message_id=777,
            user_action="pending",
            created_at=old_time,
        )
        session.add(old_response)
        session.commit()
        session.refresh(old_response)
        old_response_id = old_response.id

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update_local(
                session,
                {
                    "message": {
                        "message_id": 1,
                        "from": {"id": 8038997793, "first_name": "Hakan"},
                        "chat": {"id": 42, "type": "private"},
                        "text": "hi",
                    }
                },
            )
    finally:
        telegram_module.TelegramClient = original

    with Session(isolated_db) as session:
        old_refreshed = session.get(AgentReceiptUserResponse, old_response_id)
        assert old_refreshed.user_action == "auto_confirmed_timeout"


def test_reviewer_failure_falls_back_to_legacy(isolated_db, monkeypatch):
    """If send_inline_keyboard_proposal returns False, legacy flow runs."""
    _enable_flag_env(monkeypatch, keyboard=True)
    with Session(isolated_db) as session:
        _seed_user(session, telegram_user_id=8038997793)

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    import app.services.telegram_receipt_reply as reply_module

    orig_send_kb = reply_module.send_inline_keyboard_proposal
    reply_module.send_inline_keyboard_proposal = lambda *a, **k: False
    telegram_module.send_inline_keyboard_proposal = lambda *a, **k: False

    orig_send = reply_module.maybe_send_telegram_receipt_reply
    orig_create = reply_module.maybe_create_telegram_receipt_ai_review
    reply_module.maybe_send_telegram_receipt_reply = lambda *a, **k: False
    reply_module.maybe_create_telegram_receipt_ai_review = lambda *a, **k: None

    import app.services.receipt_extraction as extraction_module

    class _ExtractionStub:
        confidence = 0.5

    orig_extract = extraction_module.apply_receipt_extraction
    extraction_module.apply_receipt_extraction = lambda *a, **k: _ExtractionStub()

    try:
        with Session(isolated_db) as session:
            result = handle_update_local(session, _photo_message(telegram_user_id=8038997793))
    finally:
        telegram_module.TelegramClient = original
        reply_module.send_inline_keyboard_proposal = orig_send_kb
        telegram_module.send_inline_keyboard_proposal = orig_send_kb
        reply_module.maybe_send_telegram_receipt_reply = orig_send
        reply_module.maybe_create_telegram_receipt_ai_review = orig_create
        extraction_module.apply_receipt_extraction = orig_extract

    # Falls through to legacy → receipt_captured action.
    assert result["action"] == "receipt_captured"
    with Session(isolated_db) as session:
        assert session.exec(_select_first(AgentReceiptUserResponse)).first() is None


# ─── helpers ────────────────────────────────────────────────────────────────


def _select_first(model):
    from sqlmodel import select

    return select(model)


def handle_update_local(session, payload):
    # Local import: ensures the patched TelegramClient lookup happens
    # against the same module the patch targeted.
    from app.services.telegram import handle_update

    return handle_update(session, payload)
