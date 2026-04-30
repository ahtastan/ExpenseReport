"""F-AI-TG-0 Telegram AI reply draft engine.

Deterministic templates for Telegram-shaped responses. Pure functions, no
side effects, no model calls, no Telegram client. The output is a draft
that some future caller MAY review and choose to send; this module never
sends anything itself, and ``send_allowed`` is always ``False``.

Two public entry points:

  * ``build_receipt_reply_draft`` — receipt-only context (missing business
    reason / missing attendees on a Business meal/expense).
  * ``build_review_row_reply_draft`` — review-row context (deterministic
    receipt-vs-statement safety issues from PR #55, plus advisory AI
    second-read warnings from PR #57).

Both return either ``None`` (no draft warranted) or a dict with keys
``kind``, ``text``, ``severity``, ``send_allowed``.

Forbidden in ``text`` by contract (covered by tests):
  * "AI approved", "AI rejected", "report blocked by AI", "sent to Telegram"
  * any storage path, receipt path, prompt text, raw model JSON, debug JSON.

Severity vocabulary:
  * ``info``    — advisory only, no action required to ship the report.
  * ``warning`` — operator should fix before confirmation.
  * ``blocker`` — deterministic safety failure; report is not ready.

This module is the only place in the codebase that turns app state into
operator-facing copy for Telegram. Keeping it isolated and pure means the
copy is reviewable in one file and never accidentally entangles with
canonical mutations.
"""

from __future__ import annotations

from typing import Any, Mapping, MutableMapping

# Final, hand-audited copy. Adding a new template means adding a new entry
# here AND adding a test for it AND updating the forbidden-phrase test.
_DRAFTS: dict[str, dict[str, str]] = {
    "missing_business_reason": {
        "severity": "warning",
        "text": (
            "I need the business purpose for this receipt before it can be reviewed."
        ),
    },
    "missing_attendees": {
        "severity": "warning",
        "text": "Please add the attendees for this meal receipt.",
    },
    "amount_mismatch": {
        "severity": "blocker",
        "text": (
            "This receipt amount does not match the Diners statement amount. "
            "Please review it in the Review Queue."
        ),
    },
    "date_mismatch": {
        "severity": "warning",
        "text": (
            "The receipt date appears different from the statement date. "
            "Please check it in the Review Queue."
        ),
    },
    "ai_advisory_warning": {
        "severity": "info",
        "text": (
            "AI second read found a possible issue, but it is advisory only. "
            "Please review it in the Review Queue."
        ),
    },
}

# Buckets that require attendee context per EDT policy. Mirrors the list
# review_sessions.MEAL_BUCKETS uses for the duplicate-meal check, with the
# extra "Customer Entertainment" bucket included since it also requires
# attendee names.
_MEAL_BUCKETS: frozenset[str] = frozenset(
    {
        "Meals/Snacks",
        "Breakfast",
        "Lunch",
        "Dinner",
        "Entertainment",
        "Customer Entertainment",
        "Meals & Entertainment",
    }
)

# Deterministic receipt-vs-statement safety codes (PR #55). The codes that
# warrant Telegram drafts are the amount/date ones; currency mismatches
# also exist but the spec deliberately does not include a Telegram template
# for them today.
_AMOUNT_ISSUE_CODES: frozenset[str] = frozenset(
    {
        "receipt_statement_amount_missing",
        "receipt_statement_amount_mismatch",
    }
)
_DATE_ISSUE_CODES: frozenset[str] = frozenset(
    {
        "receipt_statement_date_missing",
        "receipt_statement_date_mismatch",
    }
)

# Public list of all known draft kinds. ``"none"`` is exposed for callers
# that prefer a sentinel string over ``None``.
DRAFT_KINDS: tuple[str, ...] = (
    "missing_business_reason",
    "missing_attendees",
    "amount_mismatch",
    "date_mismatch",
    "ai_advisory_warning",
    "none",
)


def _draft(kind: str) -> dict[str, Any]:
    body = _DRAFTS[kind]
    return {
        "kind": kind,
        "text": body["text"],
        "severity": body["severity"],
        "send_allowed": False,
    }


def _str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _is_business(value: Any) -> bool:
    return _str(value).lower() == "business"


def _has_attendees(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, list):
        return any(_str(item) for item in value)
    return bool(_str(value))


def build_receipt_reply_draft(receipt: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return a draft for a receipt-only context, or ``None`` if no draft is warranted.

    Recognised input keys (all optional):
      * ``business_or_personal`` — "Business" / "Personal" / None
      * ``business_reason`` — short string or None
      * ``attendees`` — string, list, or None
      * ``report_bucket`` — bucket label string or None

    Priority when multiple issues apply: ``missing_business_reason`` wins
    over ``missing_attendees`` because it blocks every receipt category,
    not just meals. Callers that want both surfaced can call this twice
    with adjusted inputs.
    """
    if not receipt:
        return None
    if not _is_business(receipt.get("business_or_personal")):
        return None

    if not _str(receipt.get("business_reason")):
        return _draft("missing_business_reason")

    bucket = _str(receipt.get("report_bucket"))
    if bucket in _MEAL_BUCKETS and not _has_attendees(receipt.get("attendees")):
        return _draft("missing_attendees")

    return None


def build_review_row_reply_draft(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return a draft for a review-row context, or ``None`` if no draft is warranted.

    Recognised input keys (all optional):
      * ``receipt_statement_issues`` — list of safety dicts in PR #55 shape,
        e.g. ``[{"code": "receipt_statement_amount_mismatch", ...}]``.
        Alternatively, the ``source.match.receipt_statement_issues`` block
        from a review-row payload — this function reads either form.
      * ``ai_review`` — the ``source.ai_review`` block from a review-row
        payload (PR #57). Only ``status`` is consulted.
      * ``receipt`` — receipt-only context dict, used as a fallback when
        no row-level issue applies.

    Priority (highest first):
      1. Deterministic amount mismatch  → blocker draft.
      2. Deterministic date mismatch    → warning draft.
      3. AI ``warn``/``block``          → advisory info draft.
      4. Receipt-only fallbacks (missing business reason / attendees).
      5. None.

    AI ``stale``/``malformed``/``pass`` never produce a Telegram draft —
    they're queue-only states by design.
    """
    if not row:
        return None

    issues = _coerce_issue_list(row.get("receipt_statement_issues"))
    if not issues:
        # Accept the row-payload nesting: source.match.receipt_statement_issues.
        source = row.get("source") if isinstance(row, MutableMapping) else None
        if isinstance(source, Mapping):
            match = source.get("match")
            if isinstance(match, Mapping):
                issues = _coerce_issue_list(match.get("receipt_statement_issues"))

    issue_codes = {issue.get("code") for issue in issues if isinstance(issue, Mapping)}

    if issue_codes & _AMOUNT_ISSUE_CODES:
        return _draft("amount_mismatch")
    if issue_codes & _DATE_ISSUE_CODES:
        return _draft("date_mismatch")

    ai_review = row.get("ai_review")
    if not isinstance(ai_review, Mapping):
        # Accept the nesting source.ai_review when the caller passed the
        # whole row payload.
        source = row.get("source") if isinstance(row, MutableMapping) else None
        if isinstance(source, Mapping):
            ai_candidate = source.get("ai_review")
            if isinstance(ai_candidate, Mapping):
                ai_review = ai_candidate

    if isinstance(ai_review, Mapping):
        status = _str(ai_review.get("status")).lower()
        if status in {"warn", "block"}:
            return _draft("ai_advisory_warning")

    receipt = row.get("receipt")
    if isinstance(receipt, Mapping):
        return build_receipt_reply_draft(receipt)
    return None


def _coerce_issue_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]
