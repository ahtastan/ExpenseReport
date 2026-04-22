from dataclasses import dataclass
from datetime import date
from pathlib import Path
from textwrap import shorten

from PIL import Image, ImageDraw, ImageFont, ImageOps, JpegImagePlugin  # noqa: F401


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
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


@dataclass(frozen=True)
class ReceiptAnnotationLine:
    receipt_id: int | None
    transaction_id: int
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


def _font(name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default()


FONT_BOLD = _font("arialbd.ttf", 74)
FONT_MEDIUM = _font("arialbd.ttf", 42)
FONT_SMALL = _font("arial.ttf", 30)


def _line_color(line: ReceiptAnnotationLine) -> str:
    bp = line.business_or_personal.lower()
    if bp == "business":
        return "green"
    if bp == "personal":
        return "red"
    return "darkorange"


def _draw_label(img: Image.Image, line: ReceiptAnnotationLine) -> Image.Image:
    draw = ImageDraw.Draw(img)
    color = _line_color(line)
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
    color = _line_color(line)
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


def _receipt_image(line: ReceiptAnnotationLine) -> Image.Image:
    if not line.receipt_path:
        return _placeholder_tile(line, "No storage path is available.")

    path = Path(line.receipt_path)
    if not path.exists():
        return _placeholder_tile(line, "The stored file path does not exist.")

    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        return _placeholder_tile(line, f"{path.suffix.upper().lstrip('.')} receipts need PDF rendering support.")

    try:
        with Image.open(path) as img:
            rendered = ImageOps.exif_transpose(img).convert("RGB")
    except OSError as exc:
        return _placeholder_tile(line, f"Could not open image: {exc}")
    return _draw_label(rendered, line)


def create_annotated_receipts_pdf(lines: list[ReceiptAnnotationLine], output_path: Path) -> int:
    ordered = sorted(lines, key=lambda line: (line.transaction_date, line.supplier.lower(), line.amount, line.receipt_id or 0))
    if not ordered:
        raise ValueError("No receipt lines are available for annotation")

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
