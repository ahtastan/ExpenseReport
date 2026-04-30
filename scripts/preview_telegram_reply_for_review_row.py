r"""F-AI-TG-1 operator-only review-row Telegram draft preview.

Read-only CLI that loads a single ReviewRow from a SQLite DB and prints the
deterministic Telegram draft JSON that the F-AI-TG-0 engine would produce
for it. No model calls. No DB writes. No Telegram client. No prod deploy.

Usage from PowerShell:

    python .\scripts\preview_telegram_reply_for_review_row.py `
      --db-path "$env:TEMP\smoke.db" `
      --review-row-id 1

    python .\scripts\preview_telegram_reply_for_review_row.py `
      --db-path "$env:TEMP\smoke.db" `
      --statement-import-id 2 `
      --receipt-id 41

Production paths under ``/var/lib/dcexpense`` and ``/opt/dcexpense`` are
refused unless the operator explicitly passes
``--i-understand-this-is-prod``. Even then the SQLite connection is opened
in URI ``mode=ro`` so the script physically cannot mutate the database.

Exit codes:
  0  draft printed (or no_draft_warranted printed)
  2  protected path refusal, review row not found, or argparse failure
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.telegram_ai_reply_drafts import (  # noqa: E402  (path setup above)
    build_review_row_reply_draft,
)


_PROTECTED_FRAGMENTS = ("/var/lib/dcexpense", "/opt/dcexpense")


def _refuse_protected_path(db_path: str, *, acknowledged: bool) -> None:
    raw = db_path.replace("\\", "/").lower()
    try:
        resolved = str(Path(db_path).resolve()).replace("\\", "/").lower()
    except OSError:
        resolved = raw
    for fragment in _PROTECTED_FRAGMENTS:
        if fragment in raw or fragment in resolved:
            if acknowledged:
                return
            print(
                f"REFUSED: {db_path!r} resolves to a protected production prefix "
                f"({fragment!r}). Pass --i-understand-this-is-prod to proceed in "
                f"read-only mode.",
                file=sys.stderr,
            )
            raise SystemExit(2)


def _open_readonly_connection(db_path: str) -> sqlite3.Connection:
    """Open the SQLite DB in URI read-only mode.

    ``mode=ro`` instructs the driver to refuse any write operation, even
    if the underlying file is writable. This is the load-bearing safety
    net for the ``--i-understand-this-is-prod`` path: if the path is the
    live prod DB, the connection cannot mutate it.
    """
    resolved = Path(db_path).resolve().as_posix()
    uri = f"file:{resolved}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _row_to_dict(cursor: sqlite3.Cursor, row: tuple[Any, ...] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


def _safe_loads(text: Any) -> dict[str, Any]:
    if not isinstance(text, str) or not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _load_review_row(
    conn: sqlite3.Connection,
    *,
    review_row_id: int | None,
    statement_import_id: int | None,
    receipt_id: int | None,
) -> dict[str, Any] | None:
    cur = conn.cursor()
    if review_row_id is not None:
        cur.execute(
            "SELECT id, statement_transaction_id, receipt_document_id, "
            "       source_json, confirmed_json "
            "FROM reviewrow WHERE id = ?",
            (review_row_id,),
        )
        record = _row_to_dict(cur, cur.fetchone())
    elif statement_import_id is not None and receipt_id is not None:
        cur.execute(
            "SELECT rr.id, rr.statement_transaction_id, rr.receipt_document_id, "
            "       rr.source_json, rr.confirmed_json "
            "FROM reviewrow rr "
            "JOIN statementtransaction st ON st.id = rr.statement_transaction_id "
            "WHERE st.statement_import_id = ? AND rr.receipt_document_id = ? "
            "ORDER BY rr.id DESC LIMIT 1",
            (statement_import_id, receipt_id),
        )
        record = _row_to_dict(cur, cur.fetchone())
    else:
        record = None

    if record is None:
        return None

    statement_import = statement_import_id
    if statement_import is None and record.get("statement_transaction_id") is not None:
        cur.execute(
            "SELECT statement_import_id FROM statementtransaction WHERE id = ?",
            (record["statement_transaction_id"],),
        )
        tx_row = cur.fetchone()
        if tx_row is not None:
            statement_import = tx_row[0]

    return {
        "review_row_id": record["id"],
        "receipt_id": record["receipt_document_id"],
        "statement_import_id": statement_import,
        "source": _safe_loads(record.get("source_json")),
        "confirmed": _safe_loads(record.get("confirmed_json")),
    }


def _build_draft_input(*, source: dict[str, Any], confirmed: dict[str, Any]) -> dict[str, Any]:
    """Assemble the input expected by build_review_row_reply_draft.

    The draft engine reads either a flat (``receipt_statement_issues`` /
    ``ai_review`` / ``receipt``) shape or the nested ``source.match.*`` /
    ``source.ai_review`` shape. Confirmed-row fields (business_or_personal
    etc.) are passed under ``receipt`` so the receipt-only fallback works.
    """
    return {
        "source": source,
        "receipt": {
            "business_or_personal": confirmed.get("business_or_personal"),
            "business_reason": confirmed.get("business_reason"),
            "attendees": confirmed.get("attendees"),
            "report_bucket": confirmed.get("report_bucket"),
        },
    }


def preview(
    *,
    db_path: str,
    review_row_id: int | None = None,
    statement_import_id: int | None = None,
    receipt_id: int | None = None,
    acknowledged: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Programmatic entry point. Returns ``(exit_code, payload)``.

    Refuses protected paths unless ``acknowledged`` is True. Always opens
    the DB in URI ``mode=ro`` so the script cannot mutate the source DB.
    """
    _refuse_protected_path(db_path, acknowledged=acknowledged)
    if not Path(db_path).exists():
        return 2, {"draft": None, "reason": "db_path_not_found", "db_path": db_path}

    with _open_readonly_connection(db_path) as conn:
        info = _load_review_row(
            conn,
            review_row_id=review_row_id,
            statement_import_id=statement_import_id,
            receipt_id=receipt_id,
        )

    if info is None:
        return 2, {"draft": None, "reason": "review_row_not_found"}

    draft_input = _build_draft_input(source=info["source"], confirmed=info["confirmed"])
    draft = build_review_row_reply_draft(draft_input)

    payload: dict[str, Any] = {
        "review_row_id": info["review_row_id"],
        "receipt_id": info["receipt_id"],
        "statement_import_id": info["statement_import_id"],
    }
    if draft is None:
        payload["draft"] = None
        payload["reason"] = "no_draft_warranted"
    else:
        payload["draft"] = draft

    return 0, payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Preview the deterministic Telegram reply draft for a review row. "
                    "Read-only. No model calls. No Telegram sending."
    )
    parser.add_argument("--db-path", required=True, help="Path to the SQLite DB.")
    parser.add_argument("--review-row-id", type=int, default=None)
    parser.add_argument("--statement-import-id", type=int, default=None)
    parser.add_argument("--receipt-id", type=int, default=None)
    parser.add_argument(
        "--i-understand-this-is-prod",
        action="store_true",
        help="Acknowledge a protected production path. The DB is still opened "
             "in URI read-only mode.",
    )
    args = parser.parse_args(argv)

    if args.review_row_id is None and (
        args.statement_import_id is None or args.receipt_id is None
    ):
        parser.error(
            "either --review-row-id, OR both --statement-import-id and "
            "--receipt-id, must be provided"
        )

    code, payload = preview(
        db_path=args.db_path,
        review_row_id=args.review_row_id,
        statement_import_id=args.statement_import_id,
        receipt_id=args.receipt_id,
        acknowledged=args.i_understand_this_is_prod,
    )
    print(json.dumps(payload, indent=2, default=str))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
