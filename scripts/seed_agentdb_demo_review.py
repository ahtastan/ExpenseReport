"""Seed one synthetic AgentDB AI second-read result for an existing receipt.

Operator/demo tooling only. This script exists so the Review Queue
``source.ai_review`` UI can be exercised without enabling AI flags, calling a
model, running matching, generating reports, importing workbooks, sending
Telegram messages, or mutating canonical expense data.

It writes only to:

* agent_receipt_review_run
* agent_receipt_read
* agent_receipt_comparison

Local dry-run example:

    python scripts/seed_agentdb_demo_review.py \
      --db-path /tmp/expense_app.db \
      --receipt-id 42 \
      --state warn \
      --amount 223.00 \
      --currency TRY \
      --date 2025-12-28 \
      --supplier "A101 YENI MAGAZACILIK" \
      --dry-run

Local write example:

    python scripts/seed_agentdb_demo_review.py \
      --db-path /tmp/expense_app.db \
      --receipt-id 42 \
      --state warn \
      --amount 223.00 \
      --currency TRY \
      --date 2025-12-28 \
      --supplier "A101 YENI MAGAZACILIK" \
      --yes

Production-path write example, intentionally explicit:

    python scripts/seed_agentdb_demo_review.py \
      --db-path /var/lib/dcexpense/expense_app.db \
      --receipt-id 42 \
      --state warn \
      --amount 223.00 \
      --currency TRY \
      --date 2025-12-28 \
      --supplier "A101 YENI MAGAZACILIK" \
      --i-understand-this-is-prod \
      --yes
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlmodel import Session, select

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.json_utils import dumps  # noqa: E402
from app.models import (  # noqa: E402
    AgentReceiptComparison,
    AgentReceiptRead,
    AgentReceiptReviewRun,
    MatchDecision,
    ReceiptDocument,
    ReviewRow,
    utc_now,
)
from app.services.agent_receipt_review_persistence import (  # noqa: E402
    COMPARATOR_VERSION,
    PROMPT_VERSION,
    SCHEMA_VERSION,
    build_canonical_receipt_snapshot,
    canonical_receipt_snapshot_hash,
)

PROTECTED_PATH_FRAGMENTS = ("/var/lib/dcexpense", "/opt/dcexpense")
VALID_STATES = ("pass", "warn", "block", "stale")


def _is_protected_path(db_path: str) -> tuple[bool, str | None]:
    raw = db_path.replace("\\", "/").lower()
    resolved = str(Path(db_path).resolve()).replace("\\", "/").lower()
    for fragment in PROTECTED_PATH_FRAGMENTS:
        if fragment in raw or fragment in resolved:
            return True, fragment
    return False, None


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def _parse_decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("amount must be a decimal value") from exc


def _stable_hash(payload: Any) -> str:
    return hashlib.sha256(dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _latest_review_row(session: Session, receipt_id: int) -> ReviewRow | None:
    return session.exec(
        select(ReviewRow)
        .where(ReviewRow.receipt_document_id == receipt_id)
        .order_by(ReviewRow.id.desc())
    ).first()


def _latest_match_decision(session: Session, receipt_id: int) -> MatchDecision | None:
    return session.exec(
        select(MatchDecision)
        .where(MatchDecision.receipt_document_id == receipt_id)
        .order_by(MatchDecision.id.desc())
    ).first()


def _differences_for_state(
    *,
    state: str,
    receipt: ReceiptDocument,
    read_date: date,
    read_supplier: str,
) -> list[str]:
    if state == "pass" or state == "stale":
        return []
    if state == "block":
        return ["amount_mismatch"]
    if receipt.extracted_date is not None and receipt.extracted_date != read_date:
        return ["date_mismatch"]
    if (receipt.extracted_supplier or "").strip().casefold() != read_supplier.strip().casefold():
        return ["supplier_mismatch"]
    return ["supplier_mismatch"]


def _comparison_statuses(differences: list[str]) -> dict[str, str]:
    codes = set(differences)
    return {
        "amount_status": "mismatch" if "amount_mismatch" in codes else "match",
        "date_status": "mismatch" if "date_mismatch" in codes else "match",
        "currency_status": "mismatch" if "currency_mismatch" in codes else "match",
        "supplier_status": "mismatch" if "supplier_mismatch" in codes else "match",
    }


def _risk_level_for_state(state: str) -> str:
    if state == "block":
        return "block"
    if state == "warn":
        return "warn"
    return "pass"


def _recommended_action_for_state(state: str) -> str:
    if state == "block":
        return "block_report"
    if state == "warn":
        return "manual_review"
    return "accept"


def _summary_for_state(state: str, differences: list[str]) -> str | None:
    if state == "block":
        return "Synthetic demo AI second read flagged an amount mismatch."
    if state == "warn":
        if "date_mismatch" in differences:
            return "Synthetic demo AI second read flagged a date mismatch."
        return "Synthetic demo AI second read flagged a supplier mismatch."
    return None


def build_seed_plan(
    *,
    session: Session,
    receipt_id: int,
    state: str,
    amount: Decimal,
    currency: str,
    read_date: date,
    supplier: str,
) -> dict[str, Any]:
    receipt = session.get(ReceiptDocument, receipt_id)
    if receipt is None:
        raise ValueError(f"receipt_id {receipt_id} was not found")

    review_row = _latest_review_row(session, receipt_id)
    match = _latest_match_decision(session, receipt_id)
    snapshot = build_canonical_receipt_snapshot(receipt)
    snapshot_json = dumps(snapshot, sort_keys=True, separators=(",", ":"))
    snapshot_hash = canonical_receipt_snapshot_hash(snapshot)
    if state == "stale":
        snapshot_hash = "0" * 64

    differences = _differences_for_state(
        state=state,
        receipt=receipt,
        read_date=read_date,
        read_supplier=supplier,
    )
    read_payload = {
        "merchant_name": supplier,
        "receipt_date": read_date.isoformat(),
        "total_amount": format(amount, "f"),
        "currency": currency,
        "source": "synthetic_demo_agentdb_seed",
    }
    now = utc_now()
    risk_level = _risk_level_for_state(state)
    statuses = _comparison_statuses(differences)

    return {
        "receipt": receipt,
        "run": AgentReceiptReviewRun(
            receipt_document_id=receipt_id,
            review_session_id=review_row.review_session_id if review_row else None,
            review_row_id=review_row.id if review_row else None,
            statement_transaction_id=(
                review_row.statement_transaction_id
                if review_row is not None
                else match.statement_transaction_id if match is not None else None
            ),
            run_source="operator_demo_cli",
            run_kind="receipt_second_read",
            status="completed",
            schema_version=SCHEMA_VERSION,
            prompt_version=PROMPT_VERSION,
            prompt_hash="synthetic_demo_no_prompt",
            model_provider=None,
            model_name="synthetic_demo",
            comparator_version=COMPARATOR_VERSION,
            canonical_snapshot_json=snapshot_json,
            input_hash=_stable_hash({"snapshot": snapshot, "read": read_payload, "state": state}),
            raw_model_json=None,
            raw_model_json_redacted=True,
            prompt_text=None,
            started_at=now,
            completed_at=now,
        ),
        "read": AgentReceiptRead(
            run_id=0,
            receipt_document_id=receipt_id,
            read_schema_version=SCHEMA_VERSION,
            read_json=dumps(read_payload, sort_keys=True),
            extracted_date=read_date,
            extracted_supplier=supplier,
            amount_text=format(amount, "f"),
            local_amount_decimal=format(amount, "f"),
            currency=currency,
        ),
        "comparison": AgentReceiptComparison(
            run_id=0,
            agent_receipt_read_id=0,
            receipt_document_id=receipt_id,
            comparator_version=COMPARATOR_VERSION,
            risk_level=risk_level,
            recommended_action=_recommended_action_for_state(state),
            attention_required=False,
            amount_status=statuses["amount_status"],
            date_status=statuses["date_status"],
            currency_status=statuses["currency_status"],
            supplier_status=statuses["supplier_status"],
            business_context_status="complete",
            differences_json=dumps(differences, sort_keys=True),
            suggested_user_message=_summary_for_state(state, differences),
            canonical_snapshot_hash=snapshot_hash,
            agent_read_hash=_stable_hash(read_payload),
        ),
        "differences": differences,
    }


def seed_demo_review(
    *,
    db_path: str,
    receipt_id: int,
    state: str,
    amount: Decimal,
    currency: str,
    read_date: date,
    supplier: str,
    dry_run: bool,
    yes: bool,
) -> dict[str, Any]:
    engine = create_engine(f"sqlite:///{Path(db_path).resolve().as_posix()}")
    with Session(engine) as session:
        plan = build_seed_plan(
            session=session,
            receipt_id=receipt_id,
            state=state,
            amount=amount,
            currency=currency,
            read_date=read_date,
            supplier=supplier,
        )
        differences = plan["differences"]
        summary: dict[str, Any] = {
            "receipt_id": receipt_id,
            "state": state,
            "differences": differences,
            "dry_run": dry_run,
            "would_write": dry_run or not yes,
        }
        if dry_run or not yes:
            return summary

        run = plan["run"]
        read = plan["read"]
        comparison = plan["comparison"]

        session.add(run)
        session.flush()
        read.run_id = run.id or 0
        session.add(read)
        session.flush()
        comparison.run_id = run.id or 0
        comparison.agent_receipt_read_id = read.id or 0
        session.add(comparison)
        session.commit()
        session.refresh(run)
        session.refresh(read)
        session.refresh(comparison)

        summary.update(
            {
                "would_write": False,
                "run_id": run.id,
                "read_id": read.id,
                "comparison_id": comparison.id,
            }
        )
        return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seed one synthetic advisory AgentDB AI review for an existing receipt.",
    )
    parser.add_argument("--db-path", required=True, help="SQLite DB path.")
    parser.add_argument("--receipt-id", required=True, type=int, help="Existing ReceiptDocument id.")
    parser.add_argument("--state", required=True, choices=VALID_STATES)
    parser.add_argument("--amount", required=True, type=_parse_decimal)
    parser.add_argument("--currency", required=True)
    parser.add_argument("--date", required=True, type=_parse_date)
    parser.add_argument("--supplier", required=True)
    parser.add_argument("--dry-run", action="store_true", help="Print what would be inserted.")
    parser.add_argument("--yes", action="store_true", help="Required for actual writes.")
    parser.add_argument(
        "--i-understand-this-is-prod",
        action="store_true",
        help="Allow protected production DB paths. Actual protected writes still require --yes.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    protected, fragment = _is_protected_path(args.db_path)
    if protected and not args.i_understand_this_is_prod:
        print(
            f"REFUSED: {args.db_path!r} resolves under protected production path {fragment!r}. "
            "Pass --i-understand-this-is-prod only for an intentional operator action.",
            file=sys.stderr,
        )
        return 2

    try:
        summary = seed_demo_review(
            db_path=args.db_path,
            receipt_id=args.receipt_id,
            state=args.state,
            amount=args.amount,
            currency=args.currency,
            read_date=args.date,
            supplier=args.supplier,
            dry_run=args.dry_run,
            yes=args.yes,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if protected:
        summary["protected_path_acknowledged"] = True

    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
