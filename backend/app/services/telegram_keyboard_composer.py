"""F-AI-Stage1 sub-PR 3: build the three-button inline-keyboard reply.

Composes a Telegram ``sendMessage`` payload containing the canonical OCR
read, the AI proposal (``suggested_*`` columns from the
``receipt_inline_keyboard`` run), and three callback buttons:
``[✅ Confirm] [✏️ Edit] [❌ Cancel]``.

Callback data uses the format ``fai1:<action>:<user_response_id>`` where
``action`` is ``confirm`` / ``edit`` / ``cancel``. Total length stays
within Telegram's 64-byte limit for any reasonable response id.

Reference: docs/F-AI-Stage1-Telegram-Inline-Keyboard.md §5.3
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

from app.models import AgentReceiptRead, ReceiptDocument

logger = logging.getLogger(__name__)

CALLBACK_DATA_PREFIX = "fai1"
CALLBACK_ACTIONS = ("confirm", "edit", "cancel")
_CALLBACK_DATA_MAX_BYTES = 64
_BUSINESS_REASON_DISPLAY_LIMIT = 200


def build_inline_keyboard_reply(
    receipt: ReceiptDocument,
    agent_read: AgentReceiptRead,
    user_response_id: int,
) -> dict[str, Any]:
    """Build the Telegram ``sendMessage`` payload (text + reply_markup).

    Args:
        receipt: the canonical receipt the keyboard is anchored to.
        agent_read: the ``agent_receipt_read`` row whose ``suggested_*``
            columns hold the AI proposal.
        user_response_id: id of the ``agent_receipt_user_response`` row
            (must already exist with ``user_action='pending'``).

    Returns:
        ``{"text": ..., "reply_markup": {...}}`` ready to be passed as
        the body of a Telegram ``sendMessage`` call (the bot client will
        ``json.dumps`` ``reply_markup`` per Telegram's wire format).
    """
    text = _build_message_body(receipt, agent_read)
    reply_markup = _build_reply_markup(user_response_id)
    return {"text": text, "reply_markup": reply_markup}


def parse_callback_data(data: str | None) -> tuple[str, int] | None:
    """Inverse of the format used by :func:`build_inline_keyboard_reply`.

    Returns ``(action, user_response_id)`` or ``None`` when ``data`` is
    malformed (wrong prefix, unknown action, non-integer id, missing
    parts). Callers handle ``None`` by silently dismissing the callback
    (no exception raised).
    """
    if not isinstance(data, str) or not data:
        return None
    parts = data.split(":")
    if len(parts) != 3:
        return None
    prefix, action, raw_id = parts
    if prefix != CALLBACK_DATA_PREFIX:
        return None
    if action not in CALLBACK_ACTIONS:
        return None
    try:
        user_response_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    return action, user_response_id


def _build_message_body(receipt: ReceiptDocument, agent_read: AgentReceiptRead) -> str:
    lines: list[str] = ["Receipt received.", ""]
    read_lines = _canonical_read_lines(receipt)
    if read_lines:
        lines.append("I read:")
        lines.extend(read_lines)
        lines.append("")
    suggestion_lines = _suggestion_lines(agent_read)
    if suggestion_lines:
        lines.append("AI suggests:")
        lines.extend(suggestion_lines)
    # Trim trailing blank lines.
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _canonical_read_lines(receipt: ReceiptDocument) -> list[str]:
    lines: list[str] = []
    if receipt.extracted_supplier:
        lines.append(f"Supplier: {receipt.extracted_supplier}")
    if receipt.extracted_date is not None:
        lines.append(f"Date: {receipt.extracted_date.isoformat()}")
    amount_str = _format_amount(receipt.extracted_local_amount, receipt.extracted_currency)
    if amount_str:
        lines.append(f"Amount: {amount_str}")
    return lines


def _suggestion_lines(agent_read: AgentReceiptRead) -> list[str]:
    lines: list[str] = []
    if agent_read.suggested_business_or_personal:
        lines.append(f"Type: {agent_read.suggested_business_or_personal}")
    if agent_read.suggested_report_bucket:
        lines.append(f"Bucket: {agent_read.suggested_report_bucket}")
    attendees = _decode_attendees(agent_read.suggested_attendees_json)
    if attendees:
        lines.append("Attendees: " + " + ".join(attendees))
    if agent_read.suggested_customer:
        lines.append(f"Customer: {agent_read.suggested_customer}")
    if agent_read.suggested_business_reason:
        reason = agent_read.suggested_business_reason
        if len(reason) > _BUSINESS_REASON_DISPLAY_LIMIT:
            reason = reason[: _BUSINESS_REASON_DISPLAY_LIMIT - 1].rstrip() + "…"
        lines.append(f"Reason: {reason}")
    return lines


def _decode_attendees(raw_json: str | None) -> list[str]:
    if not raw_json:
        return []
    try:
        decoded = json.loads(raw_json)
    except json.JSONDecodeError:
        logger.warning("invalid suggested_attendees_json: %r", raw_json[:200])
        return []
    if not isinstance(decoded, list):
        return []
    return [item.strip() for item in decoded if isinstance(item, str) and item.strip()]


def _format_amount(amount: Decimal | None, currency: str | None) -> str | None:
    if amount is None:
        return None
    quantized = amount.quantize(Decimal("0.01")) if isinstance(amount, Decimal) else amount
    text = format(quantized, "f") if isinstance(quantized, Decimal) else str(quantized)
    if currency:
        return f"{text} {currency}"
    return text


def _build_reply_markup(user_response_id: int) -> dict[str, Any]:
    buttons: list[dict[str, str]] = []
    for label, action in (
        ("✅ Confirm", "confirm"),
        ("✏️ Edit", "edit"),
        ("❌ Cancel", "cancel"),
    ):
        callback_data = build_callback_data(action, user_response_id)
        buttons.append({"text": label, "callback_data": callback_data})
    return {"inline_keyboard": [buttons]}


def build_callback_data(action: str, user_response_id: int) -> str:
    """Construct the ``callback_data`` string for one button.

    Defensive: actions outside :data:`CALLBACK_ACTIONS` raise — that is a
    programmer error, not user input.
    """
    if action not in CALLBACK_ACTIONS:
        raise ValueError(f"unknown inline-keyboard action: {action!r}")
    data = f"{CALLBACK_DATA_PREFIX}:{action}:{user_response_id}"
    if len(data.encode("utf-8")) > _CALLBACK_DATA_MAX_BYTES:
        raise ValueError(
            f"callback_data exceeds Telegram's 64-byte limit: {len(data)} chars"
        )
    return data
