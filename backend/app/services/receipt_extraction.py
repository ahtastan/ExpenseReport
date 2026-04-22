import base64
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from sqlmodel import Session

from app.models import ReceiptDocument


AMOUNT_RE = re.compile(
    r"(?:(?P<currency>TRY|TL|USD|EUR|\$|₺)\s*)?(?P<amount>\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})|\d+(?:[.,]\d{2}))\s*(?P<trailing>TRY|TL|USD|EUR)?",
    re.IGNORECASE,
)
ISO_DATE_RE = re.compile(r"(?<!\d)(?P<year>20\d{2})[-_.](?P<month>\d{1,2})[-_.](?P<day>\d{1,2})(?!\d)")
LOCAL_DATE_RE = re.compile(r"(?<!\d)(?P<day>\d{1,2})[-_.\/](?P<month>\d{1,2})[-_.\/](?P<year>20\d{2})(?!\d)")
MERCHANT_HINT_RE = re.compile(r"(?:merchant|vendor|supplier|store|restaurant)\s*[:=-]\s*(?P<merchant>[^|,\n]+)", re.IGNORECASE)


@dataclass(frozen=True)
class ReceiptExtraction:
    receipt_id: int
    status: str
    extracted_date: date | None = None
    extracted_supplier: str | None = None
    extracted_local_amount: float | None = None
    extracted_currency: str | None = None
    business_or_personal: str | None = None
    confidence: float | None = None
    missing_fields: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _coerce_amount(raw: str) -> float | None:
    text = raw.strip()
    if not text:
        return None
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") else text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _parse_date(text: str) -> date | None:
    for match in ISO_DATE_RE.finditer(text):
        try:
            return date(int(match.group("year")), int(match.group("month")), int(match.group("day")))
        except ValueError:
            continue
    for match in LOCAL_DATE_RE.finditer(text):
        try:
            return date(int(match.group("year")), int(match.group("month")), int(match.group("day")))
        except ValueError:
            continue
    return None


def _parse_amount(text: str) -> tuple[float | None, str | None]:
    candidates: list[tuple[float, str | None]] = []
    for match in AMOUNT_RE.finditer(text):
        amount = _coerce_amount(match.group("amount"))
        if amount is None:
            continue
        currency = (match.group("currency") or match.group("trailing") or "").upper()
        if currency == "$":
            currency = "USD"
        elif currency in {"TL", "₺"}:
            currency = "TRY"
        candidates.append((amount, currency or None))
    if not candidates:
        return None, None
    return max(candidates, key=lambda item: item[0])


def _clean_merchant(value: str) -> str | None:
    text = re.sub(r"[_\-]+", " ", value)
    text = re.split(r"\b(?:total|amount|date|business|personal)\b", text, maxsplit=1, flags=re.IGNORECASE)[0]
    text = re.sub(r"\b20\d{2}[-_.]\d{1,2}[-_.]\d{1,2}\b", " ", text)
    text = re.sub(r"\b\d{1,2}[-_.]\d{1,2}[-_.]\d{4}\b", " ", text)
    text = re.sub(r"\b\d+[.,]?\d*\b", " ", text)
    text = " ".join(text.split()).strip(" .,-_|")
    if not text or len(text) < 3:
        return None
    return text[:120]


def _parse_merchant(text: str, filename: str | None) -> str | None:
    hint = MERCHANT_HINT_RE.search(text)
    if hint:
        return _clean_merchant(hint.group("merchant"))
    if filename:
        return _clean_merchant(Path(filename).stem)
    return None


def _parse_business_or_personal(text: str) -> str | None:
    lowered = text.lower()
    if "personal" in lowered:
        return "Personal"
    if "business" in lowered or "customer" in lowered or "client" in lowered or "project" in lowered:
        return "Business"
    return None


def _source_text(receipt: ReceiptDocument) -> str:
    parts = [
        receipt.caption or "",
        receipt.original_file_name or "",
        Path(receipt.storage_path).name if receipt.storage_path else "",
    ]
    return " | ".join(part for part in parts if part)


_VISION_PROMPT = (
    "You are an expense receipt parser. Extract the following fields from the receipt image and return ONLY a JSON object with exactly these keys:\n"
    "  date (ISO 8601 string YYYY-MM-DD or null),\n"
    "  supplier (string or null),\n"
    "  amount (number or null),\n"
    "  currency (3-letter ISO code string or null),\n"
    "  business_or_personal (\"Business\" or \"Personal\" or null).\n"
    "Return only the JSON object, no other text."
)

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_PDF_EXTENSION = ".pdf"


def _vision_extract(storage_path: str) -> dict | None:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    path = Path(storage_path)
    if not path.exists():
        return None

    suffix = path.suffix.lower()
    if suffix not in _IMAGE_EXTENSIONS:
        return None

    try:
        import anthropic  # deferred import — optional dependency

        raw = path.read_bytes()
        b64 = base64.standard_b64encode(raw).decode()
        media_type_map = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }
        media_type = media_type_map.get(suffix, "image/jpeg")

        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=256,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                        {"type": "text", "text": _VISION_PROMPT},
                    ],
                }
            ],
        )
        text = message.content[0].text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = re.sub(r"^```[a-z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text)
        return json.loads(text)
    except Exception:
        return None


def extract_receipt_fields(receipt: ReceiptDocument) -> ReceiptExtraction:
    text = _source_text(receipt)
    notes: list[str] = []
    if not text:
        notes.append("No caption, original file name, or storage file name was available to parse.")

    # Try vision extraction first when a stored image is available and API key is set
    vision: dict | None = None
    if receipt.storage_path:
        vision = _vision_extract(receipt.storage_path)
        if vision:
            notes.append("Vision extraction succeeded.")

    def _vision_date() -> date | None:
        raw = (vision or {}).get("date")
        if not raw:
            return None
        try:
            return date.fromisoformat(str(raw))
        except ValueError:
            return None

    extracted_date = receipt.extracted_date or _vision_date() or _parse_date(text)
    vision_amount = (vision or {}).get("amount")
    vision_currency = (vision or {}).get("currency")
    det_amount, det_currency = _parse_amount(text)
    extracted_amount = vision_amount if vision_amount is not None else det_amount
    extracted_currency = vision_currency or det_currency
    vision_supplier = (vision or {}).get("supplier")
    extracted_supplier = receipt.extracted_supplier or vision_supplier or _parse_merchant(text, receipt.original_file_name)
    vision_bp = (vision or {}).get("business_or_personal")
    business_or_personal = receipt.business_or_personal or vision_bp or _parse_business_or_personal(text)

    if receipt.extracted_local_amount is not None:
        extracted_amount = receipt.extracted_local_amount
    if receipt.extracted_currency:
        extracted_currency = receipt.extracted_currency

    filled = sum(
        value is not None
        for value in [extracted_date, extracted_amount, extracted_currency, extracted_supplier, business_or_personal]
    )
    confidence = filled / 5
    missing = []
    if extracted_date is None:
        missing.append("receipt_date")
    if extracted_amount is None:
        missing.append("local_amount")
    if extracted_supplier is None:
        missing.append("supplier")
    if business_or_personal is None:
        missing.append("business_or_personal")

    status = "extracted" if not missing else "needs_extraction_review"
    return ReceiptExtraction(
        receipt_id=receipt.id or 0,
        status=status,
        extracted_date=extracted_date,
        extracted_supplier=extracted_supplier,
        extracted_local_amount=extracted_amount,
        extracted_currency=extracted_currency or ("TRY" if extracted_amount is not None else None),
        business_or_personal=business_or_personal,
        confidence=round(confidence, 2),
        missing_fields=missing,
        notes=notes,
    )


def apply_receipt_extraction(session: Session, receipt: ReceiptDocument) -> ReceiptExtraction:
    result = extract_receipt_fields(receipt)
    receipt.extracted_date = result.extracted_date
    receipt.extracted_supplier = result.extracted_supplier
    receipt.extracted_local_amount = result.extracted_local_amount
    receipt.extracted_currency = result.extracted_currency
    receipt.business_or_personal = result.business_or_personal
    receipt.ocr_confidence = result.confidence
    receipt.status = result.status
    receipt.needs_clarification = bool(result.missing_fields) or result.business_or_personal == "Business"
    receipt.updated_at = datetime.now(timezone.utc)
    session.add(receipt)
    session.commit()
    session.refresh(receipt)
    return result
