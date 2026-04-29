from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Mapping

from sqlmodel import Session, select

from app.json_utils import dumps
from app.models import (
    AgentReceiptComparison,
    AgentReceiptRead as AgentReceiptReadRow,
    AgentReceiptReviewRun,
    ReceiptDocument,
    utc_now,
)
from app.services.agent_receipt_reviewer import (
    AgentReceiptRead as AgentReceiptReadPayload,
    AgentReceiptReviewResult,
    build_agent_receipt_review_prompt,
    compare_agent_receipt_read,
)

SCHEMA_VERSION = "0a"
PROMPT_VERSION = "agent_receipt_review_prompt_0a"
COMPARATOR_VERSION = "agent_receipt_comparator_0a"


@dataclass(frozen=True)
class AgentReceiptReviewWriteResult:
    run: AgentReceiptReviewRun
    result: AgentReceiptReviewResult | None
    error: str | None = None


def build_canonical_receipt_snapshot(receipt: ReceiptDocument) -> dict[str, Any]:
    return {
        "date": receipt.extracted_date.isoformat() if receipt.extracted_date else None,
        "supplier": receipt.extracted_supplier,
        "amount": _decimal_to_string(receipt.extracted_local_amount),
        "currency": receipt.extracted_currency,
        "receipt_type": receipt.receipt_type,
        "business_or_personal": receipt.business_or_personal,
        "business_reason": receipt.business_reason,
        "attendees": receipt.attendees,
    }


def canonical_receipt_snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    return _sha256_text(_stable_json(snapshot))


def get_latest_agent_receipt_comparison(
    session: Session,
    receipt_document_id: int,
) -> AgentReceiptComparison | None:
    statement = (
        select(AgentReceiptComparison)
        .join(AgentReceiptReviewRun, AgentReceiptComparison.run_id == AgentReceiptReviewRun.id)
        .where(
            AgentReceiptComparison.receipt_document_id == receipt_document_id,
            AgentReceiptReviewRun.status == "completed",
        )
        .order_by(AgentReceiptReviewRun.completed_at.desc(), AgentReceiptReviewRun.id.desc())
    )
    return session.exec(statement).first()


def write_mock_agent_receipt_review(
    session: Session,
    *,
    receipt: ReceiptDocument,
    agent_json_text: str,
    run_source: str = "local_cli",
    store_raw_model_json: bool = False,
    store_prompt_text: bool = False,
    app_git_sha: str | None = None,
) -> AgentReceiptReviewWriteResult:
    snapshot = build_canonical_receipt_snapshot(receipt)
    snapshot_json = _stable_json(snapshot)
    prompt_text = build_agent_receipt_review_prompt(snapshot)
    now = utc_now()
    run = AgentReceiptReviewRun(
        receipt_document_id=receipt.id or 0,
        run_source=run_source,
        run_kind="receipt_second_read",
        status="started",
        schema_version=SCHEMA_VERSION,
        prompt_version=PROMPT_VERSION,
        prompt_hash=_sha256_text(prompt_text),
        model_provider=None,
        model_name="local_mock",
        comparator_version=COMPARATOR_VERSION,
        app_git_sha=app_git_sha,
        canonical_snapshot_json=snapshot_json,
        input_hash=_sha256_text(_stable_json({"canonical": snapshot, "agent_json": agent_json_text})),
        raw_model_json_redacted=not store_raw_model_json,
        raw_model_json=agent_json_text if store_raw_model_json else None,
        prompt_text=prompt_text if store_prompt_text else None,
        started_at=now,
    )
    session.add(run)
    session.flush()

    try:
        agent_payload = json.loads(agent_json_text)
        if not isinstance(agent_payload, dict):
            raise ValueError("agent JSON must contain an object")

        agent_read = AgentReceiptReadPayload.from_dict(agent_payload)
        review_result = compare_agent_receipt_read(snapshot, agent_read)
        read_row = _build_agent_read_row(run, receipt, agent_payload, review_result)
        session.add(read_row)
        session.flush()
        comparison_row = _build_comparison_row(run, read_row, receipt, review_result, snapshot)
        session.add(comparison_row)
        run.status = "completed"
        run.completed_at = utc_now()
        session.flush()
        return AgentReceiptReviewWriteResult(run=run, result=review_result)
    except Exception as exc:
        run.status = "failed"
        run.error_code = "agent_review_failed"
        run.error_message = _redacted_error_message(exc)
        run.completed_at = utc_now()
        session.flush()
        return AgentReceiptReviewWriteResult(run=run, result=None, error=run.error_message)


def _build_agent_read_row(
    run: AgentReceiptReviewRun,
    receipt: ReceiptDocument,
    agent_payload: Mapping[str, Any],
    review_result: AgentReceiptReviewResult,
) -> AgentReceiptReadRow:
    read = review_result.agent_read
    amount_scale = _amount_scale(read.total_amount)
    return AgentReceiptReadRow(
        run_id=run.id or 0,
        receipt_document_id=receipt.id or 0,
        read_schema_version=review_result.schema_version,
        read_json=dumps(read.to_dict(), sort_keys=True),
        extracted_date=read.receipt_date,
        extracted_supplier=read.merchant_name,
        amount_text=read.amount_text,
        local_amount_decimal=_decimal_to_string(read.total_amount),
        local_amount_minor=_amount_minor(read.total_amount, amount_scale),
        amount_scale=amount_scale,
        currency=read.currency,
        receipt_type=read.receipt_category,
        business_or_personal=None,
        business_reason=None,
        attendees_json=None,
        confidence_json=dumps({"confidence": agent_payload.get("confidence")}, sort_keys=True),
        evidence_json=dumps(
            {
                "amount_text": read.amount_text,
                "merchant_address": read.merchant_address,
                "raw_text_summary": read.raw_text_summary,
                "line_items": read.line_items,
            },
            sort_keys=True,
        ),
        warnings_json="[]",
    )


def _build_comparison_row(
    run: AgentReceiptReviewRun,
    read_row: AgentReceiptReadRow,
    receipt: ReceiptDocument,
    review_result: AgentReceiptReviewResult,
    snapshot: Mapping[str, Any],
) -> AgentReceiptComparison:
    comparison = review_result.comparison
    differences = comparison.differences
    return AgentReceiptComparison(
        run_id=run.id or 0,
        agent_receipt_read_id=read_row.id or 0,
        receipt_document_id=receipt.id or 0,
        comparator_version=COMPARATOR_VERSION,
        risk_level=comparison.risk_level,
        recommended_action=comparison.recommended_action,
        attention_required=comparison.risk_level != "pass",
        amount_status=_field_status(comparison.amount_match, differences, "amount"),
        date_status=_field_status(comparison.date_match, differences, "date"),
        currency_status=_field_status(comparison.currency_match, differences, "currency"),
        supplier_status=_field_status(comparison.supplier_match, differences, "supplier"),
        business_context_status="missing"
        if {"missing_business_reason", "missing_attendees"} & set(differences)
        else "complete",
        differences_json=dumps(differences, sort_keys=True),
        suggested_user_message=comparison.suggested_user_message,
        ai_review_note=None,
        canonical_snapshot_hash=canonical_receipt_snapshot_hash(snapshot),
        agent_read_hash=_sha256_text(_stable_json(review_result.agent_read.to_dict())),
    )


def _field_status(is_match: bool, differences: list[str], field: str) -> str:
    if is_match:
        return "match"
    if any(item.startswith(f"missing_agent_{field}") or item.startswith(f"missing_canonical_{field}") for item in differences):
        return "missing"
    return "mismatch"


def _decimal_to_string(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _amount_scale(value: Decimal | None) -> int | None:
    if value is None:
        return None
    return max(0, -value.as_tuple().exponent)


def _amount_minor(value: Decimal | None, scale: int | None) -> int | None:
    if value is None or scale is None:
        return None
    return int(value * (Decimal(10) ** scale))


def _stable_json(payload: Any) -> str:
    return dumps(payload, sort_keys=True, separators=(",", ":"))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _redacted_error_message(exc: Exception) -> str:
    text = str(exc).replace("\r", " ").replace("\n", " ").strip()
    return text[:500] if text else exc.__class__.__name__
