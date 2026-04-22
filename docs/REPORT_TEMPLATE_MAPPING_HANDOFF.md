# Report Template Mapping Handoff

## Objective Of This Step
Align report generation with the EDT expense form guidance in `Expense Report - How to complete New.xlsx`.

## Reference Workbook Findings
- Sheets are `Week 1A`, `Week 1B`, `Week 2A`, and `Week 2B`.
- `Week 1A` and `Week 2A` hold category totals by date in columns `E:K`.
- `Week 1B` and `Week 2B` are meal/entertainment detail pages populated by formulas from A pages.
- Each workbook supports 14 scattered expense dates, then additional workbook parts are reasonable.
- Diners Club expenses must be entered in USD.
- Receipts are required for all lodging, all entertainment, and other expenditures over `$25`.

## Exact Behavior Changed
- Diners date parsing now treats current transaction text dates as month-first.
- The importer repairs observed Excel date-cell outliers when month/day were swapped and the swapped date fits the surrounding statement window.
- The real `03_11_Receipts/Diners_Transactions.xlsx` fixture now imports as `2026-03-11` through `2026-04-08`.
- Report allocation now uses exact template-native bucket names instead of substring matching.
- `Business` as a bucket now falls back to `Other`; it no longer matches `bus` and lands in Airfare/Bus/Ferry.
- `/review` bucket inputs now use template-native dropdown options.
- Live review session 3 was repaired:
  - 91 rows updated to corrected dates.
  - old `Business` report bucket values changed to `Other`.
  - session reconfirmed with snapshot prefix `e2fcfebac4`.
  - report run 4 generated and available at `/reports/4/download`.

## Files Changed
- `backend/app/services/statement_import.py`
- `backend/app/services/report_generator.py`
- `backend/tests/test_statement_import.py`
- `backend/tests/test_review_confirmation.py`
- `frontend/review-table.html`
- `docs/current_progress.md`
- `docs/REPORT_TEMPLATE_MAPPING_HANDOFF.md`

## Tests Run And Results
- `python backend\tests\test_statement_import.py`
  - Result: passed.
- `python backend\tests\test_review_confirmation.py`
  - Result: passed.

## What Is Verified
- Real Diners fixture period is now March 11, 2026 through April 8, 2026.
- Generated run 4 workbooks use March 11 through April 8 dates.
- Row 7 Airfare/Bus/Ferry is no longer populated by broad `Business` bucket values.
- Broad `Other` bucket values land on row 26.

## What Remains Unverified
- Semantic category quality: most rows are still broad `Other` because they were previously bulk-filled as `Business`.
- Browser-level review of run 4 download.
- Precise merchant-to-template-bucket classification.

## Next Recommended Step
Add deterministic merchant-to-template-bucket suggestions for common suppliers, then use `/review` to inspect and confirm those suggestions before generating the final package.
