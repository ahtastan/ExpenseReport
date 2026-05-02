"""F-AI-Stage1 PR4: integration tests for the button-driven Edit menu.

Covers the full state machine from the keyboard-Edit tap through every
sub-menu and field commit, including the source-tag invariants on Confirm.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlmodel import Session, select

from app.models import (
    AgentReceiptRead,
    AgentReceiptReviewRun,
    AgentReceiptUserResponse,
    AppUser,
    ClarificationQuestion,
    ReceiptDocument,
)
from app.services.telegram import handle_update


# ─── helpers ────────────────────────────────────────────────────────────────


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

    def download_file(self, file_id, user_id, fallback_name):  # pragma: no cover
        return None


def _patch_telegram_client(client: _FakeClient):
    import app.services.telegram as telegram_module

    original = telegram_module.TelegramClient
    telegram_module.TelegramClient = lambda *_a, **_k: client  # type: ignore[assignment]
    return telegram_module, original


def _enable_keyboard_env(monkeypatch, *, allowlist: str = "8038997793") -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("AI_TELEGRAM_REPLY_ENABLED", "true")
    monkeypatch.setenv("AI_TELEGRAM_LIVE_MODEL_ENABLED", "true")
    monkeypatch.setenv("AI_TELEGRAM_INLINE_KEYBOARD_ENABLED", "true")
    monkeypatch.setenv("AI_TELEGRAM_REPLY_ALLOWLIST", allowlist)
    from app.config import get_settings

    get_settings.cache_clear()


def _seed_pending_response(
    session: Session,
    *,
    telegram_user_id: int = 8038997793,
    receipt_supplier: str = "Acme Cafe",
    receipt_business_or_personal: str | None = "Business",
    receipt_category_source: str | None = "auto_confirmed_default",
    receipt_report_bucket: str | None = None,
    suggested_business_or_personal: str | None = "Business",
    suggested_report_bucket: str | None = "Meals/Snacks",
    suggested_attendees: list[str] | None = None,
    suggested_business_reason: str | None = "Team lunch",
) -> dict[str, int]:
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
        category_source=receipt_category_source,
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
    read = AgentReceiptRead(
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
    session.add(read)
    session.commit()
    session.refresh(read)

    response = AgentReceiptUserResponse(
        receipt_document_id=receipt.id,
        agent_receipt_review_run_id=run.id,
        agent_receipt_read_id=read.id,
        telegram_user_id=user.telegram_user_id,
        keyboard_message_id=555,
        user_action="pending",
    )
    session.add(response)
    session.commit()
    session.refresh(response)

    return {
        "user_id": user.id,
        "telegram_user_id": user.telegram_user_id,
        "receipt_id": receipt.id,
        "run_id": run.id,
        "agent_read_id": read.id,
        "response_id": response.id,
    }


def _callback(action: str, response_id: int, telegram_user_id: int = 8038997793) -> dict[str, Any]:
    """Top-level callback for confirm/edit/cancel."""
    return {
        "callback_query": {
            "id": "cbk-top",
            "from": {"id": telegram_user_id, "first_name": "Hakan"},
            "data": f"fai1:{action}:{response_id}",
            "message": {"message_id": 555, "chat": {"id": 42}},
        }
    }


def _menu_callback(
    scope: str,
    choice: str,
    response_id: int,
    *,
    telegram_user_id: int = 8038997793,
) -> dict[str, Any]:
    """Menu-navigation callback (fai1m prefix)."""
    return {
        "callback_query": {
            "id": f"cbk-{scope}-{choice}",
            "from": {"id": telegram_user_id, "first_name": "Hakan"},
            "data": f"fai1m:{scope}:{choice}:{response_id}",
            "message": {"message_id": 555, "chat": {"id": 42}},
        }
    }


def _text(text: str, telegram_user_id: int = 8038997793) -> dict[str, Any]:
    return {
        "message": {
            "message_id": 9001,
            "from": {"id": telegram_user_id, "first_name": "Hakan"},
            "chat": {"id": 42},
            "text": text,
        }
    }


def _last_edit_text(client: _FakeClient) -> dict[str, Any]:
    edits = [c[1] for c in client.calls if c[0] == "editMessageText"]
    assert edits, "expected at least one editMessageText call"
    return edits[-1]


def _button_labels(reply_markup_str: str) -> list[str]:
    """Pull every button label out of a serialized reply_markup. Emojis
    inside button text are JSON-escaped (\\uXXXX) by the dispatcher's
    serializer so we round-trip the JSON to inspect the readable text."""
    parsed = json.loads(reply_markup_str)
    labels: list[str] = []
    for row in parsed.get("inline_keyboard", []):
        for btn in row:
            label = btn.get("text", "")
            if label:
                labels.append(label)
    return labels


def _markup_callback_datas(reply_markup_str: str) -> list[str]:
    """Return every callback_data payload, helpful for asserting menu
    structure without relying on emoji glyph encoding."""
    parsed = json.loads(reply_markup_str)
    out: list[str] = []
    for row in parsed.get("inline_keyboard", []):
        for btn in row:
            cd = btn.get("callback_data", "")
            if cd:
                out.append(cd)
    return out


# ─── top-level Edit menu ─────────────────────────────────────────────────────


def test_edit_menu_top_level_buttons_for_allowlisted_user(isolated_db, monkeypatch):
    """Allowlisted user sees Receipt info / Category / Type / Back."""
    _enable_keyboard_env(monkeypatch)
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            result = handle_update(session, _callback("edit", ids["response_id"]))
    finally:
        telegram_module.TelegramClient = original

    assert result["action"] == "callback_edit_menu_shown"
    last = _last_edit_text(client)
    labels = _button_labels(last["reply_markup"])
    # Type button only visible to allowlisted users.
    assert any("Type" in lbl for lbl in labels)
    assert any("Receipt info" in lbl for lbl in labels)
    assert any("Category" in lbl for lbl in labels)
    assert any("Back" in lbl for lbl in labels)


def test_edit_menu_top_level_buttons_for_non_allowlisted_user_no_type(
    isolated_db, monkeypatch
):
    """Non-allowlisted user does NOT see the Type button — but still sees
    Receipt info / Category / Back. Note: the keyboard-flow gate on
    Edit (should_use_inline_keyboard) usually means non-allowlisted users
    never receive a keyboard at all; this test exercises the menu builder
    directly via a callback against a manually-seeded response."""
    # Allowlist contains a different user; the seeded user is NOT in it.
    _enable_keyboard_env(monkeypatch, allowlist="9999")
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session, telegram_user_id=8038997793)

    # Allow this user past the broader "allowed_telegram_user_ids" gate so
    # the callback handler dispatches; they're just not in the keyboard
    # allowlist that authorizes the Type button.
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "8038997793")
    from app.config import get_settings

    get_settings.cache_clear()

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update(session, _callback("edit", ids["response_id"]))
    finally:
        telegram_module.TelegramClient = original

    last = _last_edit_text(client)
    labels = _button_labels(last["reply_markup"])
    assert not any("Type" in lbl for lbl in labels), (
        "Type button must be hidden for non-allowlisted users"
    )
    assert any("Receipt info" in lbl for lbl in labels)
    assert any("Category" in lbl for lbl in labels)


# ─── Receipt info menu ──────────────────────────────────────────────────────


def test_edit_receipt_menu_navigation(isolated_db, monkeypatch):
    _enable_keyboard_env(monkeypatch)
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        # Tap Edit to open the top-level menu.
        with Session(isolated_db) as session:
            handle_update(session, _callback("edit", ids["response_id"]))
        # Tap Receipt info.
        with Session(isolated_db) as session:
            r = handle_update(session, _menu_callback("edit", "receipt", ids["response_id"]))
        assert r["action"] == "callback_menu_receipt_shown"
        last = _last_edit_text(client)
        labels = _button_labels(last["reply_markup"])
        assert any("Supplier" in lbl for lbl in labels)
        assert any("Date" in lbl for lbl in labels)
        assert any("Amount" in lbl for lbl in labels)
        # Back to top-level.
        with Session(isolated_db) as session:
            r = handle_update(session, _menu_callback("rcpt", "back", ids["response_id"]))
        assert r["action"] == "callback_menu_receipt_back"
        last = _last_edit_text(client)
        labels = _button_labels(last["reply_markup"])
        assert any("Receipt info" in lbl for lbl in labels)
    finally:
        telegram_module.TelegramClient = original


def test_edit_supplier_text_reply_updates_field(isolated_db, monkeypatch):
    _enable_keyboard_env(monkeypatch)
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session, receipt_supplier="Old Supplier")

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update(session, _callback("edit", ids["response_id"]))
        with Session(isolated_db) as session:
            handle_update(session, _menu_callback("edit", "receipt", ids["response_id"]))
        with Session(isolated_db) as session:
            r = handle_update(session, _menu_callback("rcpt", "supplier", ids["response_id"]))
        assert r["action"] == "callback_menu_receipt_field_prompted"
        # Reply with the new supplier name.
        with Session(isolated_db) as session:
            r = handle_update(session, _text("New Supplier Inc"))
        assert r["action"] == "awaiting_text_field_saved"
    finally:
        telegram_module.TelegramClient = original

    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, ids["receipt_id"])
        response = session.get(AgentReceiptUserResponse, ids["response_id"])
    assert receipt.extracted_supplier == "New Supplier Inc"
    assert response.user_action == "edited"


def test_edit_date_text_reply_iso_format(isolated_db, monkeypatch):
    _enable_keyboard_env(monkeypatch)
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update(session, _callback("edit", ids["response_id"]))
            handle_update(session, _menu_callback("edit", "receipt", ids["response_id"]))
            handle_update(session, _menu_callback("rcpt", "date", ids["response_id"]))
            handle_update(session, _text("2026-03-19"))
    finally:
        telegram_module.TelegramClient = original

    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, ids["receipt_id"])
    assert receipt.extracted_date == date(2026, 3, 19)


def test_edit_date_text_reply_tr_format(isolated_db, monkeypatch):
    _enable_keyboard_env(monkeypatch)
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update(session, _callback("edit", ids["response_id"]))
            handle_update(session, _menu_callback("edit", "receipt", ids["response_id"]))
            handle_update(session, _menu_callback("rcpt", "date", ids["response_id"]))
            handle_update(session, _text("19.03.2026"))
    finally:
        telegram_module.TelegramClient = original

    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, ids["receipt_id"])
    assert receipt.extracted_date == date(2026, 3, 19)


def test_edit_date_text_reply_invalid_reprompts(isolated_db, monkeypatch):
    """A bad date stays in awaiting_date and re-prompts; doesn't advance."""
    _enable_keyboard_env(monkeypatch)
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)
        original_date = session.get(ReceiptDocument, ids["receipt_id"]).extracted_date

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update(session, _callback("edit", ids["response_id"]))
            handle_update(session, _menu_callback("edit", "receipt", ids["response_id"]))
            handle_update(session, _menu_callback("rcpt", "date", ids["response_id"]))
        with Session(isolated_db) as session:
            r = handle_update(session, _text("not a date"))
    finally:
        telegram_module.TelegramClient = original

    assert r["action"] == "awaiting_date_reprompt"
    assert any("YYYY-MM-DD" in msg for _chat, msg in client.send_messages)
    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, ids["receipt_id"])
        response = session.get(AgentReceiptUserResponse, ids["response_id"])
    # Field unchanged.
    assert receipt.extracted_date == original_date
    # Still awaiting — the user can try again.
    assert response.user_action == "awaiting_date"


def test_edit_amount_text_reply_period_decimal(isolated_db, monkeypatch):
    _enable_keyboard_env(monkeypatch)
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update(session, _callback("edit", ids["response_id"]))
            handle_update(session, _menu_callback("edit", "receipt", ids["response_id"]))
            handle_update(session, _menu_callback("rcpt", "amount", ids["response_id"]))
            handle_update(session, _text("755.00 TRY"))
    finally:
        telegram_module.TelegramClient = original

    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, ids["receipt_id"])
    assert receipt.extracted_local_amount == Decimal("755.00")
    assert receipt.extracted_currency == "TRY"


def test_edit_amount_text_reply_comma_decimal(isolated_db, monkeypatch):
    _enable_keyboard_env(monkeypatch)
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update(session, _callback("edit", ids["response_id"]))
            handle_update(session, _menu_callback("edit", "receipt", ids["response_id"]))
            handle_update(session, _menu_callback("rcpt", "amount", ids["response_id"]))
            handle_update(session, _text("755,00 TRY"))
    finally:
        telegram_module.TelegramClient = original

    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, ids["receipt_id"])
    assert receipt.extracted_local_amount == Decimal("755.00")
    assert receipt.extracted_currency == "TRY"


def test_edit_amount_text_reply_invalid_reprompts(isolated_db, monkeypatch):
    _enable_keyboard_env(monkeypatch)
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update(session, _callback("edit", ids["response_id"]))
            handle_update(session, _menu_callback("edit", "receipt", ids["response_id"]))
            handle_update(session, _menu_callback("rcpt", "amount", ids["response_id"]))
        with Session(isolated_db) as session:
            r = handle_update(session, _text("hello"))
    finally:
        telegram_module.TelegramClient = original

    assert r["action"] == "awaiting_amount_reprompt"
    with Session(isolated_db) as session:
        response = session.get(AgentReceiptUserResponse, ids["response_id"])
    assert response.user_action == "awaiting_amount"


# ─── Category Tier 1 / Tier 2 ────────────────────────────────────────────────


def test_edit_category_tier1_menu(isolated_db, monkeypatch):
    """Tier 1 menu shows 4 categories from category_vocab.categories()."""
    _enable_keyboard_env(monkeypatch)
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update(session, _callback("edit", ids["response_id"]))
            r = handle_update(session, _menu_callback("edit", "category", ids["response_id"]))
    finally:
        telegram_module.TelegramClient = original

    assert r["action"] == "callback_menu_cat1_shown"
    last = _last_edit_text(client)
    labels = _button_labels(last["reply_markup"])
    # All four non-empty Tier 1 categories visible. Personal Car omitted.
    assert any("Hotel & Travel" in lbl for lbl in labels)
    assert any("Meals & Entertainment" in lbl for lbl in labels)
    assert any("Air Travel" in lbl for lbl in labels)
    assert any("Other" in lbl for lbl in labels)
    assert not any("Personal Car" in lbl for lbl in labels)


def test_edit_category_tier2_menu_for_meals_entertainment(isolated_db, monkeypatch):
    """Tier 2 menu for Meals & Entertainment lists exactly its 5 buckets."""
    _enable_keyboard_env(monkeypatch)
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)

    from app.category_vocab import categories

    cats = categories()
    meals_idx = cats.index("Meals & Entertainment")

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update(session, _callback("edit", ids["response_id"]))
            handle_update(session, _menu_callback("edit", "category", ids["response_id"]))
            r = handle_update(
                session, _menu_callback("cat1", str(meals_idx), ids["response_id"])
            )
    finally:
        telegram_module.TelegramClient = original

    assert r["action"] == "callback_menu_cat2_shown"
    assert r["category"] == "Meals & Entertainment"
    last = _last_edit_text(client)
    labels = _button_labels(last["reply_markup"])
    for bucket in ("Meals/Snacks", "Breakfast", "Lunch", "Dinner", "Entertainment"):
        assert any(bucket in lbl for lbl in labels), (
            f"expected bucket {bucket} in tier-2 menu"
        )


def test_edit_category_tier2_menu_for_hotel_travel(isolated_db, monkeypatch):
    """Tier 2 menu for Hotel & Travel lists exactly its 5 buckets."""
    _enable_keyboard_env(monkeypatch)
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)

    from app.category_vocab import categories

    cats = categories()
    hotel_idx = cats.index("Hotel & Travel")

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update(session, _callback("edit", ids["response_id"]))
            handle_update(session, _menu_callback("edit", "category", ids["response_id"]))
            handle_update(
                session, _menu_callback("cat1", str(hotel_idx), ids["response_id"])
            )
    finally:
        telegram_module.TelegramClient = original

    last = _last_edit_text(client)
    labels = _button_labels(last["reply_markup"])
    for bucket in (
        "Hotel/Lodging/Laundry", "Auto Rental", "Auto Gasoline",
        "Taxi/Parking/Tolls/Uber", "Other Travel Related",
    ):
        assert any(bucket in lbl for lbl in labels)


def test_bucket_commit_on_meals_triggers_reason_attendees_prompt(isolated_db, monkeypatch):
    """After picking a Meals bucket, the response is in
    awaiting_attendees_reason and the user sees a Skip-button prompt."""
    _enable_keyboard_env(monkeypatch)
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)

    from app.category_vocab import all_buckets

    flat = all_buckets()
    lunch_idx = flat.index("Lunch")

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update(session, _callback("edit", ids["response_id"]))
            handle_update(session, _menu_callback("edit", "category", ids["response_id"]))
            from app.category_vocab import categories

            cats = categories()
            handle_update(
                session,
                _menu_callback("cat1", str(cats.index("Meals & Entertainment")), ids["response_id"]),
            )
            r = handle_update(
                session, _menu_callback("cat2", str(lunch_idx), ids["response_id"])
            )
    finally:
        telegram_module.TelegramClient = original

    assert r["action"] == "callback_menu_cat2_committed_awaiting_ra"
    assert r["bucket"] == "Lunch"
    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, ids["receipt_id"])
        response = session.get(AgentReceiptUserResponse, ids["response_id"])
    assert receipt.report_bucket == "Lunch"
    assert receipt.bucket_source == "telegram_user"
    assert response.user_action == "awaiting_attendees_reason"
    last = _last_edit_text(client)
    assert "attendees" in last["text"].lower()
    labels = _button_labels(last["reply_markup"])
    assert any("Skip" in lbl for lbl in labels)


def test_bucket_commit_on_non_meals_returns_to_top_keyboard(isolated_db, monkeypatch):
    """Picking a non-Meals bucket commits and returns to the
    Confirm/Edit/Cancel keyboard (no reason/attendees prompt)."""
    _enable_keyboard_env(monkeypatch)
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)

    from app.category_vocab import all_buckets, categories

    flat = all_buckets()
    cats = categories()
    auto_gas_idx = flat.index("Auto Gasoline")
    hotel_idx = cats.index("Hotel & Travel")

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update(session, _callback("edit", ids["response_id"]))
            handle_update(session, _menu_callback("edit", "category", ids["response_id"]))
            handle_update(session, _menu_callback("cat1", str(hotel_idx), ids["response_id"]))
            r = handle_update(
                session, _menu_callback("cat2", str(auto_gas_idx), ids["response_id"])
            )
    finally:
        telegram_module.TelegramClient = original

    assert r["action"] == "callback_menu_cat2_committed"
    assert r["bucket"] == "Auto Gasoline"
    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, ids["receipt_id"])
        response = session.get(AgentReceiptUserResponse, ids["response_id"])
    assert receipt.report_bucket == "Auto Gasoline"
    assert receipt.bucket_source == "telegram_user"
    assert response.user_action == "edited"
    # Last edit reverts to the Confirm/Edit/Cancel keyboard.
    last = _last_edit_text(client)
    labels = _button_labels(last["reply_markup"])
    assert any("Confirm" in lbl for lbl in labels)


def test_attendees_reason_text_reply_parsed_correctly(isolated_db, monkeypatch):
    """After picking Lunch, replying with attendees+reason fills both fields
    with telegram_user source and returns to the Confirm keyboard."""
    _enable_keyboard_env(monkeypatch)
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)

    from app.category_vocab import all_buckets, categories

    flat = all_buckets()
    cats = categories()

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update(session, _callback("edit", ids["response_id"]))
            handle_update(session, _menu_callback("edit", "category", ids["response_id"]))
            handle_update(
                session,
                _menu_callback("cat1", str(cats.index("Meals & Entertainment")), ids["response_id"]),
            )
            handle_update(
                session,
                _menu_callback("cat2", str(flat.index("Lunch")), ids["response_id"]),
            )
        with Session(isolated_db) as session:
            r = handle_update(session, _text("Hakan, Burak; team lunch"))
    finally:
        telegram_module.TelegramClient = original

    assert r["action"] == "awaiting_attendees_reason_saved"
    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, ids["receipt_id"])
        response = session.get(AgentReceiptUserResponse, ids["response_id"])
    assert receipt.attendees == "Hakan, Burak"
    assert receipt.attendees_source == "telegram_user"
    assert receipt.business_reason == "team lunch"
    assert receipt.business_reason_source == "telegram_user"
    assert response.user_action == "edited"


def test_attendees_reason_text_reply_no_semicolon_reprompts(isolated_db, monkeypatch):
    """Missing semicolon → reprompt and stay in awaiting_attendees_reason."""
    _enable_keyboard_env(monkeypatch)
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)

    from app.category_vocab import all_buckets, categories

    flat = all_buckets()
    cats = categories()

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update(session, _callback("edit", ids["response_id"]))
            handle_update(session, _menu_callback("edit", "category", ids["response_id"]))
            handle_update(
                session,
                _menu_callback("cat1", str(cats.index("Meals & Entertainment")), ids["response_id"]),
            )
            handle_update(
                session,
                _menu_callback("cat2", str(flat.index("Lunch")), ids["response_id"]),
            )
        with Session(isolated_db) as session:
            r = handle_update(session, _text("Hakan and team lunch"))
    finally:
        telegram_module.TelegramClient = original

    assert r["action"] == "awaiting_attendees_reason_reprompt"
    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, ids["receipt_id"])
        response = session.get(AgentReceiptUserResponse, ids["response_id"])
    # Fields untouched.
    assert receipt.attendees is None
    assert receipt.business_reason is None
    assert response.user_action == "awaiting_attendees_reason"
    # Format reminder sent.
    assert any("attendees" in m.lower() and "reason" in m.lower()
               for _chat, m in client.send_messages)


def test_skip_reason_attendees_button_sets_needs_clarification(isolated_db, monkeypatch):
    """Skip button after Meals bucket: needs_clarification=True, no
    attendees/reason write, return to top keyboard."""
    _enable_keyboard_env(monkeypatch)
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)

    from app.category_vocab import all_buckets, categories

    flat = all_buckets()
    cats = categories()

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update(session, _callback("edit", ids["response_id"]))
            handle_update(session, _menu_callback("edit", "category", ids["response_id"]))
            handle_update(
                session,
                _menu_callback("cat1", str(cats.index("Meals & Entertainment")), ids["response_id"]),
            )
            handle_update(
                session,
                _menu_callback("cat2", str(flat.index("Lunch")), ids["response_id"]),
            )
        with Session(isolated_db) as session:
            r = handle_update(session, _menu_callback("skip_ra", "", ids["response_id"]))
    finally:
        telegram_module.TelegramClient = original

    assert r["action"] == "callback_menu_skip_ra_done"
    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, ids["receipt_id"])
        response = session.get(AgentReceiptUserResponse, ids["response_id"])
    assert receipt.needs_clarification is True
    assert receipt.attendees is None
    assert receipt.business_reason is None
    assert response.user_action == "edited"


def test_skip_then_confirm_preserves_needs_clarification(isolated_db, monkeypatch):
    """Skip-for-now on Meals attendees+reason then Confirm: the
    'review me later' signal must survive the AI-advisory write that
    Confirm triggers. The canonical writer otherwise auto-clears
    ``needs_clarification`` whenever it writes any field."""
    _enable_keyboard_env(monkeypatch)
    with Session(isolated_db) as session:
        ids = _seed_pending_response(
            session,
            suggested_business_or_personal="Business",
            suggested_report_bucket="Meals/Snacks",
            suggested_attendees=["AI proposed attendee"],
            suggested_business_reason="AI proposed reason",
        )

    from app.category_vocab import all_buckets, categories

    flat = all_buckets()
    cats = categories()

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update(session, _callback("edit", ids["response_id"]))
            handle_update(session, _menu_callback("edit", "category", ids["response_id"]))
            handle_update(
                session,
                _menu_callback("cat1", str(cats.index("Meals & Entertainment")), ids["response_id"]),
            )
            handle_update(
                session,
                _menu_callback("cat2", str(flat.index("Lunch")), ids["response_id"]),
            )
            handle_update(session, _menu_callback("skip_ra", "", ids["response_id"]))
        with Session(isolated_db) as session:
            r = handle_update(session, _callback("confirm", ids["response_id"]))
    finally:
        telegram_module.TelegramClient = original

    assert r["action"] == "callback_confirmed"
    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, ids["receipt_id"])
        response = session.get(AgentReceiptUserResponse, ids["response_id"])
    # The "user pressed Skip" signal must persist past Confirm. The
    # writer detects the ``telegram_user_skipped`` sentinel on attendees
    # / business_reason source and skips its usual auto-clear of
    # ``needs_clarification``.
    assert receipt.needs_clarification is True
    # User-picked bucket stays sticky.
    assert receipt.report_bucket == "Lunch"
    assert receipt.bucket_source == "telegram_user"
    # Per Phase 4 spec, AI's proposal lands on Confirm with ai_advisory
    # source for fields the user didn't explicitly set. The Skip-for-now
    # sentinel does not block this — it only preserves the flag.
    assert receipt.attendees == "AI proposed attendee"
    assert receipt.attendees_source == "ai_advisory"
    assert receipt.business_reason == "AI proposed reason"
    assert receipt.business_reason_source == "ai_advisory"
    assert response.user_action == "confirmed"


# ─── Type toggle ────────────────────────────────────────────────────────────


def test_type_toggle_personal_clears_fields_and_immediately_confirms(
    isolated_db, monkeypatch
):
    """Mark Personal: business_or_personal=Personal, report_bucket=Other,
    attendees and business_reason cleared, response finalized as confirmed."""
    _enable_keyboard_env(monkeypatch)
    with Session(isolated_db) as session:
        ids = _seed_pending_response(
            session,
            receipt_business_or_personal="Business",
            receipt_category_source="auto_confirmed_default",
            receipt_report_bucket="Meals/Snacks",
        )
        # AI proposed reason and attendees that should be wiped.
        receipt = session.get(ReceiptDocument, ids["receipt_id"])
        receipt.business_reason = "AI proposed reason"
        receipt.attendees = "AI attendees"
        session.add(receipt)
        session.commit()

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update(session, _callback("edit", ids["response_id"]))
            handle_update(session, _menu_callback("edit", "type", ids["response_id"]))
        with Session(isolated_db) as session:
            r = handle_update(session, _menu_callback("type", "personal", ids["response_id"]))
    finally:
        telegram_module.TelegramClient = original

    assert r["action"] == "callback_menu_type_personal_confirmed"
    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, ids["receipt_id"])
        response = session.get(AgentReceiptUserResponse, ids["response_id"])
    assert receipt.business_or_personal == "Personal"
    assert receipt.category_source == "telegram_user"
    assert receipt.report_bucket == "Other"
    assert receipt.bucket_source == "telegram_user"
    assert receipt.business_reason is None
    assert receipt.business_reason_source == "telegram_user"
    assert receipt.attendees is None
    assert receipt.attendees_source == "telegram_user"
    assert response.user_action == "confirmed"
    # Final reply text edits the message in place.
    last = _last_edit_text(client)
    assert last["text"] == "✅ Confirmed as Personal."


def test_type_toggle_hidden_for_non_allowlisted_user(isolated_db, monkeypatch):
    """Non-allowlisted user attempting the Type sub-menu callback is
    blocked at the dispatch helper. The keyboard wouldn't even render the
    button — but a malicious tap with hand-crafted callback_data is
    rejected on the server side too."""
    _enable_keyboard_env(monkeypatch, allowlist="9999")
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session, telegram_user_id=8038997793)

    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "8038997793")
    from app.config import get_settings

    get_settings.cache_clear()

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update(session, _callback("edit", ids["response_id"]))
        with Session(isolated_db) as session:
            r = handle_update(session, _menu_callback("edit", "type", ids["response_id"]))
        # Even direct 'type:personal' callback is blocked for non-allowlisted user.
        with Session(isolated_db) as session:
            r2 = handle_update(session, _menu_callback("type", "personal", ids["response_id"]))
    finally:
        telegram_module.TelegramClient = original

    assert r["action"] == "callback_menu_type_not_allowed"
    assert r2["action"] == "callback_menu_type_not_allowed"
    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, ids["receipt_id"])
    # Personal toggle did NOT happen.
    assert receipt.business_or_personal != "Personal"


def test_type_toggle_personal_includes_in_report_under_other_other(
    isolated_db, monkeypatch
):
    """Integration: Personal receipt should land in report_generator's
    'other' day bucket. The allocator routes any non-Business row to
    day['other'], which writes to workbook row 26. We don't run the full
    Excel pipeline here — we just verify the ReceiptDocument shape that
    drives the allocator decision."""
    _enable_keyboard_env(monkeypatch)
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update(session, _callback("edit", ids["response_id"]))
            handle_update(session, _menu_callback("edit", "type", ids["response_id"]))
            handle_update(session, _menu_callback("type", "personal", ids["response_id"]))
    finally:
        telegram_module.TelegramClient = original

    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, ids["receipt_id"])

    # report_generator._allocate routes on receipt.business_or_personal: any
    # non-Business value goes into day["other"] (row 26 in the workbook).
    assert receipt.business_or_personal == "Personal"
    assert receipt.business_or_personal != "business"  # case-sensitive guard
    # report_bucket is Other but the workbook's allocator looks at
    # business_or_personal first and short-circuits to 'other' bucket.
    assert receipt.report_bucket == "Other"


# ─── Confirm path with partial edits ────────────────────────────────────────


def test_confirm_after_partial_edit_preserves_user_tagged_fields(
    isolated_db, monkeypatch
):
    """User edits the bucket via the menu, then taps Confirm. The bucket
    keeps source='telegram_user'; other fields take the AI proposal with
    source='ai_advisory'."""
    _enable_keyboard_env(monkeypatch)
    with Session(isolated_db) as session:
        ids = _seed_pending_response(
            session,
            suggested_business_or_personal="Business",
            suggested_report_bucket="Meals/Snacks",
            suggested_attendees=["Hakan"],
            suggested_business_reason="Team lunch",
        )

    from app.category_vocab import all_buckets, categories

    flat = all_buckets()
    cats = categories()
    auto_gas_idx = flat.index("Auto Gasoline")
    hotel_idx = cats.index("Hotel & Travel")

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            # Open the menu, navigate to Hotel & Travel > Auto Gasoline.
            handle_update(session, _callback("edit", ids["response_id"]))
            handle_update(session, _menu_callback("edit", "category", ids["response_id"]))
            handle_update(session, _menu_callback("cat1", str(hotel_idx), ids["response_id"]))
            handle_update(session, _menu_callback("cat2", str(auto_gas_idx), ids["response_id"]))
        with Session(isolated_db) as session:
            r = handle_update(session, _callback("confirm", ids["response_id"]))
    finally:
        telegram_module.TelegramClient = original

    assert r["action"] == "callback_confirmed"
    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, ids["receipt_id"])
        response = session.get(AgentReceiptUserResponse, ids["response_id"])

    # Bucket: user-edited, must be sticky.
    assert receipt.report_bucket == "Auto Gasoline"
    assert receipt.bucket_source == "telegram_user"
    # Type: from AI proposal because user didn't touch it. The upload-time
    # auto_confirmed_default tag should NOT block the AI overwrite.
    assert receipt.business_or_personal == "Business"
    assert receipt.category_source == "ai_advisory"
    # Reason and attendees: from AI proposal.
    assert receipt.business_reason == "Team lunch"
    assert receipt.business_reason_source == "ai_advisory"
    assert receipt.attendees == "Hakan"
    assert receipt.attendees_source == "ai_advisory"
    assert response.user_action == "confirmed"


def test_confirm_after_attendees_reason_edit_preserves_them(
    isolated_db, monkeypatch
):
    """User commits Meals bucket and replies with attendees/reason. On
    Confirm, AI proposal does not overwrite the user-edited values."""
    _enable_keyboard_env(monkeypatch)
    with Session(isolated_db) as session:
        ids = _seed_pending_response(
            session,
            suggested_business_or_personal="Business",
            suggested_report_bucket="Meals/Snacks",
            suggested_attendees=["AI attendees"],
            suggested_business_reason="AI proposed reason",
        )

    from app.category_vocab import all_buckets, categories

    flat = all_buckets()
    cats = categories()
    lunch_idx = flat.index("Lunch")
    meals_idx = cats.index("Meals & Entertainment")

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update(session, _callback("edit", ids["response_id"]))
            handle_update(session, _menu_callback("edit", "category", ids["response_id"]))
            handle_update(session, _menu_callback("cat1", str(meals_idx), ids["response_id"]))
            handle_update(session, _menu_callback("cat2", str(lunch_idx), ids["response_id"]))
        with Session(isolated_db) as session:
            handle_update(session, _text("Hakan, Burak; quarterly review dinner"))
        with Session(isolated_db) as session:
            handle_update(session, _callback("confirm", ids["response_id"]))
    finally:
        telegram_module.TelegramClient = original

    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, ids["receipt_id"])

    assert receipt.report_bucket == "Lunch"
    assert receipt.bucket_source == "telegram_user"
    assert receipt.attendees == "Hakan, Burak"
    assert receipt.attendees_source == "telegram_user"
    assert receipt.business_reason == "quarterly review dinner"
    assert receipt.business_reason_source == "telegram_user"


def test_back_navigation_from_receipt_menu(isolated_db, monkeypatch):
    """Back from Receipt menu returns to top-level Edit menu."""
    _enable_keyboard_env(monkeypatch)
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update(session, _callback("edit", ids["response_id"]))
            handle_update(session, _menu_callback("edit", "receipt", ids["response_id"]))
            r = handle_update(session, _menu_callback("rcpt", "back", ids["response_id"]))
    finally:
        telegram_module.TelegramClient = original

    assert r["action"] == "callback_menu_receipt_back"


def test_back_navigation_from_top_edit_menu(isolated_db, monkeypatch):
    """Back from top Edit menu returns to Confirm/Edit/Cancel keyboard."""
    _enable_keyboard_env(monkeypatch)
    with Session(isolated_db) as session:
        ids = _seed_pending_response(session)

    client = _FakeClient()
    telegram_module, original = _patch_telegram_client(client)
    try:
        with Session(isolated_db) as session:
            handle_update(session, _callback("edit", ids["response_id"]))
            r = handle_update(session, _menu_callback("edit", "back", ids["response_id"]))
    finally:
        telegram_module.TelegramClient = original

    assert r["action"] == "callback_menu_back_to_top"
    last = _last_edit_text(client)
    # The top-level keyboard text.
    labels = _button_labels(last["reply_markup"])
    assert any("Confirm" in lbl for lbl in labels)
