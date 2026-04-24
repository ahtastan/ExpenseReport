"""End-to-end OCR test against real OpenAI (opt-in, costs real money).

Run manually with ``RUN_VISION_E2E=1`` to verify that a multi-page vision
call against a rasterized PDF actually returns the expected fields.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import model_router  # noqa: E402


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_VISION_E2E") != "1",
    reason="Set RUN_VISION_E2E=1 to run real-OpenAI end-to-end tests",
)


def test_vision_extract_synthetic_pdf_end_to_end():
    fixture_path = Path(__file__).parent / "fixtures" / "minimal_receipt.pdf"
    result = model_router.vision_extract(str(fixture_path))
    assert result is not None
    supplier = result.fields.get("supplier")
    assert supplier is not None
    # Loose match — models phrase supplier differently.
    assert "HOTEL" in supplier.upper() or "TEST" in supplier.upper()
    amount = result.fields.get("amount")
    local_amount = result.fields.get("local_amount")
    assert amount == 3500.0 or local_amount == 3500.0
    assert "2025-08-29" in str(result.fields.get("date", ""))
