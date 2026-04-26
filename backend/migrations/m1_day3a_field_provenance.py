"""M1 Day 3a schema migration: FieldProvenanceEvent table + backfill.

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

What this migration DOES (in a single transaction):

  1. CREATE TABLE fieldprovenanceevent (14 columns) + 8 indexes
     (1 composite ix_fieldprovenanceevent_lookup +
     7 single-column indexes from SQLModel's Field(index=True)).
  2. Backfill loop: for every receiptdocument row, for every tracked
     field with a non-NULL value, INSERT one event:
       source           = 'legacy_unknown_current'
       event_type       = 'accepted'
       actor_type       = 'system_migration'
       actor_user_id    = NULL
       actor_label      = 'system:m1-day3a-backfill'
       decision_group_id = one new UUID hex per receipt (shared across
                           all of that receipt's backfill events)
       value            = serialized current column value
       value_decimal    = same value if field in MONEY_FIELDS, else NULL
       metadata_json    = {"original_created_at": <receipt.created_at>,
                           "backfill_reason": "M1 Day 3a foundation"}
  3. Verification (transaction-rollback on any failure):
     a. Table exists with all 14 expected columns.
     b. All 8 required indexes present.
     c. Pre-backfill non-null count equals post-backfill event count.
     d. Exit invariant: every (receipt, non-NULL tracked field) has
        exactly one legacy_unknown_current/accepted event.
     e. No backfill event has actor_user_id set (all NULL —
        system_migration events).

What this migration does NOT do:

  *  It does NOT modify any merge logic in receipt_extraction.py or
     elsewhere. That refactor is M1 Day 3b. Day 3a only stands the
     table up + backfills current-state events; the read side
     (get_current_event etc.) is wired but no production code calls
     it yet.
  *  It does NOT touch ReviewRow.source_json / suggested_json /
     confirmed_json. Those coexist with FieldProvenanceEvent for now;
     convergence (if any) is a later milestone decision.
  *  It does NOT infer source from ocr_confidence. All backfill rows
     get source='legacy_unknown_current' — the honest answer for "we
     don't know how this got here, but it's the current value as of
     M1 Day 3a." Per design §8 and PM decision #9.

Tracked fields (9 columns on receiptdocument):

  Money            : extracted_local_amount (also populates value_decimal)
  Categorical      : extracted_currency, receipt_type, business_or_personal,
                     report_bucket
  Identity / freeform: extracted_date, extracted_supplier, business_reason,
                     attendees

ReviewRow / ExpenseReport entity types are NOT backfilled by Day 3a —
they're reserved for Day 3c snapshot work. The enum has the values, but
no events of those entity_types are written by this script.

Idempotency probe: the table exists AND every receipt with a non-NULL
tracked field has its matching legacy_unknown_current event ⇒ exit 0
as no-op. Partial state (table exists but invariant doesn't hold) is
refused with a clear error pointing at the broken receipts; manual
investigation is required because that state shouldn't happen under
normal operation (the backfill is one transaction).

SQLite version requirement: ≥ 3.35.0 (project minimum, inherited from
Day 2.5). Day 3a doesn't actually need DROP COLUMN, but enforcing the
shared floor keeps every migration's prerequisites uniform.

Run:

    python backend/migrations/m1_day3a_field_provenance.py \\
      --db-path /tmp/expense_app.db --dry-run
    python backend/migrations/m1_day3a_field_provenance.py \\
      --db-path /tmp/expense_app.db --apply

Side effects on disk (written alongside the target DB) in --apply mode:

    <db>.pre-m1-day3a-<UTC-timestamp>.backup        # full byte-for-byte copy
    <db>.pre-m1-day3a-<UTC-timestamp>.migration.log # audit trail

Exit codes:

    0  success, or already-migrated no-op
    2  refused (production path, missing db, SQLite too old, partial state)
    3  runtime error during DDL or backfill (transaction rolled back)

Rollback procedure (if production deploy goes wrong after copy-back):

    1. sudo systemctl stop dcexpense.service
    2. sudo cp /var/lib/dcexpense/expense_app.db.pre-m1-day3a-{ts}.backup \\
              /var/lib/dcexpense/expense_app.db
    3. Roll code back to the previous main commit (git revert or hard reset)
    4. sudo systemctl start dcexpense.service
    5. Verify /health returns 200 and one receipt loads via /review

The .pre-m1-day3a-{timestamp}.backup file is auto-created by --apply
alongside the target DB before any DDL runs.

Partial rollback (table exists, no Day 3b code consumes it yet):

    1. sudo systemctl stop dcexpense.service
    2. sqlite3 /var/lib/dcexpense/expense_app.db "DROP TABLE fieldprovenanceevent"
    3. sudo systemctl start dcexpense.service

Safe because no other code references this table until Day 3b lands.
The full backup-restore procedure above is still the safer default.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sqlite3
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from backend.migrations._common import (
    check_sqlite_version,
    index_exists,
    migration_artifact_paths,
    refuse_protected_path,
    table_exists,
)


TABLE_NAME = "fieldprovenanceevent"

# Tracked fields on receiptdocument: (column_name, field_name_enum_value).
# Day 3a's backfill targets only RECEIPT entities. Day 3b/3c add ReviewRow
# and ExpenseReport entity types as their writers come online.
TRACKED_FIELDS: list[tuple[str, str]] = [
    ("extracted_local_amount",  "extracted_local_amount"),
    ("extracted_currency",      "extracted_currency"),
    ("extracted_date",          "extracted_date"),
    ("extracted_supplier",      "extracted_supplier"),
    ("receipt_type",            "receipt_type"),
    ("business_or_personal",    "business_or_personal"),
    ("report_bucket",           "report_bucket"),
    ("business_reason",         "business_reason"),
    ("attendees",               "attendees"),
]

# Field names that get value_decimal populated. Mirrors
# app.provenance_enums.MONEY_FIELDS — duplicated here so the migration
# script doesn't import from the app layer (keeps the script standalone
# and avoids coupling the migration to live model code).
MONEY_FIELD_NAMES: frozenset[str] = frozenset({
    "extracted_local_amount",
    "vat_amount",  # not a current tracked column, but listed for parity
})

LEGACY_SOURCE = "legacy_unknown_current"
ACCEPTED_EVENT_TYPE = "accepted"
SYSTEM_MIGRATION_ACTOR = "system_migration"
BACKFILL_LABEL = "system:m1-day3a-backfill"
BACKFILL_REASON = "M1 Day 3a foundation"

# Single-column indexes auto-generated by SQLModel from Field(index=True).
# This script creates the table via raw DDL, so we have to enumerate them
# explicitly. Verification confirms all 8 are present post-create.
EXPECTED_INDEXES: list[str] = [
    "ix_fieldprovenanceevent_lookup",  # composite
    "ix_fieldprovenanceevent_entity_type",
    "ix_fieldprovenanceevent_entity_id",
    "ix_fieldprovenanceevent_field_name",
    "ix_fieldprovenanceevent_source",
    "ix_fieldprovenanceevent_decision_group_id",
    "ix_fieldprovenanceevent_actor_user_id",
    "ix_fieldprovenanceevent_created_at",
]

CREATE_TABLE_SQL = f"""
CREATE TABLE {TABLE_NAME} (
    id INTEGER NOT NULL PRIMARY KEY,
    entity_type VARCHAR NOT NULL,
    entity_id INTEGER NOT NULL,
    field_name VARCHAR NOT NULL,
    event_type VARCHAR NOT NULL,
    source VARCHAR NOT NULL,
    value TEXT,
    value_decimal NUMERIC(18,4),
    confidence FLOAT,
    decision_group_id VARCHAR NOT NULL,
    actor_type VARCHAR NOT NULL,
    actor_user_id INTEGER,
    actor_label VARCHAR NOT NULL,
    metadata_json TEXT,
    created_at DATETIME NOT NULL,
    FOREIGN KEY(actor_user_id) REFERENCES appuser(id)
)
""".strip()

CREATE_INDEX_SQL: list[str] = [
    # Composite: load-bearing for "current accepted event for (entity, field)".
    f"CREATE INDEX ix_fieldprovenanceevent_lookup "
    f"ON {TABLE_NAME} (entity_type, entity_id, field_name, created_at DESC)",
    # Single-column auto-indexes (mirror SQLModel Field(index=True)).
    f"CREATE INDEX ix_fieldprovenanceevent_entity_type ON {TABLE_NAME} (entity_type)",
    f"CREATE INDEX ix_fieldprovenanceevent_entity_id ON {TABLE_NAME} (entity_id)",
    f"CREATE INDEX ix_fieldprovenanceevent_field_name ON {TABLE_NAME} (field_name)",
    f"CREATE INDEX ix_fieldprovenanceevent_source ON {TABLE_NAME} (source)",
    f"CREATE INDEX ix_fieldprovenanceevent_decision_group_id ON {TABLE_NAME} (decision_group_id)",
    f"CREATE INDEX ix_fieldprovenanceevent_actor_user_id ON {TABLE_NAME} (actor_user_id)",
    f"CREATE INDEX ix_fieldprovenanceevent_created_at ON {TABLE_NAME} (created_at)",
]


@dataclass
class MigrationResult:
    db_path: str
    backup_path: str | None
    log_path: str | None
    dry_run: bool
    already_migrated: bool
    receipts_total: int = 0
    backfilled_events: int = 0
    receipts_with_events: int = 0
    field_event_counts: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# value serialization (mirrors app.services.field_provenance._serialize_value
# without importing app code — migration scripts stay standalone).
# ---------------------------------------------------------------------------


_MONEY_QUANTIZER = Decimal("0.0001")  # 4 dp matches Numeric(18,4) precision


def _serialize_value(field_name: str, raw: object) -> str | None:
    """Serialize a column value to TEXT for the value column.

    Money fields (per MONEY_FIELD_NAMES) are routed through Decimal and
    quantized to 4 dp so the serialized string always matches the
    column's declared precision — regardless of whether raw sqlite3
    returned float (lossy REAL affinity) or the value was already a
    Decimal/string. Dates → ISO-8601. Strings as-is. NULL stays NULL.
    """
    if raw is None:
        return None
    if field_name in MONEY_FIELD_NAMES:
        if isinstance(raw, Decimal):
            return format(raw.quantize(_MONEY_QUANTIZER), "f")
        if isinstance(raw, (int, float)):
            return format(Decimal(str(raw)).quantize(_MONEY_QUANTIZER), "f")
        if isinstance(raw, str):
            return format(Decimal(raw).quantize(_MONEY_QUANTIZER), "f")
        # Unknown type for a money field — fall through to the generic path
        # below; the verification step will catch any oddity.
    if isinstance(raw, str):
        return raw
    if isinstance(raw, Decimal):
        return format(raw, "f")
    if isinstance(raw, datetime):
        return raw.isoformat()
    if hasattr(raw, "isoformat"):
        return raw.isoformat()
    return str(raw)


def _maybe_value_decimal_str(field_name: str, raw: object) -> str | None:
    """Auto-populate value_decimal for money fields only.

    Returns a Decimal-shaped string for binding via raw sqlite3 (which
    can't bind Decimal directly). SQLite's NUMERIC affinity converts the
    string to REAL on insert; SQLAlchemy reads it back as Decimal via
    the Numeric column type.
    """
    if field_name not in MONEY_FIELD_NAMES:
        return None
    if raw is None:
        return None
    if isinstance(raw, Decimal):
        return format(raw, "f")
    if isinstance(raw, (int, float)):
        return format(Decimal(str(raw)), "f")
    if isinstance(raw, str):
        return format(Decimal(raw), "f")
    return None  # unknown type — be safe, leave NULL


# ---------------------------------------------------------------------------
# idempotency probe
# ---------------------------------------------------------------------------


def _count_non_null_tracked_values(conn: sqlite3.Connection) -> tuple[int, dict[str, int]]:
    """Return (total_non_null_count, per_field_counts) on receiptdocument."""
    per_field: dict[str, int] = {}
    total = 0
    for column, field_name in TRACKED_FIELDS:
        n = conn.execute(
            f"SELECT COUNT(*) FROM receiptdocument WHERE {column} IS NOT NULL"
        ).fetchone()[0]
        per_field[field_name] = n
        total += n
    return total, per_field


def _backfill_event_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        f"SELECT COUNT(*) FROM {TABLE_NAME} "
        f"WHERE source = ? AND event_type = ? AND actor_label = ?",
        (LEGACY_SOURCE, ACCEPTED_EVENT_TYPE, BACKFILL_LABEL),
    ).fetchone()[0]


def _exit_invariant_violations(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """Return (receipt_id, field_name) pairs that violate the exit invariant.

    Invariant: every receipt with a non-NULL tracked column has at least
    one legacy_unknown_current/accepted event for that field.
    """
    violations: list[tuple[int, str]] = []
    for column, field_name in TRACKED_FIELDS:
        rows = conn.execute(
            f"""
            SELECT r.id
            FROM receiptdocument AS r
            WHERE r.{column} IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM {TABLE_NAME} AS fpe
                WHERE fpe.entity_type = 'receipt'
                  AND fpe.entity_id = r.id
                  AND fpe.field_name = ?
                  AND fpe.source = ?
                  AND fpe.event_type = ?
              )
            """,
            (field_name, LEGACY_SOURCE, ACCEPTED_EVENT_TYPE),
        ).fetchall()
        for (rid,) in rows:
            violations.append((rid, field_name))
    return violations


# ---------------------------------------------------------------------------
# DDL + backfill
# ---------------------------------------------------------------------------


def _create_table_and_indexes(conn: sqlite3.Connection, logger: logging.Logger) -> None:
    conn.execute(CREATE_TABLE_SQL)
    logger.info("CREATE TABLE %s", TABLE_NAME)
    for ddl in CREATE_INDEX_SQL:
        conn.execute(ddl)
        logger.info("%s", ddl.split("\n")[0].strip())


def _run_backfill(
    conn: sqlite3.Connection, *, now_iso: str, logger: logging.Logger
) -> tuple[int, int, dict[str, int]]:
    """Backfill events for every (receipt, non-NULL tracked field).

    Returns (events_written, receipts_with_events, per_field_counts).
    Each receipt gets one decision_group_id shared across its events.
    """
    cols = ", ".join(c for c, _ in TRACKED_FIELDS)
    rows = conn.execute(
        f"SELECT id, created_at, {cols} FROM receiptdocument ORDER BY id"
    ).fetchall()

    per_field: dict[str, int] = {fn: 0 for _, fn in TRACKED_FIELDS}
    events_written = 0
    receipts_with_events = 0

    for row in rows:
        receipt_id = row[0]
        receipt_created_at = row[1]
        column_values = row[2:]
        decision_group_id = uuid.uuid4().hex
        wrote_for_this_receipt = False

        metadata = {
            "original_created_at": (
                receipt_created_at if isinstance(receipt_created_at, str)
                else (receipt_created_at.isoformat() if receipt_created_at else None)
            ),
            "backfill_reason": BACKFILL_REASON,
        }
        metadata_json = json.dumps(metadata, sort_keys=True)

        for (column, field_name), value in zip(TRACKED_FIELDS, column_values):
            if value is None:
                continue

            conn.execute(
                f"""
                INSERT INTO {TABLE_NAME} (
                    entity_type, entity_id, field_name, event_type, source,
                    value, value_decimal, confidence,
                    decision_group_id, actor_type, actor_user_id, actor_label,
                    metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "receipt",
                    receipt_id,
                    field_name,
                    ACCEPTED_EVENT_TYPE,
                    LEGACY_SOURCE,
                    _serialize_value(field_name, value),
                    _maybe_value_decimal_str(field_name, value),
                    None,  # confidence — never set for legacy_unknown_current
                    decision_group_id,
                    SYSTEM_MIGRATION_ACTOR,
                    None,  # actor_user_id — always NULL for system_migration
                    BACKFILL_LABEL,
                    metadata_json,
                    now_iso,
                ),
            )
            per_field[field_name] += 1
            events_written += 1
            wrote_for_this_receipt = True

        if wrote_for_this_receipt:
            receipts_with_events += 1
            logger.info(
                "backfilled receipt id=%s decision_group=%s events=%d",
                receipt_id, decision_group_id,
                sum(1 for v in column_values if v is not None),
            )

    return events_written, receipts_with_events, per_field


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------


def _verify_post_state(
    conn: sqlite3.Connection,
    *,
    expected_event_count: int,
    logger: logging.Logger,
) -> None:
    # (a) Table + columns.
    info = conn.execute(f"PRAGMA table_info({TABLE_NAME})").fetchall()
    actual_cols = {row[1] for row in info}
    expected_cols = {
        "id", "entity_type", "entity_id", "field_name", "event_type", "source",
        "value", "value_decimal", "confidence", "decision_group_id",
        "actor_type", "actor_user_id", "actor_label", "metadata_json",
        "created_at",
    }
    missing = expected_cols - actual_cols
    if missing:
        raise RuntimeError(f"VERIFY FAILED: missing columns: {sorted(missing)}")

    # (b) All 8 indexes.
    for idx_name in EXPECTED_INDEXES:
        if not index_exists(conn, idx_name):
            raise RuntimeError(f"VERIFY FAILED: missing index {idx_name}")

    # (c) Pre/post non-null count == event count.
    actual_events = _backfill_event_count(conn)
    if actual_events != expected_event_count:
        raise RuntimeError(
            f"VERIFY FAILED: expected {expected_event_count} backfill events, "
            f"found {actual_events}"
        )

    # (d) Exit invariant.
    violations = _exit_invariant_violations(conn)
    if violations:
        sample = ", ".join(f"(id={r}, field={f})" for r, f in violations[:10])
        raise RuntimeError(
            f"VERIFY FAILED (exit invariant): {len(violations)} (receipt, field) "
            f"pair(s) have a non-NULL value but no backfill event. Sample: {sample}"
        )

    # (e) No backfill event has actor_user_id.
    leaked = conn.execute(
        f"SELECT COUNT(*) FROM {TABLE_NAME} "
        f"WHERE source = ? AND actor_user_id IS NOT NULL",
        (LEGACY_SOURCE,),
    ).fetchone()[0]
    if leaked:
        raise RuntimeError(
            f"VERIFY FAILED: {leaked} legacy_unknown_current event(s) have "
            f"actor_user_id set; expected all NULL for system_migration source"
        )

    logger.info(
        "verify OK: %d events, %d expected indexes present, exit invariant clean, "
        "all actor_user_id NULL",
        actual_events, len(EXPECTED_INDEXES),
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def migrate(db_path: str, *, apply: bool) -> MigrationResult:
    refuse_protected_path(db_path)
    check_sqlite_version()

    if not Path(db_path).exists():
        print(f"REFUSED: db path {db_path!r} does not exist.", file=sys.stderr)
        raise SystemExit(2)

    dry_run = not apply
    ts, projected_backup, projected_log = migration_artifact_paths(db_path, "m1-day3a")
    backup_path: str | None = None
    log_path: str | None = None
    logger = logging.getLogger(f"m1_day3a.{ts}")
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
    )

    try:
        # — pre-state metrics —
        receipts_total = conn.execute(
            "SELECT COUNT(*) FROM receiptdocument"
        ).fetchone()[0]
        result.receipts_total = receipts_total
        non_null_total, per_field_pre = _count_non_null_tracked_values(conn)
        logger.info(
            "pre-state: %d receipts total, %d non-null tracked-field values",
            receipts_total, non_null_total,
        )
        for f, n in per_field_pre.items():
            logger.info("  pre-state field %s: %d non-null", f, n)

        # — idempotency probe —
        if table_exists(conn, TABLE_NAME):
            existing = _backfill_event_count(conn)
            violations = _exit_invariant_violations(conn)
            if existing == non_null_total and not violations:
                logger.info(
                    "already migrated: table exists, %d backfill events match "
                    "%d non-null tracked values, exit invariant clean. Exit 0.",
                    existing, non_null_total,
                )
                result.already_migrated = True
                result.backfilled_events = existing
                result.field_event_counts = per_field_pre
                conn.close()
                _print_summary(result)
                return result

            # Table exists but state is partial — refuse.
            msg = (
                f"REFUSED: {TABLE_NAME} exists but state is partial. "
                f"Pre-state non-null count: {non_null_total}. "
                f"Existing backfill events: {existing}. "
                f"Exit-invariant violations: {len(violations)}. "
                f"This shouldn't happen under normal operation (the backfill "
                f"is one transaction); manual investigation required."
            )
            logger.error(msg)
            print(msg, file=sys.stderr)
            conn.close()
            raise SystemExit(2)

        # — dry-run: project event counts without writing —
        if dry_run:
            result.backfilled_events = non_null_total
            result.field_event_counts = per_field_pre
            result.receipts_with_events = sum(
                1 for _ in range(receipts_total)  # tighter count below
            )
            # Compute realistic receipts_with_events: receipts with ANY non-NULL.
            cols = " OR ".join(f"{c} IS NOT NULL" for c, _ in TRACKED_FIELDS)
            rwe = conn.execute(
                f"SELECT COUNT(*) FROM receiptdocument WHERE {cols}"
            ).fetchone()[0]
            result.receipts_with_events = rwe
            logger.info(
                "[dry-run] would CREATE TABLE %s + %d indexes; "
                "would write %d events across %d receipts",
                TABLE_NAME, len(EXPECTED_INDEXES), non_null_total, rwe,
            )
            conn.close()
            _print_summary(result)
            return result

        # — apply path —
        conn.execute("BEGIN")
        try:
            _create_table_and_indexes(conn, logger)
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            events_written, receipts_with_events, per_field_post = _run_backfill(
                conn, now_iso=now_iso, logger=logger
            )
            result.backfilled_events = events_written
            result.receipts_with_events = receipts_with_events
            result.field_event_counts = per_field_post

            _verify_post_state(
                conn, expected_event_count=non_null_total, logger=logger
            )
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
        print(f"ERROR: migration failed and was rolled back.", file=sys.stderr)
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
        f"M1 Day 3a migration: {mode}",
        f"  db:     {result.db_path}",
    ]
    if result.backup_path:
        lines.append(f"  backup: {result.backup_path}")
    if result.log_path:
        lines.append(f"  log:    {result.log_path}")
    if result.already_migrated:
        lines.append("  state:  already migrated (no-op)")
    lines.append("")
    lines.append(f"  receipts total:           {result.receipts_total}")
    lines.append(f"  receipts with events:     {result.receipts_with_events}")
    lines.append(f"  backfilled events total:  {result.backfilled_events}")
    if result.field_event_counts:
        lines.append("")
        lines.append("  per-field event counts:")
        for fn, n in sorted(result.field_event_counts.items()):
            lines.append(f"    {fn:<28s} {n}")
    print("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="M1 Day 3a FieldProvenanceEvent table + backfill migration."
    )
    parser.add_argument("--db-path", required=True, help="Path to SQLite database.")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Show what WOULD happen without committing (default).",
    )
    mode_group.add_argument(
        "--apply", action="store_true",
        help="Actually commit the migration.",
    )
    args = parser.parse_args(argv)
    apply = bool(args.apply)
    migrate(args.db_path, apply=apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
