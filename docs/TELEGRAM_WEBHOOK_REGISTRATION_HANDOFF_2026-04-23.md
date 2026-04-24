# Telegram Webhook Registration Handoff - 2026-04-23

## Objective Of This Step
Add a focused helper for registering and inspecting the Telegram webhook without exposing bot tokens or webhook secrets in terminal output.

## Files Changed
- `scripts/register_telegram_webhook.py`
- `backend/tests/test_telegram_webhook_registration.py`
- `docs/current_progress.md`
- `docs/TELEGRAM_WEBHOOK_REGISTRATION_HANDOFF_2026-04-23.md`

## Exact Behavior Added
- New script: `scripts/register_telegram_webhook.py`.
- Loads optional local env files:
  - `.env`
  - `backend/.env`
  - parent workspace `.env`
- Reads:
  - `TELEGRAM_BOT_TOKEN`
  - `TELEGRAM_WEBHOOK_SECRET`
  - `TELEGRAM_WEBHOOK_URL`
- Also accepts `--url`, which may be either:
  - a base public URL, such as `https://example.ngrok-free.app`
  - a full webhook URL, such as `https://example.ngrok-free.app/telegram/webhook`
- If a base URL is provided, the script appends `/telegram/webhook`.
- Calls Telegram `setWebhook`.
- Sends `secret_token` when `TELEGRAM_WEBHOOK_SECRET` is configured.
- Calls Telegram `getWebhookInfo` after registration and prints sanitized JSON status.
- Supports `--status` to print sanitized webhook info without registering.
- Does not print the bot token or webhook secret.

## Usage
From the repo root:

```powershell
$env:TELEGRAM_BOT_TOKEN = "..."
$env:TELEGRAM_WEBHOOK_SECRET = "..."
& "C:\Users\CASPER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\register_telegram_webhook.py --url "https://your-public-url.example"
```

Status only:

```powershell
& "C:\Users\CASPER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\register_telegram_webhook.py --status
```

## Tests Run And Results
- Red check: `backend/tests/test_telegram_webhook_registration.py` failed before implementation because `scripts/register_telegram_webhook.py` did not exist.
- Green checks after implementation:
  - `backend/tests/test_telegram_webhook_registration.py` passed.
  - `python -m py_compile scripts/register_telegram_webhook.py backend/tests/test_telegram_webhook_registration.py` passed.
  - Running the script without `TELEGRAM_BOT_TOKEN` returned sanitized JSON: `{"status": "failed", "reason": "TELEGRAM_BOT_TOKEN missing"}`.

## What Is Verified
- Base public URLs are normalized to `/telegram/webhook`.
- `setWebhook` receives the normalized URL.
- `setWebhook` receives the configured `secret_token`.
- `getWebhookInfo` is called after registration.
- Returned JSON omits both the bot token and webhook secret.
- Missing token handling is sanitized.

## Not Verified
- No real Telegram Bot API registration was performed in this step because the real token and public URL are not present in this agent process.
- No live Telegram receipt or statement message was sent after registration.

## Next Recommended Step
With a public HTTPS tunnel or deployed host running the FastAPI app, set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, and either `TELEGRAM_WEBHOOK_URL` or `--url`, run the registration helper, then send a live receipt photo and Diners statement to the bot.
