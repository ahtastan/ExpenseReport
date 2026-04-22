# Statement Import Handoff

## Objective Of This Step
Fix the Diners Excel importer so real uploaded statement files do not fail with a raw 500 when required headers are not on the first row or use small header-name variations.

## Files Changed
- `backend/app/services/statement_import.py`
- `backend/app/routes/statements.py`
- `backend/tests/test_statement_import.py`
- `docs/current_progress.md`
- `docs/STATEMENT_IMPORT_HANDOFF.md`

## Exact Behavior Changed
- The Diners importer now scans the first 30 worksheet rows to find a header row containing transaction date and supplier columns.
- Header detection now normalizes case, spacing, and punctuation.
- Header detection accepts current Diners variants such as `Tran Date`, `Transaction Date`, `Supplier`, `Supplier Name`, `Source Amount`, and spacing/case variations.
- Data import now starts after the detected header row instead of always after row 1.
- `POST /statements/import-excel` now converts importer `ValueError`s into `400` responses with the parser message instead of leaking a 500 traceback.
- Missing required headers now report a concise message such as `Could not find required statement columns: transaction date, supplier`.

## Runtime Evidence
- The exact user-uploaded file, `03_11_Receipts/Diners_Transactions.xlsx`, has `Tran Date`, `Supplier`, and `Source Amount` on row 1.
- Existing saved uploads under `backend/data/unassigned/statements` also show a Diners shape where metadata rows precede the same header row.
- The importer now covers both shapes.

## Tests Run And Results
- `python backend\tests\test_statement_import.py`
  - Result: passed with the bundled workspace Python.
  - Verifies:
    - header row can appear after leading metadata rows;
    - spacing/case/header-name variants import;
    - the real `03_11_Receipts/Diners_Transactions.xlsx` file imports;
    - missing required columns become a `400` `HTTPException`.

## What Is Verified
- The narrow Diners importer handles row-1 headers and metadata-before-header files.
- Required missing headers return a clean client-facing error from the route.
- The exact uploaded workbook path supplied by the user imports in the smoke test.

## What Remains Unverified
- Browser/API multipart upload against a live FastAPI server.
- All possible Diners export variants beyond the observed repo files.
- Ambiguous date interpretation in rows like `2026-11-03`, which was already present and is not changed in this step.
- PDF statement import, OCR/model routing, and frontend behavior remain out of scope.

## Next Recommended Step
Run `POST /statements/import-excel` against the real `03_11_Receipts/Diners_Transactions.xlsx` file through the live backend, then inspect `/statements/latest` and `/review` to confirm the imported statement appears in the operator flow.
