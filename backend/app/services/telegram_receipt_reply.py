"""Gated Telegram receipt reply helper.

F-AI-TG-4 is not a chatbot and not a free-form AI surface. This module builds
one short, safe, operator-gated receipt reply after the normal Telegram receipt
upload/OCR path has already persisted a ReceiptDocument. AI second-read output
is advisory only and can write only AgentDB rows.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping

from sqlmodel import Session

from app.models import ReceiptDocument
from app.services.merchant_buckets import suggest_bucket
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

_BUSINESS_CONTEXT_CATEGORIES = {
    "breakfast",
    "cafe",
    "customer_entertainment",
    "dinner",
    "entertainment",
    "lunch",
    "meal",
    "meals",
    "restaurant",
}
_NON_CONTEXT_CATEGORIES = {
    "auto",
    "fuel",
    "gas",
    "gasoline",
    "grocery",
    "market",
    "other",
    "parking",
    "petrol",
    "retail",
    "supermarket",
    "toll",
    "transport",
    "travel",
    "unknown",
}
_STRONG_BUSINESS_CONTEXT_TOKENS = (
    "bosnak",
    "boşnak",
    "doner",
    "döner",
    "kebap",
    "kebab",
    "borek",
    "börek",
    "restaurant",
    "restoran",
    "lokanta",
    "cafe",
    "kafe",
)
_NOT_PROVIDED = object()


@dataclass(frozen=True)
class TelegramReceiptAIReview:
    public_ai_review: dict[str, Any] | None
    agent_payload: Mapping[str, Any] | None = None


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


def should_include_receipt_business_context(
    receipt: ReceiptDocument,
    ai_review: TelegramReceiptAIReview | Mapping[str, Any] | None = None,
) -> bool:
    if _clean(receipt.business_or_personal).lower() != "business":
        return False
    ai_decision = _business_context_decision_from_ai_review(ai_review)
    if ai_decision is not None:
        return ai_decision
    return _is_meal_receipt(receipt)


def build_telegram_receipt_reply(
    receipt: ReceiptDocument,
    ai_review: TelegramReceiptAIReview | Mapping[str, Any] | None = None,
) -> str | None:
    if receipt is None:
        return None

    lines = ["Receipt received."]
    field_lines = _receipt_field_lines(receipt, ai_review=ai_review)
    if field_lines:
        lines.append("")
        lines.append("I read:")
        lines.extend(field_lines)

    if ai_review is not None:
        lines.append("")
        lines.append("AI second read is advisory only.")
        context_note = _ai_context_note(ai_review)
        if context_note:
            lines.append(context_note)

    return "\n".join(lines)


def maybe_send_telegram_receipt_reply(
    session: Session,
    client: Any,
    *,
    settings: Any,
    receipt: ReceiptDocument,
    telegram_user_id: int | None,
    chat_id: int,
    ai_review: TelegramReceiptAIReview | Mapping[str, Any] | None | object = _NOT_PROVIDED,
) -> bool:
    if not should_send_ai_receipt_reply(settings, telegram_user_id):
        return False

    ai_review_result = (
        maybe_create_telegram_receipt_ai_review(session, settings=settings, receipt=receipt)
        if ai_review is _NOT_PROVIDED
        else ai_review
    )

    text = build_telegram_receipt_reply(receipt, ai_review=ai_review_result)
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


def maybe_create_telegram_receipt_ai_review(
    session: Session,
    *,
    settings: Any,
    receipt: ReceiptDocument,
) -> TelegramReceiptAIReview | None:
    if not getattr(settings, "ai_telegram_live_model_enabled", False):
        return None
    existing = latest_ai_review_for_receipt(session, receipt)
    if existing and existing.get("status") in {"pass", "warn", "block"}:
        return TelegramReceiptAIReview(
            public_ai_review=existing,
            agent_payload=existing.get("agent_read") if isinstance(existing.get("agent_read"), Mapping) else None,
        )
    return _try_live_ai_second_read(session, receipt)


def _try_live_ai_second_read(session: Session, receipt: ReceiptDocument) -> TelegramReceiptAIReview | None:
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
        return TelegramReceiptAIReview(
            public_ai_review=latest_ai_review_for_receipt(session, receipt),
            agent_payload=live_result.agent_payload,
        )
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


def _public_ai_review(
    ai_review: TelegramReceiptAIReview | Mapping[str, Any] | None | object,
) -> dict[str, Any] | None:
    if isinstance(ai_review, TelegramReceiptAIReview):
        return ai_review.public_ai_review
    if isinstance(ai_review, Mapping):
        return dict(ai_review)
    return None


def _ai_context_note(ai_review: TelegramReceiptAIReview | Mapping[str, Any] | None) -> str | None:
    if ai_review is None:
        return None
    payload = _ai_payload(ai_review)
    if payload is None:
        return None
    context_text = _ai_context_text(payload)
    category = _clean(payload.get("business_context_category") or payload.get("receipt_category")).lower()

    if category in {"fuel", "gas", "gasoline", "petrol"} or _text_suggests_hard_non_context(context_text):
        return "AI context: This looks like a gas receipt."
    if category in {"market", "grocery", "supermarket"} or _text_suggests_market_snacks(context_text):
        return "AI context: This looks like market/snacks."
    if category in _BUSINESS_CONTEXT_CATEGORIES or _text_suggests_business_context(context_text):
        return "AI context: This looks like a food or meal receipt."
    return None


def _ai_payload(ai_review: TelegramReceiptAIReview | Mapping[str, Any]) -> Mapping[str, Any] | None:
    if isinstance(ai_review, TelegramReceiptAIReview):
        return ai_review.agent_payload
    if "agent_read" in ai_review and isinstance(ai_review["agent_read"], Mapping):
        return ai_review["agent_read"]
    return ai_review


def _receipt_field_lines(
    receipt: ReceiptDocument,
    *,
    ai_review: TelegramReceiptAIReview | Mapping[str, Any] | None = None,
) -> list[str]:
    payload = _ai_payload(ai_review) if ai_review is not None else None
    if isinstance(payload, Mapping):
        lines = _ai_receipt_field_lines(payload, receipt)
        if lines:
            return lines

    lines: list[str] = []
    if receipt.extracted_supplier:
        lines.append(f"Supplier: {receipt.extracted_supplier}")
    if receipt.extracted_date:
        lines.append(f"Date: {receipt.extracted_date.isoformat()}")
    amount = _format_amount(receipt.extracted_local_amount, receipt.extracted_currency)
    if amount:
        lines.append(f"Amount: {amount}")
    return lines


def _ai_receipt_field_lines(payload: Mapping[str, Any], receipt: ReceiptDocument) -> list[str]:
    lines: list[str] = []
    supplier = _clean(payload.get("merchant_name") or payload.get("supplier") or receipt.extracted_supplier)
    if supplier:
        lines.append(f"Supplier: {supplier}")

    date_value = payload.get("receipt_date") or payload.get("date") or receipt.extracted_date
    date_text = _clean(date_value.isoformat() if hasattr(date_value, "isoformat") else date_value)
    if date_text:
        lines.append(f"Date: {date_text}")

    amount_value = payload.get("total_amount") if payload.get("total_amount") is not None else payload.get("amount")
    if amount_value is None:
        amount_value = receipt.extracted_local_amount
    currency = _clean(payload.get("currency") or receipt.extracted_currency)
    amount = _format_amount(amount_value, currency)
    if amount:
        lines.append(f"Amount: {amount}")
    return lines


def _is_meal_receipt(receipt: ReceiptDocument) -> bool:
    bucket = _clean(receipt.report_bucket)
    if bucket in _MEAL_BUCKETS:
        return True

    suggested_bucket = suggest_bucket(receipt.extracted_supplier)
    if suggested_bucket in _MEAL_BUCKETS:
        return True

    supplier = _clean(receipt.extracted_supplier).lower()
    return _text_suggests_meal(supplier)


def _business_context_decision_from_ai_review(
    ai_review: TelegramReceiptAIReview | Mapping[str, Any] | None,
) -> bool | None:
    if ai_review is None:
        return None
    payload = _ai_payload(ai_review)
    if not isinstance(payload, Mapping):
        return None

    explicit = _optional_bool(payload.get("business_context_needed"))
    context_text = _ai_context_text(payload)
    category = _clean(payload.get("business_context_category") or payload.get("receipt_category")).lower()
    has_strong_context_evidence = _text_contains_any(context_text, _STRONG_BUSINESS_CONTEXT_TOKENS)
    if has_strong_context_evidence and not _text_suggests_hard_non_context(context_text):
        return True
    if category in _BUSINESS_CONTEXT_CATEGORIES:
        return True
    if category in _NON_CONTEXT_CATEGORIES:
        return False

    has_context_evidence = _text_suggests_business_context(context_text)
    has_non_context_evidence = _text_suggests_non_context(context_text)
    if explicit is False:
        return False
    if explicit is True:
        if has_non_context_evidence:
            return False
        return True if has_context_evidence else False
    if has_non_context_evidence:
        return False
    if has_context_evidence:
        return True
    return False


def _ai_context_text(payload: Mapping[str, Any]) -> str:
    text_parts: list[str] = []
    for key in (
        "business_context_category",
        "business_context_reason",
        "merchant_name",
        "supplier",
        "receipt_category",
        "raw_text_summary",
        "summary",
    ):
        value = payload.get(key)
        if value:
            text_parts.append(str(value))
    line_items = payload.get("line_items")
    if isinstance(line_items, list):
        for item in line_items:
            text_parts.append(str(item))
    return " ".join(text_parts).lower()


def _text_suggests_business_context(text: str) -> bool:
    return _text_contains_any(
        text,
        (
            "doner",
            "döner",
            "restaurant",
            "restoran",
            "lokanta",
            "cafe",
            "kafe",
            "cup",
            "kebap",
            "kebab",
            "borek",
            "börek",
            "bosnak",
            "boşnak",
            "meal",
            "lunch",
            "dinner",
            "breakfast",
            "customer entertainment",
            "entertainment",
        ),
    )


def _text_suggests_non_context(text: str) -> bool:
    return _text_contains_any(
        text,
        (
            "benzin",
            "diesel",
            "fuel",
            "gasoline",
            "petrol",
            "petrol ofisi",
            "market",
            "grocery",
            "supermarket",
            "parking",
            "toll",
            "otoyol",
        ),
    )


def _text_suggests_market_snacks(text: str) -> bool:
    return _text_contains_any(
        text,
        (
            "market",
            "grocery",
            "supermarket",
            "snack",
            "snacks",
            "energy drink",
            "beer",
            "cigarette",
            "cigarettes",
            "marlboro",
            "tuborg",
        ),
    )


def _text_suggests_hard_non_context(text: str) -> bool:
    return _text_contains_any(
        text,
        (
            "benzin",
            "diesel",
            "fuel",
            "gasoline",
            "petrol",
            "petrol ofisi",
            "parking",
            "toll",
            "otoyol",
            "transport",
        ),
    )


def _text_suggests_meal(text: str) -> bool:
    return _text_suggests_business_context(text)


def _text_contains_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def _format_amount(amount: Any, currency: str | None) -> str | None:
    if amount is None:
        return None
    try:
        amount_text = f"{Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"
    except (InvalidOperation, ValueError):
        amount_text = str(amount)
    currency_text = (currency or "").strip()
    return f"{amount_text} {currency_text}".strip()


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None
