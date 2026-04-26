"""Schema migration: add suggested_bucket + suggested_category columns to matchdecision.

=======================================================================
READ THIS BEFORE RUNNING
=======================================================================

This script is idempotent — running it twice is safe. Each ADD COLUMN
is skipped if the column already exists.

Per operational rule #6, the live DB at /var/lib/dcexpense/expense_app.db
is migrated by:
  1. stop the service
  2. copy the live DB to /tmp/expense_app.db
  3. run THIS script with --apply against /tmp/expense_app.db
  4. verify the migrated copy
  5. copy /tmp/expense_app.db back to /var/lib/dcexpense/
  6. restart the service

This script enforces step 3 by refusing to run on the protected paths.

What this migration DOES (in a single transaction):

  1. ALTER TABLE matchdecision ADD COLUMN suggested_bucket VARCHAR (nullable)
  2. ALTER TABLE matchdecision ADD COLUMN suggested_category VARCHAR (nullable)
  3. Verify both columns are present post-add.

Existing rows on the matchdecision table get NULL for both new columns —
they were not produced by an LLM-disambiguation call that knew about
buckets, so attributing a bucket retroactively would lie about lineage.
Only future LLM-disambiguated matches populate these fields.

Why two columns rather than one: category is derivable from bucket via
the frontend's BUCKET_TO_CATEGORY map, BUT (a) the LLM is asked for both
explicitly so the audit trail captures "the model thinks both X and Y
fit," and (b) categories may diverge from the strict bucket→category
map if EDT introduces overlapping classifications later.

Run:

    # dry-run (default — no changes committed):
    python backend/migrations/m1_match_decision_bucket_columns.py \\
      --db-path /tmp/expense_app.db
    # apply:
    python backend/migrations/m1_match_decision_bucket_columns.py \\
      --db-path /tmp/expense_app.db --apply

Side effects on disk (written alongside the target DB) in --apply mode:

    <db>.pre-match-buckets-<UTC-timestamp>.backup        # full byte-for-byte copy
    <db>.pre-match-buckets-<UTC-timestamp>.migration.log # audit trail

Exit codes:

    0  success, or already-migrated no-op
    2  refused (production path, missing db, SQLite too old)
    3  runtime error during DDL (transaction rolled back)

Rollback procedure (if production deploy goes wrong after copy-back):

    1. sudo systemctl stop dcexpense.service
    2. sudo cp /var/lib/dcexpense/expense_app.db.pre-match-buckets-{ts}.backup \\
              /var/lib/dcexpense/expense_app.db
    3. Roll code back to the previous main commit (git revert or hard reset)
    4. sudo systemctl start dcexpense.service
    5. Verify /health returns 200

The .pre-match-buckets-{timestamp}.backup file is auto-created by --apply.

Partial rollback (drop the columns without restoring the file):

    SQLite supports DROP COLUMN since 3.35.0 (project minimum). Both
    columns are nullable additions, so removing them is non-destructive
    on rows that haven't been written by the new code yet:

      ALTER TABLE matchdecision DROP COLUMN suggested_bucket;
      ALTER TABLE matchdecision DROP COLUMN suggested_category;

    The full backup-restore is still the safer default.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from backend.migrations._common import (
    check_sqlite_version,
    column_info,
    migration_artifact_paths,
    refuse_protected_path,
    table_exists,
)


TABLE_NAME = "matchdecision"

# Each: (column_name, declared_type) — declared_type matches what SQLModel
# generates for ``str | None = None`` fields (VARCHAR, nullable by default).
NEW_COLUMNS: list[tuple[str, str]] = [
    ("suggested_bucket", "VARCHAR"),
    ("suggested_category", "VARCHAR"),
]


@dataclass
class MigrationResult:
    db_path: str
    backup_path: str | None
    log_path: str | None
    dry_run: bool
    already_migrated: bool
    columns_added: list[str]


def _column_present(conn: sqlite3.Connection, column: str) -> bool:
    info = column_info(conn, TABLE_NAME, column)
    return info is not None


def _add_column(
    conn: sqlite3.Connection, column: str, declared_type: str, logger: logging.Logger
) -> bool:
    """Add a single column if missing. Returns True if added, False if skipped."""
    if _column_present(conn, column):
        logger.info("skip ADD COLUMN %s: already present", column)
        return False
    sql = f"ALTER TABLE {TABLE_NAME} ADD COLUMN {column} {declared_type}"
    conn.execute(sql)
    logger.info(sql)
    return True


def _verify_post_state(conn: sqlite3.Connection, logger: logging.Logger) -> None:
    missing: list[str] = []
    for column, declared in NEW_COLUMNS:
        info = column_info(conn, TABLE_NAME, column)
        if info is None:
            missing.append(column)
            continue
        actual_type, _exists = info
        # SQLite stores types as the user wrote them; allow either VARCHAR or empty.
        # We're not strict on case because SQLite's PRAGMA returns the original
        # declaration, which for SQLModel is "VARCHAR".
        if actual_type and declared.upper() not in actual_type.upper():
            logger.warning(
                "column %s.%s declared as %r, expected %r — accepting since "
                "SQLite is dynamically typed but flagging for review",
                TABLE_NAME, column, actual_type, declared,
            )
    if missing:
        raise RuntimeError(
            f"VERIFY FAILED: missing columns after ALTER: {missing}"
        )
    logger.info(
        "verify OK: both new columns present on %s",
        TABLE_NAME,
    )


def migrate(db_path: str, *, apply: bool) -> MigrationResult:
    refuse_protected_path(db_path)
    check_sqlite_version()

    if not Path(db_path).exists():
        print(f"REFUSED: db path {db_path!r} does not exist.", file=sys.stderr)
        raise SystemExit(2)

    dry_run = not apply
    ts, projected_backup, projected_log = migration_artifact_paths(
        db_path, "match-buckets"
    )
    backup_path: str | None = None
    log_path: str | None = None
    logger = logging.getLogger(f"match_buckets.{ts}")
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

    logger.info("db_path=%s mode=%s", db_path, "apply" if apply else "dry-run")
    if backup_path:
        logger.info("backup_path=%s", backup_path)
    logger.info("sqlite version=%s", sqlite3.sqlite_version)

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = OFF")

    result = MigrationResult(
        db_path=db_path,
        backup_path=backup_path,
        log_path=log_path,
        dry_run=dry_run,
        already_migrated=False,
        columns_added=[],
    )

    try:
        if not table_exists(conn, TABLE_NAME):
            msg = (
                f"REFUSED: table {TABLE_NAME!r} does not exist; "
                f"this DB has not had the base schema applied."
            )
            logger.error(msg)
            print(msg, file=sys.stderr)
            conn.close()
            raise SystemExit(2)

        # — idempotency probe —
        already = all(_column_present(conn, col) for col, _ in NEW_COLUMNS)
        if already:
            logger.info(
                "already migrated: both %s columns present on %s. Exit 0.",
                [c for c, _ in NEW_COLUMNS], TABLE_NAME,
            )
            result.already_migrated = True
            conn.close()
            _print_summary(result)
            return result

        if dry_run:
            for column, declared in NEW_COLUMNS:
                if _column_present(conn, column):
                    logger.info("[dry-run] would skip %s (already present)", column)
                else:
                    logger.info(
                        "[dry-run] would ALTER TABLE %s ADD COLUMN %s %s",
                        TABLE_NAME, column, declared,
                    )
            conn.close()
            _print_summary(result)
            return result

        # — apply path —
        conn.execute("BEGIN")
        try:
            for column, declared in NEW_COLUMNS:
                if _add_column(conn, column, declared, logger):
                    result.columns_added.append(column)
            _verify_post_state(conn, logger)
            conn.execute("COMMIT")
            logger.info("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            logger.error("ROLLBACK")
            raise

    except SystemExit:
        conn.close()
        raise
    except Exception as exc:
        logger.exception("migration failed: %s", exc)
        print("ERROR: migration failed and was rolled back.", file=sys.stderr)
        if log_path:
            print(f"See log: {log_path}", file=sys.stderr)
        conn.close()
        raise SystemExit(3)
    finally:
        if isinstance(handler, logging.FileHandler):
            handler.close()

    conn.close()
    _print_summary(result)
    return result


def _print_summary(result: MigrationResult) -> None:
    mode = "DRY-RUN (no changes committed)" if result.dry_run else "APPLY (committed)"
    lines = [
        f"matchdecision-bucket-columns migration: {mode}",
        f"  db:     {result.db_path}",
    ]
    if result.backup_path:
        lines.append(f"  backup: {result.backup_path}")
    if result.log_path:
        lines.append(f"  log:    {result.log_path}")
    if result.already_migrated:
        lines.append("  state:  already migrated (no-op)")
    if result.columns_added:
        lines.append(f"  added:  {', '.join(result.columns_added)}")
    print("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Schema migration: add suggested_bucket + suggested_category "
            "columns to the matchdecision table. Default mode is dry-run; "
            "pass --apply to actually commit."
        )
    )
    parser.add_argument("--db-path", required=True, help="Path to SQLite database.")
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually commit the migration. Without this flag, the script "
             "runs in dry-run mode and projects what WOULD happen.",
    )
    args = parser.parse_args(argv)
    apply = bool(args.apply)
    migrate(args.db_path, apply=apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
