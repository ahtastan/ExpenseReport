"""F-AI-Stage1 PR4 Phase 2: default Business at upload time when keyboard is on.

When the inline-keyboard flow is gated on for the uploading user (flag set
AND user in allowlist AND live-model flag on), the receipt row is created
with ``business_or_personal='Business'`` and
``category_source='auto_confirmed_default'`` immediately, before the AI
proposal runs. This guarantees the keyboard's Type field always shows
something the operator can see and toggle.

When the gate is not open (flag off, user not allowlisted, or live-model
flag off), the legacy path is preserved: ``business_or_personal`` stays
None on creation; clarifications.py later defaults non-allowlisted Telegram
users to Business via a separate code path.
"""
from __future__ import annotations

import json
from typing import Any

from sqlmodel import Session

from app.models import AgentReceiptUserResponse, AppUser, ReceiptDocument


class _FakeClient:
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
        return None


def _photo_message(*, telegram_user_id: int, fuid: str = "abc123") -> dict[str, Any]:
    return {
        "message": {
            "message_id": 1001,
            "from": {"id": telegram_user_id, "first_name": "Hakan"},
            "chat": {"id": 42, "type": "private"},
            "photo": [
                {
                    "file_id": "AgACA-test",
                    "file_unique_id": fuid,
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
    monkeypatch.setenv("AI_TELEGRAM_REPLY_ENABLED", "true")
    monkeypatch.setenv("AI_TELEGRAM_LIVE_MODEL_ENABLED", "true")
    monkeypatch.setenv("AI_TELEGRAM_REPLY_ALLOWLIST", allowlist)
    monkeypatch.setenv(
        "AI_TELEGRAM_INLINE_KEYBOARD_ENABLED", "true" if keyboard else "false"
    )
    from app.config import get_settings

    get_settings.cache_clear()


def _stub_send_inline_keyboard(session, _client, **kwargs):
    """Capture the receipt the keyboard would have sent for, but skip the
    network/keyboard plumbing. Returns True so handle_update sees a "sent"
    keyboard and short-circuits. This stub does NOT mutate
    business_or_personal — that lets the test observe the upload-time
    default value as it was at row creation."""
    from app.models import (
        AgentReceiptRead,
        AgentReceiptReviewRun,
        AgentReceiptUserResponse,
    )

    receipt = kwargs["receipt"]
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
        telegram_user_id=kwargs.get("telegram_user_id"),
        keyboard_message_id=999,
        user_action="pending",
    )
    session.add(response)
    session.commit()
    return True


def _patch_client_and_extraction(client: _FakeClient):
    """Set up the stubs needed for handle_update to run without OCR / OpenAI."""
    import app.services.receipt_extraction as extraction_module
    import app.services.telegram as telegram_module

    original_client = telegram_module.TelegramClient
    telegram_module.TelegramClient = lambda *_a, **_k: client  # type: ignore[assignment]

    class _ExtractionStub:
        confidence = 0.5

    original_extract = extraction_module.apply_receipt_extraction
    extraction_module.apply_receipt_extraction = lambda *a, **k: _ExtractionStub()

    return telegram_module, original_client, extraction_module, original_extract


def _restore(telegram_module, original_client, extraction_module, original_extract):
    telegram_module.TelegramClient = original_client
    extraction_module.apply_receipt_extraction = original_extract


def test_keyboard_flag_on_defaults_business_at_upload(isolated_db, monkeypatch):
    """Keyboard gate open (flag + allowlist + live-model) → receipt row
    created with business_or_personal='Business' and
    category_source='auto_confirmed_default'."""
    _enable_flag_env(monkeypatch, keyboard=True)
    with Session(isolated_db) as session:
        _seed_user(session, telegram_user_id=8038997793)

    client = _FakeClient()
    (
        telegram_module,
        original_client,
        extraction_module,
        original_extract,
    ) = _patch_client_and_extraction(client)

    # Stub the keyboard send so we exit handle_update via the keyboard path.
    import app.services.telegram_receipt_reply as reply_module

    orig_kb = reply_module.send_inline_keyboard_proposal
    reply_module.send_inline_keyboard_proposal = _stub_send_inline_keyboard
    telegram_module.send_inline_keyboard_proposal = _stub_send_inline_keyboard

    try:
        with Session(isolated_db) as session:
            from app.services.telegram import handle_update

            result = handle_update(
                session, _photo_message(telegram_user_id=8038997793)
            )
    finally:
        reply_module.send_inline_keyboard_proposal = orig_kb
        telegram_module.send_inline_keyboard_proposal = orig_kb
        _restore(telegram_module, original_client, extraction_module, original_extract)

    assert result["action"] == "receipt_keyboard_sent"
    with Session(isolated_db) as session:
        from sqlmodel import select

        receipt = session.exec(select(ReceiptDocument)).first()
        assert receipt is not None
        assert receipt.business_or_personal == "Business", (
            "Keyboard-on receipt should default to Business at upload"
        )
        assert receipt.category_source == "auto_confirmed_default", (
            "Source tag should mark this as system-driven default"
        )


def test_keyboard_flag_off_legacy_path_unchanged(isolated_db, monkeypatch):
    """Flag off → receipt created with business_or_personal=None initially.
    The legacy clarifications path then defaults to Business for non-allowlisted
    users, but at row creation time the field is None."""
    _enable_flag_env(monkeypatch, keyboard=False)
    with Session(isolated_db) as session:
        _seed_user(session, telegram_user_id=8038997793)

    client = _FakeClient()
    (
        telegram_module,
        original_client,
        extraction_module,
        original_extract,
    ) = _patch_client_and_extraction(client)

    # Stub the AI / send paths so we don't call real services.
    import app.services.telegram_receipt_reply as reply_module

    orig_send = reply_module.maybe_send_telegram_receipt_reply
    orig_create = reply_module.maybe_create_telegram_receipt_ai_review
    reply_module.maybe_send_telegram_receipt_reply = lambda *a, **k: False
    reply_module.maybe_create_telegram_receipt_ai_review = lambda *a, **k: None

    # Snapshot business_or_personal AT THE MOMENT the receipt row is added so
    # we can prove the upload-time value is None on the legacy path. We do
    # this by hooking apply_receipt_extraction (called immediately after
    # session.add+commit+refresh of the receipt).
    captured: dict[str, Any] = {}

    def _capture(session, receipt):
        captured["bp_at_upload"] = receipt.business_or_personal
        captured["src_at_upload"] = receipt.category_source

        class _ExtractionStub:
            confidence = 0.5

        return _ExtractionStub()

    extraction_module.apply_receipt_extraction = _capture

    try:
        with Session(isolated_db) as session:
            from app.services.telegram import handle_update

            handle_update(session, _photo_message(telegram_user_id=8038997793))
    finally:
        reply_module.maybe_send_telegram_receipt_reply = orig_send
        reply_module.maybe_create_telegram_receipt_ai_review = orig_create
        _restore(telegram_module, original_client, extraction_module, original_extract)

    assert captured.get("bp_at_upload") is None, (
        "Flag off — business_or_personal must be None at row creation; "
        "legacy clarifications.ensure_receipt_review_questions later "
        "defaults to Business for non-allowlisted users."
    )
    assert captured.get("src_at_upload") is None, (
        "Flag off — category_source must be None at row creation"
    )


def test_keyboard_flag_on_non_allowlisted_user_no_default(isolated_db, monkeypatch):
    """Flag on but user NOT in allowlist → keyboard gate stays closed,
    business_or_personal must NOT be defaulted at upload."""
    # Flag is on but allowlist points to a different user id.
    _enable_flag_env(monkeypatch, keyboard=True, allowlist="9999")
    with Session(isolated_db) as session:
        _seed_user(session, telegram_user_id=8038997793)

    client = _FakeClient()
    (
        telegram_module,
        original_client,
        extraction_module,
        original_extract,
    ) = _patch_client_and_extraction(client)

    import app.services.telegram_receipt_reply as reply_module

    orig_send = reply_module.maybe_send_telegram_receipt_reply
    orig_create = reply_module.maybe_create_telegram_receipt_ai_review
    reply_module.maybe_send_telegram_receipt_reply = lambda *a, **k: False
    reply_module.maybe_create_telegram_receipt_ai_review = lambda *a, **k: None

    captured: dict[str, Any] = {}

    def _capture(session, receipt):
        captured["bp_at_upload"] = receipt.business_or_personal
        captured["src_at_upload"] = receipt.category_source

        class _ExtractionStub:
            confidence = 0.5

        return _ExtractionStub()

    extraction_module.apply_receipt_extraction = _capture

    try:
        with Session(isolated_db) as session:
            from app.services.telegram import handle_update

            handle_update(session, _photo_message(telegram_user_id=8038997793))
    finally:
        reply_module.maybe_send_telegram_receipt_reply = orig_send
        reply_module.maybe_create_telegram_receipt_ai_review = orig_create
        _restore(telegram_module, original_client, extraction_module, original_extract)

    assert captured.get("bp_at_upload") is None, (
        "Non-allowlisted user — gate stays closed, no upload-time default"
    )
    assert captured.get("src_at_upload") is None
