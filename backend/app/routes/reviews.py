from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.db import get_session
from app.models import ClarificationQuestion, MatchDecision, ReceiptDocument, ReviewSession, StatementImport, StatementTransaction
from app.schemas import (
    ClarificationAnswer,
    ClarificationQuestionRead,
    ReviewBulkUpdateRequest,
    ReviewBulkUpdateResult,
    ReviewConfirmRequest,
    ReviewRowRead,
    ReviewRowUpdate,
    ReviewSessionRead,
    ReviewSummary,
)
from app.services.clarifications import answer_question
from app.services.review_sessions import (
    _resolve_statement_to_expense_report,
    bulk_update_review_rows,
    confirm_review_session,
    get_or_create_review_session,
    session_payload,
    update_review_row,
)

router = APIRouter()


def _expense_report_id_for_statement(session: Session, statement_import_id: int) -> int:
    statement = session.get(StatementImport, statement_import_id)
    if statement is None:
        raise HTTPException(status_code=404, detail="Statement import not found")
    if statement.uploader_user_id is None:
        raise HTTPException(
            status_code=422,
            detail="Statement has no uploader; cannot resolve expense report owner",
        )
    return _resolve_statement_to_expense_report(
        session, statement_import_id, owner_user_id=statement.uploader_user_id
    )


@router.get("/summary", response_model=ReviewSummary)
def review_summary(session: Session = Depends(get_session)):
    receipts = session.exec(select(ReceiptDocument)).all()
    statements = session.exec(select(StatementImport)).all()
    transactions = session.exec(select(StatementTransaction)).all()
    decisions = session.exec(select(MatchDecision)).all()
    questions = session.exec(select(ClarificationQuestion).where(ClarificationQuestion.status == "open")).all()
    return ReviewSummary(
        receipts_total=len(receipts),
        receipts_needing_clarification=sum(1 for receipt in receipts if receipt.needs_clarification),
        statements_total=len(statements),
        transactions_total=len(transactions),
        match_decisions_total=len(decisions),
        approved_matches=sum(1 for decision in decisions if decision.approved),
        rejected_matches=sum(1 for decision in decisions if decision.rejected),
        open_questions=len(questions),
    )


@router.get("/report/{statement_import_id}", response_model=ReviewSessionRead)
def get_report_review(statement_import_id: int, session: Session = Depends(get_session)):
    expense_report_id = _expense_report_id_for_statement(session, statement_import_id)
    review = get_or_create_review_session(session, expense_report_id=expense_report_id)
    return session_payload(session, review)


@router.post("/report/{statement_import_id}/build", response_model=ReviewSessionRead)
def build_report_review(statement_import_id: int, session: Session = Depends(get_session)):
    expense_report_id = _expense_report_id_for_statement(session, statement_import_id)
    review = get_or_create_review_session(session, expense_report_id=expense_report_id)
    return session_payload(session, review)


@router.patch("/report/rows/{row_id}", response_model=ReviewRowRead)
def edit_report_review_row(row_id: int, payload: ReviewRowUpdate, session: Session = Depends(get_session)):
    try:
        row = update_review_row(
            session,
            row_id=row_id,
            fields=payload.fields,
            attention_required=payload.attention_required,
            attention_note=payload.attention_note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    review = session.get(ReviewSession, row.review_session_id)
    if not review:
        raise HTTPException(status_code=404, detail="Review session not found")
    for item in session_payload(session, review)["rows"]:
        if item["id"] == row.id:
            return item
    raise HTTPException(status_code=404, detail="Review row not found")


@router.post("/report/{review_session_id}/bulk-update", response_model=ReviewBulkUpdateResult)
def bulk_edit_report_review_rows(
    review_session_id: int,
    payload: ReviewBulkUpdateRequest,
    session: Session = Depends(get_session),
):
    try:
        return bulk_update_review_rows(
            session,
            review_session_id=review_session_id,
            fields=payload.fields,
            scope=payload.scope,
            row_ids=payload.row_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/report/{review_session_id}/confirm", response_model=ReviewSessionRead)
def confirm_report_review(
    review_session_id: int,
    payload: ReviewConfirmRequest,
    session: Session = Depends(get_session),
):
    try:
        review = confirm_review_session(
            session,
            review_session_id=review_session_id,
            confirmed_by_user_id=payload.confirmed_by_user_id,
            confirmed_by_label=payload.confirmed_by_label,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return session_payload(session, review)


@router.get("/questions", response_model=list[ClarificationQuestionRead])
def list_questions(
    status: str = "open",
    session: Session = Depends(get_session),
):
    return session.exec(
        select(ClarificationQuestion)
        .where(ClarificationQuestion.status == status)
        .order_by(ClarificationQuestion.created_at)
    ).all()


@router.post("/questions/{question_id}/answer", response_model=list[ClarificationQuestionRead])
def answer_clarification(
    question_id: int,
    payload: ClarificationAnswer,
    session: Session = Depends(get_session),
):
    question = session.get(ClarificationQuestion, question_id)
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")
    created = answer_question(session, question, payload.answer_text)
    return created
