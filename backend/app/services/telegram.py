import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from app.config import get_settings
from app.models import AppUser, ReceiptDocument
from app.services.clarifications import (
    answer_question,
    ensure_receipt_review_questions,
    next_open_question_for_user,
)
from app.services.receipt_extraction import apply_receipt_extraction
from app.services.storage import save_bytes


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
    return max(photos, key=lambda item: item.get("file_size") or 0)


def _document_is_receipt(document: dict[str, Any]) -> bool:
    mime = (document.get("mime_type") or "").lower()
    name = (document.get("file_name") or "").lower()
    return mime.startswith("image/") or mime == "application/pdf" or name.endswith((".jpg", ".jpeg", ".png", ".webp", ".pdf"))


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

    if document and not _document_is_receipt(document):
        client.send_message(chat_id, "I received a document, but it does not look like a receipt image or PDF yet.")
        return {"ok": True, "action": "unsupported_document", "user_id": user.id}

    file_payload = photo or document
    content_type = "photo" if photo else "document"
    file_id = file_payload.get("file_id")
    original_name = file_payload.get("file_name") or f"telegram_{content_type}_{message.get('message_id')}.jpg"
    storage_path = None
    status = "received"
    try:
        downloaded = client.download_file(file_id, user.id, original_name) if file_id else None
        storage_path = str(downloaded) if downloaded else None
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
