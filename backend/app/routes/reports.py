from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session, select

from app.config import get_settings
from app.db import get_session
from app.models import ReportRun, StatementImport
from app.schemas import (
    ReportGenerateRequest,
    ReportRunListRead,
    ReportRunRead,
    ReportValidationIssue,
    ReportValidationResult,
)
from app.services.report_generator import generate_report_package
from app.services.report_validation import validate_report_readiness
from app.services.review_sessions import _resolve_statement_to_expense_report

router = APIRouter()


def _resolve_owner_for_statement(session: Session, statement_import_id: int) -> tuple[int, int]:
    """Return (expense_report_id, owner_user_id) for a statement-keyed URL.

    Raises 404 if the statement does not exist, 422 if it has no uploader.
    The uploader is required — no fallback — so report ownership is always
    grounded in a real user.
    """
    statement = session.get(StatementImport, statement_import_id)
    if statement is None:
        raise HTTPException(status_code=404, detail="Statement import not found")
    if statement.uploader_user_id is None:
        raise HTTPException(
            status_code=422,
            detail="Statement has no uploader; cannot resolve expense report owner",
        )
    expense_report_id = _resolve_statement_to_expense_report(
        session, statement_import_id, owner_user_id=statement.uploader_user_id
    )
    return expense_report_id, statement.uploader_user_id


@router.get('/', response_model=ReportRunListRead)
def list_reports(session: Session = Depends(get_session)):
    return {"items": session.exec(select(ReportRun).order_by(ReportRun.created_at.desc())).all()}


@router.get("/validate/{statement_import_id}", response_model=ReportValidationResult)
def validate_report(statement_import_id: int, session: Session = Depends(get_session)):
    expense_report_id, _ = _resolve_owner_for_statement(session, statement_import_id)
    try:
        result = validate_report_readiness(session, expense_report_id=expense_report_id)
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
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
    expense_report_id, _ = _resolve_owner_for_statement(session, payload.statement_import_id)
    try:
        return generate_report_package(
            session=session,
            expense_report_id=expense_report_id,
            employee_name=payload.employee_name,
            title_prefix=payload.title_prefix,
            allow_warnings=payload.allow_warnings,
        )
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{report_run_id}/download")
def download_report(report_run_id: int, session: Session = Depends(get_session)):
    run = session.get(ReportRun, report_run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Report run not found")
    if run.status != "completed" or not run.output_workbook_path:
        raise HTTPException(status_code=400, detail="Report run is not ready for download")

    output_path = Path(run.output_workbook_path).resolve()
    storage_root = get_settings().storage_root.resolve()
    if storage_root not in output_path.parents:
        raise HTTPException(status_code=400, detail="Report output path is outside storage root")
    if not output_path.exists() or not output_path.is_file():
        raise HTTPException(status_code=404, detail="Report output file not found")

    return FileResponse(
        output_path,
        filename=output_path.name,
        media_type="application/zip" if output_path.suffix.lower() == ".zip" else None,
    )
