"""F-AI-Stage1 context window builder.

Builds the per-user context payload that the inline-keyboard AI prompt
uses to ground its classification proposals: known employees (display
names), recent classified receipts, and recent attendees.

Read-only. Never mutates ``ReceiptDocument`` / ``AppUser``. Does not call
the model. Designed to be cheap enough to run inline on every receipt
upload by an allowlisted user.

Reference: docs/F-AI-Stage1-Telegram-Inline-Keyboard.md §5.1
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlmodel import Session, select

from app.models import AppUser, ReceiptDocument, utc_now

_EMPLOYEE_LIMIT = 50
_RECENT_RECEIPTS_LIMIT = 20
_RECENT_ATTENDEES_LIMIT = 30
_ATTENDEE_SPLIT_RE = re.compile(r"[,;+]")


def build_context_window(
    session: Session,
    *,
    user_id: int,
    lookback_days: int = 2,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the per-user context payload for the inline-keyboard AI prompt.

    Args:
        session: open SQLModel session.
        user_id: AppUser.id to scope the receipt history to. Receipts uploaded
            by other users are NOT visible (multi-user isolation; prod is
            single-user today but the boundary is honored).
        lookback_days: window for ``recent_receipts`` (and the deduped
            ``recent_attendees`` derived from them). Default 2.
        now: injectable for tests. Defaults to ``utc_now()``.

    Returns:
        ``{
          "employees": [...],          # display names from AppUser
          "recent_receipts": [...],    # last N classified receipts for this user
          "recent_attendees": [...],   # deduped attendee names from those receipts
          "lookback_days": int,
          "fetched_at": ISO-8601 string,
        }``
    """
    fetch_time = now or utc_now()
    cutoff = fetch_time - timedelta(days=lookback_days)

    employees = _employees(session)
    receipts = _recent_receipts(session, user_id=user_id, cutoff=cutoff)
    recent_receipts = [_receipt_summary(receipt) for receipt in receipts]
    recent_attendees = _dedupe_attendees(receipts)

    return {
        "employees": employees,
        "recent_receipts": recent_receipts,
        "recent_attendees": recent_attendees,
        "lookback_days": lookback_days,
        "fetched_at": _isoformat(fetch_time),
    }


def _employees(session: Session) -> list[str]:
    rows = session.exec(select(AppUser).order_by(AppUser.id)).all()
    names: list[str] = []
    for row in rows:
        name = _employee_display_name(row)
        if name:
            names.append(name)
        if len(names) >= _EMPLOYEE_LIMIT:
            break
    return names


def _employee_display_name(user: AppUser) -> str | None:
    for candidate in (user.display_name, user.first_name, user.username):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    if user.id is not None:
        return f"user_{user.id}"
    return None


def _recent_receipts(
    session: Session,
    *,
    user_id: int,
    cutoff: datetime,
) -> list[ReceiptDocument]:
    # SQLite default ordering puts NULL last on DESC, which matches the
    # "date DESC NULLS LAST" intent of the spec without needing a
    # database-specific NULLS LAST clause. The "transaction_date" the spec
    # refers to lives on StatementTransaction; on ReceiptDocument the
    # equivalent is ``extracted_date`` (the date read off the receipt).
    statement = (
        select(ReceiptDocument)
        .where(
            ReceiptDocument.uploader_user_id == user_id,
            ReceiptDocument.business_or_personal.is_not(None),
            ReceiptDocument.created_at >= cutoff,
            ReceiptDocument.status != "cancelled",
        )
        .order_by(
            ReceiptDocument.extracted_date.desc(),
            ReceiptDocument.created_at.desc(),
        )
        .limit(_RECENT_RECEIPTS_LIMIT)
    )
    return list(session.exec(statement).all())


def _receipt_summary(receipt: ReceiptDocument) -> dict[str, Any]:
    if receipt.extracted_date is not None:
        date_str: str | None = receipt.extracted_date.isoformat()
    elif receipt.created_at is not None:
        date_str = receipt.created_at.date().isoformat()
    else:
        date_str = None
    return {
        "date": date_str,
        "supplier": receipt.extracted_supplier,
        "bucket": receipt.report_bucket,
        "business_or_personal": receipt.business_or_personal,
        "amount": float(receipt.extracted_local_amount)
        if receipt.extracted_local_amount is not None
        else None,
        "currency": receipt.extracted_currency,
    }


def _dedupe_attendees(receipts: list[ReceiptDocument]) -> list[str]:
    seen: dict[str, str] = {}  # lowercase key -> first-occurrence original casing
    for receipt in receipts:
        for name in _split_attendees(receipt.attendees):
            key = name.lower()
            if key not in seen:
                seen[key] = name
            if len(seen) >= _RECENT_ATTENDEES_LIMIT:
                break
        if len(seen) >= _RECENT_ATTENDEES_LIMIT:
            break
    return list(seen.values())


def _split_attendees(value: str | None) -> list[str]:
    if not isinstance(value, str):
        return []
    parts = _ATTENDEE_SPLIT_RE.split(value)
    cleaned: list[str] = []
    for part in parts:
        text = part.strip()
        if text:
            cleaned.append(text)
    return cleaned


def _isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()
