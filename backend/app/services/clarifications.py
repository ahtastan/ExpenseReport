import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import or_
from sqlmodel import Session, select

from app.config import get_settings
from app.models import (
    AgentReceiptUserResponse,
    AppUser,
    ClarificationQuestion,
    ReceiptDocument,
)

# F-AI-Stage1 sub-PR 3: this module continues to handle the legacy
# clarification-question follow-up flow ("Was this business or personal?
# Reply with..."). When the inline-keyboard flag is on for a user, the
# Telegram receipt-upload path short-circuits this module entirely
# (telegram.handle_update sends the keyboard instead and returns early
# before ensure_receipt_review_questions runs). The constants and
# question texts below are still used as the FALLBACK path: flag-off
# users, non-allowlisted users, and any keyboard-flow failure that
# falls back to legacy. They are also still used when the user taps
# Edit on the keyboard — the bot then asks for a free-text correction
# and routes the reply through ``answer_question`` below, which adds
# ``*_source='telegram_user'`` source tags for the inline-keyboard
# audit trail.
_AMOUNT_QUANT = Decimal("0.0001")
logger = logging.getLogger(__name__)
TELEGRAM_MEAL_CONTEXT_QUESTION_KEY = "telegram_meal_context"
TELEGRAM_MARKET_CONTEXT_QUESTION_KEY = "telegram_market_context"
TELEGRAM_TELECOM_CONTEXT_QUESTION_KEY = "telegram_telecom_context"
TELEGRAM_PERSONAL_CARE_CONTEXT_QUESTION_KEY = "telegram_personal_care_context"
TELEGRAM_MEAL_CONTEXT_RETRY_QUESTION_KEY = f"{TELEGRAM_MEAL_CONTEXT_QUESTION_KEY}_retry"
TELEGRAM_MARKET_CONTEXT_RETRY_QUESTION_KEY = f"{TELEGRAM_MARKET_CONTEXT_QUESTION_KEY}_retry"
TELEGRAM_TELECOM_CONTEXT_RETRY_QUESTION_KEY = f"{TELEGRAM_TELECOM_CONTEXT_QUESTION_KEY}_retry"
TELEGRAM_PERSONAL_CARE_CONTEXT_RETRY_QUESTION_KEY = f"{TELEGRAM_PERSONAL_CARE_CONTEXT_QUESTION_KEY}_retry"
DEFAULT_BUSINESS_CONTEXT_QUESTION_KEYS = ("business_reason", "attendees")
TELEGRAM_CONTEXT_QUESTION_KEYS = (
    TELEGRAM_MEAL_CONTEXT_QUESTION_KEY,
    TELEGRAM_MARKET_CONTEXT_QUESTION_KEY,
    TELEGRAM_TELECOM_CONTEXT_QUESTION_KEY,
    TELEGRAM_PERSONAL_CARE_CONTEXT_QUESTION_KEY,
    TELEGRAM_MEAL_CONTEXT_RETRY_QUESTION_KEY,
    TELEGRAM_MARKET_CONTEXT_RETRY_QUESTION_KEY,
    TELEGRAM_TELECOM_CONTEXT_RETRY_QUESTION_KEY,
    TELEGRAM_PERSONAL_CARE_CONTEXT_RETRY_QUESTION_KEY,
)
BUSINESS_CONTEXT_QUESTION_KEYS = (
    *DEFAULT_BUSINESS_CONTEXT_QUESTION_KEYS,
    *TELEGRAM_CONTEXT_QUESTION_KEYS,
)
MEAL_ATTENDEES_QUESTION_TEXT = (
    "Please reply with who was included in the meal. "
    "Example: Hakan only, or Hakan + customer: Ahmet Yilmaz."
)
TELEGRAM_MEAL_CONTEXT_QUESTION_TEXT = (
    "Was this business or personal spending?\n"
    "If business, please reply with who was included.\n"
    "Example: business, Hakan only or business, Hakan + customer: Ahmet Yılmaz.\n"
    "If personal, reply personal."
)
TELEGRAM_MARKET_CONTEXT_QUESTION_TEXT = (
    "Was this business or personal spending?\n"
    "If business, please reply with who it was for.\n"
    "Example: business, EDT team or business, customer meeting with Ahmet + Hakan.\n"
    "If personal, reply personal."
)
TELEGRAM_TELECOM_CONTEXT_QUESTION_TEXT = (
    "Was this a business phone/internet expense or personal spending?\n"
    "If business, reply with the business reason.\n"
    "If personal, reply personal."
)
TELEGRAM_PERSONAL_CARE_CONTEXT_QUESTION_TEXT = (
    "If this was business spending, reply with the reason. If personal, reply personal."
)


def _question_exists(session: Session, receipt_id: int | None, key: str) -> bool:
    return bool(
        session.exec(
            select(ClarificationQuestion).where(
                ClarificationQuestion.receipt_document_id == receipt_id,
                ClarificationQuestion.question_key == key,
            )
        ).first()
    )


def _business_personal_allowlisted(session: Session, user_id: int | None) -> tuple[bool, int | None]:
    if user_id is None:
        return False, None
    user = session.get(AppUser, user_id)
    telegram_user_id = user.telegram_user_id if user else None
    if telegram_user_id is None:
        return False, None
    return (
        telegram_user_id in get_settings().business_personal_clarification_telegram_ids,
        telegram_user_id,
    )


def _should_default_business_for_telegram_receipt(
    session: Session,
    receipt: ReceiptDocument,
    user_id: int | None,
) -> tuple[bool, bool, int | None]:
    allowlisted, telegram_user_id = _business_personal_allowlisted(session, user_id)
    is_telegram_receipt = (
        receipt.source == "telegram"
        and receipt.uploader_user_id is not None
        and telegram_user_id is not None
    )
    return is_telegram_receipt and not allowlisted, allowlisted, telegram_user_id


def _parse_date(value: str) -> date | None:
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(value: str) -> Decimal | None:
    text = value.replace("TRY", "").replace("TL", "").replace("USD", "").replace("EUR", "").strip()
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") else text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text).quantize(_AMOUNT_QUANT)
    except (InvalidOperation, ValueError):
        return None


def _looks_like_meta_question(value: str) -> bool:
    text = value.strip().lower()
    if not text:
        return False
    starters = ("why", "how", "what", "where", "when", "can you", "can't you", "could you")
    return text.endswith("?") or text.startswith(starters)


def _looks_like_non_answer(value: str) -> bool:
    text = value.strip().lower()
    if _looks_like_meta_question(text):
        return True
    normalized = text.strip(" .,!?:;-")
    return normalized in {"hello", "hi", "hey", "yo", "selam", "merhaba"}


def _keep_open_with_helper(
    session: Session,
    question: ClarificationQuestion,
    helper_key: str,
    helper_text: str,
) -> list[ClarificationQuestion]:
    existing = session.exec(
        select(ClarificationQuestion).where(
            ClarificationQuestion.receipt_document_id == question.receipt_document_id,
            ClarificationQuestion.user_id == question.user_id,
            ClarificationQuestion.question_key == helper_key,
            ClarificationQuestion.status == "open",
        )
    ).first()
    if existing:
        return [existing]

    receipt = session.get(ReceiptDocument, question.receipt_document_id) if question.receipt_document_id else None
    helper = ClarificationQuestion(
        receipt_document_id=question.receipt_document_id,
        user_id=question.user_id,
        question_key=helper_key,
        question_text=helper_text,
    )
    session.add(helper)
    if receipt:
        receipt.needs_clarification = True
        receipt.updated_at = datetime.now(timezone.utc)
        session.add(receipt)
    session.commit()
    session.refresh(helper)
    return [helper]


def _telegram_context_question_text(base_question_key: str) -> str:
    if base_question_key == TELEGRAM_MEAL_CONTEXT_QUESTION_KEY:
        return TELEGRAM_MEAL_CONTEXT_QUESTION_TEXT
    if base_question_key == TELEGRAM_TELECOM_CONTEXT_QUESTION_KEY:
        return TELEGRAM_TELECOM_CONTEXT_QUESTION_TEXT
    if base_question_key == TELEGRAM_PERSONAL_CARE_CONTEXT_QUESTION_KEY:
        return TELEGRAM_PERSONAL_CARE_CONTEXT_QUESTION_TEXT
    return TELEGRAM_MARKET_CONTEXT_QUESTION_TEXT


def ensure_initial_receipt_question(
    session: Session,
    receipt: ReceiptDocument,
    user_id: int | None,
) -> list[ClarificationQuestion]:
    existing = session.exec(
        select(ClarificationQuestion).where(
            ClarificationQuestion.receipt_document_id == receipt.id,
            ClarificationQuestion.question_key == "business_or_personal",
        )
    ).first()
    if existing:
        return []

    question = ClarificationQuestion(
        receipt_document_id=receipt.id,
        user_id=user_id,
        question_key="business_or_personal",
        question_text=(
            "I received this receipt. Is it Business or Personal? "
            "Reply with Business, Personal, or add context like 'Business - Kartonsan dinner'."
        ),
    )
    session.add(question)
    session.commit()
    session.refresh(question)
    return [question]


def ensure_receipt_review_questions(
    session: Session,
    receipt: ReceiptDocument,
    user_id: int | None,
    *,
    include_business_context: bool = True,
    business_context_question_keys: tuple[str, ...] | None = None,
    include_business_personal: bool = True,
) -> list[ClarificationQuestion]:
    questions: list[ClarificationQuestion] = []
    default_business, allowlisted, telegram_user_id = _should_default_business_for_telegram_receipt(
        session,
        receipt,
        user_id,
    )
    if receipt.business_or_personal is None and default_business:
        receipt.business_or_personal = "Business"
        logger.info(
            "Defaulted receipt business_or_personal to Business receipt_id=%s "
            "uploader_user_id=%s telegram_user_id=%s reason=default_business_policy "
            "allowlisted=%s",
            receipt.id,
            receipt.uploader_user_id,
            telegram_user_id,
            allowlisted,
        )

    specs: list[tuple[bool, str, str]] = [
        (
            receipt.extracted_date is None,
            "receipt_date",
            "I could not read the receipt date. What date is on it? Use YYYY-MM-DD if you can.",
        ),
        (
            receipt.extracted_local_amount is None,
            "local_amount",
            "I could not read the receipt amount. What is the total amount and currency?",
        ),
        (
            receipt.extracted_supplier is None,
            "supplier",
            "I could not read the merchant name. Which store, restaurant, or vendor is this?",
        ),
        (
            include_business_personal and receipt.business_or_personal is None,
            "business_or_personal",
            "Is this Business or Personal? Reply with Business, Personal, or add context like 'Business - Kartonsan dinner'.",
        ),
    ]
    context_keys = (
        DEFAULT_BUSINESS_CONTEXT_QUESTION_KEYS
        if business_context_question_keys is None
        else tuple(key for key in business_context_question_keys if key in BUSINESS_CONTEXT_QUESTION_KEYS)
    )
    if include_business_context and context_keys:
        attendees_question_text = (
            MEAL_ATTENDEES_QUESTION_TEXT
            if "attendees" in context_keys and "business_reason" not in context_keys
            else "Who attended or benefited from this expense? If not applicable, reply 'N/A'."
        )
        specs.extend(
            [
                (
                    "business_reason" in context_keys
                    and receipt.business_or_personal == "Business"
                    and not receipt.business_reason,
                    "business_reason",
                    "What project, customer, or trip should this receipt be attached to?",
                ),
                (
                    "attendees" in context_keys
                    and receipt.business_or_personal == "Business"
                    and not receipt.attendees,
                    "attendees",
                    attendees_question_text,
                ),
                (
                    TELEGRAM_MEAL_CONTEXT_QUESTION_KEY in context_keys
                    and receipt.business_or_personal != "Personal"
                    and not receipt.attendees,
                    TELEGRAM_MEAL_CONTEXT_QUESTION_KEY,
                    TELEGRAM_MEAL_CONTEXT_QUESTION_TEXT,
                ),
                (
                    TELEGRAM_MARKET_CONTEXT_QUESTION_KEY in context_keys
                    and receipt.business_or_personal != "Personal"
                    and not receipt.business_reason,
                    TELEGRAM_MARKET_CONTEXT_QUESTION_KEY,
                    TELEGRAM_MARKET_CONTEXT_QUESTION_TEXT,
                ),
                (
                    TELEGRAM_TELECOM_CONTEXT_QUESTION_KEY in context_keys
                    and receipt.business_or_personal != "Personal"
                    and not receipt.business_reason,
                    TELEGRAM_TELECOM_CONTEXT_QUESTION_KEY,
                    TELEGRAM_TELECOM_CONTEXT_QUESTION_TEXT,
                ),
                (
                    TELEGRAM_PERSONAL_CARE_CONTEXT_QUESTION_KEY in context_keys
                    and receipt.business_or_personal != "Personal"
                    and not receipt.business_reason,
                    TELEGRAM_PERSONAL_CARE_CONTEXT_QUESTION_KEY,
                    TELEGRAM_PERSONAL_CARE_CONTEXT_QUESTION_TEXT,
                ),
            ]
        )
    for should_ask, key, text in specs:
        if should_ask and not _question_exists(session, receipt.id, key):
            questions.append(
                ClarificationQuestion(
                    receipt_document_id=receipt.id,
                    user_id=user_id,
                    question_key=key,
                    question_text=text,
                )
            )
    for question in questions:
        session.add(question)
    receipt.needs_clarification = bool(questions) or _receipt_has_open_questions(session, receipt.id)
    receipt.updated_at = datetime.now(timezone.utc)
    session.add(receipt)
    session.commit()
    for question in questions:
        session.refresh(question)
    return questions


def _receipt_has_open_questions(session: Session, receipt_id: int | None) -> bool:
    if receipt_id is None:
        return False
    return bool(
        session.exec(
            select(ClarificationQuestion).where(
                ClarificationQuestion.receipt_document_id == receipt_id,
                ClarificationQuestion.status == "open",
            )
        ).first()
    )


def _open_question_filters(
    user_id: int,
    *,
    include_business_context: bool,
    business_context_question_keys: tuple[str, ...] | None = None,
) -> list:
    filters = [
        ClarificationQuestion.user_id == user_id,
        ClarificationQuestion.status == "open",
    ]
    if not include_business_context:
        filters.append(~ClarificationQuestion.question_key.in_(BUSINESS_CONTEXT_QUESTION_KEYS))
    elif business_context_question_keys is not None:
        context_keys = tuple(key for key in business_context_question_keys if key in BUSINESS_CONTEXT_QUESTION_KEYS)
        if not context_keys:
            filters.append(~ClarificationQuestion.question_key.in_(BUSINESS_CONTEXT_QUESTION_KEYS))
        elif set(context_keys) != set(BUSINESS_CONTEXT_QUESTION_KEYS):
            filters.append(
                or_(
                    ~ClarificationQuestion.question_key.in_(BUSINESS_CONTEXT_QUESTION_KEYS),
                    ClarificationQuestion.question_key.in_(context_keys),
                )
            )
    return filters


def next_open_question_for_user(
    session: Session,
    user_id: int,
    *,
    include_business_context: bool = True,
    business_context_question_keys: tuple[str, ...] | None = None,
) -> ClarificationQuestion | None:
    """Return the next clarification question to dispatch for ``user_id``.

    Scoped to the receipt with the *most recently created* open question
    — i.e., the receipt the bot is currently interacting with. Within
    that receipt, returns the oldest open question (per-receipt FIFO).

    The pre-F1.5 implementation ordered globally across every open
    question the user had, oldest first. That de-coupled display from
    dispatch: ``telegram.handle_update`` shows the just-uploaded
    receipt's ``questions[0].question_text`` (newest receipt), but the
    answer was routed by ``next_open_question_for_user``, which would
    return the oldest open question across *all* receipts. When stale
    open questions existed on older receipts, an "amount?" prompt for
    receipt N got answered by writing the user's reply into receipt
    M's ``supplier`` slot — the dispatch correctly hit the supplier
    branch with an amount-shaped value, corrupting the older receipt's
    merchant column and leaving the newer receipt's amount NULL
    (incident F1.5, 2026-04-27).

    Receipt-scoping realigns display and dispatch: the receipt the bot
    just spoke about (most recent open question) is the one whose
    oldest open question gets the next answer. Once that receipt's
    queue drains, the *next* most recent open question's receipt
    becomes active automatically.

    Receiptless questions (rare; ``receipt_document_id IS NULL``) are
    returned as-is — there's no receipt to scope to.
    """
    most_recent = session.exec(
        select(ClarificationQuestion)
        .where(
            *_open_question_filters(
                user_id,
                include_business_context=include_business_context,
                business_context_question_keys=business_context_question_keys,
            )
        )
        .order_by(
            ClarificationQuestion.created_at.desc(),
            ClarificationQuestion.id.desc(),
        )
    ).first()
    if most_recent is None:
        return None
    receipt_id = most_recent.receipt_document_id
    if receipt_id is None:
        return most_recent
    return next_open_question_for_receipt(
        session,
        user_id,
        receipt_id,
        include_business_context=include_business_context,
        business_context_question_keys=business_context_question_keys,
    )


def next_open_question_for_receipt(
    session: Session,
    user_id: int,
    receipt_id: int | None,
    *,
    include_business_context: bool = True,
    business_context_question_keys: tuple[str, ...] | None = None,
) -> ClarificationQuestion | None:
    if receipt_id is None:
        return None
    return session.exec(
        select(ClarificationQuestion)
        .where(
            *_open_question_filters(
                user_id,
                include_business_context=include_business_context,
                business_context_question_keys=business_context_question_keys,
            ),
            ClarificationQuestion.receipt_document_id == receipt_id,
        )
        .order_by(
            ClarificationQuestion.created_at,
            ClarificationQuestion.id,
        )
    ).first()


def open_telegram_context_question_keys_for_receipt(
    session: Session,
    user_id: int,
    receipt_id: int | None,
) -> tuple[str, ...]:
    if receipt_id is None:
        return ()
    keys = session.exec(
        select(ClarificationQuestion.question_key)
        .where(
            ClarificationQuestion.user_id == user_id,
            ClarificationQuestion.receipt_document_id == receipt_id,
            ClarificationQuestion.status == "open",
            ClarificationQuestion.question_key.in_(TELEGRAM_CONTEXT_QUESTION_KEYS),
        )
        .order_by(
            ClarificationQuestion.created_at,
            ClarificationQuestion.id,
        )
    ).all()
    return tuple(dict.fromkeys(str(key) for key in keys))


def next_open_telegram_context_question_for_user(
    session: Session,
    user_id: int,
) -> ClarificationQuestion | None:
    return session.exec(
        select(ClarificationQuestion)
        .where(
            ClarificationQuestion.user_id == user_id,
            ClarificationQuestion.status == "open",
            ClarificationQuestion.question_key.in_(TELEGRAM_CONTEXT_QUESTION_KEYS),
        )
        .order_by(
            ClarificationQuestion.created_at.desc(),
            ClarificationQuestion.id.desc(),
        )
    ).first()


def looks_like_telegram_context_answer(answer_text: str) -> bool:
    text = answer_text.strip().lower()
    return "business" in text or "personal" in text


def _active_edited_user_response(
    session: Session, receipt_id: int | None
) -> AgentReceiptUserResponse | None:
    """F-AI-Stage1 sub-PR 3: return the most recent ``edited`` keyboard
    response for this receipt, if any. Used to attach source tags +
    audit trail when the user replies with a correction after tapping
    Edit on the inline keyboard.
    """
    if receipt_id is None:
        return None
    return session.exec(
        select(AgentReceiptUserResponse)
        .where(
            AgentReceiptUserResponse.receipt_document_id == receipt_id,
            AgentReceiptUserResponse.user_action == "edited",
        )
        .order_by(AgentReceiptUserResponse.id.desc())
    ).first()


def answer_question(session: Session, question: ClarificationQuestion, answer: str) -> list[ClarificationQuestion]:
    answer_text = answer.strip()
    if question.question_key in {"receipt_date", "receipt_date_retry"} and _looks_like_non_answer(answer_text):
        return _keep_open_with_helper(
            session,
            question,
            "receipt_date_help",
            (
                "I tried to read the printed receipt date, but OCR was not confident enough. "
                "Please send the date like 2025-09-04."
            ),
        )
    if question.question_key in {"local_amount", "local_amount_retry"} and _looks_like_non_answer(answer_text):
        return _keep_open_with_helper(
            session,
            question,
            "local_amount_help",
            "I need the total amount from the receipt. Please send it like 419.58 TRY.",
        )
    if question.question_key in {"business_or_personal", "business_or_personal_retry"} and _looks_like_non_answer(answer_text):
        return _keep_open_with_helper(
            session,
            question,
            "business_or_personal_help",
            "I need to classify this receipt. Please reply Business or Personal.",
        )

    question.answer_text = answer_text
    question.status = "answered"
    question.answered_at = datetime.now(timezone.utc)
    session.add(question)

    new_questions: list[ClarificationQuestion] = []
    receipt = None
    if question.receipt_document_id:
        receipt = session.get(ReceiptDocument, question.receipt_document_id)

    # F-AI-Stage1 sub-PR 3: snapshot canonical values BEFORE the parser
    # runs so we can detect which fields the answer changed and tag them
    # with ``*_source='telegram_user'`` if an ``edited`` keyboard
    # response is in flight for this receipt.
    edited_response = _active_edited_user_response(
        session, question.receipt_document_id
    )
    pre_answer_values: dict[str, Any] = {}
    if edited_response is not None and receipt is not None:
        pre_answer_values = {
            "business_or_personal": receipt.business_or_personal,
            "report_bucket": receipt.report_bucket,
            "business_reason": receipt.business_reason,
            "attendees": receipt.attendees,
        }

    if receipt and question.question_key == "business_or_personal":
        lowered = answer.lower()
        if "personal" in lowered:
            receipt.business_or_personal = "Personal"
            receipt.needs_clarification = False
        elif "business" in lowered:
            receipt.business_or_personal = "Business"
            receipt.needs_clarification = True
            if not _question_exists(session, receipt.id, "business_reason"):
                new_questions.append(
                    ClarificationQuestion(
                        receipt_document_id=receipt.id,
                        user_id=question.user_id,
                        question_key="business_reason",
                        question_text="What project, customer, or trip should this receipt be attached to?",
                    )
                )
        else:
            new_questions.append(
                ClarificationQuestion(
                    receipt_document_id=receipt.id,
                    user_id=question.user_id,
                    question_key="business_or_personal_retry",
                    question_text="I could not tell if that means Business or Personal. Which one should I use?",
                )
            )
        receipt.updated_at = datetime.now(timezone.utc)
        session.add(receipt)

    elif receipt and question.question_key in {
        TELEGRAM_MEAL_CONTEXT_QUESTION_KEY,
        TELEGRAM_MARKET_CONTEXT_QUESTION_KEY,
        TELEGRAM_TELECOM_CONTEXT_QUESTION_KEY,
        TELEGRAM_PERSONAL_CARE_CONTEXT_QUESTION_KEY,
        TELEGRAM_MEAL_CONTEXT_RETRY_QUESTION_KEY,
        TELEGRAM_MARKET_CONTEXT_RETRY_QUESTION_KEY,
        TELEGRAM_TELECOM_CONTEXT_RETRY_QUESTION_KEY,
        TELEGRAM_PERSONAL_CARE_CONTEXT_RETRY_QUESTION_KEY,
    }:
        base_question_key = question.question_key.removesuffix("_retry")
        lowered = answer.lower()
        if "personal" in lowered and "business" not in lowered:
            receipt.business_or_personal = "Personal"
            receipt.needs_clarification = False
        elif "business" in lowered:
            receipt.business_or_personal = "Business"
            context = _strip_business_context_prefix(answer_text)
            if context:
                if base_question_key == TELEGRAM_MEAL_CONTEXT_QUESTION_KEY:
                    receipt.attendees = context
                else:
                    receipt.business_reason = context
                receipt.needs_clarification = False
            else:
                receipt.needs_clarification = True
                new_questions.append(
                    ClarificationQuestion(
                        receipt_document_id=receipt.id,
                        user_id=question.user_id,
                        question_key=f"{base_question_key}_retry",
                        question_text=_telegram_context_question_text(base_question_key),
                    )
                )
        else:
            receipt.needs_clarification = True
            new_questions.append(
                ClarificationQuestion(
                    receipt_document_id=receipt.id,
                    user_id=question.user_id,
                    question_key=f"{base_question_key}_retry",
                    question_text=_telegram_context_question_text(base_question_key),
                )
            )
        receipt.updated_at = datetime.now(timezone.utc)
        session.add(receipt)

    elif receipt and question.question_key in {"business_or_personal_retry", "business_reason"}:
        if question.question_key == "business_or_personal_retry":
            lowered = answer.lower()
            if "personal" in lowered:
                receipt.business_or_personal = "Personal"
                receipt.needs_clarification = False
            elif "business" in lowered:
                receipt.business_or_personal = "Business"
                receipt.needs_clarification = True
                if not _question_exists(session, receipt.id, "business_reason"):
                    new_questions.append(
                        ClarificationQuestion(
                            receipt_document_id=receipt.id,
                            user_id=question.user_id,
                            question_key="business_reason",
                            question_text="What project, customer, or trip should this receipt be attached to?",
                        )
                    )
        else:
            receipt.business_reason = answer.strip()
            if not _question_exists(session, receipt.id, "attendees"):
                new_questions.append(
                    ClarificationQuestion(
                        receipt_document_id=receipt.id,
                        user_id=question.user_id,
                        question_key="attendees",
                        question_text="Who attended or benefited from this expense? If not applicable, reply 'N/A'.",
                    )
                )
        receipt.updated_at = datetime.now(timezone.utc)
        session.add(receipt)

    elif receipt and question.question_key == "attendees":
        receipt.attendees = answer.strip()
        receipt.needs_clarification = False
        receipt.updated_at = datetime.now(timezone.utc)
        session.add(receipt)

    elif receipt and question.question_key == "receipt_date":
        parsed = _parse_date(answer)
        if parsed:
            receipt.extracted_date = parsed
            receipt.updated_at = datetime.now(timezone.utc)
            session.add(receipt)
        else:
            new_questions.append(
                ClarificationQuestion(
                    receipt_document_id=receipt.id,
                    user_id=question.user_id,
                    question_key="receipt_date_retry",
                    question_text="I could not parse that date. Please send it like 2026-03-11.",
                )
            )

    elif receipt and question.question_key == "receipt_date_retry":
        parsed = _parse_date(answer)
        if parsed:
            receipt.extracted_date = parsed
            receipt.updated_at = datetime.now(timezone.utc)
            session.add(receipt)

    elif receipt and question.question_key == "local_amount":
        parsed = _parse_amount(answer)
        if parsed is not None:
            receipt.extracted_local_amount = parsed
            if "usd" in answer.lower():
                receipt.extracted_currency = "USD"
            elif "eur" in answer.lower():
                receipt.extracted_currency = "EUR"
            else:
                receipt.extracted_currency = receipt.extracted_currency or "TRY"
            receipt.updated_at = datetime.now(timezone.utc)
            session.add(receipt)
        else:
            new_questions.append(
                ClarificationQuestion(
                    receipt_document_id=receipt.id,
                    user_id=question.user_id,
                    question_key="local_amount_retry",
                    question_text="I could not parse that amount. Please send it like 419.58 TRY.",
                )
            )

    elif receipt and question.question_key == "local_amount_retry":
        parsed = _parse_amount(answer)
        if parsed is not None:
            receipt.extracted_local_amount = parsed
            receipt.extracted_currency = receipt.extracted_currency or "TRY"
            receipt.updated_at = datetime.now(timezone.utc)
            session.add(receipt)

    elif receipt and question.question_key == "supplier":
        receipt.extracted_supplier = answer.strip()
        receipt.updated_at = datetime.now(timezone.utc)
        session.add(receipt)

    for new_question in new_questions:
        session.add(new_question)

    if receipt:
        still_missing = not receipt.extracted_date or receipt.extracted_local_amount is None or not receipt.extracted_supplier or not receipt.business_or_personal
        if question.question_key in {
            TELEGRAM_MEAL_CONTEXT_QUESTION_KEY,
            f"{TELEGRAM_MEAL_CONTEXT_QUESTION_KEY}_retry",
        }:
            needs_business_context = receipt.business_or_personal == "Business" and not receipt.attendees
        elif question.question_key in {
            TELEGRAM_MARKET_CONTEXT_QUESTION_KEY,
            f"{TELEGRAM_MARKET_CONTEXT_QUESTION_KEY}_retry",
            TELEGRAM_TELECOM_CONTEXT_QUESTION_KEY,
            f"{TELEGRAM_TELECOM_CONTEXT_QUESTION_KEY}_retry",
            TELEGRAM_PERSONAL_CARE_CONTEXT_QUESTION_KEY,
            f"{TELEGRAM_PERSONAL_CARE_CONTEXT_QUESTION_KEY}_retry",
        }:
            needs_business_context = receipt.business_or_personal == "Business" and not receipt.business_reason
        else:
            needs_business_context = receipt.business_or_personal == "Business" and (not receipt.business_reason or not receipt.attendees)
        receipt.needs_clarification = still_missing or needs_business_context or bool(new_questions)
        receipt.updated_at = datetime.now(timezone.utc)
        session.add(receipt)

    # F-AI-Stage1 sub-PR 3: source-tag canonical writes that resolved an
    # inline-keyboard ``edited`` state. Once the edit answer is accepted
    # without a retry question, the user reply becomes the source for the
    # receipt's classification context and the response moves to a terminal
    # state so later text is not treated as part of the same Edit flow.
    if edited_response is not None and receipt is not None:
        canonical_fields: dict[str, Any] = {
            "business_or_personal": receipt.business_or_personal,
            "report_bucket": receipt.report_bucket,
            "business_reason": receipt.business_reason,
            "attendees": receipt.attendees,
        }
        changed_fields = {
            key: value
            for key, value in canonical_fields.items()
            if value != pre_answer_values[key]
        }
        if not new_questions:
            receipt.category_source = "telegram_user"
            receipt.bucket_source = "telegram_user"
            receipt.business_reason_source = "telegram_user"
            receipt.attendees_source = "telegram_user"
            session.add(receipt)
            edited_response.user_action = "confirmed"
            edited_response.user_action_at = datetime.now(timezone.utc)
        edited_response.free_text_reply = answer_text
        edited_response.canonical_write_json = json.dumps(
            {
                "source_tag": "telegram_user",
                "fields": canonical_fields,
                "changed_fields": changed_fields,
            },
            sort_keys=True,
        )
        session.add(edited_response)

    session.commit()
    for new_question in new_questions:
        session.refresh(new_question)
    return new_questions


def _strip_business_context_prefix(answer_text: str) -> str:
    text = answer_text.strip()
    lowered = text.lower()
    if "business" not in lowered:
        return text
    start = lowered.find("business") + len("business")
    return text[start:].lstrip(" :-,;").strip()
