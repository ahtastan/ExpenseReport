from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from sqlmodel import Session

from app.models import StatementImport, StatementTransaction


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def _parse_amount(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("TRY", "").replace("USD", "").replace("$", "").replace(" ", "")
    text = text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def _normalize_supplier(value: Any) -> str:
    text = str(value or "").strip()
    return " ".join(text.upper().split())


def _header_index(headers: list[str], candidates: set[str]) -> int | None:
    normalized = [h.strip().lower() for h in headers]
    for idx, header in enumerate(normalized):
        if header in candidates:
            return idx
    return None


def import_diners_excel(
    session: Session,
    file_path: Path,
    source_filename: str,
    uploader_user_id: int | None = None,
) -> StatementImport:
    wb = load_workbook(file_path, data_only=True, read_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise ValueError("Workbook is empty")

    headers = [str(value or "").strip() for value in rows[0]]
    tran_idx = _header_index(headers, {"tran date", "transaction date", "date"})
    supplier_idx = _header_index(headers, {"supplier", "merchant", "description"})
    source_amount_idx = _header_index(headers, {"source amount", "local amount", "amount local"})
    usd_idx = _header_index(headers, {"amount incl usd", "amount usd", "usd amount"})

    if tran_idx is None or supplier_idx is None:
        raise ValueError("Could not find transaction date and supplier columns")

    statement = StatementImport(
        uploader_user_id=uploader_user_id,
        source_filename=source_filename,
        storage_path=str(file_path),
    )
    session.add(statement)
    session.commit()
    session.refresh(statement)

    transaction_count = 0
    min_date: date | None = None
    max_date: date | None = None
    for row_number, row in enumerate(rows[1:], start=2):
        if not any(value not in (None, "") for value in row):
            continue
        supplier_raw = str(row[supplier_idx] or "").strip()
        if not supplier_raw:
            continue
        transaction_date = _parse_date(row[tran_idx])
        local_amount = _parse_amount(row[source_amount_idx]) if source_amount_idx is not None else None
        usd_amount = _parse_amount(row[usd_idx]) if usd_idx is not None else None
        if transaction_date:
            min_date = transaction_date if min_date is None else min(min_date, transaction_date)
            max_date = transaction_date if max_date is None else max(max_date, transaction_date)
        transaction = StatementTransaction(
            statement_import_id=statement.id,
            transaction_date=transaction_date,
            supplier_raw=supplier_raw,
            supplier_normalized=_normalize_supplier(supplier_raw),
            local_currency="TRY",
            local_amount=local_amount,
            usd_amount=usd_amount,
            source_row_ref=str(row_number),
            source_kind="excel",
        )
        session.add(transaction)
        transaction_count += 1

    statement.row_count = transaction_count
    statement.period_start = min_date
    statement.period_end = max_date
    session.add(statement)
    session.commit()
    session.refresh(statement)
    wb.close()
    return statement
