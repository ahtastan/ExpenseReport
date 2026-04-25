"""M1 Day 2.5 schema migration: money + rate columns from REAL to NUMERIC.

=======================================================================
READ THIS BEFORE RUNNING
=======================================================================

This script is idempotent. Running it twice is safe. Running it against
production paths is refused: ``/var/lib/dcexpense`` and ``/opt/dcexpense``
trigger a hard exit before any read, backup, or write.

Per operational rule #6, the live DB at /var/lib/dcexpense/expense_app.db
is migrated by:
  1. stop the service
  2. copy the live DB to /tmp/expense_app.db
  3. run THIS script with --apply against /tmp/expense_app.db
  4. verify the migrated copy
  5. copy /tmp/expense_app.db back to /var/lib/dcexpense/
  6. restart the service

This script enforces step 3 by refusing to run on the protected paths.

What this migration DOES:

  Per column, in a single transaction:
    1. ALTER TABLE … ADD COLUMN <col>_new NUMERIC(18,4)   (or NUMERIC(18,8) for rates)
    2. UPDATE table SET <col>_new = ROUND(<col>, 4) WHERE <col> IS NOT NULL
       (8 dp for the rate column). NULLs propagate via ROUND(NULL, n) = NULL.
    3. Per-row verify: every row where (old IS NULL) iff (new IS NULL),
       and ABS(old - new) < epsilon. Any mismatch aborts and rolls back.
    4. Aggregate verify: COUNT(*) WHERE old IS NOT NULL must equal the
       same for new; ABS(SUM(old) - SUM(new)) < epsilon * row_count.
    5. ALTER TABLE … DROP COLUMN <col>
    6. ALTER TABLE … RENAME COLUMN <col>_new TO <col>
    7. Recreate any indexes that were dropped with the old column.

Columns migrated (4 total):
  - receiptdocument.extracted_local_amount → NUMERIC(18,4)
  - statementtransaction.local_amount      → NUMERIC(18,4)  [INDEX]
  - statementtransaction.usd_amount        → NUMERIC(18,4)
  - fxrate.rate                            → NUMERIC(18,8)

Idempotency probe: a column whose declared type already contains "NUMERIC"
is skipped. Re-running on a fully-migrated DB is a clean no-op.

SQLite version requirement: ≥ 3.35.0 (DROP COLUMN landed March 2021).
The script aborts with a clear error if the bundled SQLite is older. If
production is ever pinned to an older SQLite, switch to the 12-step
ALTER TABLE rebuild — out of scope for this script.

Run:

    python backend/migrations/m1_day25_money_decimal.py --db-path /tmp/expense_app.db --dry-run
    python backend/migrations/m1_day25_money_decimal.py --db-path /tmp/expense_app.db --apply

Side effects on disk (written alongside the target DB) in --apply mode:

    <db>.pre-m1-day25-<UTC-timestamp>.backup        # full byte-for-byte copy
    <db>.pre-m1-day25-<UTC-timestamp>.migration.log # audit trail

Exit codes:

    0  success, or already-migrated no-op
    2  refused (production path, missing db, SQLite too old, verification failed)
    3  runtime error during DDL or backfill (transaction rolled back)
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


PROTECTED_PATH_FRAGMENTS = ("/var/lib/dcexpense", "/opt/dcexpense")

MIN_SQLITE_VERSION = (3, 35, 0)

# (table, column, declared_type, decimal_places_for_round, epsilon_for_per_row_check)
COLUMNS: list[tuple[str, str, str, int, str]] = [
    ("receiptdocument", "extracted_local_amount", "NUMERIC(18,4)", 4, "0.0001"),
    ("statementtransaction", "local_amount", "NUMERIC(18,4)", 4, "0.0001"),
    ("statementtransaction", "usd_amount", "NUMERIC(18,4)", 4, "0.0001"),
    ("fxrate", "rate", "NUMERIC(18,8)", 8, "0.00000001"),
]

# Indexes to recreate after the column rename. SQLite drops any index
# referencing a column when that column is dropped, so the rename does NOT
# carry the index over — it has to be rebuilt explicitly.
INDEXES_TO_RECREATE: list[tuple[str, str, str]] = [
    # (index_name, table, column)
    ("ix_statementtransaction_local_amount", "statementtransaction", "local_amount"),
]


@dataclass
class ColumnReport:
    table: str
    column: str
    skipped: bool = False  # already migrated
    skipped_reason: str = ""
    rows_total: int = 0
    rows_non_null: int = 0
    sum_before: Decimal | None = None
    sum_after: Decimal | None = None


@dataclass
class MigrationResult:
    db_path: str
    backup_path: str | None
    log_path: str | None
    dry_run: bool
    already_migrated: bool
    columns: list[ColumnReport] = field(default_factory=list)
    indexes_recreated: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# guards & helpers
# ---------------------------------------------------------------------------


def _refuse_protected_path(db_path: str) -> None:
    """Hard refusal if the path resolves to a protected production prefix.

    Uses Path.resolve() so symlinks and relative paths can't smuggle a
    protected location past a literal-substring check.
    """
    raw = db_path.replace("\\", "/").lower()
    resolved = str(Path(db_path).resolve()).replace("\\", "/").lower()
    for fragment in PROTECTED_PATH_FRAGMENTS:
        for candidate in (raw, resolved):
            if fragment in candidate:
                print(
                    f"REFUSED: {db_path!r} resolves to a path matching protected "
                    f"fragment {fragment!r}.\n"
                    "  Per operational rule #6, copy the live DB to /tmp first, "
                    "run the migration on the copy, then copy back.\n"
                    "  This script will not migrate the live file in place.",
                    file=sys.stderr,
                )
                raise SystemExit(2)


def _check_sqlite_version() -> None:
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        have = ".".join(str(x) for x in sqlite3.sqlite_version_info)
        need = ".".join(str(x) for x in MIN_SQLITE_VERSION)
        print(
            f"REFUSED: SQLite {have} bundled with this Python is too old. "
            f"This migration uses ALTER TABLE DROP COLUMN which requires "
            f"SQLite ≥ {need}.",
            file=sys.stderr,
        )
        raise SystemExit(2)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _column_info(conn: sqlite3.Connection, table: str, column: str) -> tuple[str, bool] | None:
    """Return (declared_type, exists) for table.column, or None if absent."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    for row in rows:
        # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk)
        if row[1] == column:
            return (row[2] or "", True)
    return None


def _is_numeric_declared(declared_type: str) -> bool:
    """True iff the column's declared type indicates NUMERIC affinity for our migration.

    We're checking whether a *previous* run already migrated this column.
    Match on "NUMERIC" substring (case-insensitive); rejects "REAL", "FLOAT".
    """
    return "NUMERIC" in (declared_type or "").upper()


def _index_exists(conn: sqlite3.Connection, index_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# per-column migration
# ---------------------------------------------------------------------------


def _indexes_on_column(
    conn: sqlite3.Connection, table: str, column: str
) -> list[str]:
    """Return names of every index on (table, column).

    SQLite refuses to DROP COLUMN if any index references it, so we have to
    drop those indexes first and recreate them after the rename.
    """
    indexes = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
        (table,),
    ).fetchall()
    referencing: list[str] = []
    for (idx_name,) in indexes:
        # Skip auto-indexes (PRIMARY KEY / UNIQUE) — they have names like
        # "sqlite_autoindex_..." and aren't user-managed.
        if idx_name.startswith("sqlite_"):
            continue
        cols = conn.execute(f"PRAGMA index_info({idx_name})").fetchall()
        if any(row[2] == column for row in cols):
            referencing.append(idx_name)
    return referencing


def _migrate_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    declared_type: str,
    decimals: int,
    epsilon: str,
    logger: logging.Logger,
    dry_run: bool,
) -> ColumnReport:
    report = ColumnReport(table=table, column=column)

    if not _table_exists(conn, table):
        report.skipped = True
        report.skipped_reason = "table not present"
        logger.info("skip %s.%s: table not present", table, column)
        return report

    info = _column_info(conn, table, column)
    if info is None:
        report.skipped = True
        report.skipped_reason = "column not present"
        logger.info("skip %s.%s: column not present", table, column)
        return report

    current_declared, _ = info
    if _is_numeric_declared(current_declared):
        report.skipped = True
        report.skipped_reason = f"already NUMERIC ({current_declared!r})"
        logger.info(
            "skip %s.%s: already migrated (declared %r)", table, column, current_declared
        )
        return report

    new_col = f"{column}_new"

    # --- pre-migration metrics -----------------------------------------------
    rows_total, rows_non_null, sum_before = conn.execute(
        f"SELECT COUNT(*), SUM(CASE WHEN {column} IS NOT NULL THEN 1 ELSE 0 END), "
        f"SUM({column}) FROM {table}"
    ).fetchone()
    report.rows_total = rows_total or 0
    report.rows_non_null = rows_non_null or 0
    report.sum_before = Decimal(str(sum_before)) if sum_before is not None else None

    logger.info(
        "%s.%s: %d rows total, %d non-null, sum=%s, declared=%r",
        table,
        column,
        report.rows_total,
        report.rows_non_null,
        report.sum_before,
        current_declared,
    )

    if dry_run:
        # Simulate the round so the dry-run report shows the post-state SUM.
        # Use SQLite's ROUND so numbers match what --apply would produce.
        sum_after_row = conn.execute(
            f"SELECT SUM(ROUND({column}, ?)) FROM {table}", (decimals,)
        ).fetchone()
        sum_after = sum_after_row[0]
        report.sum_after = Decimal(str(sum_after)) if sum_after is not None else None
        logger.info(
            "[dry-run] would migrate %s.%s; ROUND-projected sum=%s",
            table,
            column,
            report.sum_after,
        )
        return report

    # --- ADD NEW COLUMN -------------------------------------------------------
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {new_col} {declared_type}")
    logger.info("ALTER TABLE %s ADD COLUMN %s %s", table, new_col, declared_type)

    # --- BACKFILL -------------------------------------------------------------
    conn.execute(
        f"UPDATE {table} SET {new_col} = ROUND({column}, ?) WHERE {column} IS NOT NULL",
        (decimals,),
    )

    # --- VERIFY: per-row ------------------------------------------------------
    # Any row where the NULL pattern differs OR the rounded value drifted
    # beyond epsilon causes an abort. Decimal/float comparison happens inside
    # SQLite where both columns are REAL-affinity here (NUMERIC stored as REAL
    # via affinity rules), so ABS(diff) is a numeric comparison.
    bad_rows = conn.execute(
        f"""
        SELECT id, {column}, {new_col}
        FROM {table}
        WHERE NOT (
            ({column} IS NULL AND {new_col} IS NULL)
            OR ({column} IS NOT NULL AND {new_col} IS NOT NULL
                AND ABS({column} - {new_col}) < ?)
        )
        """,
        (float(epsilon),),
    ).fetchall()
    if bad_rows:
        sample = ", ".join(
            f"id={r[0]} old={r[1]!r} new={r[2]!r}" for r in bad_rows[:10]
        )
        msg = (
            f"VERIFY FAILED (per-row) for {table}.{column}: "
            f"{len(bad_rows)} row(s) drifted beyond epsilon={epsilon}. "
            f"Sample: {sample}"
        )
        logger.error(msg)
        raise RuntimeError(msg)

    # --- VERIFY: aggregate ----------------------------------------------------
    nn_old, nn_new, sum_old, sum_new = conn.execute(
        f"""
        SELECT
            SUM(CASE WHEN {column} IS NOT NULL THEN 1 ELSE 0 END),
            SUM(CASE WHEN {new_col} IS NOT NULL THEN 1 ELSE 0 END),
            SUM({column}),
            SUM({new_col})
        FROM {table}
        """
    ).fetchone()
    if nn_old != nn_new:
        msg = (
            f"VERIFY FAILED (aggregate) for {table}.{column}: "
            f"non-null count drifted: old={nn_old} new={nn_new}"
        )
        logger.error(msg)
        raise RuntimeError(msg)

    if (sum_old is None) != (sum_new is None):
        msg = (
            f"VERIFY FAILED (aggregate) for {table}.{column}: "
            f"sum nullness drifted: old={sum_old} new={sum_new}"
        )
        logger.error(msg)
        raise RuntimeError(msg)

    if sum_old is not None:
        # Cumulative drift bound: per-row error ≤ epsilon/2 (round-half), so
        # total absolute drift across N rows is ≤ epsilon/2 * N. Use epsilon * N
        # as a conservative tolerance.
        tolerance = float(epsilon) * max(report.rows_non_null, 1)
        if abs(sum_old - sum_new) > tolerance:
            msg = (
                f"VERIFY FAILED (aggregate) for {table}.{column}: "
                f"|SUM(old)-SUM(new)|={abs(sum_old - sum_new)} > tolerance={tolerance} "
                f"(epsilon={epsilon} × {report.rows_non_null} non-null rows). "
                f"old={sum_old} new={sum_new}"
            )
            logger.error(msg)
            raise RuntimeError(msg)

    report.sum_after = Decimal(str(sum_new)) if sum_new is not None else None
    logger.info(
        "verify OK %s.%s: per-row diffs all < %s, aggregate sum=%s (was %s)",
        table,
        column,
        epsilon,
        report.sum_after,
        report.sum_before,
    )

    # --- DROP indexes referencing the old column ------------------------------
    # SQLite refuses ALTER TABLE DROP COLUMN if any index references the
    # column. We drop those indexes here and let _recreate_dropped_indexes
    # rebuild them on the renamed column after the rename completes.
    for idx_name in _indexes_on_column(conn, table, column):
        conn.execute(f"DROP INDEX {idx_name}")
        logger.info("DROP INDEX %s (referenced %s.%s)", idx_name, table, column)

    # --- DROP + RENAME --------------------------------------------------------
    conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    logger.info("ALTER TABLE %s DROP COLUMN %s", table, column)

    conn.execute(f"ALTER TABLE {table} RENAME COLUMN {new_col} TO {column}")
    logger.info("ALTER TABLE %s RENAME COLUMN %s TO %s", table, new_col, column)

    return report


def _recreate_dropped_indexes(
    conn: sqlite3.Connection, logger: logging.Logger
) -> list[str]:
    recreated: list[str] = []
    for index_name, table, column in INDEXES_TO_RECREATE:
        if not _table_exists(conn, table):
            continue
        if _column_info(conn, table, column) is None:
            continue
        if _index_exists(conn, index_name):
            logger.info("index %s already present, skipping", index_name)
            continue
        conn.execute(f"CREATE INDEX {index_name} ON {table}({column})")
        recreated.append(index_name)
        logger.info("CREATE INDEX %s ON %s(%s)", index_name, table, column)
    return recreated


# ---------------------------------------------------------------------------
# main migration
# ---------------------------------------------------------------------------


def migrate(db_path: str, *, apply: bool) -> MigrationResult:
    _refuse_protected_path(db_path)
    _check_sqlite_version()

    if not Path(db_path).exists():
        print(f"REFUSED: db path {db_path!r} does not exist.", file=sys.stderr)
        raise SystemExit(2)

    dry_run = not apply
    ts = _timestamp()
    backup_path: str | None = None
    log_path: str | None = None
    logger_name = f"m1_day25.{ts}"
    logger = logging.getLogger(logger_name)
    logger.handlers.clear()
    logger.setLevel(logging.INFO)

    if apply:
        backup_path = f"{db_path}.pre-m1-day25-{ts}.backup"
        log_path = f"{db_path}.pre-m1-day25-{ts}.migration.log"
        shutil.copy2(db_path, backup_path)
        handler: logging.Handler = logging.FileHandler(log_path, encoding="utf-8")
    else:
        # Dry-run still emits an audit trail to stdout so PM can review the
        # exact step-by-step intent without touching the disk.
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

    column_reports: list[ColumnReport] = []
    indexes_recreated: list[str] = []
    already_migrated = True

    try:
        if apply:
            conn.execute("BEGIN")
        try:
            for table, column, declared_type, decimals, epsilon in COLUMNS:
                report = _migrate_column(
                    conn,
                    table=table,
                    column=column,
                    declared_type=declared_type,
                    decimals=decimals,
                    epsilon=epsilon,
                    logger=logger,
                    dry_run=dry_run,
                )
                column_reports.append(report)
                if not report.skipped:
                    already_migrated = False

            # Recreate indexes only on apply (dry-run wouldn't have dropped them).
            if apply:
                indexes_recreated = _recreate_dropped_indexes(conn, logger)

            if apply:
                conn.execute("COMMIT")
                logger.info("COMMIT")
        except BaseException:
            if apply:
                conn.execute("ROLLBACK")
                logger.error("ROLLBACK")
            raise
    except SystemExit:
        conn.close()
        raise
    except Exception as exc:
        logger.exception("migration failed: %s", exc)
        print(
            f"ERROR: migration failed{' and was rolled back' if apply else ''}.",
            file=sys.stderr,
        )
        if log_path:
            print(f"See log: {log_path}", file=sys.stderr)
        conn.close()
        raise SystemExit(3)
    finally:
        if isinstance(handler, logging.FileHandler):
            handler.close()

    conn.close()

    result = MigrationResult(
        db_path=db_path,
        backup_path=backup_path,
        log_path=log_path,
        dry_run=dry_run,
        already_migrated=already_migrated,
        columns=column_reports,
        indexes_recreated=indexes_recreated,
    )
    _print_summary(result)
    return result


def _print_summary(result: MigrationResult) -> None:
    mode = "DRY-RUN (no changes committed)" if result.dry_run else "APPLY (committed)"
    lines = [
        f"M1 Day 2.5 migration: {mode}",
        f"  db:     {result.db_path}",
    ]
    if result.backup_path:
        lines.append(f"  backup: {result.backup_path}")
    if result.log_path:
        lines.append(f"  log:    {result.log_path}")
    if result.already_migrated:
        lines.append("  state:  already migrated (no-op)")
    lines.append("")
    lines.append("  per-column report:")
    for col in result.columns:
        if col.skipped:
            lines.append(
                f"    {col.table}.{col.column:<24}  SKIPPED ({col.skipped_reason})"
            )
        else:
            lines.append(
                f"    {col.table}.{col.column:<24}  rows={col.rows_total} "
                f"non_null={col.rows_non_null} "
                f"sum_before={col.sum_before} sum_after={col.sum_after}"
            )
    if result.indexes_recreated:
        lines.append("")
        lines.append(f"  indexes recreated: {', '.join(result.indexes_recreated)}")
    print("\n".join(lines))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="M1 Day 2.5 money/rate Decimal migration."
    )
    parser.add_argument(
        "--db-path",
        required=True,
        help="Path to the SQLite database file (must not be in protected paths).",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Show what WOULD happen without committing (default).",
    )
    mode_group.add_argument(
        "--apply",
        action="store_true",
        help="Actually commit the migration.",
    )
    args = parser.parse_args(argv)

    apply = bool(args.apply)
    migrate(args.db_path, apply=apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
