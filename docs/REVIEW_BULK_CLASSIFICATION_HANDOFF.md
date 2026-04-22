# Review Bulk Classification Handoff

## 2026-04-22 React SPA Add-Back

### Objective Of This Step
Restore the bulk-classify workflow that was lost when `/review` was replaced by the single-file React SPA.

### Files Changed
- `frontend/review-table.html`
- `backend/tests/test_review_ui_static.py`
- `backend/tests/test_review_confirmation.py`
- `docs/current_progress.md`
- `docs/REVIEW_BULK_CLASSIFICATION_HANDOFF.md`

### Exact Behavior Changed
- The React review queue now has a compact `Bulk classify` toolbar above the column headers.
- The toolbar supports scope selection for flagged rows (`attention_required`) or all rows.
- The toolbar can apply B/P, a category-filtered bucket, or both.
- Applying calls the existing `POST /reviews/report/{review_session_id}/bulk-update` endpoint; no backend route or schema change was needed.
- After a successful bulk update, the UI reloads the latest statement/session through the existing `loadData` path and records a client-side audit entry.
- Empty-bucket categories stay hidden from the category dropdown, so `Personal Car` remains out of active scope.

### Tests Run And Results
- Red check: `backend/tests/test_review_ui_static.py` failed before implementation because `Bulk classify` was absent from the SPA.
- Green checks after implementation:
  - `backend/tests/test_review_ui_static.py` passed.
  - HTML parser check for `frontend/review-table.html` passed.
  - `backend/tests/test_review_confirmation.py` passed after updating the served-HTML assertion from the old `Expense Review` copy to current SPA markers (`Review Queue`, `ExpenseReport`).

### What Is Verified
- The static SPA includes the bulk toolbar, the bulk endpoint call, flagged/all scope controls, and apply action text.
- `/review` still serves parseable HTML.
- Existing review confirmation/bulk backend behavior still passes its regression smoke.

### What Remains Unverified
- Live browser click-through of the restored bulk toolbar against the local DB.
- Whether users need an additional "visible filtered rows" bulk scope beyond flagged/all.

### Next Recommended Step
Run the live browser smoke pass from `docs/current_progress.md`, including one flagged-row bulk update and reload verification.

### 2026-04-22 Live Browser Smoke Attempt
- Added smoke harnesses:
  - `scripts/live_review_smoke.js`
  - `scripts/run_live_review_smoke.ps1`
- Verified the isolated smoke backend path can seed a temporary SQLite DB and start uvicorn on `127.0.0.1:8090`.
- Browser automation is blocked in this environment:
  - Playwright launching system Chrome fails with `spawn EPERM`.
  - Playwright-managed browser install fails because Node child process spawn is denied.
  - System Chrome/Edge launched externally hit Windows `Access denied` errors in Crashpad/Mojo/network sandbox paths.
  - Edge can emit a DevTools WebSocket URL, but renderer CDP commands such as `Page.navigate` time out, so the page cannot be driven.
- No smoke server/browser process was left listening on the temp ports after attempts.
- Status: live browser interaction remains unverified because the local Windows/browser sandbox blocks usable browser automation, not because of an observed app failure.
- Follow-up desktop attempt:
  - Reused the already-running Chrome window titled `ExpenseReport — Diners Club Internal - Google Chrome`.
  - Confirmed `/review` is open and logged in; screenshot showed the app on the `Report Validation` screen.
  - Tried foregrounded coordinate clicks on the `Review Queue` sidebar item and foregrounded keyboard navigation/address-bar commands via `WScript.Shell`.
  - Those OS-level inputs did not change the page state, and no existing Chrome remote-debugging port was listening on the usual 9222-9333 range.
  - Added helper scripts used for the attempt: `scripts/capture_desktop_to_verify.ps1`, `scripts/click_desktop.ps1`, `scripts/foreground_chrome_click.ps1`, `scripts/foreground_chrome_keys.ps1`, `scripts/focus_chrome_review_capture.ps1`, and `scripts/chrome_set_review_screen.ps1`.

### 2026-04-22 Validation Context Fix
- User hit validation error `air_travel_return_date_before_travel_date`; the API already returned row context, but the React validation panel only displayed message + code.
- Fixed `frontend/review-table.html` so validation issue rows render context chips for `review_row_id`, supplier, transaction date, and bucket.
- Added static regression coverage in `backend/tests/test_review_ui_static.py`.
- Live API context for the current error:
  - Review row: `2`
  - Supplier: `Istanbul Oht-4 Dogu Sh`
  - Statement transaction date: `2026-04-01`
  - Bucket: `Airfare/Bus/Ferry/Other`
  - Current air-travel detail has `RT`, travel date `2026-05-09`, return date `2026-03-30`; return date must be corrected to the actual date on/after travel date, or the row should be changed to one-way if not a round trip.

### 2026-04-22 Air Travel Validation Values
- Narrow continuation: make the active Report Validation error self-contained by surfacing the offending air-travel values, not just the row identity.
- Added `air_travel_date`, `air_travel_return_date`, and `air_travel_rt_or_oneway` to `ValidationIssue` and `ReportValidationIssue`.
- `_review_snapshot_issues()` now includes those values on RT missing-return and return-before-travel validation errors; when `air_travel_date` is blank, it reports the same transaction-date fallback used by generation validation.
- `/review` renders those fields as validation chips (`Travel ...`, `Return ...`, `RT`) beside the existing row/supplier/date/bucket chips.
- Tests:
  - Red checks failed first: `backend/tests/smoke_air_travel.py` missing `ValidationIssue.air_travel_date`; `backend/tests/test_review_ui_static.py` missing `issue.air_travel_date`.
  - Green checks passed after implementation: `backend/tests/smoke_air_travel.py`, `backend/tests/test_review_ui_static.py`, `backend/tests/test_review_confirmation.py`, and HTML parser check.
- Live API smoke verified `GET /reports/validate/2` now returns row `2` with `air_travel_date=2026-05-09`, `air_travel_return_date=2026-03-30`, and `air_travel_rt_or_oneway=RT`.
- The business data remains intentionally unchanged; the correct return date must come from the user/source record, or the row should be classified as one-way if it was not a round trip.

### 2026-04-22 React Air Travel Return Field
- User reported the React SPA Air Travel Reconciliation panel did not show a return-date input when Round trip was selected, and the controls wrapped onto multiple lines.
- Fixed `frontend/review-table.html` so `apiRowToLocal()` carries `air_travel_return_date` as `atReturn` and `buildApiFields()` patches it back as `air_travel_return_date`.
- `AirTravelExpanded` now shows `Return date` only when the RT selector value is round trip; it normalizes stored `RT`/`rt` values for the select display.
- The air-travel detail controls now use a single nowrap flex row with tighter fixed widths and `overflowX:auto` as the fallback for narrow screens.
- Static regression coverage in `backend/tests/test_review_ui_static.py` now checks for `atReturn`, `air_travel_return_date`, `Return date`, and the nowrap layout marker.
- Verified: static UI test passed and HTML parse passed.

### 2026-04-22 React Return-Date Guard
- User reported the UI still allowed selecting/saving a return date earlier than the travel date.
- Added client-side validation in `AirTravelExpanded.save()`: when the row is Round trip and both dates are filled, `f.atReturn < f.atDate` blocks save and shows `Return date cannot be before travel date.`
- This preserves backend validation as the final gate while preventing the bad value from being submitted during normal review editing.
- Static regression coverage in `backend/tests/test_review_ui_static.py` now checks for the inline error copy and comparison guard.
- Verified: static UI test passed, HTML parse passed, and `backend/tests/smoke_air_travel.py` still passes.

### 2026-04-22 Live Browser Automation Opened
- After filesystem/network access was expanded, `scripts/run_live_review_smoke.ps1` can launch isolated Chrome via CDP and drive the app.
- Updated the smoke harness to match the React SPA flow: bulk-classify flagged rows as business/Air Travel/Airfare, patch the seeded row to RT through the API, reload, expand Air Travel via a stable `data-testid="air-travel-panel"` selector, verify two date inputs, enter a return date before travel date, assert the inline guard, then navigate to Report Validation.
- Fixed harness cleanup so it does not try to stop PID 0, and fixed the bulk-toast regex.
- Verified clean pass: `{"status":"passed","mode":"raw-cdp","sawBulkToast":true,"sawValidation":true}` with exit code 0.

## Objective Of This Step
Remove the row-by-row penalty in `/review` after statement-led rows created 91 unmatched review rows.

## Observed Problem
- Filling visible required fields did not allow confirmation because the UI kept sending `attention_required=true` from the still-checked `Flag` checkbox.
- The backend correctly blocked confirmation with `Review session has rows marked for attention`, but the product behavior was unusable for 91 rows.

## Files Changed
- `backend/app/services/review_sessions.py`
- `backend/app/routes/reviews.py`
- `backend/app/schemas.py`
- `backend/tests/test_review_confirmation.py`
- `frontend/review-table.html`
- `docs/current_progress.md`
- `docs/REVIEW_BULK_CLASSIFICATION_HANDOFF.md`

## Exact Behavior Changed
- Saving a review row now recomputes required fields after applying edits.
- If all required fields are complete, the row automatically clears `attention_required` and `attention_note`.
- If required fields are still missing, the row remains `needs_review` and the attention note lists missing fields.
- New backend endpoint: `POST /reviews/report/{review_session_id}/bulk-update`.
- New `/review` bulk toolbar can apply `business_or_personal` and `report_bucket` to flagged rows or all rows.
- Confirmation still requires explicit user action and report generation still uses the confirmed snapshot.
- Live local session 3 was bulk-updated to `Business` / `Business` for 91 flagged rows; 0 rows remain marked for attention.

## Tests Run And Results
- `python backend\tests\test_review_confirmation.py`
  - Result: passed.
  - Verifies completed required fields clear attention and bulk update clears flagged rows.
- `python -m compileall backend\app`
  - Result: passed.

## What Remains Unverified
- Browser refresh of the currently open `/review` tab after the live DB repair.
- Manual click-through of Confirm reviewed data for report and Generate report package after the repair.

## Next Recommended Step
Refresh `/review`, click `Confirm reviewed data for report`, then click `Generate report package`.
