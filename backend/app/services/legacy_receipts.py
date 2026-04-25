import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from app.models import ReceiptDocument

_AMOUNT_QUANT = Decimal("0.0001")


@dataclass
class LegacyReceiptImportSummary:
    source_path: str
    rows_read: int = 0
    receipts_created: int = 0
    receipts_updated: int = 0
    rows_skipped: int = 0


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def _parse_amount(value: Any) -> Decimal | None:
    text = str(value or "").replace(",", "").strip()
    if not text:
        return None
    try:
        return Decimal(text).quantize(_AMOUNT_QUANT)
    except (InvalidOperation, ValueError):
        return None


def _bool_from_yes(value: Any, default: bool = False) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in {"yes", "true", "1"}


def _receipt_path(row: dict[str, str], receipt_root: Path | None) -> str | None:
    file_name = row.get("Receipt File")
    if not file_name or not receipt_root:
        return None
    path = receipt_root / file_name
    return str(path) if path.exists() else None


def _existing_receipt(session: Session, file_name: str) -> ReceiptDocument | None:
    return session.exec(
        select(ReceiptDocument).where(
            ReceiptDocument.source == "legacy_mapping",
            ReceiptDocument.original_file_name == file_name,
        )
    ).first()


def import_legacy_receipt_mapping(
    session: Session,
    csv_path: Path,
    receipt_root: Path | None = None,
    update_existing: bool = True,
) -> LegacyReceiptImportSummary:
    summary = LegacyReceiptImportSummary(source_path=str(csv_path))
    rows = list(csv.DictReader(open(csv_path, "r", encoding="utf-8-sig", newline="")))
    summary.rows_read = len(rows)

    for row in rows:
        file_name = (row.get("Receipt File") or "").strip()
        if not file_name or row.get("File Exists") == "No":
            summary.rows_skipped += 1
            continue

        existing = _existing_receipt(session, file_name)
        if existing and not update_existing:
            summary.rows_skipped += 1
            continue

        receipt = existing or ReceiptDocument(source="legacy_mapping")
        receipt.status = "imported"
        receipt.content_type = "document" if (row.get("File Type") or "").lower() == "pdf" else "photo"
        receipt.original_file_name = file_name
        receipt.mime_type = "application/pdf" if receipt.content_type == "document" else "image/jpeg"
        receipt.storage_path = _receipt_path(row, receipt_root)
        receipt.caption = row.get("Reason / Notes") or None
        receipt.extracted_date = _parse_date(row.get("Receipt Date") or row.get("Statement Date"))
        receipt.extracted_supplier = row.get("Merchant (Receipt)") or row.get("Merchant (Statement Match)") or None
        receipt.extracted_local_amount = _parse_amount(row.get("Amount Local") or row.get("Statement Amount Local"))
        receipt.extracted_currency = row.get("Local Currency") or "TRY"
        receipt.ocr_confidence = 1.0 if row.get("Authoritative Source", "").startswith("VisionExtract") else None
        receipt.business_or_personal = row.get("Business or Personal") or None
        receipt.report_bucket = row.get("Suggested Expense Report Bucket") or None
        receipt.business_reason = None
        receipt.attendees = None
        receipt.needs_clarification = _bool_from_yes(row.get("Needs Manual Review"), default=True)

        session.add(receipt)
        if existing:
            summary.receipts_updated += 1
        else:
            summary.receipts_created += 1

    session.commit()
    return summary
