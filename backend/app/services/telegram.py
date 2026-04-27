import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from app.config import get_settings
from app.models import AppUser, ClarificationQuestion, ReceiptDocument
from app.services.clarifications import (
    answer_question,
    ensure_receipt_review_questions,
    next_open_question_for_user,
)
from app.services.receipt_extraction import apply_receipt_extraction
from app.services.review_sessions import get_or_create_review_session
from app.services.storage import save_bytes
from app.services.statement_import import import_diners_excel

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


def handle_update(session: Session, update: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    client = TelegramClient(settings.telegram_bot_token)
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

    text = (message.get("text") or "").strip()
    if text:
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
            open_questions = session.exec(
                select(ClarificationQuestion)
                .where(
                    ClarificationQuestion.receipt_document_id == existing.id,
                    ClarificationQuestion.status == "open",
                )
                .order_by(ClarificationQuestion.id)
            ).all()
            if open_questions:
                if existing.ocr_confidence is not None and existing.ocr_confidence >= 0.6:
                    summary = (
                        f"I read: {existing.extracted_date or '?'} | "
                        f"{existing.extracted_supplier or '?'} | "
                        f"{existing.extracted_local_amount or '?'} {existing.extracted_currency or ''}."
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
                "questions_created": 0,
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
    questions = ensure_receipt_review_questions(session, receipt, user.id)
    if questions:
        if extraction.confidence and extraction.confidence >= 0.6:
            summary = (
                f"I read: {receipt.extracted_date or '?'} | "
                f"{receipt.extracted_supplier or '?'} | "
                f"{receipt.extracted_local_amount or '?'} {receipt.extracted_currency or ''}."
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
    }
