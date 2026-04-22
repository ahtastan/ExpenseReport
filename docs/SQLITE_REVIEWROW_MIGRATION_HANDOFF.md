# SQLite ReviewRow Migration Handoff

## Objective Of This Step
Fix the backend startup failure caused by an interrupted SQLite migration for nullable `ReviewRow.receipt_document_id` and `ReviewRow.match_decision_id`.

## Observed Failure
- Running Uvicorn hit `sqlite3.OperationalError: index ix_reviewrow_review_session_id already exists`.
- The failing path was `backend/app/db.py` inside `_migrate_reviewrow_nullable_for_sqlite()`.
- SQLite kept `ix_reviewrow_*` index names attached to the renamed `reviewrow_old` table, so recreating `reviewrow` attempted to create duplicate global index names.

## Files Changed
- `backend/app/db.py`
- `backend/tests/test_db_migration.py`
- `docs/current_progress.md`
- `docs/SQLITE_REVIEWROW_MIGRATION_HANDOFF.md`

## Exact Behavior Changed
- SQLite startup repair now detects interrupted `reviewrow_old` migration leftovers.
- Stale `ix_reviewrow_*` indexes on `reviewrow_old` are dropped before SQLModel table/index creation.
- If the interrupted table has rows and the live `reviewrow` table is empty, rows are copied forward before the old table is dropped.
- Missing `reviewrow` indexes are explicitly restored after startup repair.
- The existing nullable-column migration still preserves the confirmed snapshot/report-generation architecture.

## Tests Run And Results
- `python backend\tests\test_db_migration.py`
  - Result: passed.
  - Verifies interrupted migration leftovers are repaired and indexes end up on `reviewrow`.
- `python backend\tests\test_review_confirmation.py`
  - Result: passed.
- `python backend\tests\test_statement_import.py`
  - Result: passed.
- `python -m compileall backend\app`
  - Result: passed.
- Default local DB startup smoke from `backend/`
  - Result: `startup_db_ok`.

## What Remains Unverified
- A full long-running Uvicorn manual browser session after this repair.
- Complex partial-migration states where both `reviewrow` and `reviewrow_old` contain different non-empty row sets.

## Next Recommended Step
Start the backend from the real `backend/` directory and open `/review` against the latest imported statement.
