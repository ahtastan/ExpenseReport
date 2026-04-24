# Manual Statement Live Smoke Handoff

## Current UI Architecture State
- `/review` remains the single-file React 18 SPA in `frontend/review-table.html`.
- The Add Statement modal remains unchanged in this step.
- The existing `scripts/run_live_review_smoke.ps1` harness now includes the Add Statement browser path.

## Latest Completed Implementation Step
- Extended the live review smoke to exercise Add Statement with an in-browser deterministic receipt file named `merchant=Migros_total_419.58TRY_2026-03-11.jpg`.

## Exact Behavior Verified
- `/review` opens and logs in through the existing smoke flow.
- `Add Statement` opens the modal.
- Receipt upload triggers extraction.
- Date, supplier, amount, and currency prefill from the deterministic filename.
- Supplier and amount remain editable before save.
- Saving creates a new manual review row visible as `Migros Market`.
- Existing selected-visible bulk and validation smoke checks still pass afterward.

## Tests Run
- `powershell -ExecutionPolicy Bypass -File scripts/run_live_review_smoke.ps1`
- Result: passed with raw CDP output showing `sawBulkToast=true` and `sawValidation=true`.

## Still Unverified
- PDF upload usefulness for extraction.
- Manual validation edge cases beyond the positive browser path.
- Real receipt OCR quality.

## Exact Next Recommended Step
- Stop this slice unless a new UI requirement is requested. The Add Statement happy path is now browser-smoked.

