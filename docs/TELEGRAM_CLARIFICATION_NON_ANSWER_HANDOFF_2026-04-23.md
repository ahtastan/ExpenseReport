# Telegram Clarification Non-Answer Handoff - 2026-04-23

## Objective Of This Step
Prevent Telegram chatter/meta replies from being consumed as answers to pending receipt clarification questions, and improve OCR guidance for Turkish payment-slip dates.

## Files Changed
- `backend/app/services/clarifications.py`
- `backend/app/services/model_router.py`
- `backend/tests/test_clarification_non_answer.py`
- `docs/current_progress.md`
- `docs/TELEGRAM_CLARIFICATION_NON_ANSWER_HANDOFF_2026-04-23.md`

## Exact Behavior Changed
- `answer_question()` now checks for non-answer text before marking a clarification question answered.
- Non-answer text currently includes question-like replies and simple greetings/chatter: `hello`, `hi`, `hey`, `yo`, `selam`, `merhaba`.
- For `receipt_date` / `receipt_date_retry`, non-answers keep the original question open and return `receipt_date_help`.
- For `local_amount` / `local_amount_retry`, non-answers keep the original question open and return `local_amount_help`.
- For `business_or_personal` / `business_or_personal_retry`, non-answers keep the original question open and return `business_or_personal_help`.
- Existing retry behavior remains for non-chatter invalid answers.
- `_VISION_PROMPT` now tells the OCR model to use Turkish payment-slip `ISLEM` lines such as `DD/MM/YYYY - HH:MM` as the receipt date.

## Tests Run And Results
- Red check:
  - `C:\Users\CASPER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe backend\tests\test_clarification_non_answer.py`
  - Result before fix: failed at `assert amount_question.status == "open"` because `hello` closed the amount question.
- Green checks:
  - `C:\Users\CASPER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe backend\tests\test_clarification_non_answer.py` -> passed.
  - `C:\Users\CASPER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe backend\tests\test_receipt_extraction_vision_dates.py` -> passed.
  - `C:\Users\CASPER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe backend\tests\test_model_router.py` -> passed.
  - `C:\Users\CASPER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe backend\tests\test_telegram_statement_import.py` -> passed.
  - `C:\Users\CASPER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m compileall backend\app -q` -> passed.

## Real-Data Verification Status
- Verified from user screenshots only: the live Telegram bot asked for dates even when the images visibly contained `TARIH : 04/09/2025` and `ISLEM: 26/08/2025 - 14:12`.
- Verified locally with synthetic/unit coverage: a vision response with `date="04/09/2025"` extracts `2025-09-04`.
- Verified locally with regression coverage: `why can't you read the date?` and `hello` do not close the targeted open clarification questions.
- Not verified on the VPS after this patch.
- Not verified with a live Telegram resend after this patch.
- Not verified with a live OCR model call against the airport receipt after the `ISLEM` prompt change.

## Open Assumptions
- The VPS is still running older code until the changed files are copied to `/opt/dcexpense/app` and the `dcexpense` service is restarted.
- Existing stale open clarification questions in the production database may still be queued for the same Telegram user; the next live message may answer the oldest open question first.
- The OCR model may still return `null` if the uploaded Telegram image is too small/compressed; the prompt change only improves guidance.
- The helper prompts are intentionally narrow and do not attempt free-form conversation handling beyond simple non-answers.

## Next Recommended Step
Deploy only the changed backend files to the VPS, restart `dcexpense`, confirm `/health`, check Telegram webhook status, then resend the Onder and airport receipt images to the bot.

## Commands To Rerun
Local focused checks from repo root:

```powershell
$py = "C:\Users\CASPER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
& $py backend\tests\test_clarification_non_answer.py
& $py backend\tests\test_receipt_extraction_vision_dates.py
& $py backend\tests\test_model_router.py
& $py backend\tests\test_telegram_statement_import.py
& $py -m compileall backend\app -q
```

Production deploy/check commands from repo root and server shell:

```powershell
scp backend\app\services\clarifications.py root@46.225.103.156:/opt/dcexpense/app/backend/app/services/clarifications.py
scp backend\app\services\model_router.py root@46.225.103.156:/opt/dcexpense/app/backend/app/services/model_router.py
ssh root@46.225.103.156
```

```bash
chown dcexpense:dcexpense /opt/dcexpense/app/backend/app/services/clarifications.py /opt/dcexpense/app/backend/app/services/model_router.py
chmod 640 /opt/dcexpense/app/backend/app/services/clarifications.py /opt/dcexpense/app/backend/app/services/model_router.py
sudo -u dcexpense /opt/dcexpense/venv/bin/python -m compileall /opt/dcexpense/app/backend/app -q
systemctl restart dcexpense
systemctl status dcexpense --no-pager
curl -i http://127.0.0.1:8080/health
curl -i https://app.dcexpense.com/health
cd /opt/dcexpense/app
set -a
. /etc/dcexpense/env
set +a
/opt/dcexpense/venv/bin/python scripts/register_telegram_webhook.py --status
```
