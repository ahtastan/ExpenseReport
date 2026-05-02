"""Gated Telegram receipt reply helper.

F-AI-TG-4 is not a chatbot and not a free-form AI surface. This module builds
one short, safe, operator-gated receipt reply after the normal Telegram receipt
upload/OCR path has already persisted a ReceiptDocument. AI second-read output
is advisory only and can write only AgentDB rows.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping

from sqlmodel import Session, select

from app.models import (
    AgentReceiptRead,
    AgentReceiptReviewRun,
    AgentReceiptUserResponse,
    ReceiptDocument,
    utc_now,
)
from app.services.merchant_buckets import suggest_bucket
from app.services import agent_receipt_live_provider
from app.services.agent_receipt_context import build_context_window
from app.services.agent_receipt_reviewer import (
    build_inline_keyboard_review_prompt,
    parse_inline_keyboard_response,
)
from app.services.clarifications import (
    TELEGRAM_MARKET_CONTEXT_QUESTION_KEY,
    TELEGRAM_MEAL_CONTEXT_QUESTION_KEY,
    TELEGRAM_TELECOM_CONTEXT_QUESTION_KEY,
    TELEGRAM_PERSONAL_CARE_CONTEXT_QUESTION_KEY,
)
from app.services.agent_receipt_review_persistence import (
    build_canonical_receipt_snapshot,
    latest_agent_read_payload_for_receipt,
    latest_ai_review_for_receipt,
    write_mock_agent_receipt_review,
)
from app.services.telegram_keyboard_composer import build_inline_keyboard_reply

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
    "personal_care",
    "personal_care_drugstore",
    "petrol",
    "pharmacy",
    "retail",
    "supermarket",
    "telecom",
    "telecom_bill",
    "toll",
    "transport",
    "travel",
    "unknown",
    "phone_bill",
    "utility_payment",
}
_TELECOM_CATEGORIES = {
    "communication",
    "communications",
    "gsm",
    "internet",
    "phone",
    "phone_bill",
    "telecom",
    "telecom_bill",
    "telephone",
    "utility",
    "utility_payment",
}
_TELECOM_TOKENS = (
    "abonelik",
    "fatura tahsilatı",
    "fatura tahsilati",
    "gsm",
    "iletişim",
    "iletisim",
    "internet",
    "phone bill",
    "superonline",
    "telefon",
    "turk telekom",
    "turkcell",
    "turknet",
    "turk.net",
    "türk telekom",
    "türknet",
    "vodafone",
)
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
    return True


def should_send_telegram_receipt_followups(settings: Any, telegram_user_id: int | None) -> bool:
    if telegram_user_id is None:
        return False
    allowlist = set(getattr(settings, "ai_telegram_reply_allowlist", set()) or set())
    return telegram_user_id in allowlist


def should_include_receipt_business_context(
    receipt: ReceiptDocument,
    ai_review: TelegramReceiptAIReview | Mapping[str, Any] | None = None,
) -> bool:
    return bool(receipt_business_context_question_keys(receipt, ai_review=ai_review))


def receipt_business_context_question_keys(
    receipt: ReceiptDocument,
    ai_review: TelegramReceiptAIReview | Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    if _clean(receipt.business_or_personal).lower() == "personal":
        return ()
    context_kind = _receipt_context_kind(receipt, ai_review=ai_review)
    if context_kind == "meal":
        return (TELEGRAM_MEAL_CONTEXT_QUESTION_KEY,)
    if context_kind == "market":
        return (TELEGRAM_MARKET_CONTEXT_QUESTION_KEY,)
    if context_kind == "telecom":
        return (TELEGRAM_TELECOM_CONTEXT_QUESTION_KEY,)
    if context_kind == "personal_care_drugstore":
        return (TELEGRAM_PERSONAL_CARE_CONTEXT_QUESTION_KEY,)
    return ()


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

    context_note = _receipt_context_note(receipt, ai_review=ai_review)

    if ai_review is not None:
        lines.append("")
        lines.append("AI second read is advisory only.")
    if context_note:
        if ai_review is None:
            lines.append("")
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
            agent_payload=latest_agent_read_payload_for_receipt(session, receipt)
            or (existing.get("agent_read") if isinstance(existing.get("agent_read"), Mapping) else None),
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
    if category in _TELECOM_CATEGORIES or _text_suggests_telecom_bill(context_text):
        return "AI context: This looks like a phone/telecom bill payment."
    if _category_is_personal_care_drugstore(category) or _text_suggests_personal_care_drugstore(context_text):
        return "AI context: This looks like personal care / drugstore items."
    if category in {"market", "grocery", "supermarket"} or _text_suggests_market_snacks(context_text):
        return "AI context: This looks like market/snacks."
    if category in _BUSINESS_CONTEXT_CATEGORIES or _text_suggests_business_context(context_text):
        return "AI context: This looks like a food or meal receipt."
    return None


def _receipt_context_note(
    receipt: ReceiptDocument,
    ai_review: TelegramReceiptAIReview | Mapping[str, Any] | None = None,
) -> str | None:
    context_kind = _receipt_context_kind(receipt, ai_review=ai_review)
    prefix = "AI context" if ai_review is not None else "Context"
    if context_kind == "fuel":
        return f"{prefix}: This looks like a gas receipt."
    if context_kind == "telecom":
        return f"{prefix}: This looks like a phone/telecom bill payment."
    if context_kind == "personal_care_drugstore":
        return f"{prefix}: This looks like personal care / drugstore items."
    if context_kind == "market":
        return f"{prefix}: This looks like market/snacks."
    if context_kind == "meal":
        return f"{prefix}: This looks like a food or meal receipt."
    return None


def _receipt_context_kind(
    receipt: ReceiptDocument,
    ai_review: TelegramReceiptAIReview | Mapping[str, Any] | None = None,
) -> str | None:
    receipt_text = " ".join(
        part
        for part in (
            _clean(receipt.extracted_supplier),
            _clean(receipt.report_bucket),
            _clean(receipt.receipt_type),
        )
        if part
    ).lower()
    if _text_suggests_hard_non_context(receipt_text):
        return "fuel"
    if _text_suggests_telecom_bill(receipt_text):
        return "telecom"
    if _text_suggests_personal_care_drugstore(receipt_text):
        return "personal_care_drugstore"

    payload = _ai_payload(ai_review) if ai_review is not None else None
    if isinstance(payload, Mapping):
        context_text = _ai_context_text(payload)
        category = _clean(payload.get("business_context_category") or payload.get("receipt_category")).lower()
        if category in {"fuel", "gas", "gasoline", "petrol"} or _text_suggests_hard_non_context(context_text):
            return "fuel"
        if category in _TELECOM_CATEGORIES or _text_suggests_telecom_bill(context_text):
            return "telecom"
        if _category_is_personal_care_drugstore(category) or _text_suggests_personal_care_drugstore(context_text):
            return "personal_care_drugstore"
        if category in {"market", "grocery", "supermarket"} or _text_suggests_market_snacks(context_text):
            return "market"
        if category in _BUSINESS_CONTEXT_CATEGORIES or _text_suggests_business_context(context_text):
            return "meal"

    if _text_suggests_market_snacks(receipt_text):
        return "market"
    if _is_meal_receipt(receipt):
        return "meal"
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


def _text_suggests_telecom_bill(text: str) -> bool:
    return _text_contains_any(text, _TELECOM_TOKENS)


def _category_is_personal_care_drugstore(category: str) -> bool:
    return category in {
        "drugstore",
        "health_beauty",
        "hygiene",
        "personal_care",
        "personal_care_drugstore",
        "pharmacy",
    }


def _text_suggests_personal_care_drugstore(text: str) -> bool:
    return _text_contains_any(
        text,
        (
            "rossmann",
            "gratis",
            "watsons",
            "pharmacy",
            "drugstore",
            "eczane",
            "facial tissue",
            "tissue",
            "toothpaste",
            "shampoo",
            "hygiene",
            "cosmetic",
            "cosmetics",
            "hair brush",
            "brush",
            "cleaning",
            "personal care",
            "deodorant",
            "soap",
            "toothbrush",
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
    folded_text = _fold_text(text)
    return any(token in text or _fold_text(token) in folded_text for token in tokens)


def _fold_text(value: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(char)
    )


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


# ─── F-AI-Stage1 sub-PR 3: inline-keyboard dispatch ─────────────────────────


def should_use_inline_keyboard(settings: Any, telegram_user_id: int | None) -> bool:
    """Return True iff the new keyboard flow should replace the legacy reply
    for this user/event.

    Gate: ``ai_telegram_inline_keyboard_enabled`` flag is True AND the user
    is in ``ai_telegram_reply_allowlist`` AND
    ``ai_telegram_live_model_enabled`` is True (the keyboard requires a
    live AI proposal to populate its body).
    """
    if telegram_user_id is None:
        return False
    if not getattr(settings, "ai_telegram_inline_keyboard_enabled", False):
        return False
    if not getattr(settings, "ai_telegram_live_model_enabled", False):
        return False
    allowlist = set(getattr(settings, "ai_telegram_reply_allowlist", set()) or set())
    return telegram_user_id in allowlist


def send_inline_keyboard_proposal(
    session: Session,
    client: Any,
    *,
    settings: Any,
    receipt: ReceiptDocument,
    user_id: int,
    telegram_user_id: int | None,
    chat_id: int,
) -> bool:
    """Run the inline-keyboard flow for a single receipt upload.

    Returns ``True`` when the keyboard message landed on Telegram and a
    pending ``AgentReceiptUserResponse`` row was persisted. Returns
    ``False`` on any failure — caller falls back to the legacy text reply
    so the user always gets some response.
    """
    canonical = build_canonical_receipt_snapshot(receipt)
    try:
        context_window = build_context_window(session, user_id=user_id)
    except Exception as exc:
        logger.warning(
            "inline keyboard: context build failed receipt_id=%s user_id=%s: %s",
            receipt.id,
            user_id,
            exc,
        )
        return False

    try:
        agent_receipt_live_provider.ensure_live_provider_configured()
        prompt_text = build_inline_keyboard_review_prompt(canonical, context_window)
        raw_response = agent_receipt_live_provider.call_live_model_with_image(
            receipt=receipt,
            prompt_text=prompt_text,
        )
    except agent_receipt_live_provider.LiveAgentReceiptProviderError as exc:
        logger.warning(
            "inline keyboard: live provider unavailable for receipt_id=%s: %s",
            receipt.id,
            exc,
        )
        return False
    except Exception as exc:
        logger.warning(
            "inline keyboard: live model call failed for receipt_id=%s: %s",
            receipt.id,
            exc,
        )
        return False

    suggestion = parse_inline_keyboard_response(raw_response)
    if suggestion is None:
        logger.warning(
            "inline keyboard: model response unparseable for receipt_id=%s", receipt.id
        )
        return False

    try:
        outcome = write_mock_agent_receipt_review(
            session,
            receipt=receipt,
            agent_json_text=raw_response,
            run_kind="receipt_inline_keyboard",
            run_source="telegram_receipt_inline_keyboard",
            store_raw_model_json=False,
            store_prompt_text=False,
            prompt_text_override=prompt_text,
            model_provider="openai",
            model_name=agent_receipt_live_provider.live_agent_receipt_model_name(),
            suggested_business_or_personal=suggestion.business_or_personal,
            suggested_report_bucket=suggestion.report_bucket,
            suggested_attendees=suggestion.attendees,
            suggested_customer=suggestion.customer,
            suggested_business_reason=suggestion.business_reason,
            suggested_confidence_overall=suggestion.confidence_overall,
            context_window=context_window,
        )
    except Exception as exc:
        logger.warning(
            "inline keyboard: persistence failed for receipt_id=%s: %s",
            receipt.id,
            exc,
        )
        session.rollback()
        return False

    if outcome.run.status != "completed":
        logger.warning(
            "inline keyboard: run did not complete for receipt_id=%s status=%s",
            receipt.id,
            outcome.run.status,
        )
        session.commit()
        return False

    agent_read = session.exec(
        select(AgentReceiptRead).where(AgentReceiptRead.run_id == outcome.run.id)
    ).first()
    if agent_read is None:
        logger.warning(
            "inline keyboard: agent_read row missing for run_id=%s", outcome.run.id
        )
        session.commit()
        return False

    user_response = AgentReceiptUserResponse(
        receipt_document_id=receipt.id or 0,
        agent_receipt_review_run_id=outcome.run.id or 0,
        agent_receipt_read_id=agent_read.id or 0,
        telegram_user_id=telegram_user_id,
        keyboard_message_id=None,
        user_action="pending",
    )
    session.add(user_response)
    session.commit()
    session.refresh(user_response)

    payload = build_inline_keyboard_reply(receipt, agent_read, user_response.id or 0)
    try:
        api_response = client.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": payload["text"],
                "reply_markup": json.dumps(payload["reply_markup"]),
            },
        )
    except Exception as exc:
        logger.warning(
            "inline keyboard: sendMessage failed for receipt_id=%s: %s",
            receipt.id,
            exc,
        )
        return False

    message_id = (api_response or {}).get("result", {}).get("message_id")
    if isinstance(message_id, int):
        user_response.keyboard_message_id = message_id
        session.add(user_response)
        session.commit()

    return True
