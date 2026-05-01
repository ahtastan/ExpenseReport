from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import json

from sqlmodel import Session, select

from app.json_utils import decode_decimal
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
from app.services.receipt_statement_safety import receipt_statement_issues

# F-AI-0b-2: AI second-read context is advisory-only. It is surfaced via
# source.ai_review on review-row payloads but must NEVER add entries to the
# ValidationIssue list emitted by validate_report_readiness. Only deterministic
# safety/match/business-rule checks may emit readiness issues here.

AIRFARE_BUCKET = "Airfare/Bus/Ferry/Other"
AIR_TRAVEL_DETAIL_ROWS_BY_SHEET = {
    "Week 1A": 3,
    "Week 2A": 3,
}

# Buckets that EDT treats as meals and therefore require attendees.
# Spec lists the first five; "Entertainment" matches the canonical bucket name
# used elsewhere in the codebase (``review_sessions.MEAL_BUCKETS``).
MEAL_BUCKETS_REQUIRING_ATTENDEES = {
    "Dinner",
    "Lunch",
    "Breakfast",
    "Meals/Snacks",
    "Meals & Entertainment",
    "Entertainment",
}

CUSTOMER_ENTERTAINMENT_BUCKET = "Customer Entertainment"

# Dinner per-head caps (USD). FX conversion is not live until M1 Day 7, so
# the cap check only applies to rows already denominated in USD.
DINNER_CAP_WITH_CUSTOMER_USD = 60
DINNER_CAP_SOLO_USD = 30

# Addition B: hotel-chain keywords. Case-insensitive substring match on the
# confirmed supplier name. A payment_receipt-classified receipt from any of
# these suppliers gets a soft flag — EDT prefers itemized folios.
HOTEL_CHAIN_KEYWORDS = (
    "hilton",
    "hampton",
    "wyndham",
    "tryp",
    "marriott",
    "ibis",
    "novotel",
    "holiday inn",
    "hyatt",
    "sheraton",
    "westin",
    "intercontinental",
    "ritz",
    "four seasons",
    "accor",
    "mercure",
    "best western",
    "comfort inn",
    "wingate",
)

BUSINESS_OR_PERSONAL_QUESTION_KEYS = {
    "business_or_personal",
    "business_or_personal_retry",
}
BUSINESS_REASON_QUESTION_KEYS = {
    "business_reason",
    "telegram_market_context",
    "telegram_market_context_retry",
    "telegram_telecom_context",
    "telegram_telecom_context_retry",
    "telegram_personal_care_context",
    "telegram_personal_care_context_retry",
}
ATTENDEE_QUESTION_KEYS = {
    "attendees",
    "telegram_meal_context",
    "telegram_meal_context_retry",
}
TELEGRAM_CONTEXT_QUESTION_KEYS = BUSINESS_REASON_QUESTION_KEYS | ATTENDEE_QUESTION_KEYS
TELECOM_BUCKET_TOKENS = {
    "communication",
    "communications",
    "gsm",
    "internet",
    "phone",
    "phone bill",
    "telephone",
    "telephone/internet",
    "telecom",
    "telecom bill",
    "utility payment",
}
TELECOM_TEXT_TOKENS = (
    "abonelik",
    "fatura tahsilatı",
    "fatura tahsilati",
    "gsm",
    "iletişim",
    "iletisim",
    "internet",
    "phone bill",
    "superonline",
    "telefon",
    "turk telekom",
    "turkcell",
    "turknet",
    "turk.net",
    "türk telekom",
    "türknet",
    "vodafone",
)


def _clean_string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _lower_string(value: object) -> str:
    return _clean_string(value).lower()


def _supplier_is_hotel(supplier: object) -> bool:
    if not isinstance(supplier, str):
        return False
    lowered = supplier.lower()
    return any(keyword in lowered for keyword in HOTEL_CHAIN_KEYWORDS)


def _split_attendees(value: object) -> list[str]:
    raw = (value or "").strip() if isinstance(value, str) else ""
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _is_solo_attendee_list(entries: list[str]) -> bool:
    return len(entries) == 1 and entries[0].lower() == "self"


def _has_coo_preapproval_reference(business_reason: object) -> bool:
    text = (business_reason or "").lower() if isinstance(business_reason, str) else ""
    return "coo" in text or "approved by" in text


def _amount_to_context(value: object) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _is_telecom_row(confirmed: dict | None, receipt: ReceiptDocument | None) -> bool:
    confirmed = confirmed or {}
    bucket = _lower_string(confirmed.get("report_bucket") or (receipt.report_bucket if receipt else None))
    if bucket in TELECOM_BUCKET_TOKENS:
        return True
    text = " ".join(
        part
        for part in (
            _lower_string(confirmed.get("supplier")),
            _lower_string(confirmed.get("category")),
            _lower_string(confirmed.get("report_bucket")),
            _lower_string(receipt.extracted_supplier if receipt else None),
            _lower_string(receipt.original_file_name if receipt else None),
        )
        if part
    )
    return any(token in text for token in TELECOM_TEXT_TOKENS)


def _is_meal_bucket(bucket_value: object) -> bool:
    return _clean_string(bucket_value) in MEAL_BUCKETS_REQUIRING_ATTENDEES


def _has_value(value: object) -> bool:
    return bool(_clean_string(value)) if isinstance(value, str) else value not in (None, "")


def _issue_context_from_confirmed(
    confirmed: dict | None,
    *,
    receipt_id: int | None,
    review_row_id: int | None,
    match_decision_id: int | None = None,
) -> dict:
    confirmed = confirmed or {}
    return {
        "receipt_id": receipt_id,
        "review_row_id": review_row_id,
        "statement_transaction_id": confirmed.get("transaction_id"),
        "match_decision_id": match_decision_id,
        "supplier": _clean_string(confirmed.get("supplier")) or None,
        "transaction_date": confirmed.get("transaction_date")
        if isinstance(confirmed.get("transaction_date"), str)
        else None,
        "report_bucket": _clean_string(confirmed.get("report_bucket")) or None,
        "amount": _amount_to_context(confirmed.get("amount")),
        "currency": _clean_string(confirmed.get("currency")) or None,
    }


def _open_question_still_blocks_report(
    question: ClarificationQuestion,
    *,
    confirmed: dict | None,
    receipt: ReceiptDocument | None,
) -> bool:
    """Return whether an open clarification is still relevant to report readiness.

    Telegram AI follow-up policy has changed several times during testing, so
    stale open questions can remain even after the current review row is already
    answered or reclassified. Report validation should be governed by the current
    confirmed ReviewRow state, not by obsolete helper questions.
    """
    if question.answer_text:
        return False
    key = question.question_key
    confirmed = confirmed or {}
    bp = _lower_string(confirmed.get("business_or_personal"))
    bucket = confirmed.get("report_bucket")
    if bp == "personal":
        return False
    if key in BUSINESS_OR_PERSONAL_QUESTION_KEYS:
        return bp not in {"business", "personal", "unclear"}
    if key in BUSINESS_REASON_QUESTION_KEYS:
        if bp != "business":
            return False
        if _is_telecom_row(confirmed, receipt):
            return False
        return not _has_value(confirmed.get("business_reason"))
    if key in ATTENDEE_QUESTION_KEYS:
        if bp != "business":
            return False
        if _is_telecom_row(confirmed, receipt):
            return False
        if not _is_meal_bucket(bucket):
            return False
        return not _split_attendees(confirmed.get("attendees"))
    if key in {"receipt_date", "receipt_date_retry"}:
        return not (_has_value(confirmed.get("transaction_date")) or (receipt and receipt.extracted_date))
    if key in {"local_amount", "local_amount_retry"}:
        return not (_has_value(confirmed.get("amount")) or (receipt and receipt.extracted_local_amount is not None))
    if key == "supplier":
        return not (_has_value(confirmed.get("supplier")) or (receipt and _has_value(receipt.extracted_supplier)))
    return True


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
    amount: str | None = None
    currency: str | None = None
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
        "amount": _amount_to_context(row.get("amount")),
        "currency": row.get("currency"),
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
            transaction = transaction_by_id.get(decision.statement_transaction_id)
            if transaction is not None:
                for safety_issue in receipt_statement_issues(receipt, transaction):
                    issues.append(
                        ValidationIssue(
                            severity="warning",
                            code=safety_issue.code,
                            message=safety_issue.message,
                            receipt_id=decision.receipt_document_id,
                            statement_transaction_id=decision.statement_transaction_id,
                            match_decision_id=decision.id,
                        )
                    )
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

    # B5: ReviewRow.confirmed_json is the canonical source for report_bucket and
    # business_or_personal. Build a {receipt_id -> confirmed dict} lookup from the
    # latest review session. Any approved receipt without a confirmed review row
    # is a divided-ownership hazard and gets a structured error.
    confirmed_by_receipt_id: dict[int, dict] = {}
    row_id_by_receipt_id: dict[int, int] = {}
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
            if row.id is not None:
                row_id_by_receipt_id[row.receipt_document_id] = row.id

    receipt_by_id = {receipt.id: receipt for receipt in receipts if receipt.id is not None}
    decision_by_receipt_id = {decision.receipt_document_id: decision for decision in decisions}
    approved_receipt_ids = set(receipt_by_id)
    open_questions = [
        question
        for question in session.exec(
            select(ClarificationQuestion).where(ClarificationQuestion.status == "open")
        ).all()
        if question.receipt_document_id in approved_receipt_ids
    ]
    for question in open_questions:
        receipt_id = question.receipt_document_id
        confirmed = confirmed_by_receipt_id.get(receipt_id)
        receipt = receipt_by_id.get(receipt_id)
        if not _open_question_still_blocks_report(question, confirmed=confirmed, receipt=receipt):
            continue
        decision = decision_by_receipt_id.get(receipt_id)
        context = _issue_context_from_confirmed(
            confirmed,
            receipt_id=receipt_id,
            review_row_id=row_id_by_receipt_id.get(receipt_id) if receipt_id is not None else None,
            match_decision_id=decision.id if decision else None,
        )
        issues.append(
            ValidationIssue(
                severity="error",
                code="open_clarification",
                message=(
                    "An approved receipt still has a relevant open clarification "
                    f"question ({question.question_key})."
                ),
                **context,
            )
        )

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
            row_id = row_id_by_receipt_id.get(receipt.id) if receipt.id is not None else None
            supplier = (confirmed.get("supplier") or "").strip() or None
            transaction_date = confirmed.get("transaction_date")
            amount_value = confirmed.get("amount")
            currency_value = (confirmed.get("currency") or "").strip()
            business_reason_value = confirmed.get("business_reason")
            attendees_entries = _split_attendees(confirmed.get("attendees"))
            is_telecom = _is_telecom_row(confirmed, receipt)

            if not bucket_value:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="missing_report_bucket",
                        message="A business receipt is missing an expense report bucket.",
                        receipt_id=receipt.id,
                        review_row_id=row_id,
                        statement_transaction_id=confirmed.get("transaction_id"),
                        supplier=supplier,
                        amount=_amount_to_context(amount_value),
                        currency=currency_value or None,
                    )
                )

            # Addition A: hard-block missing business reason. Reads confirmed_json
            # (canonical after M1 Day 2 pivot), not receipt column scaffolding.
            if not is_telecom and not (business_reason_value or "").strip():
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="missing_business_reason",
                        message=(
                            f"Business row {row_id} ({supplier or 'unknown supplier'}) is "
                            "missing a business reason. Fill in before generating."
                        ),
                        receipt_id=receipt.id,
                        review_row_id=row_id,
                        statement_transaction_id=confirmed.get("transaction_id"),
                        supplier=supplier,
                        transaction_date=transaction_date if isinstance(transaction_date, str) else None,
                        amount=_amount_to_context(amount_value),
                        currency=currency_value or None,
                        report_bucket=bucket_value or None,
                    )
                )

            # Addition A: hard-block meal rows missing attendees. Bucket check
            # uses the explicit spec list (MEAL_BUCKETS_REQUIRING_ATTENDEES)
            # rather than substring matching to avoid false positives.
            if _is_meal_bucket(bucket_value) and not attendees_entries:
                amount_display = (
                    f"{amount_value} {currency_value}".strip()
                    if amount_value is not None
                    else "amount unknown"
                )
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="missing_attendees_on_meal",
                        message=(
                            f"Meal row {row_id} ({supplier or 'unknown supplier'}, "
                            f"{amount_display}) is missing attendees. "
                            "EDT requires attendees on all meal expenses."
                        ),
                        receipt_id=receipt.id,
                        review_row_id=row_id,
                        statement_transaction_id=confirmed.get("transaction_id"),
                        supplier=supplier,
                        transaction_date=transaction_date if isinstance(transaction_date, str) else None,
                        amount=_amount_to_context(amount_value),
                        currency=currency_value or None,
                        report_bucket=bucket_value,
                    )
                )

            # Addition A: Customer Entertainment requires a COO / "approved by"
            # reference in the business reason. Full pre-approval modeling
            # arrives in M3; this scaffolds the gate so unapproved customer
            # entertainment cannot slip into a generated report.
            if bucket_value == CUSTOMER_ENTERTAINMENT_BUCKET:
                if not _has_coo_preapproval_reference(business_reason_value):
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            code="customer_entertainment_no_preapproval",
                            message=(
                                f"Customer entertainment row {row_id} has no COO "
                                "pre-approval reference. Note the approval reference "
                                "in the business reason field."
                            ),
                            receipt_id=receipt.id,
                            review_row_id=row_id,
                            statement_transaction_id=confirmed.get("transaction_id"),
                            supplier=supplier,
                            transaction_date=transaction_date if isinstance(transaction_date, str) else None,
                            amount=_amount_to_context(amount_value),
                            currency=currency_value or None,
                            report_bucket=bucket_value,
                        )
                    )

            # Addition B: hotel folio soft-flag. A hotel-chain supplier with
            # receipt_type=payment_receipt means the user captured a POS slip
            # instead of the itemized folio. EDT's reviewer needs the folio
            # to see room rate + charges broken out. Soft flag only.
            if (
                _supplier_is_hotel(supplier)
                and receipt.receipt_type == "payment_receipt"
            ):
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        code="hotel_needs_itemized_folio",
                        message=(
                            f"Hotel row {row_id} ({supplier}) has a payment receipt only, "
                            "not an itemized folio. EDT prefers hotel receipts that show "
                            "room cost and charges."
                        ),
                        receipt_id=receipt.id,
                        review_row_id=row_id,
                        statement_transaction_id=confirmed.get("transaction_id"),
                        supplier=supplier,
                        transaction_date=transaction_date if isinstance(transaction_date, str) else None,
                        amount=_amount_to_context(amount_value),
                        currency=currency_value or None,
                        report_bucket=bucket_value or None,
                    )
                )

            # Addition A: Dinner per-head cap. Soft flag. USD-only until FX
            # lookup lands (M1 Day 7). Skipped when attendees is empty —
            # missing_attendees_on_meal already covers that case.
            # decode_decimal tolerates both new string-shaped values
            # ("123.45", per M1 Day 2.5) and legacy float-shaped JSON
            # numbers from pre-migration ReviewRow.confirmed_json blobs.
            try:
                amount_decimal = decode_decimal(amount_value)
            except (TypeError, InvalidOperation, ValueError):
                amount_decimal = None
            if (
                bucket_value == "Dinner"
                and currency_value == "USD"
                and attendees_entries
                and amount_decimal is not None
                and amount_decimal > 0
            ):
                solo = _is_solo_attendee_list(attendees_entries)
                cap = DINNER_CAP_SOLO_USD if solo else DINNER_CAP_WITH_CUSTOMER_USD
                per_head = amount_decimal / Decimal(len(attendees_entries))
                if per_head > cap:
                    with_without = "without" if solo else "with"
                    issues.append(
                        ValidationIssue(
                            severity="warning",
                            code="dinner_exceeds_cap",
                            message=(
                                f"Dinner row {row_id} is ${per_head:.2f}/head. "
                                f"Exceeds EDT guideline of ${cap}/head {with_without} customer. "
                                "Add justification if warranted."
                            ),
                            receipt_id=receipt.id,
                            review_row_id=row_id,
                            statement_transaction_id=confirmed.get("transaction_id"),
                            supplier=supplier,
                            transaction_date=transaction_date if isinstance(transaction_date, str) else None,
                            amount=_amount_to_context(amount_value),
                            currency=currency_value or None,
                            report_bucket=bucket_value,
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
