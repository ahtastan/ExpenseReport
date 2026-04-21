# Legacy Receipt Import Handoff

## Objective Of This Step
Load the current authoritative mapped receipt CSV into the new backend database as repeatable `ReceiptDocument` records.

## Files Changed
- `backend/app/main.py`
- `backend/app/routes/imports.py`
- `backend/app/services/legacy_receipts.py`
- `scripts/import_current_matches.py`
- `scripts/README.md`
- `README.md`
- `docs/current_progress.md`
- `docs/LEGACY_IMPORT_HANDOFF.md`

## Exact Behavior Changed
- Added `import_legacy_receipt_mapping(...)` service for CSV rows shaped like `Authoritative_Receipt_Mapping_Table_Combined_Images.csv`.
- Added `scripts/import_current_matches.py` CLI importer.
- Added `POST /imports/legacy-receipts` API importer.
- Imported rows become `ReceiptDocument` records with:
  - `source="legacy_mapping"`
  - original receipt file name
  - receipt file path if present under the supplied receipt root
  - extracted date, supplier, TRY amount, currency
  - business/personal classification
  - report bucket
  - manual review flag as `needs_clarification`
- Existing legacy imports update by `source + original_file_name` instead of duplicating.

## Tests Run And Results
- `python -m compileall backend/app scripts`: passed.
- In-memory real-data verification:
  - imported 91 statement transactions from `Diners Club Statement.xlsx`
  - imported 60 receipt rows from `Authoritative_Receipt_Mapping_Table_Combined_Images.csv`
  - found 60 existing receipt file paths
  - matching considered 60 receipts, skipped 0, created 78 candidates
  - high=60, medium=3, low=15, auto_approved=60

## Real-Data Verification Status
Verified with current real Diners statement and current authoritative mapped image receipt CSV.

## Open Assumptions
- Current migration target is image receipts only from `Authoritative_Receipt_Mapping_Table_Combined_Images.csv`.
- PDF receipts and orphan rows are intentionally not included in that CSV.
- The receipt root path remains `03_11_Receipts/Receipts` for the legacy dataset.

## Next Recommended Step
Build the report validation service that checks whether a report run is ready: statement imported, receipts matched, open questions resolved, business rows categorized, and no unapproved medium/low matches included.

## Commands To Rerun
From `expense-reporting-app`:

```powershell
python -m compileall backend/app scripts
python scripts\import_current_matches.py
```

Optional custom paths:

```powershell
python scripts\import_current_matches.py ..\Authoritative_Receipt_Mapping_Table_Combined_Images.csv ..\03_11_Receipts\Receipts
```
