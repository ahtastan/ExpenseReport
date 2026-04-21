# Implementation Brief, Phase A

## Goal
Build the first real product foundation for the expense reporting app.

This phase should create the canonical backend data model and the first import path for statement-ledger data.

## Product rules
1. Statement rows are the canonical ledger.
2. Receipts enrich statement rows, but do not create independent report rows.
3. Final reports must be generated only from statement-backed categorized rows.
4. Any uncertain match must stay reviewable and auditable.

## Phase A scope

### 1. Backend project structure
Inside `backend/app/`, implement:
- `db.py`
- `config.py`
- `models.py`
- `schemas.py`
- `crud/`
- `services/statement_import.py`
- `services/report_validation.py`
- `routes/statements.py`
- `routes/transactions.py`

### 2. Canonical data model
Define SQLModel models for:

#### StatementImport
- id
- source_filename
- statement_date
- period_start
- period_end
- cardholder_name
- company_name
- created_at

#### StatementTransaction
- id
- statement_import_id
- transaction_date
- posting_date
- supplier_raw
- supplier_normalized
- local_currency
- local_amount
- usd_amount
- source_row_ref
- source_kind (`excel` / `pdf`)
- created_at

#### ReceiptDocument
- id
- file_name
- storage_path
- mime_type
- extracted_date
- extracted_supplier
- extracted_local_amount
- ocr_confidence
- created_at

#### MatchDecision
- id
- statement_transaction_id
- receipt_document_id
- confidence (`high` / `medium` / `low`)
- match_method
- approved
- rejected
- reason
- created_at
- updated_at

#### PolicyDecision
- id
- statement_transaction_id
- business_or_personal
- report_bucket
- include_in_report
- justification
- decided_by
- created_at
- updated_at

#### ReportRun
- id
- statement_import_id
- template_name
- status
- output_workbook_path
- output_pdf_path
- created_at

### 3. Database
- Use SQLite for local MVP dev, but structure config so Postgres can replace it later.
- Add `create_db_and_tables()`.
- Use one database URL from config.

### 4. Statement Excel import service
Build service for `Diners Club Statement.xlsx`-style files.

Responsibilities:
- load workbook
- parse rows from first worksheet
- normalize:
  - transaction date
  - supplier
  - local TRY amount
  - USD amount
- create one `StatementImport`
- create many `StatementTransaction` rows
- return import summary counts

### 5. API endpoints
Implement:

#### `POST /statements/import-excel`
- accept uploaded xlsx
- import rows into DB
- return statement import id + row count

#### `GET /statements`
- list imported statements

#### `GET /statements/{statement_id}/transactions`
- list normalized transactions

### 6. Validation utilities
Create `report_validation.py` with checks for:
- duplicate statement rows
- missing USD values
- missing policy decisions
- transactions marked `include_in_report=True` without a business/personal decision

## Non-goals for Phase A
Do not build yet:
- receipt OCR pipeline
- matching engine
- final report generation UI
- authentication
- multi-user permissions

## Expected outcome
After Phase A, we should be able to:
1. run backend locally
2. import a Diners statement Excel
3. persist canonical statement rows
4. inspect transactions via API
5. use this as the base for matching + categorization in Phase B
