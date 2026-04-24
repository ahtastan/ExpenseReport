# Manual Statement Entry Handoff

## Current UI Architecture State
- `/review` remains the single-file React 18 SPA in `frontend/review-table.html`.
- The Add Statement flow is a small modal mounted beside the existing Import Statement modal.
- Selected-visible bulk toolbar behavior is preserved.

## Latest Completed Implementation Step
- Added receipt-assisted manual statement entry from `/review`.
- Upload/extract uses `POST /statements/manual/receipt`, which creates a `ReceiptDocument` and runs the existing `apply_receipt_extraction()` pipeline.
- Save uses `POST /statements/manual/transactions`, which creates one manual `StatementTransaction` and refreshes the draft review session.

## Exact Behavior Changed
- Operators can upload a receipt image or PDF, extract fields, edit date/supplier/amount/currency/business purpose, then save a statement entry.
- Uploaded receipts are linked through an approved `manual_statement_entry` match when present, so the statement-led review row can show receipt data.
- If a statement import is already confirmed, the manual save creates a new draft review session instead of mutating the frozen confirmed snapshot.
- Excel import, Telegram behavior, matching logic, report generation, and snapshot confirmation behavior were not changed.

## Verified
- `backend/tests/test_manual_statement_entry.py` verifies upload/extract, manual transaction creation, approved manual match creation, row count update, and review row refresh.
- `backend/tests/test_review_ui_static.py` verifies the Add Statement UI hooks and preserves selected-visible toolbar assertions.

## Still Unverified
- Browser-level Add Statement interaction with a real receipt file.
- Whether PDF receipt extraction produces useful values; PDF OCR/rendering remains out of scope.

## Exact Next Recommended Step
- Run a live browser smoke for the Add Statement modal with a deterministic filename receipt, then confirm the new manual row appears in the review queue.

