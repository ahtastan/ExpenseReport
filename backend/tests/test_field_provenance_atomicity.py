"""Atomicity + metadata-validation tests for field_provenance.py.

The full cross-write invariant (column == latest event for every receipt
and every tracked field) lives in test_field_provenance_invariant.py
(added in step 7). This file pins the load-bearing rollback semantics
and the metadata-contract enforcement.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from sqlmodel import Session, select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.json_utils import decode_decimal  # noqa: E402
from app.models import FieldProvenanceEvent, ReceiptDocument  # noqa: E402
from app.provenance_enums import (  # noqa: E402
    ActorType,
    EntityType,
    EventType,
    FieldName,
    Source,
)
from app.services.field_provenance import record_field_event  # noqa: E402


def _create_receipt(engine) -> int:
    with Session(engine) as session:
        r = ReceiptDocument()
        session.add(r)
        session.commit()
        session.refresh(r)
        return r.id


def _kwargs_for(receipt_id: int, **overrides) -> dict:
    base = dict(
        entity_type=EntityType.RECEIPT,
        entity_id=receipt_id,
        field_name=FieldName.EXTRACTED_LOCAL_AMOUNT,
        event_type=EventType.ACCEPTED,
        source=Source.VISION,
        value=Decimal("419.5800"),
        actor_type=ActorType.VISION_PIPELINE,
        actor_label="vision:test",
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Atomicity — the load-bearing rollback test
# ---------------------------------------------------------------------------


class _IntentionalTestError(Exception):
    """Marker exception so the test's raise is unmistakable in a traceback."""


def test_session_rollback_drops_event_alongside_column_write(isolated_db):
    """The required `with session.begin():` pattern must roll back BOTH the
    column write AND the provenance event when an exception escapes the block.

    Pins design §6 contract. Day 3b's merge-logic refactor relies on this.
    """
    receipt_id = _create_receipt(isolated_db)

    # Under-test sequence: fresh session, all DB ops inside one begin block,
    # then raise.
    with Session(isolated_db) as session:
        with pytest.raises(_IntentionalTestError):
            with session.begin():
                receipt = session.get(ReceiptDocument, receipt_id)
                receipt.extracted_local_amount = Decimal("999.9999")
                record_field_event(session, **_kwargs_for(receipt_id=receipt_id))
                raise _IntentionalTestError("simulated failure mid-transaction")

    # Verify on a fresh session: neither write persisted.
    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, receipt_id)
        assert receipt.extracted_local_amount is None, (
            "column write leaked past rollback — atomicity contract broken"
        )
        events = list(
            session.exec(
                select(FieldProvenanceEvent).where(
                    FieldProvenanceEvent.entity_id == receipt_id
                )
            ).all()
        )
        assert events == [], (
            f"event leaked past rollback — atomicity contract broken; "
            f"found {len(events)} event(s)"
        )


# ---------------------------------------------------------------------------
# metadata contract — dict[str, Any] | None only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_metadata",
    [
        '{"already": "a json string"}',  # pre-serialized JSON
        ["a", "list", "is", "not", "a", "dict"],
        ("a", "tuple"),
        42,
        Decimal("3.14"),
        "raw scalar string",
    ],
    ids=["json-string", "list", "tuple", "int", "decimal", "str"],
)
def test_record_field_event_rejects_non_dict_metadata(isolated_db, bad_metadata):
    """Per design Q5, only dict[str, Any] or None is accepted."""
    rid = _create_receipt(isolated_db)
    with Session(isolated_db) as session:
        with pytest.raises(TypeError, match="metadata must be dict"):
            with session.begin():
                record_field_event(
                    session,
                    metadata=bad_metadata,
                    **_kwargs_for(receipt_id=rid),
                )


def test_record_field_event_accepts_none_metadata(isolated_db):
    """None is the documented sentinel for "no metadata"."""
    rid = _create_receipt(isolated_db)
    with Session(isolated_db) as session, session.begin():
        event_id = record_field_event(
            session, metadata=None, **_kwargs_for(receipt_id=rid)
        )

    with Session(isolated_db) as session:
        event = session.get(FieldProvenanceEvent, event_id)
        assert event.metadata_json is None


def test_metadata_with_decimal_value_round_trips_through_decimal_encoder(isolated_db):
    """Decimal values inside metadata must round-trip exactly via the
    M1 Day 2.5 DecimalEncoder + decode_decimal pair.
    """
    rid = _create_receipt(isolated_db)
    with Session(isolated_db) as session, session.begin():
        event_id = record_field_event(
            session,
            metadata={
                "raw_amount": Decimal("123.4567"),
                "vision_model": "gpt-5.4-mini",
                "escalated": False,
            },
            **_kwargs_for(receipt_id=rid),
        )

    with Session(isolated_db) as session:
        event = session.get(FieldProvenanceEvent, event_id)
        assert event.metadata_json is not None

        # Read side: parse JSON, then route money-shaped keys through
        # decode_decimal to restore Decimal precision (the documented pattern).
        parsed = json.loads(event.metadata_json)
        assert parsed["vision_model"] == "gpt-5.4-mini"
        assert parsed["escalated"] is False

        # raw_amount round-trips as a string ("123.4567") — caller decodes:
        assert parsed["raw_amount"] == "123.4567"
        assert decode_decimal(parsed["raw_amount"]) == Decimal("123.4567")


# ---------------------------------------------------------------------------
# actor_label contract — non-empty required
# ---------------------------------------------------------------------------


def test_record_field_event_rejects_empty_actor_label(isolated_db):
    """Empty actor_label must raise ValueError. Pins the wrapper guard so a
    future refactor can't silently allow events with no actor identifier.
    """
    rid = _create_receipt(isolated_db)
    with Session(isolated_db) as session:
        with pytest.raises(ValueError, match="actor_label"):
            with session.begin():
                record_field_event(
                    session,
                    **_kwargs_for(receipt_id=rid, actor_label=""),
                )
