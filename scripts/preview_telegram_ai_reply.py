"""F-AI-TG-0 dry-run preview for Telegram AI reply drafts.

Local-only utility that prints one of the deterministic draft templates
as JSON. No DB connection, no Telegram client, no live model call. Useful
to copy-paste a draft into Slack/notes during PM review.

    python scripts/preview_telegram_ai_reply.py --kind amount_mismatch

Exit code 0 on a known kind, 2 on unknown / no draft produced.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.telegram_ai_reply_drafts import (  # noqa: E402
    build_receipt_reply_draft,
    build_review_row_reply_draft,
)


def _draft_for_kind(kind: str) -> dict | None:
    if kind == "missing_business_reason":
        return build_receipt_reply_draft(
            {
                "business_or_personal": "Business",
                "business_reason": None,
                "attendees": "Hakan",
                "report_bucket": "Hotel/Lodging/Laundry",
            }
        )
    if kind == "missing_attendees":
        return build_receipt_reply_draft(
            {
                "business_or_personal": "Business",
                "business_reason": "Customer dinner",
                "attendees": None,
                "report_bucket": "Lunch",
            }
        )
    if kind == "amount_mismatch":
        return build_review_row_reply_draft(
            {
                "receipt_statement_issues": [
                    {"code": "receipt_statement_amount_mismatch"}
                ]
            }
        )
    if kind == "date_mismatch":
        return build_review_row_reply_draft(
            {
                "receipt_statement_issues": [
                    {"code": "receipt_statement_date_mismatch"}
                ]
            }
        )
    if kind == "ai_advisory_warning":
        return build_review_row_reply_draft({"ai_review": {"status": "warn"}})
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Preview a Telegram AI reply draft (no sending, no model calls)."
    )
    parser.add_argument(
        "--kind",
        required=True,
        choices=[
            "missing_business_reason",
            "missing_attendees",
            "amount_mismatch",
            "date_mismatch",
            "ai_advisory_warning",
        ],
    )
    args = parser.parse_args(argv)
    draft = _draft_for_kind(args.kind)
    if draft is None:
        print(json.dumps({"kind": "none", "send_allowed": False}, indent=2))
        return 2
    print(json.dumps(draft, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
