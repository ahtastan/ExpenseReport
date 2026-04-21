# Telegram Bot Flow

## Purpose
The Telegram bot is the capture and clarification surface for coworkers. The backend remains the source of truth.

## Receipt Capture
1. Coworker sends a receipt photo or receipt PDF.
2. Bot creates an `AppUser` if needed.
3. Bot stores the Telegram file metadata, and downloads the file when `TELEGRAM_BOT_TOKEN` is configured.
4. Bot creates a `ReceiptDocument`.
5. Bot creates the first clarification question: Business or Personal.

## Clarification Flow
Current skeleton:
1. Business or Personal
2. If Business: project/customer/trip reason
3. If Business: attendees or beneficiaries

Future OCR/matching should insert smarter questions only when required, for example:
- amount unclear
- date unclear
- multiple statement matches
- meal attendees missing
- business reason missing

## Statement Flow
1. Coworker uploads Diners statement Excel.
2. Backend imports rows into `StatementImport` and `StatementTransaction`.
3. Matching engine links receipts to statement rows.
4. Bot summarizes missing receipts and review-needed matches.

## Webhook
Telegram should post updates to:

```text
POST /telegram/webhook
```

If `TELEGRAM_WEBHOOK_SECRET` is set, Telegram must send it as:

```text
X-Telegram-Bot-Api-Secret-Token
```

## Privacy Defaults
- Use `ALLOWED_TELEGRAM_USER_IDS` before inviting coworkers.
- Keep receipt files on the private server.
- Treat statement rows as canonical for report generation.
