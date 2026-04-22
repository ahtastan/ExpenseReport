# Statement-Led Review Handoff

## Objective Of This Step
Refactor report review-session building so every statement transaction appears in `/review`, even when no receipt or approved match exists.

## Files Changed
- `backend/app/models.py`
- `backend/app/db.py`
- `backend/app/services/review_sessions.py`
- `backend/app/services/report_generator.py`
- `backend/app/services/receipt_annotations.py`
- `backend/tests/test_review_confirmation.py`
- `docs/current_progress.md`
- `docs/STATEMENT_LED_REVIEW_HANDOFF.md`

## Exact Behavior Changed
- Review sessions now seed rows from all `StatementTransaction` records for the statement import.
- Approved matches enrich the transaction row with receipt, match, and suggested classification data.
- Transactions without approved matches still get review rows with:
  - statement source data;
  - receipt source status `missing`;
  - match source status `unmatched`;
  - `receipt_id=None`;
  - `match_decision_id=None`;
  - status `needs_review`;
  - attention required until a reviewer resolves the row.
- Existing draft review sessions are backfilled with missing statement transaction rows.
- Confirmed review sessions remain frozen and are not changed by the row sync.
- `ReviewRow.receipt_document_id` and `ReviewRow.match_decision_id` are now nullable.
- A narrow SQLite startup migration rebuilds the `reviewrow` table when an existing local DB still has those two columns as `NOT NULL`.
- Report generation still requires a confirmed snapshot.
- Confirmed rows without receipts can generate a report package; the annotated receipt PDF uses missing-receipt placeholder tiles.

## Tests Run And Results
- `python backend\tests\test_review_confirmation.py`
  - Result: passed with the bundled workspace Python.
  - Verifies:
    - `GET /review` still serves the static review page;
    - latest statement lookup still works;
    - report generation still fails before confirmation;
    - confirmed snapshot data is used instead of live mutable source rows;
    - edits after confirmation require reconfirmation;
    - statements with no receipts still create review rows from statement transactions;
    - approved matches enrich one transaction while unmatched transactions remain visible;
    - existing empty draft sessions are backfilled;
    - confirmed unmatched rows can generate a package with missing-receipt placeholders.
- Real-file in-memory smoke with `03_11_Receipts/Diners_Transactions.xlsx`
  - Result: `transactions=91`, `review_rows=91`, `attention_required=91` when no receipts are loaded.
- Throwaway old SQLite-schema migration smoke
  - Result: `receipt_document_id_notnull=0`, `match_decision_id_notnull=0`.

## What Is Verified
- The review builder is now statement-led for new sessions.
- Existing empty draft sessions are repaired on access.
- The current snapshot gate remains enforced.
- Statement-only confirmed rows no longer crash report generation due to missing receipt IDs.

## What Remains Unverified
- Browser-level `/review` rendering of unmatched rows.
- Existing non-empty draft sessions with prior reviewer edits are not overwritten; this step only adds missing rows.
- Full migration behavior on every possible old local SQLite shape beyond the observed `reviewrow` NOT NULL columns.
- Auth, OCR/model routing, and match-queue UI remain out of scope.

## Next Recommended Step
Run the live backend against the real imported Diners statement, open `/review`, and verify that the review table shows one row per statement transaction, with matched rows enriched and unmatched rows clearly marked for review.
