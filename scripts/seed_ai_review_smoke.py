r"""F-AI-0b-3 visual smoke seeder.

Seeds one matched review row plus one synthetic AgentDB run/read/comparison
into a temporary SQLite database so the operator can render the Review Queue
and visually verify the AI second-read badge/panel without enabling any AI
flag, without calling any model, and without touching production.

Usage from PowerShell:

    python .\scripts\seed_ai_review_smoke.py `
      --db-path "$env:TEMP\dcexpense_ai_smoke.db" `
      --state warn

Then point the FastAPI app at that DB:

    $env:DATABASE_URL = "sqlite:///$env:TEMP\dcexpense_ai_smoke.db"
    uvicorn app.main:app --reload

Open http://127.0.0.1:8000/review and load the printed statement id.

The script refuses any path under /var/lib/dcexpense or /opt/dcexpense to
make accidental production seeding impossible.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Always set DATABASE_URL before importing app.db so the engine binds to the
# requested path rather than the user's default SQLite file.
def _bind_database_url(db_path: str) -> None:
    sqlite_url = f"sqlite:///{Path(db_path).resolve().as_posix()}"
    os.environ["DATABASE_URL"] = sqlite_url


_PROTECTED_FRAGMENTS = ("/var/lib/dcexpense", "/opt/dcexpense")


def _refuse_protected(db_path: str) -> None:
    raw = db_path.replace("\\", "/").lower()
    resolved = str(Path(db_path).resolve()).replace("\\", "/").lower()
    for fragment in _PROTECTED_FRAGMENTS:
        if fragment in raw or fragment in resolved:
            print(
                f"REFUSED: {db_path!r} resolves to a protected production prefix "
                f"({fragment!r}). Pick a path under /tmp or %TEMP%.",
                file=sys.stderr,
            )
            raise SystemExit(2)


def seed_ai_review_smoke(
    db_path: str,
    *,
    state: str = "warn",
    receipt_supplier: str = "Smoke Cafe",
    receipt_amount: Decimal = Decimal("12.34"),
    receipt_currency: str = "USD",
    receipt_date_value: date = date(2026, 4, 30),
) -> dict:
    """Seed the requested DB and return a small dict the caller can print.

    ``state`` is one of pass / warn / block / stale / malformed. ``stale``
    edits the receipt's amount AFTER the AgentDB rows are written so the
    canonical_snapshot_hash recorded on the run no longer matches the
    current receipt — that's exactly how the live helper detects staleness.
    """

    valid_states = {"pass", "warn", "block", "stale", "malformed"}
    if state not in valid_states:
        raise ValueError(f"state must be one of {sorted(valid_states)}; got {state!r}")

    _refuse_protected(db_path)
    _bind_database_url(db_path)

    # Imports must happen AFTER DATABASE_URL is set so the engine binds correctly.
    from sqlmodel import Session

    from app.db import create_db_and_tables, engine
    from app.models import (
        AgentReceiptComparison,
        AgentReceiptRead,
        AgentReceiptReviewRun,
        AppUser,
        MatchDecision,
        ReceiptDocument,
        StatementImport,
        StatementTransaction,
    )
    from app.services.agent_receipt_review_persistence import (
        build_canonical_receipt_snapshot,
        canonical_receipt_snapshot_hash,
    )

    create_db_and_tables()

    with Session(engine) as session:
        user = AppUser(display_name="ai-visual-smoke")
        session.add(user)
        session.flush()

        statement = StatementImport(
            source_filename="ai_visual_smoke.xlsx",
            row_count=1,
            uploader_user_id=user.id,
        )
        session.add(statement)
        session.flush()

        tx = StatementTransaction(
            statement_import_id=statement.id,
            transaction_date=receipt_date_value,
            supplier_raw=receipt_supplier,
            supplier_normalized=receipt_supplier.upper(),
            local_currency=receipt_currency,
            local_amount=receipt_amount,
            usd_amount=receipt_amount,
            source_row_ref="row-1",
        )
        receipt = ReceiptDocument(
            uploader_user_id=user.id,
            source="test",
            status="imported",
            content_type="photo",
            original_file_name="synthetic_smoke_receipt.jpg",
            extracted_date=receipt_date_value,
            extracted_supplier=receipt_supplier,
            extracted_local_amount=receipt_amount,
            extracted_currency=receipt_currency,
            business_or_personal="Business",
            report_bucket="Meals/Snacks",
            business_reason="Visual smoke seed",
            attendees="Hakan",
            needs_clarification=False,
        )
        session.add(tx)
        session.add(receipt)
        session.commit()
        session.refresh(statement)
        session.refresh(tx)
        session.refresh(receipt)

        decision = MatchDecision(
            statement_transaction_id=tx.id,
            receipt_document_id=receipt.id,
            confidence="high",
            match_method="visual-smoke",
            approved=True,
            reason="visual smoke fixture",
        )
        session.add(decision)
        session.commit()

        snapshot = build_canonical_receipt_snapshot(receipt)
        snapshot_json = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
        snapshot_hash = canonical_receipt_snapshot_hash(snapshot)
        completed_at = datetime.now(timezone.utc)

        run_status = "failed" if state == "malformed" else "completed"
        run = AgentReceiptReviewRun(
            receipt_document_id=receipt.id or 0,
            run_source="test",
            run_kind="receipt_second_read",
            status=run_status,
            schema_version="0a",
            prompt_version="agent_receipt_review_prompt_0a",
            prompt_hash="x" * 64,
            comparator_version="agent_receipt_comparator_0a",
            canonical_snapshot_json=snapshot_json,
            input_hash="y" * 64,
            raw_model_json_redacted=True,
            started_at=completed_at,
            completed_at=completed_at,
            error_code="agent_review_failed" if state == "malformed" else None,
            error_message="synthetic smoke malformed run" if state == "malformed" else None,
        )
        session.add(run)
        session.flush()

        if state == "malformed":
            # No comparison/read row for malformed; still need to commit the
            # failed run so the helper sees it on the next read.
            session.commit()

        if state != "malformed":
            risk_level = {
                "pass": "pass",
                "warn": "warn",
                "block": "block",
                "stale": "pass",  # state value is fine; staleness flips via hash mismatch below
            }[state]
            differences = {
                "pass": [],
                "warn": ["date_mismatch", "supplier_mismatch"],
                "block": ["amount_mismatch"],
                "stale": [],
            }[state]
            recommended_action = {
                "pass": "accept",
                "warn": "manual_review",
                "block": "block_report",
                "stale": "accept",
            }[state]
            summary = {
                "pass": None,
                "warn": "Date and supplier appear to differ from canonical OCR.",
                "block": "Receipt amount disagrees with canonical OCR.",
                "stale": None,
            }[state]
            agent_amount = (
                "999.99"
                if state == "block"
                else str(receipt_amount)
            )
            agent_supplier = (
                "Different Supplier Header"
                if state == "warn"
                else receipt_supplier
            )
            agent_date_value = date(2099, 1, 1) if state == "warn" else receipt_date_value
            read_row = AgentReceiptRead(
                run_id=run.id or 0,
                receipt_document_id=receipt.id or 0,
                read_schema_version="0a",
                read_json="{}",
                extracted_date=agent_date_value,
                extracted_supplier=agent_supplier,
                local_amount_decimal=agent_amount,
                currency=receipt_currency,
            )
            session.add(read_row)
            session.flush()

            comparison = AgentReceiptComparison(
                run_id=run.id or 0,
                agent_receipt_read_id=read_row.id or 0,
                receipt_document_id=receipt.id or 0,
                comparator_version="agent_receipt_comparator_0a",
                risk_level=risk_level,
                recommended_action=recommended_action,
                attention_required=risk_level != "pass",
                differences_json=json.dumps(differences, sort_keys=True),
                suggested_user_message=summary,
                canonical_snapshot_hash=snapshot_hash,
            )
            session.add(comparison)
            session.commit()

        # For "stale", mutate the canonical receipt AFTER the run has been
        # written. The recorded snapshot hash now disagrees with the current
        # receipt -> latest_ai_review_for_receipt() reports status=stale.
        if state == "stale":
            receipt = session.get(ReceiptDocument, receipt.id)
            assert receipt is not None
            receipt.extracted_local_amount = receipt_amount + Decimal("1.00")
            session.add(receipt)
            session.commit()

        return {
            "db_path": db_path,
            "state": state,
            "statement_import_id": statement.id,
            "receipt_id": receipt.id,
            "review_url": f"/reviews/report/{statement.id}",
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed a temporary SQLite DB with one matched receipt and "
                    "one synthetic AgentDB review for F-AI-0b-3 visual smoke.",
    )
    parser.add_argument("--db-path", required=True, help="Target SQLite DB path. Refuses prod prefixes.")
    parser.add_argument(
        "--state",
        default="warn",
        choices=["pass", "warn", "block", "stale", "malformed"],
        help="Synthetic AI second-read state to render.",
    )
    args = parser.parse_args(argv)
    result = seed_ai_review_smoke(args.db_path, state=args.state)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
