from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.db import get_session
from app.models import ClarificationQuestion, MatchDecision, ReceiptDocument, StatementImport, StatementTransaction
from app.schemas import ClarificationAnswer, ClarificationQuestionRead, ReviewSummary
from app.services.clarifications import answer_question

router = APIRouter()


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
