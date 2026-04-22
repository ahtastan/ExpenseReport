# Review Confirmation Handoff

## Objective Of This Step
Introduce a mandatory human-confirmation review stage before report generation, with a frozen confirmed snapshot used by final Excel/PDF package generation.

## Files Changed
- `backend/app/models.py`
- `backend/app/schemas.py`
- `backend/app/routes/reviews.py`
- `backend/app/services/review_sessions.py`
- `backend/app/services/report_generator.py`
- `backend/app/services/receipt_annotations.py`
- `backend/tests/test_review_confirmation.py`
- `frontend/review-table.html`
- `frontend/README.md`
- `README.md`
- `docs/current_progress.md`
- `docs/REVIEW_CONFIRMATION_HANDOFF.md`

## Exact Behavior Changed
- Added `ReviewSession` and `ReviewRow` tables.
- Added review APIs:
  - `GET /reviews/report/{statement_import_id}`
  - `POST /reviews/report/{statement_import_id}/build`
  - `PATCH /reviews/report/rows/{row_id}`
  - `POST /reviews/report/{review_session_id}/confirm`
- Review rows include provenance:
  - statement source fields;
  - receipt extracted fields;
  - match confidence/method/reason.
- Review rows maintain separate `source`, `suggested`, and `confirmed` payloads.
- Editing a review row updates the confirmed draft values and invalidates any prior confirmation.
- Confirming a review session freezes a JSON snapshot and SHA-256 hash.
- `generate_report_package()` now requires a confirmed review snapshot.
- Report generation now reads the confirmed snapshot instead of live mutable receipt/statement rows.
- Added `frontend/review-table.html`, a minimal static table for viewing, editing, flagging, and confirming review rows.
- Added explicit `JpegImagePlugin` import so annotated receipt PDF generation works in the bundled Pillow runtime.

## Tests Run And Results
- `python backend\tests\test_review_confirmation.py`
  - Result: passed.
  - Verifies:
    - report generation fails before confirmation;
    - review API/service payload exposes status and provenance fields;
    - confirmed snapshot is used instead of later live row edits;
    - editing after confirmation invalidates report generation until reconfirmed.
- `python -m compileall backend\app`
  - Result: passed.
- Real-data regression with confirmation:
  - Imported statement and legacy receipt mapping.
  - Ran matching, validation, review build, review confirmation, and package generation.
  - Result: passed.

## Real-Data Verification Status
- Statement transactions: 91.
- Legacy receipt rows: 60.
- Match candidates: 78.
- Auto-approved matches: 58.
- Validation: ready=True, errors=0, warnings=21.
- Review rows created: 58.
- Review session status: confirmed.
- Snapshot hash: present.
- Package generated: yes.
- Annotated PDF generated: yes, 7 pages.
- ZIP entries: `annotated_receipts.pdf`, `expense_report_part_1.xlsx`, `expense_report_part_2.xlsx`, `validation_summary.txt`.

## Open Assumptions
- Review rows currently seed from approved matches because that is the existing report-ready candidate set.
- The minimal web UI is a static HTML file and is not yet served by FastAPI or a frontend dev server.
- No auth/permissions are enforced yet; `confirmed_by_label` is a caller-provided audit label.
- The snapshot is effectively frozen by storing JSON/hash and invalidating on row edits; no database-level immutability constraints are added yet.
- Vision/OCR model separation is still not enforced in configuration.

## Next Recommended Step
Serve or scaffold the minimal review table so it can be exercised against a local backend, then add a small API/UI smoke workflow for loading, editing, confirming, and generating after confirmation.

## Commands To Rerun
From `expense-reporting-app`:

```powershell
$py='C:\Users\CASPER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:PYTHONDONTWRITEBYTECODE='1'
& $py backend\tests\test_review_confirmation.py
& $py -m compileall backend\app
```

Real-data regression with confirmation:

```powershell
$py='C:\Users\CASPER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:PYTHONDONTWRITEBYTECODE='1'
$env:DATABASE_URL='sqlite:///:memory:'
$env:EXPENSE_STORAGE_ROOT=(Resolve-Path 'backend').Path + '\.verify_data'
$env:EXPENSE_REPORT_TEMPLATE_PATH=(Resolve-Path '..\Expense Report Form_Blank.xlsx').Path
& $py -c "from pathlib import Path; from zipfile import ZipFile; from pypdf import PdfReader; from sqlmodel import Session; from app.db import create_db_and_tables, engine; from app.services.legacy_receipts import import_legacy_receipt_mapping; from app.services.matching import run_matching; from app.services.report_generator import generate_report_package; from app.services.report_validation import validate_report_readiness; from app.services.review_sessions import confirm_review_session, get_or_create_review_session, review_rows; from app.services.statement_import import import_diners_excel; root=Path.cwd().parent; create_db_and_tables(); session=Session(engine); statement=import_diners_excel(session, root/'Diners Club Statement.xlsx', source_filename='Diners Club Statement.xlsx'); import_legacy_receipt_mapping(session, csv_path=root/'Authoritative_Receipt_Mapping_Table_Combined_Images.csv', receipt_root=root/'03_11_Receipts'/'Receipts'); match_summary=run_matching(session, statement_import_id=statement.id, auto_approve_high_confidence=True); validation=validate_report_readiness(session, statement.id); review=get_or_create_review_session(session, statement.id); rows=review_rows(session, review.id); review=confirm_review_session(session, review.id, confirmed_by_label='real-data-smoke'); run=generate_report_package(session, statement.id, 'Ahmet Hakan Tastan', 'Diners Club Expense Report', True); package=Path(run.output_workbook_path or ''); pdf=Path(run.output_pdf_path or ''); print(f'transactions={statement.row_count}'); print(f'matching_candidates={match_summary.candidates_created}'); print(f'auto_approved={match_summary.auto_approved}'); print(f'validation_ready={validation.ready}'); print(f'validation_errors={validation.issue_count}'); print(f'validation_warnings={validation.warning_count}'); print(f'review_rows={len(rows)}'); print(f'review_status={review.status}'); print(f'snapshot_hash_exists={bool(review.snapshot_hash)}'); print(f'pdf_pages={len(PdfReader(str(pdf)).pages)}'); print('zip_entries=' + ','.join(sorted(ZipFile(package).namelist()))); session.close()"
```
