from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlmodel import Session, select

from app.db import get_session
from app.models import ReceiptDocument
from app.schemas import ReceiptRead, ReceiptUpdate
from app.services.clarifications import ensure_initial_receipt_question
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
    ensure_initial_receipt_question(session, receipt, None)
    return receipt
