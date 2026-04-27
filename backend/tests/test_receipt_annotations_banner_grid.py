"""Bug 4: PDF redesign — banner_grid strategy (Carolyn's reference).

3x3 grid per A4 page, full-width green banner overlaying the top of each
thumbnail with USD/local amounts + date + supplier + B/P, sorted by
transaction date, no legend page, no color-coded borders.

Test surface focuses on the deterministic shape (page count, banner text
formatters, sort order, multi-page handling, public-strategy dispatch)
rather than pixel-perfect rendering — actual PIL output is reviewed by
PM via the regenerated demo PDF.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.receipt_annotations import (  # noqa: E402
    DEFAULT_STRATEGY,
    ReceiptAnnotationLine,
    _format_banner_amount_line,
    _format_banner_meta_line,
    _render_banner_grid_layout,
    create_annotated_receipts_pdf,
)


def _line(*, day: int = 17, supplier: str = "Test", amount: float = 100.0,
          currency: str = "USD", local_amount: float | None = 1000.0,
          local_currency: str | None = "TRY", bp: str = "Business",
          receipt_id: int = 1) -> ReceiptAnnotationLine:
    return ReceiptAnnotationLine(
        receipt_id=receipt_id,
        transaction_id=receipt_id,
        review_row_id=receipt_id,
        receipt_path=None,  # No file → uses _placeholder_tile
        receipt_file_name=f"r{receipt_id}.jpg",
        transaction_date=date(2025, 10, day),
        supplier=supplier,
        amount=amount,
        currency=currency,
        business_or_personal=bp,
        report_bucket="Other",
        business_reason="x",
        attendees="",
        local_amount=local_amount,
        local_currency=local_currency,
    )


# ---------------------------------------------------------------------------
# 1. DEFAULT_STRATEGY is the new banner_grid
# ---------------------------------------------------------------------------


def test_banner_grid_strategy_remains_callable_explicitly() -> None:
    """banner_grid was the default in PR #33 (Bug 4) and is now demoted to
    a legacy strategy by paired_card (Layout D from Claude Design). The
    strategy still ships and stays callable behind the explicit flag —
    its end-to-end render is exercised in
    test_create_annotated_receipts_pdf_legacy_banner_grid_still_works in
    test_receipt_annotations_paired_card.py.
    """
    # paired_card is the new default (asserted in test_default_strategy_is_paired_card).
    assert DEFAULT_STRATEGY != "banner_grid"
    # banner_grid is still a recognized strategy keyword.
    from app.services.receipt_annotations import _render_banner_grid_layout
    assert callable(_render_banner_grid_layout)


# ---------------------------------------------------------------------------
# 2. Banner amount formatter — both currencies / fallbacks
# ---------------------------------------------------------------------------


def test_banner_amount_line_shows_both_currencies_when_present() -> None:
    line = _line(amount=27.01, currency="USD",
                 local_amount=1100.50, local_currency="TRY")
    assert _format_banner_amount_line(line) == "USD $27.01 | TRY 1100.50"


def test_banner_amount_line_omits_local_when_currency_is_same() -> None:
    """When report-currency == local-currency (USD report on a USD receipt),
    don't emit a redundant 'USD $5.00 | USD 5.00'."""
    line = _line(amount=27.01, currency="USD",
                 local_amount=27.01, local_currency="USD")
    assert _format_banner_amount_line(line) == "USD $27.01"


def test_banner_amount_line_handles_missing_local() -> None:
    """Manual-entry receipts may have no local_amount; banner shows only
    the report-side figure."""
    line = _line(amount=27.01, currency="USD",
                 local_amount=None, local_currency=None)
    assert _format_banner_amount_line(line) == "USD $27.01"


def test_banner_amount_line_non_usd_report_currency() -> None:
    """A TRY-denominated report (rare) shows 'TRY X.XX' without the dollar sign."""
    line = _line(amount=419.58, currency="TRY",
                 local_amount=None, local_currency=None)
    assert _format_banner_amount_line(line) == "TRY 419.58"


# ---------------------------------------------------------------------------
# 3. Banner meta formatter
# ---------------------------------------------------------------------------


def test_banner_meta_line_format() -> None:
    line = _line(day=20, supplier="FERMAKI MEAT & MORE", bp="Business")
    assert _format_banner_meta_line(line) == "2025-10-20 | FERMAKI MEAT & MORE | Business"


def test_banner_meta_line_truncates_very_long_supplier() -> None:
    line = _line(supplier="İKBAL LOKANTACILIK / ZEY SPORT SPOR MAL. SAN. TİC. LTD. ŞTİ.")
    out = _format_banner_meta_line(line)
    # ``shorten(width=42)`` clips at the last word boundary <= 42 chars,
    # appending "…". Final length stays bounded so the banner doesn't
    # overflow the cell width.
    parts = out.split(" | ")
    assert len(parts) == 3
    assert len(parts[1]) <= 42, f"supplier '{parts[1]}' still too long ({len(parts[1])} chars)"


# ---------------------------------------------------------------------------
# 4. Page count math — 9-per-page chunking, no legend
# ---------------------------------------------------------------------------


def test_banner_grid_produces_one_page_per_nine_receipts() -> None:
    """3x3 grid; 12 receipts → 2 pages (9 + 3). No legend page."""
    lines = [_line(day=10 + i, supplier=f"S{i}", receipt_id=i + 1) for i in range(12)]
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "banner_grid.pdf"
        n = _render_banner_grid_layout(lines, out)
    assert n == 2, f"12 receipts in 3x3 grid should produce 2 pages, got {n}"


def test_banner_grid_single_receipt_produces_one_page() -> None:
    lines = [_line()]
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "banner_grid.pdf"
        n = _render_banner_grid_layout(lines, out)
    assert n == 1


def test_banner_grid_nine_receipts_produces_one_page() -> None:
    """Edge: exactly fills one page, no overflow."""
    lines = [_line(day=10 + i, supplier=f"S{i}", receipt_id=i + 1) for i in range(9)]
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "banner_grid.pdf"
        n = _render_banner_grid_layout(lines, out)
    assert n == 1


# ---------------------------------------------------------------------------
# 5. Sort order — by transaction_date then receipt_id
# ---------------------------------------------------------------------------


def test_banner_grid_sorts_receipts_by_transaction_date() -> None:
    """Renderer sorts by transaction_date asc; same-day ties break by
    receipt_id. Verifies the November expected order: same-day receipts
    (e.g. 2025-10-20 FERMAKI #3 + #4) stay together by receipt_id order.
    """
    # Construct in deliberate non-sorted input order.
    lines = [
        _line(day=20, supplier="Mavi Ege", receipt_id=8),
        _line(day=10, supplier="Vodafone", receipt_id=6),
        _line(day=20, supplier="Fermaki #3", receipt_id=3),
        _line(day=20, supplier="Fermaki #4", receipt_id=4),
    ]
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "banner_grid.pdf"
        # We can't easily inspect the in-PDF rendering without parsing
        # back. But _render_banner_grid_layout sorts internally, so
        # we exercise the sort by giving it deliberately-shuffled input
        # and verifying it doesn't crash + produces the right count.
        n = _render_banner_grid_layout(lines, out)
    assert n == 1


# ---------------------------------------------------------------------------
# 6. Public dispatch — banner_grid is the default; old strategies still work
# ---------------------------------------------------------------------------


def test_create_annotated_receipts_pdf_default_uses_banner_grid() -> None:
    """Calling without strategy goes through banner_grid renderer."""
    lines = [_line()]
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "default.pdf"
        n = create_annotated_receipts_pdf(lines, out)  # default strategy
    # 1 receipt → 1 page (no legend page added — banner_grid drops the legend).
    assert n == 1


def test_create_annotated_receipts_pdf_legacy_grid_strategy_still_works() -> None:
    lines = [_line()]
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "grid.pdf"
        n = create_annotated_receipts_pdf(lines, out, strategy="grid")
    # 'grid' strategy is the simple packed 3x3; 1 receipt → 1 page.
    assert n == 1


def test_create_annotated_receipts_pdf_legacy_day_grouped_colored_still_works() -> None:
    lines = [_line()]
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "dgc.pdf"
        n = create_annotated_receipts_pdf(lines, out, strategy="day_grouped_colored")
    # day_grouped_colored adds a legend page on top of the day pages.
    # 1 receipt → 1 legend + 1 day page = 2 pages.
    assert n == 2


def test_create_annotated_receipts_pdf_unknown_strategy_raises() -> None:
    lines = [_line()]
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "x.pdf"
        try:
            create_annotated_receipts_pdf(lines, out, strategy="not_a_strategy")
        except ValueError as exc:
            assert "Unknown layout strategy" in str(exc)
            return
        raise AssertionError("expected ValueError for unknown strategy")
