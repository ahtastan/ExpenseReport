"""F1.5 date sanity validation before saving OCR output."""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlmodel import Session, select

from app.models import AppUser, ClarificationQuestion, ReceiptDocument, StatementImport
from app.services import model_router
from app.services.clarifications import ensure_receipt_review_questions
from app.services.receipt_extraction import (
    DateSanityContext,
    apply_receipt_extraction,
    validate_receipt_date,
)


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
    path = tmp_path / "telegram_photo_2014.png"
    image = Image.new("RGB", (222, 521), "white")
    image.save(path, format="PNG")
    return path


def test_date_sanity_rejects_old_date_outside_statement_period() -> None:
    context = DateSanityContext(
        statement_import_id=123,
        period_start=date(2025, 11, 1),
        period_end=date(2025, 11, 30),
    )

    result = validate_receipt_date(date(2014, 1, 15), context=context, today=date(2026, 4, 27))

    assert result.accepted is False
    assert result.reason == "outside_statement_period"


def test_date_sanity_rejects_old_date_without_statement_context() -> None:
    result = validate_receipt_date(date(2014, 1, 15), context=None, today=date(2026, 4, 27))

    assert result.accepted is False
    assert result.reason == "before_hard_floor"


def test_date_sanity_rejects_date_after_hard_floor_but_older_than_18_months() -> None:
    result = validate_receipt_date(date(2024, 6, 1), context=None, today=date(2026, 4, 27))

    assert result.accepted is False
    assert result.reason == "older_than_18_months"


def test_date_sanity_accepts_statement_period_date() -> None:
    context = DateSanityContext(
        statement_import_id=123,
        period_start=date(2025, 11, 1),
        period_end=date(2025, 11, 30),
    )

    result = validate_receipt_date(date(2025, 11, 15), context=context, today=date(2026, 4, 27))

    assert result.accepted is True
    assert result.reason is None


def _extract_with_statement_context(
    isolated_db,
    tmp_path: Path,
    monkeypatch,
    responses: list[dict | None],
) -> tuple[ReceiptDocument, set[str], _Recorder]:
    recorder = _Recorder(responses)
    monkeypatch.setattr(model_router, "_vision_call", recorder)

    with Session(isolated_db) as session:
        user = AppUser(telegram_user_id=900155)
        session.add(user)
        session.commit()
        session.refresh(user)

        statement = StatementImport(
            uploader_user_id=user.id,
            source_filename="november.xlsx",
            period_start=date(2025, 11, 1),
            period_end=date(2025, 11, 30),
            row_count=1,
        )
        session.add(statement)
        session.commit()

        receipt = ReceiptDocument(
            uploader_user_id=user.id,
            content_type="photo",
            original_file_name="telegram_photo_2014.jpg",
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


def test_implausible_first_pass_date_retries_and_saves_recovered_date(
    isolated_db,
    tmp_path,
    monkeypatch,
) -> None:
    receipt, question_keys, recorder = _extract_with_statement_context(
        isolated_db,
        tmp_path,
        monkeypatch,
        [
            {
                "date": "2014-01-15",
                "supplier": "YENI DUNYA TUR PET VE PET UR",
                "amount": 175,
                "currency": "TRY",
                "receipt_type": "payment_receipt",
            },
            {"supplier": "YENI DUNYA TUR PET VE PET UR"},
            {"date": "2025-11-15"},
        ],
    )

    assert recorder.prompts == [
        "<default>",
        model_router._VISION_PROMPT_STRICT,
        model_router._VISION_PROMPT_DATE_ONLY,
    ]
    assert receipt.extracted_date == date(2025, 11, 15)
    assert receipt.extracted_local_amount == Decimal("175.0000")
    assert receipt.extracted_currency == "TRY"
    assert receipt.extracted_supplier == "YENI DUNYA TUR PET VE PET UR"
    assert receipt.receipt_type == "payment_receipt"
    assert "receipt_date" not in question_keys


@pytest.mark.parametrize(
    "retry_value",
    ["15-11-2025", "15/11/2025", "15.11.2025", "2025/11/15"],
)
def test_date_retry_normalizes_turkish_pos_date_formats(
    isolated_db,
    tmp_path,
    monkeypatch,
    retry_value: str,
) -> None:
    receipt, question_keys, recorder = _extract_with_statement_context(
        isolated_db,
        tmp_path,
        monkeypatch,
        [
            {
                "date": None,
                "supplier": "YENI DUNYA TUR PET VE PET UR",
                "amount": 175,
                "currency": "TRY",
                "receipt_type": "payment_receipt",
            },
            {"date": retry_value},
        ],
    )

    assert recorder.prompts == ["<default>", model_router._VISION_PROMPT_DATE_ONLY]
    assert receipt.extracted_date == date(2025, 11, 15)
    assert receipt.extracted_local_amount == Decimal("175.0000")
    assert receipt.extracted_currency == "TRY"
    assert receipt.extracted_supplier == "YENI DUNYA TUR PET VE PET UR"
    assert receipt.receipt_type == "payment_receipt"
    assert "receipt_date" not in question_keys


def test_implausible_first_pass_date_saves_local_format_retry_date(
    isolated_db,
    tmp_path,
    monkeypatch,
) -> None:
    receipt, question_keys, recorder = _extract_with_statement_context(
        isolated_db,
        tmp_path,
        monkeypatch,
        [
            {
                "date": "2014-01-15",
                "supplier": "YENI DUNYA TUR PET VE PET UR",
                "amount": 175,
                "currency": "TRY",
                "receipt_type": "payment_receipt",
            },
            {"supplier": "YENI DUNYA TUR PET VE PET UR"},
            {"date": "15-11-2025"},
        ],
    )

    assert recorder.prompts == [
        "<default>",
        model_router._VISION_PROMPT_STRICT,
        model_router._VISION_PROMPT_DATE_ONLY,
    ]
    assert receipt.extracted_date == date(2025, 11, 15)
    assert receipt.extracted_local_amount == Decimal("175.0000")
    assert receipt.extracted_currency == "TRY"
    assert receipt.extracted_supplier == "YENI DUNYA TUR PET VE PET UR"
    assert receipt.receipt_type == "payment_receipt"
    assert "receipt_date" not in question_keys


def test_implausible_first_pass_date_retry_uses_enhanced_image_path(
    isolated_db,
    tmp_path,
    monkeypatch,
) -> None:
    seen_paths: list[str] = []
    real_images_for_path = model_router._vision_images_for_path

    def recording_images_for_path(storage_path):
        seen_paths.append(str(storage_path))
        return real_images_for_path(storage_path)

    monkeypatch.setattr(model_router, "_vision_images_for_path", recording_images_for_path)

    receipt, question_keys, recorder = _extract_with_statement_context(
        isolated_db,
        tmp_path,
        monkeypatch,
        [
            {
                "date": "2014-12-15",
                "supplier": "YENI DUNYA TUR PET VE PET UR",
                "amount": 175,
                "currency": "TRY",
                "receipt_type": "payment_receipt",
            },
            {"supplier": "YENI DUNYA TUR PET VE PET UR"},
            {"date": "15-11-2025"},
        ],
    )

    assert recorder.prompts == [
        "<default>",
        model_router._VISION_PROMPT_STRICT,
        model_router._VISION_PROMPT_DATE_ONLY,
    ]
    assert receipt.extracted_date == date(2025, 11, 15)
    # Non-date fields from the first pass must survive the enhanced retry.
    assert receipt.extracted_local_amount == Decimal("175.0000")
    assert receipt.extracted_currency == "TRY"
    assert receipt.extracted_supplier == "YENI DUNYA TUR PET VE PET UR"
    assert receipt.receipt_type == "payment_receipt"
    assert "receipt_date" not in question_keys
    assert len(seen_paths) == 3
    assert seen_paths[0].endswith("telegram_photo_2014.png")
    assert seen_paths[1] != seen_paths[0]
    assert Path(seen_paths[1]).name.startswith("dcexpense-supplier-retry-")
    assert seen_paths[2] != seen_paths[0]
    assert Path(seen_paths[2]).name.startswith("dcexpense-date-retry-")


def test_missing_first_pass_date_retry_uses_enhanced_image_path(
    isolated_db,
    tmp_path,
    monkeypatch,
) -> None:
    seen_paths: list[str] = []
    real_images_for_path = model_router._vision_images_for_path

    def recording_images_for_path(storage_path):
        seen_paths.append(str(storage_path))
        return real_images_for_path(storage_path)

    monkeypatch.setattr(model_router, "_vision_images_for_path", recording_images_for_path)

    receipt, question_keys, recorder = _extract_with_statement_context(
        isolated_db,
        tmp_path,
        monkeypatch,
        [
            {
                "date": None,
                "supplier": "YENI DUNYA TUR PET VE PET UR",
                "amount": 175,
                "currency": "TRY",
                "receipt_type": "payment_receipt",
            },
            {"date": "15-11-2025"},
        ],
    )

    assert recorder.prompts == ["<default>", model_router._VISION_PROMPT_DATE_ONLY]
    assert receipt.extracted_date == date(2025, 11, 15)
    # Non-date fields from the first pass must survive the enhanced retry.
    assert receipt.extracted_local_amount == Decimal("175.0000")
    assert receipt.extracted_currency == "TRY"
    assert receipt.extracted_supplier == "YENI DUNYA TUR PET VE PET UR"
    assert receipt.receipt_type == "payment_receipt"
    assert "receipt_date" not in question_keys
    assert len(seen_paths) == 2
    assert seen_paths[0].endswith("telegram_photo_2014.png")
    assert seen_paths[1] != seen_paths[0]
    assert Path(seen_paths[1]).name.startswith("dcexpense-date-retry-")


def test_invalid_calendar_retry_date_is_not_saved_and_asks_for_date(
    isolated_db,
    tmp_path,
    monkeypatch,
) -> None:
    receipt, question_keys, recorder = _extract_with_statement_context(
        isolated_db,
        tmp_path,
        monkeypatch,
        [
            {
                "date": "2014-01-15",
                "supplier": "YENI DUNYA TUR PET VE PET UR",
                "amount": 175,
                "currency": "TRY",
            },
            {"supplier": "YENI DUNYA TUR PET VE PET UR"},
            {"date": "31-02-2025"},
        ],
    )

    assert recorder.prompts == [
        "<default>",
        model_router._VISION_PROMPT_STRICT,
        model_router._VISION_PROMPT_DATE_ONLY,
    ]
    assert receipt.extracted_date is None
    assert "receipt_date" in question_keys


def test_implausible_retry_date_is_not_saved_and_asks_for_date(
    isolated_db,
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    caplog.set_level("WARNING", logger="app.services.receipt_extraction")

    receipt, question_keys, recorder = _extract_with_statement_context(
        isolated_db,
        tmp_path,
        monkeypatch,
        [
            {
                "date": "2014-01-15",
                "supplier": "YENI DUNYA TUR PET VE PET UR",
                "amount": 175,
                "currency": "TRY",
            },
            {"supplier": "YENI DUNYA TUR PET VE PET UR"},
            {"date": "2014-01-15"},
        ],
    )

    assert recorder.prompts == [
        "<default>",
        model_router._VISION_PROMPT_STRICT,
        model_router._VISION_PROMPT_DATE_ONLY,
    ]
    assert receipt.extracted_date is None
    assert "receipt_date" in question_keys
    assert "retry_raw_date='2014-01-15'" in caplog.text
    assert "retry_normalized_date=2014-01-15" in caplog.text
    assert "retry_sanity_accepted=False" in caplog.text
    assert "retry_sanity_reason=outside_statement_period" in caplog.text


def test_missing_retry_date_log_is_not_marked_sanity_accepted(
    isolated_db,
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    caplog.set_level("WARNING", logger="app.services.receipt_extraction")

    receipt, question_keys, recorder = _extract_with_statement_context(
        isolated_db,
        tmp_path,
        monkeypatch,
        [
            {
                "date": "2014-12-15",
                "supplier": "YENI DUNYA TUR PET VE PET UR",
                "amount": 175,
                "currency": "TRY",
            },
            {"supplier": "YENI DUNYA TUR PET VE PET UR"},
            {"date": None},
        ],
    )

    assert recorder.prompts == [
        "<default>",
        model_router._VISION_PROMPT_STRICT,
        model_router._VISION_PROMPT_DATE_ONLY,
    ]
    assert receipt.extracted_date is None
    assert "receipt_date" in question_keys
    assert "retry_raw_date=None" in caplog.text
    assert "retry_normalized_date=None" in caplog.text
    assert "retry_sanity_accepted=False" in caplog.text
    assert "retry_sanity_reason=retry_missing_date" in caplog.text


def test_date_only_retry_prompt_calls_out_turkish_pos_receipt_dates() -> None:
    prompt = model_router._VISION_PROMPT_DATE_ONLY

    assert "Turkish POS" in prompt
    assert "TARIH" in prompt
    assert "TARİH" in prompt
    assert "SAAT" in prompt
    assert "FIS NO" in prompt
    assert "FİŞ NO" in prompt
    assert "DD-MM-YYYY" in prompt
    assert "DD/MM/YYYY" in prompt
    assert "DD.MM.YYYY" in prompt
    assert "YYYY/MM/DD" in prompt
    assert "receipt date, not the card transaction date" in prompt
    assert "current date" in prompt
    assert "upload date" in prompt
    assert "Telegram timestamp" in prompt
    assert "implausibly old years" not in prompt
