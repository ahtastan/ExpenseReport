"""F-AI-0b-1 migration: create shadow AI receipt review tables.

This migration is additive and idempotent. It creates only:

  * agent_receipt_review_run
  * agent_receipt_read
  * agent_receipt_comparison

It does not modify, backfill, drop, or rewrite canonical expense tables.
Default mode is dry-run; pass --apply to commit the DDL. Protected production
paths are refused by the shared migration guard.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from backend.migrations._common import (
        check_sqlite_version,
        migration_artifact_paths,
        refuse_protected_path,
        table_exists,
    )
except ModuleNotFoundError:  # pytest runs from backend/
    from migrations._common import (
        check_sqlite_version,
        migration_artifact_paths,
        refuse_protected_path,
        table_exists,
    )


MIGRATION_ID = "f-ai-0b1-agent-receipt-review"
TABLE_NAMES = [
    "agent_receipt_review_run",
    "agent_receipt_read",
    "agent_receipt_comparison",
]

CREATE_TABLE_SQL = [
    """
    CREATE TABLE IF NOT EXISTS agent_receipt_review_run (
        id INTEGER NOT NULL,
        receipt_document_id INTEGER NOT NULL,
        review_session_id INTEGER,
        review_row_id INTEGER,
        statement_transaction_id INTEGER,
        run_source VARCHAR NOT NULL DEFAULT 'local_cli',
        run_kind VARCHAR NOT NULL DEFAULT 'receipt_second_read',
        status VARCHAR NOT NULL,
        schema_version VARCHAR NOT NULL,
        prompt_version VARCHAR NOT NULL,
        prompt_hash VARCHAR,
        model_provider VARCHAR,
        model_name VARCHAR NOT NULL DEFAULT 'local_mock',
        comparator_version VARCHAR NOT NULL,
        app_git_sha VARCHAR,
        canonical_snapshot_json TEXT NOT NULL DEFAULT '{}',
        statement_snapshot_json TEXT,
        input_hash VARCHAR,
        raw_model_json TEXT,
        raw_model_json_redacted BOOLEAN NOT NULL DEFAULT 1,
        prompt_text TEXT,
        error_code VARCHAR,
        error_message TEXT,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        started_at DATETIME,
        completed_at DATETIME,
        PRIMARY KEY (id),
        FOREIGN KEY(receipt_document_id) REFERENCES receiptdocument (id),
        FOREIGN KEY(review_session_id) REFERENCES reviewsession (id),
        FOREIGN KEY(review_row_id) REFERENCES reviewrow (id),
        FOREIGN KEY(statement_transaction_id) REFERENCES statementtransaction (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_receipt_read (
        id INTEGER NOT NULL,
        run_id INTEGER NOT NULL,
        receipt_document_id INTEGER NOT NULL,
        read_schema_version VARCHAR NOT NULL,
        read_json TEXT NOT NULL DEFAULT '{}',
        extracted_date DATE,
        extracted_supplier VARCHAR,
        amount_text VARCHAR,
        local_amount_decimal VARCHAR,
        local_amount_minor INTEGER,
        amount_scale INTEGER,
        currency VARCHAR,
        receipt_type VARCHAR,
        business_or_personal VARCHAR,
        business_reason TEXT,
        attendees_json TEXT,
        confidence_json TEXT,
        evidence_json TEXT,
        warnings_json TEXT,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (id),
        FOREIGN KEY(run_id) REFERENCES agent_receipt_review_run (id),
        FOREIGN KEY(receipt_document_id) REFERENCES receiptdocument (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_receipt_comparison (
        id INTEGER NOT NULL,
        run_id INTEGER NOT NULL,
        agent_receipt_read_id INTEGER NOT NULL,
        receipt_document_id INTEGER NOT NULL,
        comparator_version VARCHAR NOT NULL,
        risk_level VARCHAR NOT NULL,
        recommended_action VARCHAR NOT NULL,
        attention_required BOOLEAN NOT NULL DEFAULT 0,
        amount_status VARCHAR,
        date_status VARCHAR,
        currency_status VARCHAR,
        supplier_status VARCHAR,
        business_context_status VARCHAR,
        differences_json TEXT NOT NULL DEFAULT '[]',
        suggested_user_message TEXT,
        ai_review_note TEXT,
        canonical_snapshot_hash VARCHAR,
        agent_read_hash VARCHAR,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (id),
        FOREIGN KEY(run_id) REFERENCES agent_receipt_review_run (id),
        FOREIGN KEY(agent_receipt_read_id) REFERENCES agent_receipt_read (id),
        FOREIGN KEY(receipt_document_id) REFERENCES receiptdocument (id)
    )
    """,
]

INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_review_run_receipt_document_id ON agent_receipt_review_run(receipt_document_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_review_run_review_session_id ON agent_receipt_review_run(review_session_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_review_run_review_row_id ON agent_receipt_review_run(review_row_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_review_run_statement_transaction_id ON agent_receipt_review_run(statement_transaction_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_review_run_run_source ON agent_receipt_review_run(run_source)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_review_run_run_kind ON agent_receipt_review_run(run_kind)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_review_run_status ON agent_receipt_review_run(status)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_review_run_schema_version ON agent_receipt_review_run(schema_version)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_review_run_prompt_version ON agent_receipt_review_run(prompt_version)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_review_run_prompt_hash ON agent_receipt_review_run(prompt_hash)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_review_run_model_provider ON agent_receipt_review_run(model_provider)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_review_run_model_name ON agent_receipt_review_run(model_name)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_review_run_comparator_version ON agent_receipt_review_run(comparator_version)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_review_run_input_hash ON agent_receipt_review_run(input_hash)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_read_run_id ON agent_receipt_read(run_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_read_receipt_document_id ON agent_receipt_read(receipt_document_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_read_read_schema_version ON agent_receipt_read(read_schema_version)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_read_currency ON agent_receipt_read(currency)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_read_receipt_type ON agent_receipt_read(receipt_type)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_read_business_or_personal ON agent_receipt_read(business_or_personal)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_comparison_run_id ON agent_receipt_comparison(run_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_comparison_agent_receipt_read_id ON agent_receipt_comparison(agent_receipt_read_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_comparison_receipt_document_id ON agent_receipt_comparison(receipt_document_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_comparison_comparator_version ON agent_receipt_comparison(comparator_version)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_comparison_risk_level ON agent_receipt_comparison(risk_level)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_comparison_recommended_action ON agent_receipt_comparison(recommended_action)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_comparison_attention_required ON agent_receipt_comparison(attention_required)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_comparison_amount_status ON agent_receipt_comparison(amount_status)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_comparison_date_status ON agent_receipt_comparison(date_status)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_comparison_currency_status ON agent_receipt_comparison(currency_status)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_comparison_supplier_status ON agent_receipt_comparison(supplier_status)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_comparison_business_context_status ON agent_receipt_comparison(business_context_status)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_comparison_canonical_snapshot_hash ON agent_receipt_comparison(canonical_snapshot_hash)",
    "CREATE INDEX IF NOT EXISTS ix_agent_receipt_comparison_agent_read_hash ON agent_receipt_comparison(agent_read_hash)",
]


@dataclass
class MigrationResult:
    db_path: str
    backup_path: str | None
    log_path: str | None
    dry_run: bool
    tables_created: list[str]


def migrate(db_path: str, *, apply: bool) -> MigrationResult:
    refuse_protected_path(db_path)
    check_sqlite_version()

    if not Path(db_path).exists():
        print(f"REFUSED: db path {db_path!r} does not exist.", file=sys.stderr)
        raise SystemExit(2)

    dry_run = not apply
    ts, projected_backup, projected_log = migration_artifact_paths(db_path, MIGRATION_ID)
    backup_path: str | None = None
    log_path: str | None = None
    logger = logging.getLogger(f"agentdb.{ts}")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)

    if apply:
        backup_path = projected_backup
        log_path = projected_log
        shutil.copy2(db_path, backup_path)
        handler: logging.Handler = logging.FileHandler(log_path, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = OFF")
    missing_before = [name for name in TABLE_NAMES if not table_exists(conn, name)]
    result = MigrationResult(
        db_path=db_path,
        backup_path=backup_path,
        log_path=log_path,
        dry_run=dry_run,
        tables_created=[] if dry_run else missing_before,
    )

    try:
        logger.info("db_path=%s mode=%s", db_path, "apply" if apply else "dry-run")
        if dry_run:
            for table_name in missing_before:
                logger.info("[dry-run] would create table %s", table_name)
            _print_summary(result)
            return result

        conn.execute("BEGIN")
        try:
            for sql in CREATE_TABLE_SQL:
                conn.execute(sql)
            for sql in INDEX_SQL:
                conn.execute(sql)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

        missing_after = [name for name in TABLE_NAMES if not table_exists(conn, name)]
        if missing_after:
            raise RuntimeError(f"VERIFY FAILED: missing tables after migration: {missing_after}")
        _print_summary(result)
        return result
    except SystemExit:
        raise
    except Exception as exc:
        logger.exception("migration failed: %s", exc)
        print("ERROR: migration failed and was rolled back.", file=sys.stderr)
        raise SystemExit(3)
    finally:
        conn.close()
        if isinstance(handler, logging.FileHandler):
            handler.close()


def _print_summary(result: MigrationResult) -> None:
    mode = "DRY-RUN (no changes committed)" if result.dry_run else "APPLY (committed)"
    lines = [
        f"agent receipt review tables migration: {mode}",
        f"  db:     {result.db_path}",
    ]
    if result.backup_path:
        lines.append(f"  backup: {result.backup_path}")
    if result.log_path:
        lines.append(f"  log:    {result.log_path}")
    if result.tables_created:
        lines.append(f"  created: {', '.join(result.tables_created)}")
    print("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create F-AI-0b-1 shadow AI receipt review tables. Default mode is dry-run."
    )
    parser.add_argument("--db-path", required=True, help="Path to SQLite database.")
    parser.add_argument("--apply", action="store_true", help="Actually commit the migration.")
    args = parser.parse_args(argv)
    migrate(args.db_path, apply=args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
