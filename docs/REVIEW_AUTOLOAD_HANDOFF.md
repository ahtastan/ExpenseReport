# Review Auto-Load Handoff

## Objective Of This Step
Make `GET /review` usable without manually typing a `statement_import_id`, while preserving the existing review, confirmation, and report-generation flow.

## Files Changed
- `backend/app/routes/statements.py`
- `backend/tests/test_review_confirmation.py`
- `frontend/review-table.html`
- `docs/current_progress.md`
- `docs/REVIEW_AUTOLOAD_HANDOFF.md`

## Exact Behavior Changed
- Added `GET /statements/latest`.
- The endpoint returns the newest `StatementImport` by `created_at DESC`, matching the existing `GET /statements/` list ordering.
- If no statement import exists, the endpoint returns `404` with `No statement imports found`.
- The review table now calls `/statements/latest` on page open, fills the existing statement id input, and loads `/reviews/report/{statement_id}`.
- Manual statement id override remains available through the existing input and `Load` button.
- Confirmation and report generation remain separate actions.
- Report generation still uses the confirmed review snapshot and still requires reconfirmation after edits.

## Tests Run And Results
- `python backend\tests\test_review_confirmation.py`
  - Result: passed with the bundled workspace Python.
  - Verifies:
    - `GET /review` serves HTML containing the review page;
    - the served review HTML references `/statements/latest`;
    - `GET /statements/latest` returns the newest seeded statement import;
    - report generation still fails before confirmation;
    - confirmed snapshot data is used instead of later mutable source rows;
    - editing after confirmation still invalidates report generation until reconfirmed.

## What Is Verified
- Latest statement selection works through the API with seeded test data.
- `/review` is still served by the FastAPI backend.
- The existing confirmation/report gate behavior still passes.
- The manual statement input remains present in the static review page.

## What Remains Unverified
- Browser-level click workflow against a live FastAPI server.
- Real-data review table auto-load with the persisted local database.
- Manual statement override in a browser.
- Generated package download/open workflow from the browser.
- Authentication, permissions, OCR/model routing, and match-queue UI remain intentionally out of scope.

## Next Recommended Step
Start the backend locally, open `http://127.0.0.1:8080/review`, and manually exercise auto-load, manual statement override, edit, confirm, generate, edit-after-confirm, failed generate, reconfirm, and generate again against real imported data.
