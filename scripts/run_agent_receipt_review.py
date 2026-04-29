from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.agent_receipt_reviewer import AgentReceiptRead, compare_agent_receipt_read


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a mocked shadow AI receipt review from local JSON files. No model calls are made."
    )
    parser.add_argument("--canonical-json", required=True, type=Path, help="Path to canonical OCR JSON.")
    parser.add_argument("--agent-json", required=True, type=Path, help="Path to mocked agent receipt-read JSON.")
    parser.add_argument("--out", required=True, type=Path, help="Path to write the comparison result JSON.")
    parser.add_argument(
        "--date-tolerance-days",
        default=1,
        type=int,
        help="Allowed absolute date delta between canonical and agent receipt dates.",
    )
    args = parser.parse_args()

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


def _load_json_object(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
