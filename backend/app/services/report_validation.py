from dataclasses import dataclass
from datetime import date
import json

from sqlmodel import Session, select

from app.models import (
    ClarificationQuestion,
    ExpenseReport,
    MatchDecision,
    PolicyDecision,
    ReceiptDocument,
    ReviewRow,
    ReviewSession,
    StatementTransaction,
)


AIRFARE_BUCKET = "Airfare/Bus/Ferry/Other"
AIR_TRAVEL_DETAIL_ROWS_BY_SHEET = {
    "Week 1A": 3,
    "Week 2A": 3,
}


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    message: str
    receipt_id: int | None = None
    statement_transaction_id: int | None = None
    match_decision_id: int | None = None
    review_row_id: int | None = None
    supplier: str | None = None
    transaction_date: str | None = None
    report_bucket: str | None = None
    air_travel_date: str | None = None
    air_travel_return_date: str | None = None
    air_travel_rt_or_oneway: str | None = None


@dataclass
class ReportValidation:
    statement_import_id: int
    ready: bool
    issue_count: int
    warning_count: int
    included_transactions: int
    approved_matches: int
    business_receipts: int
    personal_receipts: int
    issues: list[ValidationIssue]


def _approved_decisions_for_statement(session: Session, statement_import_id: int) -> list[MatchDecision]:
    statement_ids = {
        tx.id
        for tx in session.exec(
            select(StatementTransaction).where(StatementTransaction.statement_import_id == statement_import_id)
        ).all()
        if tx.id is not None
    }
    if not statement_ids:
        return []
    return [
        decision
        for decision in session.exec(select(MatchDecision).where(MatchDecision.approved == True)).all()  # noqa: E712
        if decision.statement_transaction_id in statement_ids
    ]


def _latest_review_session(
    session: Session, *, expense_report_id: int
) -> ReviewSession | None:
    return session.exec(
        select(ReviewSession)
        .where(ReviewSession.expense_report_id == expense_report_id)
        .order_by(ReviewSession.created_at.desc())
    ).first()


def _parse_snapshot_date(value: object) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _snapshot_issue_context(row: dict) -> dict:
    return {
        "review_row_id": row.get("review_row_id"),
        "supplier": row.get("supplier"),
        "transaction_date": row.get("transaction_date"),
        "report_bucket": row.get("report_bucket"),
        "statement_transaction_id": row.get("transaction_id"),
    }


def _air_travel_issue_context(row: dict, travel_date: date | None, return_date: date | None) -> dict:
    context = _snapshot_issue_context(row)
    context.update(
        {
            "air_travel_date": travel_date.isoformat() if travel_date else None,
            "air_travel_return_date": return_date.isoformat() if return_date else None,
            "air_travel_rt_or_oneway": (row.get("air_travel_rt_or_oneway") or "").strip() or None,
        }
    )
    return context


def _review_snapshot_issues(
    session: Session, *, expense_report_id: int
) -> tuple[list[ValidationIssue], int | None]:
    review = _latest_review_session(session, expense_report_id=expense_report_id)
    if not review or review.status != "confirmed" or not review.snapshot_json:
        return [
            ValidationIssue(
                severity="error",
                code="review_not_confirmed",
                message="Report generation requires a confirmed review snapshot. Confirm reviewed data before generating.",
            )
        ], None

    snapshot = json.loads(review.snapshot_json)
    dates = sorted(
        {
            tx_date
            for row in snapshot
            if (tx_date := _parse_snapshot_date(row.get("transaction_date"))) is not None
        }
    )
    first7, next7 = set(dates[:7]), set(dates[7:14])
    page_counts = {"Week 1A": 0, "Week 2A": 0}
    issues: list[ValidationIssue] = []
    for row in snapshot:
        if row.get("report_bucket") != AIRFARE_BUCKET:
            continue
        tx_date = _parse_snapshot_date(row.get("transaction_date"))
        travel_date = _parse_snapshot_date(row.get("air_travel_date")) or tx_date
        return_date = _parse_snapshot_date(row.get("air_travel_return_date"))
        if (row.get("air_travel_rt_or_oneway") or "").strip().upper() == "RT" and not return_date:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="air_travel_return_date_missing",
                    message="An RT air travel row is missing its return date.",
                    **_air_travel_issue_context(row, travel_date, return_date),
                )
            )
        if tx_date in first7:
            page_counts["Week 1A"] += 1
        elif tx_date in next7:
            page_counts["Week 2A"] += 1

    for sheet_name, count in page_counts.items():
        capacity = AIR_TRAVEL_DETAIL_ROWS_BY_SHEET[sheet_name]
        if count > capacity:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="air_travel_detail_overflow",
                    message=(
                        f"{sheet_name} has {count} air travel rows, but the template only has "
                        f"{capacity} Air Travel Reconciliation detail rows. Extra air travel details "
                        "will not be written to the worksheet."
                    ),
                )
            )
    return issues, len(snapshot)


def validate_report_readiness(
    session: Session, *, expense_report_id: int
) -> ReportValidation:
    report = session.get(ExpenseReport, expense_report_id)
    if report is None:
        raise ValueError(f"ExpenseReport {expense_report_id} not found")
    if report.report_kind == "personal_reimbursement":
        raise NotImplementedError(
            "Personal reimbursement report template coming in M1 Day 8-9"
        )
    if report.report_kind != "diners_statement":
        raise ValueError(f"Unknown report_kind: {report.report_kind}")
    if report.statement_import_id is None:
        raise ValueError(
            f"Diners-statement report {expense_report_id} has no statement_import_id"
        )
    statement_import_id = report.statement_import_id

    transactions = session.exec(
        select(StatementTransaction).where(StatementTransaction.statement_import_id == statement_import_id)
    ).all()
    transaction_by_id = {tx.id: tx for tx in transactions if tx.id is not None}
    all_decisions = [
        decision
        for decision in session.exec(select(MatchDecision)).all()
        if decision.statement_transaction_id in transaction_by_id
    ]
    decisions = [decision for decision in all_decisions if decision.approved]
    issues: list[ValidationIssue] = []
    review_issue_list, confirmed_review_rows = _review_snapshot_issues(
        session, expense_report_id=expense_report_id
    )
    issues.extend(review_issue_list)

    if not transactions:
        issues.append(
            ValidationIssue(
                severity="error",
                code="no_statement_transactions",
                message="No statement transactions exist for this statement import.",
            )
        )

    approved_by_transaction: dict[int, list[MatchDecision]] = {}
    for decision in decisions:
        approved_by_transaction.setdefault(decision.statement_transaction_id, []).append(decision)

    for transaction_id, transaction_decisions in approved_by_transaction.items():
        if len(transaction_decisions) > 1:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="multiple_approved_receipts",
                    message="A statement transaction has more than one approved receipt match.",
                    statement_transaction_id=transaction_id,
                )
            )

    approved_receipt_ids_from_decisions = {decision.receipt_document_id for decision in decisions}
    unresolved_high_by_receipt: dict[int, list[MatchDecision]] = {}
    for decision in all_decisions:
        if decision.receipt_document_id in approved_receipt_ids_from_decisions:
            continue
        if decision.rejected:
            continue
        if decision.confidence == "high":
            unresolved_high_by_receipt.setdefault(decision.receipt_document_id, []).append(decision)
    for receipt_id, receipt_decisions in unresolved_high_by_receipt.items():
        issues.append(
            ValidationIssue(
                severity="warning",
                code="unresolved_high_confidence_candidate",
                message="A receipt has a high-confidence match candidate that still needs manual approval or rejection.",
                receipt_id=receipt_id,
                statement_transaction_id=receipt_decisions[0].statement_transaction_id,
                match_decision_id=receipt_decisions[0].id,
            )
        )

    receipts: list[ReceiptDocument] = []
    for decision in decisions:
        receipt = session.get(ReceiptDocument, decision.receipt_document_id)
        if receipt:
            receipts.append(receipt)
        if decision.confidence in {"medium", "low"}:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="unreviewed_non_high_match",
                    message="A medium/low confidence match is approved and should be explicitly reviewed before report generation.",
                    receipt_id=decision.receipt_document_id,
                    statement_transaction_id=decision.statement_transaction_id,
                    match_decision_id=decision.id,
                )
            )

    open_question_receipt_ids = {
        question.receipt_document_id
        for question in session.exec(
            select(ClarificationQuestion).where(ClarificationQuestion.status == "open")
        ).all()
        if question.receipt_document_id is not None
    }
    approved_receipt_ids = {receipt.id for receipt in receipts if receipt.id is not None}
    for receipt_id in sorted(open_question_receipt_ids & approved_receipt_ids):
        issues.append(
            ValidationIssue(
                severity="error",
                code="open_clarification",
                message="An approved receipt still has an open clarification question.",
                receipt_id=receipt_id,
            )
        )

    # B5: ReviewRow.confirmed_json is the canonical source for report_bucket and
    # business_or_personal. Build a {receipt_id -> confirmed dict} lookup from the
    # latest review session. Any approved receipt without a confirmed review row
    # is a divided-ownership hazard and gets a structured error.
    confirmed_by_receipt_id: dict[int, dict] = {}
    latest_review = _latest_review_session(session, expense_report_id=expense_report_id)
    if latest_review and latest_review.id is not None:
        for row in session.exec(
            select(ReviewRow).where(ReviewRow.review_session_id == latest_review.id)
        ).all():
            if row.receipt_document_id is None:
                continue
            try:
                confirmed_by_receipt_id[row.receipt_document_id] = json.loads(row.confirmed_json or "{}")
            except json.JSONDecodeError:
                confirmed_by_receipt_id[row.receipt_document_id] = {}

    for receipt in receipts:
        if receipt.id is None or receipt.id not in confirmed_by_receipt_id:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="missing_review_row",
                    message=(
                        f"Receipt {receipt.id} has no confirmed review row "
                        "— build or re-sync the review session before validating"
                    ),
                    receipt_id=receipt.id,
                )
            )
            continue
        confirmed = confirmed_by_receipt_id[receipt.id]
        bp = (confirmed.get("business_or_personal") or "").strip().lower()
        if not bp:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="missing_business_or_personal",
                    message="An approved receipt is missing business/personal classification.",
                    receipt_id=receipt.id,
                )
            )
            continue
        if bp == "business":
            bucket_value = (confirmed.get("report_bucket") or "").strip()
            if not bucket_value:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="missing_report_bucket",
                        message="A business receipt is missing an expense report bucket.",
                        receipt_id=receipt.id,
                    )
                )
            if not (receipt.business_reason or "").strip():
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        code="missing_business_reason",
                        message="A business receipt is missing a business reason/project note.",
                        receipt_id=receipt.id,
                    )
                )
            bucket = bucket_value.lower()
            if any(token in bucket for token in ("meal", "breakfast", "lunch", "dinner", "entertainment")):
                if not (receipt.attendees or "").strip():
                    issues.append(
                        ValidationIssue(
                            severity="warning",
                            code="missing_attendees",
                            message="A meal/entertainment business receipt is missing attendees.",
                            receipt_id=receipt.id,
                        )
                    )
        elif bp not in {"personal", "unclear"}:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="unknown_business_classification",
                    message="An approved receipt has an unfamiliar business/personal classification.",
                    receipt_id=receipt.id,
                )
            )

    policy_decisions = session.exec(select(PolicyDecision)).all()
    policy_by_tx = {policy.statement_transaction_id: policy for policy in policy_decisions}
    included_policy_tx_ids = {
        tx_id
        for tx_id, policy in policy_by_tx.items()
        if policy.include_in_report and tx_id in transaction_by_id
    }
    for tx_id in included_policy_tx_ids:
        policy = policy_by_tx[tx_id]
        if not policy.business_or_personal:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="policy_missing_business_classification",
                    message="An included policy decision is missing business/personal classification.",
                    statement_transaction_id=tx_id,
                )
            )
        if policy.business_or_personal.lower() == "business" and not policy.report_bucket:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="policy_missing_report_bucket",
                    message="An included business policy decision is missing report bucket.",
                    statement_transaction_id=tx_id,
                )
            )

    error_count = sum(1 for issue in issues if issue.severity == "error")
    warning_count = sum(1 for issue in issues if issue.severity == "warning")
    business_receipts = sum(
        1
        for receipt in receipts
        if receipt.id is not None
        and (confirmed_by_receipt_id.get(receipt.id, {}).get("business_or_personal") or "").lower() == "business"
    )
    personal_receipts = sum(
        1
        for receipt in receipts
        if receipt.id is not None
        and (confirmed_by_receipt_id.get(receipt.id, {}).get("business_or_personal") or "").lower() == "personal"
    )
    return ReportValidation(
        statement_import_id=statement_import_id,
        ready=error_count == 0,
        issue_count=error_count,
        warning_count=warning_count,
        included_transactions=confirmed_review_rows
        if confirmed_review_rows is not None
        else len(included_policy_tx_ids)
        if included_policy_tx_ids
        else len(approved_by_transaction),
        approved_matches=len(decisions),
        business_receipts=business_receipts,
        personal_receipts=personal_receipts,
        issues=issues,
    )
