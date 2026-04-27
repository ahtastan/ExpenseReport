# Scripts

Use this folder for migration/import utilities from the current `Expense/` script collection into the app data model.

Suggested first scripts:
- import_diners_excel.py
- import_receipt_inventory.py
- import_current_matches.py
- validate_report_run.py

## Available scripts

### import_current_matches.py
Imports the current authoritative mapped image receipts into the app database as `ReceiptDocument` rows.

From `expense-reporting-app`:

```powershell
python scripts\import_current_matches.py
```

Optional arguments:

```powershell
python scripts\import_current_matches.py <mapping_csv> <receipt_root>
```

### replay_month.py
Runs a local month replay from a receipt folder and Diners/BMO statement workbook. The script creates a replay SQLite DB under the requested output directory, never uses the production DB, and writes `replay_summary.csv` plus `replay_audit.json`.

```powershell
python scripts\replay_month.py `
  --receipts-dir "C:\path\to\receipts" `
  --statement-xlsx "C:\path\to\Statement.xlsx" `
  --expected-manifest "C:\path\to\expected_manifest.csv" `
  --output-dir "C:\path\to\replay_output"
```

`--expected-manifest` is optional. Add `--skip-report-generation` when you only want extraction, matching, and audit outputs.
