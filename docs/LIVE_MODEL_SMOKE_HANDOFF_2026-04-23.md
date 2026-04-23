# Live Model Smoke Handoff - 2026-04-23

## Objective Of This Step
Make the disposable live model smoke fail clearly when the runtime is missing the OpenAI SDK, then repair the local bundled Python runtime so the smoke can reach the real OCR/matching/synthesis calls when a key is set.

## Files Changed
- `.gitignore`
- `scripts/run_live_model_smoke.py`
- `backend/tests/test_live_model_smoke_preflight.py`
- `docs/current_progress.md`
- `docs/LIVE_MODEL_SMOKE_HANDOFF_2026-04-23.md`

## Exact Behavior Changed
- Added `_openai_sdk_status()` to `scripts/run_live_model_smoke.py`.
- The smoke now checks for an importable `openai` SDK immediately after confirming `OPENAI_API_KEY` is present.
- If the SDK is missing, the smoke prints sanitized JSON with `status="failed"`, `step="preflight"`, a dependency reason, and an install hint, then exits with code 3.
- `.env` and `backend/.env` are now ignored by git so local API keys are less likely to be staged accidentally.
- Updated both OpenAI Chat Completions call sites in `model_router.py` to send `max_completion_tokens=256` instead of `max_tokens=256`.
- Updated live-smoke template selection so fallback candidates must be files, not directories.

## Root Cause Found
First live-smoke blocker:

The user ran the smoke with:

```powershell
& "C:\Users\CASPER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\run_live_model_smoke.py
```

That same Python could not import `openai`, so `model_router._call_openai()` returned `None` and the smoke surfaced the generic `OCR model smoke returned no result` error.

Second live-smoke blocker:

After installing the SDK, the API responded with 400 errors for both `gpt-5.4-mini` and `gpt-5.4`:

```text
Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead.
```

The router now uses `max_completion_tokens` for vision OCR, matching, and synthesis text calls.

Third live-smoke blocker:

After model calls succeeded, package generation failed when openpyxl tried to load the report template. `EXPENSE_REPORT_TEMPLATE_PATH` was unset, so `Path("")` resolved to the current directory and `_first_existing()` accepted that directory before checking the real `../Expense Report Form_Blank.xlsx` workbook. `_first_existing()` now ignores directories.

## Environment Change
Installed `openai>=1.50` into:

```text
C:\Users\CASPER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe
```

The installed version observed after install is `openai 2.32.0`.

## Tests Run And Results
- Red check: `backend/tests/test_live_model_smoke_preflight.py` failed before implementation because `_openai_sdk_status()` did not exist.
- Green checks after implementation:
  - `backend/tests/test_live_model_smoke_preflight.py` passed.
  - `backend/tests/test_live_model_smoke_template_path.py` passed after the directory-skip change.
  - `backend/tests/test_model_router_openai_params.py` passed after the `max_completion_tokens` change.
  - `python -m py_compile scripts/run_live_model_smoke.py backend/tests/test_live_model_smoke_preflight.py` passed.
  - A fake-key preflight run printed the new sanitized missing-SDK JSON and exited with code 3 before dependency installation.
  - User-run live model smoke passed with a configured API key.

## What Is Verified
- The smoke has an explicit preflight for missing `openai`.
- The bundled Python can now import the installed OpenAI SDK.
- The preflight regression test still simulates a missing SDK even when the package is installed.
- Router tests verify OpenAI calls no longer send `max_tokens` and do send `max_completion_tokens`.
- Template path tests verify missing template env falls back to the real blank `.xlsx` workbook instead of the current directory.
- Live OCR returned all critical fields on `gpt-5.4-mini` with no escalation.
- Live matching returned a high-confidence transaction id on `gpt-5.4-mini`.
- Live report synthesis produced non-empty `summary.md`.
- Disposable package generated at `.verify_data/live_model_smoke_e89f3e69269b4c4a9e033a1cc1808341/reports/report_1_20260423T043010Z/expense_report_package.zip`.

## Not Verified
- The generated `summary.md` content has not been manually reviewed for finance-facing quality.
- The disposable package has not been compared visually against expected Excel/PDF output.

## Next Recommended Step
Open the disposable package and review `summary.md` plus the generated workbook/PDF outputs for content quality. If the narrative is acceptable, the next narrow product step is exposing the package summary in the normal report-download or review workflow.
