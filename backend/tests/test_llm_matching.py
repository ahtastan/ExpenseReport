"""Tests for LLM-assisted match disambiguation.

The matching service already produces deterministic high/medium/low scores.
When no unique-high exists but multiple medium candidates do, it should call
``model_router.match_disambiguate`` and — on a confident pick — promote the
chosen transaction to high and auto-approve.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import MatchDecision, ReceiptDocument, StatementTransaction  # noqa: E402
from app.services import matching, model_router  # noqa: E402


def _setup_session() -> Session:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed_ambiguous(session: Session) -> tuple[ReceiptDocument, list[StatementTransaction]]:
    """Create one receipt that matches two statement transactions equally well."""
    tx_a = StatementTransaction(
        statement_import_id=1,
        transaction_date=date(2026, 4, 2),
        supplier_raw="STARBUCKS OTG POYRAZKOY",
        supplier_normalized="starbucks otg poyrazkoy",
        local_amount=75.0,
        local_currency="TRY",
        usd_amount=2.5,
        source_row_ref="a",
    )
    tx_b = StatementTransaction(
        statement_import_id=1,
        transaction_date=date(2026, 4, 2),  # same date
        supplier_raw="STARBUCKS IST OTG",
        supplier_normalized="starbucks ist otg",
        local_amount=75.0,  # same amount — deterministic cannot pick
        local_currency="TRY",
        usd_amount=2.5,
        source_row_ref="b",
    )
    session.add(tx_a)
    session.add(tx_b)
    session.commit()
    session.refresh(tx_a)
    session.refresh(tx_b)

    receipt = ReceiptDocument(
        original_file_name="starbucks_receipt.jpg",
        storage_path="(memory)",
        extracted_date=date(2026, 4, 2),
        extracted_supplier="Starbucks OTG Poyrazkoy",
        extracted_local_amount=75.0,
        extracted_currency="TRY",
        status="extracted",
    )
    session.add(receipt)
    session.commit()
    session.refresh(receipt)
    return receipt, [tx_a, tx_b]


def run_confident_pick() -> None:
    session = _setup_session()
    try:
        receipt, (tx_a, tx_b) = _seed_ambiguous(session)

        # Deterministic scoring alone: both tx_a and tx_b score similarly.
        # Assert the premise before engaging the router.
        scores = []
        for tx in (tx_a, tx_b):
            s = matching.score_receipt_against_transaction(receipt, tx)
            assert s is not None
            scores.append(s)
        # The ambiguous fixture deliberately produces two plausible (high or
        # medium) picks with no unique-high. If the scorer ever changes to
        # break this tie without an LLM, the test will flag it immediately.
        plausible = [s for s in scores if s.confidence in {"high", "medium"}]
        high_scores = [s for s in scores if s.confidence == "high"]
        assert len(plausible) >= 2, plausible
        assert len(high_scores) != 1, (
            "Test premise broken: deterministic scorer now picks a unique high"
        )

        calls: list[tuple[dict, list[dict]]] = []

        def fake_text_call(model, prompt, payload):
            import json

            data = json.loads(payload)
            calls.append((data["receipt"], data["candidates"]))
            return {
                "transaction_id": tx_a.id,
                "confidence": "high",
                "reasoning": "supplier text matches candidate A more precisely",
            }

        original = model_router._text_call
        model_router._text_call = fake_text_call
        try:
            stats = matching.run_matching(session, statement_import_id=1)
        finally:
            model_router._text_call = original

        assert stats.llm_disambiguated == 1, stats
        assert stats.auto_approved == 1, stats
        assert calls and len(calls) == 1, calls

        approved = [d for d in session.query(MatchDecision).all() if d.approved]
        assert len(approved) == 1
        promoted = approved[0]
        assert promoted.statement_transaction_id == tx_a.id
        assert promoted.confidence == "high"
        assert promoted.match_method == "llm_disambiguated_v1"
        assert "llm(" in promoted.reason and "supplier text matches" in promoted.reason
        print("confident-pick path: OK")
    finally:
        session.close()


def run_abstain() -> None:
    session = _setup_session()
    try:
        receipt, (tx_a, tx_b) = _seed_ambiguous(session)

        def fake_text_call(model, prompt, payload):
            return {"transaction_id": None, "confidence": "low", "reasoning": "unclear"}

        original = model_router._text_call
        model_router._text_call = fake_text_call
        try:
            stats = matching.run_matching(session, statement_import_id=1)
        finally:
            model_router._text_call = original

        assert stats.llm_abstained == 1, stats
        assert stats.llm_disambiguated == 0
        assert stats.auto_approved == 0
        decisions = session.query(MatchDecision).all()
        # Both candidates should still be recorded at medium; no promotion.
        assert all(d.match_method == "date_amount_merchant_v1" for d in decisions), decisions
        assert all(d.approved is False for d in decisions)
        print("abstain path: OK")
    finally:
        session.close()


def run_unavailable() -> None:
    """When the router returns None (no API key / SDK unavailable), behavior
    matches the deterministic-only baseline with no auto-approval."""
    session = _setup_session()
    try:
        _seed_ambiguous(session)

        def fake_text_call(model, prompt, payload):
            return None

        original = model_router._text_call
        model_router._text_call = fake_text_call
        try:
            stats = matching.run_matching(session, statement_import_id=1)
        finally:
            model_router._text_call = original

        assert stats.llm_disambiguated == 0, stats
        assert stats.llm_abstained == 1, stats
        assert stats.auto_approved == 0
        print("unavailable path: OK")
    finally:
        session.close()


def main() -> None:
    run_confident_pick()
    run_abstain()
    run_unavailable()
    print("llm_matching_tests=passed")


if __name__ == "__main__":
    main()
