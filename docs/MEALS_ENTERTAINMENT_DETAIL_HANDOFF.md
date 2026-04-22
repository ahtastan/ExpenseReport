# Meals Entertainment Detail Handoff

## Objective Of This Step
Move Reason and Attendees off the main review table into a Meals & Entertainment detail section, and carry EG/MR flags into the B pages of the generated report.

## Files Changed
- `backend/app/services/review_sessions.py`
- `backend/app/services/report_generator.py`
- `backend/tests/smoke_meals_entertainment.py`
- `frontend/review-table.html`
- `docs/current_progress.md`

## Exact Behavior Added
- `/review` no longer shows `Reason` or `Attendees` columns in the main transaction table.
- Meal/entertainment buckets (`Meals/Snacks`, `Breakfast`, `Lunch`, `Dinner`, `Entertainment`) now show a detail row similar to Air Travel.
- The meal detail row collects:
  - `meal_place` for B-page `PLACE / TYPE`
  - `meal_location` for B-page `LOCATION`
  - `attendees` for B-page `PARTICIPANTS`
  - `business_reason` for B-page `BUSINESS REASON`
  - `meal_eg` and `meal_mr` through EG/MR toggle buttons
- Fresh review rows include empty meal detail defaults.
- `update_review_row` accepts meal detail keys for both new and older rows.
- `_confirmed_lines` surfaces meal detail fields on `ReportLine`.
- `_fill_workbook` writes meal detail values onto Week 1B/2B rows by date and meal code:
  - C = place/type
  - D = location
  - E = participants
  - F = business reason
  - H = `x` when EG is selected
  - I = `x` when MR is selected
  - J is left as the template amount formula
- A-page total cells now preserve multiple same-cell expenses as an Excel addition formula such as `=86.25+4.85` instead of a collapsed float.
- Review-row saves reject duplicate business meal rows with the same transaction date and meal bucket. Additional same-meal receipts must be reclassified to another meal type. The duplicate error suggests only other valid meal buckets, never the bucket that caused the conflict.

## Tests Run And Results
- Red check: `backend/tests/smoke_meals_entertainment.py` failed before implementation because `ReportLine` had no `meal_place`.
- Green check: `backend/tests/smoke_meals_entertainment.py` passed after implementation.
- Existing regressions run after implementation:
  - `backend/tests/smoke_air_travel.py`
  - `backend/tests/test_review_confirmation.py`
  - `backend/tests/test_statement_import.py`
  - HTML parse check for `frontend/review-table.html`

## What Is Verified
- A confirmed Lunch row writes Week 1A `E31=86.25`.
- A duplicate same-date `Lunch` save is rejected with a clear error suggesting `Meals/Snacks`, `Breakfast`, `Dinner`, or `Entertainment`.
- A duplicate same-date `Meals/Snacks` save is rejected with a clear error suggesting `Breakfast`, `Lunch`, `Dinner`, or `Entertainment`.
- Reclassifying the second same-date receipt as `Meals/Snacks` is allowed and writes Week 1A `E29=4.85` while the Lunch row remains `E31=86.25`.
- The matching Week 1B Lunch detail row writes `C10:F10`, `H10=x`, `I10=x`.
- The B-page amount cell `J10` remains a formula.
- The review HTML parses after moving Reason/Attendees into detail rows.

## What Remains Unverified
- Live browser toggling of EG/MR.
- Live browser display of the duplicate error returned by the API.
- Real-data package regeneration from the live local DB.

## Next Recommended Step
Add validation or warnings for B-page detail overflow/duplicates so repeated same-date same-meal rows are surfaced instead of silently skipped.
