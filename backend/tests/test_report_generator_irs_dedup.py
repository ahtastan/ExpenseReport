"""Bug 2: same-day-same-supplier-same-code receipts must NOT silently drop the
second receipt. The November dataset has two FERMAKI Meat & More receipts
on 2025-10-20 (split-bill across two card transactions) that previously
collapsed into one IRS row with one receipt's amount only — the other
disappeared.

Fix: ``group_meal_details_for_irs()`` collapses same-(code, supplier) details
into one tuple with all component amounts. ``fill_b`` writes the IRS row
metadata once (identical across the duplicates by definition) and writes a
SUM formula directly to the amount cell (column J) when ≥ 2 components are
present, so the auditor sees ``=A+B`` and recognizes the split.
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.report_generator import (  # noqa: E402
    MealDetailLine,
    group_meal_details_for_irs,
)


def _detail(*, place: str, code: str, amount: Decimal,
            participants: str = "self", reason: str = "x") -> MealDetailLine:
    return MealDetailLine(
        tx_date=date(2025, 10, 20),
        code=code,
        place=place,
        location="",
        participants=participants,
        reason=reason,
        amount=amount,
        eg=False,
        mr=False,
    )


def test_two_same_supplier_same_code_collapse_with_sum_components() -> None:
    """The FERMAKI case: two D-coded same-supplier details on the same day
    return ONE tuple whose sum_components carries both amounts."""
    fermaki_a = _detail(place="Fermaki Meat & More", code="D", amount=Decimal("92.72"))
    fermaki_b = _detail(place="Fermaki Meat & More", code="D", amount=Decimal("16.65"))
    grouped = group_meal_details_for_irs([fermaki_a, fermaki_b])

    assert len(grouped) == 1, f"expected 1 collapsed group, got {len(grouped)}"
    primary, sum_components = grouped[0]
    # Primary metadata comes from the first detail (they're identical by
    # construction in the real flow — same supplier means same place/etc.).
    assert primary.place == "Fermaki Meat & More"
    assert primary.code == "D"
    # Sum components carry BOTH amounts so caller can write =92.72+16.65.
    assert sum_components == [Decimal("92.72"), Decimal("16.65")]


def test_single_supplier_returns_none_for_sum_components() -> None:
    """When a (code, supplier) appears once, sum_components is None — caller
    leaves the template's pre-existing amount formula in place."""
    yuvam = _detail(place="Yuvamceto Kebap", code="D", amount=Decimal("22.29"))
    grouped = group_meal_details_for_irs([yuvam])
    assert len(grouped) == 1
    primary, sum_components = grouped[0]
    assert primary.place == "Yuvamceto Kebap"
    assert sum_components is None


def test_three_same_supplier_same_code_produce_three_component_sum() -> None:
    """Edge: three split-bill transactions for one dinner."""
    a = _detail(place="Fermaki", code="D", amount=Decimal("50.00"))
    b = _detail(place="Fermaki", code="D", amount=Decimal("30.00"))
    c = _detail(place="Fermaki", code="D", amount=Decimal("20.00"))
    grouped = group_meal_details_for_irs([a, b, c])
    assert len(grouped) == 1
    _primary, sum_components = grouped[0]
    assert sum_components == [Decimal("50.00"), Decimal("30.00"), Decimal("20.00")]


def test_supplier_normalization_collapses_case_and_whitespace() -> None:
    """'Fermaki Meat' and 'fermaki meat' (case diff or trailing space) should
    collapse — they're the same supplier from the operator's perspective."""
    a = _detail(place="Fermaki Meat & More", code="D", amount=Decimal("92.72"))
    b = _detail(place=" fermaki meat & more ", code="D", amount=Decimal("16.65"))
    grouped = group_meal_details_for_irs([a, b])
    assert len(grouped) == 1, "case + trailing-space variants should collapse"
    _primary, sum_components = grouped[0]
    assert sum_components == [Decimal("92.72"), Decimal("16.65")]


def test_different_suppliers_same_code_kept_as_separate_groups() -> None:
    """Tellioglu Snacks + Mavi Ege Snacks on the same day: each is its own
    group. (They'll later contend for the M-code row in fill_b, where
    'first wins' is preserved — that's a separate template-layout concern,
    not a Bug-2 concern.)"""
    tellioglu = _detail(place="Tellioglu Akaryakit", code="M", amount=Decimal("3.30"))
    mavi = _detail(place="Mavi Ege Market", code="M", amount=Decimal("21.68"))
    grouped = group_meal_details_for_irs([tellioglu, mavi])
    assert len(grouped) == 2, "different suppliers must remain separate"
    # Each group has sum_components=None (single detail per group).
    for primary, sum_components in grouped:
        assert sum_components is None


def test_different_codes_same_supplier_kept_separate() -> None:
    """Same supplier serving Snacks AND Dinner on the same day → two groups."""
    snack = _detail(place="Fermaki", code="M", amount=Decimal("10.00"))
    dinner = _detail(place="Fermaki", code="D", amount=Decimal("90.00"))
    grouped = group_meal_details_for_irs([snack, dinner])
    assert len(grouped) == 2
    codes = sorted(primary.code for primary, _ in grouped)
    assert codes == ["D", "M"]
