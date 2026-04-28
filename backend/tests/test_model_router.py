"""Tests for the single-tier OCR vision pipeline (post-F1.3 rollback).

The router must:
  - call the full vision model for the first pass;
  - run a supplier/header retry for image receipts with otherwise complete
    first-pass facts, so wrong-but-confident merchant OCR can be corrected
    from an enhanced header crop before saving;
  - retry with the stricter merchant-only prompt when the first-pass
    supplier is missing — the ``UNREADABLE_MERCHANT`` sentinel,
    ``None``, or an empty/whitespace string — since all three shapes
    mean the model couldn't read the merchant masthead;
  - on retry, swap supplier from the retry response while preserving
    first-pass date / amount / currency / receipt_type;
  - run focused date-only and amount-only retries when those fields are
    missing, without overwriting clean first-pass values;
  - return ``None`` when the first-pass call itself produced no
    parseable response (focused retries require a valid first-pass payload).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import model_router  # noqa: E402


def _fake_image(tmpdir: Path) -> Path:
    # The router only reads bytes for base64 encoding; a tiny file suffices.
    path = tmpdir / "receipt.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    return path


def _valid_receipt_image(tmpdir: Path, name: str = "receipt.jpg") -> Path:
    path = tmpdir / name
    image = Image.new("RGB", (222, 521), "white")
    image.save(path, format="JPEG")
    return path


class _Recorder:
    """Stand-in for ``_call_openai`` that records calls and replays queued responses."""

    def __init__(self, responses: list[dict | None]):
        self._responses = list(responses)
        self.calls: list[str] = []
        self.prompts: list[str] = []

    def __call__(self, model, images, *args, **kwargs):  # matches the real signature
        self.calls.append(model)
        prompt = args[0] if args else kwargs.get("prompt")
        self.prompts.append(prompt if prompt is not None else "<default>")
        if not self._responses:
            return None
        return self._responses.pop(0)


def test_clean_first_pass_runs_supplier_header_retry_without_changing_facts(tmp_path, monkeypatch):
    """A clear image receipt still gets a supplier/header retry, but date,
    amount, currency, and receipt_type stay anchored to the first pass."""
    rec = _Recorder([
        {"date": "2026-04-01", "supplier": "Migros", "amount": 42.5,
         "currency": "TRY", "receipt_type": "payment_receipt"},
        {"supplier": "Migros"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    result = model_router.vision_extract(str(_fake_image(tmp_path)))
    assert result is not None
    assert result.escalated is True
    assert rec.calls == [model_router.VISION_MODEL, model_router.VISION_MODEL]
    assert rec.prompts == ["<default>", model_router._VISION_PROMPT_STRICT]
    assert result.fields["date"] == "2026-04-01"
    assert result.fields["amount"] == 42.5
    assert result.fields["currency"] == "TRY"
    assert result.fields["supplier"] == "Migros"
    assert result.fields["receipt_type"] == "payment_receipt"


def test_supplier_header_retry_prefers_yeni_irma_over_wrong_confident_first_pass(
    tmp_path,
    monkeypatch,
):
    rec = _Recorder([
        {"date": "2025-11-15", "supplier": "YENI DUNYA TUR PET VE PET UR",
         "amount": 175, "currency": "TRY", "receipt_type": "payment_receipt"},
        {"supplier": "YENI IRMA TUR PET VE PET UR"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)

    result = model_router.vision_extract(str(_fake_image(tmp_path)))

    assert result is not None
    assert result.escalated is True
    assert rec.prompts == ["<default>", model_router._VISION_PROMPT_STRICT]
    assert result.fields["supplier"] == "YENI IRMA TUR PET VE PET UR"
    assert result.fields["date"] == "2025-11-15"
    assert result.fields["amount"] == 175
    assert result.fields["currency"] == "TRY"
    assert result.fields["receipt_type"] == "payment_receipt"


def test_supplier_header_retry_prefers_turkish_legal_name_over_ocr_confusion(
    tmp_path,
    monkeypatch,
):
    rec = _Recorder([
        {"date": "2025-11-15", "supplier": "İSRAİL KÖSE",
         "amount": 715, "currency": "TRY", "receipt_type": "payment_receipt"},
        {"supplier": "İSMAİL KÖSE PETROL LTD. ŞTİ."},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)

    result = model_router.vision_extract(str(_fake_image(tmp_path)))

    assert result is not None
    assert result.fields["supplier"] == "İSMAİL KÖSE PETROL LTD. ŞTİ."
    assert result.fields["date"] == "2025-11-15"
    assert result.fields["amount"] == 715
    assert result.fields["currency"] == "TRY"


def test_supplier_header_retry_null_preserves_first_pass_supplier(tmp_path, monkeypatch):
    rec = _Recorder([
        {"date": "2025-11-15", "supplier": "YENI DUNYA TUR PET VE PET UR",
         "amount": 175, "currency": "TRY"},
        {"supplier": None},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)

    result = model_router.vision_extract(str(_fake_image(tmp_path)))

    assert result is not None
    assert result.escalated is False
    assert result.fields["supplier"] == "YENI DUNYA TUR PET VE PET UR"


def test_supplier_header_retry_unreadable_preserves_first_pass_supplier(tmp_path, monkeypatch):
    rec = _Recorder([
        {"date": "2025-11-15", "supplier": "YENI DUNYA TUR PET VE PET UR",
         "amount": 175, "currency": "TRY"},
        {"supplier": model_router.UNREADABLE_MERCHANT_SENTINEL},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)

    result = model_router.vision_extract(str(_fake_image(tmp_path)))

    assert result is not None
    assert result.escalated is False
    assert result.fields["supplier"] == "YENI DUNYA TUR PET VE PET UR"


def test_missing_amount_triggers_amount_only_retry(tmp_path, monkeypatch):
    """F1.4: amount absence gets one focused amount/currency retry."""
    rec = _Recorder([
        {"date": "2026-04-01", "supplier": "Migros", "amount": None},
        {"amount": 42.5, "currency": "TRY"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    result = model_router.vision_extract(str(_fake_image(tmp_path)))
    assert result is not None
    assert result.escalated is True
    assert rec.calls == [model_router.VISION_MODEL, model_router.VISION_MODEL]
    assert rec.prompts[1] == model_router._VISION_PROMPT_AMOUNT_ONLY
    assert result.fields["amount"] == 42.5
    assert result.fields["currency"] == "TRY"
    assert result.fields["date"] == "2026-04-01"
    assert result.fields["supplier"] == "Migros"


def test_suspicious_small_try_amount_uses_amount_retry_when_larger_total_found(
    tmp_path,
    monkeypatch,
):
    rec = _Recorder([
        {"date": "2025-11-15", "supplier": "45BUSINESSHOTEL", "amount": 680,
         "currency": "TRY", "receipt_type": "payment_receipt"},
        {"supplier": "45BUSINESSHOTEL"},
        {"amount": 15680, "currency": "TRY"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)

    result = model_router.vision_extract(str(_fake_image(tmp_path)))

    assert result is not None
    assert rec.prompts == [
        "<default>",
        model_router._VISION_PROMPT_STRICT,
        model_router._VISION_PROMPT_AMOUNT_ONLY,
    ]
    assert result.fields["amount"] == 15680
    assert result.fields["currency"] == "TRY"
    assert result.fields["date"] == "2025-11-15"
    assert result.fields["supplier"] == "45BUSINESSHOTEL"
    assert result.fields["receipt_type"] == "payment_receipt"


def test_first_pass_amount_text_overrides_locale_damaged_numeric_amount(tmp_path, monkeypatch):
    rec = _Recorder([
        {
            "date": "2025-11-15",
            "supplier": "45BUSINESSHOTEL",
            "amount_text": "15.680,00 TL",
            "amount": 15.68,
            "currency": "TRY",
            "receipt_type": "payment_receipt",
        },
        {"supplier": "45BUSINESSHOTEL"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)

    result = model_router.vision_extract(str(_fake_image(tmp_path)))

    assert result is not None
    assert result.fields["amount"] == 15680
    assert result.fields["currency"] == "TRY"
    assert result.fields["amount_text"] == "15.680,00 TL"


def test_first_pass_clean_amount_text_ignores_tax_only_amount_label(tmp_path, monkeypatch):
    rec = _Recorder([
        {
            "date": "2025-11-15",
            "supplier": "45BUSINESSHOTEL",
            "amount_text": "15.680,00 TL",
            "amount_label": "KDV",
            "amount": 15.68,
            "currency": "TRY",
            "receipt_type": "payment_receipt",
        },
        {"supplier": "45BUSINESSHOTEL"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)

    result = model_router.vision_extract(str(_fake_image(tmp_path)))

    assert result is not None
    assert result.fields["amount"] == 15680
    assert result.fields["currency"] == "TRY"


def test_first_pass_amount_text_handles_us_grouped_total(tmp_path, monkeypatch):
    rec = _Recorder([
        {
            "date": "2025-11-15",
            "supplier": "45BUSINESSHOTEL",
            "amount_text": "15,680.00 TL",
            "amount": 15.68,
            "currency": "TRY",
            "receipt_type": "payment_receipt",
        },
        {"supplier": "45BUSINESSHOTEL"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)

    result = model_router.vision_extract(str(_fake_image(tmp_path)))

    assert result is not None
    assert result.fields["amount"] == 15680
    assert result.fields["currency"] == "TRY"


def test_first_pass_amount_text_handles_space_and_plain_turkish_totals(tmp_path, monkeypatch):
    for raw_text in ("15 680,00 TL", "15680,00 TL"):
        rec = _Recorder([
            {
                "date": "2025-11-15",
                "supplier": "45BUSINESSHOTEL",
                "amount_text": raw_text,
                "amount": 15.68,
                "currency": "TRY",
                "receipt_type": "payment_receipt",
            },
            {"supplier": "45BUSINESSHOTEL"},
        ])
        monkeypatch.setattr(model_router, "_vision_call", rec)

        result = model_router.vision_extract(str(_fake_image(tmp_path)))

        assert result is not None
        assert result.fields["amount"] == 15680
        assert result.fields["currency"] == "TRY"


@pytest.mark.parametrize(
    "raw_text",
    [
        "TOPLAM (KDV DAHIL) 15.680,00 TL",
        "GENEL TOPLAM KDV DAHIL 15.680,00 TL",
        "ÖDENECEK TUTAR (KDV DAHIL) 15.680,00 TL",
    ],
)
def test_first_pass_amount_text_accepts_kdv_dahil_total_labels(tmp_path, monkeypatch, raw_text):
    rec = _Recorder([
        {
            "date": "2025-11-15",
            "supplier": "Restaurant",
            "amount_text": raw_text,
            "amount": 15.68,
            "currency": "TRY",
            "receipt_type": "payment_receipt",
        },
        {"supplier": "Restaurant"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)

    result = model_router.vision_extract(str(_fake_image(tmp_path)))

    assert result is not None
    assert result.fields["amount"] == 15680
    assert result.fields["currency"] == "TRY"


def test_amount_text_keeps_small_turkish_amounts_correct(tmp_path, monkeypatch):
    for raw_text, expected in (("175,00 TL", 175), ("715,00 TL", 715)):
        rec = _Recorder([
            {
                "date": "2025-11-15",
                "supplier": "Restaurant",
                "amount_text": raw_text,
                "amount": expected,
                "currency": "TRY",
                "receipt_type": "payment_receipt",
            },
            {"supplier": "Restaurant"},
            {"amount": None, "currency": "TRY"},
        ])
        monkeypatch.setattr(model_router, "_vision_call", rec)

        result = model_router.vision_extract(str(_fake_image(tmp_path)))

        assert result is not None
        assert result.fields["amount"] == expected
        assert result.fields["currency"] == "TRY"


def test_amount_retry_amount_text_overrides_locale_damaged_retry_number(tmp_path, monkeypatch):
    rec = _Recorder([
        {"date": "2025-11-15", "supplier": "45BUSINESSHOTEL", "amount": 580,
         "currency": "TRY", "receipt_type": "payment_receipt"},
        {"supplier": "45BUSINESSHOTEL"},
        {"amount_text": "15.680,00 TL", "amount": 15.68, "currency": "TRY"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)

    result = model_router.vision_extract(str(_fake_image(tmp_path)))

    assert result is not None
    assert result.fields["amount"] == 15680
    assert result.fields["currency"] == "TRY"


def test_amount_text_invalid_falls_back_to_numeric_amount(tmp_path, monkeypatch):
    rec = _Recorder([
        {
            "date": "2025-11-15",
            "supplier": "Restaurant",
            "amount_text": "TOTAL UNREADABLE",
            "amount": 715,
            "currency": "TRY",
            "receipt_type": "payment_receipt",
        },
        {"supplier": "Restaurant"},
        {"amount": None, "currency": "TRY"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)

    result = model_router.vision_extract(str(_fake_image(tmp_path)))

    assert result is not None
    assert result.fields["amount"] == 715
    assert result.fields["currency"] == "TRY"


def test_kdv_only_amount_text_does_not_use_same_numeric_value_as_total(tmp_path, monkeypatch):
    rec = _Recorder([
        {
            "date": "2025-11-15",
            "supplier": "Hotel",
            "amount_text": "KDV TOPLAM 1.568,00 TL",
            "amount": 1568,
            "currency": "TRY",
            "receipt_type": "payment_receipt",
        },
        {"amount": None, "currency": "TRY"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)

    result = model_router.vision_extract(str(_fake_image(tmp_path)))

    assert result is not None
    assert result.fields["amount"] is None
    assert result.fields["currency"] == "TRY"


@pytest.mark.parametrize("raw_text", ["KDV 62,85 TL", "TOPKDV 62,85 TL"])
def test_tax_only_amount_text_does_not_use_same_numeric_value_as_total(tmp_path, monkeypatch, raw_text):
    rec = _Recorder([
        {
            "date": "2025-11-15",
            "supplier": "Restaurant",
            "amount_text": raw_text,
            "amount": 62.85,
            "currency": "TRY",
            "receipt_type": "payment_receipt",
        },
        {"amount": None, "currency": "TRY"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)

    result = model_router.vision_extract(str(_fake_image(tmp_path)))

    assert result is not None
    assert result.fields["amount"] is None
    assert result.fields["currency"] == "TRY"


def test_tax_label_with_small_clean_amount_text_blocks_numeric_fallback(tmp_path, monkeypatch):
    rec = _Recorder([
        {
            "date": "2025-11-15",
            "supplier": "Restaurant",
            "amount_text": "62,85 TL",
            "amount_label": "KDV",
            "amount": 62.85,
            "currency": "TRY",
            "receipt_type": "payment_receipt",
        },
        {"amount": None, "currency": "TRY"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)

    result = model_router.vision_extract(str(_fake_image(tmp_path)))

    assert result is not None
    assert result.fields["amount"] is None
    assert result.fields["currency"] == "TRY"


def test_missing_amount_retry_completion_logs_at_info(tmp_path, monkeypatch, caplog):
    rec = _Recorder([
        {"date": "2026-04-01", "supplier": "Migros", "amount": None, "currency": "TRY"},
        {"amount": 42.5, "currency": "TRY"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    caplog.set_level(logging.INFO, logger="app.services.model_router")

    result = model_router.vision_extract(str(_valid_receipt_image(tmp_path)))

    assert result is not None
    amount_retry_records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("Amount total retry completed")
    ]
    assert amount_retry_records
    assert all(record.levelno == logging.INFO for record in amount_retry_records)


def test_suspicious_amount_retry_completion_logs_warning(tmp_path, monkeypatch, caplog):
    rec = _Recorder([
        {"date": "2025-11-15", "supplier": "45BUSINESSHOTEL", "amount": 680,
         "currency": "TRY", "receipt_type": "payment_receipt"},
        {"supplier": "45BUSINESSHOTEL"},
        {"amount": 15680, "currency": "TRY"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    caplog.set_level(logging.INFO, logger="app.services.model_router")

    result = model_router.vision_extract(str(_valid_receipt_image(tmp_path)))

    assert result is not None
    amount_retry_records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("Amount total retry completed")
    ]
    assert amount_retry_records
    assert any(record.levelno == logging.WARNING for record in amount_retry_records)


def test_suspicious_amount_retry_null_preserves_first_pass_amount(tmp_path, monkeypatch):
    rec = _Recorder([
        {"date": "2025-11-15", "supplier": "ISMAIL KOSE PETROL", "amount": 715,
         "currency": "TRY", "receipt_type": "payment_receipt"},
        {"supplier": "ISMAIL KOSE PETROL"},
        {"amount": None, "currency": "TRY"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)

    result = model_router.vision_extract(str(_fake_image(tmp_path)))

    assert result is not None
    assert result.fields["amount"] == 715
    assert result.fields["currency"] == "TRY"
    assert result.fields["date"] == "2025-11-15"
    assert result.fields["supplier"] == "ISMAIL KOSE PETROL"


def test_suspicious_amount_retry_requires_truncated_suffix_pattern(tmp_path, monkeypatch):
    rec = _Recorder([
        {"date": "2025-11-15", "supplier": "ISMAIL KOSE PETROL", "amount": 715,
         "currency": "TRY", "receipt_type": "payment_receipt"},
        {"supplier": "ISMAIL KOSE PETROL"},
        {"amount": 9999, "currency": "TRY"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)

    result = model_router.vision_extract(str(_fake_image(tmp_path)))

    assert result is not None
    assert result.fields["amount"] == 715
    assert result.fields["currency"] == "TRY"


def test_suspicious_amount_retry_requires_large_multiplier(tmp_path, monkeypatch):
    rec = _Recorder([
        {"date": "2025-11-15", "supplier": "ISMAIL KOSE PETROL", "amount": 715,
         "currency": "TRY", "receipt_type": "payment_receipt"},
        {"supplier": "ISMAIL KOSE PETROL"},
        {"amount": 1715, "currency": "TRY"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)

    result = model_router.vision_extract(str(_fake_image(tmp_path)))

    assert result is not None
    assert result.fields["amount"] == 715
    assert result.fields["currency"] == "TRY"


def test_missing_date_triggers_date_only_retry(tmp_path, monkeypatch):
    """F1.4: date absence gets one focused date retry."""
    rec = _Recorder([
        {"date": None, "supplier": "Migros", "amount": 42.5, "currency": "TRY"},
        {"date": "2026-04-01"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    result = model_router.vision_extract(str(_fake_image(tmp_path)))
    assert result is not None
    assert result.escalated is True
    assert rec.calls == [model_router.VISION_MODEL, model_router.VISION_MODEL]
    assert rec.prompts[1] == model_router._VISION_PROMPT_DATE_ONLY
    assert result.fields["date"] == "2026-04-01"
    assert result.fields["amount"] == 42.5
    assert result.fields["currency"] == "TRY"
    assert result.fields["supplier"] == "Migros"


def test_missing_date_retry_uses_enhanced_image_path(tmp_path, monkeypatch):
    """F1.7: date-only retry gets a crop/upscale image, not the original."""
    original = _valid_receipt_image(tmp_path)
    enhanced = _valid_receipt_image(tmp_path, "enhanced.png")
    seen_paths: list[str] = []

    monkeypatch.setattr(
        model_router,
        "_create_enhanced_date_retry_image",
        lambda storage_path: model_router._PreparedDateRetryImage(
            path=enhanced,
            notes=["enhanced date retry image created for test"],
        ),
    )

    def fake_images_for_path(storage_path):
        seen_paths.append(str(storage_path))
        return [("image/png", Path(storage_path).name)], []

    rec = _Recorder([
        {"date": None, "supplier": "Migros", "amount": 42.5, "currency": "TRY"},
        {"date": "2026-04-01"},
    ])
    monkeypatch.setattr(model_router, "_vision_images_for_path", fake_images_for_path)
    monkeypatch.setattr(model_router, "_vision_call", rec)

    result = model_router.vision_extract(str(original))

    assert result is not None
    assert seen_paths == [str(original), str(enhanced)]
    assert result.fields["date"] == "2026-04-01"


def test_valid_first_pass_date_does_not_prepare_enhanced_retry(tmp_path, monkeypatch):
    original = _valid_receipt_image(tmp_path)
    rec = _Recorder([
        {"date": "2026-04-01", "supplier": "Migros", "amount": 42.5, "currency": "TRY"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)

    def fail_if_called(storage_path):
        raise AssertionError("date retry enhancement should not run when first-pass date is valid")

    monkeypatch.setattr(model_router, "_create_enhanced_date_retry_image", fail_if_called)

    result = model_router.vision_extract(str(original))

    assert result is not None
    assert result.escalated is False
    assert result.fields["date"] == "2026-04-01"


def test_date_retry_preprocessing_creates_larger_contact_sheet(tmp_path):
    original = _valid_receipt_image(tmp_path)
    original_bytes = original.read_bytes()

    prepared = model_router._create_enhanced_date_retry_image(str(original))

    assert prepared is not None
    try:
        assert prepared.path.exists()
        with Image.open(original) as source, Image.open(prepared.path) as enhanced:
            assert enhanced.width > source.width
            assert enhanced.height > source.height
        assert original.read_bytes() == original_bytes
        assert any("top_40" in note for note in prepared.notes)
        assert any("middle_40" in note for note in prepared.notes)
        assert any("full" in note for note in prepared.notes)
    finally:
        prepared.path.unlink(missing_ok=True)


def test_date_retry_preprocessing_failure_falls_back_to_original(tmp_path, monkeypatch):
    original = tmp_path / "corrupt.jpg"
    original.write_bytes(b"not an image")
    seen_paths: list[str] = []

    def fake_images_for_path(storage_path):
        seen_paths.append(str(storage_path))
        return [("image/jpeg", "encoded-original")], []

    rec = _Recorder([{"date": "2026-04-01"}])
    monkeypatch.setattr(model_router, "_vision_images_for_path", fake_images_for_path)
    monkeypatch.setattr(model_router, "_vision_call", rec)

    result = model_router.vision_retry_date(str(original))

    assert result is not None
    assert seen_paths == [str(original)]
    assert result.fields["date"] == "2026-04-01"


def test_supplier_retry_preprocessing_creates_larger_header_contact_sheet(tmp_path):
    original = _valid_receipt_image(tmp_path)
    original_bytes = original.read_bytes()

    prepared = model_router._create_enhanced_supplier_retry_image(str(original))

    assert prepared is not None
    try:
        assert prepared.path.exists()
        with Image.open(original) as source, Image.open(prepared.path) as enhanced:
            assert enhanced.width > source.width
            assert enhanced.height > source.height
        assert original.read_bytes() == original_bytes
        assert any("header_25" in note for note in prepared.notes)
        assert any("top_50" in note for note in prepared.notes)
        assert prepared.metadata["enhanced_used"] is True
    finally:
        prepared.path.unlink(missing_ok=True)


def test_supplier_retry_temp_enhanced_image_is_cleaned_up(tmp_path, monkeypatch):
    original = _valid_receipt_image(tmp_path)
    seen_paths: list[str] = []

    def fake_images_for_path(storage_path):
        seen_paths.append(str(storage_path))
        return [("image/png", Path(storage_path).name)], []

    rec = _Recorder([
        {"date": "2025-11-15", "supplier": "YENI DUNYA TUR PET VE PET UR",
         "amount": 175, "currency": "TRY"},
        {"supplier": "YENI IRMA TUR PET VE PET UR"},
    ])
    monkeypatch.setattr(model_router, "_vision_images_for_path", fake_images_for_path)
    monkeypatch.setattr(model_router, "_vision_call", rec)

    result = model_router.vision_extract(str(original))

    assert result is not None
    assert result.fields["supplier"] == "YENI IRMA TUR PET VE PET UR"
    assert len(seen_paths) == 2
    assert Path(seen_paths[1]).name.startswith("dcexpense-supplier-retry-")
    assert not Path(seen_paths[1]).exists()


def test_missing_date_does_not_trigger_amount_or_supplier_retry(tmp_path, monkeypatch):
    rec = _Recorder([
        {"date": None, "supplier": "Migros", "amount": 42.5, "currency": "TRY"},
        {"date": "2026-04-01"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    result = model_router.vision_extract(str(_fake_image(tmp_path)))
    assert result is not None
    assert rec.calls == [model_router.VISION_MODEL, model_router.VISION_MODEL]
    assert rec.prompts == ["<default>", model_router._VISION_PROMPT_DATE_ONLY]


def test_missing_amount_does_not_trigger_date_or_supplier_retry(tmp_path, monkeypatch):
    rec = _Recorder([
        {"date": "2026-04-01", "supplier": "Migros", "amount": None, "currency": "TRY"},
        {"amount": 42.5, "currency": "TRY"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    result = model_router.vision_extract(str(_fake_image(tmp_path)))
    assert result is not None
    assert rec.calls == [model_router.VISION_MODEL, model_router.VISION_MODEL]
    assert rec.prompts == ["<default>", model_router._VISION_PROMPT_AMOUNT_ONLY]


def test_missing_currency_retries_without_overwriting_first_pass_amount(tmp_path, monkeypatch):
    rec = _Recorder([
        {"date": "2026-04-01", "supplier": "Migros", "amount": 42.5, "currency": None},
        {"amount": 9999.99, "currency": "TRY", "date": "1999-01-01", "supplier": "Wrong"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    result = model_router.vision_extract(str(_fake_image(tmp_path)))
    assert result is not None
    assert result.escalated is True
    assert rec.calls == [model_router.VISION_MODEL, model_router.VISION_MODEL]
    assert rec.prompts[1] == model_router._VISION_PROMPT_AMOUNT_ONLY
    assert result.fields["date"] == "2026-04-01"
    assert result.fields["supplier"] == "Migros"
    assert result.fields["amount"] == 42.5
    assert result.fields["currency"] == "TRY"


def test_focused_retries_only_fill_missing_fields(tmp_path, monkeypatch):
    rec = _Recorder([
        {"date": None, "supplier": None, "amount": None, "currency": None,
         "receipt_type": "payment_receipt"},
        {"supplier": "Migros", "date": "1999-01-01", "amount": 9999.99},
        {"date": "2026-04-01", "supplier": "Wrong", "amount": 9999.99},
        {"amount": 42.5, "currency": "TRY", "date": "1999-01-01", "supplier": "Wrong"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    result = model_router.vision_extract(str(_fake_image(tmp_path)))
    assert result is not None
    assert result.escalated is True
    assert rec.prompts == [
        "<default>",
        model_router._VISION_PROMPT_STRICT,
        model_router._VISION_PROMPT_DATE_ONLY,
        model_router._VISION_PROMPT_AMOUNT_ONLY,
    ]
    assert result.fields["supplier"] == "Migros"
    assert result.fields["date"] == "2026-04-01"
    assert result.fields["amount"] == 42.5
    assert result.fields["currency"] == "TRY"
    assert result.fields["receipt_type"] == "payment_receipt"


def test_failed_focused_retries_keep_first_pass_fields(tmp_path, monkeypatch):
    rec = _Recorder([
        {"date": None, "supplier": "Migros", "amount": 42.5, "currency": "TRY"},
        {"date": None, "amount": 9999.99, "supplier": "Wrong"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    result = model_router.vision_extract(str(_fake_image(tmp_path)))
    assert result is not None
    assert result.escalated is False
    assert rec.calls == [model_router.VISION_MODEL, model_router.VISION_MODEL]
    assert rec.prompts == ["<default>", model_router._VISION_PROMPT_DATE_ONLY]
    assert result.fields["date"] is None
    assert result.fields["supplier"] == "Migros"
    assert result.fields["amount"] == 42.5
    assert result.fields["currency"] == "TRY"


def test_null_supplier_triggers_merchant_only_retry(tmp_path, monkeypatch):
    """A null supplier means the model couldn't read the merchant — the
    same condition the explicit sentinel signals. F1.3 patch: retry on
    null supplier as well as on the sentinel. The retry is merchant-only
    and preserves first-pass date / amount / currency, so it cannot
    blank good fields — making it safe to fire on the broader
    "supplier missing" signal."""
    rec = _Recorder([
        {"date": "2026-04-01", "supplier": None, "amount": 42.5,
         "currency": "TRY", "receipt_type": "payment_receipt"},
        {"supplier": "Migros"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    result = model_router.vision_extract(str(_fake_image(tmp_path)))
    assert result is not None
    assert result.escalated is True
    assert rec.calls == [model_router.VISION_MODEL, model_router.VISION_MODEL]
    # Supplier comes from the retry; date/amount/currency/receipt_type
    # all preserved from the first pass.
    assert result.fields["supplier"] == "Migros"
    assert result.fields["date"] == "2026-04-01"
    assert result.fields["amount"] == 42.5
    assert result.fields["currency"] == "TRY"
    assert result.fields["receipt_type"] == "payment_receipt"


def test_empty_string_supplier_triggers_merchant_only_retry(tmp_path, monkeypatch):
    """An empty (or whitespace-only) supplier string is the same kind of
    "couldn't read the masthead" signal as null. F1.3 patch: trigger the
    merchant-only retry. Whitespace-only strings are tested too because
    a model that emits a literal space character is functionally
    identical to one that emits nothing."""
    for empty_supplier in ("", "   ", "\t"):
        rec = _Recorder([
            {"date": "2026-04-01", "supplier": empty_supplier, "amount": 42.5,
             "currency": "TRY"},
            {"supplier": "Migros"},
        ])
        monkeypatch.setattr(model_router, "_vision_call", rec)
        result = model_router.vision_extract(str(_fake_image(tmp_path)))
        assert result is not None, f"empty supplier {empty_supplier!r} returned None"
        assert result.escalated is True, (
            f"empty supplier {empty_supplier!r} did not trigger retry"
        )
        assert rec.calls == [model_router.VISION_MODEL, model_router.VISION_MODEL]
        assert result.fields["supplier"] == "Migros"
        # First-pass date/amount/currency preserved across retry.
        assert result.fields["date"] == "2026-04-01"
        assert result.fields["amount"] == 42.5
        assert result.fields["currency"] == "TRY"


def test_first_pass_unavailable_returns_none_without_retry(tmp_path, monkeypatch):
    """If the first call returns ``None`` (no API key, parse failure,
    transient error), the router must surface ``None`` rather than
    burning a second LLM call. The merchant-only retry would not help
    and would just double the latency penalty for an already-failed
    extraction."""
    rec = _Recorder([None])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    result = model_router.vision_extract(str(_fake_image(tmp_path)))
    assert result is None
    assert rec.calls == [model_router.VISION_MODEL]


def test_unsupported_file_extension_makes_no_model_calls(tmp_path, monkeypatch):
    rec = _Recorder([])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    unsupported = tmp_path / "receipt.txt"
    unsupported.write_text("not an image")
    result = model_router.vision_extract(str(unsupported))
    assert result is None
    assert rec.calls == []


@pytest.mark.parametrize(
    "width,height",
    [(1, 1), (222, 521), (2400, 3000), (5000, 5000)],
)
def test_scale_for_date_retry_crop_respects_bounds(width: int, height: int) -> None:
    """The scale factor must always be in [1, 4]. The helper either fits
    the upscaled crop within the 2400-largest-side cap or, for inputs
    already at/above the cap, returns scale=1 (no further upscale, no
    downscale)."""
    scale = model_router._scale_for_date_retry_crop(width, height)
    assert 1 <= scale <= 4
    largest_side = max(width, height)
    # Cap is honored: either the upscaled side fits within 2400, or the
    # input was already large enough that the helper backs off to scale=1.
    assert scale * largest_side <= max(2400, largest_side)


def test_scale_for_date_retry_crop_upscales_low_res_meaningfully() -> None:
    """The actual prod failure mode (a 222x521 Telegram thumbnail) must
    receive a non-trivial upscale so the date retry sees readable pixels."""
    scale = model_router._scale_for_date_retry_crop(222, 521)
    assert scale >= 2, f"low-res 222x521 must be upscaled at least 2x, got scale={scale}"
