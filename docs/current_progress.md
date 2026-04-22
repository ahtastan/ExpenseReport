# Current Progress

## Objective
Build the private-server backend for an OpenClaw/Telegram expense bot where coworkers can send receipts, answer clarifying questions, upload Diners statements, and later generate expense packages.

## Implemented
- SQLite/SQLModel app foundation.
- Local file storage under `backend/data` by default.
- Telegram webhook skeleton for receipt photo/PDF capture.
- Receipt upload/list/update APIs.
- Deterministic receipt-field extraction from captions/file names.
- Manual `POST /receipts/{receipt_id}/extract` rerun endpoint.
- Clarification question flow for missing date, amount, merchant, business/personal, business reason, and attendees.
- Statement-led report review sessions and review rows for user confirmation before final package generation.
- Review rows are seeded from every statement transaction; approved receipt matches enrich rows but do not determine whether a row exists.
- Minimal web review table at `GET /review`, served from `frontend/review-table.html`.
- Latest statement import lookup at `GET /statements/latest`, using the same newest-first `created_at` ordering as the statement list.
- Diners Excel importer scans for the header row instead of assuming row 1, and tolerates spacing/case/header-name variants for the current Diners format.
- Diners Excel importer treats current Diners transaction dates as month-first and repairs observed Excel date-cell outliers where month/day were swapped outside the surrounding statement window.
- Web review table can load review rows, save edits, confirm reviewed data, and explicitly generate a report package through `POST /reports/generate`.
- Web review table auto-loads the latest statement import and related review session on page open, while keeping manual statement id loading available.
- Web review table includes a narrow bulk-classification control for flagged/all rows so required review fields can be filled without row-by-row saving.
- Web review table now uses template-native report bucket choices instead of free-text buckets such as `Business`.
- Web review table shows report-generation success/error states and makes reconfirmation after edits visible.
- Report generation is blocked until a confirmed review snapshot exists.
- Generated report packages can be downloaded from `GET /reports/{report_run_id}/download`, and `/review` shows a download link after generation.
- Final report generation reads the confirmed review snapshot instead of live mutable receipt rows.
- Report generation now maps exact template-native bucket names to the EDT expense form rows; invalid buckets fall back to `Other` instead of substring-matching `Business` as `bus`.
- Diners Excel import into canonical statement transactions.
- Receipt-to-statement matching service using local amount, date proximity, and merchant similarity.
- Match decision list/approve/reject APIs.
- Review summary API.
- Legacy receipt mapping import service, CLI script, and API route.
- Report-readiness validation service and `GET /reports/validate/{statement_import_id}` endpoint.
- Database-backed report package generation service and `POST /reports/generate` endpoint.
- Report generation uses the existing corporate blank Excel template, writes confirmed statement-backed review rows into one or more workbook parts, builds an annotated receipt PDF, and includes a validation summary in the package.
- Annotated receipt PDF generation from confirmed review rows using the legacy 3x3 A4 visual style, with placeholder tiles when receipt files are missing.

## Real-Data Status
- Verified `Diners Club Statement.xlsx` imports as 91 transactions.
- Verified `03_11_Receipts/Diners_Transactions.xlsx` imports through the Diners importer smoke test.
- Verified period detection: `2026-03-11` through `2026-04-08`.
- Seeded 60 known mapped image receipts from `Authoritative_Receipt_Mapping_Table_Combined_Images.csv` in an in-memory verification run.
- Matching considered 60 receipts, skipped 0, created 78 candidates, marked 60 as high confidence, and auto-approved 58 unique high-confidence matches after uniqueness checks.
- Legacy import verification loaded 60 mapped receipt rows and resolved 60 existing receipt file paths.
- Report validation real-data smoke test: ready=True, errors=0, warnings=21 after stricter matching auto-approved 58 unique high-confidence matches.
- Report generation real-data smoke test completed: generated `expense_report_package.zip` containing `expense_report_part_1.xlsx`, `expense_report_part_2.xlsx`, `annotated_receipts.pdf`, and `validation_summary.txt`.
- Live report run 2 generated `expense_report_package.zip` and is available at `/reports/2/download`.
- Annotated receipt PDF verification: `annotated_receipts.pdf` exists and has 7 pages for 58 approved matches.
- Receipt extraction smoke test parsed a synthetic Telegram-style caption/file name into date, supplier, amount, currency, and business/personal with confidence 1.0.
- Receipt extraction/report regression status is documented in `docs/RECEIPT_EXTRACTION_HANDOFF.md`.
- Review confirmation gate smoke test passed: report generation is blocked before confirmation, confirmed snapshot is used instead of live mutable rows, and edits after confirmation require reconfirmation.
- Review-row save behavior now clears attention automatically when required fields are complete, and the bulk review update smoke test passed for clearing flagged rows.
- Statement-led review smoke test passed: statements with no receipts still create review rows, approved matches enrich transaction rows, existing empty draft sessions are backfilled, and confirmed unmatched rows can generate a package with missing-receipt placeholders.
- Real-file statement-led smoke test with `03_11_Receipts/Diners_Transactions.xlsx`: transactions=91, review_rows=91, attention_required=91 when no receipts are loaded.
- Live local session repair: review session 3 was bulk-updated with `Business` / `Business` for 91 flagged rows, leaving 0 attention rows; user confirmation is still required before report generation.
- SQLite startup migration smoke test passed after repairing the interrupted `reviewrow` nullable-column migration: stale `reviewrow_old` index names are cleaned up and `reviewrow` indexes are restored on the live table.
- Default local DB startup smoke passed for `backend/data/expense_app.db`; the stale `reviewrow_old` table/indexes from the failed startup were removed.
- Review table serving smoke test passed: `GET /review` returns the static HTML review page.
- Latest statement selection smoke test passed: `GET /statements/latest` returns the newest seeded statement import and `/review` includes the latest-statement auto-load path.
- Statement import smoke test passed for metadata rows before headers, header spelling/spacing variants, the real `Diners_Transactions.xlsx` upload file, and clean 400-level missing-header failures.
- Date import regression now verifies the real `Diners_Transactions.xlsx` period as `2026-03-11` through `2026-04-08`.
- Report bucket allocation regression now verifies `Business` falls back to `Other` and template buckets map to their intended rows.
- Live local session repair: review session 3 was updated from the old frozen snapshot to corrected dates and template bucket `Other`; report run 4 was generated and is available at `/reports/4/download`.
- Earlier approved-match real-data review regression passed with 58 review rows, confirmed snapshot hash, and the existing report package output; current statement-led importer/review smoke now verifies 91 statement rows create 91 review rows before receipt enrichment.
- Matching logic has been implemented against receipt fields, but OCR is not implemented yet.
- Vision-based OCR extraction added to `receipt_extraction.py` via `_vision_extract()`: calls Claude claude-opus-4-7 with base64-encoded image when `ANTHROPIC_API_KEY` is set and a local image file exists. Falls back transparently to deterministic extraction when key is absent or API fails.
- `anthropic>=0.40` added to `backend/pyproject.toml`.
- Regression smoke test (synthetic, no API key): status=extracted, date=2026-03-11, supplier=Migros, amount=419.58, currency=TRY, confidence=1.0 â€” passed.

- Deterministic merchant-to-bucket suggestion service (`backend/app/services/merchant_buckets.py`) implemented. New unmatched review rows get pre-filled `report_bucket` suggestions via `suggest_bucket(supplier_raw)`. 16/16 pattern cases verified; `_statement_payload()` integration verified; existing test suites passed.
- Review UI now has a 4-parent-category dropdown (Hotel & Travel / Meals & Entertainment / Air Travel / Other) that filters the child bucket dropdown. Parent is derived from the current bucket on render; selection is not persisted as a new field. Bulk-classify bar also has a Category filter. Personal Car was removed from the category selector because personal expense reports are out of scope for now.
- Air Travel Reconciliation wired end-to-end. New review rows get 9 optional air-travel defaults in `confirmed_json` (`air_travel_date`, `_from`, `_to`, `_airline`, `_rt_or_oneway`, `_paid_by="DC Card"`, `_total_tkt_cost`, `_prior_tkt_value=0`, `_comments`). `update_review_row` accepts the new keys on pre-existing rows. `_confirmed_lines` surfaces them onto `ReportLine`. `_fill_workbook` writes B/C/D/E/F/G/H/I/K for up to 3 airfare-bucket lines per page â€” row 47 on Week 1A, row 48 on Week 2A â€” and never touches column J (template formula `=H-I` preserved). Airfare rows also write the ticket cost to the main row-7 `AIRFARE/BUS/FERRY/OTHER` day column, even when the statement/review amount is zero. Review UI renders a hidden detail `<tr>` under every row, shown only when the bucket is `Airfare/Bus/Ferry/Other`, bound to the existing PATCH payload. `saveRow` skips `data-row` elements without `data-key` (the derived category select) and sends empty numeric inputs as `null`. Smoke (`backend/tests/smoke_air_travel.py`): Week 1A row 47 populated with synthetic Pegasus line, column J still holds `=H47-I47`, row 48 left blank, and `E7=523.45` from ticket cost. Existing regressions (`test_review_confirmation.py`, `test_statement_import.py`) still pass.
- Meals & Entertainment detail entry added to `/review`. Reason and Attendees were removed from the main table and moved into a hidden detail row for meal/entertainment buckets, alongside Place/Type, Location, and EG/MR toggle buttons. Confirmed meal detail fields now feed Week 1B/2B columns C/D/E/F/H/I on the row matching the transaction date and meal type while preserving the existing amount formula in column J. Selected EG/MR flags write `x` into their cells. Smoke (`backend/tests/smoke_meals_entertainment.py`) verifies a Lunch row writes Week 1A `E31=86.25`, Week 1B `C10:F10`, `H10=x`, `I10=x`, and leaves `J10` as a formula.
- Review-row saves now reject duplicate business Meals & Entertainment rows with the same transaction date and same meal bucket. If a second receipt exists for the same breakfast/lunch/dinner/etc., the user must classify it under another meal type; the error message suggests only other valid meal buckets and never the duplicate bucket itself.
- When multiple expenses land in the same A-page template total cell, the workbook now writes an Excel addition formula preserving the individual components, e.g. `=86.25+4.85`, instead of collapsing them to a single summed float. The meal smoke now covers two Lunch expenses on the same transaction date.
- Live browser smoke is now available and passing through `scripts/run_live_review_smoke.ps1`: it launches isolated Chrome via CDP, logs in, bulk-classifies flagged rows, expands Air Travel, verifies the return-date guard, and reaches Report Validation.
- `/review` now includes a `Validate before generate` button that calls `GET /reports/validate/{statement_import_id}` and displays blocking errors/warnings before package generation. Validation now uses the confirmed review snapshot for generation-facing checks, reports an error when review data is not confirmed, and warns when either week page has more than 3 Air Travel Reconciliation rows, because only 3 detail rows can be written to the template.
- Air Travel Reconciliation was compacted in `/review` so the detail controls fit on one line with smaller boxes. RT rows now reveal an extra `Return date` field, validation blocks confirmed RT airfare rows without that return date or with a return date before the travel date, and generated workbook travel-date cells write a date range such as `12.03.2026 - 15.03.2026` when RT + return date are present.
- React SPA Air Travel Reconciliation now preserves/patches `air_travel_return_date`, shows the `Return date` input whenever Round trip is selected, and keeps the detail controls on one horizontal row with overflow as the narrow-screen fallback.
- React SPA Air Travel Reconciliation now blocks saving a round-trip row when the return date is earlier than the travel date, showing an inline error before the PATCH request.
- Report validation issues can now include confirmed-review row context (`review_row_id`, supplier, transaction date, and bucket). `/review` renders that context beside validation messages so Air Travel RT errors point to the exact row that needs correction.
- Air Travel RT validation issues now also include `air_travel_date`, `air_travel_return_date`, and `air_travel_rt_or_oneway`; `/review` renders those values as chips so the bad date pair is visible directly from Report Validation.
- Review UI redesigned as a full single-file React 18 SPA (IBM Plex Sans, CDN Babel). Adds login screen, sidebar navigation, dashboard with stat cards and category spend bars, review queue with inline dropdowns and expandable detail rows, workflow bar, validation panel, receipt preview modal, import modal, and client-side audit log. HTML parse verified.
- Category dropdown bug fixed: selecting a parent category (Hotel & Travel / Meals & Entertainment / Air Travel / Other) now updates local React state only via `handleLocalUpdate`, instead of calling the API with `{report_bucket:null}` and losing the selection. The bucket dropdown repopulates with the correct child options; the API is called only when the user picks a specific bucket.
- Bulk-classify was re-added to the React SPA review queue. The toolbar can apply B/P and/or a category-filtered bucket to flagged rows or all rows through the existing `POST /reviews/report/{review_session_id}/bulk-update` endpoint, then reloads the current statement/session.
- Validation issue rows now render API-provided row context chips (`Row #`, supplier, transaction date, and bucket), so Air Travel RT date errors point to the exact review row to fix.

## Not Implemented Yet
- Personal expense report categories, including Personal Car mileage reimbursement, are intentionally out of scope for now.
- Telegram webhook registration script.
- Vision OCR tested against real receipt images.
- PDF receipt OCR/rendering.
- Full frontend app beyond the served static review table.
- Authentication/admin web UI.

## Recommended Next Step
Live browser smoke now passes in the isolated test path. The next narrow app/data step is to correct live review row `2`: supplier `Istanbul Oht-4 Dogu Sh`, bucket `Airfare/Bus/Ferry/Other`, `RT`, travel date `2026-05-09`, return date `2026-03-30`. Correct that row's return date or classify it as one-way, then reconfirm and re-run validation.

**Live browser smoke pass** — start the backend, open `/review`, log in as `ahmet/demo`, and verify:
1. Review queue loads rows from the latest statement import.
2. Selecting a parent category repopulates the bucket dropdown without clearing the selection.
3. Selecting a bucket saves immediately via PATCH and persists on page reload.
4. An Air Travel row shows the compact reconciliation detail when expanded.
5. A Meals & Entertainment row shows the detail panel when expanded.
6. Click `Validate before generate` — confirm messages identify the exact row/date/supplier/bucket.
7. Use the restored bulk-classify toolbar on flagged rows and verify the page reloads with updated rows.
Current live validation error: review row `2`, supplier `Istanbul Oht-4 Dogu Sh`, bucket `Airfare/Bus/Ferry/Other`, has `RT`, travel date `2026-05-09`, and return date `2026-03-30`. Correct that row's return date or classify it as one-way, then reconfirm and re-run validation.
If all pass, the next feature step should be deciding whether the bulk toolbar needs a selected-visible-rows scope.
