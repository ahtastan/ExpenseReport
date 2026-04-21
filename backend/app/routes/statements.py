from fastapi import APIRouter, Depends, File, UploadFile
from sqlmodel import Session, select

from app.db import get_session
from app.models import StatementImport, StatementTransaction
from app.schemas import StatementImportRead, StatementTransactionRead
from app.services.statement_import import import_diners_excel
from app.services.storage import save_upload_file

router = APIRouter()


@router.get("/", response_model=list[StatementImportRead])
def list_statements(session: Session = Depends(get_session)):
    return session.exec(select(StatementImport).order_by(StatementImport.created_at.desc())).all()


@router.post("/import-excel", response_model=StatementImportRead)
async def import_statement_excel(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    stored_path = await save_upload_file(file, "statements")
    return import_diners_excel(session, stored_path, file.filename or stored_path.name)


@router.get("/{statement_id}/transactions", response_model=list[StatementTransactionRead])
def list_statement_transactions(statement_id: int, session: Session = Depends(get_session)):
    return session.exec(
        select(StatementTransaction)
        .where(StatementTransaction.statement_import_id == statement_id)
        .order_by(StatementTransaction.transaction_date, StatementTransaction.id)
    ).all()
