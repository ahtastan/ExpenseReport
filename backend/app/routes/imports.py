from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel
from sqlmodel import Session

from app.db import get_session
from app.services.legacy_receipts import import_legacy_receipt_mapping
from app.services.storage import save_upload_file

router = APIRouter()


class LegacyReceiptImportResponse(BaseModel):
    source_path: str
    rows_read: int
    receipts_created: int
    receipts_updated: int
    rows_skipped: int


@router.post("/legacy-receipts", response_model=LegacyReceiptImportResponse)
async def import_legacy_receipts(
    file: UploadFile = File(...),
    receipt_root: str | None = Form(default=None),
    session: Session = Depends(get_session),
):
    stored_csv = await save_upload_file(file, "imports")
    summary = import_legacy_receipt_mapping(
        session,
        stored_csv,
        receipt_root=Path(receipt_root) if receipt_root else None,
    )
    return LegacyReceiptImportResponse(**summary.__dict__)
