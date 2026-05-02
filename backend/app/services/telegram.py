import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from app.config import get_settings
from app.json_utils import dumps as json_dumps
from app.models import (
    AgentReceiptRead,
    AgentReceiptReviewRun,
    AgentReceiptUserResponse,
    AppUser,
    ClarificationQuestion,
    ReceiptDocument,
    utc_now,
)
from app.services.agent_receipt_canonical_writer import (
    CanonicalWriteLinkageError,
    write_ai_proposal_to_canonical,
)
from app.services.clarifications import (
    TELEGRAM_MARKET_CONTEXT_QUESTION_KEY,
    answer_question,
    ensure_receipt_review_questions,
    looks_like_telegram_context_answer,
    next_open_telegram_context_question_for_user,
    next_open_question_for_receipt,
    next_open_question_for_user,
    open_telegram_context_question_keys_for_receipt,
)
from app.services.receipt_extraction import apply_receipt_extraction
from app.services.review_sessions import get_or_create_review_session
from app.services.storage import save_bytes
from app.services.statement_import import import_diners_excel
from app.services.telegram_keyboard_composer import (
    build_category_tier1_markup,
    build_category_tier2_markup,
    build_edit_menu_markup,
    build_inline_keyboard_reply,
    build_receipt_menu_markup,
    build_skip_reason_attendees_markup,
    build_type_menu_markup,
    parse_callback_data,
    parse_menu_callback_data,
)
from app.services.telegram_edit_parsers import (
    parse_amount_reply,
    parse_attendees_reason_reply,
    parse_date_reply,
    parse_supplier_reply,
)
from app.services.telegram_receipt_reply import (
    maybe_create_telegram_receipt_ai_review,
    maybe_send_telegram_receipt_reply,
    receipt_business_context_question_keys,
    send_inline_keyboard_proposal,
    should_include_receipt_business_context,
    should_send_ai_receipt_reply,
    should_send_telegram_receipt_followups,
    should_use_inline_keyboard,
)

INLINE_KEYBOARD_TIMEOUT = timedelta(hours=24)
INLINE_KEYBOARD_EDIT_REPLY_WINDOW = timedelta(minutes=60)

logger = logging.getLogger(__name__)


class TelegramClient:
    def __init__(self, token: str | None):
        self.token = token

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
        data = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(self._api_url(method), data=data)
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    def send_message(self, chat_id: int, text: str) -> None:
        if not self.token:
            return
        self.call("sendMessage", {"chat_id": chat_id, "text": text})

    def download_file(self, file_id: str, user_id: int | None, fallback_name: str) -> Path | None:
        if not self.token:
            return None
        file_info = self.call("getFile", {"file_id": file_id})
        file_path = file_info.get("result", {}).get("file_path")
        if not file_path:
            return None
        ext = Path(file_path).suffix or Path(fallback_name).suffix
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        with urllib.request.urlopen(url, timeout=30) as response:
            content = response.read()
        target_name = fallback_name if Path(fallback_name).suffix else f"{Path(fallback_name).stem}{ext or '.jpg'}"
        return save_bytes(content, "receipts", user_id, target_name)


def _display_name(user: dict[str, Any]) -> str | None:
    parts = [user.get("first_name"), user.get("last_name")]
    name = " ".join(part for part in parts if part)
    return name or user.get("username")


def upsert_telegram_user(session: Session, user_payload: dict[str, Any]) -> AppUser:
    telegram_user_id = user_payload.get("id")
    user = session.exec(select(AppUser).where(AppUser.telegram_user_id == telegram_user_id)).first()
    if not user:
        user = AppUser(telegram_user_id=telegram_user_id)
    user.username = user_payload.get("username")
    user.first_name = user_payload.get("first_name")
    user.last_name = user_payload.get("last_name")
    user.display_name = _display_name(user_payload)
    user.updated_at = datetime.now(timezone.utc)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _best_photo(message: dict[str, Any]) -> dict[str, Any] | None:
    photos = message.get("photo") or []
    if not photos:
        return None
    if all(_photo_file_size(item) > 0 for item in photos):
        return max(photos, key=_photo_file_size)
    return max(photos, key=_photo_area)


def _photo_file_size(photo: dict[str, Any]) -> int:
    try:
        return int(photo.get("file_size") or 0)
    except (TypeError, ValueError):
        return 0


def _photo_area(photo: dict[str, Any]) -> int:
    try:
        width = int(photo.get("width") or 0)
        height = int(photo.get("height") or 0)
    except (TypeError, ValueError):
        return 0
    return width * height


def _stored_image_metadata(path: Path) -> tuple[int | None, int | None, int | None]:
    try:
        byte_size = path.stat().st_size
    except OSError:
        byte_size = None
    try:
        from PIL import Image  # deferred import; optional outside OCR installs

        with Image.open(path) as image:
            return image.width, image.height, byte_size
    except Exception:
        return None, None, byte_size


def _log_stored_receipt_media(path: Path, *, content_type: str, original_name: str) -> None:
    width, height, byte_size = _stored_image_metadata(path)
    logger.info(
        "Telegram stored receipt media: content_type=%s original_name=%s path=%s width=%s height=%s bytes=%s",
        content_type,
        original_name,
        path,
        width,
        height,
        byte_size,
    )


def _send_receipt_progress_ack(client: TelegramClient, chat_id: int, receipt_id: int | None) -> None:
    try:
        client.send_message(chat_id, "Reading receipt…")
    except Exception as exc:
        logger.warning(
            "Telegram receipt progress ack failed for chat_id=%s receipt_id=%s: %s",
            chat_id,
            receipt_id,
            exc,
        )


def _format_receipt_amount(amount: Any, currency: str | None) -> str:
    if amount is None:
        amount_text = "?"
    else:
        try:
            amount_text = f"{Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"
        except (InvalidOperation, ValueError):
            amount_text = str(amount)
    currency_text = (currency or "").strip()
    return f"{amount_text} {currency_text}".strip()


def _document_is_receipt(document: dict[str, Any]) -> bool:
    mime = (document.get("mime_type") or "").lower()
    name = (document.get("file_name") or "").lower()
    return mime.startswith("image/") or mime == "application/pdf" or name.endswith((".jpg", ".jpeg", ".png", ".webp", ".pdf"))


def _document_is_statement(document: dict[str, Any]) -> bool:
    mime = (document.get("mime_type") or "").lower()
    name = (document.get("file_name") or "").lower()
    excel_mimes = {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel.sheet.macroenabled.12",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.template",
        "application/vnd.ms-excel.template.macroenabled.12",
    }
    return mime in excel_mimes or name.endswith((".xlsx", ".xlsm", ".xltx", ".xltm"))


def _statement_period_text(statement) -> str:
    if statement.period_start and statement.period_end:
        return f" for {statement.period_start.isoformat()} to {statement.period_end.isoformat()}"
    return ""


def _latest_receipt_id_for_user(session: Session, user_id: int) -> int | None:
    return session.exec(
        select(ReceiptDocument.id)
        .where(ReceiptDocument.uploader_user_id == user_id)
        .order_by(ReceiptDocument.created_at.desc(), ReceiptDocument.id.desc())
    ).first()


def _pending_responses_for_user(
    session: Session,
    user_id: int,
) -> list[AgentReceiptUserResponse]:
    rows = session.exec(
        select(AgentReceiptUserResponse, ReceiptDocument)
        .join(
            ReceiptDocument,
            AgentReceiptUserResponse.receipt_document_id == ReceiptDocument.id,
        )
        .where(
            ReceiptDocument.uploader_user_id == user_id,
            AgentReceiptUserResponse.user_action == "pending",
        )
        .order_by(AgentReceiptUserResponse.id)
    ).all()
    return [response for response, _receipt in rows]


def _recent_edited_response_for_user(
    session: Session,
    user_id: int,
    *,
    now: datetime | None = None,
) -> AgentReceiptUserResponse | None:
    """Return a recent inline-keyboard Edit response for this user, if any."""
    cutoff = (now or utc_now()) - INLINE_KEYBOARD_EDIT_REPLY_WINDOW
    rows = session.exec(
        select(AgentReceiptUserResponse, ReceiptDocument)
        .join(
            ReceiptDocument,
            AgentReceiptUserResponse.receipt_document_id == ReceiptDocument.id,
        )
        .where(
            ReceiptDocument.uploader_user_id == user_id,
            AgentReceiptUserResponse.user_action == "edited",
        )
        .order_by(
            AgentReceiptUserResponse.user_action_at.desc(),
            AgentReceiptUserResponse.id.desc(),
        )
    ).all()
    for response, _receipt in rows:
        action_at = response.user_action_at
        if action_at is None:
            continue
        if action_at.tzinfo is None:
            action_at = action_at.replace(tzinfo=timezone.utc)
        if action_at >= cutoff:
            return response
    return None


_AWAITING_STATES: tuple[str, ...] = (
    "awaiting_supplier",
    "awaiting_date",
    "awaiting_amount",
    "awaiting_attendees_reason",
)


def _awaiting_response_for_user(
    session: Session,
    user_id: int,
) -> AgentReceiptUserResponse | None:
    """Return the most recent ``AgentReceiptUserResponse`` for this user
    that is in one of the ``awaiting_*`` states. Most recent wins so a
    cancelled/edited follow-up doesn't accidentally route a stale prompt."""
    rows = session.exec(
        select(AgentReceiptUserResponse, ReceiptDocument)
        .join(
            ReceiptDocument,
            AgentReceiptUserResponse.receipt_document_id == ReceiptDocument.id,
        )
        .where(
            ReceiptDocument.uploader_user_id == user_id,
            AgentReceiptUserResponse.user_action.in_(_AWAITING_STATES),  # type: ignore[attr-defined]
        )
        .order_by(
            AgentReceiptUserResponse.user_action_at.desc(),
            AgentReceiptUserResponse.id.desc(),
        )
    ).all()
    if not rows:
        return None
    response, _receipt = rows[0]
    return response


def _open_question_for_edited_response(
    session: Session,
    *,
    user_id: int,
    user_response: AgentReceiptUserResponse,
) -> ClarificationQuestion | None:
    context_keys = open_telegram_context_question_keys_for_receipt(
        session,
        user_id,
        user_response.receipt_document_id,
    )
    if not context_keys:
        return None
    return next_open_question_for_receipt(
        session,
        user_id,
        user_response.receipt_document_id,
        include_business_context=True,
        business_context_question_keys=context_keys,
    )


def _close_pending_response(
    session: Session,
    client: "TelegramClient",
    *,
    user_response: AgentReceiptUserResponse,
    reason: str,
    chat_id: int | None,
) -> None:
    """Auto-confirm a pending inline-keyboard proposal due to supersede or
    timeout. Writes canonical with ``source_tag='auto_confirmed_default'``,
    flips the response row, and edits the original keyboard message.
    """
    receipt = session.get(ReceiptDocument, user_response.receipt_document_id)
    agent_read = session.get(AgentReceiptRead, user_response.agent_receipt_read_id)
    if receipt is None or agent_read is None:
        # Best-effort: mark closed without canonical write.
        user_response.user_action = (
            "auto_confirmed_timeout" if reason == "timeout" else "auto_confirmed_supersede"
        )
        user_response.user_action_at = utc_now()
        session.add(user_response)
        session.commit()
        return

    try:
        written = write_ai_proposal_to_canonical(
            session,
            receipt=receipt,
            agent_read=agent_read,
            source_tag="auto_confirmed_default",
            expected_review_run_id=user_response.agent_receipt_review_run_id,
        )
    except CanonicalWriteLinkageError as exc:
        logger.error(
            "canonical write linkage failed during auto-close response_id=%s "
            "agent_read_id=%s receipt_id=%s: %s",
            user_response.id,
            agent_read.id,
            receipt.id,
            exc,
        )
        _mark_response_failed_validation(session, user_response)
        return
    user_response.user_action = (
        "auto_confirmed_timeout" if reason == "timeout" else "auto_confirmed_supersede"
    )
    user_response.user_action_at = utc_now()
    user_response.canonical_write_json = json_dumps(written, sort_keys=True)
    session.add(user_response)
    session.commit()

    if chat_id is not None and user_response.keyboard_message_id is not None:
        edit_text = (
            "Auto-confirmed after 24h."
            if reason == "timeout"
            else "Auto-confirmed (you sent another receipt)."
        )
        try:
            client.call(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": user_response.keyboard_message_id,
                    "text": edit_text,
                },
            )
        except Exception as exc:
            logger.warning(
                "inline keyboard auto-close: editMessageText failed for response_id=%s: %s",
                user_response.id,
                exc,
            )


def _mark_response_failed_validation(
    session: Session,
    user_response: AgentReceiptUserResponse,
) -> None:
    user_response.user_action = "failed_validation"
    user_response.user_action_at = utc_now()
    session.add(user_response)
    session.commit()


def _auto_close_pending_responses(
    session: Session,
    client: "TelegramClient",
    *,
    user_id: int,
    telegram_user_id: int | None,
    chat_id: int | None,
    reason: str,
    now: datetime | None = None,
) -> int:
    """Walk all pending responses for the user and close those that match
    the given ``reason``.

    - ``reason='supersede'``: closes ALL pending rows (caller invokes this
      at the start of a new receipt upload, before opening a new keyboard).
    - ``reason='timeout'``: closes only rows whose ``created_at`` is older
      than :data:`INLINE_KEYBOARD_TIMEOUT`. Safe to call on every webhook
      event (no-op when no rows are stale).
    """
    if reason not in {"supersede", "timeout"}:
        raise ValueError(f"unknown _auto_close reason: {reason!r}")

    pending = _pending_responses_for_user(session, user_id=user_id)
    if not pending:
        return 0

    cutoff = (now or utc_now()) - INLINE_KEYBOARD_TIMEOUT
    closed = 0
    for response in pending:
        if reason == "timeout":
            response_created_at = response.created_at
            if response_created_at is None:
                continue
            if response_created_at.tzinfo is None:
                response_created_at = response_created_at.replace(tzinfo=timezone.utc)
            if response_created_at > cutoff:
                continue
        if response.telegram_user_id is None:
            logger.warning(
                "inline keyboard auto-close: response_id=%s has null "
                "telegram_user_id; marking failed_validation",
                response.id,
            )
            _mark_response_failed_validation(session, response)
            closed += 1
            continue
        if response.telegram_user_id != telegram_user_id:
            logger.warning(
                "inline keyboard auto-close: response_id=%s owned by "
                "telegram_user_id=%s but current telegram_user_id=%s; "
                "marking failed_validation",
                response.id,
                response.telegram_user_id,
                telegram_user_id,
            )
            _mark_response_failed_validation(session, response)
            closed += 1
            continue
        _close_pending_response(
            session,
            client,
            user_response=response,
            reason=reason,
            chat_id=chat_id,
        )
        closed += 1
    return closed


def _resolve_chat_id_for_response(
    session: Session, user_response: AgentReceiptUserResponse
) -> int | None:
    receipt = session.get(ReceiptDocument, user_response.receipt_document_id)
    return receipt.telegram_chat_id if receipt is not None else None


def _handle_callback_query(
    session: Session,
    client: "TelegramClient",
    callback_query: dict[str, Any],
) -> dict[str, Any]:
    """Handle a Telegram inline-keyboard button tap.

    Always answers the callback (dismissing the loading spinner). On
    malformed data, missing response, or already-finalized response,
    returns an ignored result without raising.
    """
    callback_id = callback_query.get("id")
    user_payload = callback_query.get("from") or {}
    if not user_payload:
        _safe_answer_callback(client, callback_id)
        return {"ok": False, "action": "ignored", "message": "callback missing user"}

    if get_settings().allowed_telegram_user_ids and user_payload.get("id") not in get_settings().allowed_telegram_user_ids:
        _safe_answer_callback(client, callback_id)
        return {"ok": False, "action": "blocked", "message": "user not allowlisted"}

    user = upsert_telegram_user(session, user_payload)
    raw_data = callback_query.get("data")
    parsed = parse_callback_data(raw_data)
    menu_parsed = parse_menu_callback_data(raw_data) if parsed is None else None
    message = callback_query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")

    if parsed is None and menu_parsed is None:
        logger.warning(
            "inline keyboard: malformed callback_data data=%r user_id=%s",
            raw_data,
            user.id,
        )
        _safe_answer_callback(client, callback_id)
        return {"ok": True, "action": "callback_malformed_ignored"}

    if parsed is not None:
        action, user_response_id = parsed
        menu_scope: str | None = None
        menu_choice: str | None = None
    else:
        # menu-navigation callback
        assert menu_parsed is not None
        menu_scope, menu_choice, user_response_id = menu_parsed
        action = "menu"

    user_response = session.get(AgentReceiptUserResponse, user_response_id)
    if user_response is None:
        logger.warning(
            "inline keyboard: callback for unknown response_id=%s user_id=%s",
            user_response_id,
            user.id,
        )
        _safe_answer_callback(client, callback_id)
        return {"ok": True, "action": "callback_unknown_ignored"}

    callback_telegram_user_id = user_payload.get("id")
    if user_response.telegram_user_id != callback_telegram_user_id:
        logger.warning(
            "callback_query received from telegram_user_id=%s "
            "for user_response.id=%s owned by telegram_user_id=%s; ignoring",
            callback_telegram_user_id,
            user_response.id,
            user_response.telegram_user_id,
        )
        _safe_answer_callback(client, callback_id)
        return {
            "ok": True,
            "action": "callback_owner_mismatch_ignored",
            "user_response_id": user_response_id,
        }

    # Lazy timeout sweep on every callback event.
    _auto_close_pending_responses(
        session,
        client,
        user_id=user.id,
        telegram_user_id=user.telegram_user_id,
        chat_id=chat_id,
        reason="timeout",
    )
    session.refresh(user_response)

    # Top-level Confirm/Edit/Cancel are valid only while pending. Menu
    # callbacks are valid whenever the response is in any interim Edit state
    # (``pending`` for the very first Edit tap, ``edited`` after navigating,
    # or any of the ``awaiting_*`` reply-collection states — the user can
    # back out via the menu instead of typing).
    _MENU_VALID_STATES = {
        "pending",
        "edited",
        "awaiting_supplier",
        "awaiting_date",
        "awaiting_amount",
        "awaiting_attendees_reason",
    }
    if action == "menu":
        if user_response.user_action not in _MENU_VALID_STATES:
            _safe_answer_callback(client, callback_id)
            return {
                "ok": True,
                "action": "callback_already_finalized",
                "user_response_id": user_response_id,
            }
    elif user_response.user_action != "pending":
        # Idempotent: button tapped twice / Telegram retry.
        _safe_answer_callback(client, callback_id)
        return {"ok": True, "action": "callback_already_finalized", "user_response_id": user_response_id}

    receipt = session.get(ReceiptDocument, user_response.receipt_document_id)
    agent_read = session.get(AgentReceiptRead, user_response.agent_receipt_read_id)
    if receipt is None or agent_read is None:
        logger.warning(
            "inline keyboard: missing receipt or agent_read for response_id=%s",
            user_response_id,
        )
        _safe_answer_callback(client, callback_id)
        return {"ok": False, "action": "callback_missing_state", "user_response_id": user_response_id}
    if receipt.uploader_user_id != user.id:
        logger.warning(
            "callback_query received from telegram_user_id=%s user_id=%s "
            "for user_response.id=%s receipt_id=%s owned by user_id=%s; ignoring",
            callback_telegram_user_id,
            user.id,
            user_response.id,
            receipt.id,
            receipt.uploader_user_id,
        )
        _safe_answer_callback(client, callback_id)
        return {
            "ok": True,
            "action": "callback_owner_mismatch_ignored",
            "user_response_id": user_response_id,
        }

    if action == "confirm":
        result = _handle_callback_confirm(
            session, client,
            user_response=user_response,
            receipt=receipt,
            agent_read=agent_read,
            chat_id=chat_id,
            message_id=message_id,
        )
    elif action == "edit":
        result = _handle_callback_edit(
            session, client,
            settings=get_settings(),
            user_response=user_response,
            receipt=receipt,
            agent_read=agent_read,
            user=user,
            chat_id=chat_id,
            message_id=message_id,
        )
    elif action == "menu":
        assert menu_scope is not None and menu_choice is not None
        result = _handle_menu_callback(
            session,
            client,
            settings=get_settings(),
            scope=menu_scope,
            choice=menu_choice,
            user_response=user_response,
            receipt=receipt,
            agent_read=agent_read,
            user=user,
            chat_id=chat_id,
            message_id=message_id,
        )
    else:  # action == "cancel"
        result = _handle_callback_cancel(
            session, client,
            user_response=user_response,
            receipt=receipt,
            chat_id=chat_id,
            message_id=message_id,
        )

    _safe_answer_callback(client, callback_id)
    return result


def _safe_answer_callback(client: "TelegramClient", callback_id: str | None) -> None:
    if not callback_id or not client.enabled:
        return
    try:
        client.call("answerCallbackQuery", {"callback_query_id": callback_id})
    except Exception as exc:
        logger.warning("answerCallbackQuery failed callback_id=%s: %s", callback_id, exc)


def _handle_callback_confirm(
    session: Session,
    client: "TelegramClient",
    *,
    user_response: AgentReceiptUserResponse,
    receipt: ReceiptDocument,
    agent_read: AgentReceiptRead,
    chat_id: int | None,
    message_id: int | None,
) -> dict[str, Any]:
    try:
        written = write_ai_proposal_to_canonical(
            session,
            receipt=receipt,
            agent_read=agent_read,
            source_tag="ai_advisory",
            expected_review_run_id=user_response.agent_receipt_review_run_id,
            respect_existing_user_source=True,
        )
    except CanonicalWriteLinkageError as exc:
        logger.error(
            "canonical write linkage failed during confirm callback response_id=%s "
            "agent_read_id=%s receipt_id=%s: %s",
            user_response.id,
            agent_read.id,
            receipt.id,
            exc,
        )
        _mark_response_failed_validation(session, user_response)
        return {
            "ok": False,
            "action": "callback_failed_validation",
            "user_response_id": user_response.id,
        }
    user_response.user_action = "confirmed"
    user_response.user_action_at = utc_now()
    user_response.canonical_write_json = json_dumps(written, sort_keys=True)
    session.add(user_response)
    session.commit()

    summary_bits: list[str] = []
    if receipt.report_bucket:
        summary_bits.append(receipt.report_bucket)
    if receipt.business_or_personal:
        summary_bits.append(receipt.business_or_personal)
    if receipt.attendees:
        summary_bits.append(receipt.attendees)
    summary = " / ".join(summary_bits) if summary_bits else "the AI proposal"
    edit_text = f"✅ Confirmed. Categorized as {summary}."

    if chat_id is not None and message_id is not None:
        try:
            client.call(
                "editMessageText",
                {"chat_id": chat_id, "message_id": message_id, "text": edit_text},
            )
        except Exception as exc:
            logger.warning(
                "inline keyboard confirm: editMessageText failed: %s", exc
            )

    return {
        "ok": True,
        "action": "callback_confirmed",
        "user_response_id": user_response.id,
        "receipt_id": receipt.id,
    }


def _handle_callback_edit(
    session: Session,
    client: "TelegramClient",
    *,
    settings: Any,
    user_response: AgentReceiptUserResponse,
    receipt: ReceiptDocument,
    agent_read: AgentReceiptRead,
    user: AppUser,
    chat_id: int | None,
    message_id: int | None,
) -> dict[str, Any]:
    """PR4: replace text-parse Edit fallback with the button-driven menu.

    Flips the response state to ``edited`` and shows the top-level Edit
    menu by editing the original message in place. The Type button is only
    rendered for users in :data:`ai_telegram_reply_allowlist` (the only
    accounts authorized to mark receipts Personal).
    """
    user_response.user_action = "edited"
    user_response.user_action_at = utc_now()
    session.add(user_response)
    session.commit()

    include_type = _user_in_keyboard_allowlist(settings, user.telegram_user_id)
    markup = build_edit_menu_markup(
        user_response.id, include_type_button=include_type
    )
    edit_text = "What would you like to edit?"
    _edit_message_with_markup(
        client,
        chat_id=chat_id,
        message_id=message_id,
        text=edit_text,
        reply_markup=markup,
        log_context=f"response_id={user_response.id}",
    )

    return {
        "ok": True,
        "action": "callback_edit_menu_shown",
        "user_response_id": user_response.id,
        "receipt_id": receipt.id,
    }


def _user_in_keyboard_allowlist(settings: Any, telegram_user_id: int | None) -> bool:
    """True when this user is in the AI-Telegram reply allowlist (the same
    list the inline-keyboard gate uses). Allowlist membership authorizes
    the Type-toggle (Mark Personal) button."""
    if telegram_user_id is None:
        return False
    allowlist = set(getattr(settings, "ai_telegram_reply_allowlist", set()) or set())
    return telegram_user_id in allowlist


def _edit_message_with_markup(
    client: "TelegramClient",
    *,
    chat_id: int | None,
    message_id: int | None,
    text: str,
    reply_markup: dict[str, Any] | None,
    log_context: str,
) -> None:
    """Best-effort editMessageText with optional reply_markup. Logs and
    swallows network errors (the alternative is a half-finalized state)."""
    if chat_id is None or message_id is None:
        return
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
    }
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, separators=(",", ":"))
    try:
        client.call("editMessageText", payload)
    except Exception as exc:
        logger.warning(
            "inline keyboard edit: editMessageText failed (%s): %s",
            log_context,
            exc,
        )


def _send_message_with_markup(
    client: "TelegramClient",
    *,
    chat_id: int,
    text: str,
    reply_markup: dict[str, Any] | None,
) -> None:
    """Send a NEW message with optional inline keyboard markup. Used when
    we want to keep the original keyboard message intact (e.g. Skip prompt
    after a Meals bucket pick)."""
    if not client.enabled:
        return
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
    }
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, separators=(",", ":"))
    try:
        client.call("sendMessage", payload)
    except Exception as exc:
        logger.warning("inline keyboard edit: sendMessage failed: %s", exc)


def _handle_menu_callback(
    session: Session,
    client: "TelegramClient",
    *,
    settings: Any,
    scope: str,
    choice: str,
    user_response: AgentReceiptUserResponse,
    receipt: ReceiptDocument,
    agent_read: AgentReceiptRead,
    user: AppUser,
    chat_id: int | None,
    message_id: int | None,
) -> dict[str, Any]:
    """Dispatch a menu-navigation callback by ``scope``. Each scope is
    handled by its own helper below."""
    if scope == "edit":
        return _handle_menu_edit(
            session, client,
            settings=settings,
            choice=choice,
            user_response=user_response,
            receipt=receipt,
            agent_read=agent_read,
            user=user,
            chat_id=chat_id,
            message_id=message_id,
        )
    if scope == "rcpt":
        return _handle_menu_receipt(
            session, client,
            settings=settings,
            choice=choice,
            user_response=user_response,
            receipt=receipt,
            agent_read=agent_read,
            user=user,
            chat_id=chat_id,
            message_id=message_id,
        )
    if scope == "cat1":
        return _handle_menu_cat1(
            session, client,
            settings=settings,
            choice=choice,
            user_response=user_response,
            receipt=receipt,
            agent_read=agent_read,
            user=user,
            chat_id=chat_id,
            message_id=message_id,
        )
    if scope == "cat2":
        return _handle_menu_cat2(
            session, client,
            settings=settings,
            choice=choice,
            user_response=user_response,
            receipt=receipt,
            agent_read=agent_read,
            user=user,
            chat_id=chat_id,
            message_id=message_id,
        )
    if scope == "type":
        return _handle_menu_type(
            session, client,
            settings=settings,
            choice=choice,
            user_response=user_response,
            receipt=receipt,
            agent_read=agent_read,
            user=user,
            chat_id=chat_id,
            message_id=message_id,
        )
    if scope == "skip_ra":
        return _handle_menu_skip_reason_attendees(
            session, client,
            settings=settings,
            user_response=user_response,
            receipt=receipt,
            agent_read=agent_read,
            chat_id=chat_id,
            message_id=message_id,
        )
    # parse_menu_callback_data already restricted scope; defensive default
    logger.warning("inline keyboard menu: unknown scope=%r", scope)
    return {"ok": False, "action": "callback_menu_unknown_scope"}


def _show_top_level_keyboard(
    client: "TelegramClient",
    *,
    receipt: ReceiptDocument,
    agent_read: AgentReceiptRead,
    user_response: AgentReceiptUserResponse,
    chat_id: int | None,
    message_id: int | None,
) -> None:
    """Re-show the original Confirm/Edit/Cancel keyboard with the receipt's
    current state (post-edit). Used by every "Back" action and by the
    return path after a non-Meals bucket commit."""
    payload = build_inline_keyboard_reply(receipt, agent_read, user_response.id)
    _edit_message_with_markup(
        client,
        chat_id=chat_id,
        message_id=message_id,
        text=payload["text"],
        reply_markup=payload["reply_markup"],
        log_context=f"response_id={user_response.id}/back",
    )


def _handle_menu_edit(
    session: Session,
    client: "TelegramClient",
    *,
    settings: Any,
    choice: str,
    user_response: AgentReceiptUserResponse,
    receipt: ReceiptDocument,
    agent_read: AgentReceiptRead,
    user: AppUser,
    chat_id: int | None,
    message_id: int | None,
) -> dict[str, Any]:
    """Top-level Edit menu dispatcher: receipt / category / type / back."""
    if choice == "receipt":
        markup = build_receipt_menu_markup(user_response.id)
        _edit_message_with_markup(
            client,
            chat_id=chat_id,
            message_id=message_id,
            text="Edit receipt info — pick a field:",
            reply_markup=markup,
            log_context=f"response_id={user_response.id}/edit:receipt",
        )
        return {"ok": True, "action": "callback_menu_receipt_shown"}
    if choice == "category":
        markup = build_category_tier1_markup(user_response.id)
        _edit_message_with_markup(
            client,
            chat_id=chat_id,
            message_id=message_id,
            text="Pick a category:",
            reply_markup=markup,
            log_context=f"response_id={user_response.id}/edit:category",
        )
        return {"ok": True, "action": "callback_menu_cat1_shown"}
    if choice == "type":
        if not _user_in_keyboard_allowlist(settings, user.telegram_user_id):
            logger.warning(
                "inline keyboard menu: type button tapped by non-allowlisted "
                "telegram_user_id=%s response_id=%s",
                user.telegram_user_id,
                user_response.id,
            )
            return {"ok": False, "action": "callback_menu_type_not_allowed"}
        markup = build_type_menu_markup(user_response.id)
        _edit_message_with_markup(
            client,
            chat_id=chat_id,
            message_id=message_id,
            text="Mark this receipt as:",
            reply_markup=markup,
            log_context=f"response_id={user_response.id}/edit:type",
        )
        return {"ok": True, "action": "callback_menu_type_shown"}
    if choice == "back":
        _show_top_level_keyboard(
            client,
            receipt=receipt,
            agent_read=agent_read,
            user_response=user_response,
            chat_id=chat_id,
            message_id=message_id,
        )
        return {"ok": True, "action": "callback_menu_back_to_top"}
    logger.warning("inline keyboard menu edit: unknown choice=%r", choice)
    return {"ok": False, "action": "callback_menu_edit_unknown_choice"}


def _handle_menu_receipt(
    session: Session,
    client: "TelegramClient",
    *,
    settings: Any,
    choice: str,
    user_response: AgentReceiptUserResponse,
    receipt: ReceiptDocument,
    agent_read: AgentReceiptRead,
    user: AppUser,
    chat_id: int | None,
    message_id: int | None,
) -> dict[str, Any]:
    """Receipt info menu: supplier / date / amount / back."""
    if choice == "back":
        # Back to the top-level Edit menu.
        include_type = _user_in_keyboard_allowlist(settings, user.telegram_user_id)
        markup = build_edit_menu_markup(
            user_response.id, include_type_button=include_type
        )
        _edit_message_with_markup(
            client,
            chat_id=chat_id,
            message_id=message_id,
            text="What would you like to edit?",
            reply_markup=markup,
            log_context=f"response_id={user_response.id}/rcpt:back",
        )
        return {"ok": True, "action": "callback_menu_receipt_back"}

    state_for_choice = {
        "supplier": ("awaiting_supplier", "Type the corrected supplier name. Example: GÜKSOYLAR DAYANIKLI TÜK."),
        "date": ("awaiting_date", "Type the date. Format: YYYY-MM-DD or DD.MM.YYYY. Example: 2026-03-19 or 19.03.2026"),
        "amount": ("awaiting_amount", "Type the amount and currency. Example: 755.00 TRY or 755,00 TRY"),
    }
    if choice not in state_for_choice:
        logger.warning("inline keyboard menu receipt: unknown choice=%r", choice)
        return {"ok": False, "action": "callback_menu_receipt_unknown_choice"}

    new_state, prompt_text = state_for_choice[choice]
    user_response.user_action = new_state
    user_response.user_action_at = utc_now()
    session.add(user_response)
    session.commit()

    # Edit the message to show the prompt; no inline keyboard so the user
    # types a reply.
    _edit_message_with_markup(
        client,
        chat_id=chat_id,
        message_id=message_id,
        text=prompt_text,
        reply_markup=None,
        log_context=f"response_id={user_response.id}/rcpt:{choice}",
    )
    return {"ok": True, "action": "callback_menu_receipt_field_prompted",
            "field": choice}


def _handle_menu_cat1(
    session: Session,
    client: "TelegramClient",
    *,
    settings: Any,
    choice: str,
    user_response: AgentReceiptUserResponse,
    receipt: ReceiptDocument,
    agent_read: AgentReceiptRead,
    user: AppUser,
    chat_id: int | None,
    message_id: int | None,
) -> dict[str, Any]:
    """Category Tier 1 dispatch: 'back' returns to top-level Edit menu;
    a numeric index opens the Tier 2 sub-menu for that category."""
    from app.category_vocab import categories

    if choice == "back":
        include_type = _user_in_keyboard_allowlist(settings, user.telegram_user_id)
        markup = build_edit_menu_markup(
            user_response.id, include_type_button=include_type
        )
        _edit_message_with_markup(
            client,
            chat_id=chat_id,
            message_id=message_id,
            text="What would you like to edit?",
            reply_markup=markup,
            log_context=f"response_id={user_response.id}/cat1:back",
        )
        return {"ok": True, "action": "callback_menu_cat1_back"}

    cats = categories()
    try:
        idx = int(choice)
    except ValueError:
        logger.warning("inline keyboard menu cat1: non-int choice=%r", choice)
        return {"ok": False, "action": "callback_menu_cat1_bad_index"}
    if idx < 0 or idx >= len(cats):
        logger.warning(
            "inline keyboard menu cat1: out-of-range idx=%d (len=%d)",
            idx,
            len(cats),
        )
        return {"ok": False, "action": "callback_menu_cat1_bad_index"}
    category = cats[idx]
    markup = build_category_tier2_markup(user_response.id, category)
    _edit_message_with_markup(
        client,
        chat_id=chat_id,
        message_id=message_id,
        text=f"Pick a bucket under {category}:",
        reply_markup=markup,
        log_context=f"response_id={user_response.id}/cat1:{idx}",
    )
    return {
        "ok": True,
        "action": "callback_menu_cat2_shown",
        "category": category,
    }


def _handle_menu_cat2(
    session: Session,
    client: "TelegramClient",
    *,
    settings: Any,
    choice: str,
    user_response: AgentReceiptUserResponse,
    receipt: ReceiptDocument,
    agent_read: AgentReceiptRead,
    user: AppUser,
    chat_id: int | None,
    message_id: int | None,
) -> dict[str, Any]:
    """Category Tier 2 dispatch: 'back' returns to Tier 1; a numeric index
    commits the bucket. If the bucket's parent category is in
    CATEGORIES_REQUIRING_REASON_AND_ATTENDEES, prompt for those next;
    otherwise return to the top-level Confirm/Edit/Cancel keyboard."""
    from app.category_vocab import (
        CATEGORIES_REQUIRING_REASON_AND_ATTENDEES,
        all_buckets,
        category_for_bucket,
    )

    if choice == "back":
        markup = build_category_tier1_markup(user_response.id)
        _edit_message_with_markup(
            client,
            chat_id=chat_id,
            message_id=message_id,
            text="Pick a category:",
            reply_markup=markup,
            log_context=f"response_id={user_response.id}/cat2:back",
        )
        return {"ok": True, "action": "callback_menu_cat2_back"}

    flat = all_buckets()
    try:
        idx = int(choice)
    except ValueError:
        logger.warning("inline keyboard menu cat2: non-int choice=%r", choice)
        return {"ok": False, "action": "callback_menu_cat2_bad_index"}
    if idx < 0 or idx >= len(flat):
        logger.warning(
            "inline keyboard menu cat2: out-of-range idx=%d (len=%d)",
            idx,
            len(flat),
        )
        return {"ok": False, "action": "callback_menu_cat2_bad_index"}
    bucket = flat[idx]
    parent_category = category_for_bucket(bucket)
    if parent_category is None:  # pragma: no cover — defended at index lookup
        logger.error("inline keyboard menu cat2: bucket %r has no parent", bucket)
        return {"ok": False, "action": "callback_menu_cat2_orphan_bucket"}

    receipt.report_bucket = bucket
    receipt.bucket_source = "telegram_user"
    receipt.updated_at = utc_now()
    session.add(receipt)

    if parent_category in CATEGORIES_REQUIRING_REASON_AND_ATTENDEES:
        # Switch state and ask for attendees+reason. Keep the response in
        # an interim state so the next text reply is parsed.
        user_response.user_action = "awaiting_attendees_reason"
        user_response.user_action_at = utc_now()
        session.add(user_response)
        session.commit()
        prompt = (
            "Type attendees and reason, separated by a semicolon.\n\n"
            "Format: <attendees>; <reason>\n\n"
            "Examples:\n"
            "• Burak, Ahmet Yılmaz; customer dinner\n"
            "• Hakan only; team lunch\n\n"
            "Or tap below to skip."
        )
        markup = build_skip_reason_attendees_markup(user_response.id)
        _edit_message_with_markup(
            client,
            chat_id=chat_id,
            message_id=message_id,
            text=prompt,
            reply_markup=markup,
            log_context=f"response_id={user_response.id}/cat2:meals_prompt",
        )
        return {
            "ok": True,
            "action": "callback_menu_cat2_committed_awaiting_ra",
            "bucket": bucket,
        }

    # Non-Meals bucket → return to top-level Confirm/Edit/Cancel keyboard.
    user_response.user_action = "edited"
    user_response.user_action_at = utc_now()
    session.add(user_response)
    session.commit()
    _show_top_level_keyboard(
        client,
        receipt=receipt,
        agent_read=agent_read,
        user_response=user_response,
        chat_id=chat_id,
        message_id=message_id,
    )
    return {
        "ok": True,
        "action": "callback_menu_cat2_committed",
        "bucket": bucket,
    }


def _handle_menu_type(
    session: Session,
    client: "TelegramClient",
    *,
    settings: Any,
    choice: str,
    user_response: AgentReceiptUserResponse,
    receipt: ReceiptDocument,
    agent_read: AgentReceiptRead,
    user: AppUser,
    chat_id: int | None,
    message_id: int | None,
) -> dict[str, Any]:
    """Type toggle: 'back' returns to top-level Edit menu; 'personal'
    immediately marks the receipt Personal (clearing fields) and finalizes
    the response — no further keyboard interaction."""
    if not _user_in_keyboard_allowlist(settings, user.telegram_user_id):
        logger.warning(
            "inline keyboard menu type: blocked non-allowlisted "
            "telegram_user_id=%s response_id=%s",
            user.telegram_user_id,
            user_response.id,
        )
        return {"ok": False, "action": "callback_menu_type_not_allowed"}

    if choice == "back":
        markup = build_edit_menu_markup(
            user_response.id, include_type_button=True
        )
        _edit_message_with_markup(
            client,
            chat_id=chat_id,
            message_id=message_id,
            text="What would you like to edit?",
            reply_markup=markup,
            log_context=f"response_id={user_response.id}/type:back",
        )
        return {"ok": True, "action": "callback_menu_type_back"}

    if choice != "personal":
        logger.warning("inline keyboard menu type: unknown choice=%r", choice)
        return {"ok": False, "action": "callback_menu_type_unknown_choice"}

    # Mark Personal — clears fields (per the spec), commits, and finalizes.
    receipt.business_or_personal = "Personal"
    receipt.category_source = "telegram_user"
    receipt.report_bucket = "Other"
    receipt.bucket_source = "telegram_user"
    receipt.business_reason = None
    receipt.business_reason_source = "telegram_user"
    receipt.attendees = None
    receipt.attendees_source = "telegram_user"
    receipt.needs_clarification = False
    receipt.updated_at = utc_now()
    session.add(receipt)

    written = _build_personal_canonical_payload(receipt)
    user_response.user_action = "confirmed"
    user_response.user_action_at = utc_now()
    user_response.canonical_write_json = json_dumps(written, sort_keys=True)
    session.add(user_response)
    session.commit()

    _edit_message_with_markup(
        client,
        chat_id=chat_id,
        message_id=message_id,
        text="✅ Confirmed as Personal.",
        reply_markup=None,
        log_context=f"response_id={user_response.id}/type:personal",
    )
    return {
        "ok": True,
        "action": "callback_menu_type_personal_confirmed",
        "receipt_id": receipt.id,
    }


def _handle_awaiting_text_reply(
    session: Session,
    client: "TelegramClient",
    *,
    settings: Any,
    user_response: AgentReceiptUserResponse,
    user: AppUser,
    chat_id: int,
    text: str,
) -> dict[str, Any]:
    """Parse a free-text reply for the receipt currently in an awaiting_*
    state. On parse success, write the field, return to the top-level
    Edit menu (or the Confirm/Edit/Cancel keyboard for attendees+reason).
    On parse failure, re-prompt with a corrective message and stay in the
    same awaiting_* state."""
    receipt = session.get(ReceiptDocument, user_response.receipt_document_id)
    if receipt is None:
        logger.warning(
            "awaiting text reply: receipt missing for response_id=%s",
            user_response.id,
        )
        return {"ok": False, "action": "awaiting_text_missing_receipt"}
    if receipt.uploader_user_id != user.id:
        # Defensive: text reply must come from the receipt's uploader.
        logger.warning(
            "awaiting text reply: telegram_user_id=%s tried to answer "
            "response_id=%s for receipt owned by user_id=%s",
            user.telegram_user_id,
            user_response.id,
            receipt.uploader_user_id,
        )
        return {"ok": False, "action": "awaiting_text_owner_mismatch"}

    state = user_response.user_action

    if state == "awaiting_supplier":
        parsed_supplier = parse_supplier_reply(text)
        if parsed_supplier is None:
            client.send_message(
                chat_id,
                "Couldn't read that — type the supplier name (non-empty).",
            )
            return {"ok": True, "action": "awaiting_supplier_reprompt"}
        receipt.extracted_supplier = parsed_supplier
        receipt.updated_at = utc_now()
        session.add(receipt)
        return _post_field_edit_return_to_menu(
            session, client,
            settings=settings,
            user=user,
            user_response=user_response,
            chat_id=chat_id,
            ack_text=f"✅ Supplier updated to: {parsed_supplier}",
            field_name="supplier",
        )

    if state == "awaiting_date":
        parsed_date = parse_date_reply(text)
        if parsed_date is None:
            client.send_message(
                chat_id,
                "Couldn't read that — please use format YYYY-MM-DD or DD.MM.YYYY",
            )
            return {"ok": True, "action": "awaiting_date_reprompt"}
        receipt.extracted_date = parsed_date
        receipt.updated_at = utc_now()
        session.add(receipt)
        return _post_field_edit_return_to_menu(
            session, client,
            settings=settings,
            user=user,
            user_response=user_response,
            chat_id=chat_id,
            ack_text=f"✅ Date updated to: {parsed_date.isoformat()}",
            field_name="date",
        )

    if state == "awaiting_amount":
        parsed_amount = parse_amount_reply(text)
        if parsed_amount is None:
            client.send_message(
                chat_id,
                "Couldn't read that — type amount and currency, e.g. 755.00 TRY or 755,00 TRY",
            )
            return {"ok": True, "action": "awaiting_amount_reprompt"}
        amount, currency = parsed_amount
        receipt.extracted_local_amount = amount
        receipt.extracted_currency = currency
        receipt.updated_at = utc_now()
        session.add(receipt)
        return _post_field_edit_return_to_menu(
            session, client,
            settings=settings,
            user=user,
            user_response=user_response,
            chat_id=chat_id,
            ack_text=f"✅ Amount updated to: {amount} {currency}",
            field_name="amount",
        )

    if state == "awaiting_attendees_reason":
        parsed_ra = parse_attendees_reason_reply(text)
        if parsed_ra is None:
            client.send_message(
                chat_id,
                "Couldn't read that — please use format <attendees>; <reason>. "
                "Example: Hakan only; team lunch",
            )
            return {"ok": True, "action": "awaiting_attendees_reason_reprompt"}
        attendees, reason = parsed_ra
        receipt.attendees = attendees
        receipt.attendees_source = "telegram_user"
        receipt.business_reason = reason
        receipt.business_reason_source = "telegram_user"
        receipt.updated_at = utc_now()
        session.add(receipt)
        # Return to top-level Confirm/Edit/Cancel keyboard since the user
        # just completed a Meals categorization workflow.
        agent_read = session.get(AgentReceiptRead, user_response.agent_receipt_read_id)
        user_response.user_action = "edited"
        user_response.user_action_at = utc_now()
        session.add(user_response)
        session.commit()
        client.send_message(chat_id, "✅ Attendees and reason saved.")
        if agent_read is not None and receipt.telegram_chat_id is not None:
            payload = build_inline_keyboard_reply(receipt, agent_read, user_response.id)
            _send_message_with_markup(
                client,
                chat_id=chat_id,
                text=payload["text"],
                reply_markup=payload["reply_markup"],
            )
        return {"ok": True, "action": "awaiting_attendees_reason_saved"}

    logger.warning(
        "awaiting text reply: unknown state=%r response_id=%s",
        state,
        user_response.id,
    )
    return {"ok": False, "action": "awaiting_text_unknown_state"}


def _post_field_edit_return_to_menu(
    session: Session,
    client: "TelegramClient",
    *,
    settings: Any,
    user: AppUser,
    user_response: AgentReceiptUserResponse,
    chat_id: int,
    ack_text: str,
    field_name: str,
) -> dict[str, Any]:
    """After a Receipt-info text edit lands, ack the user and re-show the
    top-level Edit menu (so they can tweak more or back out to Confirm)."""
    user_response.user_action = "edited"
    user_response.user_action_at = utc_now()
    session.add(user_response)
    session.commit()

    client.send_message(chat_id, ack_text)
    include_type = _user_in_keyboard_allowlist(settings, user.telegram_user_id)
    markup = build_edit_menu_markup(user_response.id, include_type_button=include_type)
    _send_message_with_markup(
        client,
        chat_id=chat_id,
        text="What would you like to edit?",
        reply_markup=markup,
    )
    return {
        "ok": True,
        "action": "awaiting_text_field_saved",
        "field": field_name,
        "user_response_id": user_response.id,
    }


def _build_personal_canonical_payload(receipt: ReceiptDocument) -> dict[str, Any]:
    """Mirror the shape that ``write_ai_proposal_to_canonical`` records, but
    for the Personal toggle path (which writes user-driven values directly
    rather than from the AI proposal)."""
    return {
        "business_or_personal": "Personal",
        "category_source": "telegram_user",
        "report_bucket": "Other",
        "bucket_source": "telegram_user",
        "business_reason": None,
        "business_reason_source": "telegram_user",
        "attendees": None,
        "attendees_source": "telegram_user",
    }


def _handle_menu_skip_reason_attendees(
    session: Session,
    client: "TelegramClient",
    *,
    settings: Any,
    user_response: AgentReceiptUserResponse,
    receipt: ReceiptDocument,
    agent_read: AgentReceiptRead,
    chat_id: int | None,
    message_id: int | None,
) -> dict[str, Any]:
    """Skip the attendees+reason prompt after a Meals bucket pick. Does NOT
    write attendees/business_reason. Marks the receipt as needing follow-up
    via ``needs_clarification=True`` (the only ReceiptDocument-level
    attention column available)."""
    receipt.needs_clarification = True
    receipt.updated_at = utc_now()
    session.add(receipt)

    user_response.user_action = "edited"
    user_response.user_action_at = utc_now()
    session.add(user_response)
    session.commit()

    _show_top_level_keyboard(
        client,
        receipt=receipt,
        agent_read=agent_read,
        user_response=user_response,
        chat_id=chat_id,
        message_id=message_id,
    )
    return {
        "ok": True,
        "action": "callback_menu_skip_ra_done",
        "receipt_id": receipt.id,
    }


def _handle_callback_cancel(
    session: Session,
    client: "TelegramClient",
    *,
    user_response: AgentReceiptUserResponse,
    receipt: ReceiptDocument,
    chat_id: int | None,
    message_id: int | None,
) -> dict[str, Any]:
    receipt.status = "cancelled"
    receipt.updated_at = utc_now()
    session.add(receipt)

    user_response.user_action = "cancelled"
    user_response.user_action_at = utc_now()
    session.add(user_response)
    session.commit()

    edit_text = "❌ Cancelled. Re-send the receipt to retry."
    if chat_id is not None and message_id is not None:
        try:
            client.call(
                "editMessageText",
                {"chat_id": chat_id, "message_id": message_id, "text": edit_text},
            )
        except Exception as exc:
            logger.warning(
                "inline keyboard cancel: editMessageText failed: %s", exc
            )

    return {
        "ok": True,
        "action": "callback_cancelled",
        "user_response_id": user_response.id,
        "receipt_id": receipt.id,
    }


def handle_update(session: Session, update: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    client = TelegramClient(settings.telegram_bot_token)

    # F-AI-Stage1 sub-PR 3: callback_query is dispatched first because
    # button taps don't carry a ``message`` field.
    callback_query = update.get("callback_query")
    if callback_query:
        return _handle_callback_query(session, client, callback_query)

    message = update.get("message") or update.get("edited_message") or {}
    if not message:
        return {"ok": True, "action": "ignored", "message": "No message payload"}

    user_payload = message.get("from") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if not user_payload or not chat_id:
        return {"ok": False, "action": "ignored", "message": "Missing user or chat"}

    if settings.allowed_telegram_user_ids and user_payload.get("id") not in settings.allowed_telegram_user_ids:
        client.send_message(chat_id, "This private expense bot has not been enabled for your Telegram account yet.")
        return {"ok": False, "action": "blocked", "message": "Telegram user is not allowlisted"}

    user = upsert_telegram_user(session, user_payload)

    # Lazy timeout sweep on every webhook event.
    _auto_close_pending_responses(
        session,
        client,
        user_id=user.id,
        telegram_user_id=user.telegram_user_id,
        chat_id=chat_id,
        reason="timeout",
    )
    ai_receipt_reply_allowed = should_send_ai_receipt_reply(settings, user.telegram_user_id)
    ai_receipt_followups_allowed = should_send_telegram_receipt_followups(settings, user.telegram_user_id)

    text = (message.get("text") or "").strip()
    if text:
        # PR4: button-driven Edit menu state machine. If the user's most
        # recent response is in any ``awaiting_*`` state, this text is the
        # answer to that prompt. Route through the parsers and short-circuit
        # the legacy clarifications flow.
        awaiting_response = _awaiting_response_for_user(session, user.id)
        if awaiting_response is not None:
            return _handle_awaiting_text_reply(
                session,
                client,
                settings=settings,
                user_response=awaiting_response,
                user=user,
                chat_id=chat_id,
                text=text,
            )

        if ai_receipt_reply_allowed and ai_receipt_followups_allowed:
            edited_response = _recent_edited_response_for_user(session, user.id)
            if edited_response is not None:
                open_question = _open_question_for_edited_response(
                    session,
                    user_id=user.id,
                    user_response=edited_response,
                )
                if open_question is None:
                    logger.warning(
                        "inline keyboard edit: missing seeded clarification question "
                        "for response_id=%s receipt_id=%s user_id=%s",
                        edited_response.id,
                        edited_response.receipt_document_id,
                        user.id,
                    )
                    client.send_message(
                        chat_id,
                        "Reply with the correction for this receipt, for example: business, customer dinner with Hakan.",
                    )
                    return {
                        "ok": True,
                        "action": "edit_reply_missing_question_prompted",
                        "user_id": user.id,
                        "user_response_id": edited_response.id,
                        "receipt_id": edited_response.receipt_document_id,
                    }
            else:
                latest_receipt_id = _latest_receipt_id_for_user(session, user.id)
                latest_receipt = session.get(ReceiptDocument, latest_receipt_id) if latest_receipt_id is not None else None
                active_context_question_keys = open_telegram_context_question_keys_for_receipt(
                    session,
                    user.id,
                    latest_receipt_id,
                )
                business_context_question_keys = active_context_question_keys or (
                    receipt_business_context_question_keys(latest_receipt)
                    if latest_receipt is not None
                    else ()
                )
                include_business_context = bool(active_context_question_keys) or (
                    should_include_receipt_business_context(latest_receipt)
                    if latest_receipt is not None
                    else False
                )
                open_question = next_open_question_for_receipt(
                    session,
                    user.id,
                    latest_receipt_id,
                    include_business_context=include_business_context,
                    business_context_question_keys=business_context_question_keys,
                )
                if open_question is None and looks_like_telegram_context_answer(text):
                    open_question = next_open_telegram_context_question_for_user(session, user.id)
        elif ai_receipt_reply_allowed:
            open_question = next_open_question_for_user(session, user.id, include_business_context=False)
        else:
            open_question = next_open_question_for_user(session, user.id)
        if open_question:
            created = answer_question(session, open_question, text)
            if created:
                client.send_message(chat_id, created[0].question_text)
            else:
                # No new question was created by answer_question (e.g. the
                # follow-up such as `attendees` already existed from the
                # initial seeding). Ask the next still-open question so the
                # user can keep progressing through the queue instead of
                # getting stuck after a single "Got it".
                if ai_receipt_reply_allowed and ai_receipt_followups_allowed:
                    receipt = (
                        session.get(ReceiptDocument, open_question.receipt_document_id)
                        if open_question.receipt_document_id is not None
                        else None
                    )
                    include_business_context = (
                        should_include_receipt_business_context(receipt)
                        if receipt is not None
                        else False
                    )
                    business_context_question_keys = (
                        receipt_business_context_question_keys(receipt)
                        if receipt is not None
                        else ()
                    )
                    follow_up = next_open_question_for_receipt(
                        session,
                        user.id,
                        open_question.receipt_document_id,
                        include_business_context=include_business_context,
                        business_context_question_keys=business_context_question_keys,
                    )
                elif ai_receipt_reply_allowed:
                    follow_up = next_open_question_for_user(session, user.id, include_business_context=False)
                else:
                    follow_up = next_open_question_for_user(session, user.id)
                if follow_up:
                    client.send_message(chat_id, follow_up.question_text)
                else:
                    client.send_message(chat_id, "Got it. I saved that clarification.")
            return {
                "ok": True,
                "action": "answered_clarification",
                "user_id": user.id,
                "questions_created": len(created),
            }
        client.send_message(chat_id, "Send me a receipt photo/PDF or a Diners statement, and I will file it for review.")
        return {"ok": True, "action": "text_acknowledged", "user_id": user.id}

    photo = _best_photo(message)
    document = message.get("document")
    if not photo and not document:
        client.send_message(chat_id, "I can currently capture receipt photos, receipt PDFs, and clarification replies.")
        return {"ok": True, "action": "unsupported_message", "user_id": user.id}

    if document and _document_is_statement(document):
        file_id = document.get("file_id")
        original_name = document.get("file_name") or f"telegram_statement_{message.get('message_id')}.xlsx"
        try:
            downloaded = client.download_file(file_id, user.id, original_name) if file_id else None
        except Exception as exc:
            print(f"Telegram statement download failed for file_id={file_id}: {exc}")
            downloaded = None
        if downloaded is None:
            text = "I received the statement, but could not download it from Telegram. Please try again."
            client.send_message(chat_id, text)
            return {"ok": False, "action": "statement_download_failed", "user_id": user.id, "message": text}
        try:
            statement = import_diners_excel(session, downloaded, original_name, uploader_user_id=user.id)
            get_or_create_review_session(session, statement.id)
        except ValueError as exc:
            text = f"I received the statement, but could not import it: {exc}"
            client.send_message(chat_id, text)
            return {"ok": False, "action": "statement_import_failed", "user_id": user.id, "message": str(exc)}
        period = _statement_period_text(statement)
        text = f"Imported Diners statement{period}: {statement.row_count} transactions. Review is ready in /review."
        client.send_message(chat_id, text)
        return {
            "ok": True,
            "action": "statement_imported",
            "user_id": user.id,
            "statement_import_id": statement.id,
            "transactions_imported": statement.row_count,
            "message": text,
        }

    if document and not _document_is_receipt(document):
        client.send_message(chat_id, "I received a document, but it does not look like a receipt image or PDF yet.")
        return {"ok": True, "action": "unsupported_document", "user_id": user.id}

    file_payload = photo or document
    content_type = "photo" if photo else "document"
    file_id = file_payload.get("file_id")
    original_name = file_payload.get("file_name") or f"telegram_{content_type}_{message.get('message_id')}.jpg"

    # B10 idempotency — Telegram retries the webhook on timeout/error. Without
    # a dedupe check we would redownload, re-OCR, and create a clone row for
    # the same file_unique_id. Only treat as duplicate if the prior row made
    # it past the download stage; "received_download_failed" /
    # "received_metadata_only" rows represent failed first attempts, so the
    # retry is the user's real chance to get the file stored — fall through
    # to create-new.
    file_unique_id = file_payload.get("file_unique_id")
    if file_unique_id:
        existing = session.exec(
            select(ReceiptDocument).where(
                ReceiptDocument.uploader_user_id == user.id,
                ReceiptDocument.telegram_file_unique_id == file_unique_id,
                ReceiptDocument.status.in_(["received", "extracted", "needs_extraction_review"]),
            )
        ).first()
        if existing is not None:
            print(
                f"Telegram retry dedupe: returning existing receipt {existing.id} "
                f"for file_unique_id={file_unique_id}"
            )
            duplicate_questions: list[ClarificationQuestion] = []
            ai_review = None
            if ai_receipt_reply_allowed:
                ai_review = maybe_create_telegram_receipt_ai_review(
                    session,
                    settings=settings,
                    receipt=existing,
                )
            if ai_receipt_reply_allowed and ai_receipt_followups_allowed:
                business_context_question_keys = receipt_business_context_question_keys(existing, ai_review=ai_review)
                include_business_context = bool(business_context_question_keys)
                duplicate_questions = ensure_receipt_review_questions(
                    session,
                    existing,
                    user.id,
                    include_business_context=include_business_context,
                    business_context_question_keys=business_context_question_keys,
                    include_business_personal=False,
                )
            elif ai_receipt_reply_allowed:
                include_business_context = False
                business_context_question_keys = ()
            else:
                include_business_context = True
                business_context_question_keys = None
            next_question = next_open_question_for_receipt(
                session,
                user.id,
                existing.id,
                include_business_context=include_business_context,
                business_context_question_keys=business_context_question_keys,
            )
            open_questions = [next_question] if next_question is not None else []
            ai_receipt_reply_sent = False
            if ai_receipt_reply_allowed:
                ai_receipt_reply_sent = maybe_send_telegram_receipt_reply(
                    session,
                    client,
                    settings=settings,
                    receipt=existing,
                    telegram_user_id=user.telegram_user_id,
                    chat_id=chat_id,
                    ai_review=ai_review,
                )
            if ai_receipt_reply_sent:
                if open_questions:
                    client.send_message(chat_id, open_questions[0].question_text)
            elif open_questions:
                if existing.ocr_confidence is not None and existing.ocr_confidence >= 0.6:
                    summary = (
                        f"I read: {existing.extracted_date or '?'} | "
                        f"{existing.extracted_supplier or '?'} | "
                        f"{_format_receipt_amount(existing.extracted_local_amount, existing.extracted_currency)}."
                    )
                    client.send_message(chat_id, f"{summary}\n{open_questions[0].question_text}")
                else:
                    client.send_message(chat_id, open_questions[0].question_text)
            else:
                client.send_message(chat_id, "Receipt saved.")
            return {
                "ok": True,
                "action": "receipt_duplicate",
                "receipt_id": existing.id,
                "user_id": user.id,
                "questions_created": len(duplicate_questions),
            }

    storage_path = None
    status = "received"
    downloaded = None
    try:
        downloaded = client.download_file(file_id, user.id, original_name) if file_id else None
        storage_path = str(downloaded) if downloaded else None
        if downloaded is not None:
            _log_stored_receipt_media(downloaded, content_type=content_type, original_name=original_name)
        if not downloaded:
            status = "received_metadata_only"
    except Exception as exc:
        status = "received_download_failed"
        storage_path = None
        print(f"Telegram download failed for file_id={file_id}: {exc}")

    # F-AI-Stage1 PR4 Phase 2: when the inline-keyboard flow is gated on for
    # this user (allowlist + flag), default business_or_personal=Business at
    # upload time so the AI proposal and keyboard always show a defined Type.
    # The legacy clarifications-based default for non-allowlisted users runs
    # later (clarifications.py:_should_default_business_for_telegram_receipt)
    # and only fires when business_or_personal is still None.
    keyboard_gate_open = should_use_inline_keyboard(settings, user.telegram_user_id)
    initial_business_or_personal: str | None = None
    initial_category_source: str | None = None
    if keyboard_gate_open:
        initial_business_or_personal = "Business"
        initial_category_source = "auto_confirmed_default"

    receipt = ReceiptDocument(
        uploader_user_id=user.id,
        status=status,
        content_type=content_type,
        telegram_chat_id=chat_id,
        telegram_message_id=message.get("message_id"),
        telegram_file_id=file_id,
        telegram_file_unique_id=file_payload.get("file_unique_id"),
        original_file_name=original_name,
        mime_type=file_payload.get("mime_type"),
        storage_path=storage_path,
        caption=message.get("caption"),
        business_or_personal=initial_business_or_personal,
        category_source=initial_category_source,
    )
    session.add(receipt)
    session.commit()
    session.refresh(receipt)

    if downloaded is not None:
        _send_receipt_progress_ack(client, chat_id, receipt.id)

    extraction = apply_receipt_extraction(session, receipt)

    # F-AI-Stage1 sub-PR 3: when the inline-keyboard flag is on for this
    # user, supersede any pending old keyboard, then run the new flow and
    # short-circuit the legacy clarification-question + reply sequence.
    # ``keyboard_gate_open`` was computed above for the upload-time default.
    if keyboard_gate_open:
        _auto_close_pending_responses(
            session,
            client,
            user_id=user.id,
            telegram_user_id=user.telegram_user_id,
            chat_id=chat_id,
            reason="supersede",
        )
        keyboard_sent = send_inline_keyboard_proposal(
            session,
            client,
            settings=settings,
            receipt=receipt,
            user_id=user.id,
            telegram_user_id=user.telegram_user_id,
            chat_id=chat_id,
        )
        if keyboard_sent:
            return {
                "ok": True,
                "action": "receipt_keyboard_sent",
                "receipt_id": receipt.id,
                "user_id": user.id,
            }
        # Safety net: keyboard send failed → fall through to legacy reply.

    ai_review = (
        maybe_create_telegram_receipt_ai_review(session, settings=settings, receipt=receipt)
        if ai_receipt_reply_allowed
        else None
    )
    if ai_receipt_reply_allowed:
        business_context_question_keys = (
            receipt_business_context_question_keys(receipt, ai_review=ai_review)
            if ai_receipt_followups_allowed
            else ()
        )
        include_business_context = bool(business_context_question_keys) if ai_receipt_followups_allowed else False
        questions = ensure_receipt_review_questions(
            session,
            receipt,
            user.id,
            include_business_context=include_business_context,
            business_context_question_keys=business_context_question_keys,
            include_business_personal=False,
        )
    else:
        questions = ensure_receipt_review_questions(session, receipt, user.id)
    ai_receipt_reply_sent = maybe_send_telegram_receipt_reply(
        session,
        client,
        settings=settings,
        receipt=receipt,
        telegram_user_id=user.telegram_user_id,
        chat_id=chat_id,
        ai_review=ai_review,
    )
    if ai_receipt_reply_sent:
        if questions:
            client.send_message(chat_id, questions[0].question_text)
    elif questions:
        if extraction.confidence and extraction.confidence >= 0.6:
            summary = (
                f"I read: {receipt.extracted_date or '?'} | "
                f"{receipt.extracted_supplier or '?'} | "
                f"{_format_receipt_amount(receipt.extracted_local_amount, receipt.extracted_currency)}."
            )
            client.send_message(chat_id, f"{summary}\n{questions[0].question_text}")
        else:
            client.send_message(chat_id, questions[0].question_text)
    else:
        client.send_message(chat_id, "Receipt saved.")

    return {
        "ok": True,
        "action": "receipt_captured",
        "receipt_id": receipt.id,
        "user_id": user.id,
        "questions_created": len(questions),
        "ai_receipt_reply_sent": ai_receipt_reply_sent,
    }
