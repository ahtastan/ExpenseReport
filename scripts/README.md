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
