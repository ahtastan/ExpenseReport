"""M1 Day 2 schema migration: ReceiptDocument.receipt_type column.

=======================================================================
READ THIS BEFORE RUNNING
=======================================================================

This script is idempotent. Running it twice is safe. Running it against
production paths is refused: ``/var/lib/dcexpense`` and ``/opt/dcexpense``
trigger a hard exit before any read, backup, or write.

What this migration DOES:

  1. ADD COLUMN (nullable) receiptdocument.receipt_type VARCHAR(50)
     Values: 'itemized' | 'payment_receipt' | 'invoice' | 'confirmation'
             | 'unknown' | NULL (not yet classified)
  2. CREATE INDEX IF NOT EXISTS ix_receiptdocument_receipt_type ON
     receiptdocument(receipt_type)

What this migration does NOT do:

  * No backfill. NULL is the "not yet classified" sentinel and is the
    intentional default. Retroactive classification is a separate step
    (backend/scripts/classify_existing_receipts.py) that walks the
    storage path and calls the vision model.
  * No DB-level CHECK constraint on the allowed values. Enforcement
    happens in ``app.services.receipt_extraction`` when writing.

Run:

    python backend/migrations/m1_day2_receipt_type.py <db_path>

Side effects on disk (written alongside the target DB):

    <db>.pre-m1-day2-<UTC-timestamp>.backup
    <db>.pre-m1-day2-<UTC-timestamp>.migration.log

Exit codes:

    0  success, or already-migrated no-op
    2  refused (production path, missing db)
    3  runtime error during DDL (transaction rolled back)
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


PROTECTED_PATH_FRAGMENTS = ("/var/lib/dcexpense", "/opt/dcexpense")

NEW_COLUMN = ("receiptdocument", "receipt_type", "VARCHAR(50)")
NEW_INDEX = ("ix_receiptdocument_receipt_type", "receiptdocument", "receipt_type")


@dataclass
class MigrationResult:
    db_path: str
    backup_path: str
    log_path: str
    already_migrated: bool


def _refuse_protected_path(db_path: str) -> None:
    resolved = str(Path(db_path).resolve()).replace("\\", "/")
    for fragment in PROTECTED_PATH_FRAGMENTS:
        if fragment in resolved:
            print(
                f"REFUSED: {db_path!r} matches protected path fragment "
                f"{fragment!r}. Copy the DB to a scratch location and run "
                "the migration there.",
                file=sys.stderr,
            )
            raise SystemExit(2)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _index_exists(conn: sqlite3.Connection, index_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    ).fetchone()
    return row is not None


def migrate(db_path: str) -> MigrationResult:
    _refuse_protected_path(db_path)

    if not Path(db_path).exists():
        print(f"REFUSED: db path {db_path!r} does not exist.", file=sys.stderr)
        raise SystemExit(2)

    ts = _timestamp()
    backup_path = f"{db_path}.pre-m1-day2-{ts}.backup"
    log_path = f"{db_path}.pre-m1-day2-{ts}.migration.log"

    shutil.copy2(db_path, backup_path)

    logger = logging.getLogger(f"m1_day2.{ts}")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False

    logger.info("db_path=%s", db_path)
    logger.info("backup_path=%s", backup_path)

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = OFF")

    try:
        table, column, sqltype = NEW_COLUMN
        index_name, index_table, index_column = NEW_INDEX

        already = (
            _table_exists(conn, table)
            and _column_exists(conn, table, column)
            and _index_exists(conn, index_name)
        )
        if already:
            logger.info("already migrated: receipt_type column + index present")
            conn.close()
            print("already migrated (no-op)")
            print(f"log: {log_path}")
            return MigrationResult(
                db_path=db_path,
                backup_path=backup_path,
                log_path=log_path,
                already_migrated=True,
            )

        if not _table_exists(conn, table):
            msg = f"REFUSED: table {table!r} is not present in the database."
            logger.error(msg)
            print(msg, file=sys.stderr)
            conn.close()
            raise SystemExit(2)

        conn.execute("BEGIN")
        try:
            if not _column_exists(conn, table, column):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sqltype}")
                logger.info("added column %s.%s %s", table, column, sqltype)
            else:
                logger.info("column %s.%s already present", table, column)

            conn.execute(
                f"CREATE INDEX IF NOT EXISTS {index_name} "
                f"ON {index_table}({index_column})"
            )
            logger.info("ensured index %s on %s(%s)", index_name, index_table, index_column)

            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

        conn.close()
        print(f"migrated: added {table}.{column} + index {index_name}")
        print(f"backup: {backup_path}")
        print(f"log: {log_path}")
        return MigrationResult(
            db_path=db_path,
            backup_path=backup_path,
            log_path=log_path,
            already_migrated=False,
        )
    except BaseException as exc:
        logger.error("migration failed: %r", exc)
        try:
            conn.close()
        except Exception:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M1 Day 2 migration — ReceiptDocument.receipt_type")
    parser.add_argument("db_path", help="Path to the SQLite database file to migrate.")
    args = parser.parse_args(argv)
    try:
        migrate(args.db_path)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
