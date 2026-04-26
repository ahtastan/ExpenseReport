"""Service-layer tests for backend/app/services/field_provenance.py.

Covers the happy-path write + the four read helpers per design §10.
Atomicity, metadata validation, and the cross-write invariant get
their own files.

Three-phase test structure (forced by SQLAlchemy 2.x autobegin):
  1. Setup session — create the receipt, commit, exit.
  2. Action session — open fresh, run `with session.begin():` for writes.
  3. Verify session — open fresh, read back, no commit needed.
Each phase is its own ``with Session(engine)`` block.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest
from sqlmodel import Session

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import FieldProvenanceEvent, ReceiptDocument  # noqa: E402
from app.provenance_enums import (  # noqa: E402
    ActorType,
    EntityType,
    EventType,
    FieldName,
    Source,
)
from app.services.field_provenance import (  # noqa: E402
    get_current_event,
    get_decision_group,
    get_field_history,
    record_field_event,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _create_receipt(engine) -> int:
    """Insert a minimal ReceiptDocument in its own session; return its id."""
    with Session(engine) as session:
        r = ReceiptDocument()
        session.add(r)
        session.commit()
        session.refresh(r)
        assert r.id is not None
        return r.id


def _kwargs_for(
    *,
    receipt_id: int,
    field_name: FieldName = FieldName.EXTRACTED_LOCAL_AMOUNT,
    event_type: EventType = EventType.ACCEPTED,
    source: Source = Source.VISION,
    value=Decimal("419.5800"),
    actor_type: ActorType = ActorType.VISION_PIPELINE,
    actor_label: str = "vision:test",
    **overrides,
) -> dict:
    base = dict(
        entity_type=EntityType.RECEIPT,
        entity_id=receipt_id,
        field_name=field_name,
        event_type=event_type,
        source=source,
        value=value,
        actor_type=actor_type,
        actor_label=actor_label,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# record_field_event — happy path + value/value_decimal handling
# ---------------------------------------------------------------------------


def test_record_field_event_writes_row(isolated_db):
    rid = _create_receipt(isolated_db)
    with Session(isolated_db) as session, session.begin():
        event_id = record_field_event(session, **_kwargs_for(receipt_id=rid))

    with Session(isolated_db) as session:
        event = session.get(FieldProvenanceEvent, event_id)
        assert event is not None
        assert event.entity_type == "receipt"
        assert event.entity_id == rid
        assert event.field_name == "extracted_local_amount"
        assert event.event_type == "accepted"
        assert event.source == "vision"
        assert event.value == "419.5800"
        assert event.actor_type == "vision_pipeline"
        assert event.actor_label == "vision:test"
        assert event.decision_group_id  # auto-generated UUID


def test_record_field_event_auto_populates_value_decimal_for_money_fields(isolated_db):
    rid = _create_receipt(isolated_db)
    with Session(isolated_db) as session, session.begin():
        event_id = record_field_event(
            session,
            **_kwargs_for(
                receipt_id=rid,
                field_name=FieldName.EXTRACTED_LOCAL_AMOUNT,
                value=Decimal("419.5800"),
            ),
        )

    with Session(isolated_db) as session:
        event = session.get(FieldProvenanceEvent, event_id)
        assert event.value_decimal == Decimal("419.5800")
        assert event.value == "419.5800"


def test_record_field_event_leaves_value_decimal_null_for_non_money_fields(isolated_db):
    rid = _create_receipt(isolated_db)
    with Session(isolated_db) as session, session.begin():
        event_id = record_field_event(
            session,
            **_kwargs_for(
                receipt_id=rid,
                field_name=FieldName.EXTRACTED_SUPPLIER,
                source=Source.DETERMINISTIC,
                value="Migros",
                actor_type=ActorType.DETERMINISTIC_PIPELINE,
                actor_label="deterministic:test",
            ),
        )

    with Session(isolated_db) as session:
        event = session.get(FieldProvenanceEvent, event_id)
        assert event.value == "Migros"
        assert event.value_decimal is None


def test_record_field_event_auto_generates_decision_group_id_when_omitted(isolated_db):
    rid = _create_receipt(isolated_db)
    with Session(isolated_db) as session, session.begin():
        id_a = record_field_event(session, **_kwargs_for(receipt_id=rid))
        id_b = record_field_event(session, **_kwargs_for(receipt_id=rid))

    with Session(isolated_db) as session:
        ev_a = session.get(FieldProvenanceEvent, id_a)
        ev_b = session.get(FieldProvenanceEvent, id_b)
        # Each call without an explicit decision_group_id gets a fresh UUID.
        assert ev_a.decision_group_id != ev_b.decision_group_id
        # And both are non-empty 32-char hex strings (uuid4().hex).
        assert len(ev_a.decision_group_id) == 32
        assert len(ev_b.decision_group_id) == 32


def test_record_field_event_serializes_decimal_value_via_decimal_encoder(isolated_db):
    """The wire format must match the M1 Day 2.5 fixed-point convention.

    Uses EXTRACTED_LOCAL_AMOUNT (a 4-dp money field) with a value that
    exercises the encoder's fixed-point path. Both ``value`` (TEXT) and
    ``value_decimal`` (NUMERIC(18,4)) preserve the exact value at 4-dp
    precision.
    """
    rid = _create_receipt(isolated_db)
    with Session(isolated_db) as session, session.begin():
        event_id = record_field_event(
            session,
            **_kwargs_for(
                receipt_id=rid,
                field_name=FieldName.EXTRACTED_LOCAL_AMOUNT,
                value=Decimal("0.0001"),  # smallest 4-dp money increment
            ),
        )

    with Session(isolated_db) as session:
        event = session.get(FieldProvenanceEvent, event_id)
        # Fixed-point string, NOT scientific notation ("1E-4").
        assert event.value == "0.0001"
        # Denormalized money-shape column has the exact value.
        assert event.value_decimal == Decimal("0.0001")


def test_record_field_event_quantizes_money_field_to_4dp(isolated_db):
    """Money-field values are quantized to 4 dp BEFORE serialization.

    Pinning test for the PM step-6 ack: the service-layer wrapper must
    produce the same value TEXT representation as the M1 Day 3a backfill,
    which always quantizes to 4 dp via Numeric(18,4) precision. A caller
    passing Decimal("100") (no fractional precision) gets back the
    fully-padded "100.0000" form on both the value and value_decimal
    columns.
    """
    rid = _create_receipt(isolated_db)
    with Session(isolated_db) as session, session.begin():
        event_id = record_field_event(
            session,
            **_kwargs_for(
                receipt_id=rid,
                field_name=FieldName.EXTRACTED_LOCAL_AMOUNT,
                value=Decimal("100"),  # 0 dp; should be padded to 4 dp
            ),
        )

    with Session(isolated_db) as session:
        event = session.get(FieldProvenanceEvent, event_id)
        # Quantization gives both columns the same 4-dp representation.
        assert event.value == "100.0000"
        assert event.value_decimal == Decimal("100.0000")


def test_fx_rate_skips_value_decimal_to_preserve_8dp_precision(isolated_db):
    """FX_RATE is NOT in MONEY_FIELDS (per step-5 PM decision), so its
    8-dp value lives only in the TEXT ``value`` column where precision is
    preserved. ``value_decimal`` is left NULL to avoid silent 4-dp
    truncation in the Numeric(18,4) column and to keep
    SUM(value_decimal) queries strictly amount-shaped.
    """
    rid = _create_receipt(isolated_db)
    with Session(isolated_db) as session, session.begin():
        event_id = record_field_event(
            session,
            **_kwargs_for(
                receipt_id=rid,
                field_name=FieldName.FX_RATE,
                value=Decimal("0.00000001"),  # 8-dp rate boundary
            ),
        )

    with Session(isolated_db) as session:
        event = session.get(FieldProvenanceEvent, event_id)
        # value (TEXT) preserves the exact 8-dp value.
        assert event.value == "0.00000001"
        # value_decimal is NULL — FX_RATE no longer in MONEY_FIELDS.
        assert event.value_decimal is None


def test_record_field_event_rejects_bogus_enum_value(isolated_db):
    """Passing a bare string instead of an enum raises TypeError."""
    rid = _create_receipt(isolated_db)
    kwargs = _kwargs_for(receipt_id=rid)
    kwargs["field_name"] = "extracted_local_amount"  # str, not FieldName

    with Session(isolated_db) as session:
        with pytest.raises(TypeError, match="field_name must be FieldName"):
            with session.begin():
                record_field_event(session, **kwargs)


# ---------------------------------------------------------------------------
# get_current_event
# ---------------------------------------------------------------------------


def test_get_current_event_returns_most_recent_accepted_or_overridden(isolated_db):
    rid = _create_receipt(isolated_db)
    with Session(isolated_db) as session, session.begin():
        # First an accepted, then an overridden — overridden should win because
        # it's more recent and is also a current-state event type.
        record_field_event(
            session,
            **_kwargs_for(
                receipt_id=rid, event_type=EventType.ACCEPTED, value=Decimal("100.0000")
            ),
        )
        record_field_event(
            session,
            **_kwargs_for(
                receipt_id=rid,
                event_type=EventType.OVERRIDDEN,
                source=Source.USER_WEB,
                value=Decimal("150.0000"),
                actor_type=ActorType.UNAUTHENTICATED_USER,
                actor_label="web:demo",
            ),
        )

    with Session(isolated_db) as session:
        current = get_current_event(
            session,
            entity_type=EntityType.RECEIPT,
            entity_id=rid,
            field_name=FieldName.EXTRACTED_LOCAL_AMOUNT,
        )
        assert current is not None
        assert current.event_type == "overridden"
        assert current.value == "150.0000"


def test_get_current_event_ignores_proposed_and_rejected(isolated_db):
    """proposed / rejected don't represent current state and must be ignored."""
    rid = _create_receipt(isolated_db)
    with Session(isolated_db) as session, session.begin():
        # Older accepted event — represents current state.
        record_field_event(
            session,
            **_kwargs_for(
                receipt_id=rid, event_type=EventType.ACCEPTED, value=Decimal("100.0000")
            ),
        )
        # Newer proposed + rejected — must NOT shadow the accepted one.
        record_field_event(
            session,
            **_kwargs_for(
                receipt_id=rid, event_type=EventType.PROPOSED, value=Decimal("999.9999")
            ),
        )
        record_field_event(
            session,
            **_kwargs_for(
                receipt_id=rid, event_type=EventType.REJECTED, value=Decimal("888.8888")
            ),
        )

    with Session(isolated_db) as session:
        current = get_current_event(
            session,
            entity_type=EntityType.RECEIPT,
            entity_id=rid,
            field_name=FieldName.EXTRACTED_LOCAL_AMOUNT,
        )
        assert current is not None
        assert current.event_type == "accepted"
        assert current.value == "100.0000"


def test_get_current_event_returns_none_when_no_history(isolated_db):
    rid = _create_receipt(isolated_db)
    with Session(isolated_db) as session:
        current = get_current_event(
            session,
            entity_type=EntityType.RECEIPT,
            entity_id=rid,
            field_name=FieldName.EXTRACTED_LOCAL_AMOUNT,
        )
        assert current is None


# ---------------------------------------------------------------------------
# get_field_history
# ---------------------------------------------------------------------------


def test_get_field_history_orders_newest_first(isolated_db):
    rid = _create_receipt(isolated_db)
    with Session(isolated_db) as session, session.begin():
        for i, value in enumerate([Decimal("100"), Decimal("200"), Decimal("300")]):
            record_field_event(
                session,
                **_kwargs_for(
                    receipt_id=rid,
                    event_type=EventType.ACCEPTED if i == 0 else EventType.OVERRIDDEN,
                    value=value,
                ),
            )

    with Session(isolated_db) as session:
        history = get_field_history(
            session,
            entity_type=EntityType.RECEIPT,
            entity_id=rid,
            field_name=FieldName.EXTRACTED_LOCAL_AMOUNT,
        )
        assert len(history) == 3
        # Newest first: 300, then 200, then 100. Money fields quantize to 4 dp.
        assert [e.value for e in history] == ["300.0000", "200.0000", "100.0000"]


# ---------------------------------------------------------------------------
# get_decision_group
# ---------------------------------------------------------------------------


def test_get_decision_group_returns_all_events_with_matching_id_ordered_by_created_at_asc(
    isolated_db,
):
    """The §7 worked example: 2 proposed + 1 accepted, all sharing decision_group_id."""
    decision_group = "abc123def456" * 2  # 24 chars; valid hex-like string
    rid = _create_receipt(isolated_db)
    with Session(isolated_db) as session, session.begin():
        # Vision proposed
        record_field_event(
            session,
            **_kwargs_for(
                receipt_id=rid,
                event_type=EventType.PROPOSED,
                source=Source.VISION,
                value=Decimal("419.5800"),
                decision_group_id=decision_group,
                confidence=0.92,
            ),
        )
        # Deterministic proposed
        record_field_event(
            session,
            **_kwargs_for(
                receipt_id=rid,
                event_type=EventType.PROPOSED,
                source=Source.DETERMINISTIC,
                value=Decimal("420.0000"),
                decision_group_id=decision_group,
                confidence=0.78,
                actor_type=ActorType.DETERMINISTIC_PIPELINE,
                actor_label="deterministic:_parse_amount",
            ),
        )
        # Accepted (the merge winner)
        record_field_event(
            session,
            **_kwargs_for(
                receipt_id=rid,
                event_type=EventType.ACCEPTED,
                source=Source.VISION,
                value=Decimal("419.5800"),
                decision_group_id=decision_group,
                confidence=0.92,
            ),
        )

    with Session(isolated_db) as session:
        events = get_decision_group(session, decision_group_id=decision_group)
        assert len(events) == 3
        # ASC by (created_at, id) — insertion order preserved.
        assert [e.event_type for e in events] == ["proposed", "proposed", "accepted"]
        assert [e.source for e in events] == ["vision", "deterministic", "vision"]
        assert all(e.decision_group_id == decision_group for e in events)


def test_decision_group_unrelated_events_not_included(isolated_db):
    """An event with a different decision_group_id must not appear."""
    group_a = "a" * 32
    group_b = "b" * 32
    rid = _create_receipt(isolated_db)
    with Session(isolated_db) as session, session.begin():
        record_field_event(
            session,
            **_kwargs_for(receipt_id=rid, decision_group_id=group_a),
        )
        record_field_event(
            session,
            **_kwargs_for(receipt_id=rid, decision_group_id=group_b),
        )

    with Session(isolated_db) as session:
        a_events = get_decision_group(session, decision_group_id=group_a)
        b_events = get_decision_group(session, decision_group_id=group_b)
        assert len(a_events) == 1
        assert len(b_events) == 1
        assert a_events[0].id != b_events[0].id
