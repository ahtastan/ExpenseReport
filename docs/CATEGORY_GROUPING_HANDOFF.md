# Category Grouping UI Handoff

## Update
Personal Car is intentionally removed from the active `/review` category selector because personal expense reports are out of scope for now. See `AIRFARE_MAIN_ROW_HANDOFF.md` for the focused update that made this change.

## Objective Of This Step
Group the 20 flat EDT template buckets under 5 parent categories in the `/review` UI so the user picks a broad category first, then filters to a narrower bucket. Frontend-only change.

## Files Changed
- `frontend/review-table.html`
- `docs/current_progress.md`

## Exact Behavior Added

### 5 parent categories (UI only, not persisted)
| Category | Child buckets |
|---|---|
| Hotel & Travel | Hotel/Lodging/Laundry, Auto Rental, Auto Gasoline, Taxi/Parking/Tolls/Uber, Other Travel Related |
| Meals & Entertainment | Meals/Snacks, Breakfast, Lunch, Dinner, Entertainment |
| Air Travel | Airfare/Bus/Ferry/Other |
| Personal Car | *(none yet — placeholder for mileage feature)* |
| Other | Membership/Subscription Fees, Customer Gifts, Telephone/Internet, Postage/Shipping, Admin Supplies, Lab Supplies, Field Service Supplies, Assets, Other |

### Behavior
- New `Category` column appears between `Business` and `Bucket`.
- On render, the row's category is **derived** from `confirmed.report_bucket` (via a local `BUCKET_TO_CATEGORY` lookup). Category is not saved to the DB.
- Changing a row's category repopulates only that row's bucket dropdown with the children of the chosen category. If the previously selected bucket still belongs to the new category, it stays selected; otherwise it clears.
- Bulk-classify bar gained a `Category` dropdown that filters `Bucket` the same way.
- Saving a row still sends only the standard `fields` payload (including `report_bucket`) — no new PATCH schema, no backend changes.

## Tests Run And Results
- Python HTML parser pass on `review-table.html` — clean.
- No backend changes; existing tests unchanged (`test_statement_import.py` / `test_review_confirmation.py` still in their last-known-good state from prior handoff).

## What Is Verified
- Category → bucket mapping covers all 20 existing EDT template buckets (audited 1:1 against `REPORT_BUCKETS`).
- `Personal Car` has no children yet and is visible as a placeholder.
- No schema change, no backend change, no change to report generator.

## What Remains Unverified
- Live browser render of the new column (static HTML only parsed, not rendered).
- User-visible column width adjustment (`min-width: 1380px` on table may need bumping).
- Session repair for currently-confirmed session 3 is unaffected (confirmed sessions remain confirmed).

## Reference Findings From The Guide Workbook
File: `Expense Report - How to complete New.xlsx` (copied to `Expense/guide.xlsx` for inspection).

### Air Travel Reconciliation (Week 1A rows 44-49; Week 2A rows 45-49)
- Row 44 (title): `AIR TRAVEL RECONCILIATION (Enter amount expensed above. Enter details in this section.)`
- Header rows 45-46:
  - B: TRAVEL DATE(S)
  - C: LOCATION FROM
  - D: TO
  - E: AIRLINE
  - F: RT / ONE WAY
  - G: TICKET PAID BY
  - H: TOTAL TKT COST
  - I: PRIOR TKT VALUE
  - J: AMOUNT EXPENSED (formula `=H-I`)
  - K: COMMENTS
- Data rows start at row 47.
- The row-7 main-line airfare total is independent of this detail table — the detail table just documents the trip.

### Personal Car / Mileage (rows 12-13)
- Row 12 (cells `E12`-`K12`): miles/km count per date column.
- Cell `D13`: `$/mile` or `$/km` rate (single value for the whole page).
- Row 13 (formulas `=E12*$D$13` etc.): auto-computed reimbursement per day.
- Cell `C13` unit label formula: `=IF(M6<>(B65),"(__/KM)","($/Miles)")` — i.e., if report currency `M6` is USD (matches `B65`), unit is Miles; otherwise KM. Because `M6` is always `USD` in practice, unit defaults to Miles. A manual override of this cell is possible but template-supported.

## Next Recommended Step — Two Options
Pick one and execute as an independent handoff:

### Option A: Air Travel Reconciliation
1. Add optional fields on the review row's `confirmed_json`: `air_travel_date`, `air_travel_from`, `air_travel_to`, `air_travel_airline`, `air_travel_rt_or_oneway`, `air_travel_paid_by`, `air_travel_total_tkt_cost`, `air_travel_prior_tkt_value`, `air_travel_comments`. These are optional and do not break missing-field validation.
2. In `/review`, render an extra detail panel below the row when its parent category is `Air Travel`.
3. In `report_generator._fill_workbook`, for every line with bucket `Airfare/Bus/Ferry/Other`, write one row starting at `B47` with the detail fields on the matching sheet.
4. Smoke: import a statement that includes an airline supplier, fill detail fields, generate report, verify row 47+ populated.

### Option B: Personal Car mileage
1. Add a new `ReviewSession.mileage_unit` column (`"miles"` or `"km"`, nullable). Add a guard so it cannot change within a session.
2. Add optional `confirmed_json` fields per row: `personal_car_quantity`, `personal_car_rate`. Only populate when parent category is `Personal Car`.
3. In UI, show a session-level picker (locked after the first value is set); show per-row quantity + rate inputs only when category is `Personal Car`.
4. In `report_generator._fill_workbook`, aggregate per-date miles/km into `E12`-`K12`, write the rate into `D13`, leave row 13 formulas intact.
5. Smoke: create a session, pick `miles`, add a mileage row, generate report, verify row 12 and `D13` populated.

## Commands To Rerun
No new commands. Prior regression:

```bash
PY='C:/Users/CASPER/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
"$PY" backend/tests/test_statement_import.py
PYTHONDONTWRITEBYTECODE=1 EXPENSE_REPORT_TEMPLATE_PATH="$(pwd)/../Expense Report Form_Blank.xlsx" \
  "$PY" backend/tests/test_review_confirmation.py
```

## Do Not Do Next
- Do not wire `Personal Car` child buckets until Option B schema is designed — leaving it empty on purpose is correct for now.
- Do not change the row PATCH payload shape until Option A or B lands.
- Do not treat the current derived category as a persisted field — it is rendered from the bucket.
