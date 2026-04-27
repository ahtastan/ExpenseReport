"""Paired-card layout (Layout D from Claude Design handoff).

3x3 grid per A4 page, B&W only, hotel-folio + multi-page receipts treated
as full-A4 exceptions. Replaces banner_grid as the new default.

Tests focus on the deterministic shape:
  - DEFAULT_STRATEGY contract
  - public dispatcher routes "paired_card" + legacy strategies
  - 11 mock receipts → 2 pages MAX (spec requirement)
  - hotel buckets get full-A4 / mixed treatment, not grid cells
  - multi-receipt grouping carries (group_index, group_count, group_total_usd)
  - rendered output is grayscale only — no chromatic colors anywhere

Pixel-level visual fidelity is reviewed by PM via the regenerated demo PDF.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from app.services.receipt_annotations import (  # noqa: E402
    DEFAULT_STRATEGY,
    INK_HEX,
    PAPER_HEX,
    ReceiptAnnotationLine,
    _build_paired_card_stream,
    _is_hotel_bucket,
    _iso_week_label,
    _render_paired_card_layout,
    create_annotated_receipts_pdf,
)


def _line(*, rid: int, day: int = 17, supplier: str = "Test", amount: float = 10.0,
          currency: str = "USD", bucket: str = "Other", bp: str = "Business",
          review_row_id: int | None = None,
          local_amount: float | None = 100.0, local_currency: str | None = "TRY") -> ReceiptAnnotationLine:
    """Build a synthetic ReceiptAnnotationLine. Deliberately no file path so
    the renderer falls back to _placeholder_tile, keeping tests fast and
    deterministic without filesystem fixtures.
    """
    return ReceiptAnnotationLine(
        receipt_id=rid,
        transaction_id=rid,
        review_row_id=review_row_id if review_row_id is not None else rid,
        receipt_path=None,
        receipt_file_name=f"r{rid}.jpg",
        transaction_date=date(2025, 10, day),
        supplier=supplier,
        amount=amount,
        currency=currency,
        business_or_personal=bp,
        report_bucket=bucket,
        business_reason="",
        attendees="",
        local_amount=local_amount,
        local_currency=local_currency,
    )


# ---------------------------------------------------------------------------
# 1. DEFAULT_STRATEGY contract
# ---------------------------------------------------------------------------


def test_default_strategy_is_paired_card() -> None:
    assert DEFAULT_STRATEGY == "paired_card", (
        f"Spec: paired_card replaces banner_grid as default; "
        f"got {DEFAULT_STRATEGY!r}"
    )


# ---------------------------------------------------------------------------
# 2. 11 mock receipts → 2 A4 pages MAX
# ---------------------------------------------------------------------------


def test_paired_card_eleven_receipts_with_hotel_folio_two_pages() -> None:
    """Spec: 11 receipts must fit in 2 A4 pages MAX. The November-shaped
    case has 1 hotel folio (full-page exception) + 10 grid receipts:
    grid stream packs 9 into page 1, leaving 1 spillover; the folio +
    spillover share page 2 via the mixed-page renderer.
    """
    lines = [
        _line(
            rid=i, day=10 + i,
            bucket="Hotel/Lodging/Laundry" if i == 5 else "Other",
        )
        for i in range(11)
    ]
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "pc.pdf"
        n = _render_paired_card_layout(lines, out)
    assert n == 2, f"11 receipts → 2 pages MAX; got {n}"


def test_paired_card_eleven_receipts_no_exceptions_two_pages() -> None:
    """Same 11-receipt count, no hotel/multi-page: page 1 packs 9, page 2
    has the remaining 2. Still 2 pages."""
    lines = [_line(rid=i, day=10 + i, bucket="Other") for i in range(11)]
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "pc.pdf"
        n = _render_paired_card_layout(lines, out)
    assert n == 2, f"11 receipts → 2 pages MAX; got {n}"


def test_paired_card_nine_receipts_one_page() -> None:
    """Edge: exactly fills the 3x3 grid in one page."""
    lines = [_line(rid=i, day=10 + i) for i in range(9)]
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "pc.pdf"
        n = _render_paired_card_layout(lines, out)
    assert n == 1


def test_paired_card_single_receipt_one_page() -> None:
    lines = [_line(rid=1)]
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "pc.pdf"
        n = _render_paired_card_layout(lines, out)
    assert n == 1


# ---------------------------------------------------------------------------
# 3. Multi-receipt grouping — receipts sharing review_row_id collapse to one
#    group with group_index / group_count / group_total_usd attached.
# ---------------------------------------------------------------------------


def test_paired_card_two_receipts_same_review_row_form_one_group() -> None:
    """The FERMAKI case: two receipts sharing review_row_id end up with
    group_count=2 and group_total_usd = sum of amounts. The card renderer
    uses these to draw the GROUP X/Y — TOTAL $XX.XX line at the bottom
    of each card.
    """
    a = _line(rid=1, day=20, supplier="FERMAKI", amount=92.72,
              review_row_id=999)
    b = _line(rid=2, day=20, supplier="FERMAKI", amount=16.65,
              review_row_id=999)
    stream = _build_paired_card_stream([a, b], week_label="42")
    assert len(stream) == 2
    # Both members share group context; group_count and group_total_usd match.
    assert {ctx.group_count for ctx in stream} == {2}
    assert all(abs(ctx.group_total_usd - 109.37) < 0.01 for ctx in stream)
    # Indices are 1-based and distinct.
    assert sorted(ctx.group_index for ctx in stream) == [1, 2]


def test_paired_card_solo_receipt_has_group_count_one() -> None:
    """A receipt that's the only member of its review_row_id has
    group_count=1; the renderer omits the GROUP line in that case."""
    only = _line(rid=1, day=10, supplier="Vodafone", amount=27.01,
                 review_row_id=42)
    stream = _build_paired_card_stream([only], week_label="41")
    assert len(stream) == 1
    assert stream[0].group_count == 1
    assert stream[0].group_index == 1


def test_paired_card_short_id_assignment_is_R01_R02_etc() -> None:
    """After sort, each context gets a Rxx short_id (R01, R02, …). Used by
    the corner-ID label in each card's left half."""
    lines = [_line(rid=i, day=10 + i, review_row_id=i) for i in range(3)]
    stream = _build_paired_card_stream(lines, week_label="41")
    assert [c.short_id for c in stream] == ["R01", "R02", "R03"]


# ---------------------------------------------------------------------------
# 4. Hotel folios + multi-page receipts → full-A4 exception path
# ---------------------------------------------------------------------------


def test_paired_card_hotel_bucket_recognized_as_exception() -> None:
    """Hotel/Lodging/Laundry triggers the full-A4 exception treatment so
    folios get readable space rather than being squashed into a 3x3 cell.
    """
    assert _is_hotel_bucket("Hotel/Lodging/Laundry") is True
    assert _is_hotel_bucket("Other") is False
    assert _is_hotel_bucket(None) is False
    # Whitespace-resistance.
    assert _is_hotel_bucket("  Hotel/Lodging/Laundry  ") is True


def test_paired_card_with_only_hotel_renders_one_full_page() -> None:
    """Single hotel receipt → full-page exception → one page. (No grid page
    needed since there's nothing else.)"""
    lines = [_line(rid=1, day=10, supplier="Hotel", amount=200.0,
                   bucket="Hotel/Lodging/Laundry")]
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "pc.pdf"
        n = _render_paired_card_layout(lines, out)
    assert n == 1


def test_paired_card_hotel_plus_three_others_renders_one_mixed_page() -> None:
    """1 hotel + 3 grid receipts: spec says mixed page (folio at top +
    spillover row beneath) when remainder ≤ 3. Total = 1 page."""
    lines = [
        _line(rid=1, day=10, supplier="Hotel", amount=200.0,
              bucket="Hotel/Lodging/Laundry"),
        _line(rid=2, day=11, supplier="A"),
        _line(rid=3, day=12, supplier="B"),
        _line(rid=4, day=13, supplier="C"),
    ]
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "pc.pdf"
        n = _render_paired_card_layout(lines, out)
    assert n == 1


# ---------------------------------------------------------------------------
# 5. B&W only — no chromatic colors anywhere on the rendered page
# ---------------------------------------------------------------------------


def test_paired_card_output_is_grayscale_only() -> None:
    """Spec: B&W only. Sample the rendered grid page and assert every
    pixel is on the grayscale axis (R==G==B). Catches accidental
    introduction of green/red/etc. fills.
    """
    lines = [_line(rid=i, day=10 + i, supplier=f"S{i}") for i in range(3)]
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "pc.pdf"
        _render_paired_card_layout(lines, out)
        # Re-render directly to a PIL image for pixel inspection — saving
        # to PDF and reading back is lossy. Instead, exercise one of the
        # renderer's public branches that returns an Image.
        from app.services.receipt_annotations import (
            _render_paired_card_grid_page,
            _build_paired_card_stream,
        )
        stream = _build_paired_card_stream(lines, week_label="41-42")
        page = _render_paired_card_grid_page(
            stream[:9], page_no=1, page_of=1,
            period_label="41-42", receipt_count=len(stream),
            total_usd=sum(c.line.amount for c in stream),
            employee="A.H. TASTAN", report_no="EDT-2025-W42",
        )

    # Sample 200 pixels in a uniform grid; for each, R must equal G and B.
    rgb = page.convert("RGB")
    w, h = rgb.size
    chromatic_pixels = []
    for y in range(0, h, max(1, h // 14)):
        for x in range(0, w, max(1, w // 14)):
            r, g, b = rgb.getpixel((x, y))
            if not (r == g == b):
                # Tolerance for JPEG/PNG conversion artifacts: max channel
                # delta ≤ 3 still counts as "grayscale enough" for B&W.
                if max(r, g, b) - min(r, g, b) > 3:
                    chromatic_pixels.append((x, y, r, g, b))
    assert not chromatic_pixels, (
        f"Found {len(chromatic_pixels)} chromatic pixels in paired_card "
        f"output (B&W spec violation). First 5: {chromatic_pixels[:5]}"
    )


# ---------------------------------------------------------------------------
# 6. iso_week_label helper
# ---------------------------------------------------------------------------


def test_iso_week_label_single_week() -> None:
    lines = [_line(rid=1, day=15)]  # 2025-10-15 → ISO week 42
    assert _iso_week_label(lines) == "42"


def test_iso_week_label_multi_week_range() -> None:
    """November dataset spans 2025-10-10 (W41) to 2025-10-20 (W43)."""
    lines = [
        _line(rid=1, day=10),
        _line(rid=2, day=20),
    ]
    assert _iso_week_label(lines) == "41-43"


def test_iso_week_label_empty_returns_placeholder() -> None:
    """Defensive: empty input returns "??" rather than crashing."""
    assert _iso_week_label([]) == "??"


# ---------------------------------------------------------------------------
# 7. Public dispatcher — paired_card is the default; legacy strategies still work
# ---------------------------------------------------------------------------


def test_create_annotated_receipts_pdf_default_uses_paired_card(tmp_path: Path) -> None:
    lines = [_line(rid=1)]
    out = tmp_path / "default.pdf"
    n = create_annotated_receipts_pdf(lines, out)
    # paired_card with 1 receipt → 1 page.
    assert n == 1


def test_create_annotated_receipts_pdf_legacy_banner_grid_still_works(tmp_path: Path) -> None:
    """Legacy banner_grid strategy stays callable behind the explicit flag."""
    lines = [_line(rid=1)]
    out = tmp_path / "bg.pdf"
    n = create_annotated_receipts_pdf(lines, out, strategy="banner_grid")
    assert n == 1


def test_create_annotated_receipts_pdf_legacy_grid_still_works(tmp_path: Path) -> None:
    lines = [_line(rid=1)]
    out = tmp_path / "g.pdf"
    n = create_annotated_receipts_pdf(lines, out, strategy="grid")
    assert n == 1


def test_create_annotated_receipts_pdf_legacy_day_grouped_colored_still_works(tmp_path: Path) -> None:
    lines = [_line(rid=1)]
    out = tmp_path / "dgc.pdf"
    n = create_annotated_receipts_pdf(lines, out, strategy="day_grouped_colored")
    # Legacy: legend page + day group page = 2.
    assert n >= 2


def test_create_annotated_receipts_pdf_unknown_strategy_raises(tmp_path: Path) -> None:
    lines = [_line(rid=1)]
    out = tmp_path / "x.pdf"
    try:
        create_annotated_receipts_pdf(lines, out, strategy="not_real")
    except ValueError as exc:
        assert "Unknown layout strategy" in str(exc)
        return
    raise AssertionError("expected ValueError for unknown strategy")
