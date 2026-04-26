"""Service-layer wrapper for the FieldProvenanceEvent audit ledger.

This is the single entry point for writing provenance events. The
invariant — "no tracked-field write without an event in the same DB
transaction" — is enforced by the caller pattern, not by SQL: callers
MUST wrap column writes and ``record_field_event`` calls inside the
same ``with session.begin():`` block. See docs/M1_DAY3A_DESIGN.md §6
for the failure modes that motivate this.

The four read helpers (``get_current_event``, ``get_field_history``,
``get_decision_group``) are convenience wrappers around the load-bearing
queries the M3 approval UI and Day 3b's merge logic will use.

All public functions take session as a positional argument and the rest
as keyword-only. Strictness here prevents accidental positional-argument
misalignment as the signature evolves (e.g., adding a new field doesn't
silently shift downstream callers).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import desc
from sqlmodel import Session, select

from app.json_utils import dumps as _json_dumps
from app.models import FieldProvenanceEvent
from app.provenance_enums import (
    MONEY_FIELDS,
    ActorType,
    EntityType,
    EventType,
    FieldName,
    Source,
)

# Event types that produce current state (the rest are bookkeeping).
# Used by ``get_current_event`` to filter to just the events that
# represent "this is the value of the field right now."
_CURRENT_STATE_EVENT_TYPES: tuple[str, ...] = (
    EventType.ACCEPTED.value,
    EventType.OVERRIDDEN.value,
)

# Money-field quantizer: matches Numeric(18,4) precision on the production
# money columns (M1 Day 2.5) AND on FieldProvenanceEvent.value_decimal.
# Applied to money values BEFORE serialization so the value TEXT column
# matches that precision and matches the M1 Day 3a backfill output (no
# asymmetry between live writes and backfilled events).
_MONEY_QUANTIZER = Decimal("0.0001")


# ---------------------------------------------------------------------------
# value coercion
# ---------------------------------------------------------------------------


def _quantize_money(field_name: FieldName, value: Any) -> Any:
    """For money fields, return the value quantized to 4 dp via Decimal.

    Non-money fields and None pass through unchanged. Numeric inputs (int,
    float, str) are routed through Decimal(str(...)) to avoid float-binary
    noise (same trick as M1 Day 2.5's decode_decimal). Anything else also
    passes through and is left for downstream type guards / serializers
    to handle.
    """
    if field_name not in MONEY_FIELDS or value is None:
        return value
    if isinstance(value, Decimal):
        return value.quantize(_MONEY_QUANTIZER)
    if isinstance(value, (int, float)):
        return Decimal(str(value)).quantize(_MONEY_QUANTIZER)
    if isinstance(value, str):
        return Decimal(value).quantize(_MONEY_QUANTIZER)
    return value


def _serialize_value(value: Any) -> str | None:
    """Serialize an arbitrary field value to TEXT for the ``value`` column.

    Routes through app.json_utils.dumps so Decimals become fixed-point
    strings (matches the M1 Day 2.5 wire convention) and dates/datetimes
    fall through ``default=str``. ``None`` round-trips as ``None``,
    NOT the string "null", so the column stays SQL NULL when the
    caller passes None.
    """
    if value is None:
        return None
    # json.dumps wraps strings in quotes; we want the raw string for
    # human-readable inspection of the value column. Same for Decimal:
    # we want "419.5800" not '"419.5800"'.
    if isinstance(value, str):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat"):  # date
        return value.isoformat()
    # Bool / int / float / list / dict / etc. — let json_dumps handle.
    return _json_dumps(value)


def _maybe_value_decimal(field_name: FieldName, value: Any) -> Decimal | None:
    """Auto-populate value_decimal for money fields per design Q4."""
    if field_name not in MONEY_FIELDS:
        return None
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    # If the caller passed a money field's value as a string or number,
    # round-trip through Decimal-via-str so we don't introduce float
    # binary noise (see app.json_utils.decode_decimal for the same idea).
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        return Decimal(value)
    raise TypeError(
        f"value for money field {field_name.value!r} must be Decimal/int/"
        f"float/str, got {type(value).__name__}"
    )


def _serialize_metadata(metadata: dict[str, Any] | None) -> str | None:
    """Validate metadata is dict-or-None and serialize via DecimalEncoder.

    Per design Q5, the wrapper rejects anything other than dict or None.
    Pre-serialized JSON strings, lists, scalars, etc. all raise TypeError
    so callers can't sneak in inconsistent encodings.
    """
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise TypeError(
            f"metadata must be dict[str, Any] or None, got "
            f"{type(metadata).__name__}: {metadata!r}"
        )
    return _json_dumps(metadata)


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def record_field_event(
    session: Session,
    *,
    entity_type: EntityType,
    entity_id: int,
    field_name: FieldName,
    event_type: EventType,
    source: Source,
    value: Any,
    confidence: float | None = None,
    decision_group_id: str | None = None,
    actor_type: ActorType,
    actor_user_id: int | None = None,
    actor_label: str,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Write one provenance event. Caller owns the surrounding transaction.

    Atomicity contract: this function does session.add() + session.flush()
    only — it does NOT commit. The caller MUST be inside a transaction
    that also includes the corresponding column write to the product
    table (or a no-write event like 'rejected'/'snapshotted'). Day 3b
    refactors the merge logic to honor this contract.

    The required caller pattern is ``with session.begin():`` — NOT manual
    session.commit() / session.rollback(). See M1_DAY3A_DESIGN.md §6 for
    the autoflush-on-exception failure mode that makes manual commit
    insufficient.

    value_decimal is auto-populated from value when value is a Decimal
    (or coercible) AND field_name is in MONEY_FIELDS. Otherwise NULL.

    metadata contract:
      - Type: dict[str, Any] | None. Anything else raises TypeError.
      - Serialization: wrapper internally serializes via
        app.json_utils.dumps before storing in metadata_json. Decimal
        values inside metadata round-trip safely.
      - Read side: callers reading metadata_json should use
        json.loads + decode_decimal per money key.
    """
    # Enum-type guards. These are belt-and-suspenders since static type
    # checkers should already catch them, but bare strings sneak in
    # easily during quick refactors.
    if not isinstance(entity_type, EntityType):
        raise TypeError(f"entity_type must be EntityType, got {type(entity_type).__name__}")
    if not isinstance(field_name, FieldName):
        raise TypeError(f"field_name must be FieldName, got {type(field_name).__name__}")
    if not isinstance(event_type, EventType):
        raise TypeError(f"event_type must be EventType, got {type(event_type).__name__}")
    if not isinstance(source, Source):
        raise TypeError(f"source must be Source, got {type(source).__name__}")
    if not isinstance(actor_type, ActorType):
        raise TypeError(f"actor_type must be ActorType, got {type(actor_type).__name__}")

    if not actor_label:
        raise ValueError("actor_label is required and must be non-empty")

    # Money-field values quantize to 4 dp BEFORE serialization so the value
    # TEXT representation matches the value_decimal column's declared
    # precision AND the M1 Day 3a backfill output (PM step-6 ack). Non-money
    # values pass through unchanged.
    quantized = _quantize_money(field_name, value)

    event = FieldProvenanceEvent(
        entity_type=entity_type.value,
        entity_id=entity_id,
        field_name=field_name.value,
        event_type=event_type.value,
        source=source.value,
        value=_serialize_value(quantized),
        value_decimal=_maybe_value_decimal(field_name, quantized),
        confidence=confidence,
        decision_group_id=decision_group_id or uuid.uuid4().hex,
        actor_type=actor_type.value,
        actor_user_id=actor_user_id,
        actor_label=actor_label,
        metadata_json=_serialize_metadata(metadata),
    )
    session.add(event)
    session.flush()  # populates event.id without committing
    assert event.id is not None  # flush must have assigned the PK
    return event.id


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def get_current_event(
    session: Session,
    *,
    entity_type: EntityType,
    entity_id: int,
    field_name: FieldName,
) -> FieldProvenanceEvent | None:
    """Return the most recent ACCEPTED or OVERRIDDEN event for (entity, field).

    The product column's current value should equal this event's value
    (Day 3b's refactor enforces it; ``test_invariant_column_equals_latest_event``
    detects violations).

    Returns None if no accepted/overridden event has ever been written
    for this field on this entity. Pre-backfill rows return None;
    post-backfill rows return the legacy_unknown_current event written
    by the M1 Day 3a migration.
    """
    if not isinstance(entity_type, EntityType):
        raise TypeError(f"entity_type must be EntityType, got {type(entity_type).__name__}")
    if not isinstance(field_name, FieldName):
        raise TypeError(f"field_name must be FieldName, got {type(field_name).__name__}")

    stmt = (
        select(FieldProvenanceEvent)
        .where(
            FieldProvenanceEvent.entity_type == entity_type.value,
            FieldProvenanceEvent.entity_id == entity_id,
            FieldProvenanceEvent.field_name == field_name.value,
            FieldProvenanceEvent.event_type.in_(_CURRENT_STATE_EVENT_TYPES),
        )
        .order_by(desc(FieldProvenanceEvent.created_at), desc(FieldProvenanceEvent.id))
        .limit(1)
    )
    return session.exec(stmt).first()


def get_field_history(
    session: Session,
    *,
    entity_type: EntityType,
    entity_id: int,
    field_name: FieldName,
    limit: int | None = None,
) -> list[FieldProvenanceEvent]:
    """Return every event for (entity, field), newest first.

    Includes proposed/rejected events alongside accepted/overridden, so
    the M3 approval UI can render full lineage. Pass ``limit`` to cap
    the result for paging; default is unbounded (entities have at most
    a few dozen events in practice).
    """
    if not isinstance(entity_type, EntityType):
        raise TypeError(f"entity_type must be EntityType, got {type(entity_type).__name__}")
    if not isinstance(field_name, FieldName):
        raise TypeError(f"field_name must be FieldName, got {type(field_name).__name__}")

    stmt = (
        select(FieldProvenanceEvent)
        .where(
            FieldProvenanceEvent.entity_type == entity_type.value,
            FieldProvenanceEvent.entity_id == entity_id,
            FieldProvenanceEvent.field_name == field_name.value,
        )
        .order_by(desc(FieldProvenanceEvent.created_at), desc(FieldProvenanceEvent.id))
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.exec(stmt).all())


def get_decision_group(
    session: Session,
    *,
    decision_group_id: str,
) -> list[FieldProvenanceEvent]:
    """Return every event sharing the decision_group_id, oldest first.

    Used by the M3 approval UI to render "what alternatives existed at
    extraction time" — i.e., all proposed/accepted/rejected events from
    a single merge run. Ordered ASC by created_at so the UI can present
    the merge timeline left-to-right.
    """
    if not decision_group_id:
        raise ValueError("decision_group_id is required and must be non-empty")

    stmt = (
        select(FieldProvenanceEvent)
        .where(FieldProvenanceEvent.decision_group_id == decision_group_id)
        .order_by(FieldProvenanceEvent.created_at, FieldProvenanceEvent.id)
    )
    return list(session.exec(stmt).all())
