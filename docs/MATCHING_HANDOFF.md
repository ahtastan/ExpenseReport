# Matching Handoff

## Objective Of This Step
Connect captured receipts to imported Diners statement transactions with auditable match decisions.

## Files Changed
- `backend/app/main.py`
- `backend/app/schemas.py`
- `backend/app/routes/receipts.py`
- `backend/app/routes/reviews.py`
- `backend/app/routes/matching.py`
- `backend/app/services/matching.py`
- `docs/current_progress.md`
- `docs/MATCHING_HANDOFF.md`

## Behavior Changed
- `PATCH /receipts/{receipt_id}` can now store extracted/manual receipt metadata needed for matching.
- `POST /matching/run` creates or updates match decisions from receipt fields and statement transactions.
- Match scoring uses:
  - local amount exact/near/loose match
  - same or nearby transaction date
  - merchant similarity
- Exact or near-exact local amount plus same transaction date is high confidence even if merchant OCR text is weak.
- Unique high-confidence matches can be auto-approved.
- `GET /matching/decisions` lists candidates/decisions.
- `POST /matching/decisions/{decision_id}/approve` approves a match.
- `POST /matching/decisions/{decision_id}/reject` rejects a match.
- `GET /reviews/summary` returns compact counts for receipts, statements, transactions, matches, and open questions.

## Tests Run And Results
- `python -m compileall backend/app`: passed.
- In-memory real-data verification imported 91 transactions from real `Diners Club Statement.xlsx`.
- Seeded 60 known mapped image receipts from `Authoritative_Receipt_Mapping_Table_Combined_Images.csv`.
- Matching run considered 60 receipts, skipped 0, created 78 candidates, marked 60 high confidence, 3 medium confidence, 15 low confidence, and auto-approved 60 unique high-confidence matches.

## Real-Data Verification Status
- Statement import is verified with real data.
- Matching implementation is verified structurally with existing mapped receipt CSV rows.
- OCR is not available yet, so real receipt photos are not automatically extracted.

## Open Assumptions
- Statement local currency is TRY for current Diners workflow.
- Receipts must have `extracted_date` and `extracted_local_amount` before matching.
- Merchant text is helpful but not required for amount/date matching.
- Auto-approval is acceptable only for a unique high-confidence candidate.

## Next Recommended Step
Create a migration/import utility for `Authoritative_Receipt_Mapping_Table_Combined_Images.csv` so the known 60 mapped image receipts can be loaded as `ReceiptDocument` rows and used as a regression set for matching.

## Commands To Rerun
From `expense-reporting-app/backend`:

```powershell
python -m compileall app
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8080
```

In-memory statement import smoke test:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:DATABASE_URL='sqlite:///:memory:'
python -c "from pathlib import Path; from sqlmodel import Session, select; from app.db import create_db_and_tables, engine; from app.models import StatementTransaction; from app.services.statement_import import import_diners_excel; create_db_and_tables(); p=Path(r'..\..\Diners Club Statement.xlsx'); s=Session(engine); imp=import_diners_excel(s, p, p.name); print(imp.row_count, len(s.exec(select(StatementTransaction)).all()))"
```
