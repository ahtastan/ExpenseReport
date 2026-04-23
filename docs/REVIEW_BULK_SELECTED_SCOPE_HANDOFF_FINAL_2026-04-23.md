# Selected Scope Bulk Update Handoff

## 1. Current Architecture State
- **Backend**: FastAPI + SQLModel. Session building seeds rows for all statement transactions. Confirmation snapshots the data. The `POST /reviews/report/{review_session_id}/bulk-update` endpoint accepts a `scope` and now conditionally accepts `row_ids`.
- **Frontend**: Single-file React 18 SPA served statically. Uses client-side filtering via tab buttons. The bulk-classify toolbar operates on backend API calls and reloads data upon success.
- **Model Integration**: Local OCR/matching/synthesis works and passed live smoke. The VPS environment is currently running an older codebase and lacks the recent OCR prompt and Telegram clarification patches.

## 2. Files Changed
- `backend/app/schemas.py`
- `backend/app/routes/reviews.py`
- `backend/app/services/review_sessions.py`
- `frontend/review-table.html`
- `backend/tests/test_review_confirmation.py`

## 3. Exact Behavior Changed
- `ReviewBulkUpdateRequest` in `schemas.py` now accepts an optional `row_ids: list[int]`.
- `bulk_update_review_rows` in `review_sessions.py` now supports `"selected"` scope. When specified, it filters the rows to update based on the presence of their ID in the provided `row_ids` list. It throws a 400 bad request error if `"selected"` scope is requested but `row_ids` is missing or empty.
- The `bulk_update_review_rows` call in `reviews.py` now explicitly passes `payload.row_ids`.
- The `frontend/review-table.html` SPA dropdown has a new option: `selected (visible)`.
- When users click `Apply` with the `selected` scope, `handleBulkApply` snapshots the IDs from the `filtered` list (representing the currently visible rows) and includes them as `row_ids` in the `POST` request.

## 4. Tests Run And Results
- Added a regression test block in `backend/tests/test_review_confirmation.py` covering the new `"selected"` scope behavior.
- Executed `python backend/tests/test_review_confirmation.py`
- **Result**: Passed (`Exit code: 0`). The selected row was updated correctly and the unselected row was explicitly left unchanged.

## 5. What Is Verified
- The `"selected"` scope correctly applies updates strictly to the provided row IDs.
- The `"all"` and `"attention_required"` scopes remain fully functional.
- The React SPA correctly surfaces the option and maps it to `filtered` row IDs during the API call.

## 6. What Is Still Unverified
- Real-world browser interaction for clicking the bulk toolbar controls (blocked locally by sandbox restrictions).
- The previously pending deployment of Telegram/OCR fixes to the VPS.

## 7. Exact Next Recommended Step
Deploy the local Telegram OCR/clarification fixes to the VPS (`clarifications.py` and `model_router.py`), restart the `dcexpense` systemd service, verify the `/health` endpoint, check webhook status, and test end-to-end with the Onder/airport receipt images on Telegram.

## 8. Risks If The Next Model Continues Carelessly
- **VPS Environment Mismatch**: If the next model tries to test live OCR on Telegram without deploying the local fixes first, it will fail because the VPS does not have the updated prompts or the `openai` SDK dependency installed in its Python environment.
- **Breaking Scope Logic**: Modifying the frontend `filtered` array or the `handleBulkApply` state closure without understanding that `row_ids` are evaluated *at click time* could introduce a race condition where the user updates a hidden list of rows.
- **Snapshot Disruption**: The bulk update works because it resets `status="edited"` and conditionally clears `attention_required` which safely kicks the session back to `"draft"`. Modifying row logic without adhering to this validation state machine will break report generation.
