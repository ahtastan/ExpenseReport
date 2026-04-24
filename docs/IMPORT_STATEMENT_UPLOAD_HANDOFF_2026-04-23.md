# Import Statement Upload Handoff - 2026-04-23

## Current UI Architecture State
- `/review` is still a single-file React 18 SPA served from `frontend/review-table.html`.
- Import Statement remains the existing inline modal near Add Statement and uses the shared `apiForm` helper.
- Add Statement, selected-visible bulk toolbar, confirmation snapshots, report generation, Telegram, and VPS deployment behavior were intentionally left unchanged.

## Latest Completed Step
- Fixed the Import Statement modal to call `POST /statements/import-excel`, matching `backend/app/main.py` mounting `backend/app/routes/statements.py` at `/statements`.
- Extended the existing live `/review` raw-CDP smoke so it generates `live_import_statement_smoke.xlsx`, opens Import Statement, uploads the workbook, waits for the import success state, and verifies the imported row appears after refresh.

## Verified
- Static UI check verifies `/statements/import-excel` is present and the stale `/statements/import` form-post string is absent.
- Statement importer regression still passes for header variants, month-first dates, swapped-date repair, the real Diners fixture, and missing-header 400s.
- Live browser smoke passes through login, Add Statement validation/happy path, selected-visible toolbar behavior, validation, Import Statement upload success, and the refreshed imported row.

## Not Verified
- Manual operator upload of a large real statement through a non-smoke browser session.
- Invalid statement upload copy/UX beyond the existing backend error propagation.
- Any Telegram, VPS, confirmation snapshot, report package, or matching behavior for this slice.

## Exact Next Safest Step
- If continuing UI-only, add a tiny Import Statement invalid-upload smoke/check for operator-facing error clarity, without changing importer parsing or backend business logic.

## Risks If Continuing Carelessly
- Repointing routes broadly could break Telegram or direct statement API clients that already use `/statements/import-excel`.
- Importing a statement mid-smoke changes the latest statement and can invalidate earlier review-row assumptions; keep this check at the end of the live smoke.
- Touching shared review refresh, confirmation, matching, or report-generation paths from this UI fix could accidentally thaw confirmed sessions or alter package output.
