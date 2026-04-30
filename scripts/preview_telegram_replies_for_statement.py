r"""F-AI-TG-3 batch Telegram draft preview for a statement import.

Read-only operator CLI. Loads every ReviewRow for one statement import and
prints the deterministic Telegram draft JSON (or null) per row, using the
F-AI-TG-0 draft engine. No model calls. No DB writes. No Telegram client.

Usage from PowerShell:

    python .\scripts\preview_telegram_replies_for_statement.py `
      --db-path "$env:TEMP\smoke.db" `
      --statement-import-id 2

    python .\scripts\preview_telegram_replies_for_statement.py `
      --db-path "$env:TEMP\smoke.db" `
      --statement-import-id 2 `
      --only-with-drafts

Production paths under ``/var/lib/dcexpense`` and ``/opt/dcexpense`` are
refused unless ``--i-understand-this-is-prod`` is passed. Even then the
SQLite connection is opened in URI ``mode=ro`` (inherited from the
F-AI-TG-1 script's helper) so the script physically cannot mutate the
database.

Exit codes:
  0  preview printed
  2  protected path refusal, DB not found, or argparse failure
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
BACKEND_ROOT = REPO_ROOT / "backend"
for _p in (SCRIPTS_ROOT, BACKEND_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Reuse the F-AI-TG-1 helpers so the protected-path / read-only / draft-input
# logic stays the load-bearing single source of truth. Importing private
# names is intentional here: they're stable, small, and module-internal,
# and the alternative is a refactor PR that touches an already-merged file.
from preview_telegram_reply_for_review_row import (  # noqa: E402  (sys.path setup above)
    _build_draft_input,
    _open_readonly_connection,
    _refuse_protected_path,
    _safe_loads,
)
from app.services.telegram_ai_reply_drafts import (  # noqa: E402
    build_review_row_reply_draft,
)


def _load_rows_for_statement(
    conn: sqlite3.Connection, statement_import_id: int
) -> list[tuple[int, int, int, str, str]]:
    cur = conn.cursor()
    cur.execute(
        "SELECT rr.id, rr.statement_transaction_id, rr.receipt_document_id, "
        "       rr.source_json, rr.confirmed_json "
        "FROM reviewrow rr "
        "JOIN statementtransaction st ON st.id = rr.statement_transaction_id "
        "WHERE st.statement_import_id = ? "
        "ORDER BY rr.id ASC",
        (statement_import_id,),
    )
    return cur.fetchall()


def preview_statement(
    *,
    db_path: str,
    statement_import_id: int,
    only_with_drafts: bool = False,
    acknowledged: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Return ``(exit_code, payload)`` for one statement's draft preview.

    Always read-only. Refuses production paths unless ``acknowledged=True``.
    """
    _refuse_protected_path(db_path, acknowledged=acknowledged)
    if not Path(db_path).exists():
        return 2, {
            "statement_import_id": statement_import_id,
            "row_count": 0,
            "draft_count": 0,
            "rows": [],
            "reason": "db_path_not_found",
            "db_path": db_path,
        }

    rows_payload: list[dict[str, Any]] = []
    draft_count = 0
    with _open_readonly_connection(db_path) as conn:
        raw_rows = _load_rows_for_statement(conn, statement_import_id)
        for review_row_id, tx_id, receipt_id, source_json, confirmed_json in raw_rows:
            source = _safe_loads(source_json)
            confirmed = _safe_loads(confirmed_json)
            draft_input = _build_draft_input(source=source, confirmed=confirmed)
            draft = build_review_row_reply_draft(draft_input)
            entry = {
                "review_row_id": review_row_id,
                "receipt_id": receipt_id,
                "statement_transaction_id": tx_id,
                "draft": draft,
            }
            if draft is not None:
                draft_count += 1
            if only_with_drafts and draft is None:
                continue
            rows_payload.append(entry)

    return 0, {
        "statement_import_id": statement_import_id,
        "row_count": len(raw_rows),
        "draft_count": draft_count,
        "rows": rows_payload,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Preview deterministic Telegram reply drafts for every review row "
                    "in a statement import. Read-only. No model calls. No Telegram sending."
    )
    parser.add_argument("--db-path", required=True, help="Path to the SQLite DB.")
    parser.add_argument("--statement-import-id", required=True, type=int)
    parser.add_argument(
        "--only-with-drafts",
        action="store_true",
        help="Suppress rows whose draft is null.",
    )
    parser.add_argument(
        "--i-understand-this-is-prod",
        action="store_true",
        help="Acknowledge a protected production path. The DB is still opened "
             "in URI read-only mode.",
    )
    args = parser.parse_args(argv)
    code, payload = preview_statement(
        db_path=args.db_path,
        statement_import_id=args.statement_import_id,
        only_with_drafts=args.only_with_drafts,
        acknowledged=args.i_understand_this_is_prod,
    )
    print(json.dumps(payload, indent=2, default=str))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
