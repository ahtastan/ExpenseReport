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


# F-AI-0b-2 difference -> field/severity classification. Mirrors the warn/block
# split used by agent_receipt_reviewer's comparator.
_AI_DIFFERENCE_FIELD_AND_SEVERITY: dict[str, tuple[str, str]] = {
    "amount_mismatch": ("amount", "block"),
    "missing_canonical_amount": ("amount", "block"),
    "missing_agent_amount": ("amount", "block"),
    "currency_mismatch": ("currency", "block"),
    "missing_canonical_currency": ("currency", "block"),
    "missing_agent_currency": ("currency", "block"),
    "date_mismatch": ("date", "warn"),
    "missing_canonical_date": ("date", "warn"),
    "missing_agent_date": ("date", "warn"),
    "supplier_mismatch": ("supplier", "warn"),
    "missing_canonical_supplier": ("supplier", "warn"),
    "missing_agent_supplier": ("supplier", "warn"),
    "missing_business_reason": ("business_context", "warn"),
    "missing_attendees": ("business_context", "warn"),
}

# Whitelist of recommended_action codes the public API may surface. Any
# unrecognised value coming from the comparator is dropped to avoid leaking
# new internal vocabulary by accident.
_AI_PUBLIC_RECOMMENDED_ACTIONS = {
    "accept",
    "ask_user",
    "manual_review",
    "block_report",
}

# Whitelist of risk_level codes. Anything else makes the row "malformed".
_AI_PUBLIC_RISK_LEVELS = {"pass", "warn", "block"}


def latest_ai_review_for_receipt(
    session: Session,
    receipt: ReceiptDocument,
) -> dict[str, Any] | None:
    """Return the public AI-second-read dict for a receipt's review-row payload.

    F-AI-0b-2 advisory display only. This helper:
      * never writes to the DB,
      * never calls models,
      * never mutates the receipt,
      * returns ``None`` when there is no signal worth surfacing,
      * returns a synthetic ``status`` (pass/warn/block/stale/malformed) plus
        the safe public projection of agent vs canonical values.

    The return shape is documented in the F-AI-0b-2 design report:
    ``status`` is always present; ``differences``/``summary`` are omitted when
    empty/null; nothing internal (prompt_text, raw_model_json, hashes,
    storage paths) is ever included.
    """
    if receipt is None or receipt.id is None:
        return None

    completed_comparison = get_latest_agent_receipt_comparison(session, receipt.id)

    if completed_comparison is None:
        # No completed comparison exists. If the latest run for this receipt
        # is failed/incomplete, surface a quiet "malformed" status so the UI
        # can mark it as ignored. Otherwise omit the field entirely.
        latest_run = _latest_run_for_receipt(session, receipt.id)
        if latest_run is None:
            return None
        if latest_run.status in {"completed"}:
            # Defensive: completed run but no comparison row — broken state.
            return _malformed_payload(latest_run)
        if latest_run.status in {"failed", "started"}:
            return _malformed_payload(latest_run)
        return None

    run = session.get(AgentReceiptReviewRun, completed_comparison.run_id)
    if run is None:
        # Comparison without parent run is broken state.
        return _malformed_payload(None)

    # Stale detection: recompute the canonical snapshot hash from the current
    # receipt state. If it differs from the snapshot recorded with the run,
    # the receipt was edited after the AI second-read was produced.
    current_hash = canonical_receipt_snapshot_hash(build_canonical_receipt_snapshot(receipt))
    if (
        completed_comparison.canonical_snapshot_hash
        and current_hash != completed_comparison.canonical_snapshot_hash
    ):
        return _stale_payload(run)

    risk_level = completed_comparison.risk_level
    if risk_level not in _AI_PUBLIC_RISK_LEVELS:
        return _malformed_payload(run)

    differences = _coerce_differences(completed_comparison.differences_json)

    canonical_snapshot = _safe_load_canonical_snapshot(run.canonical_snapshot_json)
    agent_read_row = _latest_agent_read_for_run(session, run.id)

    payload: dict[str, Any] = {
        "status": risk_level,
        "label": "AI second read",
        "risk_level": risk_level,
    }

    action = completed_comparison.recommended_action
    if action in _AI_PUBLIC_RECOMMENDED_ACTIONS:
        # Public surface uses "review" instead of internal "manual_review".
        payload["recommended_action"] = "review" if action == "manual_review" else action

    summary = (completed_comparison.suggested_user_message or "").strip()
    if summary:
        payload["summary"] = summary

    if run.completed_at is not None:
        payload["completed_at"] = _isoformat(run.completed_at)

    rich_differences = _build_public_differences(
        differences,
        agent_read_row=agent_read_row,
        canonical_snapshot=canonical_snapshot,
    )
    if rich_differences:
        payload["differences"] = rich_differences

    agent_view = _public_agent_read(agent_read_row)
    if agent_view:
        payload["agent_read"] = agent_view

    canonical_view = _public_canonical_view(canonical_snapshot)
    if canonical_view:
        payload["canonical"] = canonical_view

    return payload


def _latest_run_for_receipt(
    session: Session, receipt_document_id: int
) -> AgentReceiptReviewRun | None:
    statement = (
        select(AgentReceiptReviewRun)
        .where(AgentReceiptReviewRun.receipt_document_id == receipt_document_id)
        .order_by(
            AgentReceiptReviewRun.completed_at.desc(),
            AgentReceiptReviewRun.id.desc(),
        )
    )
    return session.exec(statement).first()


def _latest_agent_read_for_run(
    session: Session, run_id: int | None
) -> AgentReceiptReadRow | None:
    if run_id is None:
        return None
    statement = (
        select(AgentReceiptReadRow)
        .where(AgentReceiptReadRow.run_id == run_id)
        .order_by(AgentReceiptReadRow.id.desc())
    )
    return session.exec(statement).first()


def _stale_payload(run: AgentReceiptReviewRun) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "stale",
        "label": "AI second read",
    }
    if run.completed_at is not None:
        payload["completed_at"] = _isoformat(run.completed_at)
    return payload


def _malformed_payload(run: AgentReceiptReviewRun | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "malformed",
        "label": "AI second read",
    }
    if run is not None and run.completed_at is not None:
        payload["completed_at"] = _isoformat(run.completed_at)
    return payload


def _coerce_differences(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, str)]


def _safe_load_canonical_snapshot(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _build_public_differences(
    codes: list[str],
    *,
    agent_read_row: AgentReceiptReadRow | None,
    canonical_snapshot: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rich: list[dict[str, Any]] = []
    for code in codes:
        field, severity = _AI_DIFFERENCE_FIELD_AND_SEVERITY.get(code, ("unknown", "warn"))
        diff: dict[str, Any] = {
            "code": code,
            "field": field,
            "severity": severity,
        }
        agent_value, canonical_value = _difference_values(field, agent_read_row, canonical_snapshot)
        if agent_value is not None or canonical_value is not None:
            diff["agent_value"] = agent_value
            diff["canonical_value"] = canonical_value
        rich.append(diff)
    return rich


def _difference_values(
    field: str,
    agent_read_row: AgentReceiptReadRow | None,
    canonical_snapshot: Mapping[str, Any],
) -> tuple[Any, Any]:
    if field == "amount":
        agent_value = agent_read_row.local_amount_decimal if agent_read_row else None
        canonical_value = canonical_snapshot.get("amount") or None
        return agent_value, canonical_value
    if field == "currency":
        agent_value = agent_read_row.currency if agent_read_row else None
        canonical_value = canonical_snapshot.get("currency") or None
        return agent_value, canonical_value
    if field == "date":
        agent_date = agent_read_row.extracted_date if agent_read_row else None
        agent_value = agent_date.isoformat() if isinstance(agent_date, date) else agent_date
        canonical_value = canonical_snapshot.get("date") or None
        return agent_value, canonical_value
    if field == "supplier":
        agent_value = agent_read_row.extracted_supplier if agent_read_row else None
        canonical_value = canonical_snapshot.get("supplier") or None
        return agent_value, canonical_value
    return None, None


def _public_agent_read(agent_read_row: AgentReceiptReadRow | None) -> dict[str, Any]:
    if agent_read_row is None:
        return {}
    view: dict[str, Any] = {}
    if agent_read_row.extracted_date is not None:
        view["date"] = agent_read_row.extracted_date.isoformat()
    if agent_read_row.local_amount_decimal:
        view["amount"] = agent_read_row.local_amount_decimal
    if agent_read_row.currency:
        view["currency"] = agent_read_row.currency
    if agent_read_row.extracted_supplier:
        view["supplier"] = agent_read_row.extracted_supplier
    return view


def _public_canonical_view(canonical_snapshot: Mapping[str, Any]) -> dict[str, Any]:
    view: dict[str, Any] = {}
    for key in ("date", "amount", "currency", "supplier"):
        value = canonical_snapshot.get(key)
        if value:
            view[key] = value
    return view


def _isoformat(value: Any) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


def write_mock_agent_receipt_review(
    session: Session,
    *,
    receipt: ReceiptDocument,
    agent_json_text: str,
    run_source: str = "local_cli",
    store_raw_model_json: bool = False,
    store_prompt_text: bool = False,
    app_git_sha: str | None = None,
    prompt_text_override: str | None = None,
    model_provider: str | None = None,
    model_name: str = "local_mock",
    review_session_id: int | None = None,
    review_row_id: int | None = None,
    statement_transaction_id: int | None = None,
    statement_snapshot: Mapping[str, Any] | None = None,
) -> AgentReceiptReviewWriteResult:
    """Persist one local/mock shadow review without mutating canonical rows.

    The run row is created as ``started`` and finalized to ``completed`` or
    ``failed`` in the same transaction. Read/comparison rows are append-only
    artifacts and are inserted only for valid completed runs; reruns create a
    fresh run/read/comparison set.
    """

    snapshot = build_canonical_receipt_snapshot(receipt)
    snapshot_json = _stable_json(snapshot)
    prompt_text = prompt_text_override or build_agent_receipt_review_prompt(snapshot)
    statement_snapshot_json = _stable_json(statement_snapshot) if statement_snapshot is not None else None
    now = utc_now()
    run = AgentReceiptReviewRun(
        receipt_document_id=receipt.id or 0,
        review_session_id=review_session_id,
        review_row_id=review_row_id,
        statement_transaction_id=statement_transaction_id,
        run_source=run_source,
        run_kind="receipt_second_read",
        status="started",
        schema_version=SCHEMA_VERSION,
        prompt_version=PROMPT_VERSION,
        prompt_hash=_sha256_text(prompt_text),
        model_provider=model_provider,
        model_name=model_name,
        comparator_version=COMPARATOR_VERSION,
        app_git_sha=app_git_sha,
        canonical_snapshot_json=snapshot_json,
        statement_snapshot_json=statement_snapshot_json,
        input_hash=_sha256_text(
            _stable_json(
                {
                    "canonical": snapshot,
                    "statement": statement_snapshot,
                    "agent_json": agent_json_text,
                }
            )
        ),
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
