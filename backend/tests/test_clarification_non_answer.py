"""Clarification flow should not treat user questions as failed answers."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

VERIFY_ROOT = Path.cwd() / ".verify_data"
VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{VERIFY_ROOT / f'clarification_non_answer_{uuid4().hex}.db'}"
os.environ["EXPENSE_STORAGE_ROOT"] = str(VERIFY_ROOT)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlmodel import Session, select  # noqa: E402

from app.db import create_db_and_tables, engine  # noqa: E402
from app.models import AppUser, ClarificationQuestion, ReceiptDocument  # noqa: E402
from app.services.clarifications import answer_question  # noqa: E402


def main() -> None:
    create_db_and_tables()
    with Session(engine) as session:
        user = AppUser(telegram_user_id=123)
        receipt = ReceiptDocument(uploader_user_id=1, original_file_name="04-09-Onder.jpg")
        session.add(user)
        session.add(receipt)
        session.commit()
        session.refresh(user)
        session.refresh(receipt)

        question = ClarificationQuestion(
            receipt_document_id=receipt.id,
            user_id=user.id,
            question_key="receipt_date",
            question_text="I could not read the receipt date. What date is on it?",
        )
        session.add(question)
        session.commit()
        session.refresh(question)

        created = answer_question(session, question, "why can't you read the date?")
        session.refresh(question)
        session.refresh(receipt)
        all_questions = session.exec(select(ClarificationQuestion)).all()

        assert question.status == "open"
        assert question.answer_text is None
        assert receipt.extracted_date is None
        assert receipt.needs_clarification is True
        assert len(created) == 1
        assert created[0].question_key == "receipt_date_help"
        assert "I tried to read" in created[0].question_text
        assert len(all_questions) == 2

        amount_question = ClarificationQuestion(
            receipt_document_id=receipt.id,
            user_id=user.id,
            question_key="local_amount",
            question_text="I could not read the receipt amount. What is the total amount and currency?",
        )
        session.add(amount_question)
        session.commit()
        session.refresh(amount_question)

        created = answer_question(session, amount_question, "hello")
        session.refresh(amount_question)
        session.refresh(receipt)

        assert amount_question.status == "open"
        assert amount_question.answer_text is None
        assert receipt.extracted_local_amount is None
        assert len(created) == 1
        assert created[0].question_key == "local_amount_help"
        assert "total amount" in created[0].question_text

        bp_question = ClarificationQuestion(
            receipt_document_id=receipt.id,
            user_id=user.id,
            question_key="business_or_personal",
            question_text="Is this Business or Personal?",
        )
        session.add(bp_question)
        session.commit()
        session.refresh(bp_question)

        created = answer_question(session, bp_question, "hello")
        session.refresh(bp_question)

        assert bp_question.status == "open"
        assert bp_question.answer_text is None
        assert len(created) == 1
        assert created[0].question_key == "business_or_personal_help"
        assert "Business or Personal" in created[0].question_text
    print("clarification_non_answer_tests=passed")


if __name__ == "__main__":
    main()
