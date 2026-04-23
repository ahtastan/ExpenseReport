"""Disposable live smoke for OpenAI-backed model paths.

This script intentionally uses a disposable SQLite DB and output directory. It
does not mutate source receipt/statement files.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = REPO_ROOT.parent
VERIFY_ROOT = REPO_ROOT / ".verify_data"


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def _first_existing(paths: list[Path]) -> Path | None:
    for path in paths:
        if path.exists() and path.is_file():
            return path
    return None


def _openai_sdk_status() -> dict[str, object]:
    try:
        import openai  # noqa: WPS433
    except Exception as exc:
        return {
            "ok": False,
            "step": "preflight",
            "reason": f"OpenAI SDK import failed: {exc}",
            "hint": "Install project dependencies for this Python, e.g. python -m pip install -e backend or python -m pip install 'openai>=1.50'.",
        }
    return {
        "ok": True,
        "step": "preflight",
        "openai_version": getattr(openai, "__version__", "unknown"),
    }


def _find_receipt_image() -> Path:
    override = os.getenv("LIVE_MODEL_SMOKE_RECEIPT")
    if override:
        path = Path(override)
        if path.exists():
            return path
        raise FileNotFoundError(f"LIVE_MODEL_SMOKE_RECEIPT does not exist: {path}")

    receipt_dir = WORKSPACE_ROOT / "03_11_Receipts" / "Receipts"
    for suffix in ("*.jpeg", "*.jpg", "*.png"):
        found = sorted(receipt_dir.glob(suffix))
        if found:
            return found[0]
    raise FileNotFoundError(f"No receipt image found under {receipt_dir}")


def _statement_path() -> Path:
    override = os.getenv("LIVE_MODEL_SMOKE_STATEMENT")
    if override:
        path = Path(override)
        if path.exists():
            return path
        raise FileNotFoundError(f"LIVE_MODEL_SMOKE_STATEMENT does not exist: {path}")
    path = WORKSPACE_ROOT / "03_11_Receipts" / "Diners_Transactions.xlsx"
    if not path.exists():
        raise FileNotFoundError(f"Statement workbook not found: {path}")
    return path


def main() -> int:
    for env_path in (REPO_ROOT / ".env", REPO_ROOT / "backend" / ".env", WORKSPACE_ROOT / ".env"):
        _load_env_file(env_path)

    if not os.getenv("OPENAI_API_KEY"):
        print(json.dumps({"status": "skipped", "reason": "OPENAI_API_KEY missing"}))
        return 2

    sdk_status = _openai_sdk_status()
    if not sdk_status["ok"]:
        print(json.dumps({"status": "failed", **sdk_status}))
        return 3

    VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
    run_id = uuid4().hex
    db_path = VERIFY_ROOT / f"live_model_smoke_{run_id}.db"
    storage_root = VERIFY_ROOT / f"live_model_smoke_{run_id}"
    storage_root.mkdir(parents=True, exist_ok=True)

    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["EXPENSE_STORAGE_ROOT"] = str(storage_root)
    template_path = _first_existing(
        [
            Path(os.getenv("EXPENSE_REPORT_TEMPLATE_PATH", "")),
            WORKSPACE_ROOT / "Expense Report Form_Blank.xlsx",
            REPO_ROOT.parent / "Expense Report Form_Blank.xlsx",
        ]
    )
    if template_path is None:
        raise FileNotFoundError("Expense report template was not found")
    os.environ["EXPENSE_REPORT_TEMPLATE_PATH"] = str(template_path)

    backend_root = REPO_ROOT / "backend"
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))

    from sqlmodel import Session, select  # noqa: WPS433

    from app.db import create_db_and_tables, engine  # noqa: WPS433
    from app.models import StatementTransaction  # noqa: WPS433
    from app.services import model_router  # noqa: WPS433
    from app.services.report_generator import generate_report_package  # noqa: WPS433
    from app.services.review_sessions import (  # noqa: WPS433
        confirm_review_session,
        get_or_create_review_session,
        review_rows,
        update_review_row,
    )
    from app.services.statement_import import import_diners_excel  # noqa: WPS433

    receipt_image = _find_receipt_image()
    ocr_result = model_router.vision_extract(str(receipt_image))
    if ocr_result is None:
        raise RuntimeError("OCR model smoke returned no result")

    create_db_and_tables()
    with Session(engine) as session:
        statement = import_diners_excel(session, _statement_path(), "live_model_smoke_diners.xlsx")
        transactions = session.exec(
            select(StatementTransaction)
            .where(StatementTransaction.statement_import_id == statement.id)
            .order_by(StatementTransaction.transaction_date, StatementTransaction.id)
        ).all()
        if len(transactions) < 2:
            raise RuntimeError("Need at least two statement transactions for matching smoke")

        receipt_payload = {
            "supplier": transactions[0].supplier_raw,
            "date": transactions[0].transaction_date.isoformat() if transactions[0].transaction_date else None,
            "local_amount": transactions[0].local_amount,
            "local_currency": transactions[0].local_currency,
        }
        candidate_payload = [
            {
                "transaction_id": tx.id,
                "supplier": tx.supplier_raw,
                "date": tx.transaction_date.isoformat() if tx.transaction_date else None,
                "local_amount": tx.local_amount,
                "local_currency": tx.local_currency,
                "deterministic_reason": "live smoke candidate",
            }
            for tx in transactions[:2]
        ]
        match_result = model_router.match_disambiguate(receipt_payload, candidate_payload)
        if match_result is None:
            raise RuntimeError("Matching model smoke returned no parseable result")

        review = get_or_create_review_session(session, statement.id)
        for row in review_rows(session, review.id)[:3]:
            update_review_row(
                session,
                row.id,
                fields={
                    "business_or_personal": "Business",
                    "report_bucket": "Other",
                    "business_reason": "Live model smoke",
                },
            )
        for row in review_rows(session, review.id)[3:]:
            update_review_row(
                session,
                row.id,
                fields={"business_or_personal": "Personal", "report_bucket": "Personal"},
            )
        confirm_review_session(session, review.id, confirmed_by_label="live-model-smoke")
        report_run = generate_report_package(session, statement.id, "Live Model Smoke", "Live Model Smoke", True)

    if not report_run.output_workbook_path:
        raise RuntimeError("Report generation did not produce a package path")
    with ZipFile(report_run.output_workbook_path) as zf:
        names = set(zf.namelist())
        if "summary.md" not in names:
            raise RuntimeError("summary.md missing from generated package")
        summary = zf.read("summary.md").decode("utf-8", errors="replace")

    print(
        json.dumps(
            {
                "status": "passed",
                "ocr": {
                    "model": ocr_result.model,
                    "escalated": ocr_result.escalated,
                    "fields_present": sorted(k for k, v in ocr_result.fields.items() if v not in (None, "")),
                },
                "matching": {
                    "model": match_result.model,
                    "transaction_id_present": match_result.transaction_id is not None,
                    "confidence": match_result.confidence,
                },
                "synthesis": {
                    "summary_md_present": bool(summary.strip()),
                    "package": str(report_run.output_workbook_path),
                },
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
