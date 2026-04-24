"""M1 Day 1 schema migration: ExpenseReport + FxRate + nullable FKs.

=======================================================================
READ THIS BEFORE RUNNING
=======================================================================

This script is idempotent. Running it twice is safe. Running it against
production paths is refused: ``/var/lib/dcexpense`` and ``/opt/dcexpense``
trigger a hard exit before any read, backup, or write.

What this migration DOES:

  1.  Creates tables ``expensereport`` and ``fxrate``.
  2.  ADD COLUMN (nullable):
        - appuser.current_report_id
        - appuser.current_report_set_at
        - receiptdocument.expense_report_id
        - reviewsession.expense_report_id
        - reportrun.expense_report_id
  3.  Backfills one ``expensereport`` row per existing ``statementimport``
      (``report_kind='diners_statement'``, ``status='submitted'``,
      ``report_currency='USD'``). Title is built from cardholder + period;
      falls back to source_filename when both are null.
  4.  Sets ``expense_report_id`` on every existing ``reviewsession`` and
      ``reportrun`` that references a backfilled statement.
  5.  Sets ``expense_report_id`` on every ``receiptdocument`` that has an
      approved ``matchdecision`` pointing at a ``statementtransaction``
      whose ``statementimport`` was just backfilled. Receipts without any
      approved match stay ``NULL``; operators link them later via
      M1 Day 4+ endpoints.

What this migration does NOT do (loud, intentional):

  *  It does NOT relax SQLite NOT NULL on
     ``reviewsession.statement_import_id`` or
     ``reportrun.statement_import_id``. SQLite cannot change NOT NULL via
     ALTER. Legacy rows keep their non-null value. The Python/SQLModel
     layer treats them as ``int | None`` so new code can leave them unset
     on ExpenseReport-only inserts. If you need to enforce nullability at
     the SQL layer, that's a table-rebuild (M2 via Alembic), not this.
  *  It does NOT enforce the new FKs at the SQLite level. SQLite ALTER
     TABLE ADD COLUMN cannot attach FK constraints to an existing table.
     The SQLModel/SQLAlchemy layer still emits FKs on CREATE and is the
     source of truth for integrity at the application layer.
  *  It does NOT create a synthetic owner user. If a StatementImport has
     ``uploader_user_id IS NULL`` and ``appuser`` has no row with id=1,
     the migration aborts cleanly BEFORE any mutation, listing the
     offending statement IDs. You resolve those rows manually, then
     re-run.

Run:

    python backend/migrations/m1_day1_expensereport_schema.py <db_path>

Side effects on disk (written alongside the target DB):

    <db>.pre-m1-day1-<UTC-timestamp>.backup        # full byte-for-byte copy
    <db>.pre-m1-day1-<UTC-timestamp>.migration.log # audit trail (one line per step)

Exit codes:

    0  success, or already-migrated no-op
    2  refused (production path, missing db, validation failure)
    3  runtime error during DDL or backfill (transaction rolled back)
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


PROTECTED_PATH_FRAGMENTS = ("/var/lib/dcexpense", "/opt/dcexpense")

NEW_COLUMNS = {
    "appuser": [
        ("current_report_id", "INTEGER"),
        ("current_report_set_at", "DATETIME"),
    ],
    "receiptdocument": [
        ("expense_report_id", "INTEGER"),
    ],
    "reviewsession": [
        ("expense_report_id", "INTEGER"),
    ],
    "reportrun": [
        ("expense_report_id", "INTEGER"),
    ],
}

NEW_INDEXES = [
    ("ix_appuser_current_report_id", "appuser", "current_report_id"),
    ("ix_receiptdocument_expense_report_id", "receiptdocument", "expense_report_id"),
    ("ix_reviewsession_expense_report_id", "reviewsession", "expense_report_id"),
    ("ix_reportrun_expense_report_id", "reportrun", "expense_report_id"),
]

EXPENSEREPORT_DDL = """
CREATE TABLE IF NOT EXISTS expensereport (
    id INTEGER NOT NULL PRIMARY KEY,
    owner_user_id INTEGER NOT NULL,
    report_kind VARCHAR NOT NULL,
    title VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    period_start DATE,
    period_end DATE,
    report_currency VARCHAR NOT NULL,
    statement_import_id INTEGER,
    notes VARCHAR,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    FOREIGN KEY(owner_user_id) REFERENCES appuser(id),
    FOREIGN KEY(statement_import_id) REFERENCES statementimport(id)
)
"""

EXPENSEREPORT_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_expensereport_owner_user_id ON expensereport(owner_user_id)",
    "CREATE INDEX IF NOT EXISTS ix_expensereport_report_kind ON expensereport(report_kind)",
    "CREATE INDEX IF NOT EXISTS ix_expensereport_status ON expensereport(status)",
    "CREATE INDEX IF NOT EXISTS ix_expensereport_statement_import_id ON expensereport(statement_import_id)",
]

FXRATE_DDL = """
CREATE TABLE IF NOT EXISTS fxrate (
    id INTEGER NOT NULL PRIMARY KEY,
    rate_date DATE NOT NULL,
    from_currency VARCHAR NOT NULL,
    to_currency VARCHAR NOT NULL,
    rate FLOAT NOT NULL,
    source VARCHAR NOT NULL,
    fetched_at DATETIME NOT NULL
)
"""

FXRATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_fxrate_rate_date ON fxrate(rate_date)",
    "CREATE INDEX IF NOT EXISTS ix_fxrate_from_currency ON fxrate(from_currency)",
    "CREATE INDEX IF NOT EXISTS ix_fxrate_to_currency ON fxrate(to_currency)",
]


@dataclass
class MigrationResult:
    db_path: str
    backup_path: str | None
    log_path: str | None
    already_migrated: bool
    statements_backfilled: int
    reviewsessions_linked: int
    reportruns_linked: int
    receipts_linked: int
    receipts_unlinked: int
    receipts_ambiguous: int
    warnings: list[str]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _refuse_protected_path(db_path: str) -> None:
    norm = db_path.replace("\\", "/").lower()
    for fragment in PROTECTED_PATH_FRAGMENTS:
        if fragment in norm:
            print(
                f"REFUSED: db path {db_path!r} matches protected production path "
                f"fragment {fragment!r}. This migration will not touch production.",
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


def _count(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _all_new_columns_present(conn: sqlite3.Connection) -> bool:
    """True iff every pending ADD COLUMN has landed.

    A table that doesn't exist at all is treated as vacuously migrated for its
    columns (nothing to add there). Only tables that DO exist must carry the
    new columns.
    """
    for table, cols in NEW_COLUMNS.items():
        if not _table_exists(conn, table):
            continue
        for name, _sqltype in cols:
            if not _column_exists(conn, table, name):
                return False
    return _table_exists(conn, "expensereport") and _table_exists(conn, "fxrate")


def _every_statement_backfilled(conn: sqlite3.Connection) -> bool:
    if not _table_exists(conn, "statementimport") or not _table_exists(conn, "expensereport"):
        return False
    missing = conn.execute(
        """
        SELECT COUNT(*)
        FROM statementimport si
        LEFT JOIN expensereport er ON er.statement_import_id = si.id
        WHERE er.id IS NULL
        """
    ).fetchone()[0]
    return missing == 0


def _build_title(
    cardholder_name: str | None,
    period_start: str | None,
    period_end: str | None,
    source_filename: str | None,
) -> str:
    parts: list[str] = []
    if cardholder_name:
        parts.append(cardholder_name.strip())
    if period_start and period_end:
        parts.append(f"{period_start}–{period_end}")
    elif period_start:
        parts.append(str(period_start))
    elif period_end:
        parts.append(str(period_end))
    if parts:
        return "Diners statement " + " ".join(parts)
    if source_filename:
        return f"Diners statement {source_filename}"
    return "Diners statement (untitled)"


def _find_orphan_statements(conn: sqlite3.Connection) -> list[int]:
    """Return ids of StatementImport rows that have no viable owner.

    A row is orphaned when ``uploader_user_id IS NULL`` and ``appuser`` has
    no row with ``id=1`` to fall back to. The migration refuses to proceed
    while any such rows exist.
    """
    has_user_one = conn.execute(
        "SELECT 1 FROM appuser WHERE id = 1"
    ).fetchone() is not None
    if has_user_one:
        return []
    rows = conn.execute(
        "SELECT id FROM statementimport WHERE uploader_user_id IS NULL ORDER BY id"
    ).fetchall()
    return [row[0] for row in rows]


# ---------------------------------------------------------------------------
# main migration
# ---------------------------------------------------------------------------


def migrate(db_path: str) -> MigrationResult:
    """Run the M1 Day 1 migration. Safe to call multiple times."""
    _refuse_protected_path(db_path)

    if not Path(db_path).exists():
        print(f"REFUSED: db path {db_path!r} does not exist.", file=sys.stderr)
        raise SystemExit(2)

    ts = _timestamp()
    backup_path = f"{db_path}.pre-m1-day1-{ts}.backup"
    log_path = f"{db_path}.pre-m1-day1-{ts}.migration.log"

    # Always take a fresh backup before doing anything that touches the DB.
    # If we abort for validation reasons the backup is still there, untouched.
    shutil.copy2(db_path, backup_path)

    logger = logging.getLogger(f"m1_day1.{ts}")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False

    warnings: list[str] = []

    def warn(msg: str) -> None:
        warnings.append(msg)
        logger.warning(msg)

    logger.info("db_path=%s", db_path)
    logger.info("backup_path=%s", backup_path)

    # isolation_level=None gives us manual transaction control. Without it,
    # Python 3.11's sqlite3 auto-commits around DDL (CREATE/ALTER), which
    # leaves no active transaction for an explicit COMMIT to close when a
    # re-run has no DML to execute.
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = OFF")  # we manage FK integrity explicitly

    try:
        # --- Idempotency probe ------------------------------------------------
        if _all_new_columns_present(conn) and _every_statement_backfilled(conn):
            logger.info("already migrated: new columns present and every "
                        "statementimport has a backfilled expensereport. Exit 0.")
            conn.close()
            print("already migrated (no-op)")
            print(f"log: {log_path}")
            return MigrationResult(
                db_path=db_path,
                backup_path=backup_path,
                log_path=log_path,
                already_migrated=True,
                statements_backfilled=0,
                reviewsessions_linked=0,
                reportruns_linked=0,
                receipts_linked=0,
                receipts_unlinked=0,
                receipts_ambiguous=0,
                warnings=warnings,
            )

        # --- Pre-validation (no writes) ---------------------------------------
        if _table_exists(conn, "statementimport") and _table_exists(conn, "appuser"):
            orphans = _find_orphan_statements(conn)
            if orphans:
                msg = (
                    "REFUSED: StatementImport rows with uploader_user_id NULL "
                    "and no appuser.id=1 fallback. Resolve these rows manually "
                    f"and re-run. Offending statement IDs: {orphans}"
                )
                logger.error(msg)
                print(msg, file=sys.stderr)
                conn.close()
                raise SystemExit(2)

        # --- DDL + backfill inside an explicit transaction --------------------
        # conn begins a transaction implicitly on the first DML. Issue BEGIN
        # so that CREATE/ALTER land inside it too; if anything fails we ROLLBACK
        # and leave the DB byte-identical to the backup we just wrote.
        conn.execute("BEGIN")
        try:
            # Create new tables (and indexes). Use plain execute (not
            # executescript) because executescript issues a COMMIT before
            # running, which would close our explicit transaction.
            conn.execute(EXPENSEREPORT_DDL)
            for ddl in EXPENSEREPORT_INDEXES:
                conn.execute(ddl)
            logger.info("ensured table expensereport + indexes")

            conn.execute(FXRATE_DDL)
            for ddl in FXRATE_INDEXES:
                conn.execute(ddl)
            logger.info("ensured table fxrate + indexes")

            # Add new columns (per-column guard = idempotent).
            for table, cols in NEW_COLUMNS.items():
                if not _table_exists(conn, table):
                    logger.info("skipping %s: table not present", table)
                    continue
                for name, sqltype in cols:
                    if _column_exists(conn, table, name):
                        logger.info("column %s.%s already present", table, name)
                        continue
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sqltype}")
                    logger.info("added column %s.%s %s", table, name, sqltype)

            # Indexes on newly-added columns (guarded by IF NOT EXISTS).
            for index_name, table, column in NEW_INDEXES:
                if not _table_exists(conn, table):
                    continue
                if not _column_exists(conn, table, column):
                    continue
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {index_name} ON {table}({column})"
                )

            # --- Backfill: one expensereport per statementimport -------------
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            statements_backfilled = 0
            if _table_exists(conn, "statementimport"):
                rows = conn.execute(
                    """
                    SELECT id, uploader_user_id, source_filename, statement_date,
                           period_start, period_end, cardholder_name, company_name,
                           created_at
                    FROM statementimport
                    ORDER BY id
                    """
                ).fetchall()
                for row in rows:
                    (
                        statement_id,
                        uploader_user_id,
                        source_filename,
                        _statement_date,
                        period_start,
                        period_end,
                        cardholder_name,
                        _company_name,
                        created_at,
                    ) = row
                    existing = conn.execute(
                        "SELECT id FROM expensereport WHERE statement_import_id = ?",
                        (statement_id,),
                    ).fetchone()
                    if existing is not None:
                        logger.info(
                            "statement id=%s already has expensereport id=%s",
                            statement_id,
                            existing[0],
                        )
                        continue
                    if uploader_user_id is None:
                        # Pre-validation already refused to run if appuser.id=1
                        # is missing, so this branch is safe.
                        uploader_user_id = 1
                        warn(
                            f"statement id={statement_id} had uploader_user_id NULL; "
                            "fell back to appuser.id=1"
                        )
                    title = _build_title(
                        cardholder_name, period_start, period_end, source_filename
                    )
                    created_iso = created_at or now_iso
                    conn.execute(
                        """
                        INSERT INTO expensereport (
                            owner_user_id, report_kind, title, status,
                            period_start, period_end, report_currency,
                            statement_import_id, notes, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            uploader_user_id,
                            "diners_statement",
                            title,
                            "submitted",
                            period_start,
                            period_end,
                            "USD",
                            statement_id,
                            None,
                            created_iso,
                            created_iso,
                        ),
                    )
                    statements_backfilled += 1
                    logger.info(
                        "backfilled expensereport for statement id=%s title=%r",
                        statement_id,
                        title,
                    )

            # --- Link existing reviewsession / reportrun ---------------------
            reviewsessions_linked = 0
            if _table_exists(conn, "reviewsession"):
                cursor = conn.execute(
                    """
                    UPDATE reviewsession
                    SET expense_report_id = (
                        SELECT er.id FROM expensereport er
                        WHERE er.statement_import_id = reviewsession.statement_import_id
                        LIMIT 1
                    )
                    WHERE expense_report_id IS NULL
                      AND statement_import_id IS NOT NULL
                    """
                )
                reviewsessions_linked = cursor.rowcount or 0
                logger.info("linked %d reviewsession rows", reviewsessions_linked)

            reportruns_linked = 0
            if _table_exists(conn, "reportrun"):
                cursor = conn.execute(
                    """
                    UPDATE reportrun
                    SET expense_report_id = (
                        SELECT er.id FROM expensereport er
                        WHERE er.statement_import_id = reportrun.statement_import_id
                        LIMIT 1
                    )
                    WHERE expense_report_id IS NULL
                      AND statement_import_id IS NOT NULL
                    """
                )
                reportruns_linked = cursor.rowcount or 0
                logger.info("linked %d reportrun rows", reportruns_linked)

            # --- Link receiptdocument via approved matchdecision -------------
            receipts_linked = 0
            receipts_ambiguous = 0
            if (
                _table_exists(conn, "receiptdocument")
                and _table_exists(conn, "matchdecision")
                and _table_exists(conn, "statementtransaction")
            ):
                # Pick the expense report derived from the approved match with
                # the smallest matchdecision.id for each receipt. Ambiguous
                # = receipt that has >1 distinct candidate expense_report_id.
                candidates = conn.execute(
                    """
                    SELECT md.receipt_document_id, er.id AS expense_report_id,
                           md.id AS md_id
                    FROM matchdecision md
                    JOIN statementtransaction st ON st.id = md.statement_transaction_id
                    JOIN expensereport er ON er.statement_import_id = st.statement_import_id
                    WHERE md.approved = 1
                    ORDER BY md.receipt_document_id, md.id
                    """
                ).fetchall()

                by_receipt: dict[int, list[tuple[int, int]]] = {}
                for receipt_id, expense_report_id, md_id in candidates:
                    by_receipt.setdefault(receipt_id, []).append(
                        (expense_report_id, md_id)
                    )

                for receipt_id, rows in by_receipt.items():
                    distinct_reports = {er_id for er_id, _ in rows}
                    chosen_er, chosen_md = rows[0]  # smallest md_id via ORDER BY
                    if len(distinct_reports) > 1:
                        receipts_ambiguous += 1
                        warn(
                            f"receipt id={receipt_id} has approved matches "
                            f"pointing at multiple expense reports "
                            f"{sorted(distinct_reports)}; picking "
                            f"expense_report_id={chosen_er} via matchdecision "
                            f"id={chosen_md}"
                        )
                    # Only set if currently NULL — never overwrite an operator choice.
                    cursor = conn.execute(
                        """
                        UPDATE receiptdocument
                        SET expense_report_id = ?
                        WHERE id = ? AND expense_report_id IS NULL
                        """,
                        (chosen_er, receipt_id),
                    )
                    if cursor.rowcount:
                        receipts_linked += 1
                        logger.info(
                            "linked receipt id=%s -> expense_report_id=%s",
                            receipt_id,
                            chosen_er,
                        )

            # Count receipts left unlinked for the summary (NULL post-migration).
            receipts_unlinked = 0
            if _table_exists(conn, "receiptdocument"):
                receipts_unlinked = conn.execute(
                    "SELECT COUNT(*) FROM receiptdocument WHERE expense_report_id IS NULL"
                ).fetchone()[0]

            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    except SystemExit:
        conn.close()
        raise
    except Exception as exc:  # pragma: no cover - defensive path
        logger.exception("migration failed: %s", exc)
        print(
            f"ERROR: migration failed and was rolled back. See log: {log_path}",
            file=sys.stderr,
        )
        conn.close()
        raise SystemExit(3)
    finally:
        handler.close()

    conn.close()

    summary = (
        f"M1 Day 1 migration complete.\n"
        f"  db:                         {db_path}\n"
        f"  backup:                     {backup_path}\n"
        f"  log:                        {log_path}\n"
        f"  expense reports backfilled: {statements_backfilled}\n"
        f"  review sessions linked:     {reviewsessions_linked}\n"
        f"  report runs linked:         {reportruns_linked}\n"
        f"  receipts linked:            {receipts_linked}\n"
        f"  receipts unlinked:          {receipts_unlinked}\n"
        f"  receipts ambiguous:         {receipts_ambiguous}\n"
        f"  warnings:                   {len(warnings)}\n"
    )
    print(summary)
    _tail_log(log_path)

    return MigrationResult(
        db_path=db_path,
        backup_path=backup_path,
        log_path=log_path,
        already_migrated=False,
        statements_backfilled=statements_backfilled,
        reviewsessions_linked=reviewsessions_linked,
        reportruns_linked=reportruns_linked,
        receipts_linked=receipts_linked,
        receipts_unlinked=receipts_unlinked,
        receipts_ambiguous=receipts_ambiguous,
        warnings=warnings,
    )


def _tail_log(log_path: str, lines: int = 20) -> None:
    try:
        content = Path(log_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    tail = content[-lines:]
    print("--- last %d log lines ---" % len(tail))
    for line in tail:
        print(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("db_path", help="Path to the SQLite database file.")
    args = parser.parse_args(argv)
    migrate(args.db_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
