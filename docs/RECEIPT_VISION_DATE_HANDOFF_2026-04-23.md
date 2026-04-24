# Receipt Vision Date Handoff - 2026-04-23

## Objective Of This Step
Fix a live Telegram receipt OCR miss where a Turkish receipt image visibly showed `TARIH : 04/09/2025`, but the bot still asked the user for the receipt date.

## Root Cause
The receipt filename `04-09-Önder.jpg` contains day/month but no year, so deterministic filename parsing cannot create a full date.

The vision merge path only accepted model dates in ISO format via `date.fromisoformat()`. If the model returned the visible receipt date as `04/09/2025`, the app discarded it and treated the date as missing.

## Files Changed
- `backend/app/services/clarifications.py`
- `backend/app/services/receipt_extraction.py`
- `backend/app/services/model_router.py`
- `backend/tests/test_clarification_non_answer.py`
- `backend/tests/test_receipt_extraction_vision_dates.py`
- `docs/current_progress.md`
- `docs/RECEIPT_VISION_DATE_HANDOFF_2026-04-23.md`

## Exact Behavior Changed
- Vision-returned dates are still parsed as ISO first.
- If ISO parsing fails, the merge now falls back to the existing local receipt date parser, which supports `DD/MM/YYYY` and `DD.MM.YYYY`.
- The vision OCR prompt now explicitly mentions Turkish `TARIH` labels and asks the model to convert `DD/MM/YYYY` or `DD.MM.YYYY` to ISO.
- Question-like replies to receipt-date prompts, such as `why can't you read the date?`, no longer close the date question as a failed answer. The original question stays open and the bot sends an explanatory helper prompt.

## Tests Run And Results
- Red check: `backend/tests/test_receipt_extraction_vision_dates.py` failed before implementation because `04/09/2025` from vision was discarded.
- Green checks after implementation:
  - `backend/tests/test_receipt_extraction_vision_dates.py` passed.
  - `backend/tests/test_clarification_non_answer.py` passed.
  - `backend/tests/test_model_router.py` passed.
  - `backend/tests/test_telegram_statement_import.py` passed.
  - `python -m compileall backend/app -q` passed.

## What Is Verified
- A vision response with `date="04/09/2025"` now extracts `2025-09-04`.
- The receipt no longer includes `receipt_date` in missing fields for that response.
- A meta-question reply to an open date prompt keeps the original question open and creates a helper prompt instead of a parse retry.
- Existing model-router and Telegram statement import regressions still pass.

## Not Verified
- The live Telegram receipt has not yet been retried after deploying this patch to the VPS.

## Next Recommended Step
Deploy `clarifications.py`, `receipt_extraction.py`, and `model_router.py` to the server, restart `dcexpense`, then resend the `04-09-Önder.jpg` receipt to the Telegram bot.
