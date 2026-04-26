"""Enum vocabulary for the FieldProvenanceEvent audit ledger (M1 Day 3a).

These enums are the controlled vocabulary used by every code path that
writes or reads a provenance event. Keeping them in their own module
(separate from models.py) means consumers — Day 3b's merge-logic
refactor, Day 3c's snapshot writer, M3's approval UI, M5's ERP export —
can import the vocabulary without dragging in the full SQLModel table
registry.

All enums inherit (str, Enum) so values serialize cleanly to the TEXT
columns on FieldProvenanceEvent and round-trip through json.dumps
without a custom encoder.

See docs/M1_DAY3A_DESIGN.md §1–§5 for the rationale behind each value
and the reserved future-value strategy.
"""

from __future__ import annotations

from enum import Enum


class EntityType(str, Enum):
    """Which table the FieldProvenanceEvent.entity_id references.

    Day 3a writes only ``RECEIPT`` events (the backfill targets
    receiptdocument). REVIEW_ROW and EXPENSE_REPORT are reserved for
    Day 3c snapshot work and any user-edit pipeline that targets
    ReviewRow directly.
    """

    # Initial values used by Day 3a + 3b + 3c
    RECEIPT        = "receipt"          # receiptdocument.id
    REVIEW_ROW     = "review_row"       # reviewrow.id
    EXPENSE_REPORT = "expense_report"   # expensereport.id

    # — RESERVED FUTURE VALUES (not yet emitted by any code path) —
    # M3 (match-decision provenance): statementtransaction matches
    # STATEMENT_TRANSACTION = "statement_transaction"
    # M3 (approval workflow): which match was accepted/rejected
    # MATCH_DECISION        = "match_decision"
    # M4 (policy decisions): per-row policy verdicts
    # POLICY_DECISION       = "policy_decision"


class FieldName(str, Enum):
    """Which column on the entity the event describes.

    9 current values reflect tracked columns on ReceiptDocument /
    StatementTransaction today. 7 reserved future values are listed
    so the design surface is visible; they start producing events
    when their underlying columns/logic land in M1 Day 6 / Day 7 / M3.
    """

    # Money (current)
    EXTRACTED_LOCAL_AMOUNT = "extracted_local_amount"

    # Categorical (current)
    EXTRACTED_CURRENCY     = "extracted_currency"
    RECEIPT_TYPE           = "receipt_type"
    BUSINESS_OR_PERSONAL   = "business_or_personal"
    REPORT_BUCKET          = "report_bucket"

    # Identity / freeform (current)
    EXTRACTED_DATE         = "extracted_date"
    EXTRACTED_SUPPLIER     = "extracted_supplier"
    BUSINESS_REASON        = "business_reason"
    ATTENDEES              = "attendees"

    # — RESERVED FUTURE VALUES —
    # M1 Day 6 (VAT/KDV)
    VAT_AMOUNT              = "vat_amount"
    VAT_RATE                = "vat_rate"

    # M1 Day 7 (FX architecture)
    FX_RATE                 = "fx_rate"
    FX_SOURCE               = "fx_source"
    FX_DATE                 = "fx_date"

    # M3 (approval/match decisions)
    MATCH_DECISION_ID       = "match_decision_id"
    MANUAL_FINANCE_OVERRIDE = "manual_finance_override"


# Money-field membership: used by record_field_event() to auto-populate
# value_decimal on FieldProvenanceEvent. Per design Q4 + step-5 refinement,
# only true *amount* fields get the denormalized Decimal column populated.
# Rates and multipliers (FX_RATE, VAT_RATE) are NOT included — they're
# stored exact-precision in the value TEXT column only, so the
# Numeric(18,4) value_decimal column doesn't silently truncate their
# 8-dp precision and SUM(value_decimal) audit queries stay strictly
# amount-shaped.
MONEY_FIELDS: frozenset[FieldName] = frozenset({
    FieldName.EXTRACTED_LOCAL_AMOUNT,
    FieldName.VAT_AMOUNT,
})


class Source(str, Enum):
    """Where the value originated.

    "Stored" is intentionally absent — a "stored" event would lie about
    lineage. When merge logic preserves a previously-accepted value, it
    looks up the prior accepted event and preserves it as current
    instead of writing a new event claiming the value came from
    "stored". See design §3.
    """

    DETERMINISTIC          = "deterministic"
    VISION                 = "vision"
    USER_TELEGRAM          = "user_telegram"
    USER_WEB               = "user_web"
    DINERS_STATEMENT       = "diners_statement"
    ECB                    = "ecb"
    MANUAL_FINANCE         = "manual_finance"
    SYSTEM_MIGRATION       = "system_migration"
    LEGACY_UNKNOWN_CURRENT = "legacy_unknown_current"


class ActorType(str, Enum):
    """What process / human wrote the event row.

    Distinct from Source: source = where the value came from, actor =
    what code path wrote the event. Pre-SSO, web users get
    UNAUTHENTICATED_USER; post-SSO migration (M2) starts populating
    WEB_USER for new events while old events stay as-is. See design §4.
    """

    TELEGRAM_USER          = "telegram_user"
    WEB_USER               = "web_user"
    UNAUTHENTICATED_USER   = "unauthenticated_user"   # pre-SSO browser
    SYSTEM_MIGRATION       = "system_migration"
    SYSTEM_JOB             = "system_job"             # cron, import, FX fetch
    VISION_PIPELINE        = "vision_pipeline"
    DETERMINISTIC_PIPELINE = "deterministic_pipeline"


class EventType(str, Enum):
    """What kind of event this row records.

    Semantics:
      - PROPOSED:    candidate value from a source; doesn't change current state.
      - ACCEPTED:    this value is now current state (first-time set).
      - REJECTED:    candidate explicitly considered and discarded (optional).
      - OVERRIDDEN:  explicit replacement of a previously accepted value
                     (user edit, manual finance correction).
      - SNAPSHOTTED: report submission froze this event id into a report line.

    ACCEPTED vs OVERRIDDEN: ACCEPTED = first time the field acquired a
    current value (or field was previously NULL); OVERRIDDEN = a prior
    ACCEPTED event exists and is being replaced. Day 3b's merge logic
    decides which to write by checking get_current_event(). See §5.
    """

    PROPOSED    = "proposed"
    ACCEPTED    = "accepted"
    REJECTED    = "rejected"
    OVERRIDDEN  = "overridden"
    SNAPSHOTTED = "snapshotted"
