# Expense Reporting App

A scalable, statement-first expense reporting application extracted from the current Diners Club workflow.

## Why this exists
The current `Expense/` folder proves the workflow works, but it is still script-heavy and requires manual fixes. This repo is the productized next step.

## Product goals
- Statement-first expense reporting
- Receipt matching with auditability
- Human review queue for uncertain matches
- Consistent Excel/PDF report generation
- Multi-user support for coworkers
- Repeatable processing across statement periods and cardholders

## Recommended product shape
- **Backend:** FastAPI
- **Database:** PostgreSQL
- **Worker queue:** Celery or Dramatiq
- **File storage:** local/S3-compatible blob storage
- **Frontend:** React / Next.js internal web app
- **Report rendering:** Python + openpyxl with controlled templates

## Core product modules
1. Statement ingestion
2. Receipt ingestion
3. OCR + normalization
4. Matching engine
5. Review UI
6. Policy engine (business/personal, buckets, exceptions)
7. Report generator
8. Audit log + exports

## Repo layout
- `backend/` API + domain logic
- `frontend/` internal review/reporting UI
- `docs/` architecture and roadmap
- `samples/` example input/output files
- `scripts/` migration/import utilities from the current project

## Immediate next build target
Build a usable internal MVP that can:
- upload a statement Excel/PDF
- upload receipts
- auto-match by date + local amount
- show review-needed rows
- generate final workbook + annotated receipt PDF

## Current source material
The legacy scripts and outputs remain in `Expense/` and can be migrated incrementally.

## Current implementation status
The backend now has a local-first foundation for the private-server version:

- SQLite/SQLModel database setup
- local file storage under `backend/data/` by default
- Telegram webhook skeleton for receipt photos/PDFs
- coworker/user capture from Telegram sender metadata
- receipt records, deterministic receipt-field extraction, and targeted clarification questions
- clarification answer flow for date, amount, merchant, business/personal, business reason, and attendees
- report review session rows with confirmed snapshot gating before package generation
- Diners Excel statement import into canonical statement transactions
- list APIs for receipts, statements, transactions, review questions, and reports

## Backend quick start
From `expense-reporting-app/backend`:

```powershell
copy .env.example .env
python -m pip install -e .
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8080
```

Useful endpoints:

```text
GET  /health
GET  /telegram/status
POST /telegram/webhook
POST /receipts/upload
POST /receipts/{receipt_id}/extract
POST /imports/legacy-receipts
GET  /reviews/questions
GET  /reviews/summary
POST /reviews/questions/{question_id}/answer
GET  /reviews/report/{statement_import_id}
PATCH /reviews/report/rows/{row_id}
POST /reviews/report/{review_session_id}/confirm
POST /statements/import-excel
GET  /statements/{statement_id}/transactions
POST /matching/run
GET  /matching/decisions
GET  /reports/validate/{statement_import_id}
POST /reports/generate
```

## Telegram setup notes
Set these in `.env` on the private server:

```text
TELEGRAM_BOT_TOKEN=...
TELEGRAM_WEBHOOK_SECRET=...
ALLOWED_TELEGRAM_USER_IDS=123456789,987654321
```

The current bot flow is documented in `docs/TELEGRAM_BOT_FLOW.md`.

## Legacy migration utility
To load the current known mapped image receipts from the parent `Expense/` folder:

```powershell
python scripts\import_current_matches.py
```

This imports `Authoritative_Receipt_Mapping_Table_Combined_Images.csv` into `ReceiptDocument` rows and links storage paths to `03_11_Receipts\Receipts` when files exist.
