# Vision OCR Extraction Handoff

## Objective Of This Step
Add Claude vision-based OCR extraction to `receipt_extraction.py` so the system can read field values from actual receipt image pixels, not just captions/filenames.

## Files Changed
- `backend/app/services/receipt_extraction.py` — added `_vision_extract()` and wired it into `extract_receipt_fields()`
- `backend/pyproject.toml` — added `anthropic>=0.40` dependency
- `docs/current_progress.md` — updated status

## Exact Behavior Added

### `_vision_extract(storage_path: str) -> dict | None`
- Returns `None` immediately if `ANTHROPIC_API_KEY` env var is absent.
- Returns `None` if the storage file does not exist or is not a supported image type (`.jpg`, `.jpeg`, `.png`, `.webp`, `.gif`).
- Reads the image, base64-encodes it, calls `claude-opus-4-7` with a structured JSON-only prompt.
- Returns a dict with keys: `date`, `supplier`, `amount`, `currency`, `business_or_personal`.
- Returns `None` on any exception (API error, JSON parse error, network failure) — no exception propagates.
- PDF OCR is **not** handled here — left for a future step.

### `extract_receipt_fields()` integration
Vision results take priority over deterministic parsing, but only for fields not already set on the receipt:
- `date`: vision → deterministic regex
- `amount`/`currency`: vision → deterministic regex
- `supplier`: receipt DB field → vision → deterministic merchant parse
- `business_or_personal`: receipt DB field → vision → keyword heuristic
- A `"Vision extraction succeeded."` note is appended when vision returns results.

## Tests Run And Results
- `python -m compileall backend\app` — passed, no errors.
- Synthetic extraction smoke (no `ANTHROPIC_API_KEY` set, vision skipped):
  - Input: filename `migros_2026-03-11_419.58TRY.jpg`, caption `Business merchant: Migros total 419.58 TRY customer dinner`
  - Result: status=extracted, date=2026-03-11, supplier=Migros, amount=419.58, currency=TRY, bp=Business, confidence=1.0
  - Clarification questions: business_reason, attendees — answered, needs_clarification=False
  - Passed.

## Open Assumptions
- `anthropic` package must be installed separately (`pip install anthropic>=0.40`); it is not yet present in the shared runtime.
- Vision call is on-demand per receipt (single call), not batch — cost is controlled.
- PDF receipt OCR not implemented; `.pdf` storage files are skipped by `_vision_extract`.
- `ocr_confidence` still reflects field-completeness ratio (0–1), not the model's internal confidence.

## Next Recommended Step
1. `pip install anthropic>=0.40` in the backend venv.
2. Set `ANTHROPIC_API_KEY` in `.env` or environment.
3. Run a real-image smoke test on one receipt from `03_11_Receipts/Receipts/`:
   ```python
   from app.services.receipt_extraction import _vision_extract
   result = _vision_extract("path/to/OM_1776120585936.jpeg")
   print(result)
   ```
4. If vision fields parse correctly, run the full real-data regression to confirm no regression in report generation.

## Commands To Rerun
From `expense-reporting-app` (deterministic regression, no API key needed):

```bash
PY='C:/Users/CASPER/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
PYTHONDONTWRITEBYTECODE=1 DATABASE_URL='sqlite:///:memory:' EXPENSE_STORAGE_ROOT="$(pwd)/backend/.verify_data" \
"$PY" -c "
from sqlmodel import Session, select
from app.db import create_db_and_tables, engine
from app.models import ClarificationQuestion, ReceiptDocument
from app.services.clarifications import answer_question, ensure_receipt_review_questions
from app.services.receipt_extraction import apply_receipt_extraction
create_db_and_tables()
session = Session(engine)
receipt = ReceiptDocument(source='test', status='received', content_type='photo', original_file_name='migros_2026-03-11_419.58TRY.jpg', caption='Business merchant: Migros total 419.58 TRY customer dinner')
session.add(receipt); session.commit(); session.refresh(receipt)
result = apply_receipt_extraction(session, receipt)
print(f'status={result.status}')
print(f'date={receipt.extracted_date}')
print(f'amount={receipt.extracted_local_amount}')
print(f'confidence={receipt.ocr_confidence}')
session.close()
"
```
