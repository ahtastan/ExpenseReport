# Claude Handoff - Expense Reporting App

## Repo And Runtime
- Repo: `C:\Users\CASPER\.openclaw\workspace\Expense\expense-reporting-app`
- Backend app: `backend/app/main.py`
- Frontend review page: `frontend/review-table.html`
- Run backend from:
  ```bat
  cd /d C:\Users\CASPER\.openclaw\workspace\Expense\expense-reporting-app\backend
  C:\Users\CASPER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload
  ```
- Review page: `http://127.0.0.1:8080/review`

## Current Product State
The app is now a statement-led expense review and report-generation workflow:
1. Import Diners statement.
2. Build review rows from every statement transaction.
3. Receipts/matches enrich rows but do not determine row existence.
4. User edits/reviews rows in `/review`.
5. User confirms the review snapshot.
6. Report generation reads only the confirmed snapshot.
7. Generated package can be downloaded from `/reports/{run_id}/download`.

## Important Current Data
- Current local statement import: `statement_import_id=2`
- Current local review session: `review_session_id=3`
- Review session 3 is confirmed.
- Latest repaired snapshot prefix: `e2fcfebac4`
- Latest corrected report run: `report_run_id=4`
- Download URL while backend is running: `http://127.0.0.1:8080/reports/4/download`
- Run 2 exists but was generated before date/bucket fixes and should not be treated as final.

## What Was Fixed Recently
- `/review` auto-loads the latest statement import.
- Statement importer finds Diners headers even if not on row 1.
- Importer returns clean 400 errors for missing required columns.
- Review building is statement-led: all statement transactions appear even without receipts.
- SQLite startup migration repaired stale `reviewrow_old` index leftovers.
- Row saves auto-clear attention when required fields are complete.
- Bulk classification was added for flagged/all rows.
- Report download endpoint was added.
- The EDT reference workbook `Expense Report - How to complete New.xlsx` was inspected and used as template guidance.
- Date parsing was corrected for the real Diners file:
  - text dates are month-first;
  - Excel date-cell outliers are swapped only when the corrected date fits the surrounding statement window.
- Report bucket allocation now uses exact EDT template buckets; no substring matching.
- `Business` bucket no longer accidentally maps to Airfare/Bus/Ferry because it contains `bus`.

## Reference Workbook Guidance
Reference file inspected:
`C:\Users\CASPER\OneDrive - Enzymatic Deinking Technologies LLC\Masaüstü\Expense Report - How to complete New.xlsx`

Relevant template rows:
- Row 7: `Airfare/Bus/Ferry/Other`
- Row 8: `Hotel/Lodging/Laundry`
- Row 9: `Auto Rental`
- Row 10: `Auto Gasoline`
- Row 11: `Taxi/Parking/Tolls/Uber`
- Row 14: `Other Travel Related`
- Row 18: `Membership/Subscription Fees`
- Row 19: `Customer Gifts`
- Row 20: `Telephone/Internet`
- Row 21: `Postage/Shipping`
- Row 22: `Admin Supplies`
- Row 23: `Lab Supplies`
- Row 24: `Field Service Supplies`
- Row 25: `Assets`
- Row 26: `Other`
- Row 29: `Meals/Snacks`
- Row 30: `Breakfast`
- Row 31: `Lunch`
- Row 32: `Dinner`
- Row 35: `Entertainment`

## Latest Verification
Commands that passed:
```bat
C:\Users\CASPER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe backend\tests\test_statement_import.py
C:\Users\CASPER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe backend\tests\test_review_confirmation.py
```

Run 4 workbook inspection verified:
- Dates span `2026-03-11` through `2026-04-08`.
- Airfare row 7 is no longer populated by broad `Business` bucket values.
- Broad repaired bucket values land on row 26 `Other`.

## Known Limitations
- Most live rows are still categorized as broad `Other` because the user previously bulk-filled `Business`.
- Receipt matching/enrichment is not currently reflected in the live review session.
- OCR/model routing is not ready for production use.
- Report package mechanics work, but semantic classification needs improvement before a final polished submission.

## Next Best Step
Implement deterministic merchant-to-template-bucket suggestions:
- Uber, Takside, Bitaksi, Faturamati Taksi, Havuzlar Taksi -> `Taxi/Parking/Tolls/Uber`
- Hampton/Hilton/Hotel -> `Hotel/Lodging/Laundry`
- Shell, Petrol, Opet, Akaryakit -> `Auto Gasoline`
- Yemeksepeti, Starbucks, Doner, Kofteci -> meal bucket, likely `Meals/Snacks` unless user chooses breakfast/lunch/dinner
- Market/pharmacy/pet shop/tekel/retail -> likely `Other`

Keep these as suggestions, require user confirmation, and preserve the confirmed snapshot gate.

## Do Not Do Next
- Do not add auth.
- Do not redesign `/review` into a dashboard.
- Do not start broad OCR/model routing.
- Do not remove the confirmation gate.
- Do not treat run 2 as final.
