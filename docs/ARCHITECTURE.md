# Architecture

## 1. Source of truth
The app should be **statement-first**.

That means:
- statement rows are the canonical transaction ledger
- receipts enrich statement rows
- reports are generated from statement rows plus policy decisions
- receipts should not invent report rows that do not exist in the statement ledger

## 2. Core entities

### StatementTransaction
- cardholder
- statement period
- transaction date
- posting date
- supplier
- local currency
- local amount
- USD amount
- source file + row id

### ReceiptDocument
- file id
- uploader
- image/pdf path
- extracted merchant
- extracted date
- extracted local amount
- OCR confidence

### MatchCandidate
- statement transaction id
- receipt id
- score
- reasons
- status: auto_matched / review_needed / rejected / approved

### PolicyDecision
- transaction id
- business_or_personal
- report_bucket
- include_in_report
- justification
- decided_by

### ReportRun
- statement period
- cardholder
- policy snapshot
- generated workbook path
- generated pdf path
- run status

## 3. Processing pipeline
1. Import statement
2. Parse statement rows into normalized transactions
3. Import receipts
4. OCR receipts and normalize date/amount/merchant
5. Match receipts to statement rows
6. Surface uncertain rows in review queue
7. Apply policy classification
8. Generate report workbook and annotated receipt pack
9. Store outputs and audit log

## 4. Matching strategy
Primary keys:
- local amount equality
- transaction date equality or small proximity window when posting date differs

Secondary keys:
- supplier similarity
- merchant OCR similarity
- currency consistency

### Match confidence bands
- **High:** exact date + exact local amount
- **Medium:** date close + exact amount or exact date + merchant slightly off
- **Low:** fuzzy amount/date/merchant only

Low confidence should never silently reach a final report.

## 5. Manual-fix reduction strategy
The current process needed manual fixes because too much was implicit.

### Fix that by enforcing:
- one canonical statement table
- one canonical match table
- explicit review queue
- no PDF/report generation from ad hoc script state
- row-level audit reasons for every match and every report inclusion

## 6. Multi-user needs
For coworker use, add:
- login / role model
- per-cardholder access control
- per-report review ownership
- immutable audit history
- downloadable approval packages

## 7. Recommended deployment path
### MVP
- single internal server
- FastAPI + Postgres
- local/shared folder storage
- React frontend

### Scale-up
- object storage
- background workers
- OCR provider abstraction
- multi-tenant org/workspace model
- approval workflow + notifications
