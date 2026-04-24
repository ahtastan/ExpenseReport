# Review Bulk Selected Scope Handoff - 2026-04-23

## Objective Of This Step
Add a `selected (visible)` scope to the React SPA bulk-classify toolbar so users can filter the review queue to a specific state (e.g., `Needs review`) and bulk-update only those visible rows, preventing unintended updates to hidden rows.

## Files Changed
- `backend/app/schemas.py`
- `backend/app/routes/reviews.py`
- `backend/app/services/review_sessions.py`
- `frontend/review-table.html`
- `backend/tests/test_review_confirmation.py`
- `docs/current_progress.md`
- `docs/REVIEW_BULK_SELECTED_SCOPE_HANDOFF_2026-04-23.md`

## Exact Behavior Changed
- The frontend bulk-classify dropdown now includes a `selected (visible)` option.
- When `selected (visible)` is chosen, `handleBulkApply` snapshots the IDs of the currently visible filtered rows and sends them as `row_ids` in the `POST /reviews/report/{review_session_id}/bulk-update` payload.
- `ReviewBulkUpdateRequest` in the backend now accepts an optional `row_ids: list[int]`.
- The `bulk_update_review_rows` service now accepts the `"selected"` scope. It filters the update to only those `row_ids`.
- The service rejects the `"selected"` scope if `row_ids` is empty or missing.
- Existing `"all"` and `"attention_required"` scopes are fully preserved.
- The confirmation and report generation mechanisms are completely untouched.

## Tests Run And Results
- Added regression coverage in `backend/tests/test_review_confirmation.py` that verifies the `selected` scope successfully limits its bulk updates to the provided `row_ids`.
- Result: Passed successfully (`review_confirmation_tests=passed`).

## What Is Verified
- The `selected` scope accurately filters rows during a bulk update on the backend.
- The `ReviewBulkUpdateRequest` correctly parses `row_ids`.
- The frontend provides the new dropdown option and maps it to `row_ids` for the `selected` scope.

## What Remains Unverified
- The live browser interaction with the newly updated `selected` bulk scope toolbar.
- The pending Telegram/VPS deploy step has still not been executed.

## Next Recommended Step
Deploy the pending Telegram/OCR logic to the VPS to unblock live field testing, or test the newly added `selected` bulk scope on the local web view (`/review`).
