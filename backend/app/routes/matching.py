from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.db import get_session
from app.models import MatchDecision
from app.schemas import MatchDecisionRead, MatchRunRequest, MatchRunSummary
from app.services.matching import run_matching

router = APIRouter()


@router.post("/run", response_model=MatchRunSummary)
def run_matcher(
    payload: MatchRunRequest,
    session: Session = Depends(get_session),
):
    stats = run_matching(
        session,
        statement_import_id=payload.statement_import_id,
        receipt_id=payload.receipt_id,
        auto_approve_high_confidence=payload.auto_approve_high_confidence,
    )
    return MatchRunSummary(**stats.__dict__)


@router.get("/decisions", response_model=list[MatchDecisionRead])
def list_match_decisions(
    receipt_id: int | None = None,
    transaction_id: int | None = None,
    approved: bool | None = None,
    confidence: str | None = None,
    session: Session = Depends(get_session),
):
    query = select(MatchDecision).order_by(MatchDecision.created_at.desc())
    if receipt_id is not None:
        query = query.where(MatchDecision.receipt_document_id == receipt_id)
    if transaction_id is not None:
        query = query.where(MatchDecision.statement_transaction_id == transaction_id)
    if approved is not None:
        query = query.where(MatchDecision.approved == approved)
    if confidence is not None:
        query = query.where(MatchDecision.confidence == confidence)
    return session.exec(query).all()


@router.post("/decisions/{decision_id}/approve", response_model=MatchDecisionRead)
def approve_match_decision(decision_id: int, session: Session = Depends(get_session)):
    decision = session.get(MatchDecision, decision_id)
    if not decision:
        raise HTTPException(status_code=404, detail="Match decision not found")
    decision.approved = True
    decision.rejected = False
    session.add(decision)
    session.commit()
    session.refresh(decision)
    return decision


@router.post("/decisions/{decision_id}/reject", response_model=MatchDecisionRead)
def reject_match_decision(decision_id: int, session: Session = Depends(get_session)):
    decision = session.get(MatchDecision, decision_id)
    if not decision:
        raise HTTPException(status_code=404, detail="Match decision not found")
    decision.approved = False
    decision.rejected = True
    session.add(decision)
    session.commit()
    session.refresh(decision)
    return decision
