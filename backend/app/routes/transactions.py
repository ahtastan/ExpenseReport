from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from app.db import get_session
from app.models import StatementTransaction
from app.schemas import StatementTransactionRead

router = APIRouter()


@router.get("/", response_model=list[StatementTransactionRead])
def list_transactions(
    statement_import_id: int | None = None,
    session: Session = Depends(get_session),
):
    query = select(StatementTransaction).order_by(StatementTransaction.transaction_date, StatementTransaction.id)
    if statement_import_id is not None:
        query = query.where(StatementTransaction.statement_import_id == statement_import_id)
    return session.exec(query).all()
