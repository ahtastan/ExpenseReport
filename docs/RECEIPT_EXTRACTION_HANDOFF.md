# Receipt Extraction Handoff

## Objective Of This Step
Add the first receipt extraction pipeline for Telegram/API receipts so structured matching fields are populated before clarification and statement matching.

## Files Changed
- `backend/app/services/receipt_extraction.py`
- `backend/app/services/clarifications.py`
- `backend/app/routes/receipts.py`
- `backend/app/services/telegram.py`
- `backend/app/schemas.py`
- `README.md`
- `docs/TELEGRAM_BOT_FLOW.md`
- `docs/current_progress.md`
- `docs/RECEIPT_EXTRACTION_HANDOFF.md`

## Exact Behavior Changed
- Added deterministic extraction from receipt caption, original filename, and stored filename.
- Extraction attempts to populate:
  - `extracted_date`
  - `extracted_supplier`
  - `extracted_local_amount`
  - `extracted_currency`
  - `business_or_personal`
  - `ocr_confidence`
- Added `POST /receipts/{receipt_id}/extract` to rerun extraction for a stored receipt.
- `POST /receipts/upload` now runs extraction before creating review questions.
- Telegram receipt capture now runs extraction before creating review questions.
- Telegram replies may include an "I read..." summary when extraction confidence is at least `0.6`.
- Clarification queue now asks only for missing fields/context: date, amount, supplier, business/personal, business reason, attendees.
- Clarification answers now write back to receipt date, amount/currency, supplier, business reason, attendees, and `needs_clarification`.

## Tests Run And Results
- `python -m compileall backend\app`
  - Result: passed.
- Synthetic in-memory extraction smoke:
  - Input filename: `migros_2026-03-11_419.58TRY.jpg`.
  - Input caption: `Business merchant: Migros total 419.58 TRY customer dinner`.
  - Parsed: date `2026-03-11`, supplier `Migros`, amount `419.58`, currency `TRY`, business/personal `Business`, confidence `1.0`.
  - Created questions: `business_reason`, `attendees`.
  - After answering both, `needs_clarification=False`.
  - Result: passed.
- Real-data report regression:
  - Imported statement and legacy receipt mapping.
  - Ran matching, validation, report package generation, and PDF page-count check.
  - Result: passed.

## Real-Data Verification Status
- No OCR/vision was run on receipt pixels in this step.
- Existing real-data report flow still uses authoritative legacy receipt fields.
- Regression check results:
  - statement transactions: 91
  - match candidates: 78
  - auto-approved matches: 58
  - validation: ready=True, errors=0, warnings=21
  - generated package: `expense_report_package.zip`
  - generated annotated PDF pages: 7
  - package entries: `annotated_receipts.pdf`, `expense_report_part_1.xlsx`, `expense_report_part_2.xlsx`, `validation_summary.txt`

## Open Assumptions
- Caption/filename extraction is an MVP bridge, not final OCR.
- Default currency is `TRY` when an amount exists without explicit currency.
- `ocr_confidence` currently means field-completeness confidence, not model confidence.
- PDF receipt OCR/rendering is not implemented.
- True OCR/vision should plug into `receipt_extraction.py` and reuse the same field application and clarification queue.

## Next Recommended Step
Implement true image OCR/vision extraction behind `receipt_extraction.py`, then verify it on real receipt images from `03_11_Receipts/Receipts`.

## Commands To Rerun
From `expense-reporting-app`:

```powershell
$py='C:\Users\CASPER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:PYTHONDONTWRITEBYTECODE='1'
$env:DATABASE_URL='sqlite:///:memory:'
$env:EXPENSE_STORAGE_ROOT=(Resolve-Path 'backend').Path + '\.verify_data'
& $py -c "from sqlmodel import Session, select; from app.db import create_db_and_tables, engine; from app.models import ClarificationQuestion, ReceiptDocument; from app.services.clarifications import answer_question, ensure_receipt_review_questions; from app.services.receipt_extraction import apply_receipt_extraction; create_db_and_tables(); session=Session(engine); receipt=ReceiptDocument(source='test', status='received', content_type='photo', original_file_name='migros_2026-03-11_419.58TRY.jpg', caption='Business merchant: Migros total 419.58 TRY customer dinner'); session.add(receipt); session.commit(); session.refresh(receipt); result=apply_receipt_extraction(session, receipt); questions=ensure_receipt_review_questions(session, receipt, None); print(f'status={result.status}'); print(f'date={receipt.extracted_date}'); print(f'supplier={receipt.extracted_supplier}'); print(f'amount={receipt.extracted_local_amount}'); print(f'currency={receipt.extracted_currency}'); print(f'bp={receipt.business_or_personal}'); print(f'confidence={receipt.ocr_confidence}'); print('questions=' + ','.join(q.question_key for q in questions)); q=session.exec(select(ClarificationQuestion).where(ClarificationQuestion.question_key == 'business_reason')).first(); answer_question(session, q, 'Kartonsan dinner'); q2=session.exec(select(ClarificationQuestion).where(ClarificationQuestion.question_key == 'attendees')).first(); answer_question(session, q2, 'Ahmet and customer team'); session.refresh(receipt); print(f'needs_clarification={receipt.needs_clarification}'); session.close()"
& $py -m compileall backend\app
```

Real-data report regression command from `expense-reporting-app`:

```powershell
$py='C:\Users\CASPER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:PYTHONDONTWRITEBYTECODE='1'
$env:DATABASE_URL='sqlite:///:memory:'
$env:EXPENSE_STORAGE_ROOT=(Resolve-Path 'backend').Path + '\.verify_data'
$env:EXPENSE_REPORT_TEMPLATE_PATH=(Resolve-Path '..\Expense Report Form_Blank.xlsx').Path
& $py -c "from pathlib import Path; from zipfile import ZipFile; from pypdf import PdfReader; from sqlmodel import Session; from app.db import create_db_and_tables, engine; from app.services.legacy_receipts import import_legacy_receipt_mapping; from app.services.matching import run_matching; from app.services.report_generator import generate_report_package; from app.services.report_validation import validate_report_readiness; from app.services.statement_import import import_diners_excel; root=Path.cwd().parent; create_db_and_tables(); session=Session(engine); statement=import_diners_excel(session, root/'Diners Club Statement.xlsx', source_filename='Diners Club Statement.xlsx'); import_legacy_receipt_mapping(session, csv_path=root/'Authoritative_Receipt_Mapping_Table_Combined_Images.csv', receipt_root=root/'03_11_Receipts'/'Receipts'); match_summary=run_matching(session, statement_import_id=statement.id, auto_approve_high_confidence=True); validation=validate_report_readiness(session, statement.id); run=generate_report_package(session, statement.id, 'Ahmet Hakan Tastan', 'Diners Club Expense Report', True); package=Path(run.output_workbook_path or ''); pdf=Path(run.output_pdf_path or ''); print(f'transactions={statement.row_count}'); print(f'matching_candidates={match_summary.candidates_created}'); print(f'auto_approved={match_summary.auto_approved}'); print(f'validation_ready={validation.ready}'); print(f'validation_errors={validation.issue_count}'); print(f'validation_warnings={validation.warning_count}'); print(f'pdf_pages={len(PdfReader(str(pdf)).pages)}'); print('zip_entries=' + ','.join(sorted(ZipFile(package).namelist()))); session.close()"
```
