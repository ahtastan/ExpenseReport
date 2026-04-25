"""Telegram clarification queue should advance to the next open question.

Regression for: after answering ``business_reason``, the bot used to reply
``"Got it. I saved that clarification."`` and stop, because ``answer_question``
returns an empty list when the follow-up (``attendees``) already exists from
the initial seeding. The handler now checks for any remaining open question
for the user and asks it before falling back to the "Got it" message.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

VERIFY_ROOT = Path.cwd() / ".verify_data"
VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{VERIFY_ROOT / f'telegram_clar_advance_{uuid4().hex}.db'}"
os.environ["EXPENSE_STORAGE_ROOT"] = str(VERIFY_ROOT)
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlmodel import Session, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import create_db_and_tables, engine  # noqa: E402
from datetime import date  # noqa: E402

from app.models import AppUser, ClarificationQuestion, ReceiptDocument  # noqa: E402
from app.services import telegram as telegram_service  # noqa: E402
from app.services.clarifications import ensure_receipt_review_questions  # noqa: E402


class _FakeTelegramClient:
    def __init__(self, token: str | None = None):
        self.token = token
        self.messages: list[str] = []

    def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append(text)

    def download_file(self, *args, **kwargs) -> None:  # pragma: no cover
        return None


def test_clarification_advance_full_flow() -> None:
    main()


def main() -> None:
    get_settings.cache_clear()
    create_db_and_tables()

    fake_client = _FakeTelegramClient("test-token")
    original_client = telegram_service.TelegramClient
    telegram_service.TelegramClient = lambda token: fake_client
    try:
        telegram_user_id = 424242
        chat_id = 777

        with Session(engine) as session:
            user = AppUser(telegram_user_id=telegram_user_id)
            session.add(user)
            session.commit()
            session.refresh(user)

            # Simulate a Business receipt where OCR populated date/amount/supplier
            # but business_reason + attendees are still pending.
            receipt = ReceiptDocument(
                uploader_user_id=user.id,
                original_file_name="airport_slip.jpg",
                extracted_date=date(2025, 8, 26),
                extracted_local_amount=Decimal("750.0"),
                extracted_currency="TRY",
                extracted_supplier="IST Sey",
                business_or_personal="Business",
                status="extracted",
            )
            session.add(receipt)
            session.commit()
            session.refresh(receipt)

            questions = ensure_receipt_review_questions(session, receipt, user.id)
            question_keys = {q.question_key for q in questions}
            assert "business_reason" in question_keys
            assert "attendees" in question_keys

            open_before = session.exec(
                select(ClarificationQuestion).where(ClarificationQuestion.status == "open")
            ).all()
            assert len(open_before) == 2

        # Answer business_reason via the Telegram handler.
        payload = {
            "message": {
                "message_id": 1,
                "from": {"id": telegram_user_id, "first_name": "Op"},
                "chat": {"id": chat_id},
                "text": "Istanbul customer visit",
            }
        }
        with Session(engine) as session:
            result = telegram_service.handle_update(session, payload)
            assert result["ok"] is True
            assert result["action"] == "answered_clarification"

        # The bot must have ASKED the next open question (attendees), NOT sent
        # the terminal "Got it" message — the queue still has work.
        assert fake_client.messages, "bot sent no reply"
        last_reply = fake_client.messages[-1]
        assert "Who attended" in last_reply, (
            f"expected attendees question, got: {last_reply!r}"
        )
        assert "Got it. I saved that clarification." not in last_reply

        # Confirm DB reflects the business_reason save and attendees still open.
        with Session(engine) as session:
            receipt_row = session.exec(select(ReceiptDocument)).first()
            assert receipt_row.business_reason == "Istanbul customer visit"
            assert receipt_row.attendees is None
            still_open = session.exec(
                select(ClarificationQuestion).where(ClarificationQuestion.status == "open")
            ).all()
            assert len(still_open) == 1
            assert still_open[0].question_key == "attendees"

        # Answer attendees. Now there are no more open questions — bot should
        # fall back to the terminal "Got it" message.
        payload2 = {
            "message": {
                "message_id": 2,
                "from": {"id": telegram_user_id, "first_name": "Op"},
                "chat": {"id": chat_id},
                "text": "self",
            }
        }
        with Session(engine) as session:
            telegram_service.handle_update(session, payload2)

        assert fake_client.messages[-1] == "Got it. I saved that clarification."

        with Session(engine) as session:
            receipt_row = session.exec(select(ReceiptDocument)).first()
            assert receipt_row.attendees == "self"
            remaining = session.exec(
                select(ClarificationQuestion).where(ClarificationQuestion.status == "open")
            ).all()
            assert remaining == []

    finally:
        telegram_service.TelegramClient = original_client
        get_settings.cache_clear()

    print("telegram_clarification_advance_tests=passed")


if __name__ == "__main__":
    main()
