# Telegram Bot Flow

## Purpose
The Telegram bot is the capture and clarification surface for coworkers. The backend remains the source of truth.

## Receipt Capture
1. Coworker sends a receipt photo or receipt PDF.
2. Bot creates an `AppUser` if needed.
3. Bot stores the Telegram file metadata, and downloads the file when `TELEGRAM_BOT_TOKEN` is configured.
4. Bot creates a `ReceiptDocument`.
5. Bot runs the receipt extraction layer against caption/file-name hints.
6. Bot creates targeted clarification questions only for missing or business-context fields.

## Clarification Flow
Current flow:
1. Missing receipt date, amount, or merchant if extraction cannot infer them.
2. Business or Personal if extraction cannot infer it.
3. If Business: project/customer/trip reason.
4. If Business: attendees or beneficiaries.

The extraction layer is currently deterministic and parses captions/file names. Future OCR/vision should plug into the same receipt fields before clarification questions are created.

Future matching should insert smarter questions only when required, for example:
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
