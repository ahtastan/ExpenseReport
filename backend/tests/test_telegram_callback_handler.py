"""F-AI-Stage1 sub-PR 3: callback_query handler scenarios."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlmodel import Session

from app.models import (
    AgentReceiptRead,
    AgentReceiptReviewRun,
    AgentReceiptUserResponse,
    AppUser,
    ReceiptDocument,
)
from app.services.telegram import handle_update


class _FakeClient:
    """Records calls; never hits Telegram."""

    enabled = True

    def __init__(self) -> None:
        self.send_messages: list[tuple[int, str]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def send_message(self, chat_id: int, text: str) -> None:
        self.send_messages.append((chat_id, text))

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, payload))
        return {"ok": True, "result": {"message_id": 999}}


def _seed_pending_response(
    session: Session,
    *,
    suggested_business_or_personal: str | None = "Business",
    suggested_report_bucket: str | None = "Meals/Snacks",
    suggested_attendees: list[str] | None = None,
    suggested_business_reason: str | None = "Team lunch",
    created_at: datetime | None = None,
) -> dict[str, int]:
    user = AppUser(telegram_user_id=8038997793, display_name="Hakan")
    session.add(user)
    session.commit()
    session.refresh(user)

    receipt = ReceiptDocument(
        uploader_user_id=user.id,
        source="telegram",
        status="received",
        content_type="photo",
        telegram_chat_id=42,
        telegram_message_id=100,
        extracted_supplier="Acme Cafe",
        extracted_date=date(2026, 5, 1),
        extracted_local_amount=Decimal("42.50"),
        extracted_currency="TRY",
    )
    session.add(receipt)
    session.commit()
    session.refresh(receipt)

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

    attendees_json = (
        json.dumps(suggested_attendees) if suggested_attendees is not None else None
    )
    read_row = AgentReceiptRead(
        run_id=run.id,
        receipt_document_id=receipt.id,
        read_schema_version="stage1",
        read_json="{}",
        suggested_business_or_personal=suggested_business_or_personal,
        suggested_report_bucket=suggested_report_bucket,
        suggested_attendees_json=attendees_json,
        suggested_business_reason=suggested_business_reason,
        suggested_confidence_overall=0.85,
    )
    session.add(read_row)
    session.commit()
    session.refresh(read_row)

    response = AgentReceiptUserResponse(
        receipt_document_id=receipt.id,
        agent_receipt_review_run_id=run.id,
        agent_receipt_read_id=read_row.id,
        telegram_user_id=user.telegram_user_id,
        keyboard_message_id=555,
        user_action="pending",
    )
    if created_at is not None:
        response.created_at = created_at
    session.add(response)
    session.commit()
    session.refresh(response)

    return {
        "user_id": user.id,
        "telegram_user_id": user.telegram_user_id,
        "receipt_id": receipt.id,
        "run_id": run.id,
        "agent_read_id": read_row.id,
        "response_id": response.id,
    }


def _callback_payload(
    *,
    telegram_user_id: int,
    response_id: int,
    action: str = "confirm",
    callback_id: str = "cbk1",
) -> dict[str, Any]:
    return {
        "callback_query": {
            "id": callback_id,
            "from": {"id": telegram_user_id, "first_name": "Hakan"},
            "data": f"fai1:{action}:{response_id}",
            "message": {
                "message_id": 555,
                "chat": {"id": 42},
            },
        }
    }


def _patch_telegram_client(client: _FakeClient):
    """Helper: monkeypatch TelegramClient so handle_update uses our fake."""
    import app.services.telegram as telegram_module

    original = telegram_module.TelegramClient
    telegram_module.TelegramClient = lambda *_args, **_kwargs: client  # type: ignore[assignment]
    return telegram_module, original


def test_confirm_writes_canonical_with_ai_advisory_source(isolated_db):
    with Session(isolated_db) as session:
        ids = _seed_pending_response(
            session,
            suggested_attendees=["Hakan", "Burak Yilmaz"],
        )

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            result = handle_update(
                session,
                _callback_payload(
                    telegram_user_id=ids["telegram_user_id"],
                    response_id=ids["response_id"],
                    action="confirm",
                ),
            )

        assert result["action"] == "callback_confirmed"
        assert result["receipt_id"] == ids["receipt_id"]
    finally:
        telegram_module.TelegramClient = original

    with Session(isolated_db) as session:
        refreshed_receipt = session.get(ReceiptDocument, ids["receipt_id"])
        refreshed_response = session.get(AgentReceiptUserResponse, ids["response_id"])
        assert refreshed_receipt.business_or_personal == "Business"
        assert refreshed_receipt.report_bucket == "Meals/Snacks"
        assert refreshed_receipt.attendees == "Hakan + Burak Yilmaz"
        assert refreshed_receipt.business_reason == "Team lunch"
        assert refreshed_receipt.category_source == "ai_advisory"
        assert refreshed_receipt.bucket_source == "ai_advisory"
        assert refreshed_receipt.attendees_source == "ai_advisory"
        assert refreshed_receipt.business_reason_source == "ai_advisory"
        assert refreshed_receipt.needs_clarification is False
        assert refreshed_response.user_action == "confirmed"
        assert refreshed_response.user_action_at is not None
        assert refreshed_response.canonical_write_json is not None
        canonical_payload = json.loads(refreshed_response.canonical_write_json)
        assert canonical_payload["source_tag"] == "ai_advisory"

    edit_calls = [c for c in client.calls if c[0] == "editMessageText"]
    answer_calls = [c for c in client.calls if c[0] == "answerCallbackQuery"]
    assert edit_calls, "expected editMessageText call to remove keyboard"
    assert answer_calls, "expected answerCallbackQuery to dismiss spinner"
    assert "✅ Confirmed" in edit_calls[0][1]["text"]


def test_confirm_skips_blank_suggestions(isolated_db):
    with Session(isolated_db) as session:
        ids = _seed_pending_response(
            session,
            suggested_business_or_personal=None,
            suggested_report_bucket="Meals/Snacks",
            suggested_attendees=None,
            suggested_business_reason="Team lunch",
        )
        # Pre-populate the canonical value: the AI proposal must NOT
        # blank this out when its suggested_business_or_personal is None.
        receipt = session.get(ReceiptDocument, ids["receipt_id"])
        receipt.business_or_personal = "Personal"
        receipt.category_source = "telegram_user"
        session.add(receipt)
        session.commit()

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update(
                session,
                _callback_payload(
                    telegram_user_id=ids["telegram_user_id"],
                    response_id=ids["response_id"],
                    action="confirm",
                ),
            )
    finally:
        telegram_module.TelegramClient = original

    with Session(isolated_db) as session:
        refreshed = session.get(ReceiptDocument, ids["receipt_id"])
        # The pre-set value survives.
        assert refreshed.business_or_personal == "Personal"
        assert refreshed.category_source == "telegram_user"
        # Other fields the AI did propose are written.
        assert refreshed.report_bucket == "Meals/Snacks"
        assert refreshed.bucket_source == "ai_advisory"
        assert refreshed.business_reason == "Team lunch"
        assert refreshed.business_reason_source == "ai_advisory"


def test_edit_marks_response_edited_and_replies(isolated_db):
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            result = handle_update(
                session,
                _callback_payload(
                    telegram_user_id=ids["telegram_user_id"],
                    response_id=ids["response_id"],
                    action="edit",
                ),
            )
    finally:
        telegram_module.TelegramClient = original

    assert result["action"] == "callback_edit_requested"
    with Session(isolated_db) as session:
        refreshed = session.get(AgentReceiptUserResponse, ids["response_id"])
        assert refreshed.user_action == "edited"
    edit_message_calls = [c for c in client.calls if c[0] == "editMessageText"]
    assert any("Got it" in c[1]["text"] for c in edit_message_calls)
    assert client.send_messages, "expected a follow-up sendMessage prompting the correction"


def test_cancel_sets_receipt_cancelled(isolated_db):
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            result = handle_update(
                session,
                _callback_payload(
                    telegram_user_id=ids["telegram_user_id"],
                    response_id=ids["response_id"],
                    action="cancel",
                ),
            )
    finally:
        telegram_module.TelegramClient = original

    assert result["action"] == "callback_cancelled"
    with Session(isolated_db) as session:
        refreshed_receipt = session.get(ReceiptDocument, ids["receipt_id"])
        refreshed_response = session.get(AgentReceiptUserResponse, ids["response_id"])
        assert refreshed_receipt.status == "cancelled"
        assert refreshed_response.user_action == "cancelled"
        # No canonical write happened — fields stay where they were.
        assert refreshed_receipt.category_source is None


def test_double_tap_idempotent(isolated_db):
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session, suggested_attendees=["Hakan"])

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update(
                session,
                _callback_payload(
                    telegram_user_id=ids["telegram_user_id"],
                    response_id=ids["response_id"],
                    action="confirm",
                ),
            )
        # Second arrival of the same callback (Telegram retry).
        with Session(isolated_db) as session:
            second = handle_update(
                session,
                _callback_payload(
                    telegram_user_id=ids["telegram_user_id"],
                    response_id=ids["response_id"],
                    action="confirm",
                ),
            )
    finally:
        telegram_module.TelegramClient = original

    assert second["action"] == "callback_already_finalized"


def test_malformed_callback_data_logged_silently(isolated_db):
    with Session(isolated_db) as session:
        user = AppUser(telegram_user_id=8038997793, display_name="Hakan")
        session.add(user)
        session.commit()

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            result = handle_update(
                session,
                {
                    "callback_query": {
                        "id": "cbk_garbage",
                        "from": {"id": 8038997793, "first_name": "Hakan"},
                        "data": "totally not the format",
                        "message": {"message_id": 1, "chat": {"id": 42}},
                    }
                },
            )
    finally:
        telegram_module.TelegramClient = original

    assert result["action"] == "callback_malformed_ignored"
    # Spinner still dismissed.
    assert any(c[0] == "answerCallbackQuery" for c in client.calls)


def test_callback_for_unknown_response_id(isolated_db):
    with Session(isolated_db) as session:
        user = AppUser(telegram_user_id=8038997793, display_name="Hakan")
        session.add(user)
        session.commit()

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            result = handle_update(
                session,
                _callback_payload(telegram_user_id=8038997793, response_id=99999, action="confirm"),
            )
    finally:
        telegram_module.TelegramClient = original

    assert result["action"] == "callback_unknown_ignored"
    assert any(c[0] == "answerCallbackQuery" for c in client.calls)


def test_callback_for_already_finalized_response(isolated_db):
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)
        response = session.get(AgentReceiptUserResponse, ids["response_id"])
        response.user_action = "confirmed"
        session.add(response)
        session.commit()

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            result = handle_update(
                session,
                _callback_payload(
                    telegram_user_id=ids["telegram_user_id"],
                    response_id=ids["response_id"],
                    action="confirm",
                ),
            )
    finally:
        telegram_module.TelegramClient = original

    assert result["action"] == "callback_already_finalized"
    # No editMessageText for an already-finalized response.
    assert not any(c[0] == "editMessageText" for c in client.calls)
