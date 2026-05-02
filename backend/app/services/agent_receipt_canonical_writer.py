"""F-AI-Stage1 sub-PR 3: write an AI proposal into canonical fields.

This is the **first** AgentDB → canonical write path in the project. All
canonical writes from this module are source-tagged via the
``*_source`` columns introduced in sub-PR 1.

Idempotent: calling twice with the same input is safe — second call
rewrites the same values with the same source tags.

Selective: when an individual ``suggested_*`` field is ``None`` (the
model declined to propose), that field on ``ReceiptDocument`` is left
untouched and its ``*_source`` column is NOT set. This prevents AI
uncertainty from blanking out values that may have been set elsewhere
(e.g. by the user in the review-table UI).

Reference: docs/F-AI-Stage1-Telegram-Inline-Keyboard.md §5.5
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlmodel import Session, select

from app.json_utils import dumps
from app.models import (
    AgentReceiptRead,
    ReceiptDocument,
    ReviewRow,
    utc_now,
)

logger = logging.getLogger(__name__)


class CanonicalWriteLinkageError(RuntimeError):
    """Raised when an AI read is not linked to the receipt being written."""


_ALLOWED_SOURCE_TAGS = {
    "user",
    "telegram_user",
    "ai_advisory",
    "auto_confirmed_default",
    "matching",
    "auto_suggester",
    "legacy_unknown",
}


def write_ai_proposal_to_canonical(
    session: Session,
    *,
    receipt: ReceiptDocument,
    agent_read: AgentReceiptRead,
    source_tag: str,
    expected_review_run_id: int | None = None,
) -> dict[str, Any]:
    """Write the AI proposal into the canonical receipt + review row.

    Args:
        session: open SQLModel session. Caller is responsible for commit.
        receipt: the canonical ``ReceiptDocument`` to update.
        agent_read: the AI proposal carrier (``suggested_*`` columns).
        source_tag: one of ``ai_advisory`` (Confirm-button) or
            ``auto_confirmed_default`` (timeout / supersede). Other
            values from the source-tag vocabulary are accepted for
            forward compatibility.
        expected_review_run_id: optional linkage guard used by
            ``AgentReceiptUserResponse`` callers.

    Returns:
        The dict that was persisted, suitable for storage in
        ``AgentReceiptUserResponse.canonical_write_json`` as an audit
        trail of what changed.
    """
    if agent_read.receipt_document_id != receipt.id:
        logger.error(
            "write_ai_proposal_to_canonical: linkage mismatch - "
            "agent_read.id=%s claims receipt_document_id=%s but caller "
            "passed receipt.id=%s; refusing to write",
            agent_read.id,
            agent_read.receipt_document_id,
            receipt.id,
        )
        raise CanonicalWriteLinkageError(
            f"agent_read {agent_read.id} does not belong to receipt {receipt.id}"
        )
    if expected_review_run_id is not None and agent_read.run_id != expected_review_run_id:
        logger.error(
            "write_ai_proposal_to_canonical: review-run linkage mismatch - "
            "agent_read.id=%s claims run_id=%s but caller expected run_id=%s; "
            "refusing to write",
            agent_read.id,
            agent_read.run_id,
            expected_review_run_id,
        )
        raise CanonicalWriteLinkageError(
            f"agent_read {agent_read.id} does not belong to review run "
            f"{expected_review_run_id}"
        )

    if source_tag not in _ALLOWED_SOURCE_TAGS:
        raise ValueError(f"unknown source_tag: {source_tag!r}")

    suggestion = _suggestion_view(agent_read)
    written: dict[str, Any] = {"source_tag": source_tag, "fields": {}}

    if suggestion["business_or_personal"] is not None:
        receipt.business_or_personal = suggestion["business_or_personal"]
        receipt.category_source = source_tag
        written["fields"]["business_or_personal"] = suggestion["business_or_personal"]

    if suggestion["report_bucket"] is not None:
        receipt.report_bucket = suggestion["report_bucket"]
        receipt.bucket_source = source_tag
        written["fields"]["report_bucket"] = suggestion["report_bucket"]

    attendees_value = _attendees_to_canonical_string(suggestion["attendees"])
    if attendees_value is not None:
        receipt.attendees = attendees_value
        receipt.attendees_source = source_tag
        written["fields"]["attendees"] = attendees_value

    if suggestion["business_reason"] is not None:
        receipt.business_reason = suggestion["business_reason"]
        receipt.business_reason_source = source_tag
        written["fields"]["business_reason"] = suggestion["business_reason"]

    # Record the customer alongside business_reason for audit. The
    # canonical schema has no ``customer`` column today; storing it in
    # the audit payload keeps the information without blanking other
    # fields. ``ReceiptDocument.business_reason`` already incorporates
    # customer context when the model included one.
    if suggestion["customer"] is not None:
        written["fields"]["customer"] = suggestion["customer"]

    # Once the AI proposal is accepted (or auto-confirmed) the receipt
    # no longer needs the clarification-question follow-up flow. Always
    # clear ``needs_clarification`` whenever any field is written, so
    # the legacy "Was this business or personal..." prompts don't
    # re-fire on a Telegram user who already used the keyboard.
    if written["fields"]:
        receipt.needs_clarification = False
        receipt.updated_at = utc_now()
        session.add(receipt)

    review_row_payload = _merge_into_review_row(
        session,
        receipt=receipt,
        agent_read=agent_read,
        suggestion=suggestion,
        attendees_value=attendees_value,
        source_tag=source_tag,
    )
    if review_row_payload is not None:
        written["review_row_id"] = review_row_payload["review_row_id"]

    return written


def _suggestion_view(agent_read: AgentReceiptRead) -> dict[str, Any]:
    return {
        "business_or_personal": _clean_optional(agent_read.suggested_business_or_personal),
        "report_bucket": _clean_optional(agent_read.suggested_report_bucket),
        "attendees": _decode_attendees(agent_read.suggested_attendees_json),
        "customer": _clean_optional(agent_read.suggested_customer),
        "business_reason": _clean_optional(agent_read.suggested_business_reason),
        "confidence_overall": agent_read.suggested_confidence_overall,
    }


def _attendees_to_canonical_string(attendees: list[str] | None) -> str | None:
    """Join the suggested attendee list into the canonical ``" + "``-style.

    Returns ``None`` (skip-write semantics) when the AI did not propose
    an attendee list. Returns an empty string when the AI proposed an
    empty list (a positive "no attendees" signal — for example, a fuel
    receipt). ``ReceiptDocument.attendees`` holds the canonical string
    used by validation downstream.
    """
    if attendees is None:
        return None
    if not attendees:
        return ""
    return " + ".join(attendees)


def _decode_attendees(raw_json: str | None) -> list[str] | None:
    if raw_json is None:
        return None
    try:
        decoded = json.loads(raw_json)
    except json.JSONDecodeError:
        logger.warning("invalid suggested_attendees_json: %r", raw_json[:200])
        return None
    if not isinstance(decoded, list):
        return None
    return [item.strip() for item in decoded if isinstance(item, str) and item.strip()]


def _clean_optional(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return value


def _merge_into_review_row(
    session: Session,
    *,
    receipt: ReceiptDocument,
    agent_read: AgentReceiptRead,
    suggestion: dict[str, Any],
    attendees_value: str | None,
    source_tag: str,
) -> dict[str, Any] | None:
    """Merge the AI proposal into the receipt's ``ReviewRow.confirmed_json``.

    No-op when no ``ReviewRow`` is associated with this receipt yet
    (Telegram-only receipts often don't have a row until a statement is
    imported). When a row exists, merge the proposed fields plus the
    four ``*_source`` keys; existing keys not in the proposal are
    preserved.
    """
    if receipt.id is None:
        return None
    review_row = session.exec(
        select(ReviewRow).where(ReviewRow.receipt_document_id == receipt.id)
    ).first()
    if review_row is None:
        return None

    confirmed = _safe_load_json_object(review_row.confirmed_json)

    if suggestion["business_or_personal"] is not None:
        confirmed["business_or_personal"] = suggestion["business_or_personal"]
        confirmed["category_source"] = source_tag
    if suggestion["report_bucket"] is not None:
        confirmed["report_bucket"] = suggestion["report_bucket"]
        confirmed["bucket_source"] = source_tag
    if attendees_value is not None:
        confirmed["attendees"] = attendees_value
        confirmed["attendees_source"] = source_tag
    if suggestion["business_reason"] is not None:
        confirmed["business_reason"] = suggestion["business_reason"]
        confirmed["business_reason_source"] = source_tag
    if suggestion["customer"] is not None:
        confirmed["customer"] = suggestion["customer"]

    review_row.confirmed_json = dumps(confirmed, sort_keys=True)
    review_row.updated_at = utc_now()
    session.add(review_row)
    return {"review_row_id": review_row.id}


def _safe_load_json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
