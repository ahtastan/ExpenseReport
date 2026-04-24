import mimetypes
import os

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlmodel import Session, select

from app.db import get_session
from app.models import ReceiptDocument
from app.schemas import ReceiptExtractionRead, ReceiptRead, ReceiptUpdate
from app.services.clarifications import ensure_receipt_review_questions
from app.services.receipt_extraction import apply_receipt_extraction
from app.services.storage import save_upload_file

router = APIRouter()


@router.get("/", response_model=list[ReceiptRead])
def list_receipts(
    needs_clarification: bool | None = None,
    session: Session = Depends(get_session),
):
    query = select(ReceiptDocument).order_by(ReceiptDocument.created_at.desc())
    if needs_clarification is not None:
        query = query.where(ReceiptDocument.needs_clarification == needs_clarification)
    return session.exec(query).all()


@router.get("/{receipt_id}", response_model=ReceiptRead)
def get_receipt(receipt_id: int, session: Session = Depends(get_session)):
    receipt = session.get(ReceiptDocument, receipt_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return receipt


@router.get("/{receipt_id}/file")
def get_receipt_file(receipt_id: int, session: Session = Depends(get_session)):
    receipt = session.get(ReceiptDocument, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="receipt not found")
    if not receipt.storage_path:
        raise HTTPException(status_code=404, detail="receipt has no attached file")
    if not os.path.isfile(receipt.storage_path):
        raise HTTPException(status_code=404, detail="attached file missing on disk")
    media_type = receipt.mime_type
    if not media_type:
        guessed, _ = mimetypes.guess_type(receipt.original_file_name or receipt.storage_path or "")
        media_type = guessed or "application/octet-stream"
    filename = receipt.original_file_name or f"receipt_{receipt_id}"
    return FileResponse(
        path=receipt.storage_path,
        media_type=media_type,
        filename=filename,
        content_disposition_type="inline",
    )


@router.patch("/{receipt_id}", response_model=ReceiptRead)
def update_receipt(
    receipt_id: int,
    payload: ReceiptUpdate,
    session: Session = Depends(get_session),
):
    receipt = session.get(ReceiptDocument, receipt_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(receipt, field, value)
    session.add(receipt)
    session.commit()
    session.refresh(receipt)
    return receipt


@router.post("/{receipt_id}/extract", response_model=ReceiptExtractionRead)
def extract_receipt(receipt_id: int, session: Session = Depends(get_session)):
    receipt = session.get(ReceiptDocument, receipt_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")
    result = apply_receipt_extraction(session, receipt)
    ensure_receipt_review_questions(session, receipt, receipt.uploader_user_id)
    return ReceiptExtractionRead(**result.__dict__)


@router.post("/upload", response_model=ReceiptRead)
async def upload_receipt(
    file: UploadFile = File(...),
    caption: str | None = None,
    session: Session = Depends(get_session),
):
    stored_path = await save_upload_file(file, "receipts")
    receipt = ReceiptDocument(
        source="api",
        status="received",
        content_type="document" if file.content_type == "application/pdf" else "photo",
        original_file_name=file.filename,
        mime_type=file.content_type,
        storage_path=str(stored_path),
        caption=caption,
    )
    session.add(receipt)
    session.commit()
    session.refresh(receipt)
    apply_receipt_extraction(session, receipt)
    ensure_receipt_review_questions(session, receipt, None)
    return receipt
