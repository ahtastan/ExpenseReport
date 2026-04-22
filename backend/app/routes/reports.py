from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session, select

from app.config import get_settings
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
