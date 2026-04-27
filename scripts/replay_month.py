from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

from replay_support import (
    ExpectedReceipt,
    ReceiptReplayResult,
    json_default,
    parse_expected_manifest,
    result_to_csv_row,
    summarize_results,
    with_expected_matches,
)


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
RECEIPT_EXTENSIONS = {
    ".heic",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay a monthly statement and receipt folder against an isolated "
            "local SQLite database. No Telegram webhook or production database is used."
        )
    )
    parser.add_argument("--receipts-dir", required=True, type=Path, help="Folder containing receipt images/PDFs")
    parser.add_argument(
        "--statement-xlsx",
        required=True,
        type=Path,
        help="Diners/BMO statement workbook accepted by /statements/import-excel",
    )
    parser.add_argument(
        "--expected-manifest",
        type=Path,
        default=None,
        help="Optional CSV of expected receipt fields for pass/fail comparisons",
    )
    parser.add_argument("--output-dir", required=True, type=Path, help="Directory for replay CSV/JSON/report outputs")
    parser.add_argument("--employee-name", default="Offline Replay", help="Employee name for optional report generation")
    parser.add_argument("--title-prefix", default="Offline Replay Report", help="Report title prefix")
    parser.add_argument(
        "--skip-report-generation",
        action="store_true",
        help="Skip the safe report-generation attempt and only write replay CSV/JSON",
    )
    return parser


def _resolve_existing(path: Path, *, kind: str) -> Path:
    resolved = path.expanduser().resolve()
    if kind == "dir" and not resolved.is_dir():
        raise ValueError(f"{path} is not an existing directory")
    if kind == "file" and not resolved.is_file():
        raise ValueError(f"{path} is not an existing file")
    return resolved


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def configure_isolated_backend(output_dir: Path) -> tuple[Any, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_root = output_dir.resolve()
    db_path = output_root / f"replay_{_timestamp()}.sqlite3"
    storage_root = output_root / "storage"
    storage_root.mkdir(parents=True, exist_ok=True)

    if output_root not in db_path.resolve().parents:
        raise RuntimeError(f"Refusing to create replay DB outside output-dir: {db_path}")

    os.environ["DATABASE_URL"] = _sqlite_url(db_path)
    os.environ["EXPENSE_STORAGE_ROOT"] = str(storage_root)
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))

    from app.config import get_settings

    get_settings.cache_clear()
    from app.db import create_db_and_tables, engine

    create_db_and_tables()
    return engine, db_path, storage_root


def _receipt_files(receipts_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in receipts_dir.iterdir()
        if path.is_file() and path.suffix.lower() in RECEIPT_EXTENSIONS
    )


def _mime_type(path: Path) -> str | None:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed


def _content_type(path: Path) -> str:
    return "document" if path.suffix.lower() == ".pdf" else "photo"


def _expected_for(filename: str, manifest: dict[str, ExpectedReceipt]) -> ExpectedReceipt | None:
    direct = manifest.get(filename)
    if direct is not None:
        return direct
    lowered = filename.casefold()
    for key, expected in manifest.items():
        if key.casefold() == lowered:
            return expected
    return None


def create_replay_user(session: Any) -> Any:
    from app.models import AppUser

    user = AppUser(username="offline_replay", display_name="Offline Replay")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def import_statement(session: Any, statement_xlsx: Path, uploader_user_id: int) -> Any:
    from app.services.statement_import import import_diners_excel

    return import_diners_excel(
        session,
        statement_xlsx,
        statement_xlsx.name,
        uploader_user_id=uploader_user_id,
    )


def process_receipts(
    session: Any,
    receipt_files: list[Path],
    manifest: dict[str, ExpectedReceipt],
) -> list[ReceiptReplayResult]:
    from app.models import ReceiptDocument
    from app.services.receipt_extraction import apply_receipt_extraction

    results: list[ReceiptReplayResult] = []
    for path in receipt_files:
        receipt = ReceiptDocument(
            source="offline_replay",
            status="received",
            content_type=_content_type(path),
            original_file_name=path.name,
            mime_type=_mime_type(path),
            storage_path=str(path),
        )
        session.add(receipt)
        session.commit()
        session.refresh(receipt)

        extraction_error = None
        missing_fields = None
        try:
            extraction = apply_receipt_extraction(session, receipt)
            session.refresh(receipt)
            extraction_status = extraction.status
            missing_fields = ",".join(extraction.missing_fields) if extraction.missing_fields else None
        except Exception as exc:  # noqa: BLE001 - audit and continue the rest of the month.
            extraction_status = "error"
            extraction_error = f"{type(exc).__name__}: {exc}"
            receipt.status = "extraction_error"
            receipt.needs_clarification = True
            session.add(receipt)
            session.commit()
            session.refresh(receipt)

        expected = _expected_for(path.name, manifest)
        applied_expected_for_report = False
        if expected is not None:
            if expected.expected_bucket and not receipt.report_bucket:
                receipt.report_bucket = expected.expected_bucket
                applied_expected_for_report = True
            if expected.expected_business_or_personal and not receipt.business_or_personal:
                receipt.business_or_personal = expected.expected_business_or_personal
                applied_expected_for_report = True
        if applied_expected_for_report:
            session.add(receipt)
            session.commit()
            session.refresh(receipt)

        result = ReceiptReplayResult(
            filename=path.name,
            receipt_id=receipt.id,
            extraction_status=extraction_status,
            extraction_error=extraction_error,
            observed_date=receipt.extracted_date,
            observed_supplier=receipt.extracted_supplier,
            observed_amount=receipt.extracted_local_amount,
            observed_currency=receipt.extracted_currency,
            observed_bucket=receipt.report_bucket,
            observed_business_or_personal=receipt.business_or_personal,
            missing_fields=missing_fields,
        )
        results.append(with_expected_matches(result, expected))
    return results


def run_deterministic_matching(
    session: Any,
    statement_import_id: int,
    results: list[ReceiptReplayResult],
) -> list[ReceiptReplayResult]:
    from sqlmodel import select

    from app.models import MatchDecision, ReceiptDocument, StatementTransaction
    from app.services.matching import score_receipt_against_transaction

    transactions = session.exec(
        select(StatementTransaction)
        .where(StatementTransaction.statement_import_id == statement_import_id)
        .order_by(StatementTransaction.transaction_date, StatementTransaction.id)
    ).all()
    receipts = session.exec(select(ReceiptDocument).order_by(ReceiptDocument.id)).all()

    scores_by_receipt_id: dict[int, list[Any]] = {}
    high_receipt_count_by_transaction_id: dict[int, int] = {}
    for receipt in receipts:
        if receipt.id is None:
            continue
        scores = [
            score
            for transaction in transactions
            if (score := score_receipt_against_transaction(receipt, transaction)) is not None
        ]
        scores.sort(key=lambda item: item.score, reverse=True)
        scores_by_receipt_id[receipt.id] = scores
        for score in scores:
            if score.confidence == "high" and score.transaction.id is not None:
                high_receipt_count_by_transaction_id[score.transaction.id] = (
                    high_receipt_count_by_transaction_id.get(score.transaction.id, 0) + 1
                )

    match_updates: dict[int, dict[str, Any]] = {}
    for receipt in receipts:
        if receipt.id is None:
            continue
        scores = scores_by_receipt_id.get(receipt.id, [])
        if not scores:
            match_updates[receipt.id] = {"matched": False}
            continue

        best = scores[0]
        high_scores = [score for score in scores if score.confidence == "high"]
        approved_best = (
            best.confidence == "high"
            and len(high_scores) == 1
            and best.transaction.id is not None
            and high_receipt_count_by_transaction_id.get(best.transaction.id, 0) == 1
        )
        for score in scores[:5]:
            if score.transaction.id is None:
                continue
            decision = MatchDecision(
                receipt_document_id=receipt.id,
                statement_transaction_id=score.transaction.id,
                confidence=score.confidence,
                match_method="offline_replay_date_amount_merchant_v1",
                approved=approved_best and score.transaction.id == best.transaction.id,
                rejected=False,
                reason=score.reason,
            )
            session.add(decision)

        match_updates[receipt.id] = {
            "matched": True,
            "matched_transaction_id": best.transaction.id,
            "matched_transaction_date": best.transaction.transaction_date,
            "matched_supplier": best.transaction.supplier_raw,
            "matched_amount": best.transaction.local_amount,
            "matched_currency": best.transaction.local_currency,
            "match_confidence": best.confidence,
            "match_score": best.score,
            "match_reason": best.reason,
        }
    session.commit()

    updated: list[ReceiptReplayResult] = []
    for result in results:
        if result.receipt_id is None:
            updated.append(result)
            continue
        updated.append(replace(result, **match_updates.get(result.receipt_id, {"matched": False})))
    return updated


def attempt_report_generation(
    session: Any,
    *,
    statement_import_id: int,
    owner_user_id: int,
    employee_name: str,
    title_prefix: str,
    skip: bool,
) -> dict[str, Any]:
    if skip:
        return {"attempted": False, "status": "skipped", "reason": "--skip-report-generation was provided"}

    try:
        from app.services.report_generator import generate_report_package
        from app.services.review_sessions import (
            _resolve_statement_to_expense_report,
            confirm_review_session,
            get_or_create_review_session,
            review_rows,
        )

        expense_report_id = _resolve_statement_to_expense_report(
            session,
            statement_import_id,
            owner_user_id=owner_user_id,
        )
        review = get_or_create_review_session(session, expense_report_id=expense_report_id)
        rows = review_rows(session, review.id or 0)
        blockers = [
            {
                "review_row_id": row.id,
                "statement_transaction_id": row.statement_transaction_id,
                "attention_note": row.attention_note,
            }
            for row in rows
            if row.attention_required
        ]
        if not rows:
            return {"attempted": False, "status": "skipped", "reason": "review session has no rows"}
        if blockers:
            return {
                "attempted": False,
                "status": "skipped",
                "reason": "review rows require attention before confirmation",
                "blockers": blockers[:20],
                "blocker_count": len(blockers),
            }

        confirmed = confirm_review_session(session, review.id or 0, confirmed_by_label="offline-replay")
        run = generate_report_package(
            session=session,
            expense_report_id=expense_report_id,
            employee_name=employee_name,
            title_prefix=title_prefix,
            allow_warnings=True,
        )
        return {
            "attempted": True,
            "status": run.status,
            "review_session_id": confirmed.id,
            "expense_report_id": expense_report_id,
            "report_run_id": run.id,
            "output_workbook_path": run.output_workbook_path,
            "output_pdf_path": run.output_pdf_path,
        }
    except (FileNotFoundError, NotImplementedError, ValueError) as exc:
        return {"attempted": True, "status": "skipped", "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - preserve full run results even if report generation fails.
        return {"attempted": True, "status": "failed", "reason": f"{type(exc).__name__}: {exc}"}


def write_summary_csv(output_path: Path, results: list[ReceiptReplayResult]) -> None:
    fieldnames = list(result_to_csv_row(ReceiptReplayResult(filename="")).keys())
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for result in results:
            writer.writerow(result_to_csv_row(result))


def write_audit_json(output_path: Path, audit: dict[str, Any]) -> None:
    output_path.write_text(
        json.dumps(audit, indent=2, sort_keys=True, default=json_default),
        encoding="utf-8",
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    receipts_dir = _resolve_existing(args.receipts_dir, kind="dir")
    statement_xlsx = _resolve_existing(args.statement_xlsx, kind="file")
    expected_manifest = args.expected_manifest.expanduser().resolve() if args.expected_manifest else None
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest = parse_expected_manifest(expected_manifest)
    engine, db_path, storage_root = configure_isolated_backend(output_dir)

    from sqlmodel import Session

    receipt_paths = _receipt_files(receipts_dir)
    with Session(engine) as session:
        user = create_replay_user(session)
        owner_user_id = user.id
        if owner_user_id is None:
            raise RuntimeError("replay user did not receive an id")
        statement = import_statement(session, statement_xlsx, owner_user_id)
        statement_id = statement.id
        if statement_id is None:
            raise RuntimeError("statement import did not receive an id")
        statement_audit = {
            "id": statement_id,
            "source_filename": statement.source_filename,
            "row_count": statement.row_count,
            "period_start": statement.period_start,
            "period_end": statement.period_end,
        }
        results = process_receipts(session, receipt_paths, manifest)
        results = run_deterministic_matching(session, statement_id, results)
        results = [with_expected_matches(result, _expected_for(result.filename, manifest)) for result in results]
        summary = summarize_results(results)
        report = attempt_report_generation(
            session,
            statement_import_id=statement_id,
            owner_user_id=owner_user_id,
            employee_name=args.employee_name,
            title_prefix=args.title_prefix,
            skip=args.skip_report_generation,
        )

    summary_csv_path = output_dir / "replay_summary.csv"
    audit_json_path = output_dir / "replay_audit.json"
    write_summary_csv(summary_csv_path, results)
    finished_at = datetime.now(timezone.utc)
    audit = {
        "started_at": started_at,
        "finished_at": finished_at,
        "inputs": {
            "receipts_dir": str(receipts_dir),
            "statement_xlsx": str(statement_xlsx),
            "expected_manifest": str(expected_manifest) if expected_manifest else None,
        },
        "outputs": {
            "output_dir": str(output_dir),
            "summary_csv": str(summary_csv_path),
            "audit_json": str(audit_json_path),
            "storage_root": str(storage_root),
            "sqlite_db": str(db_path),
        },
        "manifest": {
            "present": bool(expected_manifest and expected_manifest.exists()),
            "row_count": len(manifest),
        },
        "statement": statement_audit,
        "matching": {
            "method": "deterministic score_receipt_against_transaction only",
            "llm_disambiguation": "disabled",
            "llm_bucket_classification": "disabled",
        },
        "summary": summary,
        "report_generation": report,
    }
    write_audit_json(audit_json_path, audit)
    return audit


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        audit = run(args)
    except Exception as exc:  # noqa: BLE001 - CLI boundary.
        print(f"replay_month failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    for key, value in audit["summary"].items():
        print(f"{key}={value}")
    print(f"replay_summary_csv={audit['outputs']['summary_csv']}")
    print(f"replay_audit_json={audit['outputs']['audit_json']}")
    print(f"replay_sqlite_db={audit['outputs']['sqlite_db']}")
    print(f"report_generation={audit['report_generation']['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
