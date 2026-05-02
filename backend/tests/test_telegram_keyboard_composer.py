"""F-AI-Stage1 sub-PR 3: keyboard composer + callback_data round-trip."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from app.models import AgentReceiptRead, ReceiptDocument
from app.services.telegram_keyboard_composer import (
    CALLBACK_DATA_PREFIX,
    build_callback_data,
    build_inline_keyboard_reply,
    parse_callback_data,
)


def _agent_read(**kwargs) -> AgentReceiptRead:
    base = {
        "run_id": 1,
        "receipt_document_id": 1,
        "read_schema_version": "stage1",
        "read_json": "{}",
        "suggested_business_or_personal": "Business",
        "suggested_report_bucket": "Meals/Snacks",
        "suggested_attendees_json": json.dumps(["Hakan", "Burak Yilmaz"]),
        "suggested_customer": "DcExpense",
        "suggested_business_reason": "Team lunch debrief",
        "suggested_confidence_overall": 0.88,
    }
    base.update(kwargs)
    return AgentReceiptRead(**base)


def _receipt(**kwargs) -> ReceiptDocument:
    base = {
        "id": 1,
        "source": "telegram",
        "status": "received",
        "content_type": "photo",
        "extracted_supplier": "Acme Cafe",
        "extracted_date": date(2026, 5, 1),
        "extracted_local_amount": Decimal("42.50"),
        "extracted_currency": "TRY",
    }
    base.update(kwargs)
    return ReceiptDocument(**base)


def test_message_body_business_meal_full_snapshot():
    receipt = _receipt()
    agent_read = _agent_read()
    payload = build_inline_keyboard_reply(receipt, agent_read, user_response_id=42)

    expected = (
        "Receipt received.\n"
        "\n"
        "I read:\n"
        "Supplier: Acme Cafe\n"
        "Date: 2026-05-01\n"
        "Amount: 42.50 TRY\n"
        "\n"
        "AI suggests:\n"
        "Type: Business\n"
        "Bucket: Meals/Snacks\n"
        "Attendees: Hakan + Burak Yilmaz\n"
        "Customer: DcExpense\n"
        "Reason: Team lunch debrief"
    )
    assert payload["text"] == expected


def test_empty_attendees_omits_line():
    agent_read = _agent_read(suggested_attendees_json=json.dumps([]))
    payload = build_inline_keyboard_reply(_receipt(), agent_read, user_response_id=1)
    assert "Attendees:" not in payload["text"]


def test_none_customer_omits_line():
    agent_read = _agent_read(suggested_customer=None)
    payload = build_inline_keyboard_reply(_receipt(), agent_read, user_response_id=1)
    assert "Customer:" not in payload["text"]


def test_long_business_reason_truncates_to_200_chars():
    long_reason = "x" * 400
    agent_read = _agent_read(suggested_business_reason=long_reason)
    payload = build_inline_keyboard_reply(_receipt(), agent_read, user_response_id=1)
    reason_line = next(line for line in payload["text"].splitlines() if line.startswith("Reason:"))
    # Strip the "Reason: " prefix; rest should be ≤ 200 chars.
    body = reason_line[len("Reason: "):]
    assert len(body) <= 200
    assert body.endswith("…")


def test_callback_data_round_trips():
    data = build_callback_data("confirm", 12345)
    assert data == "fai1:confirm:12345"
    assert parse_callback_data(data) == ("confirm", 12345)


def test_parse_callback_data_rejects_garbage():
    assert parse_callback_data(None) is None
    assert parse_callback_data("") is None
    assert parse_callback_data("garbage") is None
    assert parse_callback_data("fai1:confirm") is None
    assert parse_callback_data("other:confirm:1") is None
    assert parse_callback_data("fai1:unknown:1") is None
    assert parse_callback_data("fai1:confirm:not-an-int") is None


def test_callback_data_within_64_byte_limit_for_realistic_ids():
    # 9-digit response id → "fai1:confirm:999999999" = 22 bytes
    for response_id in (1, 100, 999_999_999):
        for action in ("confirm", "edit", "cancel"):
            data = build_callback_data(action, response_id)
            assert len(data.encode("utf-8")) <= 64


def test_callback_data_prefix_constant_matches_format():
    data = build_callback_data("cancel", 7)
    assert data.startswith(f"{CALLBACK_DATA_PREFIX}:")


def test_reply_markup_has_three_buttons_in_one_row():
    payload = build_inline_keyboard_reply(_receipt(), _agent_read(), user_response_id=1)
    rows = payload["reply_markup"]["inline_keyboard"]
    assert len(rows) == 1
    assert len(rows[0]) == 3
    actions = [parse_callback_data(btn["callback_data"])[0] for btn in rows[0]]
    assert actions == ["confirm", "edit", "cancel"]
