from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlmodel import Session, create_engine

from app.config import get_settings
from app.models import ReceiptDocument
from app.services.agent_receipt_review_persistence import write_mock_agent_receipt_review
from app.services.agent_receipt_reviewer import AgentReceiptRead, compare_agent_receipt_read


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a mocked shadow AI receipt review from local JSON files. No model calls are made."
    )
    parser.add_argument("--canonical-json", type=Path, help="Path to canonical OCR JSON.")
    parser.add_argument("--agent-json", required=True, type=Path, help="Path to mocked agent receipt-read JSON.")
    parser.add_argument("--out", required=True, type=Path, help="Path to write the comparison result JSON.")
    parser.add_argument("--receipt-id", type=int, help="ReceiptDocument id to shadow-review when --write-db is set.")
    parser.add_argument("--db", help="SQLite database path or URL for --write-db mode.")
    parser.add_argument("--write-db", action="store_true", help="Persist a local/mock shadow review to AgentDB tables.")
    parser.add_argument("--mock", action="store_true", help="Use mocked local agent JSON. Required with --write-db.")
    parser.add_argument(
        "--date-tolerance-days",
        default=1,
        type=int,
        help="Allowed absolute date delta between canonical and agent receipt dates.",
    )
    args = parser.parse_args()

    if args.write_db:
        return _run_write_db(args, parser)

    if args.canonical_json is None:
        parser.error("--canonical-json is required unless --write-db is provided")

    canonical_fields = _load_json_object(args.canonical_json)
    agent_read = AgentReceiptRead.from_dict(_load_json_object(args.agent_json))
    result = compare_agent_receipt_read(
        canonical_fields,
        agent_read,
        date_tolerance_days=args.date_tolerance_days,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote shadow receipt review result to {args.out}")
    return 0


def _run_write_db(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if not args.mock:
        parser.error("--write-db requires --mock in this scaffold")
    if not args.db:
        parser.error("--write-db requires --db")
    if args.receipt_id is None:
        parser.error("--write-db requires --receipt-id")

    settings = get_settings()
    if not settings.ai_agent_db_write_enabled:
        parser.error("AI_AGENT_DB_WRITE_ENABLED must be true to use --write-db")

    agent_json_text = args.agent_json.read_text(encoding="utf-8")
    engine = create_engine(_sqlite_url(args.db), connect_args={"check_same_thread": False})
    with Session(engine) as session:
        receipt = session.get(ReceiptDocument, args.receipt_id)
        if receipt is None:
            raise SystemExit(f"ReceiptDocument {args.receipt_id} was not found")

        outcome = write_mock_agent_receipt_review(
            session,
            receipt=receipt,
            agent_json_text=agent_json_text,
            store_raw_model_json=settings.ai_store_raw_model_json,
            store_prompt_text=settings.ai_store_prompt_text,
        )
        run_id = outcome.run.id
        session.commit()

    if outcome.result is None:
        print(f"Agent receipt review failed: {outcome.error}", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(outcome.result.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote shadow receipt review result to {args.out}")
    print(f"Wrote AgentDB shadow review run id {run_id}")
    return 0


def _sqlite_url(raw: str) -> str:
    if raw.startswith("sqlite:"):
        return raw
    return f"sqlite:///{Path(raw).resolve().as_posix()}"


def _load_json_object(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
