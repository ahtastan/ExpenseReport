from datetime import date

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlmodel import Session, select

from app.db import get_session
from app.models import AppUser, MatchDecision, ReceiptDocument, ReviewSession, StatementImport, StatementTransaction
from app.schemas import (
    ManualStatementCreate,
    ManualStatementCreateResult,
    ManualStatementDraft,
    StatementImportRead,
    StatementTransactionRead,
)
from app.services.clarifications import ensure_receipt_review_questions
from app.services.receipt_extraction import apply_receipt_extraction
from app.services.review_sessions import (
    _resolve_statement_to_expense_report,
    get_or_create_review_session,
    session_payload,
)
from app.services.statement_import import _normalize_supplier, import_diners_excel
from app.services.storage import save_upload_file

router = APIRouter()


def get_or_create_demo_user(session: Session) -> AppUser:
    user = session.exec(select(AppUser).where(AppUser.username == "demo")).first()
    if user:
        return user
    user = AppUser(username="demo", display_name="Demo User")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _browser_demo_user_id(session: Session) -> int:
    user = get_or_create_demo_user(session)
    if user.id is None:
        raise HTTPException(status_code=500, detail="Demo user could not be initialized")
    return user.id


def _latest_review(session: Session, statement_import_id: int) -> ReviewSession | None:
    return session.exec(
        select(ReviewSession)
        .where(ReviewSession.statement_import_id == statement_import_id)
        .order_by(ReviewSession.created_at.desc())
    ).first()


def _statement_for_manual_entry(
    session: Session, statement_import_id: int | None, owner_user_id: int
) -> StatementImport:
    if statement_import_id is not None:
        statement = session.get(StatementImport, statement_import_id)
        if not statement:
            raise HTTPException(status_code=404, detail="Statement import not found")
        if statement.uploader_user_id is None:
            statement.uploader_user_id = owner_user_id
            session.add(statement)
            session.commit()
            session.refresh(statement)
        return statement

    statement = StatementImport(
        uploader_user_id=owner_user_id,
        source_filename="manual_statement_entries",
        storage_path=None,
        row_count=0,
    )
    session.add(statement)
    session.commit()
    session.refresh(statement)
    return statement


def _update_statement_summary(statement: StatementImport, transaction_date: date | None) -> None:
    statement.row_count += 1
    if transaction_date:
        statement.period_start = transaction_date if statement.period_start is None else min(statement.period_start, transaction_date)
        statement.period_end = transaction_date if statement.period_end is None else max(statement.period_end, transaction_date)


def _resolve_manual_entry_owner(statement: StatementImport) -> int:
    if statement.uploader_user_id is None:
        raise HTTPException(
            status_code=422,
            detail="Statement has no uploader; cannot resolve expense report owner",
        )
    return statement.uploader_user_id


def _draft_review_for_manual_entry(session: Session, statement: StatementImport) -> ReviewSession:
    owner_user_id = _resolve_manual_entry_owner(statement)
    expense_report_id = _resolve_statement_to_expense_report(
        session, statement.id or 0, owner_user_id=owner_user_id
    )
    latest = _latest_review(session, statement.id or 0)
    if latest and latest.status == "confirmed":
        review = ReviewSession(
            statement_import_id=statement.id or 0,
            expense_report_id=expense_report_id,
            status="draft",
        )
        session.add(review)
        session.commit()
        session.refresh(review)
        return review
    return get_or_create_review_session(session, expense_report_id=expense_report_id)


@router.get("/", response_model=list[StatementImportRead])
def list_statements(session: Session = Depends(get_session)):
    return session.exec(select(StatementImport).order_by(StatementImport.created_at.desc(), StatementImport.id.desc())).all()


@router.get("/latest", response_model=StatementImportRead)
def latest_statement(session: Session = Depends(get_session)):
    statement = session.exec(select(StatementImport).order_by(StatementImport.created_at.desc(), StatementImport.id.desc())).first()
    if not statement:
        raise HTTPException(status_code=404, detail="No statement imports found")
    return statement


@router.post("/import-excel", response_model=StatementImportRead)
async def import_statement_excel(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    stored_path = await save_upload_file(file, "statements")
    owner_user_id = _browser_demo_user_id(session)
    try:
        return import_diners_excel(
            session,
            stored_path,
            file.filename or stored_path.name,
            uploader_user_id=owner_user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/manual/receipt", response_model=ManualStatementDraft)
async def upload_manual_statement_receipt(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    stored_path = await save_upload_file(file, "receipts")
    content_type = file.content_type or ""
    receipt = ReceiptDocument(
        source="review_ui",
        status="received",
        content_type="document" if content_type == "application/pdf" else "photo",
        original_file_name=file.filename,
        mime_type=content_type or None,
        storage_path=str(stored_path),
    )
    session.add(receipt)
    session.commit()
    session.refresh(receipt)
    result = apply_receipt_extraction(session, receipt)
    ensure_receipt_review_questions(session, receipt, None)
    return ManualStatementDraft(**result.__dict__)


@router.post("/manual/transactions", response_model=ManualStatementCreateResult)
def create_manual_statement_transaction(
    payload: ManualStatementCreate,
    session: Session = Depends(get_session),
):
    supplier = payload.supplier.strip()
    currency = payload.currency.strip().upper() or "TRY"
    if not supplier:
        raise HTTPException(status_code=400, detail="Supplier is required")
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than zero")

    owner_user_id = _browser_demo_user_id(session)
    statement = _statement_for_manual_entry(session, payload.statement_import_id, owner_user_id)
    receipt = session.get(ReceiptDocument, payload.receipt_id) if payload.receipt_id else None
    if payload.receipt_id and not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")
    if receipt and receipt.uploader_user_id is None:
        receipt.uploader_user_id = owner_user_id
        session.add(receipt)
    if receipt and payload.business_reason is not None:
        receipt.business_reason = payload.business_reason.strip() or None
        # F-AI-Stage1 sub-PR 5: source-tag manual statement entry. The web
        # operator typed this reason directly, so the source is ``user``.
        if receipt.business_reason is not None:
            receipt.business_reason_source = "user"
        session.add(receipt)

    transaction = StatementTransaction(
        statement_import_id=statement.id or 0,
        transaction_date=payload.transaction_date,
        supplier_raw=supplier,
        supplier_normalized=_normalize_supplier(supplier),
        local_currency=currency,
        local_amount=payload.amount,
        source_row_ref=f"manual receipt {receipt.id}" if receipt and receipt.id else "manual entry",
        source_kind="manual",
    )
    session.add(transaction)
    _update_statement_summary(statement, payload.transaction_date)
    session.add(statement)
    session.commit()
    session.refresh(transaction)
    session.refresh(statement)

    if receipt and receipt.id is not None and transaction.id is not None:
        decision = MatchDecision(
            statement_transaction_id=transaction.id,
            receipt_document_id=receipt.id,
            confidence="high",
            match_method="manual_statement_entry",
            approved=True,
            rejected=False,
            reason="Operator created this statement transaction from the uploaded receipt.",
        )
        session.add(decision)
        session.commit()

    review = _draft_review_for_manual_entry(session, statement)
    review = get_or_create_review_session(
        session, expense_report_id=review.expense_report_id
    )
    return ManualStatementCreateResult(
        transaction=transaction,
        review_session=session_payload(session, review),
    )


@router.get("/{statement_id}/transactions", response_model=list[StatementTransactionRead])
def list_statement_transactions(statement_id: int, session: Session = Depends(get_session)):
    return session.exec(
        select(StatementTransaction)
        .where(StatementTransaction.statement_import_id == statement_id)
        .order_by(StatementTransaction.transaction_date, StatementTransaction.id)
    ).all()
