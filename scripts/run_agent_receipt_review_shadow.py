"""Operator-only mock shadow AI receipt review runner.

This is the F-AI-0d shadow-runner path for creating AgentDB AI second-read
rows from an existing ``ReceiptDocument``. The only supported provider is
``mock``. No live model modules are imported, no AI flags are required, and no
canonical expense data is mutated.

Dry-run:

    python scripts/run_agent_receipt_review_shadow.py ^
      --db-path C:\\tmp\\expense_app.db ^
      --receipt-id 41 ^
      --provider mock ^
      --dry-run

Write:

    python scripts/run_agent_receipt_review_shadow.py ^
      --db-path C:\\tmp\\expense_app.db ^
      --receipt-id 41 ^
      --provider mock ^
      --yes

Mock controls:

    python scripts/run_agent_receipt_review_shadow.py ^
      --db-path C:\\tmp\\expense_app.db ^
      --receipt-id 41 ^
      --provider mock ^
      --mock-state warn ^
      --mock-amount 999.99 ^
      --mock-currency TRY ^
      --mock-date 2025-12-27 ^
      --mock-supplier "TRANSIT CUP" ^
      --yes
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlmodel import Session, select

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.models import (  # noqa: E402
    AgentReceiptComparison,
    AgentReceiptRead as AgentReceiptReadRow,
    ReceiptDocument,
)
from app.services.agent_receipt_review_persistence import (  # noqa: E402
    build_canonical_receipt_snapshot,
    write_mock_agent_receipt_review,
)
from app.services.agent_receipt_reviewer import (  # noqa: E402
    AgentReceiptRead,
    compare_agent_receipt_read,
)

PROTECTED_PATH_FRAGMENTS = ("/var/lib/dcexpense", "/opt/dcexpense")


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


def _coerce_snapshot_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _coerce_snapshot_amount(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _shift_date(value: date | None) -> date | None:
    return value - timedelta(days=3) if value is not None else None


def build_mock_agent_payload(
    canonical: dict[str, Any],
    *,
    mock_state: str,
    mock_amount: Decimal | None,
    mock_currency: str | None,
    mock_date: date | None,
    mock_supplier: str | None,
) -> dict[str, Any]:
    canonical_date = _coerce_snapshot_date(canonical.get("date"))
    canonical_amount = _coerce_snapshot_amount(canonical.get("amount"))
    canonical_currency = canonical.get("currency")
    canonical_supplier = canonical.get("supplier")

    amount = mock_amount if mock_amount is not None else canonical_amount
    currency = mock_currency if mock_currency is not None else canonical_currency
    receipt_date = mock_date if mock_date is not None else canonical_date
    supplier = mock_supplier if mock_supplier is not None else canonical_supplier

    if mock_state == "warn":
        receipt_date = mock_date if mock_date is not None else _shift_date(canonical_date)
        supplier = mock_supplier if mock_supplier is not None else "MOCK WARN SUPPLIER"
    elif mock_state == "block":
        if mock_amount is None and canonical_amount is not None:
            amount = canonical_amount + Decimal("1.00")
    elif mock_state != "pass":
        raise ValueError(f"unsupported mock state {mock_state!r}")

    return {
        "merchant_name": supplier,
        "merchant_address": None,
        "receipt_date": receipt_date.isoformat() if receipt_date else None,
        "receipt_time": None,
        "total_amount": format(amount, "f") if amount is not None else None,
        "currency": currency,
        "amount_text": format(amount, "f") if amount is not None else None,
        "line_items": [],
        "tax_amount": None,
        "payment_method": None,
        "receipt_category": canonical.get("receipt_type"),
        "confidence": 1.0,
        "raw_text_summary": "Deterministic mock shadow read; no model call.",
    }


def _sqlite_url(path: str) -> str:
    return f"sqlite:///{Path(path).resolve().as_posix()}"


def _ids_for_run(session: Session, run_id: int | None) -> tuple[int | None, int | None]:
    if run_id is None:
        return None, None
    read_row = session.exec(
        select(AgentReceiptReadRow).where(AgentReceiptReadRow.run_id == run_id).order_by(AgentReceiptReadRow.id.desc())
    ).first()
    comparison = session.exec(
        select(AgentReceiptComparison)
        .where(AgentReceiptComparison.run_id == run_id)
        .order_by(AgentReceiptComparison.id.desc())
    ).first()
    return read_row.id if read_row else None, comparison.id if comparison else None


def run_shadow_review(
    *,
    db_path: str,
    receipt_id: int,
    provider: str,
    mock_state: str,
    mock_amount: Decimal | None,
    mock_currency: str | None,
    mock_date: date | None,
    mock_supplier: str | None,
    dry_run: bool,
    yes: bool,
) -> dict[str, Any]:
    if provider != "mock":
        raise ValueError("only provider=mock is supported")

    engine = create_engine(_sqlite_url(db_path), connect_args={"check_same_thread": False})
    with Session(engine) as session:
        receipt = session.get(ReceiptDocument, receipt_id)
        if receipt is None:
            raise ValueError(f"receipt_id {receipt_id} was not found")

        canonical = build_canonical_receipt_snapshot(receipt)
        mock_payload = build_mock_agent_payload(
            canonical,
            mock_state=mock_state,
            mock_amount=mock_amount,
            mock_currency=mock_currency,
            mock_date=mock_date,
            mock_supplier=mock_supplier,
        )
        result = compare_agent_receipt_read(canonical, AgentReceiptRead.from_dict(mock_payload))
        would_write = dry_run or not yes
        summary: dict[str, Any] = {
            "receipt_id": receipt_id,
            "provider": provider,
            "dry_run": dry_run,
            "would_write": would_write,
            "risk_level": result.comparison.risk_level,
            "differences": result.comparison.differences,
        }
        if would_write:
            return summary

        outcome = write_mock_agent_receipt_review(
            session,
            receipt=receipt,
            agent_json_text=json.dumps(mock_payload, sort_keys=True),
            run_source="operator_shadow_cli",
            store_raw_model_json=False,
            store_prompt_text=False,
        )
        if outcome.result is None:
            raise ValueError(outcome.error or "mock shadow review failed")
        outcome.run.model_name = provider
        session.flush()
        read_id, comparison_id = _ids_for_run(session, outcome.run.id)
        session.commit()
        summary.update(
            {
                "run_id": outcome.run.id,
                "read_id": read_id,
                "comparison_id": comparison_id,
                "risk_level": outcome.result.comparison.risk_level,
                "differences": outcome.result.comparison.differences,
            }
        )
        return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an operator-only mock shadow AI receipt review into AgentDB.",
    )
    parser.add_argument("--db-path", required=True, help="SQLite DB path.")
    parser.add_argument("--receipt-id", required=True, type=int, help="Existing ReceiptDocument id.")
    parser.add_argument("--provider", required=True, choices=["mock"])
    parser.add_argument("--mock-state", choices=["pass", "warn", "block"], default="pass")
    parser.add_argument("--mock-amount", type=_parse_decimal)
    parser.add_argument("--mock-currency")
    parser.add_argument("--mock-date", type=_parse_date)
    parser.add_argument("--mock-supplier")
    parser.add_argument("--dry-run", action="store_true", help="Print comparison without writing AgentDB rows.")
    parser.add_argument("--yes", action="store_true", help="Required for actual AgentDB writes.")
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
        summary = run_shadow_review(
            db_path=args.db_path,
            receipt_id=args.receipt_id,
            provider=args.provider,
            mock_state=args.mock_state,
            mock_amount=args.mock_amount,
            mock_currency=args.mock_currency,
            mock_date=args.mock_date,
            mock_supplier=args.mock_supplier,
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
