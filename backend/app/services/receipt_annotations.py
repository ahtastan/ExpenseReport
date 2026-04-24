"""Annotated-receipts PDF generator.

Addition B: default layout is ``day_grouped_colored`` — receipts are grouped
by transaction date (with adjacent-day consolidation up to 9 per page),
every receipt gets a colored border matching its report line, and the
output opens with a legend page listing all lines with their colors.

The old 3x3 packed grid is preserved behind ``strategy='grid'``.

Public API:

    create_annotated_receipts_pdf(lines, output_path, *, strategy='day_grouped_colored') -> int

Composable helpers (useful for tests and future strategies):

    assign_colors_to_lines(lines) -> dict[line_key, hex_color]
    group_by_day(lines) -> dict[date, list[line]]
    consolidate_consecutive_days(by_day, max_per_group=9) -> list[list[line]]
    group_receipts_for_pdf(lines, *, strategy) -> list[list[line]]
    render_legend_page(lines, colors) -> list[PIL.Image]
    render_day_page(group, colors) -> list[PIL.Image]
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from textwrap import shorten

from PIL import Image, ImageDraw, ImageFont, ImageOps, JpegImagePlugin  # noqa: F401


# ─── Canvas / layout constants ────────────────────────────────────────────────

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
PDF_EXTENSIONS = {".pdf"}

A4_WIDTH = 2480
A4_HEIGHT = 3508
MARGIN_X = 45
MARGIN_Y = 60
GAP_X = 16
GAP_Y = 22
COLUMNS = 3
ROWS = 3
CELL_WIDTH = (A4_WIDTH - 2 * MARGIN_X - GAP_X * (COLUMNS - 1)) // COLUMNS
CELL_HEIGHT = (A4_HEIGHT - 2 * MARGIN_Y - GAP_Y * (ROWS - 1)) // ROWS

DAY_HEADER_HEIGHT = 240  # ~1 inch on an A4 canvas
DAY_GRID_HEIGHT = A4_HEIGHT - 2 * MARGIN_Y - DAY_HEADER_HEIGHT - GAP_Y
DAY_CELL_HEIGHT = (DAY_GRID_HEIGHT - GAP_Y * (ROWS - 1)) // ROWS

BORDER_WIDTH = 12  # 3pt at ~288 DPI — thick enough to survive print compression
MAX_RECEIPTS_PER_DAY_GROUP = 9
PDF_RASTER_DPI = 180
PDF_MAX_PAGES = 10

DEFAULT_STRATEGY = "day_grouped_colored"

# Tableau 10 palette — colorblind-aware, print-safe, max differentiation.
LINE_COLOR_PALETTE: tuple[str, ...] = (
    "#1f77b4",  # blue
    "#ff7f0e",  # orange
    "#2ca02c",  # green
    "#d62728",  # red
    "#9467bd",  # purple
    "#8c564b",  # brown
    "#e377c2",  # pink
    "#17becf",  # teal
    "#bcbd22",  # olive
    "#7f7f7f",  # gray
)
LINE_COLOR_NAMES: tuple[str, ...] = (
    "blue", "orange", "green", "red", "purple",
    "brown", "pink", "teal", "olive", "gray",
)


# ─── Data shape ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ReceiptAnnotationLine:
    receipt_id: int | None
    transaction_id: int
    review_row_id: int | None
    receipt_path: str | None
    receipt_file_name: str
    transaction_date: date
    supplier: str
    amount: float
    currency: str
    business_or_personal: str
    report_bucket: str
    business_reason: str
    attendees: str


# ─── Fonts ────────────────────────────────────────────────────────────────────

def _font(name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default()


FONT_BOLD = _font("arialbd.ttf", 74)
FONT_MEDIUM = _font("arialbd.ttf", 42)
FONT_SMALL = _font("arial.ttf", 30)
FONT_DAY_HEADER = _font("arialbd.ttf", 60)
FONT_DAY_SUB = _font("arial.ttf", 36)
FONT_LEGEND_TITLE = _font("arialbd.ttf", 110)
FONT_LEGEND_ENTRY = _font("arial.ttf", 42)


# ─── Line keys, color assignment, grouping ────────────────────────────────────

def _line_key(line: ReceiptAnnotationLine) -> int | None:
    """Stable ReviewRow/report-line identifier for the receipt's color group."""
    return line.review_row_id


def assign_colors_to_lines(
    lines: list[ReceiptAnnotationLine],
) -> dict[int | None, str]:
    """Assign a palette color to each distinct line in input order.

    Order is the order ``lines`` are given. Duplicates (multiple receipts
    sharing a line key) reuse the same color. Palette cycles past 10 lines.
    """
    colors: dict[int | None, str] = {}
    ordered_keys: list[int | None] = []
    for line in lines:
        k = _line_key(line)
        if k not in colors:
            ordered_keys.append(k)
            colors[k] = LINE_COLOR_PALETTE[(len(ordered_keys) - 1) % len(LINE_COLOR_PALETTE)]
    return colors


def group_by_day(
    lines: list[ReceiptAnnotationLine],
) -> dict[date, list[ReceiptAnnotationLine]]:
    """Bucket receipts by transaction_date. Within each bucket receipts are
    sorted by (line_key, receipt_id) so same-line receipts stay adjacent."""
    by_day: dict[date, list[ReceiptAnnotationLine]] = {}
    for line in lines:
        by_day.setdefault(line.transaction_date, []).append(line)
    for day in by_day:
        by_day[day].sort(
            key=lambda ln: (_line_key(ln), ln.receipt_id or 0)
        )
    return by_day


def consolidate_consecutive_days(
    by_day: dict[date, list[ReceiptAnnotationLine]],
    max_per_group: int = MAX_RECEIPTS_PER_DAY_GROUP,
) -> list[list[ReceiptAnnotationLine]]:
    """Pack date-ordered receipt groups without exceeding the page cap.

    - Receipts stay in transaction-date order, and same-date receipts stay
      together unless that day exceeds ``max_per_group``.
    - Multiple dates may share one page when the combined receipt count fits.
    - Multi-page PDF receipts count as 1 toward the cap (each
      ``ReceiptAnnotationLine`` is one receipt by construction).
    """
    dates = sorted(by_day.keys())
    groups: list[list[ReceiptAnnotationLine]] = []
    current: list[ReceiptAnnotationLine] = []

    for d in dates:
        day_lines = by_day[d]
        for offset in range(0, len(day_lines), max_per_group):
            chunk = day_lines[offset : offset + max_per_group]
            if current and (len(current) + len(chunk)) > max_per_group:
                groups.append(current)
                current = []
            if len(chunk) == max_per_group:
                if current:
                    groups.append(current)
                    current = []
                groups.append(list(chunk))
                continue
            current.extend(chunk)

    if current:
        groups.append(current)
    return groups


def group_receipts_for_pdf(
    lines: list[ReceiptAnnotationLine],
    *,
    strategy: str = DEFAULT_STRATEGY,
    max_per_group: int = MAX_RECEIPTS_PER_DAY_GROUP,
) -> list[list[ReceiptAnnotationLine]]:
    """Return groups of receipts, one group per page of the output PDF."""
    if not lines:
        return []
    if strategy == "day_grouped_colored":
        return consolidate_consecutive_days(group_by_day(lines), max_per_group)
    if strategy == "grid":
        return [list(lines)]
    raise ValueError(f"Unknown layout strategy: {strategy!r}")


# ─── Receipt image rendering ──────────────────────────────────────────────────

def _line_color_bp(line: ReceiptAnnotationLine) -> str:
    bp = line.business_or_personal.lower()
    if bp == "business":
        return "green"
    if bp == "personal":
        return "red"
    return "darkorange"


def _draw_label(
    img: Image.Image,
    line: ReceiptAnnotationLine,
) -> Image.Image:
    """Overlay the amount + metadata label box used in the old grid layout.

    Retained for backward compatibility with ``strategy='grid'``. The new
    day-grouped layout draws its header at the top of each page instead
    of per-receipt labels.
    """
    draw = ImageDraw.Draw(img)
    color = _line_color_bp(line)
    amount = f"{line.currency} {line.amount:,.2f}".strip()
    label = f"{amount} | {line.business_or_personal or 'REVIEW'}"
    sub = shorten(f"{line.transaction_date.isoformat()} | {line.supplier}", width=88, placeholder="...")
    receipt_label = line.receipt_id if line.receipt_id is not None else "missing"
    meta = shorten(f"{line.report_bucket or 'Unbucketed'} | R{receipt_label} TX{line.transaction_id}", width=88, placeholder="...")

    label_box = draw.textbbox((0, 0), label, font=FONT_BOLD)
    sub_box = draw.textbbox((0, 0), sub, font=FONT_SMALL)
    meta_box = draw.textbbox((0, 0), meta, font=FONT_SMALL)
    width = max(label_box[2] - label_box[0], sub_box[2] - sub_box[0], meta_box[2] - meta_box[0])
    height = (label_box[3] - label_box[1]) + (sub_box[3] - sub_box[1]) + (meta_box[3] - meta_box[1]) + 44
    x = max(15, img.width - width - 60)
    y = 15

    draw.rounded_rectangle((x - 22, y - 12, x + width + 22, y + height), radius=14, fill="white", outline=color, width=5)
    draw.text((x, y), label, fill=color, font=FONT_BOLD)
    draw.text((x, y + 88), sub, fill=color, font=FONT_SMALL)
    draw.text((x, y + 128), meta, fill=color, font=FONT_SMALL)
    return img


def _placeholder_tile(line: ReceiptAnnotationLine, reason: str) -> Image.Image:
    img = Image.new("RGB", (900, 1200), "white")
    draw = ImageDraw.Draw(img)
    color = _line_color_bp(line)
    draw.rounded_rectangle((35, 35, 865, 1165), radius=18, outline=color, width=6)
    draw.text((70, 85), "Receipt file not rendered", fill=color, font=FONT_MEDIUM)
    draw.text((70, 155), reason, fill="black", font=FONT_SMALL)
    details = [
        f"Receipt: {line.receipt_file_name}",
        f"Date: {line.transaction_date.isoformat()}",
        f"Merchant: {line.supplier}",
        f"Amount: {line.currency} {line.amount:,.2f}",
        f"Type: {line.business_or_personal or 'Review'}",
        f"Bucket: {line.report_bucket or 'Unbucketed'}",
        f"Receipt ID: {line.receipt_id if line.receipt_id is not None else 'missing'}",
        f"Transaction ID: {line.transaction_id}",
    ]
    y = 245
    for detail in details:
        draw.text((70, y), shorten(detail, width=44, placeholder="..."), fill="black", font=FONT_SMALL)
        y += 52
    return img


def _pdf_pages_to_images(path: Path) -> list[Image.Image] | None:
    """Render every page of a PDF to a PIL image via pypdfium2.

    Returns ``None`` if pypdfium2 is unavailable or the file can't be opened.
    Caps at ``PDF_MAX_PAGES`` to bound pathological folio files.
    """
    try:
        import pypdfium2 as pdfium  # deferred import
    except Exception:
        return None
    try:
        document = pdfium.PdfDocument(str(path))
    except Exception:
        return None
    images: list[Image.Image] = []
    scale = PDF_RASTER_DPI / 72.0
    try:
        render_count = min(len(document), PDF_MAX_PAGES)
        for idx in range(render_count):
            page = document[idx]
            try:
                bitmap = page.render(scale=scale)
                images.append(bitmap.to_pil().convert("RGB"))
            finally:
                try:
                    page.close()
                except Exception:
                    pass
    finally:
        try:
            document.close()
        except Exception:
            pass
    return images or None


def _load_receipt_pages(line: ReceiptAnnotationLine) -> list[Image.Image]:
    """Return one PIL image per page of a receipt (single-image receipts → 1 page)."""
    if not line.receipt_path:
        return [_placeholder_tile(line, "No storage path is available.")]
    path = Path(line.receipt_path)
    if not path.exists():
        return [_placeholder_tile(line, "The stored file path does not exist.")]
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        try:
            with Image.open(path) as img:
                rendered = ImageOps.exif_transpose(img).convert("RGB")
            return [rendered]
        except OSError as exc:
            return [_placeholder_tile(line, f"Could not open image: {exc}")]
    if suffix in PDF_EXTENSIONS:
        pages = _pdf_pages_to_images(path)
        if pages:
            return pages
        return [_placeholder_tile(line, "PDF could not be rasterized (pypdfium2 missing or file unreadable).")]
    return [_placeholder_tile(line, f"{suffix.upper().lstrip('.')} receipts are not rendered.")]


def _receipt_image(line: ReceiptAnnotationLine) -> Image.Image:
    """Old grid-layout single-image view (backward-compat for strategy='grid')."""
    pages = _load_receipt_pages(line)
    first = pages[0]
    return _draw_label(first, line)


def _apply_color_border(img: Image.Image, hex_color: str) -> Image.Image:
    """Paint a colored rectangle just inside the receipt frame."""
    draw = ImageDraw.Draw(img)
    w, h = img.size
    half = BORDER_WIDTH // 2
    draw.rectangle(
        (half, half, w - half - 1, h - half - 1),
        outline=hex_color,
        width=BORDER_WIDTH,
    )
    return img


# ─── Legend rendering ─────────────────────────────────────────────────────────

def _per_line_summaries(
    lines: list[ReceiptAnnotationLine],
    colors: dict[int | None, str],
) -> list[dict]:
    """One summary entry per distinct line in color-assignment order."""
    by_key: dict[int | None, dict] = {}
    ordered_keys: list[int | None] = []
    for line in lines:
        k = _line_key(line)
        if k not in by_key:
            ordered_keys.append(k)
            by_key[k] = {
                "line_key": k,
                "bucket": line.report_bucket or "Unbucketed",
                "date": line.transaction_date,
                "supplier": line.supplier,
                "amount": 0.0,
                "currency": line.currency,
                "color": colors.get(k, "#000000"),
                "count": 0,
            }
        by_key[k]["amount"] += float(line.amount or 0)
        by_key[k]["count"] += 1
    return [by_key[k] for k in ordered_keys]


def render_legend_page(
    lines: list[ReceiptAnnotationLine],
    colors: dict[int | None, str],
) -> list[Image.Image]:
    """Render the color legend. Flows to multiple pages if >20 lines."""
    summaries = _per_line_summaries(lines, colors)
    if not summaries:
        return []

    entries_per_page = 20
    pages: list[Image.Image] = []
    for offset in range(0, len(summaries), entries_per_page):
        chunk = summaries[offset : offset + entries_per_page]
        page = Image.new("RGB", (A4_WIDTH, A4_HEIGHT), "white")
        draw = ImageDraw.Draw(page)

        title = "Receipt Legend"
        if offset > 0:
            title = f"Receipt Legend (cont.)"
        draw.text((MARGIN_X, MARGIN_Y), title, fill="black", font=FONT_LEGEND_TITLE)

        y = MARGIN_Y + 170
        for idx, summary in enumerate(chunk, start=offset + 1):
            swatch_size = 70
            draw.rectangle(
                (MARGIN_X, y, MARGIN_X + 260, y + swatch_size),
                fill=summary["color"],
                outline="black",
                width=3,
            )
            text = (
                f"Line {idx} — {summary['bucket']}, "
                f"{summary['currency']} {summary['amount']:,.2f} on "
                f"{summary['date'].isoformat()}, {summary['supplier']}"
            )
            draw.text(
                (MARGIN_X + 290, y + 12),
                shorten(text, width=120, placeholder="..."),
                fill="black",
                font=FONT_LEGEND_ENTRY,
            )
            y += swatch_size + 36

        pages.append(page)
    return pages


# ─── Day-group page rendering ─────────────────────────────────────────────────

def _format_day_range(group: list[ReceiptAnnotationLine]) -> str:
    days = sorted({line.transaction_date for line in group})
    if len(days) == 1:
        return days[0].isoformat()
    return f"{days[0].isoformat()} – {days[-1].isoformat()}"


def _group_total_by_currency(
    group: list[ReceiptAnnotationLine],
) -> str:
    totals: dict[str, float] = {}
    for line in group:
        totals[line.currency] = totals.get(line.currency, 0.0) + float(line.amount or 0)
    parts = [f"{cur} {amt:,.2f}" for cur, amt in sorted(totals.items())]
    return ", ".join(parts) or "—"


def _date_subtotals_text(
    group: list[ReceiptAnnotationLine],
    *,
    width: int = 135,
) -> str:
    days = sorted({line.transaction_date for line in group})
    if len(days) <= 1:
        return ""

    totals_by_day: dict[date, dict[str, float]] = {}
    for line in group:
        day_totals = totals_by_day.setdefault(line.transaction_date, {})
        day_totals[line.currency] = day_totals.get(line.currency, 0.0) + float(line.amount or 0)

    parts: list[str] = []
    for day in days:
        currency_totals = totals_by_day.get(day, {})
        totals = ", ".join(
            f"{cur} {amt:,.2f}" for cur, amt in sorted(currency_totals.items())
        )
        parts.append(f"{day.isoformat()} {totals}")
    return shorten("Date subtotals: " + " | ".join(parts), width=width, placeholder="...")


def render_day_page(
    group: list[ReceiptAnnotationLine],
    colors: dict[int | None, str],
) -> list[Image.Image]:
    """Render a single day-group. Returns 1+ pages.

    Page 1: header strip + up-to-9 thumbnails with colored borders.
    Pages 2+: for each multi-page PDF receipt, its pages 2..N rendered
    full-size (also with colored borders) so hotel folios land inside
    the group.
    """
    if not group:
        return []

    grid_page = Image.new("RGB", (A4_WIDTH, A4_HEIGHT), "white")
    draw = ImageDraw.Draw(grid_page)

    day_range = _format_day_range(group)
    total_text = _group_total_by_currency(group)
    draw.text((MARGIN_X, MARGIN_Y), f"Day: {day_range}", fill="black", font=FONT_DAY_HEADER)
    draw.text(
        (MARGIN_X, MARGIN_Y + 80),
        f"{len(group)} receipt{'s' if len(group) != 1 else ''}, total {total_text}",
        fill="black",
        font=FONT_DAY_SUB,
    )
    date_subtotals = _date_subtotals_text(group)
    if date_subtotals:
        draw.text(
            (MARGIN_X, MARGIN_Y + 130),
            date_subtotals,
            fill="black",
            font=FONT_SMALL,
        )

    grid_origin_y = MARGIN_Y + DAY_HEADER_HEIGHT
    extra_pages: list[Image.Image] = []
    for idx, line in enumerate(group[:COLUMNS * ROWS]):
        pages = _load_receipt_pages(line)
        thumb = pages[0].copy()
        thumb.thumbnail((CELL_WIDTH, DAY_CELL_HEIGHT), Image.Resampling.LANCZOS)
        thumb = _apply_color_border(thumb, colors.get(_line_key(line), "#000000"))
        col = idx % COLUMNS
        row = idx // COLUMNS
        x0 = MARGIN_X + col * (CELL_WIDTH + GAP_X)
        y0 = grid_origin_y + row * (DAY_CELL_HEIGHT + GAP_Y)
        x = x0 + (CELL_WIDTH - thumb.width) // 2
        y = y0 + (DAY_CELL_HEIGHT - thumb.height) // 2
        grid_page.paste(thumb, (x, y))

        # Render any additional PDF pages as full-size pages appended after
        # the grid page. Preserves hotel-folio readability.
        if len(pages) > 1:
            color = colors.get(_line_key(line), "#000000")
            for extra in pages[1:]:
                full = extra.copy()
                full.thumbnail((A4_WIDTH - 2 * MARGIN_X, A4_HEIGHT - 2 * MARGIN_Y), Image.Resampling.LANCZOS)
                full = _apply_color_border(full, color)
                page = Image.new("RGB", (A4_WIDTH, A4_HEIGHT), "white")
                px = (A4_WIDTH - full.width) // 2
                py = (A4_HEIGHT - full.height) // 2
                page.paste(full, (px, py))
                extra_pages.append(page)

    return [grid_page, *extra_pages]


# ─── Grid (legacy) strategy ───────────────────────────────────────────────────

def _render_grid_layout(
    lines: list[ReceiptAnnotationLine],
    output_path: Path,
) -> int:
    """Old 3x3 packed grid. No colors, no legend, no day headers."""
    ordered = sorted(
        lines,
        key=lambda line: (line.transaction_date, line.supplier.lower(), line.amount, line.receipt_id or 0),
    )
    pages: list[Image.Image] = []
    for offset in range(0, len(ordered), COLUMNS * ROWS):
        batch = ordered[offset : offset + COLUMNS * ROWS]
        page = Image.new("RGB", (A4_WIDTH, A4_HEIGHT), "white")
        for idx, line in enumerate(batch):
            img = _receipt_image(line)
            img.thumbnail((CELL_WIDTH, CELL_HEIGHT), Image.Resampling.LANCZOS)
            col = idx % COLUMNS
            row = idx // COLUMNS
            x0 = MARGIN_X + col * (CELL_WIDTH + GAP_X)
            y0 = MARGIN_Y + row * (CELL_HEIGHT + GAP_Y)
            x = x0 + (CELL_WIDTH - img.width) // 2
            y = y0 + (CELL_HEIGHT - img.height) // 2
            page.paste(img, (x, y))
        pages.append(page)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(output_path, save_all=True, append_images=pages[1:])
    return len(pages)


# ─── Public entry point ───────────────────────────────────────────────────────

def create_annotated_receipts_pdf(
    lines: list[ReceiptAnnotationLine],
    output_path: Path,
    *,
    strategy: str = DEFAULT_STRATEGY,
) -> int:
    """Render the annotated receipts PDF. Returns the page count written."""
    if not lines:
        raise ValueError("No receipt lines are available for annotation")

    if strategy == "grid":
        return _render_grid_layout(lines, output_path)

    if strategy != "day_grouped_colored":
        raise ValueError(f"Unknown layout strategy: {strategy!r}")

    colors = assign_colors_to_lines(lines)
    groups = group_receipts_for_pdf(lines, strategy=strategy)

    pages: list[Image.Image] = []
    pages.extend(render_legend_page(lines, colors))
    for group in groups:
        pages.extend(render_day_page(group, colors))

    if not pages:
        # Defensive — should be impossible given the non-empty check above.
        raise ValueError("No pages rendered")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(output_path, save_all=True, append_images=pages[1:])
    return len(pages)
