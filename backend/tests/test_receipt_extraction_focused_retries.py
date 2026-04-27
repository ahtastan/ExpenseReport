"""F1.4 focused OCR retries must run before clarification questions."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlmodel import Session, select

from app.models import AppUser, ClarificationQuestion, ReceiptDocument
from app.services import model_router
from app.services.clarifications import ensure_receipt_review_questions
from app.services.receipt_extraction import apply_receipt_extraction


class _Recorder:
    def __init__(self, responses: list[dict | None]):
        self._responses = list(responses)
        self.prompts: list[str] = []

    def __call__(self, model, images, *args, **kwargs):
        prompt = args[0] if args else kwargs.get("prompt")
        self.prompts.append(prompt if prompt is not None else "<default>")
        if not self._responses:
            return None
        return self._responses.pop(0)


def _fake_image(tmp_path: Path) -> Path:
    path = tmp_path / "telegram_photo_77.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    return path


def _extract_and_question_keys(
    isolated_db,
    tmp_path: Path,
    monkeypatch,
    responses: list[dict | None],
) -> tuple[ReceiptDocument, set[str], _Recorder]:
    recorder = _Recorder(responses)
    monkeypatch.setattr(model_router, "_vision_call", recorder)

    with Session(isolated_db) as session:
        user = AppUser(telegram_user_id=900077)
        session.add(user)
        session.commit()
        session.refresh(user)

        receipt = ReceiptDocument(
            uploader_user_id=user.id,
            content_type="photo",
            original_file_name="telegram_photo_77.jpg",
            storage_path=str(_fake_image(tmp_path)),
        )
        session.add(receipt)
        session.commit()
        session.refresh(receipt)

        apply_receipt_extraction(session, receipt)
        ensure_receipt_review_questions(session, receipt, user.id)
        session.refresh(receipt)
        questions = session.exec(select(ClarificationQuestion)).all()
        return receipt, {question.question_key for question in questions}, recorder


def test_date_retry_prevents_date_clarification(isolated_db, tmp_path, monkeypatch):
    receipt, question_keys, recorder = _extract_and_question_keys(
        isolated_db,
        tmp_path,
        monkeypatch,
        [
            {"date": None, "supplier": "Yeni Truva Market", "amount": 175, "currency": "TRY"},
            {"date": "2025-11-15"},
        ],
    )

    assert recorder.prompts == ["<default>", model_router._VISION_PROMPT_DATE_ONLY]
    assert receipt.extracted_date == date(2025, 11, 15)
    assert "receipt_date" not in question_keys


def test_amount_retry_prevents_amount_clarification(isolated_db, tmp_path, monkeypatch):
    receipt, question_keys, recorder = _extract_and_question_keys(
        isolated_db,
        tmp_path,
        monkeypatch,
        [
            {"date": "2025-11-15", "supplier": "Yeni Truva Market", "amount": None, "currency": None},
            {"amount": 175, "currency": "TRY"},
        ],
    )

    assert recorder.prompts == ["<default>", model_router._VISION_PROMPT_AMOUNT_ONLY]
    assert receipt.extracted_local_amount == Decimal("175.0000")
    assert receipt.extracted_currency == "TRY"
    assert "local_amount" not in question_keys


def test_supplier_retry_prevents_supplier_clarification(isolated_db, tmp_path, monkeypatch):
    receipt, question_keys, recorder = _extract_and_question_keys(
        isolated_db,
        tmp_path,
        monkeypatch,
        [
            {"date": "2025-11-15", "supplier": None, "amount": 175, "currency": "TRY"},
            {"supplier": "Yeni Truva Market"},
        ],
    )

    assert recorder.prompts == ["<default>", model_router._VISION_PROMPT_STRICT]
    assert receipt.extracted_supplier == "Yeni Truva Market"
    assert "supplier" not in question_keys
