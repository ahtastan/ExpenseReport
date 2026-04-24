import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from sqlmodel import Session

from app.models import ReceiptDocument
from app.services import model_router


AMOUNT_RE = re.compile(
    r"(?:(?P<currency>TRY|TL|USD|EUR|\$|₺)\s*)?(?P<amount>\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})|\d+(?:[.,]\d{2}))\s*(?P<trailing>TRY|TL|USD|EUR)?",
    re.IGNORECASE,
)
ISO_DATE_RE = re.compile(r"(?<!\d)(?P<year>20\d{2})[-_.](?P<month>\d{1,2})[-_.](?P<day>\d{1,2})(?!\d)")
LOCAL_DATE_RE = re.compile(r"(?<!\d)(?P<day>\d{1,2})[-_.\/](?P<month>\d{1,2})[-_.\/](?P<year>20\d{2})(?!\d)")
MERCHANT_HINT_RE = re.compile(r"(?:merchant|vendor|supplier|store|restaurant)\s*[:=-]\s*(?P<merchant>[^|,\n]+)", re.IGNORECASE)
# Platform-generated placeholder stems produced by services/telegram.py when a
# user-supplied file name is not available (e.g. "telegram_photo_42.jpg",
# "telegram_document_17.pdf", "telegram_statement_5.xlsx"). These carry no
# real merchant signal and must NOT shadow vision-extracted supplier names.
TELEGRAM_PLACEHOLDER_STEM_RE = re.compile(
    r"^telegram[_\s-]*(?:photo|document|statement)(?:[_\s-]*\d+)?$",
    re.IGNORECASE,
)


# Addition B: receipt_type classification from the vision model.
# Anything outside this set is coerced to "unknown" on write.
RECEIPT_TYPES = {"itemized", "payment_receipt", "invoice", "confirmation", "unknown"}


def _coerce_receipt_type(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace(" ", "_").replace("-", "_")
    if not normalized:
        return None
    return normalized if normalized in RECEIPT_TYPES else "unknown"


@dataclass(frozen=True)
class ReceiptExtraction:
    receipt_id: int
    status: str
    extracted_date: date | None = None
    extracted_supplier: str | None = None
    extracted_local_amount: float | None = None
    extracted_currency: str | None = None
    business_or_personal: str | None = None
    receipt_type: str | None = None
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
        stem = Path(filename).stem
        # Reject Telegram-generated placeholder stems that would otherwise
        # shadow the real merchant name produced by the vision pipeline.
        if TELEGRAM_PLACEHOLDER_STEM_RE.match(stem):
            return None
        return _clean_merchant(stem)
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


def extract_receipt_fields(receipt: ReceiptDocument) -> ReceiptExtraction:
    text = _source_text(receipt)
    notes: list[str] = []
    if not text:
        notes.append("No caption, original file name, or storage file name was available to parse.")

    # Stage 1: deterministic parse (regex over caption/filename).
    det_date = _parse_date(text)
    det_amount, det_currency = _parse_amount(text)
    det_supplier = _parse_merchant(text, receipt.original_file_name)
    det_bp = _parse_business_or_personal(text)

    # Stage 2: escalate to the staged vision pipeline only when critical fields
    # are still missing. The router tries mini first and escalates to the full
    # model only when the mini attempt is invalid/incomplete (cost-controlled).
    vision: dict | None = None
    needs_vision = any(value is None for value in (det_date, det_amount, det_supplier))
    if needs_vision and receipt.storage_path:
        vision_result = model_router.vision_extract(receipt.storage_path)
        if vision_result is not None:
            vision = vision_result.fields
            notes.extend(vision_result.notes)

    def _vision_date() -> date | None:
        raw = (vision or {}).get("date")
        if not raw:
            return None
        try:
            return date.fromisoformat(str(raw))
        except ValueError:
            return _parse_date(str(raw))

    # Merge priority: previously-stored value > deterministic > vision.
    # Deterministic wins over vision because it reflects ground truth from the
    # upload metadata that the user typed; vision fills only the gaps.
    extracted_date = receipt.extracted_date or det_date or _vision_date()
    vision_amount = (vision or {}).get("amount")
    vision_currency = (vision or {}).get("currency")
    extracted_amount = det_amount if det_amount is not None else vision_amount
    extracted_currency = det_currency or vision_currency
    vision_supplier = (vision or {}).get("supplier")
    if receipt.content_type == "document":
        # Document filenames are upload IDs / booking refs / customer names,
        # not merchant names. Vision gets the final word on supplier.
        extracted_supplier = receipt.extracted_supplier or vision_supplier or det_supplier
    else:
        extracted_supplier = receipt.extracted_supplier or det_supplier or vision_supplier
    vision_bp = (vision or {}).get("business_or_personal")
    business_or_personal = receipt.business_or_personal or det_bp or vision_bp

    # Addition B: receipt_type follows the "stored wins" merge rule. Vision
    # only gets to assign on first classification; user/operator overrides
    # are preserved on re-extract.
    vision_receipt_type = _coerce_receipt_type((vision or {}).get("receipt_type"))
    receipt_type = receipt.receipt_type or vision_receipt_type

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
        receipt_type=receipt_type,
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
    # Addition B: stored wins — don't clobber an existing classification.
    if receipt.receipt_type is None and result.receipt_type is not None:
        receipt.receipt_type = result.receipt_type
    receipt.ocr_confidence = result.confidence
    receipt.status = result.status
    receipt.needs_clarification = bool(result.missing_fields) or result.business_or_personal == "Business"
    receipt.updated_at = datetime.now(timezone.utc)
    session.add(receipt)
    session.commit()
    session.refresh(receipt)
    return result
