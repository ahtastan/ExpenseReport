"""F1.8 business/personal clarification policy for Telegram receipts."""

from __future__ import annotations

import logging
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlmodel import Session, select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.models import AppUser, ClarificationQuestion, ReceiptDocument
from app.services import model_router
from app.services.clarifications import answer_question, ensure_receipt_review_questions
from app.services.receipt_extraction import apply_receipt_extraction


def _user(session: Session, telegram_user_id: int) -> AppUser:
    user = AppUser(telegram_user_id=telegram_user_id)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _complete_telegram_receipt(session: Session, user: AppUser) -> ReceiptDocument:
    receipt = ReceiptDocument(
        uploader_user_id=user.id,
        source="telegram",
        telegram_chat_id=12345,
        original_file_name="telegram_photo_1.jpg",
        extracted_date=date(2025, 11, 15),
        extracted_supplier="YENI TRUVA MARKET",
        extracted_local_amount=Decimal("175.0000"),
        extracted_currency="TRY",
        business_or_personal=None,
        status="needs_extraction_review",
    )
    session.add(receipt)
    session.commit()
    session.refresh(receipt)
    return receipt


def _question_keys(session: Session, receipt: ReceiptDocument) -> list[str]:
    return [
        question.question_key
        for question in session.exec(
            select(ClarificationQuestion)
            .where(ClarificationQuestion.receipt_document_id == receipt.id)
            .order_by(ClarificationQuestion.id)
        ).all()
    ]


def test_non_allowlisted_telegram_user_defaults_to_business_without_bp_question(
    isolated_db,
    monkeypatch,
    caplog,
) -> None:
    monkeypatch.delenv("BUSINESS_PERSONAL_CLARIFICATION_TELEGRAM_IDS", raising=False)
    get_settings.cache_clear()
    caplog.set_level(logging.INFO, logger="app.services.clarifications")

    with Session(isolated_db) as session:
        user = _user(session, telegram_user_id=100001)
        receipt = _complete_telegram_receipt(session, user)

        questions = ensure_receipt_review_questions(session, receipt, user.id)
        session.refresh(receipt)

        assert receipt.business_or_personal == "Business"
        keys = [question.question_key for question in questions]
        assert "business_or_personal" not in keys
        assert "business_reason" in keys
        assert "attendees" in keys
        assert "default_business_policy" in caplog.text
        assert "allowlisted=False" in caplog.text


def test_allowlisted_telegram_user_still_gets_business_personal_question(
    isolated_db,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BUSINESS_PERSONAL_CLARIFICATION_TELEGRAM_IDS", "100002, 100003")
    get_settings.cache_clear()

    with Session(isolated_db) as session:
        user = _user(session, telegram_user_id=100002)
        receipt = _complete_telegram_receipt(session, user)

        questions = ensure_receipt_review_questions(session, receipt, user.id)
        session.refresh(receipt)

        assert receipt.business_or_personal is None
        keys = [question.question_key for question in questions]
        assert "business_or_personal" in keys
        assert "business_reason" not in keys
        assert "attendees" not in keys


def test_non_telegram_receipt_still_gets_business_personal_question(
    isolated_db,
    monkeypatch,
) -> None:
    monkeypatch.delenv("BUSINESS_PERSONAL_CLARIFICATION_TELEGRAM_IDS", raising=False)
    get_settings.cache_clear()

    with Session(isolated_db) as session:
        user = _user(session, telegram_user_id=100008)
        receipt = _complete_telegram_receipt(session, user)
        receipt.source = "review_ui"
        session.add(receipt)
        session.commit()
        session.refresh(receipt)

        questions = ensure_receipt_review_questions(session, receipt, user.id)
        session.refresh(receipt)

        assert receipt.business_or_personal is None
        keys = [question.question_key for question in questions]
        assert "business_or_personal" in keys
        assert "business_reason" not in keys
        assert "attendees" not in keys


def test_existing_personal_value_is_preserved_for_default_telegram_user(
    isolated_db,
    monkeypatch,
) -> None:
    monkeypatch.delenv("BUSINESS_PERSONAL_CLARIFICATION_TELEGRAM_IDS", raising=False)
    get_settings.cache_clear()

    with Session(isolated_db) as session:
        user = _user(session, telegram_user_id=100009)
        receipt = _complete_telegram_receipt(session, user)
        receipt.business_or_personal = "Personal"
        session.add(receipt)
        session.commit()
        session.refresh(receipt)

        questions = ensure_receipt_review_questions(session, receipt, user.id)
        session.refresh(receipt)

        assert receipt.business_or_personal == "Personal"
        assert questions == []
        assert _question_keys(session, receipt) == []


def test_allowlisted_user_answering_personal_closes_without_business_reason(
    isolated_db,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BUSINESS_PERSONAL_CLARIFICATION_TELEGRAM_IDS", "100004")
    get_settings.cache_clear()

    with Session(isolated_db) as session:
        user = _user(session, telegram_user_id=100004)
        receipt = _complete_telegram_receipt(session, user)
        questions = ensure_receipt_review_questions(session, receipt, user.id)
        bp_question = next(q for q in questions if q.question_key == "business_or_personal")

        created = answer_question(session, bp_question, "Personal")
        session.refresh(receipt)

        assert created == []
        assert receipt.business_or_personal == "Personal"
        assert receipt.business_reason is None
        assert receipt.needs_clarification is False
        assert _question_keys(session, receipt) == ["business_or_personal"]


def test_allowlisted_user_answering_business_continues_business_context_path(
    isolated_db,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BUSINESS_PERSONAL_CLARIFICATION_TELEGRAM_IDS", "100005")
    get_settings.cache_clear()

    with Session(isolated_db) as session:
        user = _user(session, telegram_user_id=100005)
        receipt = _complete_telegram_receipt(session, user)
        questions = ensure_receipt_review_questions(session, receipt, user.id)
        bp_question = next(q for q in questions if q.question_key == "business_or_personal")

        created = answer_question(session, bp_question, "Business")
        session.refresh(receipt)

        assert receipt.business_or_personal == "Business"
        assert [question.question_key for question in created] == ["business_reason"]

        created = answer_question(session, created[0], "Kartonsan dinner")
        session.refresh(receipt)

        assert receipt.business_reason == "Kartonsan dinner"
        assert [question.question_key for question in created] == ["attendees"]


def test_ocr_personal_classification_is_ignored_and_default_user_becomes_business(
    isolated_db,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("BUSINESS_PERSONAL_CLARIFICATION_TELEGRAM_IDS", raising=False)
    get_settings.cache_clear()

    def fake_vision_call(model, images, *args, **kwargs):
        return {
            "date": "2025-11-15",
            "supplier": "YENI TRUVA MARKET",
            "amount": 175,
            "currency": "TRY",
            "business_or_personal": "Personal",
            "receipt_type": "payment_receipt",
        }

    monkeypatch.setattr(model_router, "_vision_call", fake_vision_call)
    image_path = tmp_path / "receipt.jpg"
    image_path.write_bytes(b"\xff\xd8\xff\xd9")

    with Session(isolated_db) as session:
        user = _user(session, telegram_user_id=100006)
        receipt = ReceiptDocument(
            uploader_user_id=user.id,
            source="telegram",
            telegram_chat_id=12345,
            original_file_name="telegram_photo_6.jpg",
            storage_path=str(image_path),
        )
        session.add(receipt)
        session.commit()
        session.refresh(receipt)

        apply_receipt_extraction(session, receipt)
        session.refresh(receipt)
        assert receipt.business_or_personal is None

        ensure_receipt_review_questions(session, receipt, user.id)
        session.refresh(receipt)

        assert receipt.business_or_personal == "Business"
        assert "business_or_personal" not in _question_keys(session, receipt)


def test_ocr_personal_classification_is_ignored_for_allowlisted_user(
    isolated_db,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BUSINESS_PERSONAL_CLARIFICATION_TELEGRAM_IDS", "100007")
    get_settings.cache_clear()

    def fake_vision_call(model, images, *args, **kwargs):
        return {
            "date": "2025-11-15",
            "supplier": "YENI TRUVA MARKET",
            "amount": 175,
            "currency": "TRY",
            "business_or_personal": "Personal",
            "receipt_type": "payment_receipt",
        }

    monkeypatch.setattr(model_router, "_vision_call", fake_vision_call)
    image_path = tmp_path / "receipt.jpg"
    image_path.write_bytes(b"\xff\xd8\xff\xd9")

    with Session(isolated_db) as session:
        user = _user(session, telegram_user_id=100007)
        receipt = ReceiptDocument(
            uploader_user_id=user.id,
            source="telegram",
            telegram_chat_id=12345,
            original_file_name="telegram_photo_7.jpg",
            storage_path=str(image_path),
        )
        session.add(receipt)
        session.commit()
        session.refresh(receipt)

        apply_receipt_extraction(session, receipt)
        ensure_receipt_review_questions(session, receipt, user.id)
        session.refresh(receipt)

        assert receipt.business_or_personal is None
        assert "business_or_personal" in _question_keys(session, receipt)
