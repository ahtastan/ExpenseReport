"""Document receipts must prefer vision supplier over filename-parsed supplier.

Regression for B9: the merge order for supplier was ``stored > deterministic
> vision`` for every receipt. For documents (PDFs, XLSX attachments, etc.)
the filename is typically an upload ID, booking reference, or customer name
— not a merchant name — so vision must win over the filename-parsed value.
Photos still use the original order because photo filenames and captions
carry real merchant signal.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from uuid import uuid4

VERIFY_ROOT = Path.cwd() / ".verify_data"
VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{VERIFY_ROOT / f'receipt_doc_supplier_{uuid4().hex}.db'}"
os.environ["EXPENSE_STORAGE_ROOT"] = str(VERIFY_ROOT)
os.environ.pop("OPENAI_API_KEY", None)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import ReceiptDocument  # noqa: E402
from app.services import model_router  # noqa: E402
from app.services.receipt_extraction import extract_receipt_fields  # noqa: E402


def _with_fake_vision(fields: dict):
    original = model_router.vision_extract

    def fake(storage_path: str):
        return model_router.VisionResult(
            fields=fields,
            model=model_router.MINI_MODEL,
            escalated=False,
            notes=["fake vision for document-supplier test"],
        )

    model_router.vision_extract = fake
    return original


def test_document_receipt_prefers_vision_supplier_over_filename():
    pdf_path = VERIFY_ROOT / f"LAS2025000004589_CUSTOMER_NAME_INC_{uuid4().hex}.pdf"
    pdf_path.write_bytes(b"")  # vision is mocked; no bytes read

    original = _with_fake_vision(
        {
            "date": "2025-08-29",
            "supplier": "Real Hotel Name",
            "amount": 3500.0,
            "currency": "TRY",
        }
    )
    try:
        receipt = ReceiptDocument(
            id=101,
            content_type="document",
            original_file_name="LAS2025000004589_CUSTOMER_NAME_INC.pdf",
            storage_path=str(pdf_path),
        )
        result = extract_receipt_fields(receipt)
    finally:
        model_router.vision_extract = original

    assert result.extracted_supplier == "Real Hotel Name", (
        f"vision supplier must win for documents, got {result.extracted_supplier!r}"
    )
    # Regression for B4 proper: the other fields the vision call returned
    # must still land as before (we only reordered supplier).
    assert result.extracted_date == date(2025, 8, 29)
    assert result.extracted_local_amount == 3500.0
    assert result.extracted_currency == "TRY"
    assert "supplier" not in result.missing_fields


def test_photo_receipt_still_prefers_deterministic_supplier_over_vision():
    jpg_path = VERIFY_ROOT / f"onder_supermarket_{uuid4().hex}.jpg"
    jpg_path.write_bytes(b"")

    original = _with_fake_vision(
        {
            "date": "2025-08-29",
            "supplier": "Different Supplier From Vision",
            "amount": 42.5,
            "currency": "TRY",
        }
    )
    try:
        receipt = ReceiptDocument(
            id=102,
            content_type="photo",
            original_file_name="onder_supermarket.jpg",
            storage_path=str(jpg_path),
        )
        result = extract_receipt_fields(receipt)
    finally:
        model_router.vision_extract = original

    assert result.extracted_supplier == "onder supermarket", (
        "photo path must keep deterministic-first order; "
        f"got {result.extracted_supplier!r}"
    )
    # Vision still fills date/amount/currency that deterministic couldn't.
    assert result.extracted_date == date(2025, 8, 29)
    assert result.extracted_local_amount == 42.5
    assert result.extracted_currency == "TRY"
