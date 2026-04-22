# Claude Handoff - Expense Reporting App - 2026-04-22

## Repo And Runtime
- Repo: `C:\Users\CASPER\.openclaw\workspace\Expense\expense-reporting-app`
- Backend app: `backend/app/main.py`
- Review UI: `frontend/review-table.html`
- Corporate template: `C:\Users\CASPER\.openclaw\workspace\Expense\Expense Report Form_Blank.xlsx`
- Local backend command:
  ```bat
  cd /d C:\Users\CASPER\.openclaw\workspace\Expense\expense-reporting-app\backend
  C:\Users\CASPER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload
  ```
- Review page: `http://127.0.0.1:8080/review`

## Working Model
The app is statement-led:
1. Import Diners statement.
2. Create one review row per statement transaction.
3. Approved receipt matches enrich rows but do not decide whether rows exist.
4. User edits rows in `/review`.
5. User confirms the review snapshot.
6. Report generation reads only the confirmed snapshot.
7. Generated package is downloadable from `/reports/{report_run_id}/download`.

Do not remove or bypass the confirmed-snapshot gate. Edits after confirmation must require reconfirmation.

## Recent User Decisions
- Personal expense reports are out of scope for now.
- `Personal Car` should not be selectable in the category UI.
- Air Travel rows need a compact reconciliation section.
- If Air Travel is `RT`, user must enter a return date.
- RT return date must not be before outbound travel date.
- RT workbook travel date cell must be written as `DD.MM.YYYY - DD.MM.YYYY`, e.g. `17.01.2025 - 25.01.2025`.
- EG/MR selections for Meals & Entertainment write `x` in the report cells.
- Duplicate same-date same-meal expenses are not allowed; user must classify the extra receipt to another meal type.
- Multiple expenses written to the same A-page amount cell should be an Excel addition formula, e.g. `=86.25+4.85`.

## Current Implemented Behavior

### Review UI
- `/review` auto-loads the latest statement import.
- Main table has no Reason or Attendees columns.
- Category selector has 4 parent groups:
  - `Hotel & Travel`
  - `Meals & Entertainment`
  - `Air Travel`
  - `Other`
- `Personal Car` is removed.
- Bulk classify supports flagged/all rows.
- `Validate before generate` calls `GET /reports/validate/{statement_import_id}` and renders validation results.
- Validation messages can include row context: row id, transaction date, supplier, bucket.

### Air Travel
- Bucket: `Airfare/Bus/Ferry/Other`.
- Detail fields live in `ReviewRow.confirmed_json`, not DB columns:
  - `air_travel_date`
  - `air_travel_from`
  - `air_travel_to`
  - `air_travel_airline`
  - `air_travel_rt_or_oneway`
  - `air_travel_return_date`
  - `air_travel_paid_by`
  - `air_travel_total_tkt_cost`
  - `air_travel_prior_tkt_value`
  - `air_travel_comments`
- UI renders a compact one-line Air Travel Reconciliation detail row.
- `Return date` is shown only when RT is selected.
- Return date input has `min` equal to outbound travel date; changing outbound date nudges an earlier return date forward.
- Workbook detail rows:
  - Week 1A rows 47-49
  - Week 2A rows 48-50
  - B = travel date or RT date range
  - C = from
  - D = to
  - E = airline
  - F = RT/One way
  - G = paid by
  - H = total ticket cost
  - I = prior ticket value
  - J = template formula, do not overwrite
  - K = comments
- Main A-page row 7 writes ticket cost from `air_travel_total_tkt_cost` when present, even if statement amount is zero.
- Validation warns if a week page has more than 3 airfare detail rows.
- Validation errors:
  - `air_travel_return_date_missing`
  - `air_travel_return_date_before_travel_date`

### Meals & Entertainment
- Buckets:
  - `Meals/Snacks`
  - `Breakfast`
  - `Lunch`
  - `Dinner`
  - `Entertainment`
- Detail row collects:
  - place/type
  - location
  - participants
  - business reason
  - EG
  - MR
- B-page writes:
  - C = place/type
  - D = location
  - E = participants
  - F = business reason
  - H = `x` when EG selected
  - I = `x` when MR selected
  - J remains template amount formula.
- Duplicate business meal rows with same transaction date and same meal bucket are rejected on save.

### A-Page Totals
- Amounts that land in the same cell are written as formulas preserving components, e.g. `=86.25+4.85`.
- Single amounts remain plain numbers.

## Important Files
- `frontend/review-table.html`
- `backend/app/services/review_sessions.py`
- `backend/app/services/report_generator.py`
- `backend/app/services/report_validation.py`
- `backend/app/schemas.py`
- `backend/tests/smoke_air_travel.py`
- `backend/tests/smoke_meals_entertainment.py`
- `backend/tests/test_review_confirmation.py`
- `backend/tests/test_statement_import.py`

## Focused Handoffs To Read
- `docs/current_progress.md`
- `docs/AIR_TRAVEL_RETURN_DATE_HANDOFF.md`
- `docs/REPORT_PREFLIGHT_VALIDATION_HANDOFF.md`
- `docs/MEALS_ENTERTAINMENT_DETAIL_HANDOFF.md`
- `docs/AIRFARE_MAIN_ROW_HANDOFF.md`
- `docs/CATEGORY_GROUPING_HANDOFF.md`

## Verification Commands That Passed
From `C:\Users\CASPER\.openclaw\workspace\Expense\expense-reporting-app\backend`:
```bat
set PYTHONPATH=.
set PYTHONDONTWRITEBYTECODE=1
set PYTHONIOENCODING=utf-8
C:\Users\CASPER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -X utf8 tests\smoke_air_travel.py
C:\Users\CASPER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -X utf8 tests\smoke_meals_entertainment.py
```

From `C:\Users\CASPER\.openclaw\workspace\Expense\expense-reporting-app`:
```bat
set PYTHONPATH=backend
set PYTHONDONTWRITEBYTECODE=1
set PYTHONIOENCODING=utf-8
set EXPENSE_REPORT_TEMPLATE_PATH=C:\Users\CASPER\.openclaw\workspace\Expense\Expense Report Form_Blank.xlsx
C:\Users\CASPER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -X utf8 backend\tests\test_review_confirmation.py

set PYTHONPATH=backend
C:\Users\CASPER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -X utf8 backend\tests\test_statement_import.py

C:\Users\CASPER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -X utf8 -c "from html.parser import HTMLParser; from pathlib import Path; HTMLParser().feed(Path('frontend/review-table.html').read_text(encoding='utf-8')); print('html_parse=passed')"
```

Manual generated workbook inspection also passed:
- Generated a real report package with an RT airfare row.
- Inspected `expense_report_part_1.xlsx`, `Week 1A`.
- Verified:
  - `B47 = '17.01.2025 - 25.01.2025'`
  - `C47 = 'IST'`
  - `D47 = 'LHR'`
  - `F47 = 'RT'`
  - `H47 = 500`
  - `I47 = 0`
  - `J47 = '=H47-I47'`
  - `E7 = 500`

## Not Yet Verified In Browser
- Compact Air Travel row visual fit at user's current viewport.
- Return-date show/hide in live browser.
- Return-date `min` behavior in live browser.
- Contextual validation messages rendered in the live browser, although HTML parse and backend smoke pass.

## Suggested Next Step
Run one live-browser validation pass:
1. Start backend.
2. Open `/review`.
3. Pick/create an Air Travel row.
4. Select `RT`.
5. Leave return date blank or set it before travel date.
6. Confirm reviewed data if needed.
7. Click `Validate before generate`.
8. Confirm message includes row/date/supplier/bucket context.

If that works, the next build step should be either:
- improve visual row highlighting for validation issues; or
- move to the next report section needing template-specific detail handling.

## Do Not Do Next
- Do not add Personal Car yet.
- Do not add auth/admin UI.
- Do not redesign `/review`.
- Do not overwrite Air Travel column J.
- Do not bypass validation/confirmation gates.
- Do not broaden into OCR/model routing unless explicitly asked.
