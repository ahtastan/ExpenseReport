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

DEFAULT_STRATEGY = "paired_card"

# ─── Banner-grid layout constants (legacy — Carolyn's reference banner) ───────
# Kept callable behind strategy="banner_grid" but no longer the default. The
# new default ``paired_card`` (Layout D from Claude Design) is the EDT-style
# B&W audit annex.

BANNER_HEIGHT_PX = 160                  # ~1.5cm at 300 DPI; sized for two text lines
BANNER_BG_HEX = "#2ca02c"               # Tableau green; matches reference PDF
BANNER_TEXT_COLOR = "#ffffff"
BANNER_INNER_PAD_X = 32                 # left padding for banner text
BANNER_INNER_PAD_Y_TOP = 12             # space above first text line
BANNER_LINE_GAP_PX = 56                 # vertical gap between line 1 and line 2
THIN_BORDER_HEX = "#bdbdbd"             # 2px gray hairline around each thumb
THIN_BORDER_WIDTH_PX = 2

# ─── Paired-card layout constants (Layout D from Claude Design handoff) ───────
# A4 portrait, 3x3 grid of "paired cards" — left half is the receipt thumbnail
# and a small "Rxx" corner ID, right half is an info column with amount /
# date / supplier / bucket+xlsx-ref / Business-Personal tag, plus an optional
# GROUP X/Y line at the bottom when 2+ receipts share a single XLSX line.
# B&W only: thin black rules + hatching for accent. Helvetica + JetBrains Mono
# (fallback to system sans/mono per ``_font``).
#
# Design used 794×1123px @ 96dpi ⇒ 300dpi scale factor ≈ 3.124. Constants
# below are pre-scaled to 300dpi so PIL renders at print resolution.

INK_HEX = "#111111"          # primary text + rules (--ink)
INK_2_HEX = "#2B2B2B"        # secondary text (--ink-2)
INK_3_HEX = "#6B6B6B"        # tertiary / metadata (--ink-3)
RULE_SOFT_HEX = "#C9C9C9"    # sub-rules (--rule-soft)
PAPER_HEX = "#FFFFFF"        # background (--paper)

# Page geometry (300 DPI, scaled from design's 96dpi 794×1123 = 36px margin).
# All paired-card-specific so we don't disturb the existing MARGIN_X/Y
# constants used by legacy strategies.
PC_MARGIN_X = 113            # ~36 design px × 3.124
PC_MARGIN_Y = 113
PC_HEADER_TOP_Y = 56         # ~18 design px × 3.124  (running-header strip)
PC_HEADER_BAND_Y = 125       # ~40 design px (RECEIPT ANNEX | period)
PC_FOOTER_BOTTOM_Y = 56      # symmetric to header
PC_GRID_TOP_Y = 256           # below the RECEIPT ANNEX header band
PC_GRID_BOTTOM_Y = A4_HEIGHT - 90   # leaves room for footer

PC_COLS = 3
PC_GAP_PX = 25                # ~8 design px
PC_GRID_WIDTH = A4_WIDTH - 2 * PC_MARGIN_X
PC_CELL_WIDTH = (PC_GRID_WIDTH - PC_GAP_PX * (PC_COLS - 1)) // PC_COLS
PC_CELL_HEIGHT = 1006         # ~322 design px × 3.124
PC_CARD_BORDER_WIDTH = 3      # ~1pt at 300dpi (≈ 1.04pt/px)

PC_THUMB_SIDE_PCT = 0.50      # left half is receipt thumbnail
PC_INFO_PADDING_X = 28
PC_INFO_PADDING_Y = 28

# Bucket → Page 1A row mapping, for the "WKS NN-NN, ROW N" xlsx-ref label
# under each card's bucket line. Mirrors the row layout in
# report_generator._fill_workbook's fill_a().
_BUCKET_TO_PAGE_1A_ROW: dict[str, int] = {
    "Airfare/Bus/Ferry/Other": 7,
    "Hotel/Lodging/Laundry": 8,
    "Auto Rental": 9,
    "Auto Gasoline": 10,
    "Taxi/Parking/Tolls/Uber": 11,
    "Other Travel Related": 14,
    "Membership/Subscription Fees": 18,
    "Customer Gifts": 19,
    "Telephone/Internet": 20,
    "Postage/Shipping": 21,
    "Admin Supplies": 22,
    "Lab Supplies": 23,
    "Field Service Supplies": 24,
    "Assets": 25,
    "Other": 26,
    "Meals/Snacks": 29,
    "Breakfast": 30,
    "Lunch": 31,
    "Dinner": 32,
    "Entertainment": 35,
}

_HOTEL_BUCKETS: frozenset[str] = frozenset({"Hotel/Lodging/Laundry"})

# Single-letter EDT codes for the per-card bucket badge. Keeps the auditor
# scanning categories at-a-glance: [F] for Fuel, [D] for Dinner, etc.
# The letter precedes the short label inside the solid-black bucket tag.
_BUCKET_LETTER_CODE: dict[str, str] = {
    "Airfare/Bus/Ferry/Other":         "A",
    "Hotel/Lodging/Laundry":           "H",
    "Auto Rental":                     "R",
    "Auto Gasoline":                   "F",   # F = Fuel (per PM spec)
    "Taxi/Parking/Tolls/Uber":         "T",
    "Other Travel Related":            "O",
    "Membership/Subscription Fees":    "S",   # S = Subscription
    "Customer Gifts":                  "G",
    "Telephone/Internet":              "P",   # P = Phone
    "Postage/Shipping":                "X",   # X = shipping
    "Admin Supplies":                  "U",   # U = supplies
    "Lab Supplies":                    "K",   # K = lab
    "Field Service Supplies":          "V",   # V = service
    "Assets":                          "Z",
    "Other":                           "O",
    "Meals/Snacks":                    "M",
    "Breakfast":                       "B",
    "Lunch":                           "L",
    "Dinner":                          "D",
    "Entertainment":                   "E",
}

# Short bucket labels for the badge — full bucket names like
# "Airfare/Bus/Ferry/Other" overflow the cell width at 7pt mono. The
# auditor can still identify the bucket from the prefix letter + the
# short form; the exact long name is also on the XLSX itself via the
# WKS NN-NN, ROW N reference under the badge.
_BUCKET_SHORT_LABEL: dict[str, str] = {
    "Airfare/Bus/Ferry/Other":         "AIRFARE/BUS",
    "Hotel/Lodging/Laundry":           "HOTEL/LODGING",
    "Auto Rental":                     "AUTO RENTAL",
    "Auto Gasoline":                   "AUTO GASOLINE",
    "Taxi/Parking/Tolls/Uber":         "TAXI/PARKING",
    "Other Travel Related":            "OTHER TRAVEL",
    "Membership/Subscription Fees":    "MEMBERSHIP",
    "Customer Gifts":                  "CUSTOMER GIFTS",
    "Telephone/Internet":              "TEL/INTERNET",
    "Postage/Shipping":                "POSTAGE",
    "Admin Supplies":                  "ADMIN SUPPLY",
    "Lab Supplies":                    "LAB SUPPLY",
    "Field Service Supplies":          "FIELD SVC",
    "Assets":                          "ASSETS",
    "Other":                           "OTHER",
    "Meals/Snacks":                    "MEALS/SNACKS",
    "Breakfast":                       "BREAKFAST",
    "Lunch":                           "LUNCH",
    "Dinner":                          "DINNER",
    "Entertainment":                   "ENTERTAIN",
}

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
    # Local-currency anchor for the banner_grid layout (Bug 4). Reflects the
    # BMO-side authoritative amount — e.g., a receipt-side OCR'd 250.00 TRY
    # plus the report's USD-converted 6.12 lets the banner show
    # "USD $6.12 | TRY 250.00" so an auditor sees both sides at a glance.
    # Default None so callers that don't know the local amount (legacy
    # call sites, tests built before Bug 4) keep working.
    local_amount: float | None = None
    local_currency: str | None = None


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
# Banner fonts for the banner_grid layout (Bug 4). Sized for the
# BANNER_HEIGHT_PX of 160; tuned so two lines of text fit with comfortable
# padding at the cell width of ~800 px.
FONT_BANNER_AMOUNT = _font("arialbd.ttf", 44)  # line 1: "USD $X.XX | TRY YYY.YY"
FONT_BANNER_META = _font("arial.ttf", 30)      # line 2: "DATE | SUPPLIER | B/P"

# Paired-card fonts (Layout D). Sizes scaled from the design's pt sizes at
# 300 DPI: 1pt ≈ 4.17 px. Helvetica Neue / Arial bold + JetBrains Mono
# regular & bold. Falls back to ImageFont.load_default if no .ttf installed
# (CI/headless containers without system fonts).
def _font_first_available(names: tuple[str, ...], size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Try each font name in order; return the first that loads, else default.

    Lets us prefer JetBrains Mono if present (matches the design exactly),
    fall through to Roboto Mono / Menlo / Consolas / generic mono as
    available on the host. Important on Windows-only dev boxes that don't
    have JetBrains Mono installed by default.
    """
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()

_SANS_BOLD = ("Helvetica-Bold.ttf", "HelveticaNeue-Bold.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf")
_SANS = ("Helvetica.ttf", "HelveticaNeue.ttf", "arial.ttf", "DejaVuSans.ttf")
_MONO_BOLD = ("JetBrainsMono-Bold.ttf", "JetBrainsMonoNL-Bold.ttf", "RobotoMono-Bold.ttf", "consolab.ttf", "DejaVuSansMono-Bold.ttf")
_MONO = ("JetBrainsMono-Regular.ttf", "JetBrainsMonoNL-Regular.ttf", "RobotoMono-Regular.ttf", "consola.ttf", "DejaVuSansMono.ttf")

# Card body fonts (per design D)
FONT_PC_AMOUNT_USD = _font_first_available(_MONO_BOLD, 42)    # 10pt mono bold
FONT_PC_AMOUNT_TRY = _font_first_available(_MONO_BOLD, 35)    # 8.5pt mono bold
FONT_PC_DATE = _font_first_available(_MONO, 35)               # 8.5pt mono
FONT_PC_SUPPLIER = _font_first_available(_SANS_BOLD, 28)      # 6.7pt sans bold uppercase (tightened from 8.5pt for narrow cells)
FONT_PC_BUCKET = _font_first_available(_SANS, 29)             # 7pt uppercase
FONT_PC_TAG = _font_first_available(_MONO, 29)                # 7pt mono uppercase
FONT_PC_GROUP = _font_first_available(_MONO_BOLD, 31)         # 7.5pt mono bold uppercase
FONT_PC_CORNER_ID = _font_first_available(_MONO, 27)          # 6.5pt mono uppercase

# Page chrome fonts
FONT_PC_RUNHEAD = _font_first_available(_MONO, 31)            # 7.5pt mono uppercase
FONT_PC_RUNFOOT = _font_first_available(_MONO, 29)            # 7pt mono uppercase
FONT_PC_BAND_TITLE = _font_first_available(_SANS_BOLD, 42)    # 10pt sans bold uppercase
FONT_PC_BAND_META = _font_first_available(_MONO, 31)          # 7.5pt mono uppercase
FONT_PC_FULL_AMOUNT = _font_first_available(_MONO_BOLD, 54)   # 13pt mono bold (full-page exception)
FONT_PC_FULL_LOCAL = _font_first_available(_MONO_BOLD, 42)    # 10pt
FONT_PC_FULL_DATE = _font_first_available(_MONO, 38)          # 9pt
FONT_PC_FULL_SUPPLIER = _font_first_available(_SANS_BOLD, 42) # 10pt sans bold
FONT_PC_FULL_BUCKET = _font_first_available(_SANS, 33)        # 8pt
FONT_PC_FULL_NOTE_ITALIC = _font_first_available(_SANS, 35)   # 8.5pt italic-ish


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


# ─── Banner-grid layout (Bug 4 — Carolyn's reference) ─────────────────────────


def _format_banner_amount_line(line: ReceiptAnnotationLine) -> str:
    """Top banner line: 'USD $X.XX | TRY YYY.YY' when both currencies known.

    Falls back gracefully:
      - Both: ``'USD $5.21 | TRY 119.34'``
      - Local only (currency != USD, no usd amount): ``'TRY 119.34'``
      - USD only: ``'USD $5.21'``
      - Neither: ``''`` (empty banner line; unusual)

    The report-currency leg uses ``amount`` + ``currency`` (which is USD
    in the diners_statement flow). The local-currency leg uses
    ``local_amount`` + ``local_currency``, populated from the BMO
    statement-side authoritative amount in _confirmed_lines.
    """
    parts: list[str] = []
    if line.amount is not None and line.currency:
        if line.currency.upper() == "USD":
            parts.append(f"USD ${float(line.amount):.2f}")
        else:
            parts.append(f"{line.currency} {float(line.amount):.2f}")
    if (
        line.local_amount is not None
        and line.local_currency
        and line.local_currency.upper() != (line.currency or "").upper()
    ):
        parts.append(f"{line.local_currency} {float(line.local_amount):.2f}")
    return " | ".join(parts)


def _format_banner_meta_line(line: ReceiptAnnotationLine) -> str:
    """Bottom banner line: 'YYYY-MM-DD | SUPPLIER | Business' (or Personal).

    Truncated supplier names get the ``shorten`` treatment to fit within
    the cell width — the green banner is bounded by CELL_WIDTH; long
    chain names like 'İKBAL LOKANTACILIK / ZEY SPORT SPOR MAL. SAN.' are
    visually cut at ~40 chars.
    """
    bp = (line.business_or_personal or "").strip()
    supplier = shorten(line.supplier or "", width=42, placeholder="…")
    parts = [line.transaction_date.isoformat(), supplier]
    if bp:
        parts.append(bp)
    return " | ".join(parts)


def _draw_thin_border(img: Image.Image) -> Image.Image:
    """2px gray hairline around a thumbnail — replaces the old 12px
    color-coded BORDER_WIDTH from day_grouped_colored. Cheap visual
    separation between adjacent cells in the 3x3 grid.
    """
    bordered = img.copy()
    draw = ImageDraw.Draw(bordered)
    w, h = bordered.size
    draw.rectangle(
        (0, 0, w - 1, h - 1),
        outline=THIN_BORDER_HEX,
        width=THIN_BORDER_WIDTH_PX,
    )
    return bordered


def _make_banner_thumbnail(
    receipt_img: Image.Image,
    line: ReceiptAnnotationLine,
) -> Image.Image:
    """Render one cell of the 3x3 grid: receipt thumbnail + green top banner.

    The banner overlays the upper BANNER_HEIGHT_PX of the cell. The
    receipt image is letterboxed to fill the rest of the cell (preserving
    aspect ratio); a 2px gray border separates this cell from neighbors.
    """
    canvas = Image.new("RGB", (CELL_WIDTH, CELL_HEIGHT), "white")

    # Receipt fills the whole cell; banner composites on top of it later.
    img = receipt_img.copy()
    img.thumbnail((CELL_WIDTH, CELL_HEIGHT), Image.Resampling.LANCZOS)
    img_x = (CELL_WIDTH - img.width) // 2
    img_y = (CELL_HEIGHT - img.height) // 2
    canvas.paste(img, (img_x, img_y))

    # Green banner overlay across full top of cell.
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, CELL_WIDTH, BANNER_HEIGHT_PX), fill=BANNER_BG_HEX)
    draw.text(
        (BANNER_INNER_PAD_X, BANNER_INNER_PAD_Y_TOP),
        _format_banner_amount_line(line),
        fill=BANNER_TEXT_COLOR,
        font=FONT_BANNER_AMOUNT,
    )
    draw.text(
        (BANNER_INNER_PAD_X, BANNER_INNER_PAD_Y_TOP + BANNER_LINE_GAP_PX),
        _format_banner_meta_line(line),
        fill=BANNER_TEXT_COLOR,
        font=FONT_BANNER_META,
    )

    return _draw_thin_border(canvas)


def _render_full_page_banner_continuation(
    img: Image.Image,
    line: ReceiptAnnotationLine,
    *,
    page_num: int,
    total_pages: int,
) -> Image.Image:
    """A full-page A4 page for a multi-page receipt's pages 2..N.

    Same green banner as on the grid cell, but at the top of a full A4
    page so multi-page hotel folios remain readable without being shrunk
    to thumbnail size. Banner shows '… (page N of M)' so the auditor
    knows where they are.
    """
    page = Image.new("RGB", (A4_WIDTH, A4_HEIGHT), "white")
    big = img.copy()
    big.thumbnail(
        (A4_WIDTH - 2 * MARGIN_X, A4_HEIGHT - 2 * MARGIN_Y - BANNER_HEIGHT_PX),
        Image.Resampling.LANCZOS,
    )
    px = (A4_WIDTH - big.width) // 2
    py = MARGIN_Y + BANNER_HEIGHT_PX
    page.paste(big, (px, py))

    draw = ImageDraw.Draw(page)
    # Banner spans full page width (not cell width).
    draw.rectangle((0, 0, A4_WIDTH, BANNER_HEIGHT_PX), fill=BANNER_BG_HEX)
    draw.text(
        (BANNER_INNER_PAD_X * 2, BANNER_INNER_PAD_Y_TOP),
        _format_banner_amount_line(line),
        fill=BANNER_TEXT_COLOR,
        font=FONT_BANNER_AMOUNT,
    )
    draw.text(
        (BANNER_INNER_PAD_X * 2, BANNER_INNER_PAD_Y_TOP + BANNER_LINE_GAP_PX),
        f"{_format_banner_meta_line(line)}  (page {page_num} of {total_pages})",
        fill=BANNER_TEXT_COLOR,
        font=FONT_BANNER_META,
    )
    return page


def _render_banner_grid_layout(
    lines: list[ReceiptAnnotationLine],
    output_path: Path,
) -> int:
    """3x3 grid PDF with green banners — the Carolyn-approved layout.

    Sort order: ``(transaction_date, receipt_id)`` — natural chronological
    ordering with stable tie-break for same-day receipts. PM's grouping
    rule (receipts contributing to one report line stay on the same page)
    is satisfied for the November dataset by date sorting alone, since
    same-(supplier, code, date) groups are contiguous after sort.

    Multi-page receipts (e.g. hotel folios): page 1 of the receipt goes
    in the grid as a thumbnail; pages 2..N are emitted as full-A4 pages
    AFTER the grid page they belong to, using the same banner. This
    matches the prior day_grouped_colored behavior so hotel folios stay
    readable.

    No legend page, no color-coded borders. Each thumbnail's green banner
    is a self-contained cross-reference (amount, date, supplier, B/P) so
    the auditor doesn't need a separate legend.
    """
    ordered = sorted(
        lines,
        key=lambda ln: (ln.transaction_date, ln.receipt_id or 0),
    )
    pages: list[Image.Image] = []

    for offset in range(0, len(ordered), COLUMNS * ROWS):
        batch = ordered[offset : offset + COLUMNS * ROWS]
        page = Image.new("RGB", (A4_WIDTH, A4_HEIGHT), "white")

        # Lay out 3x3 grid; track multi-page receipts encountered for
        # post-grid full-page emission.
        multi_page_extras: list[Image.Image] = []
        for idx, line in enumerate(batch):
            cell_pages = _load_receipt_pages(line) or [_placeholder_tile(line, "no file")]
            thumb = _make_banner_thumbnail(cell_pages[0], line)
            col = idx % COLUMNS
            row = idx // COLUMNS
            x0 = MARGIN_X + col * (CELL_WIDTH + GAP_X)
            y0 = MARGIN_Y + row * (CELL_HEIGHT + GAP_Y)
            x = x0 + (CELL_WIDTH - thumb.width) // 2
            y = y0 + (CELL_HEIGHT - thumb.height) // 2
            page.paste(thumb, (x, y))

            if len(cell_pages) > 1:
                total = len(cell_pages)
                for extra_idx, extra_img in enumerate(cell_pages[1:], start=2):
                    multi_page_extras.append(
                        _render_full_page_banner_continuation(
                            extra_img, line,
                            page_num=extra_idx, total_pages=total,
                        )
                    )

        pages.append(page)
        # Emit any multi-page receipt continuations right after the grid
        # page they belong to, so the hotel folio stays adjacent to its
        # thumbnail.
        pages.extend(multi_page_extras)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not pages:
        # Defensive — should be impossible given the non-empty check upstream.
        pages = [Image.new("RGB", (A4_WIDTH, A4_HEIGHT), "white")]
    pages[0].save(output_path, save_all=True, append_images=pages[1:])
    return len(pages)


# ─── Paired-card layout (Layout D — Claude Design handoff) ───────────────────


@dataclass(frozen=False)
class _PairedCardContext:
    """One receipt in the paired-card stream.

    Mutable so we can attach the short_id ("R01" etc.) after sorting + the
    multi-page flag after probing the file. ``group_*`` fields describe the
    XLSX line this receipt belongs to: ``group_count`` is how many receipts
    share the same review_row_id (and therefore the same XLSX row), and
    ``group_total_usd`` is the sum of those receipts' USD amounts. When
    ``group_count == 1``, the bottom of the card omits the GROUP X/Y line.
    """
    line: ReceiptAnnotationLine
    group_index: int
    group_count: int
    group_total_usd: float
    xlsx_ref: str
    short_id: str = ""
    multi_page: bool = False


def _bucket_to_xlsx_ref(bucket: str | None, week_label: str) -> str:
    """Format the under-bucket label, e.g. ``'WKS 41-43, ROW 32'``.

    Falls back to ``ROW 26`` (the catch-all "Other" row on Page 1A) when
    the bucket isn't in the static map — e.g. operator typed a custom
    bucket name during a manual edit.
    """
    row = _BUCKET_TO_PAGE_1A_ROW.get((bucket or "").strip(), 26)
    return f"WKS {week_label}, ROW {row}"


def _iso_week_label(lines: list[ReceiptAnnotationLine]) -> str:
    """Return e.g. ``'41-43'`` for the ISO-week range spanning the receipts.

    Single-week input returns just ``'41'``. Used in the page chrome and
    in each card's xlsx_ref label.
    """
    if not lines:
        return "??"
    weeks = sorted({ln.transaction_date.isocalendar()[1] for ln in lines})
    if len(weeks) == 1:
        return f"{weeks[0]}"
    return f"{weeks[0]}-{weeks[-1]}"


def _truncate(text: str, max_chars: int) -> str:
    """Tail-cut with ellipsis for cell-bound strings (supplier name, etc.).

    Mirrors the design's ``truncate(s, n=25)`` helper. We use a real
    ellipsis character (single glyph) so the rendered width is more
    predictable than ``...``.
    """
    if not text:
        return ""
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def _draw_text_safe(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    *,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: str = INK_HEX,
) -> None:
    """Wrapper around ``draw.text`` that swallows None/empty silently.

    Lets card-rendering code stay readable without ``if value:`` guards
    around every optional field.
    """
    if not text:
        return
    draw.text(xy, text, fill=fill, font=font)


def _is_hotel_bucket(bucket: str | None) -> bool:
    return (bucket or "").strip() in _HOTEL_BUCKETS


def _detect_multi_page(line: ReceiptAnnotationLine) -> bool:
    """True iff the receipt is a multi-page PDF (hotel folio)."""
    pages = _load_receipt_pages(line)
    return bool(pages) and len(pages) > 1


def _build_paired_card_stream(
    lines: list[ReceiptAnnotationLine],
    week_label: str,
) -> list[_PairedCardContext]:
    """Build the paired-card stream with group context attached.

    Group key: ``review_row_id`` — receipts that share an XLSX line (e.g.
    a split-bill dinner across two card transactions) appear with
    ``group_count > 1`` and matching ``group_total_usd``. Receipts with no
    review_row_id are grouped each-in-its-own-row (singleton groups).

    Returned list is sorted by (transaction_date, receipt_id) so the
    paginator can chunk in chronological order. ``short_id`` ("R01"…) is
    assigned after sort.
    """
    by_row: dict[object, list[ReceiptAnnotationLine]] = {}
    for line in lines:
        # Use review_row_id as the group key; receipts without one are
        # treated as their own singleton group (id-keyed by transaction_id).
        key = line.review_row_id if line.review_row_id is not None else f"_solo_{line.transaction_id}"
        by_row.setdefault(key, []).append(line)

    stream: list[_PairedCardContext] = []
    for members in by_row.values():
        members.sort(key=lambda m: (m.transaction_date, m.receipt_id or 0))
        group_total = sum(float(m.amount) for m in members)
        for idx, member in enumerate(members, start=1):
            stream.append(_PairedCardContext(
                line=member,
                group_index=idx,
                group_count=len(members),
                group_total_usd=group_total,
                xlsx_ref=_bucket_to_xlsx_ref(member.report_bucket, week_label),
            ))

    stream.sort(key=lambda c: (c.line.transaction_date, c.line.receipt_id or 0))

    for i, ctx in enumerate(stream, start=1):
        ctx.short_id = f"R{i:02d}"
        ctx.multi_page = _detect_multi_page(ctx.line)

    return stream


def _format_pc_amount_usd(line: ReceiptAnnotationLine) -> str:
    """Top of card: 'USD $X.XX' (mono bold). Currency-agnostic fallback."""
    amt = float(line.amount or 0.0)
    if (line.currency or "").upper() == "USD":
        return f"USD ${amt:,.2f}"
    return f"{line.currency or ''} {amt:,.2f}".strip()


def _format_pc_amount_local(line: ReceiptAnnotationLine) -> str:
    """Second line: 'TRY YYY.YY' if local-currency anchor differs from
    report currency. Empty when the local leg matches the USD leg or is
    absent (manual entry receipts).
    """
    if line.local_amount is None or not line.local_currency:
        return ""
    if (line.local_currency or "").upper() == (line.currency or "").upper():
        return ""
    return f"{line.local_currency} {float(line.local_amount):,.2f}"


PC_SUPPLIER_LINE_CHARS = 13     # ~311 px / 28-px sans-bold ≈ 14 chars; bound at 13 for wide-glyph headroom (M/W/letters with diacritics)
PC_SUPPLIER_MAX_LINES = 2


def _wrap_pc_supplier(line: ReceiptAnnotationLine) -> list[str]:
    """Wrap supplier name to up to two lines for the right info column.

    Excel-style ``wrap_text``: split at the latest whitespace boundary that
    keeps the first line within ``PC_SUPPLIER_LINE_CHARS``. If the
    remainder still overflows the second line, it is truncated with an
    ellipsis. Single very-long words (no whitespace) fall back to a
    one-line truncation since wrapping mid-word would look wrong.
    """
    text = (line.supplier or "").upper().strip()
    if not text:
        return [""]
    if len(text) <= PC_SUPPLIER_LINE_CHARS:
        return [text]

    words = text.split()
    if len(words) == 1:
        return [_truncate(text, PC_SUPPLIER_LINE_CHARS + 1)]

    # Greedy: pick the largest prefix of words that still fits.
    split_at = 0
    for i in range(1, len(words)):
        candidate = " ".join(words[:i])
        if len(candidate) <= PC_SUPPLIER_LINE_CHARS:
            split_at = i
        else:
            break

    if split_at == 0:
        # First word alone overflows — fall back to one-line truncation.
        return [_truncate(text, PC_SUPPLIER_LINE_CHARS + 1)]

    line1 = " ".join(words[:split_at])
    line2 = " ".join(words[split_at:])
    if len(line2) > PC_SUPPLIER_LINE_CHARS:
        line2 = _truncate(line2, PC_SUPPLIER_LINE_CHARS + 1)
    return [line1, line2]


def _draw_paired_card(
    page: Image.Image,
    ctx: _PairedCardContext,
    *,
    x: int,
    y: int,
    w: int,
    h: int,
) -> None:
    """Render one paired card at (x, y) with width w and height h.

    Card border: 1pt ink. Vertical divider between left thumb and right
    info column: 0.75pt ink. Two halves are 50/50 by design (PC_THUMB_SIDE_PCT).

    Right info column field stack (top to bottom):
      1. Amount line (USD $X.XX)
      2. Local-currency line (TRY YYY.YY) — omitted when local==report
      3. Soft hairline rule
      4. Date (YYYY-MM-DD)
      5. Supplier (uppercase, Excel-style wrap to 1 or 2 lines)
      6. Bucket — WKS NN-NN, ROW N (uppercase, gray)
      7. (flex spacer)
      8. BUSINESS / PERSONAL tag
      9. (only if group_count >= 2) GROUP X/Y — TOTAL $XX.XX with 1pt top rule

    Supplier wrapping consumes one or two lines (35 px each); the bucket
    badge below it shifts down by ~38 px when wrap is active so the field
    stack stays readable. Bottom-anchored elements (BP tag, group line)
    are unaffected.
    """
    draw = ImageDraw.Draw(page)
    line = ctx.line

    # Card border.
    draw.rectangle((x, y, x + w - 1, y + h - 1), outline=INK_HEX, width=PC_CARD_BORDER_WIDTH)

    # Left half — receipt thumbnail (centered, letterboxed) + corner ID.
    thumb_w = int(w * PC_THUMB_SIDE_PCT)
    info_x = x + thumb_w
    # Vertical divider between halves.
    draw.line(
        ((info_x, y + 1), (info_x, y + h - 2)),
        fill=INK_HEX, width=2,
    )

    # Receipt image: load page 1, scale into an inset box (~78% of half-width)
    # matching the design's letterbox proportions.
    receipt_pages = _load_receipt_pages(line) or [_placeholder_tile(line, "no file")]
    inset_w = int(thumb_w * 0.78)
    inset_h = h - 50
    img = receipt_pages[0].copy()
    img.thumbnail((inset_w, inset_h), Image.Resampling.LANCZOS)
    img_x = x + (thumb_w - img.width) // 2
    img_y = y + (h - img.height) // 2
    page.paste(img, (img_x, img_y))

    # Corner ID badge (top-left of left half), e.g. "R01"
    _draw_text_safe(
        draw, (x + 14, y + 14),
        ctx.short_id,
        font=FONT_PC_CORNER_ID,
        fill=INK_3_HEX,
    )

    # Hotel folio "FOLIO" badge — top-right corner of the card. Single-page
    # hotel receipts now stay in the grid (post-FIX-2); the corner badge
    # is the visual cue that this card is a hotel folio so the auditor can
    # spot it without reading the bucket badge inside.
    if _is_hotel_bucket(line.report_bucket):
        folio_text = "FOLIO"
        # Compute the badge box size using the same padding as
        # _draw_paired_card_tag so the badge sits flush against the card's
        # top-right inside corner.
        bbox = draw.textbbox((0, 0), folio_text, font=FONT_PC_TAG)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        pad_x, pad_y = 14, 6
        badge_w = text_w + 2 * pad_x
        badge_h = text_h + 2 * pad_y
        bx = x + w - badge_w - 12
        by = y + 12
        draw.rectangle(
            (bx, by, bx + badge_w, by + badge_h),
            fill=INK_HEX, outline=INK_HEX, width=2,
        )
        draw.text(
            (bx + pad_x, by + pad_y - 4),
            folio_text, fill=PAPER_HEX, font=FONT_PC_TAG,
        )

    # Right half — info column.
    cx = info_x + PC_INFO_PADDING_X
    cy = y + PC_INFO_PADDING_Y
    info_right = x + w - PC_INFO_PADDING_X

    # 1. USD amount (10pt mono bold)
    _draw_text_safe(draw, (cx, cy), _format_pc_amount_usd(line), font=FONT_PC_AMOUNT_USD)
    cy += 50

    # 2. Local-currency amount (8.5pt mono bold) — when present + different.
    local = _format_pc_amount_local(line)
    if local:
        _draw_text_safe(draw, (cx, cy), local, font=FONT_PC_AMOUNT_TRY)
    cy += 42

    # 3. Soft hairline rule.
    draw.line(((cx, cy), (info_right, cy)), fill=RULE_SOFT_HEX, width=1)
    cy += 22

    # 4. Date.
    _draw_text_safe(draw, (cx, cy), line.transaction_date.isoformat(), font=FONT_PC_DATE)
    cy += 50

    # 5. Supplier (uppercase, Excel-style wrap up to 2 lines).
    supplier_lines = _wrap_pc_supplier(line)
    for i, supplier_line in enumerate(supplier_lines):
        _draw_text_safe(draw, (cx, cy + i * 32), supplier_line, font=FONT_PC_SUPPLIER)
    cy += 50 + (len(supplier_lines) - 1) * 32

    # 6. Bucket badge (solid black tag with white text) — visually distinct
    #    so the auditor's eye lands on the category. Format: "[X] LABEL"
    #    where X is the single-letter EDT code (F = Fuel, M = Meals, etc.).
    code = _BUCKET_LETTER_CODE.get(line.report_bucket or "", "O")
    label = _BUCKET_SHORT_LABEL.get(line.report_bucket or "", (line.report_bucket or "OTHER").upper())
    badge_text = f"[{code}] {label}"
    _draw_paired_card_tag(draw, x=cx, y=cy, text=badge_text, solid=True)
    cy += 50

    # 7. WKS NN-NN, ROW N — plain meta line beneath the badge.
    _draw_text_safe(draw, (cx, cy), ctx.xlsx_ref, font=FONT_PC_BUCKET, fill=INK_3_HEX)

    # — Bottom-anchored: BUSINESS/PERSONAL tag + optional GROUP line —
    # Tag and group line live near the bottom of the card; compute up from y+h.
    bottom = y + h - PC_INFO_PADDING_Y
    if ctx.group_count >= 2:
        # Group line height: ~38px text + 8px top rule + 8px gap.
        group_block_h = 60
        group_y = bottom - group_block_h
        # 1pt top rule.
        draw.line(((cx, group_y), (info_right, group_y)), fill=INK_HEX, width=3)
        group_text = (
            f"GROUP {ctx.group_index}/{ctx.group_count} — "
            f"TOTAL ${ctx.group_total_usd:,.2f}"
        )
        _draw_text_safe(draw, (cx, group_y + 18), group_text, font=FONT_PC_GROUP)
        bottom = group_y - 10

    # Tag (above group line if present).
    bp = (line.business_or_personal or "").strip().upper() or "BUSINESS"
    tag_text = "BUSINESS" if bp == "BUSINESS" else "PERSONAL"
    tag_y = bottom - 38
    _draw_paired_card_tag(
        draw,
        x=cx, y=tag_y,
        text=tag_text,
        solid=(tag_text == "BUSINESS"),
    )


def _draw_paired_card_tag(
    draw: ImageDraw.ImageDraw,
    *,
    x: int,
    y: int,
    text: str,
    solid: bool,
) -> None:
    """Draw a small uppercase tag like 'BUSINESS' (solid black) or
    'PERSONAL' (outline only). 0.5pt border, 1px×5px padding per design.
    """
    pad_x, pad_y = 14, 6
    bbox = draw.textbbox((0, 0), text, font=FONT_PC_TAG)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    box_w = text_w + 2 * pad_x
    box_h = text_h + 2 * pad_y

    if solid:
        draw.rectangle(
            (x, y, x + box_w, y + box_h),
            fill=INK_HEX, outline=INK_HEX, width=2,
        )
        draw.text((x + pad_x, y + pad_y - 4), text, fill=PAPER_HEX, font=FONT_PC_TAG)
    else:
        draw.rectangle(
            (x, y, x + box_w, y + box_h),
            fill=PAPER_HEX, outline=INK_HEX, width=2,
        )
        draw.text((x + pad_x, y + pad_y - 4), text, fill=INK_HEX, font=FONT_PC_TAG)


def _draw_paired_card_chrome(
    page: Image.Image,
    *,
    page_no: int,
    page_of: int,
    layout_tag: str,
    period_label: str,
    receipt_count: int,
    total_usd: float,
    employee: str,
    report_no: str,
) -> None:
    """Running header (top) + footer (bottom) drawn on every page.

    Header: "EDT EXPENSE ANNEX · {report_no} · {employee}" left;
            "{layout_tag} · BUSINESS · PAGE X/Y" right.
    Footer: "PREPARED {date} · {N} RECEIPTS · ${TOTAL}" left; "X / Y" right.
    """
    draw = ImageDraw.Draw(page)
    today_iso = date.today().isoformat()

    head_left = f"EDT EXPENSE ANNEX · {report_no} · {employee}".upper()
    head_right = f"{layout_tag} · BUSINESS · PAGE {page_no}/{page_of}"
    _draw_text_safe(draw, (PC_MARGIN_X, PC_HEADER_TOP_Y), head_left,
                    font=FONT_PC_RUNHEAD, fill=INK_3_HEX)

    head_right_w = draw.textlength(head_right, font=FONT_PC_RUNHEAD)
    _draw_text_safe(draw,
                    (A4_WIDTH - PC_MARGIN_X - int(head_right_w), PC_HEADER_TOP_Y),
                    head_right, font=FONT_PC_RUNHEAD, fill=INK_3_HEX)

    # Footer
    foot_left = (
        f"PREPARED {today_iso} · {receipt_count} RECEIPTS · ${total_usd:,.2f}"
    )
    foot_right = f"{page_no} / {page_of}"
    _draw_text_safe(draw, (PC_MARGIN_X, A4_HEIGHT - PC_FOOTER_BOTTOM_Y - 30),
                    foot_left, font=FONT_PC_RUNFOOT, fill=INK_3_HEX)
    foot_right_w = draw.textlength(foot_right, font=FONT_PC_RUNFOOT)
    _draw_text_safe(draw,
                    (A4_WIDTH - PC_MARGIN_X - int(foot_right_w),
                     A4_HEIGHT - PC_FOOTER_BOTTOM_Y - 30),
                    foot_right, font=FONT_PC_RUNFOOT, fill=INK_3_HEX)

    # RECEIPT ANNEX band
    band_y = PC_HEADER_BAND_Y + 50
    _draw_text_safe(draw, (PC_MARGIN_X, band_y), "RECEIPT ANNEX",
                    font=FONT_PC_BAND_TITLE)
    band_meta = f"WKS {period_label} · {receipt_count} RECEIPTS · ${total_usd:,.2f}"
    band_meta_w = draw.textlength(band_meta, font=FONT_PC_BAND_META)
    _draw_text_safe(draw,
                    (A4_WIDTH - PC_MARGIN_X - int(band_meta_w), band_y + 16),
                    band_meta, font=FONT_PC_BAND_META, fill=INK_2_HEX)
    # 1.5pt rule beneath the band.
    rule_y = band_y + 60
    draw.rectangle(
        (PC_MARGIN_X, rule_y, A4_WIDTH - PC_MARGIN_X, rule_y + 4),
        fill=INK_HEX,
    )


def _render_paired_card_grid_page(
    chunk: list[_PairedCardContext],
    *,
    page_no: int,
    page_of: int,
    period_label: str,
    receipt_count: int,
    total_usd: float,
    employee: str,
    report_no: str,
) -> Image.Image:
    """Render a 3x3 grid page with up to 9 paired cards."""
    page = Image.new("RGB", (A4_WIDTH, A4_HEIGHT), PAPER_HEX)
    _draw_paired_card_chrome(
        page, page_no=page_no, page_of=page_of,
        layout_tag="LAYOUT D · PAIRED CARD",
        period_label=period_label,
        receipt_count=receipt_count, total_usd=total_usd,
        employee=employee, report_no=report_no,
    )

    grid_top = PC_GRID_TOP_Y
    for idx, ctx in enumerate(chunk[:9]):
        col = idx % PC_COLS
        row = idx // PC_COLS
        cx = PC_MARGIN_X + col * (PC_CELL_WIDTH + PC_GAP_PX)
        cy = grid_top + row * (PC_CELL_HEIGHT + PC_GAP_PX)
        _draw_paired_card(page, ctx, x=cx, y=cy, w=PC_CELL_WIDTH, h=PC_CELL_HEIGHT)
    return page


def _render_paired_card_full_page(
    ctx: _PairedCardContext,
    *,
    page_no: int,
    page_of: int,
    period_label: str,
    receipt_count: int,
    total_usd: float,
    employee: str,
    report_no: str,
    reason: str,
) -> Image.Image:
    """Full-A4 single-receipt page for hotel folios + multi-page receipts.

    Top: header chrome (same as grid pages, but with a "FULL-PAGE EXCEPTION"
    sub-band carrying the reason). Then a 3-column info strip with
    amount/date/supplier/group. Then a large thumbnail filling the rest.
    """
    page = Image.new("RGB", (A4_WIDTH, A4_HEIGHT), PAPER_HEX)
    _draw_paired_card_chrome(
        page, page_no=page_no, page_of=page_of,
        layout_tag="LAYOUT D · FULL-PAGE RECEIPT",
        period_label=period_label,
        receipt_count=receipt_count, total_usd=total_usd,
        employee=employee, report_no=report_no,
    )

    draw = ImageDraw.Draw(page)
    line = ctx.line

    # Sub-band: "FULL-PAGE EXCEPTION ... REASON: <reason>"
    sub_y = PC_GRID_TOP_Y + 20
    _draw_text_safe(draw, (PC_MARGIN_X, sub_y),
                    "RECEIPT ANNEX · FULL-PAGE EXCEPTION",
                    font=FONT_PC_BAND_TITLE)
    reason_text = f"REASON: {reason}"
    reason_w = draw.textlength(reason_text, font=FONT_PC_BAND_META)
    _draw_text_safe(draw,
                    (A4_WIDTH - PC_MARGIN_X - int(reason_w), sub_y + 16),
                    reason_text, font=FONT_PC_BAND_META, fill=INK_2_HEX)
    rule_y = sub_y + 60
    draw.rectangle(
        (PC_MARGIN_X, rule_y, A4_WIDTH - PC_MARGIN_X, rule_y + 4),
        fill=INK_HEX,
    )

    # Info strip (3-column). Border 1pt; padding 14×10; 14px gap.
    strip_top = rule_y + 30
    strip_h = 280
    strip_left = PC_MARGIN_X
    strip_right = A4_WIDTH - PC_MARGIN_X
    draw.rectangle(
        (strip_left, strip_top, strip_right, strip_top + strip_h),
        outline=INK_HEX, width=PC_CARD_BORDER_WIDTH,
    )
    col_w = (strip_right - strip_left) // 3

    # Column 1: amount + date
    c1x = strip_left + 30
    c1y = strip_top + 30
    _draw_text_safe(draw, (c1x, c1y), _format_pc_amount_usd(line),
                    font=FONT_PC_FULL_AMOUNT)
    local = _format_pc_amount_local(line)
    if local:
        _draw_text_safe(draw, (c1x, c1y + 70), local, font=FONT_PC_FULL_LOCAL)
    _draw_text_safe(draw, (c1x, c1y + 140),
                    line.transaction_date.isoformat(),
                    font=FONT_PC_FULL_DATE, fill=INK_2_HEX)

    # Column 2: supplier + bucket + tag
    c2x = strip_left + col_w + 30
    c2y = strip_top + 30
    _draw_text_safe(draw, (c2x, c2y), _truncate((line.supplier or "").upper(), 32),
                    font=FONT_PC_FULL_SUPPLIER)
    bucket_label = (line.report_bucket or "OTHER").upper()
    _draw_text_safe(draw, (c2x, c2y + 60),
                    f"{bucket_label} — {ctx.xlsx_ref}",
                    font=FONT_PC_FULL_BUCKET, fill=INK_2_HEX)
    bp = (line.business_or_personal or "").strip().upper()
    _draw_paired_card_tag(
        draw, x=c2x, y=c2y + 130,
        text="PERSONAL" if bp == "PERSONAL" else "BUSINESS",
        solid=bp != "PERSONAL",
    )

    # Column 3: GROUP X/Y or "SOLE RECEIPT FOR LINE"
    c3x = strip_left + 2 * col_w + 30
    c3y = strip_top + 30
    if ctx.group_count >= 2:
        text = (
            f"GROUP {ctx.group_index}/{ctx.group_count} — "
            f"TOTAL ${ctx.group_total_usd:,.2f}"
        )
        _draw_text_safe(draw, (c3x, c3y), text, font=FONT_PC_GROUP)
    else:
        _draw_text_safe(draw, (c3x, c3y), "SOLE RECEIPT FOR LINE",
                        font=FONT_PC_BAND_META, fill=INK_3_HEX)
    if line.business_reason:
        _draw_text_safe(draw, (c3x, c3y + 60),
                        _truncate(line.business_reason, 80),
                        font=FONT_PC_FULL_NOTE_ITALIC, fill=INK_2_HEX)

    # Big thumbnail (multi-page receipts: render the first page; subsequent
    # pages get their own _render_paired_card_full_page calls upstream so
    # each PDF page maps to one receipt page).
    big_top = strip_top + strip_h + 30
    big_bottom = A4_HEIGHT - PC_FOOTER_BOTTOM_Y - 80
    big_h = big_bottom - big_top
    big_left = PC_MARGIN_X
    big_right = A4_WIDTH - PC_MARGIN_X
    big_w = big_right - big_left
    draw.rectangle(
        (big_left, big_top, big_right, big_bottom),
        outline=INK_HEX, width=2,
    )

    pages = _load_receipt_pages(line) or [_placeholder_tile(line, "no file")]
    big_img = pages[0].copy()
    big_img.thumbnail((big_w - 30, big_h - 30), Image.Resampling.LANCZOS)
    place_x = big_left + (big_w - big_img.width) // 2
    place_y = big_top + (big_h - big_img.height) // 2
    page.paste(big_img, (place_x, place_y))

    return page


def _render_paired_card_layout(
    lines: list[ReceiptAnnotationLine],
    output_path: Path,
    *,
    employee: str = "A.H. TASTAN",
    report_no: str = "EDT-2025-WNN",
) -> int:
    """Render the paired-card PDF (Layout D from the Claude Design handoff).

    Pagination strategy:
      1. Build the stream from input lines, attaching group context
         (review_row_id-keyed) and assigning short ids R01..RNN.
      2. Split into ``grid_stream`` (regular receipts) and ``full_stream``
         (hotel folios + multi-page PDFs).
      3. Pack grid into 9-card pages. The last under-9 chunk is held as
         ``remainder``.
      4. If we have an exception AND remainder ≤ 3 cards: emit a MIXED
         page (folio at top, spillover cards beneath). Remaining
         exceptions emit one full-A4 page each.
      5. Otherwise: emit remainder as its own grid page, then exceptions
         as full-A4 pages.

    Worked example matching the design: 11 receipts with one hotel folio →
    1 grid page (9 cards) + 1 mixed page (folio + 1 spillover card OR
    folio + remaining 2 cards). Total 2 pages. The spec says
    "11 receipts → 2 A4 pages MAX" and this honors it.
    """
    if not lines:
        raise ValueError("No receipt lines are available for annotation")

    week_label = _iso_week_label(lines)
    stream = _build_paired_card_stream(lines, week_label)

    # Only MULTI-PAGE receipts (typically hotel folios spanning multiple PDF
    # pages) trigger the full-A4 exception path. Single-page hotel receipts
    # — e.g. a payment-slip-only "45BUSINESSHOTEL" with one card-machine
    # printout — fit fine in the regular 3x3 grid alongside other receipts.
    # The hotel-vs-non-hotel distinction surfaces visually via a small
    # "FOLIO" badge in the card corner (drawn in _draw_paired_card), not
    # via page layout. (Per PM revision on PR #34.)
    grid_stream = [c for c in stream if not c.multi_page]
    full_stream = [c for c in stream if c.multi_page]

    full_grid_pages: list[list[_PairedCardContext]] = []
    remainder: list[_PairedCardContext] = []
    chunk_size = 9
    for i in range(0, len(grid_stream), chunk_size):
        chunk = grid_stream[i : i + chunk_size]
        if len(chunk) == chunk_size:
            full_grid_pages.append(chunk)
        else:
            remainder = chunk

    page_specs: list[tuple] = []
    for chunk in full_grid_pages:
        page_specs.append(("grid", chunk))

    if full_stream and remainder and len(remainder) <= 3:
        # Mixed page: first folio shares with the spillover.
        page_specs.append(("mixed", full_stream[0], remainder))
        for ctx in full_stream[1:]:
            page_specs.append(("full", ctx))
    else:
        if remainder:
            page_specs.append(("grid", remainder))
        for ctx in full_stream:
            page_specs.append(("full", ctx))

    total = len(page_specs)
    if total == 0:
        # No data at all (shouldn't happen given non-empty check above) —
        # emit a single blank page rather than crash on the PDF write.
        total = 1
        page_specs = [("grid", [])]

    receipt_count = len(stream)
    total_usd = sum(float(c.line.amount or 0.0) for c in stream)
    rendered_pages: list[Image.Image] = []

    for n, spec in enumerate(page_specs, start=1):
        common = dict(
            page_no=n, page_of=total,
            period_label=week_label,
            receipt_count=receipt_count,
            total_usd=total_usd,
            employee=employee,
            report_no=report_no,
        )
        if spec[0] == "grid":
            rendered_pages.append(_render_paired_card_grid_page(spec[1], **common))
        elif spec[0] == "mixed":
            rendered_pages.append(_render_paired_card_mixed_page(
                spec[1], spec[2], **common,
            ))
        else:  # "full"
            ctx = spec[1]
            # Reason for the full-A4 exception: it's always a multi-page
            # receipt at this point (single-page hotels stay in the grid
            # post-FIX-2). Sub-classify hotel-vs-other for the header band.
            reason = (
                "HOTEL FOLIO (MULTI-PAGE)"
                if _is_hotel_bucket(ctx.line.report_bucket)
                else "MULTI-PAGE RECEIPT"
            )
            rendered_pages.append(_render_paired_card_full_page(
                ctx, reason=reason, **common,
            ))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not rendered_pages:
        rendered_pages = [Image.new("RGB", (A4_WIDTH, A4_HEIGHT), PAPER_HEX)]
    rendered_pages[0].save(
        output_path, save_all=True, append_images=rendered_pages[1:],
    )
    return len(rendered_pages)


def _render_paired_card_mixed_page(
    folio: _PairedCardContext,
    spill: list[_PairedCardContext],
    *,
    page_no: int,
    page_of: int,
    period_label: str,
    receipt_count: int,
    total_usd: float,
    employee: str,
    report_no: str,
) -> Image.Image:
    """Mixed page: folio at top, spillover cards in a single grid row beneath.

    Used when an exception (hotel folio / multi-page receipt) coexists with
    a grid remainder of ≤ 3 cards. Saves one page over emitting them
    separately.
    """
    page = Image.new("RGB", (A4_WIDTH, A4_HEIGHT), PAPER_HEX)
    _draw_paired_card_chrome(
        page, page_no=page_no, page_of=page_of,
        layout_tag="LAYOUT D · FOLIO + SPILLOVER",
        period_label=period_label,
        receipt_count=receipt_count, total_usd=total_usd,
        employee=employee, report_no=report_no,
    )
    draw = ImageDraw.Draw(page)
    line = folio.line

    # Sub-band identifying the folio reason.
    sub_y = PC_GRID_TOP_Y + 20
    _draw_text_safe(draw, (PC_MARGIN_X, sub_y),
                    "RECEIPT ANNEX · FULL-PAGE EXCEPTION + SPILLOVER",
                    font=FONT_PC_BAND_TITLE)
    reason = "HOTEL FOLIO (MULTI-PAGE)" if folio.multi_page else "HOTEL FOLIO"
    reason_w = draw.textlength(f"REASON: {reason}", font=FONT_PC_BAND_META)
    _draw_text_safe(draw,
                    (A4_WIDTH - PC_MARGIN_X - int(reason_w), sub_y + 16),
                    f"REASON: {reason}", font=FONT_PC_BAND_META, fill=INK_2_HEX)
    rule_y = sub_y + 60
    draw.rectangle(
        (PC_MARGIN_X, rule_y, A4_WIDTH - PC_MARGIN_X, rule_y + 4),
        fill=INK_HEX,
    )

    # Folio info strip (compressed).
    strip_top = rule_y + 25
    strip_h = 220
    strip_left = PC_MARGIN_X
    strip_right = A4_WIDTH - PC_MARGIN_X
    draw.rectangle(
        (strip_left, strip_top, strip_right, strip_top + strip_h),
        outline=INK_HEX, width=PC_CARD_BORDER_WIDTH,
    )
    col_w = (strip_right - strip_left) // 3
    c1x = strip_left + 30
    c2x = strip_left + col_w + 30
    cy_strip = strip_top + 25
    _draw_text_safe(draw, (c1x, cy_strip), _format_pc_amount_usd(line),
                    font=FONT_PC_FULL_AMOUNT)
    local = _format_pc_amount_local(line)
    if local:
        _draw_text_safe(draw, (c1x, cy_strip + 60), local, font=FONT_PC_FULL_LOCAL)
    _draw_text_safe(draw, (c1x, cy_strip + 120),
                    line.transaction_date.isoformat(),
                    font=FONT_PC_FULL_DATE, fill=INK_2_HEX)
    _draw_text_safe(draw, (c2x, cy_strip),
                    _truncate((line.supplier or "").upper(), 32),
                    font=FONT_PC_FULL_SUPPLIER)
    _draw_text_safe(draw, (c2x, cy_strip + 60),
                    f"{(line.report_bucket or 'OTHER').upper()} — {folio.xlsx_ref}",
                    font=FONT_PC_FULL_BUCKET, fill=INK_2_HEX)
    _draw_paired_card_tag(
        draw, x=c2x, y=cy_strip + 130, text="BUSINESS", solid=True,
    )

    # Folio thumbnail (half-height since we're sharing with spillover).
    folio_top = strip_top + strip_h + 25
    folio_bottom = folio_top + 1300
    folio_left = PC_MARGIN_X
    folio_right = A4_WIDTH - PC_MARGIN_X
    folio_w = folio_right - folio_left
    folio_h = folio_bottom - folio_top
    draw.rectangle(
        (folio_left, folio_top, folio_right, folio_bottom),
        outline=INK_HEX, width=2,
    )
    pages_for_folio = _load_receipt_pages(line) or [_placeholder_tile(line, "no file")]
    folio_img = pages_for_folio[0].copy()
    folio_img.thumbnail((folio_w - 30, folio_h - 30), Image.Resampling.LANCZOS)
    fx = folio_left + (folio_w - folio_img.width) // 2
    fy = folio_top + (folio_h - folio_img.height) // 2
    page.paste(folio_img, (fx, fy))

    # Spillover divider.
    spill_div_y = folio_bottom + 30
    _draw_text_safe(draw, (PC_MARGIN_X, spill_div_y),
                    "SPILLOVER — REMAINING RECEIPTS, DATE ORDER",
                    font=FONT_PC_BAND_META)
    count_text = f"{len(spill)} CARD{'S' if len(spill) != 1 else ''}"
    count_w = draw.textlength(count_text, font=FONT_PC_BAND_META)
    _draw_text_safe(draw,
                    (A4_WIDTH - PC_MARGIN_X - int(count_w), spill_div_y),
                    count_text, font=FONT_PC_BAND_META, fill=INK_3_HEX)
    div_rule_y = spill_div_y + 36
    draw.rectangle(
        (PC_MARGIN_X, div_rule_y, A4_WIDTH - PC_MARGIN_X, div_rule_y + 4),
        fill=INK_HEX,
    )

    # Spillover row (≤ 3 cards in one row).
    spill_top = div_rule_y + 25
    for idx, ctx in enumerate(spill[:3]):
        cx = PC_MARGIN_X + idx * (PC_CELL_WIDTH + PC_GAP_PX)
        _draw_paired_card(page, ctx, x=cx, y=spill_top,
                          w=PC_CELL_WIDTH, h=PC_CELL_HEIGHT)

    return page


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

    if strategy == "paired_card":
        return _render_paired_card_layout(lines, output_path)

    if strategy == "banner_grid":
        return _render_banner_grid_layout(lines, output_path)

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
