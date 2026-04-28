"""F2 amount parsing hardening for Turkish receipt totals."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.receipt_extraction import _parse_amount  # noqa: E402


def _amount(text: str) -> tuple[Decimal | None, str | None]:
    return _parse_amount(text)


def test_turkish_thousand_decimal_formats_parse_to_full_amount() -> None:
    for raw in ("15.680,00 TL", "15,680.00 TL", "15 680,00 TL", "15680,00 TL"):
        amount, currency = _amount(raw)

        assert amount == Decimal("15680.0000")
        assert currency == "TRY"


def test_simple_turkish_amounts_remain_correct() -> None:
    for raw, expected in (("175,00 TL", "175.0000"), ("715,00 TL", "715.0000")):
        amount, currency = _amount(raw)

        assert amount == Decimal(expected)
        assert currency == "TRY"


def test_kdv_amount_is_not_chosen_over_total_label() -> None:
    amount, currency = _amount(
        """
        KDV TOPLAM      1.568,00 TL
        SATIS TUTAR   15.680,00 TL
        """
    )

    assert amount == Decimal("15680.0000")
    assert currency == "TRY"


def test_smaller_line_item_is_not_chosen_over_total_label() -> None:
    amount, currency = _amount(
        """
        ODA             680,00 TL
        TOPLAM       15.680,00 TL
        """
    )

    assert amount == Decimal("15680.0000")
    assert currency == "TRY"
