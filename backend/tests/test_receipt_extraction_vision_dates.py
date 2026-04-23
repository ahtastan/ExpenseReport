"""Regression tests for vision-returned receipt date formats."""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from uuid import uuid4

VERIFY_ROOT = Path.cwd() / ".verify_data"
VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{VERIFY_ROOT / f'receipt_vision_date_{uuid4().hex}.db'}"
os.environ["EXPENSE_STORAGE_ROOT"] = str(VERIFY_ROOT)
os.environ.pop("OPENAI_API_KEY", None)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import ReceiptDocument  # noqa: E402
from app.services import model_router  # noqa: E402
from app.services.receipt_extraction import extract_receipt_fields  # noqa: E402


def main() -> None:
    original = model_router.vision_extract

    def fake_vision_extract(storage_path: str):
        return model_router.VisionResult(
            fields={
                "date": "04/09/2025",
                "supplier": "Onder Supermarket",
                "amount": 369.45,
                "currency": "TRY",
                "business_or_personal": "Personal",
            },
            model=model_router.MINI_MODEL,
            escalated=False,
            notes=["fake Turkish receipt slash date"],
        )

    model_router.vision_extract = fake_vision_extract
    try:
        receipt = ReceiptDocument(
            id=1,
            original_file_name="04-09-Onder.jpg",
            storage_path=str(VERIFY_ROOT / "04-09-Onder.jpg"),
        )
        result = extract_receipt_fields(receipt)
    finally:
        model_router.vision_extract = original

    assert result.extracted_date == date(2025, 9, 4)
    assert result.extracted_local_amount == 369.45
    assert "receipt_date" not in result.missing_fields
    print("receipt_extraction_vision_date_tests=passed")


if __name__ == "__main__":
    main()
