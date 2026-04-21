from dataclasses import dataclass

from sqlmodel import Session, select

from app.models import ClarificationQuestion, MatchDecision, PolicyDecision, ReceiptDocument, StatementTransaction


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    message: str
    receipt_id: int | None = None
    statement_transaction_id: int | None = None
    match_decision_id: int | None = None


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


def validate_report_readiness(session: Session, statement_import_id: int) -> ReportValidation:
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

    for receipt in receipts:
        bp = (receipt.business_or_personal or "").strip().lower()
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
            if not (receipt.report_bucket or "").strip():
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
            bucket = (receipt.report_bucket or "").lower()
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
    business_receipts = sum(1 for receipt in receipts if (receipt.business_or_personal or "").lower() == "business")
    personal_receipts = sum(1 for receipt in receipts if (receipt.business_or_personal or "").lower() == "personal")
    return ReportValidation(
        statement_import_id=statement_import_id,
        ready=error_count == 0,
        issue_count=error_count,
        warning_count=warning_count,
        included_transactions=len(included_policy_tx_ids) if included_policy_tx_ids else len(approved_by_transaction),
        approved_matches=len(decisions),
        business_receipts=business_receipts,
        personal_receipts=personal_receipts,
        issues=issues,
    )
