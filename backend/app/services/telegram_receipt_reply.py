"""Gated Telegram receipt reply helper.

F-AI-TG-4 is not a chatbot and not a free-form AI surface. This module builds
one short, safe, operator-gated receipt reply after the normal Telegram receipt
upload/OCR path has already persisted a ReceiptDocument. AI second-read output
is advisory only and can write only AgentDB rows.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping

from sqlmodel import Session

from app.models import ReceiptDocument
from app.services import agent_receipt_live_provider
from app.services.agent_receipt_review_persistence import (
    build_canonical_receipt_snapshot,
    latest_ai_review_for_receipt,
    write_mock_agent_receipt_review,
)

logger = logging.getLogger(__name__)

_MEAL_BUCKETS = {
    "Meals/Snacks",
    "Breakfast",
    "Lunch",
    "Dinner",
    "Entertainment",
    "Customer Entertainment",
    "Meals & Entertainment",
}


def parse_telegram_allowlist(raw: str | None) -> set[int]:
    if not raw:
        return set()
    values: set[int] = set()
    for item in raw.replace(",", " ").split():
        item = item.strip()
        if not item:
            continue
        values.add(int(item))
    return values


def should_send_ai_receipt_reply(settings: Any, telegram_user_id: int | None) -> bool:
    if not getattr(settings, "ai_telegram_reply_enabled", False):
        return False
    if telegram_user_id is None:
        return False
    allowlist = set(getattr(settings, "ai_telegram_reply_allowlist", set()) or set())
    return telegram_user_id in allowlist


def build_telegram_receipt_reply(
    receipt: ReceiptDocument,
    ai_review: Mapping[str, Any] | None = None,
) -> str | None:
    if receipt is None:
        return None

    lines = ["Receipt received."]
    field_lines = _receipt_field_lines(receipt)
    if field_lines:
        lines.append("")
        lines.append("I read:")
        lines.extend(field_lines)

    if ai_review is not None:
        lines.append("")
        lines.append("AI second read is advisory only.")

    prompts = _clarification_prompt_lines(receipt)
    if prompts:
        lines.append("")
        lines.extend(prompts)

    return "\n".join(lines)


def maybe_send_telegram_receipt_reply(
    session: Session,
    client: Any,
    *,
    settings: Any,
    receipt: ReceiptDocument,
    telegram_user_id: int | None,
    chat_id: int,
) -> bool:
    if not should_send_ai_receipt_reply(settings, telegram_user_id):
        return False

    ai_review: dict[str, Any] | None = None
    if getattr(settings, "ai_telegram_live_model_enabled", False):
        ai_review = _try_live_ai_second_read(session, receipt)

    text = build_telegram_receipt_reply(receipt, ai_review=ai_review)
    if not text:
        return False

    try:
        client.send_message(chat_id, text)
    except Exception as exc:
        logger.warning(
            "Telegram AI receipt reply send failed for chat_id=%s receipt_id=%s: %s",
            chat_id,
            receipt.id,
            exc,
        )
        return False
    return True


def _try_live_ai_second_read(session: Session, receipt: ReceiptDocument) -> dict[str, Any] | None:
    try:
        agent_receipt_live_provider.ensure_live_provider_configured()
        canonical = build_canonical_receipt_snapshot(receipt)
        live_result = agent_receipt_live_provider.call_live_agent_receipt_review(
            receipt=receipt,
            canonical=canonical,
            statement_context=None,
        )
        outcome = write_mock_agent_receipt_review(
            session,
            receipt=receipt,
            agent_json_text=json.dumps(live_result.agent_payload, sort_keys=True),
            run_source="telegram_receipt_ai_reply",
            store_raw_model_json=False,
            store_prompt_text=False,
            prompt_text_override=live_result.prompt_text,
            model_provider="openai",
            model_name=live_result.model_name,
        )
        session.commit()
        if outcome.result is None:
            logger.warning(
                "Telegram AI receipt reply live second-read failed for receipt_id=%s: %s",
                receipt.id,
                outcome.error,
            )
            return None
        return latest_ai_review_for_receipt(session, receipt)
    except agent_receipt_live_provider.LiveAgentReceiptProviderError as exc:
        logger.warning(
            "Telegram AI receipt reply live provider unavailable for receipt_id=%s: %s",
            receipt.id,
            exc,
        )
        return None
    except Exception as exc:
        logger.warning(
            "Telegram AI receipt reply live second-read skipped for receipt_id=%s: %s",
            receipt.id,
            exc,
        )
        return None


def _receipt_field_lines(receipt: ReceiptDocument) -> list[str]:
    lines: list[str] = []
    if receipt.extracted_supplier:
        lines.append(f"Supplier: {receipt.extracted_supplier}")
    if receipt.extracted_date:
        lines.append(f"Date: {receipt.extracted_date.isoformat()}")
    amount = _format_amount(receipt.extracted_local_amount, receipt.extracted_currency)
    if amount:
        lines.append(f"Amount: {amount}")
    return lines


def _clarification_prompt_lines(receipt: ReceiptDocument) -> list[str]:
    prompts: list[str] = []
    if _is_business(receipt.business_or_personal) and not _clean(receipt.business_reason):
        prompts.append("Please reply with the business purpose for this receipt.")
    if _is_meal_or_restaurant(receipt) and not _has_attendees(receipt.attendees):
        prompts.append("Please reply with the attendees for this meal receipt.")
    return prompts


def _format_amount(amount: Any, currency: str | None) -> str | None:
    if amount is None:
        return None
    try:
        amount_text = f"{Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"
    except (InvalidOperation, ValueError):
        amount_text = str(amount)
    currency_text = (currency or "").strip()
    return f"{amount_text} {currency_text}".strip()


def _is_business(value: Any) -> bool:
    return _clean(value).lower() == "business"


def _is_meal_or_restaurant(receipt: ReceiptDocument) -> bool:
    bucket = _clean(receipt.report_bucket)
    if bucket in _MEAL_BUCKETS:
        return True
    supplier = _clean(receipt.extracted_supplier).lower()
    return any(token in supplier for token in ("restaurant", "cafe", "cup", "lokanta", "restoran"))


def _has_attendees(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, list):
        return any(_clean(item) for item in value)
    return bool(_clean(value))


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""
