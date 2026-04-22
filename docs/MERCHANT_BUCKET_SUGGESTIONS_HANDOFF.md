# Merchant Bucket Suggestions Handoff

## Objective Of This Step
Add deterministic merchant-to-template-bucket suggestions so new review rows arrive pre-classified instead of blank, reducing manual review effort.

## Files Changed
- `backend/app/services/merchant_buckets.py` — new module with `suggest_bucket()`
- `backend/app/services/review_sessions.py` — import + wire into `_statement_payload()`
- `docs/current_progress.md` — updated

## Exact Behavior Added

### `suggest_bucket(supplier_raw: str | None) -> str | None`
Scans the supplier string case-insensitively against ordered regex rules.
Returns an exact EDT template bucket name, or `None` if no rule matches.

Rules (in priority order):
| Pattern keywords | Bucket |
|---|---|
| uber, takside, bitaksi, faturamati taksi, havuzlar taksi, taksi | `Taxi/Parking/Tolls/Uber` |
| hampton, hilton, marriott, sheraton, hyatt, hotel, otel | `Hotel/Lodging/Laundry` |
| shell, opet, petrol, akaryakıt, bp, total oil, lukoil | `Auto Gasoline` |
| yemeksepeti, getir, starbucks, cafe, doner, kofteci, restaurant, lokanta, pizza, burger, mcdonalds, kfc | `Meals/Snacks` |
| thy, turkish airlines, pegasus, sunexpress, flypgs, anadolujet | `Airfare/Bus/Ferry/Other` |
| turkcell, vodafone, türk telekom, superonline, turknet, internet, gsm, fatura | `Telephone/Internet` |
| avis, hertz, budget rent, sixt, europcar, oto kiralama | `Auto Rental` |
| sinema, cinema, tiyatro, biletix, konser | `Entertainment` |
| kırtasiye, ofis depo, officedepot, staples | `Admin Supplies` |
| migros, bim, a101, sok, file, market, eczane, pharmacy, tekel, petshop | `Other` |
| (no match) | `None` (user must classify) |

### Integration point
`_statement_payload()` in `review_sessions.py` now sets `"report_bucket": suggest_bucket(tx.supplier_raw)`.
This populates both `suggested_json` and `confirmed_json` when a new review row is created.
It only affects **new unmatched rows** — `_row_payload()` (matched-receipt rows) is unchanged.

## Tests Run And Results
- `python -m compileall -f backend/app` — passed, no errors.
- `suggest_bucket` unit smoke: 16/16 cases correct.
- Integration smoke: 4-row review session showed correct buckets for BITAKSI, HILTON, Yemeksepeti; `None` for unknown vendor.
- `python backend/tests/test_statement_import.py` — passed.
- `python backend/tests/test_review_confirmation.py` — passed.

## What Is Verified
- Pattern matching correct for all handoff-specified merchants.
- Bucket strings are exact EDT template names (no typos).
- Existing confirmed sessions are unaffected (only new rows get suggestions).
- Confirmation gate and validation still pass.

## What Remains Unverified
- Live `/review` display with real Diners import.
- Real Diners supplier names (e.g. "FATURAMATI TAKSI" vs actual truncated card statement name).
- Matched-receipt rows already carrying `receipt.report_bucket` are still honored (by design — `_row_payload` unchanged).

## Next Recommended Step
1. Start the backend: `cd backend && python -m uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload`
2. Import the real `Diners_Transactions.xlsx` via `POST /statements/import`.
3. Open `/review`, check that the new statement's rows show bucket suggestions for known suppliers.
4. Adjust any misclassified rows, confirm, generate report run 5.
5. If real supplier names don't match patterns, add new patterns to `merchant_buckets.py`.

## Commands To Rerun
```bash
PY='C:/Users/CASPER/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
PYTHONDONTWRITEBYTECODE=1 EXPENSE_REPORT_TEMPLATE_PATH="$(pwd)/../Expense Report Form_Blank.xlsx" \
  "$PY" backend/tests/test_review_confirmation.py
PYTHONDONTWRITEBYTECODE=1 "$PY" backend/tests/test_statement_import.py
```
