"""B10 regression — Telegram webhook retries must not duplicate receipts.

When Telegram retries a webhook (timeout, 5xx, etc.) the handler used to
redownload, re-OCR, and create a clone ``ReceiptDocument`` with the same
``telegram_file_unique_id``. These tests pin the dedupe behavior:

- same (user, file_unique_id) with a successful prior row → reuse
- NULL file_unique_id → always create new
- same file_unique_id across different users → separate rows
- prior row was a failed download → retry gets to actually save the file
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

VERIFY_ROOT = Path.cwd() / ".verify_data"
VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{VERIFY_ROOT / f'telegram_idempotency_{uuid4().hex}.db'}"
os.environ["EXPENSE_STORAGE_ROOT"] = str(VERIFY_ROOT)
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import create_db_and_tables, engine  # noqa: E402
from app.models import AppUser, ReceiptDocument  # noqa: E402
from app.services import telegram as telegram_service  # noqa: E402


class _FakeTelegramClient:
    """Records sent messages and returns a fake downloaded path."""

    def __init__(self, token: str | None = None, *, download_result: Path | None = None):
        self.token = token
        self.messages: list[str] = []
        self._download_result = download_result

    def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append(text)

    def download_file(self, file_id, user_id, fallback_name):
        # Return a real file path so the handler stores status="received"
        # rather than "received_metadata_only".
        if self._download_result is not None:
            return self._download_result
        # Write a tiny fake file so storage_path is a real path.
        target = VERIFY_ROOT / f"fake_{uuid4().hex}.jpg"
        target.write_bytes(b"\x00\x01\x02")
        return target


@pytest.fixture(autouse=True)
def _fresh_db():
    create_db_and_tables()
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _install_fake_client(**kwargs) -> _FakeTelegramClient:
    fake = _FakeTelegramClient("test-token", **kwargs)
    telegram_service.TelegramClient = lambda token: fake
    return fake


def _make_user(telegram_user_id: int) -> int:
    with Session(engine) as session:
        user = AppUser(telegram_user_id=telegram_user_id)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user.id


def _photo_payload(telegram_user_id: int, chat_id: int, message_id: int, file_unique_id: str | None):
    photo = {"file_id": f"file_id_{message_id}", "file_size": 1024}
    if file_unique_id is not None:
        photo["file_unique_id"] = file_unique_id
    return {
        "message": {
            "message_id": message_id,
            "from": {"id": telegram_user_id, "first_name": "Op"},
            "chat": {"id": chat_id},
            "photo": [photo],
        }
    }


def test_duplicate_telegram_photo_returns_existing_receipt() -> None:
    original_client = telegram_service.TelegramClient
    try:
        _install_fake_client()
        telegram_user_id = 501
        chat_id = 900

        with Session(engine) as session:
            first = telegram_service.handle_update(
                session,
                _photo_payload(telegram_user_id, chat_id, 1, "unique123"),
            )
        assert first["ok"] is True
        assert first["action"] == "receipt_captured"
        first_id = first["receipt_id"]

        with Session(engine) as session:
            second = telegram_service.handle_update(
                session,
                _photo_payload(telegram_user_id, chat_id, 2, "unique123"),
            )
        assert second["ok"] is True
        assert second["action"] == "receipt_duplicate"
        assert second["receipt_id"] == first_id

        with Session(engine) as session:
            rows = session.exec(
                select(ReceiptDocument).where(
                    ReceiptDocument.telegram_file_unique_id == "unique123"
                )
            ).all()
        assert len(rows) == 1, f"expected 1 receipt, got {len(rows)}"
        assert rows[0].id == first_id
    finally:
        telegram_service.TelegramClient = original_client


def test_duplicate_with_null_file_unique_id_creates_new() -> None:
    original_client = telegram_service.TelegramClient
    try:
        _install_fake_client()
        telegram_user_id = 502
        chat_id = 901

        with Session(engine) as session:
            first = telegram_service.handle_update(
                session,
                _photo_payload(telegram_user_id, chat_id, 10, None),
            )
            second = telegram_service.handle_update(
                session,
                _photo_payload(telegram_user_id, chat_id, 11, None),
            )

        assert first["action"] == "receipt_captured"
        assert second["action"] == "receipt_captured"
        assert first["receipt_id"] != second["receipt_id"]

        with Session(engine) as session:
            user = session.exec(
                select(AppUser).where(AppUser.telegram_user_id == telegram_user_id)
            ).first()
            rows = session.exec(
                select(ReceiptDocument).where(
                    ReceiptDocument.uploader_user_id == user.id
                )
            ).all()
        assert len(rows) == 2, f"expected 2 receipts (null unique_id → no dedupe), got {len(rows)}"
    finally:
        telegram_service.TelegramClient = original_client


def test_different_user_same_file_unique_id_creates_new() -> None:
    original_client = telegram_service.TelegramClient
    try:
        _install_fake_client()

        with Session(engine) as session:
            first = telegram_service.handle_update(
                session,
                _photo_payload(601, 910, 20, "shared"),
            )
            second = telegram_service.handle_update(
                session,
                _photo_payload(602, 911, 21, "shared"),
            )

        assert first["action"] == "receipt_captured"
        assert second["action"] == "receipt_captured"
        assert first["receipt_id"] != second["receipt_id"]

        with Session(engine) as session:
            rows = session.exec(
                select(ReceiptDocument).where(
                    ReceiptDocument.telegram_file_unique_id == "shared"
                )
            ).all()
        assert len(rows) == 2
        uploader_ids = {r.uploader_user_id for r in rows}
        assert len(uploader_ids) == 2, "each user should own their own receipt"
    finally:
        telegram_service.TelegramClient = original_client


def test_duplicate_after_download_failure_creates_new_row() -> None:
    """Amendment 1: a prior failed download must not block the retry.

    If the first attempt landed with status='received_download_failed', the
    retry is the user's real chance to get the file stored. Don't dedupe.
    The stale failed row stays in the DB untouched (cleanup is separate).
    """
    original_client = telegram_service.TelegramClient
    try:
        _install_fake_client()
        telegram_user_id = 701
        chat_id = 920
        user_id = _make_user(telegram_user_id)

        with Session(engine) as session:
            failed = ReceiptDocument(
                uploader_user_id=user_id,
                status="received_download_failed",
                content_type="photo",
                telegram_file_unique_id="retry-me",
                original_file_name="telegram_photo_30.jpg",
                storage_path=None,
            )
            session.add(failed)
            session.commit()
            session.refresh(failed)
            failed_id = failed.id

        with Session(engine) as session:
            result = telegram_service.handle_update(
                session,
                _photo_payload(telegram_user_id, chat_id, 30, "retry-me"),
            )
        assert result["action"] == "receipt_captured"
        assert result["receipt_id"] != failed_id

        with Session(engine) as session:
            rows = session.exec(
                select(ReceiptDocument)
                .where(ReceiptDocument.telegram_file_unique_id == "retry-me")
                .order_by(ReceiptDocument.id)
            ).all()
        assert len(rows) == 2, f"expected both failed+new rows, got {len(rows)}"
        assert rows[0].id == failed_id
        assert rows[0].status == "received_download_failed"
        assert rows[0].storage_path is None
        assert rows[1].status in ("received", "extracted", "needs_extraction_review")
    finally:
        telegram_service.TelegramClient = original_client
