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
from app.services.agent_receipt_canonical_writer import write_ai_proposal_to_canonical
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
from app.services.telegram_keyboard_composer import parse_callback_data
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

    written = write_ai_proposal_to_canonical(
        session,
        receipt=receipt,
        agent_read=agent_read,
        source_tag="auto_confirmed_default",
    )
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


def _auto_close_pending_responses(
    session: Session,
    client: "TelegramClient",
    *,
    user_id: int,
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
    parsed = parse_callback_data(callback_query.get("data"))
    message = callback_query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")

    if parsed is None:
        logger.warning(
            "inline keyboard: malformed callback_data data=%r user_id=%s",
            callback_query.get("data"),
            user.id,
        )
        _safe_answer_callback(client, callback_id)
        return {"ok": True, "action": "callback_malformed_ignored"}

    action, user_response_id = parsed
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
        chat_id=chat_id,
        reason="timeout",
    )
    session.refresh(user_response)

    if user_response.user_action != "pending":
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
    written = write_ai_proposal_to_canonical(
        session,
        receipt=receipt,
        agent_read=agent_read,
        source_tag="ai_advisory",
    )
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
    user_response: AgentReceiptUserResponse,
    receipt: ReceiptDocument,
    agent_read: AgentReceiptRead,
    user: AppUser,
    chat_id: int | None,
    message_id: int | None,
) -> dict[str, Any]:
    user_response.user_action = "edited"
    user_response.user_action_at = utc_now()
    session.add(user_response)
    session.commit()

    ai_review: dict[str, Any] | None = None
    try:
        parsed_read_json = json.loads(agent_read.read_json or "{}")
        if isinstance(parsed_read_json, dict):
            ai_review = parsed_read_json
    except json.JSONDecodeError:
        logger.warning(
            "inline keyboard edit: invalid agent_read.read_json for response_id=%s",
            user_response.id,
        )
    business_context_question_keys = receipt_business_context_question_keys(
        receipt,
        ai_review=ai_review,
    )
    question_key = (
        business_context_question_keys[0]
        if business_context_question_keys
        else TELEGRAM_MARKET_CONTEXT_QUESTION_KEY
    )
    ensure_receipt_review_questions(
        session,
        receipt,
        user.id,
        include_business_context=True,
        business_context_question_keys=(question_key,),
        include_business_personal=False,
    )

    edit_text = "✏️ Got it — please type the correction."
    if chat_id is not None and message_id is not None:
        try:
            client.call(
                "editMessageText",
                {"chat_id": chat_id, "message_id": message_id, "text": edit_text},
            )
        except Exception as exc:
            logger.warning(
                "inline keyboard edit: editMessageText failed: %s", exc
            )
    if chat_id is not None:
        client.send_message(
            chat_id,
            "Reply with the corrected classification (e.g. business, customer dinner with Hakan).",
        )

    return {
        "ok": True,
        "action": "callback_edit_requested",
        "user_response_id": user_response.id,
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
        chat_id=chat_id,
        reason="timeout",
    )
    ai_receipt_reply_allowed = should_send_ai_receipt_reply(settings, user.telegram_user_id)
    ai_receipt_followups_allowed = should_send_telegram_receipt_followups(settings, user.telegram_user_id)

    text = (message.get("text") or "").strip()
    if text:
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
    if should_use_inline_keyboard(settings, user.telegram_user_id):
        _auto_close_pending_responses(
            session,
            client,
            user_id=user.id,
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
