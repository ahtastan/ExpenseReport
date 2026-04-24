from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from app.db import get_session
from app.models import AppUser, ExpenseReport, ReceiptDocument, ReportRun, ReviewSession
from app.schemas import (
    ReceiptAttachResponse,
    ReceiptDetachResponse,
    ReportCreate,
    ReportDetail,
    ReportRead,
)

router = APIRouter()


def _require_report_for_owner(
    session: Session, report_id: int, owner_user_id: int
) -> ExpenseReport:
    report = session.get(ExpenseReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    if report.owner_user_id != owner_user_id:
        raise HTTPException(status_code=403, detail="Report does not belong to this user")
    return report


@router.post("", response_model=ReportRead, status_code=status.HTTP_201_CREATED)
def create_report(
    payload: ReportCreate, session: Session = Depends(get_session)
) -> ReportRead:
    owner = session.get(AppUser, payload.owner_user_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="owner_user_id does not exist")

    report = ExpenseReport(
        owner_user_id=payload.owner_user_id,
        report_kind=payload.report_kind,
        title=payload.title,
        status="draft",
        report_currency=payload.report_currency,
        period_start=payload.period_start,
        period_end=payload.period_end,
        notes=payload.notes,
    )
    session.add(report)
    session.commit()
    session.refresh(report)
    print(f"ExpenseReport created id={report.id} owner={report.owner_user_id} kind={report.report_kind}")
    return ReportRead.model_validate(report)


@router.get("", response_model=list[ReportRead])
def list_reports(
    owner_user_id: int,
    status: str | None = None,
    report_kind: str | None = None,
    session: Session = Depends(get_session),
) -> list[ReportRead]:
    stmt = select(ExpenseReport).where(ExpenseReport.owner_user_id == owner_user_id)
    if status is not None:
        stmt = stmt.where(ExpenseReport.status == status)
    if report_kind is not None:
        stmt = stmt.where(ExpenseReport.report_kind == report_kind)
    stmt = stmt.order_by(ExpenseReport.updated_at.desc(), ExpenseReport.id.desc())
    rows = session.exec(stmt).all()
    return [ReportRead.model_validate(r) for r in rows]


@router.get("/{report_id}", response_model=ReportDetail)
def read_report(
    report_id: int,
    owner_user_id: int,
    session: Session = Depends(get_session),
) -> ReportDetail:
    report = _require_report_for_owner(session, report_id, owner_user_id)

    receipt_count = len(
        session.exec(
            select(ReceiptDocument.id).where(ReceiptDocument.expense_report_id == report.id)
        ).all()
    )

    review_session_ids = session.exec(
        select(ReviewSession.id)
        .where(ReviewSession.expense_report_id == report.id)
        .order_by(ReviewSession.id.desc())
    ).all()
    latest_review_session_id = review_session_ids[0] if review_session_ids else None

    report_run_ids = session.exec(
        select(ReportRun.id)
        .where(ReportRun.expense_report_id == report.id)
        .order_by(ReportRun.id.asc())
    ).all()

    base = ReportRead.model_validate(report).model_dump()
    return ReportDetail(
        **base,
        receipt_count=receipt_count,
        review_session_id=latest_review_session_id,
        report_run_ids=list(report_run_ids),
    )


@router.post(
    "/{report_id}/receipts/{receipt_id}", response_model=ReceiptAttachResponse
)
def attach_receipt(
    report_id: int,
    receipt_id: int,
    owner_user_id: int,
    session: Session = Depends(get_session),
) -> ReceiptAttachResponse:
    report = _require_report_for_owner(session, report_id, owner_user_id)

    receipt = session.get(ReceiptDocument, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    if receipt.uploader_user_id != owner_user_id:
        raise HTTPException(
            status_code=403, detail="Receipt does not belong to this user"
        )

    if receipt.expense_report_id == report.id:
        print(f"Attach receipt idempotent: receipt={receipt.id} already on report={report.id}")
        return ReceiptAttachResponse(
            receipt_id=receipt.id,
            expense_report_id=report.id,
            message="Already attached",
        )
    if receipt.expense_report_id is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Receipt {receipt.id} is already attached to report "
                f"{receipt.expense_report_id}; detach first"
            ),
        )

    receipt.expense_report_id = report.id
    receipt.updated_at = datetime.now(timezone.utc)
    session.add(receipt)
    session.commit()
    session.refresh(receipt)
    print(f"Attach receipt: receipt={receipt.id} -> report={report.id}")
    return ReceiptAttachResponse(
        receipt_id=receipt.id,
        expense_report_id=report.id,
        message="Attached",
    )


@router.delete(
    "/{report_id}/receipts/{receipt_id}", response_model=ReceiptDetachResponse
)
def detach_receipt(
    report_id: int,
    receipt_id: int,
    owner_user_id: int,
    session: Session = Depends(get_session),
) -> ReceiptDetachResponse:
    report = _require_report_for_owner(session, report_id, owner_user_id)

    receipt = session.get(ReceiptDocument, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    if receipt.uploader_user_id != owner_user_id:
        raise HTTPException(
            status_code=403, detail="Receipt does not belong to this user"
        )
    if receipt.expense_report_id != report.id:
        raise HTTPException(
            status_code=404, detail="Receipt is not attached to this report"
        )

    receipt.expense_report_id = None
    receipt.updated_at = datetime.now(timezone.utc)
    session.add(receipt)
    session.commit()
    print(f"Detach receipt: receipt={receipt.id} from report={report.id}")
    return ReceiptDetachResponse(receipt_id=receipt.id, message="Detached")
