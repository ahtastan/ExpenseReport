"""Telegram-generated placeholder filenames must not shadow vision supplier.

Regression for the "telegram photo" supplier bug: receipts uploaded via
Telegram that lack a user-supplied file name are saved with a
platform-generated placeholder such as ``telegram_photo_42.jpg``.
Before the fix, ``_parse_merchant`` would strip the digits and return
``"telegram photo"`` as the deterministic merchant name, which then won
over the vision-extracted supplier name because the merge order is
``stored > deterministic > vision``. The fix rejects those placeholder
stems so the vision output is used instead.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from uuid import uuid4

VERIFY_ROOT = Path.cwd() / ".verify_data"
VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{VERIFY_ROOT / f'receipt_tg_placeholder_{uuid4().hex}.db'}"
os.environ["EXPENSE_STORAGE_ROOT"] = str(VERIFY_ROOT)
os.environ.pop("OPENAI_API_KEY", None)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import ReceiptDocument  # noqa: E402
from app.services import model_router  # noqa: E402
from app.services.receipt_extraction import (  # noqa: E402
    extract_receipt_fields,
    _parse_merchant,
)


def _assert_placeholder_rejected() -> None:
    """_parse_merchant should return None for every Telegram placeholder form.

    Covers the shapes emitted by ``services/telegram.py`` when no user file
    name is available: photo, document, and statement with or without the
    numeric message id suffix.
    """
    rejected_names = [
        "telegram_photo_42.jpg",
        "telegram_photo.jpg",
        "telegram_document_17.pdf",
        "telegram_document.pdf",
        "telegram_statement_5.xlsx",
        "telegram-photo-9.jpg",
        "Telegram_Photo_123.png",
    ]
    for name in rejected_names:
        assert _parse_merchant("", name) is None, f"did not reject {name!r}"

    kept_names = [
        ("IST Sey.jpg", "IST Sey"),
        ("Airport_Istanbul_Sey.jpg", "Airport Istanbul Sey"),
        ("onder_supermarket.jpg", "onder supermarket"),
    ]
    for name, expected in kept_names:
        got = _parse_merchant("", name)
        assert got == expected, f"{name!r}: expected {expected!r}, got {got!r}"


def _assert_vision_supplier_wins_over_placeholder() -> None:
    """End-to-end: a receipt with a placeholder filename must land the real
    vision-extracted supplier, not the placeholder stem."""

    original = model_router.vision_extract

    def fake_vision_extract(storage_path: str):
        return model_router.VisionResult(
            fields={
                "date": "2025-08-28",
                "supplier": "Eroglu Grup Akaryakit",
                "amount": 145.0,
                "currency": "TRY",
                "business_or_personal": "Business",
            },
            model=model_router.MINI_MODEL,
            escalated=False,
            notes=["fake vision supplier for placeholder filename"],
        )

    model_router.vision_extract = fake_vision_extract
    try:
        receipt = ReceiptDocument(
            id=99,
            original_file_name="telegram_photo_42.jpg",
            storage_path=str(VERIFY_ROOT / "telegram_photo_42.jpg"),
        )
        result = extract_receipt_fields(receipt)
    finally:
        model_router.vision_extract = original

    assert result.extracted_supplier == "Eroglu Grup Akaryakit", (
        f"expected vision supplier to win, got {result.extracted_supplier!r}"
    )
    assert result.extracted_date == date(2025, 8, 28)
    assert result.extracted_local_amount == 145.0
    assert result.extracted_currency == "TRY"
    assert "supplier" not in result.missing_fields


def test_placeholder_rejected() -> None:
    _assert_placeholder_rejected()


def test_vision_supplier_wins_over_placeholder() -> None:
    _assert_vision_supplier_wins_over_placeholder()


def main() -> None:
    _assert_placeholder_rejected()
    _assert_vision_supplier_wins_over_placeholder()
    print("receipt_extraction_telegram_placeholder_tests=passed")


if __name__ == "__main__":
    main()
