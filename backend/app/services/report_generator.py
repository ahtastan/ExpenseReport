from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import load_workbook
from sqlmodel import Session, select

from app.config import get_settings
from app.models import MatchDecision, ReceiptDocument, ReportRun, StatementTransaction
from app.services.receipt_annotations import ReceiptAnnotationLine, create_annotated_receipts_pdf
from app.services.report_validation import ReportValidation, validate_report_readiness


DEFAULT_BUSINESS_REASONS = {
    date(2026, 3, 11): "Kartonsan Service Visit",
    date(2026, 3, 12): "Kartonsan Service Visit",
    date(2026, 3, 13): "Kartonsan Service Visit",
    date(2026, 4, 1): "Sanipak Visit",
}


@dataclass(frozen=True)
class ReportLine:
    transaction_id: int
    receipt_id: int
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


def _approved_lines(session: Session, statement_import_id: int) -> list[ReportLine]:
    transactions = {
        tx.id: tx
        for tx in session.exec(
            select(StatementTransaction).where(StatementTransaction.statement_import_id == statement_import_id)
        ).all()
        if tx.id is not None
    }
    decisions = [
        decision
        for decision in session.exec(select(MatchDecision).where(MatchDecision.approved == True)).all()  # noqa: E712
        if decision.statement_transaction_id in transactions
    ]
    lines: list[ReportLine] = []
    for decision in decisions:
        tx = transactions.get(decision.statement_transaction_id)
        receipt = session.get(ReceiptDocument, decision.receipt_document_id)
        if not tx or not receipt or tx.id is None or receipt.id is None:
            continue
        tx_date = tx.transaction_date or receipt.extracted_date
        amount = tx.usd_amount if tx.usd_amount is not None else tx.local_amount
        if not tx_date or amount is None:
            continue
        business_reason = (
            receipt.business_reason
            or DEFAULT_BUSINESS_REASONS.get(tx_date)
            or ("Personal spending on Diners card" if (receipt.business_or_personal or "").lower() != "business" else "")
        )
        lines.append(
            ReportLine(
                transaction_id=tx.id,
                receipt_id=receipt.id,
                receipt_path=receipt.storage_path,
                receipt_file_name=receipt.original_file_name or f"receipt_{receipt.id}",
                transaction_date=tx_date,
                supplier=tx.supplier_raw,
                amount=float(amount),
                currency="USD" if tx.usd_amount is not None else tx.local_currency,
                business_or_personal=receipt.business_or_personal or "",
                report_bucket=receipt.report_bucket or "",
                business_reason=business_reason,
                attendees=receipt.attendees or "",
            )
        )
    return sorted(lines, key=lambda line: (line.transaction_date, line.supplier, line.amount))


def _allocate(line: ReportLine, day_totals: dict[date, dict[str, float]], detail_lines: dict[date, list[tuple[str, str, str, float]]]) -> None:
    bucket = line.report_bucket.lower()
    bp = line.business_or_personal.lower()
    day = day_totals[line.transaction_date]
    if bp != "business":
        day["other"] += line.amount
        return

    if "hotel" in bucket:
        day["hotel"] += line.amount
    elif "auto gasoline" in bucket or "fuel" in bucket:
        day["gas"] += line.amount
    elif "taxi/parking/tolls/uber" in bucket or "taxi" in bucket or "uber" in bucket:
        day["ground"] += line.amount
    elif "airfare" in bucket or "bus" in bucket or "ferry" in bucket:
        day["airfare"] += line.amount
    elif "other (travel related)" in bucket:
        day["travel_other"] += line.amount
    elif "breakfast" in bucket:
        day["meal_b"] += line.amount
        detail_lines[line.transaction_date].append(("B", line.supplier, line.business_reason, line.amount))
    elif "lunch" in bucket:
        day["meal_l"] += line.amount
        detail_lines[line.transaction_date].append(("L", line.supplier, line.business_reason, line.amount))
    elif "dinner" in bucket:
        day["meal_d"] += line.amount
        detail_lines[line.transaction_date].append(("D", line.supplier, line.business_reason, line.amount))
    elif "meal" in bucket or "snack" in bucket:
        day["meal_m"] += line.amount
        detail_lines[line.transaction_date].append(("M", line.supplier, line.business_reason, line.amount))
    elif "entertainment" in bucket:
        day["ent"] += line.amount
        detail_lines[line.transaction_date].append(("E", line.supplier, line.business_reason, line.amount))
    else:
        day["other"] += line.amount


def _fill_workbook(template_path: Path, output_path: Path, employee_name: str, title: str, lines: list[ReportLine]) -> None:
    wb = load_workbook(template_path)
    ws1a, ws1b, ws2a, ws2b = wb["Week 1A"], wb["Week 1B"], wb["Week 2A"], wb["Week 2B"]
    ws1a["B3"] = employee_name
    ws1a["G3"] = title

    day_totals: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    detail_lines: dict[date, list[tuple[str, str, str, float]]] = defaultdict(list)
    for line in lines:
        _allocate(line, day_totals, detail_lines)

    dates = sorted({line.transaction_date for line in lines})
    first7, next7 = dates[:7], dates[7:14]
    cols = ["E", "F", "G", "H", "I", "J", "K"]

    def fill_a(ws, page_dates: list[date]) -> None:
        for col, tx_date in zip(cols, page_dates):
            vals = day_totals[tx_date]
            ws[f"{col}5"] = datetime(tx_date.year, tx_date.month, tx_date.day)
            ws[f"{col}6"] = DEFAULT_BUSINESS_REASONS.get(tx_date, "")
            ws[f"{col}7"] = vals.get("airfare") or None
            ws[f"{col}8"] = vals.get("hotel") or None
            ws[f"{col}10"] = vals.get("gas") or None
            ws[f"{col}11"] = vals.get("ground") or None
            ws[f"{col}14"] = vals.get("travel_other") or None
            ws[f"{col}26"] = vals.get("other") or None
            ws[f"{col}29"] = vals.get("meal_m") or None
            ws[f"{col}30"] = vals.get("meal_b") or None
            ws[f"{col}31"] = vals.get("meal_l") or None
            ws[f"{col}32"] = vals.get("meal_d") or None
            ws[f"{col}35"] = vals.get("ent") or None

    def fill_b(ws, page_dates: list[date]) -> None:
        rownum = 8
        for tx_date in page_dates:
            for code, supplier, reason, _usd in detail_lines.get(tx_date, []):
                if rownum > 42:
                    return
                ws[f"B{rownum}"] = datetime(tx_date.year, tx_date.month, tx_date.day)
                ws[f"E{rownum}"] = supplier[:40]
                ws[f"F{rownum}"] = reason[:50]
                ws[f"G{rownum}"] = code
                rownum += 1

    fill_a(ws1a, first7)
    fill_a(ws2a, next7)
    fill_b(ws1b, first7)
    fill_b(ws2b, next7)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)


def _write_summary(path: Path, validation: ReportValidation, lines: list[ReportLine], workbook_paths: list[Path]) -> None:
    summary_lines = [
        f"statement_import_id={validation.statement_import_id}",
        f"ready={validation.ready}",
        f"errors={validation.issue_count}",
        f"warnings={validation.warning_count}",
        f"approved_matches={validation.approved_matches}",
        f"report_lines={len(lines)}",
        f"business_receipts={validation.business_receipts}",
        f"personal_receipts={validation.personal_receipts}",
        "workbooks=" + ", ".join(p.name for p in workbook_paths),
        "",
        "issues:",
    ]
    summary_lines.extend(f"{issue.severity}|{issue.code}|{issue.message}" for issue in validation.issues)
    path.write_text("\n".join(summary_lines), encoding="utf-8")


def _annotation_lines(lines: list[ReportLine]) -> list[ReceiptAnnotationLine]:
    return [
        ReceiptAnnotationLine(
            receipt_id=line.receipt_id,
            transaction_id=line.transaction_id,
            receipt_path=line.receipt_path,
            receipt_file_name=line.receipt_file_name,
            transaction_date=line.transaction_date,
            supplier=line.supplier,
            amount=line.amount,
            currency=line.currency,
            business_or_personal=line.business_or_personal,
            report_bucket=line.report_bucket,
            business_reason=line.business_reason,
            attendees=line.attendees,
        )
        for line in lines
    ]


def generate_report_package(
    session: Session,
    statement_import_id: int,
    employee_name: str,
    title_prefix: str,
    allow_warnings: bool = True,
) -> ReportRun:
    settings = get_settings()
    if not settings.report_template_path or not settings.report_template_path.exists():
        raise FileNotFoundError("Expense report template was not found. Set EXPENSE_REPORT_TEMPLATE_PATH.")

    validation = validate_report_readiness(session, statement_import_id)
    if validation.issue_count:
        raise ValueError(f"Report has {validation.issue_count} blocking validation error(s)")
    if validation.warning_count and not allow_warnings:
        raise ValueError(f"Report has {validation.warning_count} validation warning(s)")

    lines = _approved_lines(session, statement_import_id)
    if not lines:
        raise ValueError("No approved matches are available for report generation")

    run = ReportRun(statement_import_id=statement_import_id, template_name=settings.report_template_path.name, status="running")
    session.add(run)
    session.commit()
    session.refresh(run)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = settings.storage_root / "reports" / f"report_{run.id}_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    dates = sorted({line.transaction_date for line in lines})
    chunks = [dates[i : i + 14] for i in range(0, len(dates), 14)]
    workbook_paths: list[Path] = []
    for idx, chunk_dates in enumerate(chunks, start=1):
        chunk_lines = [line for line in lines if line.transaction_date in set(chunk_dates)]
        title = f"{title_prefix} - Part {idx}" if len(chunks) > 1 else title_prefix
        workbook_path = output_dir / f"expense_report_part_{idx}.xlsx"
        _fill_workbook(settings.report_template_path, workbook_path, employee_name, title, chunk_lines)
        workbook_paths.append(workbook_path)

    summary_path = output_dir / "validation_summary.txt"
    _write_summary(summary_path, validation, lines, workbook_paths)
    annotated_pdf_path = output_dir / "annotated_receipts.pdf"
    create_annotated_receipts_pdf(_annotation_lines(lines), annotated_pdf_path)

    if len(workbook_paths) == 1 and not annotated_pdf_path.exists():
        final_path = workbook_paths[0]
    else:
        final_path = output_dir / "expense_report_package.zip"
        with ZipFile(final_path, "w", compression=ZIP_DEFLATED) as zf:
            for workbook_path in workbook_paths:
                zf.write(workbook_path, workbook_path.name)
            zf.write(summary_path, summary_path.name)
            zf.write(annotated_pdf_path, annotated_pdf_path.name)

    run.status = "completed"
    run.output_workbook_path = str(final_path)
    run.output_pdf_path = str(annotated_pdf_path)
    session.add(run)
    session.commit()
    session.refresh(run)
    return run
