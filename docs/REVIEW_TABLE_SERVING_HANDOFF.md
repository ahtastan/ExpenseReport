# Review Table Serving Handoff

## Objective Of This Step
Serve the existing review table from the FastAPI backend and add a narrow report-generation action without changing the review, confirmation, OCR, matching, or auth model.

## Files Changed
- `backend/app/main.py`
- `backend/tests/test_review_confirmation.py`
- `frontend/review-table.html`
- `docs/current_progress.md`
- `docs/REVIEW_TABLE_SERVING_HANDOFF.md`

## Exact Behavior Changed
- Added `GET /review`, excluded from the OpenAPI schema, which serves `frontend/review-table.html` from the same origin as the backend.
- Added a `Generate report package` button to the review table.
- The new button calls `POST /reports/generate` with the loaded review session's `statement_import_id`.
- Confirmation remains a separate explicit action through `POST /reviews/report/{review_session_id}/confirm`.
- The review table now shows visible success/error notices for confirmation, row saves, and report generation.
- The review table states when review data is draft vs confirmed, and row saves visibly warn that edits require reconfirmation before report generation.
- API error text from JSON `detail` responses is shown in the page instead of only via browser alerts.

## Tests Run And Results
- `python backend\tests\test_review_confirmation.py`
  - Expected result: passed.
  - Verifies:
    - `GET /review` returns HTML containing the review page;
    - report generation still fails before confirmation;
    - confirmed snapshot data is used instead of later mutable source rows;
    - editing after confirmation still invalidates the generation gate until reconfirmed.

## What Is Verified
- The backend can serve the review table HTML from `/review`.
- The existing confirmation gate remains enforced in the report generator.
- The generated-report button is wired to the existing `POST /reports/generate` endpoint without adding a new backend path.
- The UI keeps confirmation and report generation as separate user actions.
- The UI visibly warns that edits after confirmation require reconfirmation.

## What Remains Unverified
- Browser-level click workflow against a live FastAPI server.
- Real-data end-to-end review table use with loaded statement rows.
- Generated package download/open workflow from the browser.
- Authentication and permissions, which remain intentionally out of scope.
- OCR/model routing, which remains intentionally out of scope.

## Next Recommended Step
Start the backend locally, open `http://127.0.0.1:8080/review`, and manually exercise load, edit, confirm, generate, edit-after-confirm, failed generate, reconfirm, and generate again against real imported data.
