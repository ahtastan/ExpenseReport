"""Addition B regression suite.

Covers:
  - Schema migration for ReceiptDocument.receipt_type
  - Vision prompt contains the new rubric
  - Extraction pipeline stores receipt_type (valid + invalid→unknown)
  - Hotel folio soft-flag validator (fires when payment_receipt, silent when itemized)
  - Retroactive classifier script (dry-run vs --apply, idempotency)
  - PDF layout: color assignment, day grouping, consolidation, legend, hotel-folio
    multi-page counts-as-one-for-grouping, default strategy
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

VERIFY_ROOT = Path.cwd() / ".verify_data"
VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{VERIFY_ROOT / f'addition_b_{uuid4().hex}.db'}"
os.environ["EXPENSE_STORAGE_ROOT"] = str(VERIFY_ROOT)
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import pytest  # noqa: E402

from app.json_utils import DecimalEncoder  # noqa: E402
from PIL import Image  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine  # noqa: E402

from app.models import (  # noqa: E402
    AppUser,
    ExpenseReport,
    MatchDecision,
    ReceiptDocument,
    ReviewRow,
    ReviewSession,
    StatementImport,
    StatementTransaction,
)
from app.services import model_router, receipt_annotations  # noqa: E402
from app.services.receipt_annotations import (  # noqa: E402
    LINE_COLOR_PALETTE,
    ReceiptAnnotationLine,
    _date_subtotals_text,
    assign_colors_to_lines,
    consolidate_consecutive_days,
    create_annotated_receipts_pdf,
    group_by_day,
    group_receipts_for_pdf,
    render_legend_page,
)
from app.services.receipt_extraction import (  # noqa: E402
    RECEIPT_TYPES,
    _coerce_receipt_type,
    apply_receipt_extraction,
)
from app.services.report_validation import validate_report_readiness  # noqa: E402


# ─── Fixtures helpers ─────────────────────────────────────────────────────────

def _make_line(
    *,
    transaction_id: int,
    tx_date: date,
    supplier: str = "Supplier",
    amount: float = 50.0,
    currency: str = "USD",
    bucket: str = "Other",
    receipt_id: int | None = None,
    review_row_id: int | None = None,
    receipt_path: str | None = None,
    bp: str = "Business",
) -> ReceiptAnnotationLine:
    return ReceiptAnnotationLine(
        receipt_id=receipt_id if receipt_id is not None else transaction_id,
        transaction_id=transaction_id,
        review_row_id=review_row_id if review_row_id is not None else transaction_id,
        receipt_path=receipt_path,
        receipt_file_name=f"tx_{transaction_id}.jpg",
        transaction_date=tx_date,
        supplier=supplier,
        amount=amount,
        currency=currency,
        business_or_personal=bp,
        report_bucket=bucket,
        business_reason="Test reason",
        attendees="self",
    )


def _seed_hotel_fixture(
    session: Session,
    *,
    supplier: str,
    receipt_type: str,
) -> int:
    """Seed one statement/tx/receipt/decision/report/review/row for a hotel-validator test.
    Returns expense_report_id."""
    user = AppUser(telegram_user_id=100 + hash(uuid4().hex) % 9000, display_name="Hotel Tester")
    session.add(user)
    session.flush()

    statement = StatementImport(
        source_filename=f"htl_{uuid4().hex[:6]}.xlsx",
        row_count=1,
        uploader_user_id=user.id,
    )
    session.add(statement)
    session.commit()
    session.refresh(statement)

    tx = StatementTransaction(
        statement_import_id=statement.id,
        transaction_date=date(2026, 4, 1),
        supplier_raw=supplier,
        supplier_normalized=supplier.upper(),
        local_currency="USD",
        local_amount=Decimal("350.0"),
        usd_amount=Decimal("350.0"),
    )
    receipt = ReceiptDocument(
        source="test",
        status="imported",
        content_type="photo",
        original_file_name="hotel.jpg",
        extracted_date=date(2026, 4, 1),
        extracted_supplier=supplier,
        extracted_local_amount=Decimal("350.0"),
        extracted_currency="USD",
        business_or_personal="Business",
        report_bucket="Hotel/Lodging/Laundry",
        business_reason="Customer visit overnight",
        attendees="self",
        receipt_type=receipt_type,
        needs_clarification=False,
    )
    session.add(tx)
    session.add(receipt)
    session.commit()
    session.refresh(tx)
    session.refresh(receipt)

    decision = MatchDecision(
        statement_transaction_id=tx.id,
        receipt_document_id=receipt.id,
        confidence="high",
        match_method="hotel_test",
        approved=True,
        reason="Addition B hotel fixture",
    )
    session.add(decision)
    session.commit()

    report = ExpenseReport(
        owner_user_id=user.id,
        report_kind="diners_statement",
        title="Hotel test",
        status="draft",
        report_currency="USD",
        statement_import_id=statement.id,
    )
    session.add(report)
    session.commit()
    session.refresh(report)

    review = ReviewSession(
        expense_report_id=report.id,
        statement_import_id=statement.id,
        status="draft",
    )
    session.add(review)
    session.commit()
    session.refresh(review)

    confirmed = {
        "transaction_id": tx.id,
        "receipt_id": receipt.id,
        "transaction_date": "2026-04-01",
        "supplier": supplier,
        "amount": Decimal("350.0"),
        "currency": "USD",
        "business_or_personal": "Business",
        "report_bucket": "Hotel/Lodging/Laundry",
        "business_reason": "Customer visit overnight",
        "attendees": "self",
    }
    row = ReviewRow(
        review_session_id=review.id,
        statement_transaction_id=tx.id,
        receipt_document_id=receipt.id,
        match_decision_id=decision.id,
        status="confirmed",
        attention_required=False,
        source_json=json.dumps({"statement": {}, "receipt": {}, "match": {"status": "matched"}}),
        suggested_json=json.dumps(confirmed, cls=DecimalEncoder),
        confirmed_json=json.dumps(confirmed, cls=DecimalEncoder),
    )
    session.add(row)
    session.commit()
    session.refresh(row)

    review.status = "confirmed"
    review.snapshot_json = json.dumps([{**confirmed, "review_row_id": row.id}], cls=DecimalEncoder)
    session.add(review)
    session.commit()

    return report.id


# ═══════════════════════════════════════════════════════════════════════════════
# Part 1 — schema migration
# ═══════════════════════════════════════════════════════════════════════════════

def _migrate_fresh_db(migration_func) -> Path:
    """Build a tiny SQLite DB with just the receiptdocument table and run
    the M1 Day 2 migration against it. Returns the DB path."""
    tmp = Path(tempfile.mkdtemp(prefix="m1d2_mig_")) / "app.db"
    conn = sqlite3.connect(tmp)
    conn.execute(
        """
        CREATE TABLE receiptdocument (
            id INTEGER PRIMARY KEY,
            original_file_name TEXT
        )
        """
    )
    conn.commit()
    conn.close()
    migration_func(str(tmp))
    return tmp


def test_migration_adds_receipt_type_column_and_index():
    from migrations.m1_day2_receipt_type import migrate

    db_path = _migrate_fresh_db(migrate)

    conn = sqlite3.connect(db_path)
    cols = {row[1]: row for row in conn.execute("PRAGMA table_info(receiptdocument)").fetchall()}
    assert "receipt_type" in cols, f"receipt_type column missing; got {list(cols)}"
    # Column type VARCHAR(50)
    assert "VARCHAR" in cols["receipt_type"][2].upper()

    index_rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='receiptdocument'"
    ).fetchall()
    index_names = {row[0] for row in index_rows}
    assert "ix_receiptdocument_receipt_type" in index_names
    conn.close()
    shutil.rmtree(db_path.parent, ignore_errors=True)


def test_migration_rerun_is_no_op(capsys):
    from migrations.m1_day2_receipt_type import migrate

    db_path = _migrate_fresh_db(migrate)
    # First migrate() call already ran in _migrate_fresh_db. Run a second time.
    result = migrate(str(db_path))
    captured = capsys.readouterr()
    assert result.already_migrated is True
    assert "already migrated" in captured.out
    shutil.rmtree(db_path.parent, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Part 2 — vision prompt + extraction pipeline
# ═══════════════════════════════════════════════════════════════════════════════

def test_vision_prompt_mentions_receipt_type_and_five_values():
    prompt = model_router._VISION_PROMPT
    assert "receipt_type" in prompt
    for kind in ("itemized", "payment_receipt", "invoice", "confirmation", "unknown"):
        assert kind in prompt, f"prompt missing {kind!r}"


def test_receipt_type_from_vision_lands_on_model(isolated_db, monkeypatch):
    # Stub vision to return a valid receipt_type. No network.
    def fake_vision(path):
        return model_router.VisionResult(
            fields={
                "date": "2026-04-01",
                "supplier": "Migros",
                "amount": 120.5,
                "currency": "TRY",
                "business_or_personal": "Business",
                "receipt_type": "itemized",
            },
            model="fake",
            escalated=False,
            notes=[],
        )

    monkeypatch.setattr(model_router, "vision_extract", fake_vision)

    with Session(isolated_db) as session:
        receipt = ReceiptDocument(
            source="test",
            status="received",
            content_type="photo",
            original_file_name="migros.jpg",
            storage_path=str(VERIFY_ROOT / "fake_migros.jpg"),
        )
        session.add(receipt)
        session.commit()
        session.refresh(receipt)

        apply_receipt_extraction(session, receipt)
        session.refresh(receipt)
        assert receipt.receipt_type == "itemized"


def test_receipt_type_invalid_value_coerced_to_unknown(isolated_db, monkeypatch):
    def fake_vision(path):
        return model_router.VisionResult(
            fields={
                "date": "2026-04-01",
                "supplier": "Mystery",
                "amount": 1.0,
                "currency": "USD",
                "business_or_personal": "Business",
                "receipt_type": "nonsense_value",
            },
            model="fake",
            escalated=False,
            notes=[],
        )

    monkeypatch.setattr(model_router, "vision_extract", fake_vision)

    with Session(isolated_db) as session:
        receipt = ReceiptDocument(
            source="test",
            status="received",
            content_type="photo",
            original_file_name="mystery.jpg",
            storage_path=str(VERIFY_ROOT / "fake_mystery.jpg"),
        )
        session.add(receipt)
        session.commit()
        session.refresh(receipt)

        apply_receipt_extraction(session, receipt)
        session.refresh(receipt)
        assert receipt.receipt_type == "unknown"


# ═══════════════════════════════════════════════════════════════════════════════
# Part 3 — hotel folio soft-flag
# ═══════════════════════════════════════════════════════════════════════════════

def test_hotel_payment_receipt_soft_flags(isolated_db):
    with Session(isolated_db) as session:
        report_id = _seed_hotel_fixture(
            session, supplier="Hilton Istanbul", receipt_type="payment_receipt"
        )
        validation = validate_report_readiness(session, expense_report_id=report_id)

    codes = [i.code for i in validation.issues]
    assert "hotel_needs_itemized_folio" in codes, f"got issues={codes}"
    flag = next(i for i in validation.issues if i.code == "hotel_needs_itemized_folio")
    assert flag.severity == "warning"
    assert "Hilton" in flag.message
    # Soft flag — does not block.
    errors = [i for i in validation.issues if i.severity == "error"]
    assert errors == [], f"unexpected errors: {[i.code for i in errors]}"
    assert validation.ready is True


def test_hotel_with_itemized_folio_no_flag(isolated_db):
    with Session(isolated_db) as session:
        report_id = _seed_hotel_fixture(
            session, supplier="Hampton Inn Ankara", receipt_type="itemized"
        )
        validation = validate_report_readiness(session, expense_report_id=report_id)

    codes = [i.code for i in validation.issues]
    assert "hotel_needs_itemized_folio" not in codes
    assert validation.ready is True


# ═══════════════════════════════════════════════════════════════════════════════
# Part 4 — classifier script
# ═══════════════════════════════════════════════════════════════════════════════

def test_classifier_script_dry_run_and_apply(isolated_db, monkeypatch, capsys):
    # Seed a receipt with receipt_type=NULL and a fake storage_path.
    fake_path = VERIFY_ROOT / f"fake_{uuid4().hex}.jpg"
    fake_path.write_bytes(b"\x00\x01")

    with Session(isolated_db) as session:
        receipt = ReceiptDocument(
            source="test",
            status="received",
            content_type="photo",
            original_file_name="c.jpg",
            storage_path=str(fake_path),
            receipt_type=None,
        )
        session.add(receipt)
        session.commit()
        session.refresh(receipt)
        rid = receipt.id

    # Stub vision to return "invoice".
    def fake_vision(path):
        return model_router.VisionResult(
            fields={"receipt_type": "invoice"},
            model="fake",
            escalated=False,
            notes=[],
        )

    monkeypatch.setattr(model_router, "vision_extract", fake_vision)

    # Import the script module fresh.
    from scripts import classify_existing_receipts as script  # type: ignore

    # Dry-run: should log but not update.
    exit_code = script.main([])
    assert exit_code == 0
    with Session(isolated_db) as session:
        after_dry = session.get(ReceiptDocument, rid)
        assert after_dry.receipt_type is None, "dry-run must not write"

    # Apply: should update to 'invoice'.
    exit_code = script.main(["--apply"])
    assert exit_code == 0
    with Session(isolated_db) as session:
        after_apply = session.get(ReceiptDocument, rid)
        assert after_apply.receipt_type == "invoice"

    # Idempotent: running again finds no NULL rows, no change.
    capsys.readouterr()
    exit_code = script.main(["--apply"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "candidates: 0" in out


# ═══════════════════════════════════════════════════════════════════════════════
# Part 5 — PDF layout
# ═══════════════════════════════════════════════════════════════════════════════

def test_group_receipts_for_pdf_packs_date_ordered_receipts_without_overcrowding():
    # Low-volume receipts can share a page even across date gaps, in date order.
    lines = [
        _make_line(transaction_id=1, tx_date=date(2026, 4, 1)),
        _make_line(transaction_id=2, tx_date=date(2026, 4, 5)),
        _make_line(transaction_id=3, tx_date=date(2026, 4, 10)),
    ]
    groups = group_receipts_for_pdf(lines, strategy="day_grouped_colored")
    assert len(groups) == 1
    assert [line.transaction_date for line in groups[0]] == [
        date(2026, 4, 1),
        date(2026, 4, 5),
        date(2026, 4, 10),
    ]

    # Grid strategy: single group
    grid_groups = group_receipts_for_pdf(lines, strategy="grid")
    assert len(grid_groups) == 1
    assert len(grid_groups[0]) == 3


def test_create_annotated_receipts_pdf_renders_with_default_strategy(tmp_path):
    """Default is now banner_grid (Bug 4): 3 receipts fit on one A4 page,
    no legend. The legacy day_grouped_colored layout still works when
    invoked explicitly — see the next test.
    """
    lines = [_make_line(transaction_id=i, tx_date=date(2026, 4, 1)) for i in range(3)]
    out = tmp_path / "annot.pdf"
    page_count = create_annotated_receipts_pdf(lines, out)
    assert out.exists()
    assert out.stat().st_size > 0
    # banner_grid: 3 receipts → 1 page (3x3 grid, no legend).
    assert page_count == 1


def test_create_annotated_receipts_pdf_renders_with_legacy_day_grouped_colored(tmp_path):
    """Legacy day_grouped_colored still renders correctly when explicitly
    selected: legend page + day group page = 2 pages min."""
    lines = [_make_line(transaction_id=i, tx_date=date(2026, 4, 1)) for i in range(3)]
    out = tmp_path / "annot_legacy.pdf"
    page_count = create_annotated_receipts_pdf(
        lines, out, strategy="day_grouped_colored"
    )
    assert out.exists()
    assert out.stat().st_size > 0
    # Legend (1 page for 3 lines) + 1 day group page = 2 pages minimum.
    assert page_count >= 2


def test_day_grouping_merges_two_small_days():
    # Day 1: 4 receipts, Day 2: 4 receipts. Total 8 ≤ 9 → merge.
    lines: list[ReceiptAnnotationLine] = []
    for i in range(4):
        lines.append(_make_line(transaction_id=100 + i, tx_date=date(2026, 4, 1)))
    for i in range(4):
        lines.append(_make_line(transaction_id=200 + i, tx_date=date(2026, 4, 2)))
    groups = group_receipts_for_pdf(lines, strategy="day_grouped_colored")
    assert len(groups) == 1, f"expected merged single group, got {len(groups)} groups"
    assert len(groups[0]) == 8


def test_day_grouping_splits_when_over_nine():
    # Day 1: 6, Day 2: 5. Combined 11 > 9 → two separate groups.
    lines: list[ReceiptAnnotationLine] = []
    for i in range(6):
        lines.append(_make_line(transaction_id=300 + i, tx_date=date(2026, 4, 1)))
    for i in range(5):
        lines.append(_make_line(transaction_id=400 + i, tx_date=date(2026, 4, 2)))
    groups = group_receipts_for_pdf(lines, strategy="day_grouped_colored")
    assert len(groups) == 2
    assert len(groups[0]) == 6
    assert len(groups[1]) == 5


def test_day_grouping_splits_large_same_day_to_avoid_overcrowding():
    lines = [
        _make_line(transaction_id=600 + i, tx_date=date(2026, 4, 1))
        for i in range(10)
    ]

    groups = group_receipts_for_pdf(lines, strategy="day_grouped_colored")

    assert len(groups) == 2
    assert len(groups[0]) == 9
    assert len(groups[1]) == 1
    assert all(line.transaction_date == date(2026, 4, 1) for group in groups for line in group)


def test_date_subtotals_text_lists_multi_date_same_currency_totals():
    lines = [
        _make_line(transaction_id=1, tx_date=date(2026, 4, 1), amount=20.0, currency="USD"),
        _make_line(transaction_id=2, tx_date=date(2026, 4, 5), amount=35.0, currency="USD"),
        _make_line(transaction_id=3, tx_date=date(2026, 4, 5), amount=5.5, currency="USD"),
    ]

    text = _date_subtotals_text(lines)

    assert text == "Date subtotals: 2026-04-01 USD 20.00 | 2026-04-05 USD 40.50"


def test_date_subtotals_text_lists_mixed_currencies_per_date():
    lines = [
        _make_line(transaction_id=1, tx_date=date(2026, 4, 1), amount=20.0, currency="USD"),
        _make_line(transaction_id=2, tx_date=date(2026, 4, 1), amount=7.5, currency="EUR"),
        _make_line(transaction_id=3, tx_date=date(2026, 4, 5), amount=35.0, currency="USD"),
    ]

    text = _date_subtotals_text(lines)

    assert text == "Date subtotals: 2026-04-01 EUR 7.50, USD 20.00 | 2026-04-05 USD 35.00"


def test_date_subtotals_text_omits_single_date_groups():
    lines = [
        _make_line(transaction_id=1, tx_date=date(2026, 4, 1), amount=20.0, currency="USD"),
        _make_line(transaction_id=2, tx_date=date(2026, 4, 1), amount=35.0, currency="USD"),
    ]

    assert _date_subtotals_text(lines) == ""


def test_color_assignment_sequential_to_palette():
    lines = [
        _make_line(transaction_id=1, tx_date=date(2026, 4, 1)),
        _make_line(transaction_id=2, tx_date=date(2026, 4, 1)),
        _make_line(transaction_id=3, tx_date=date(2026, 4, 1)),
    ]
    colors = assign_colors_to_lines(lines)
    assert colors[1] == LINE_COLOR_PALETTE[0]
    assert colors[2] == LINE_COLOR_PALETTE[1]
    assert colors[3] == LINE_COLOR_PALETTE[2]


def test_color_assignment_uses_review_row_id_not_transaction_id():
    shared_line_a = ReceiptAnnotationLine(
        receipt_id=101,
        transaction_id=1,
        review_row_id=900,
        receipt_path=None,
        receipt_file_name="tx_1.jpg",
        transaction_date=date(2026, 4, 1),
        supplier="Supplier",
        amount=50.0,
        currency="USD",
        business_or_personal="Business",
        report_bucket="Other",
        business_reason="Test reason",
        attendees="self",
    )
    shared_line_b = ReceiptAnnotationLine(
        receipt_id=102,
        transaction_id=2,
        review_row_id=900,
        receipt_path=None,
        receipt_file_name="tx_2.jpg",
        transaction_date=date(2026, 4, 1),
        supplier="Supplier",
        amount=50.0,
        currency="USD",
        business_or_personal="Business",
        report_bucket="Other",
        business_reason="Test reason",
        attendees="self",
    )
    next_line = ReceiptAnnotationLine(
        receipt_id=103,
        transaction_id=3,
        review_row_id=901,
        receipt_path=None,
        receipt_file_name="tx_3.jpg",
        transaction_date=date(2026, 4, 1),
        supplier="Supplier",
        amount=50.0,
        currency="USD",
        business_or_personal="Business",
        report_bucket="Other",
        business_reason="Test reason",
        attendees="self",
    )

    colors = assign_colors_to_lines([shared_line_a, shared_line_b, next_line])

    assert colors[900] == LINE_COLOR_PALETTE[0]
    assert colors[901] == LINE_COLOR_PALETTE[1]
    assert len(colors) == 2


def test_color_assignment_cycles_palette_beyond_ten():
    # 12 distinct transaction_ids; line 11 should wrap to palette[0].
    lines = [
        _make_line(transaction_id=i, tx_date=date(2026, 4, 1))
        for i in range(1, 13)
    ]
    colors = assign_colors_to_lines(lines)
    assert colors[11] == LINE_COLOR_PALETTE[0]  # 11th distinct line → index 10 → wraps to 0
    assert colors[12] == LINE_COLOR_PALETTE[1]


def test_legend_page_lists_all_lines_with_colors():
    lines = [
        _make_line(transaction_id=i, tx_date=date(2026, 4, 1), supplier=f"Supp{i}", amount=10.0 * i, bucket=f"Bucket{i}")
        for i in range(1, 6)
    ]
    colors = assign_colors_to_lines(lines)
    pages = render_legend_page(lines, colors)
    assert len(pages) == 1  # 5 lines easily fit on one legend page
    # Sample pixels where we expect the color swatches to sit — the legend
    # paints rectangles in each of the Tableau 10 colors. We verify at least
    # one pixel for each of the first three lines' colors appears on the page.
    img = pages[0].convert("RGB")
    sampled = {img.getpixel((x, y)) for x in range(50, 250, 10) for y in range(230, 500, 10)}
    for k in (1, 2, 3):
        want = tuple(int(colors[k].lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
        assert want in sampled, f"expected swatch for line {k} color {colors[k]} on legend page"


def test_hotel_folio_multi_page_counts_as_one_for_grouping(tmp_path):
    # Simulate: one hotel folio PDF with 3 pages + 7 other receipts on same day.
    # All are ReceiptAnnotationLine records, so count = 8 (≤9) → single group.
    folio_path = tmp_path / "folio.pdf"
    try:
        import pypdfium2 as pdfium  # noqa: F401
    except Exception:
        pytest.skip("pypdfium2 not available")

    # Build a trivial 3-page PDF using PIL -> PDF.
    pages = [Image.new("RGB", (600, 800), color=c) for c in ("white", "lightblue", "lightyellow")]
    pages[0].save(folio_path, save_all=True, append_images=pages[1:])

    lines = [_make_line(transaction_id=500, tx_date=date(2026, 4, 1), receipt_path=str(folio_path))]
    for i in range(7):
        lines.append(_make_line(transaction_id=501 + i, tx_date=date(2026, 4, 1)))

    groups = group_receipts_for_pdf(lines, strategy="day_grouped_colored")
    assert len(groups) == 1
    assert len(groups[0]) == 8

    # And the render actually emits >1 page for the group (grid page + folio extras).
    out = tmp_path / "annot.pdf"
    page_count = create_annotated_receipts_pdf(lines, out)
    # Legend (1) + day grid (1) + 2 extra folio pages = at least 4.
    assert page_count >= 3, f"expected ≥3 pages, got {page_count}"


# ─── Smaller unit checks ──────────────────────────────────────────────────────

def test_coerce_receipt_type_normalizes_variants():
    assert _coerce_receipt_type("ITEMIZED") == "itemized"
    assert _coerce_receipt_type(" payment_receipt ") == "payment_receipt"
    assert _coerce_receipt_type("payment receipt") == "payment_receipt"
    assert _coerce_receipt_type("garbage") == "unknown"
    assert _coerce_receipt_type(None) is None
    assert _coerce_receipt_type(42) is None


def test_receipt_types_set_matches_spec():
    assert RECEIPT_TYPES == {
        "itemized", "payment_receipt", "invoice", "confirmation", "unknown"
    }
