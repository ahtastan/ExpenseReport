from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.db import get_session
from app.models import ReportRun
from app.schemas import ReportGenerateRequest, ReportRunRead, ReportValidationIssue, ReportValidationResult
from app.services.report_generator import generate_report_package
from app.services.report_validation import validate_report_readiness

router = APIRouter()


@router.get('/')
def list_reports(session: Session = Depends(get_session)):
    return {"items": session.exec(select(ReportRun).order_by(ReportRun.created_at.desc())).all()}


@router.get("/validate/{statement_import_id}", response_model=ReportValidationResult)
def validate_report(statement_import_id: int, session: Session = Depends(get_session)):
    result = validate_report_readiness(session, statement_import_id)
    return ReportValidationResult(
        statement_import_id=result.statement_import_id,
        ready=result.ready,
        issue_count=result.issue_count,
        warning_count=result.warning_count,
        included_transactions=result.included_transactions,
        approved_matches=result.approved_matches,
        business_receipts=result.business_receipts,
        personal_receipts=result.personal_receipts,
        issues=[ReportValidationIssue(**issue.__dict__) for issue in result.issues],
    )


@router.post("/generate", response_model=ReportRunRead)
def generate_report(payload: ReportGenerateRequest, session: Session = Depends(get_session)):
    try:
        return generate_report_package(
            session=session,
            statement_import_id=payload.statement_import_id,
            employee_name=payload.employee_name,
            title_prefix=payload.title_prefix,
            allow_warnings=payload.allow_warnings,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
