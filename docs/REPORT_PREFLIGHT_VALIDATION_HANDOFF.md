# Report Preflight Validation Handoff

## Objective Of This Step
Add a narrow preflight validation path users can run from `/review` before generating the report package, focused on the known Air Travel detail-row overflow risk.

## Files Changed
- `backend/app/services/report_validation.py`
- `backend/app/services/report_generator.py`
- `backend/tests/smoke_air_travel.py`
- `frontend/review-table.html`
- `docs/current_progress.md`

## Exact Behavior Added
- `/review` now has a `Validate before generate` button beside the existing confirm/generate actions.
- The button calls `GET /reports/validate/{statement_import_id}` for the current review session's statement import.
- Validation results render in the existing report status area with error/warning counts and escaped issue messages.
- `validate_report_readiness()` now checks the latest confirmed review snapshot, matching the data source used by report generation.
- If no confirmed review snapshot exists, validation returns a blocking `review_not_confirmed` error.
- If more than 3 confirmed airfare rows fall on Week 1A or Week 2A, validation returns an `air_travel_detail_overflow` warning because the template only has 3 Air Travel Reconciliation detail rows per page.
- `generate_report_package()` still preserves the previous user-facing error text for unconfirmed review data.

## Tests Run And Results
- Red check: `backend/tests/smoke_air_travel.py` failed before implementation because the expected `air_travel_detail_overflow` warning was missing.
- Green check: `backend/tests/smoke_air_travel.py` passed after implementation.
- Existing regressions run after implementation:
  - `backend/tests/test_review_confirmation.py`
  - `backend/tests/smoke_meals_entertainment.py`
  - `backend/tests/test_statement_import.py`
  - HTML parse check for `frontend/review-table.html`

## What Is Verified
- A confirmed review with 4 airfare rows on the first week page emits exactly one `air_travel_detail_overflow` warning.
- Existing Air Travel workbook behavior still writes the first detail row and preserves column J formulas.
- Existing report generation still blocks unconfirmed review data with the established `confirmed review data` message.
- The review HTML parses after adding the validation button and result rendering.

## What Remains Unverified
- Live browser click behavior for the new validation button.
- Real local session validation output after the user's current data is confirmed.
- Whether future worksheet overflow should block generation instead of remaining a warning.

## Next Recommended Step
Run one live browser validation pass on the current `/review` session before generating the next package.
