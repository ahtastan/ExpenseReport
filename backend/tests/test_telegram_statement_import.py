"""Telegram statement-upload smoke test."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

from openpyxl import Workbook

VERIFY_ROOT = Path.cwd() / ".verify_data"
VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{VERIFY_ROOT / f'telegram_statement_{uuid4().hex}.db'}"
os.environ["EXPENSE_STORAGE_ROOT"] = str(VERIFY_ROOT)
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlmodel import Session, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import create_db_and_tables, engine  # noqa: E402
from app.models import ReviewRow, ReviewSession, StatementImport, StatementTransaction  # noqa: E402
from app.services import telegram as telegram_service  # noqa: E402


def _write_statement(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.append(["Tran Date", "Supplier", "Source Amount", "Amount Incl"])
    ws.append(["03/11/2026", "Airport Taxi", "100.00 TRY", 2.50])
    ws.append(["03/12/2026", "Hotel", "250.00 TRY", 6.00])
    wb.save(path)
    wb.close()


class _FakeTelegramClient:
    def __init__(self, token: str | None, statement_path: Path):
        self.token = token
        self.statement_path = statement_path
        self.messages: list[str] = []

    def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append(text)

    def download_file(self, file_id: str, user_id: int | None, fallback_name: str) -> Path | None:
        return self.statement_path


def main() -> None:
    get_settings.cache_clear()
    create_db_and_tables()

    statement_path = VERIFY_ROOT / "telegram_diners_statement.xlsx"
    _write_statement(statement_path)
    fake_client = _FakeTelegramClient("test-token", statement_path)
    original_client = telegram_service.TelegramClient
    telegram_service.TelegramClient = lambda token: fake_client
    try:
        payload = {
            "message": {
                "message_id": 77,
                "from": {"id": 12345, "first_name": "Ahmet", "username": "ahmet"},
                "chat": {"id": 67890},
                "document": {
                    "file_id": "telegram-file-id",
                    "file_unique_id": "telegram-unique-id",
                    "file_name": "Diners_Transactions.xlsx",
                    "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                },
            }
        }
        with Session(engine) as session:
            result = telegram_service.handle_update(session, payload)
            assert result["ok"] is True
            assert result["action"] == "statement_imported"
            assert result["statement_import_id"] is not None
            assert result["transactions_imported"] == 2

            statement = session.get(StatementImport, result["statement_import_id"])
            assert statement is not None
            assert statement.uploader_user_id == result["user_id"]
            transactions = session.exec(
                select(StatementTransaction).where(StatementTransaction.statement_import_id == statement.id)
            ).all()
            assert len(transactions) == 2
            review = session.exec(
                select(ReviewSession).where(ReviewSession.statement_import_id == statement.id)
            ).first()
            assert review is not None
            rows = session.exec(select(ReviewRow).where(ReviewRow.review_session_id == review.id)).all()
            assert len(rows) == 2
    finally:
        telegram_service.TelegramClient = original_client
        get_settings.cache_clear()

    assert fake_client.messages
    assert "Imported Diners statement" in fake_client.messages[-1]
    assert "2 transactions" in fake_client.messages[-1]
    print("telegram_statement_import_tests=passed")


if __name__ == "__main__":
    main()
