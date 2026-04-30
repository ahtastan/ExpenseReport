import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlmodel import Session, select

from app.json_utils import DecimalEncoder
from app.models import (
    ExpenseReport,
    MatchDecision,
    ReceiptDocument,
    ReviewRow,
    ReviewSession,
    StatementTransaction,
)
from app.services.agent_receipt_review_persistence import latest_ai_review_for_receipt
from app.services.receipt_statement_safety import (
    receipt_statement_issue_note,
    receipt_statement_issues,
)
from app.services.merchant_buckets import suggest_bucket
from app.services.telegram_ai_reply_drafts import build_review_row_reply_draft


REQUIRED_FIELDS = [
    "transaction_date",
    "supplier",
    "amount",
    "currency",
    "business_or_personal",
    "report_bucket",
]

PUBLIC_PATH_KEYS = {"storage_path", "receipt_path"}

# Optional fields for the Air Travel Reconciliation detail table.
# Not required for confirmation; populated per-row when the bucket is "Airfare/Bus/Ferry/Other".
AIR_TRAVEL_FIELDS = [
    "air_travel_date",
    "air_travel_from",
    "air_travel_to",
    "air_travel_airline",
    "air_travel_rt_or_oneway",
    "air_travel_return_date",
    "air_travel_paid_by",
    "air_travel_total_tkt_cost",
    "air_travel_prior_tkt_value",
    "air_travel_comments",
]


MEAL_DETAIL_FIELDS = [
    "meal_place",
    "meal_location",
    "meal_eg",
    "meal_mr",
]

MEAL_BUCKETS = ["Meals/Snacks", "Breakfast", "Lunch", "Dinner", "Entertainment"]


def _default_air_travel(tx_date_iso: str | None) -> dict[str, Any]:
    """Default values for the air-travel detail fields on a fresh review row."""
    return {
        "air_travel_date": tx_date_iso,
        "air_travel_from": None,
        "air_travel_to": None,
        "air_travel_airline": None,
        "air_travel_rt_or_oneway": None,
        "air_travel_return_date": None,
        "air_travel_paid_by": "DC Card",
        "air_travel_total_tkt_cost": None,
        "air_travel_prior_tkt_value": 0,
        "air_travel_comments": None,
    }


def _default_meal_detail() -> dict[str, Any]:
    return {
        "meal_place": None,
        "meal_location": None,
        "meal_eg": False,
        "meal_mr": False,
    }


def _missing_required_fields(confirmed: dict[str, Any]) -> list[str]:
    return [field for field in REQUIRED_FIELDS if confirmed.get(field) in (None, "")]


def _attention_note(missing: list[str], safety_issues: list[dict] | None = None) -> str | None:
    parts = [f"missing {field}" for field in missing]
    if safety_issues:
        parts.append(receipt_statement_issue_note(safety_issues))
    return "; ".join(part for part in parts if part) or None


def _source_receipt_statement_issues(source_json: str | None) -> list[dict]:
    try:
        source = _loads(source_json)
    except json.JSONDecodeError:
        return []
    issues = source.get("match", {}).get("receipt_statement_issues", [])
    return issues if isinstance(issues, list) else []


def _meal_duplicate_key(confirmed: dict[str, Any]) -> tuple[str, str] | None:
    if (confirmed.get("business_or_personal") or "").lower() != "business":
        return None
    bucket = confirmed.get("report_bucket")
    tx_date = confirmed.get("transaction_date")
    if bucket not in MEAL_BUCKETS or not tx_date:
        return None
    return str(tx_date), str(bucket)


def _other_meal_bucket_suggestions(bucket: str) -> str:
    choices = [item for item in MEAL_BUCKETS if item != bucket]
    if len(choices) == 1:
        return choices[0]
    return f"{', '.join(choices[:-1])}, or {choices[-1]}"


def _loads(raw: str | None) -> dict[str, Any]:
    return json.loads(raw or "{}")


def _public_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _public_payload(child)
            for key, child in value.items()
            if key not in PUBLIC_PATH_KEYS
        }
    if isinstance(value, list):
        return [_public_payload(item) for item in value]
    return value


def _dumps(value: dict[str, Any] | list[dict[str, Any]]) -> str:
    # DecimalEncoder emits Decimal as a fixed-point string so blob round-trips
    # preserve money/rate precision (M1 Day 2.5). default=str remains as a
    # safety net for any other non-JSON-native scalars (e.g. date/datetime).
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        cls=DecimalEncoder,
        default=str,
    )


def _latest_session(session: Session, expense_report_id: int) -> ReviewSession | None:
    return session.exec(
        select(ReviewSession)
        .where(ReviewSession.expense_report_id == expense_report_id)
        .order_by(ReviewSession.created_at.desc())
    ).first()


def _resolve_statement_to_expense_report(
    session: Session, statement_import_id: int, *, owner_user_id: int
) -> int:
    """Find or create a diners_statement ExpenseReport for a statement.

    Compatibility glue for URL-bound ``statement_import_id`` routes and the
    manual-entry flow. Callers must pass ``owner_user_id`` explicitly; the
    helper never falls back or invents an owner. Returns the ExpenseReport id.
    """
    existing = session.exec(
        select(ExpenseReport)
        .where(
            ExpenseReport.statement_import_id == statement_import_id,
            ExpenseReport.report_kind == "diners_statement",
            ExpenseReport.owner_user_id == owner_user_id,
        )
        .order_by(ExpenseReport.id.asc())
    ).first()
    if existing is not None and existing.id is not None:
        return existing.id

    report = ExpenseReport(
        owner_user_id=owner_user_id,
        report_kind="diners_statement",
        title=f"Diners statement {statement_import_id}",
        status="draft",
        report_currency="USD",
        statement_import_id=statement_import_id,
    )
    session.add(report)
    session.commit()
    session.refresh(report)
    return report.id  # type: ignore[return-value]


def _amount_and_currency(tx: StatementTransaction) -> tuple[Decimal | None, str | None]:
    if tx.usd_amount is not None:
        return tx.usd_amount, "USD"
    if tx.local_amount is not None:
        return tx.local_amount, tx.local_currency
    return None, tx.local_currency


def _statement_payload(tx: StatementTransaction) -> tuple[dict[str, Any], dict[str, Any]]:
    amount, currency = _amount_and_currency(tx)
    tx_date = tx.transaction_date.isoformat() if tx.transaction_date else None
    source = {
        "statement": {
            "transaction_id": tx.id,
            "transaction_date": tx_date,
            "supplier_raw": tx.supplier_raw,
            "local_amount": tx.local_amount,
            "local_currency": tx.local_currency,
            "usd_amount": tx.usd_amount,
            "source_row_ref": tx.source_row_ref,
        },
        "receipt": {
            "status": "missing",
            "receipt_id": None,
            "original_file_name": None,
            "storage_path": None,
            "extracted_date": None,
            "extracted_supplier": None,
            "extracted_local_amount": None,
            "extracted_currency": None,
            "ocr_confidence": None,
        },
        "match": {
            "status": "unmatched",
            "match_decision_id": None,
            "confidence": None,
            "match_method": None,
            "reason": "No approved receipt match exists for this statement transaction.",
            "approved": False,
        },
    }
    suggested = {
        "transaction_id": tx.id,
        "receipt_id": None,
        "receipt_path": None,
        "receipt_file_name": None,
        "transaction_date": tx_date,
        "supplier": tx.supplier_raw,
        "amount": amount,
        "currency": currency,
        "business_or_personal": None,
        "report_bucket": suggest_bucket(tx.supplier_raw),
        "business_reason": None,
        "attendees": None,
        "match_confidence": None,
        "review_status": "unmatched",
        **_default_air_travel(tx_date),
        **_default_meal_detail(),
    }
    return source, suggested


def _row_payload(
    session: Session,
    tx: StatementTransaction,
    receipt: ReceiptDocument,
    decision: MatchDecision,
) -> tuple[dict[str, Any], dict[str, Any]]:
    amount, currency = _amount_and_currency(tx)
    tx_date = tx.transaction_date or receipt.extracted_date
    safety_issues = [issue.as_dict() for issue in receipt_statement_issues(receipt, tx)]
    match_payload: dict[str, Any] = {
        "status": "matched",
        "match_decision_id": decision.id,
        "confidence": decision.confidence,
        "match_method": decision.match_method,
        "reason": decision.reason,
        "approved": decision.approved,
    }
    # Only include receipt_statement_issues when there are real issues, so a clean
    # match does not expose an empty list in the API response.
    if safety_issues:
        match_payload["receipt_statement_issues"] = safety_issues
    source = {
        "statement": {
            "transaction_id": tx.id,
            "transaction_date": tx.transaction_date.isoformat() if tx.transaction_date else None,
            "supplier_raw": tx.supplier_raw,
            "local_amount": tx.local_amount,
            "local_currency": tx.local_currency,
            "usd_amount": tx.usd_amount,
            "source_row_ref": tx.source_row_ref,
        },
        "receipt": {
            "receipt_id": receipt.id,
            "original_file_name": receipt.original_file_name,
            "storage_path": receipt.storage_path,
            "extracted_date": receipt.extracted_date.isoformat() if receipt.extracted_date else None,
            "extracted_supplier": receipt.extracted_supplier,
            "extracted_local_amount": receipt.extracted_local_amount,
            "extracted_currency": receipt.extracted_currency,
            "ocr_confidence": receipt.ocr_confidence,
        },
        "match": match_payload,
    }
    # F-AI-0b-2: surface advisory AI second-read context only when an
    # AgentDB review exists for this receipt. Never present on
    # statement-only/unmatched rows. Never affects attention_required,
    # confirm_review_session, or report_validation.
    ai_review = latest_ai_review_for_receipt(session, receipt)
    if ai_review is not None:
        source["ai_review"] = ai_review

    # F-AI-TG-2: deterministic Telegram reply draft preview for the row.
    # Display-only; ``send_allowed`` is always False from the draft engine.
    # The draft input is assembled from the row's existing source block plus
    # a small receipt-context dict so build_review_row_reply_draft() can
    # reach both the deterministic safety codes and the per-receipt
    # business-context fields. This NEVER calls a model, NEVER sends a
    # Telegram message, NEVER mutates canonical rows, and NEVER affects
    # attention_required, confirm_review_session, or report_validation.
    telegram_draft = build_review_row_reply_draft(
        {
            "source": source,
            "receipt": {
                "business_or_personal": receipt.business_or_personal,
                "business_reason": receipt.business_reason,
                "attendees": receipt.attendees,
                "report_bucket": receipt.report_bucket
                or suggest_bucket(tx.supplier_raw),
            },
        }
    )
    if telegram_draft is not None:
        source["telegram_draft"] = telegram_draft
    suggested = {
        "transaction_id": tx.id,
        "receipt_id": receipt.id,
        "receipt_path": receipt.storage_path,
        "receipt_file_name": receipt.original_file_name or f"receipt_{receipt.id}",
        "transaction_date": tx_date.isoformat() if tx_date else None,
        "supplier": tx.supplier_raw,
        "amount": amount,
        "currency": currency,
        "business_or_personal": receipt.business_or_personal,
        "report_bucket": receipt.report_bucket or suggest_bucket(tx.supplier_raw),
        "business_reason": receipt.business_reason,
        "attendees": receipt.attendees,
        "match_confidence": decision.confidence,
        "review_status": "suggested",
        **_default_air_travel(tx_date.isoformat() if tx_date else None),
        **_default_meal_detail(),
    }
    return source, suggested


def get_or_create_review_session(
    session: Session, *, expense_report_id: int
) -> ReviewSession:
    """Return the current review session for an expense report, creating one if needed.

    The ``expense_report_id`` is the primary operational key. For
    ``report_kind='diners_statement'`` reports, the underlying
    ``statement_import_id`` is read off the ExpenseReport and copied onto
    the ReviewSession so row-sync can walk the statement transactions.
    For ``report_kind='personal_reimbursement'``, no statement exists, so
    the review session is created empty (no rows synced from transactions).
    """
    report = session.get(ExpenseReport, expense_report_id)
    if report is None:
        raise ValueError(f"ExpenseReport {expense_report_id} not found")

    existing = _latest_session(session, expense_report_id)
    if existing:
        if existing.status != "confirmed":
            _sync_review_rows(session, existing)
        return existing

    review = ReviewSession(
        expense_report_id=expense_report_id,
        statement_import_id=report.statement_import_id,
        status="draft",
    )
    session.add(review)
    session.commit()
    session.refresh(review)
    _sync_review_rows(session, review)
    session.refresh(review)
    return review


def _sync_review_rows(session: Session, review: ReviewSession) -> None:
    # Personal-reimbursement reports carry no statement, so there are no
    # transactions to sync rows from. Leave the review session empty.
    if review.statement_import_id is None:
        session.commit()
        return

    existing_rows_by_tx: dict[int, ReviewRow] = {
        row.statement_transaction_id: row
        for row in review_rows(session, review.id or 0)
    }
    transactions = [
        tx
        for tx in session.exec(
            select(StatementTransaction)
            .where(StatementTransaction.statement_import_id == review.statement_import_id)
            .order_by(StatementTransaction.transaction_date, StatementTransaction.id)
        ).all()
        if tx.id is not None
    ]
    transaction_by_id = {tx.id: tx for tx in transactions if tx.id is not None}
    approved_by_transaction: dict[int, list[MatchDecision]] = {}
    for decision in session.exec(select(MatchDecision).where(MatchDecision.approved == True)).all():  # noqa: E712
        if decision.statement_transaction_id in transaction_by_id:
            approved_by_transaction.setdefault(decision.statement_transaction_id, []).append(decision)

    # Late-match upgrades only fire while the session itself is still a draft.
    # Confirmed sessions carry a snapshot; rewriting their rows would silently
    # drift from the snapshot hash.
    session_is_draft = review.status == "draft"

    for tx in transactions:
        if tx.id is None:
            continue
        decisions = sorted(approved_by_transaction.get(tx.id, []), key=lambda item: item.id or 0)
        decision = decisions[0] if decisions else None
        receipt = session.get(ReceiptDocument, decision.receipt_document_id) if decision else None
        has_full_match = bool(
            decision and receipt and receipt.id is not None and decision.id is not None
        )

        existing_row = existing_rows_by_tx.get(tx.id)
        if existing_row is not None:
            # Upgrade an existing row only when it is still untouched and a
            # match has since been approved. Rows the user has edited or that
            # belong to a confirmed session are left alone.
            upgradable = (
                session_is_draft
                and existing_row.status not in ("edited", "confirmed")
                and existing_row.receipt_document_id is None
                and has_full_match
            )
            if not upgradable:
                continue
            source, suggested = _row_payload(session, tx, receipt, decision)
            missing = _missing_required_fields(suggested)
            safety_issues = _source_receipt_statement_issues(_dumps(source))
            existing_row.receipt_document_id = receipt.id
            existing_row.match_decision_id = decision.id
            existing_row.status = (
                "needs_review" if missing or safety_issues or decision.confidence != "high" else "suggested"
            )
            existing_row.attention_required = bool(missing or safety_issues)
            existing_row.attention_note = _attention_note(missing, safety_issues)
            existing_row.source_json = _dumps(source)
            existing_row.suggested_json = _dumps(suggested)
            existing_row.confirmed_json = _dumps(suggested)
            existing_row.updated_at = datetime.now(timezone.utc)
            session.add(existing_row)
            continue

        if has_full_match:
            source, suggested = _row_payload(session, tx, receipt, decision)
            receipt_id = receipt.id
            decision_id = decision.id
            match_confidence = decision.confidence
        else:
            source, suggested = _statement_payload(tx)
            receipt_id = None
            decision_id = None
            match_confidence = None
        missing = _missing_required_fields(suggested)
        safety_issues = _source_receipt_statement_issues(_dumps(source))
        row = ReviewRow(
            review_session_id=review.id or 0,
            statement_transaction_id=tx.id,
            receipt_document_id=receipt_id,
            match_decision_id=decision_id,
            status="needs_review" if missing or safety_issues or match_confidence != "high" else "suggested",
            attention_required=bool(missing or safety_issues) or decision is None,
            attention_note=_attention_note(missing, safety_issues),
            source_json=_dumps(source),
            suggested_json=_dumps(suggested),
            confirmed_json=_dumps(suggested),
        )
        session.add(row)
    session.commit()


def review_rows(session: Session, review_session_id: int) -> list[ReviewRow]:
    return session.exec(
        select(ReviewRow).where(ReviewRow.review_session_id == review_session_id).order_by(ReviewRow.id)
    ).all()


def session_payload(session: Session, review: ReviewSession) -> dict[str, Any]:
    rows = []
    for row in review_rows(session, review.id or 0):
        source = _public_payload(_loads(row.source_json))
        if row.receipt_document_id is not None and "ai_review" not in source:
            receipt = session.get(ReceiptDocument, row.receipt_document_id)
            if receipt is not None:
                ai_review = latest_ai_review_for_receipt(session, receipt)
                if ai_review is not None:
                    source["ai_review"] = _public_payload(ai_review)
        rows.append(
            {
                "id": row.id,
                "status": row.status,
                "attention_required": row.attention_required,
                "attention_note": row.attention_note,
                "source": source,
                "suggested": _public_payload(_loads(row.suggested_json)),
                "confirmed": _public_payload(_loads(row.confirmed_json)),
            }
        )
    return {
        "id": review.id,
        "statement_import_id": review.statement_import_id,
        "status": review.status,
        "confirmed_at": review.confirmed_at,
        "confirmed_by_user_id": review.confirmed_by_user_id,
        "confirmed_by_label": review.confirmed_by_label,
        "snapshot_hash": review.snapshot_hash,
        "rows": rows,
    }


def _invalidate(review: ReviewSession) -> None:
    review.status = "draft"
    review.confirmed_at = None
    review.confirmed_by_user_id = None
    review.confirmed_by_label = None
    review.snapshot_json = None
    review.snapshot_hash = None
    review.updated_at = datetime.now(timezone.utc)


def update_review_row(
    session: Session,
    row_id: int,
    fields: dict[str, Any] | None = None,
    attention_required: bool | None = None,
    attention_note: str | None = None,
) -> ReviewRow:
    row = session.get(ReviewRow, row_id)
    if not row:
        raise ValueError("Review row not found")
    review = session.get(ReviewSession, row.review_session_id)
    if not review:
        raise ValueError("Review session not found")

    confirmed = _loads(row.confirmed_json)
    if fields:
        for key, value in fields.items():
            if key in confirmed or key in AIR_TRAVEL_FIELDS or key in MEAL_DETAIL_FIELDS:
                confirmed[key] = value
        duplicate_key = _meal_duplicate_key(confirmed)
        if duplicate_key:
            for other_row in review_rows(session, row.review_session_id):
                if other_row.id == row.id:
                    continue
                other_confirmed = _loads(other_row.confirmed_json)
                if _meal_duplicate_key(other_confirmed) == duplicate_key:
                    tx_date, bucket = duplicate_key
                    suggestions = _other_meal_bucket_suggestions(bucket)
                    raise ValueError(
                        f"Only one {bucket} expense is allowed on {tx_date}. "
                        f"Try {suggestions} for additional receipts."
                    )
        row.confirmed_json = _dumps(confirmed)
        row.status = "edited"
    missing = _missing_required_fields(confirmed)
    safety_issues = _source_receipt_statement_issues(row.source_json)
    safety_acknowledged = attention_required is False
    if missing:
        row.attention_required = True
        row.attention_note = _attention_note(missing, safety_issues)
        row.status = "needs_review"
    elif safety_issues and not safety_acknowledged:
        row.attention_required = True
        row.attention_note = receipt_statement_issue_note(safety_issues)
        row.status = "needs_review"
    else:
        row.attention_required = False
        row.attention_note = None
        row.status = "edited" if fields or attention_required is not None or attention_note is not None else row.status
    row.updated_at = datetime.now(timezone.utc)
    _invalidate(review)
    session.add(row)
    session.add(review)
    session.commit()
    session.refresh(row)
    return row


def bulk_update_review_rows(
    session: Session,
    review_session_id: int,
    fields: dict[str, Any],
    scope: str = "attention_required",
    row_ids: list[int] | None = None,
) -> dict[str, int]:
    review = session.get(ReviewSession, review_session_id)
    if not review:
        raise ValueError("Review session not found")
    if scope not in {"attention_required", "all", "selected"}:
        raise ValueError("Bulk update scope must be attention_required, all, or selected")
    if scope == "selected" and not row_ids:
        raise ValueError("Bulk update with selected scope requires row_ids")

    rows = review_rows(session, review_session_id)
    updated = 0
    for row in rows:
        if scope == "attention_required" and not row.attention_required:
            continue
        if scope == "selected" and row.id not in row_ids:
            continue
        update_review_row(session, row.id or 0, fields=fields)
        updated += 1

    remaining_attention = sum(1 for row in review_rows(session, review_session_id) if row.attention_required)
    return {"updated_rows": updated, "remaining_attention_rows": remaining_attention}


def confirm_review_session(
    session: Session,
    review_session_id: int,
    confirmed_by_user_id: int | None = None,
    confirmed_by_label: str | None = None,
) -> ReviewSession:
    review = session.get(ReviewSession, review_session_id)
    if not review:
        raise ValueError("Review session not found")
    rows = review_rows(session, review_session_id)
    if not rows:
        raise ValueError("Review session has no rows to confirm")
    if any(row.attention_required for row in rows):
        raise ValueError("Review session has rows marked for attention")

    snapshot: list[dict[str, Any]] = []
    for row in rows:
        confirmed = _loads(row.confirmed_json)
        missing = _missing_required_fields(confirmed)
        if missing:
            raise ValueError(f"Review row {row.id} is missing required fields: {', '.join(missing)}")
        confirmed["review_row_id"] = row.id
        confirmed["review_session_id"] = review.id
        snapshot.append(confirmed)
        row.status = "confirmed"
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)

    snapshot_json = _dumps(snapshot)
    review.status = "confirmed"
    review.snapshot_json = snapshot_json
    review.snapshot_hash = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
    review.confirmed_by_user_id = confirmed_by_user_id
    review.confirmed_by_label = confirmed_by_label
    review.confirmed_at = datetime.now(timezone.utc)
    review.updated_at = datetime.now(timezone.utc)
    session.add(review)
    session.commit()
    session.refresh(review)
    return review


def confirmed_snapshot(
    session: Session, *, expense_report_id: int
) -> tuple[ReviewSession, list[dict[str, Any]]]:
    review = _latest_session(session, expense_report_id)
    if not review or review.status != "confirmed" or not review.snapshot_json:
        raise ValueError("Report generation requires confirmed review data")
    return review, json.loads(review.snapshot_json)
