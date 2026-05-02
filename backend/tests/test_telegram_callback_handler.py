"""F-AI-Stage1 sub-PR 3: callback_query handler scenarios."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from sqlmodel import Session, select

from app.models import (
    AgentReceiptRead,
    AgentReceiptReviewRun,
    AgentReceiptUserResponse,
    AppUser,
    ClarificationQuestion,
    ReceiptDocument,
)
from app.services.agent_receipt_canonical_writer import (
    CanonicalWriteLinkageError,
    write_ai_proposal_to_canonical,
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

    def download_file(self, file_id, user_id, fallback_name):
        return None


def _seed_pending_response(
    session: Session,
    *,
    user: AppUser | None = None,
    telegram_user_id: int = 8038997793,
    receipt_supplier: str = "Acme Cafe",
    receipt_report_bucket: str | None = None,
    receipt_business_or_personal: str | None = None,
    suggested_business_or_personal: str | None = "Business",
    suggested_report_bucket: str | None = "Meals/Snacks",
    suggested_attendees: list[str] | None = None,
    suggested_business_reason: str | None = "Team lunch",
    created_at: datetime | None = None,
) -> dict[str, int]:
    if user is None:
        user = AppUser(telegram_user_id=telegram_user_id, display_name="Hakan")
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
        extracted_supplier=receipt_supplier,
        extracted_date=date(2026, 5, 1),
        extracted_local_amount=Decimal("42.50"),
        extracted_currency="TRY",
        business_or_personal=receipt_business_or_personal,
        report_bucket=receipt_report_bucket,
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


def _text_payload(
    *,
    telegram_user_id: int,
    text: str,
    chat_id: int = 42,
    message_id: int = 9001,
) -> dict[str, Any]:
    return {
        "message": {
            "message_id": message_id,
            "from": {"id": telegram_user_id, "first_name": "Hakan"},
            "chat": {"id": chat_id},
            "text": text,
        }
    }


def _photo_payload(
    *,
    telegram_user_id: int,
    chat_id: int = 42,
    message_id: int = 1001,
    file_unique_id: str = "receipt-photo-1",
) -> dict[str, Any]:
    return {
        "message": {
            "message_id": message_id,
            "from": {"id": telegram_user_id, "first_name": "Hakan"},
            "chat": {"id": chat_id},
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


def _set_ai_reply_env(monkeypatch, *, allowlist: str = "8038997793") -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("AI_TELEGRAM_REPLY_ENABLED", "true")
    monkeypatch.setenv("AI_TELEGRAM_LIVE_MODEL_ENABLED", "true")
    monkeypatch.setenv("AI_TELEGRAM_INLINE_KEYBOARD_ENABLED", "true")
    monkeypatch.setenv("AI_TELEGRAM_REPLY_ALLOWLIST", allowlist)
    from app.config import get_settings

    get_settings.cache_clear()


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


def test_edit_marks_response_edited_and_shows_menu(isolated_db):
    """PR4: Edit button taps show the top-level Edit menu in place. The
    previous PR3 behavior of seeding a clarification question + sending a
    follow-up text prompt is replaced."""
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

    assert result["action"] == "callback_edit_menu_shown"
    with Session(isolated_db) as session:
        refreshed = session.get(AgentReceiptUserResponse, ids["response_id"])
        assert refreshed.user_action == "edited"
    edit_message_calls = [c for c in client.calls if c[0] == "editMessageText"]
    assert edit_message_calls, "expected an editMessageText to swap in the menu"
    last_edit = edit_message_calls[-1][1]
    assert "edit" in last_edit["text"].lower()
    assert "reply_markup" in last_edit, "Edit menu must include reply_markup"
    assert "📝" in last_edit["reply_markup"] or "Receipt" in last_edit["reply_markup"]


def test_edit_does_not_seed_clarification_question(isolated_db):
    """PR4: button-driven Edit menu does NOT seed a ClarificationQuestion.
    The PR3 seed-on-Edit behavior is replaced by the menu state machine.
    This test guards against accidental regression to the legacy flow."""
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session, receipt_supplier="Serbest Market")

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

    assert result["action"] == "callback_edit_menu_shown"
    with Session(isolated_db) as session:
        questions = session.exec(
            select(ClarificationQuestion).where(
                ClarificationQuestion.receipt_document_id == ids["receipt_id"],
            )
        ).all()
    assert questions == [], (
        "PR4 keyboard Edit must not seed clarification questions; the "
        "button-driven menu replaces that flow."
    )


# PR3 text-parse Edit round-trip and source-tag tests removed in PR4.
# The keyboard Edit flow no longer routes free-text replies through
# clarifications.py; instead, button taps drive an ``awaiting_*`` state
# machine that parses each field individually. New tests for the menu
# flow live in tests/test_telegram_button_edit_menu.py and
# tests/test_telegram_edit_parsers.py.


def test_canonical_writer_rejects_mismatched_linkage(isolated_db):
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)
        target_receipt = ReceiptDocument(
            source="telegram",
            status="received",
            content_type="photo",
            extracted_supplier="Wrong Receipt",
            extracted_date=date(2026, 5, 2),
            extracted_local_amount=Decimal("10.00"),
            extracted_currency="TRY",
        )
        session.add(target_receipt)
        session.commit()
        session.refresh(target_receipt)

        agent_read = session.get(AgentReceiptRead, ids["agent_read_id"])
        with pytest.raises(CanonicalWriteLinkageError):
            write_ai_proposal_to_canonical(
                session,
                receipt=target_receipt,
                agent_read=agent_read,
                source_tag="ai_advisory",
            )
        session.rollback()

        refreshed = session.get(ReceiptDocument, target_receipt.id)
        assert refreshed.business_or_personal is None
        assert refreshed.report_bucket is None
        assert refreshed.attendees is None
        assert refreshed.business_reason is None
        assert refreshed.category_source is None
        assert refreshed.bucket_source is None
        assert refreshed.attendees_source is None
        assert refreshed.business_reason_source is None


def test_canonical_writer_rejects_mismatched_review_run(isolated_db):
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)
        receipt = session.get(ReceiptDocument, ids["receipt_id"])
        agent_read = session.get(AgentReceiptRead, ids["agent_read_id"])
        other_run = AgentReceiptReviewRun(
            receipt_document_id=receipt.id,
            run_source="telegram_receipt_inline_keyboard",
            run_kind="receipt_inline_keyboard",
            status="completed",
            schema_version="stage1",
            prompt_version="agent_receipt_inline_keyboard_prompt_stage1_v1",
            comparator_version="agent_receipt_comparator_0a",
        )
        session.add(other_run)
        session.commit()
        session.refresh(other_run)

        with pytest.raises(CanonicalWriteLinkageError):
            write_ai_proposal_to_canonical(
                session,
                receipt=receipt,
                agent_read=agent_read,
                source_tag="ai_advisory",
                expected_review_run_id=other_run.id,
            )
        session.rollback()

        refreshed = session.get(ReceiptDocument, ids["receipt_id"])
        assert refreshed.business_or_personal is None
        assert refreshed.report_bucket is None
        assert refreshed.category_source is None
        assert refreshed.bucket_source is None


def test_callback_handles_linkage_error_gracefully(isolated_db, caplog):
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)
        other_receipt = ReceiptDocument(
            source="telegram",
            status="received",
            content_type="photo",
            extracted_supplier="Other Receipt",
            extracted_date=date(2026, 5, 2),
            extracted_local_amount=Decimal("10.00"),
            extracted_currency="TRY",
        )
        session.add(other_receipt)
        session.commit()
        session.refresh(other_receipt)

        agent_read = session.get(AgentReceiptRead, ids["agent_read_id"])
        agent_read.receipt_document_id = other_receipt.id
        session.add(agent_read)
        session.commit()

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    caplog.set_level(logging.ERROR, logger="app.services.telegram")
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

    assert result["action"] == "callback_failed_validation"
    assert any(c[0] == "answerCallbackQuery" for c in client.calls)
    assert "canonical write linkage failed" in caplog.text
    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, ids["receipt_id"])
        response = session.get(AgentReceiptUserResponse, ids["response_id"])

    assert response.user_action == "failed_validation"
    assert response.canonical_write_json is None
    assert receipt.business_or_personal is None
    assert receipt.report_bucket is None
    assert receipt.category_source is None


@pytest.mark.parametrize("action", ("confirm", "edit", "cancel"))
def test_callback_rejected_when_telegram_user_does_not_own_response(
    isolated_db,
    caplog,
    action,
):
    owner_telegram_id = 8038997793
    other_telegram_id = 8038997794
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session, telegram_user_id=owner_telegram_id)

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    caplog.set_level(logging.WARNING, logger="app.services.telegram")
    try:
        with Session(isolated_db) as session:
            result = handle_update(
                session,
                _callback_payload(
                    telegram_user_id=other_telegram_id,
                    response_id=ids["response_id"],
                    action=action,
                ),
            )
    finally:
        telegram_module.TelegramClient = original

    assert result["action"] == "callback_owner_mismatch_ignored"
    assert any(c[0] == "answerCallbackQuery" for c in client.calls)
    assert "owned by telegram_user_id" in caplog.text
    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, ids["receipt_id"])
        response = session.get(AgentReceiptUserResponse, ids["response_id"])
        questions = session.exec(select(ClarificationQuestion)).all()

    assert response.user_action == "pending"
    assert response.user_action_at is None
    assert response.canonical_write_json is None
    assert receipt.status == "received"
    assert receipt.business_or_personal is None
    assert receipt.report_bucket is None
    assert receipt.category_source is None
    assert questions == []


@pytest.mark.parametrize("action", ("confirm", "edit", "cancel"))
def test_callback_rejected_when_user_response_has_null_owner(
    isolated_db,
    caplog,
    action,
):
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)
        response = session.get(AgentReceiptUserResponse, ids["response_id"])
        response.telegram_user_id = None
        session.add(response)
        session.commit()

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    caplog.set_level(logging.WARNING, logger="app.services.telegram")
    try:
        with Session(isolated_db) as session:
            result = handle_update(
                session,
                _callback_payload(
                    telegram_user_id=ids["telegram_user_id"],
                    response_id=ids["response_id"],
                    action=action,
                ),
            )
    finally:
        telegram_module.TelegramClient = original

    assert result["action"] == "callback_owner_mismatch_ignored"
    assert any(c[0] == "answerCallbackQuery" for c in client.calls)
    assert "owned by telegram_user_id=None" in caplog.text
    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, ids["receipt_id"])
        response = session.get(AgentReceiptUserResponse, ids["response_id"])
        questions = session.exec(select(ClarificationQuestion)).all()

    assert response.user_action == "pending"
    assert response.user_action_at is None
    assert response.canonical_write_json is None
    assert receipt.status == "received"
    assert receipt.business_or_personal is None
    assert receipt.report_bucket is None
    assert receipt.category_source is None
    assert questions == []


@pytest.mark.parametrize(
    ("action", "expected_result", "expected_response_action"),
    (
        ("confirm", "callback_confirmed", "confirmed"),
        ("edit", "callback_edit_menu_shown", "edited"),
        ("cancel", "callback_cancelled", "cancelled"),
    ),
)
def test_callback_succeeds_when_telegram_user_matches_response_owner(
    isolated_db,
    action,
    expected_result,
    expected_response_action,
):
    # Ownership-positive regression: the same Telegram user that received
    # the keyboard can still use every button.
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
                    action=action,
                ),
            )
    finally:
        telegram_module.TelegramClient = original

    assert result["action"] == expected_result
    assert any(c[0] == "answerCallbackQuery" for c in client.calls)
    with Session(isolated_db) as session:
        response = session.get(AgentReceiptUserResponse, ids["response_id"])
        receipt = session.get(ReceiptDocument, ids["receipt_id"])
        questions = session.exec(select(ClarificationQuestion)).all()

    assert response.user_action == expected_response_action
    assert response.user_action_at is not None
    if action == "confirm":
        assert receipt.business_or_personal == "Business"
        assert receipt.category_source == "ai_advisory"
    elif action == "edit":
        # PR4: keyboard Edit shows the menu; no clarification question seeded.
        assert questions == []
    else:
        assert receipt.status == "cancelled"


# PR3 cross-receipt fallback test removed in PR4. The keyboard Edit flow
# no longer seeds clarification questions, so the cross-receipt routing
# path it guarded against is unreachable from the keyboard. The legacy
# clarifications.py path (still used for non-allowlisted users) keeps its
# own coverage in test_clarification_queue_receipt_scope.py.

# PR3 missing-seeded-question warning test removed in PR4. The new
# keyboard Edit flow never seeds a clarification, so the "missing seeded
# clarification" warning the test exercised is unreachable from the menu
# state machine. Awaiting_* states route through telegram_edit_parsers,
# not clarifications.py.


def test_legacy_user_text_reply_unchanged(isolated_db, monkeypatch):
    _set_ai_reply_env(monkeypatch)
    with Session(isolated_db) as session:
        user = AppUser(telegram_user_id=8038997793, display_name="Hakan")
        session.add(user)
        session.commit()
        session.refresh(user)

        old_receipt = ReceiptDocument(
            uploader_user_id=user.id,
            source="telegram",
            status="received",
            content_type="photo",
            extracted_supplier="Old Market",
            extracted_date=date(2026, 4, 1),
            extracted_local_amount=Decimal("12.00"),
            extracted_currency="TRY",
        )
        session.add(old_receipt)
        session.commit()
        session.refresh(old_receipt)
        session.add(
            ClarificationQuestion(
                receipt_document_id=old_receipt.id,
                user_id=user.id,
                question_key="telegram_market_context",
                question_text="Was this business or personal spending?",
            )
        )

        latest_receipt = ReceiptDocument(
            uploader_user_id=user.id,
            source="telegram",
            status="received",
            content_type="photo",
            extracted_supplier="Later Fuel",
            extracted_date=date(2026, 5, 1),
            extracted_local_amount=Decimal("50.00"),
            extracted_currency="TRY",
            report_bucket="Auto Gasoline",
            needs_clarification=False,
        )
        session.add(latest_receipt)
        session.commit()
        old_receipt_id = old_receipt.id

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            result = handle_update(
                session,
                _text_payload(
                    telegram_user_id=8038997793,
                    text="business, legacy market context",
                ),
            )
    finally:
        telegram_module.TelegramClient = original

    assert result["action"] == "answered_clarification"
    with Session(isolated_db) as session:
        old_receipt = session.get(ReceiptDocument, old_receipt_id)
        question = session.exec(select(ClarificationQuestion)).one()

    assert question.status == "answered"
    assert old_receipt.business_reason == "legacy market context"


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
