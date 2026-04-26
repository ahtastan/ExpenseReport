"""Tests for LLM-suggested EDT bucket flowing from match_disambiguate
through run_matching to ReceiptDocument.report_bucket.

Five run_* functions, mirroring the script-style pattern of the existing
test_llm_matching.py:

  1. match_disambiguate returns the bucket+category when both are in the
     closed set (EDT_BUCKETS / EDT_CATEGORIES).
  2. match_disambiguate drops unknown / wrong-type bucket values to None.
  3. run_matching, given an LLM-disambiguated high-confidence pick with a
     bucket suggestion and a receipt that has no existing report_bucket,
     auto-applies the bucket onto the receipt (and onto MatchDecision).
  4. run_matching, given the same input but with the receipt already having
     report_bucket set, leaves the receipt unchanged (operator wins).
  5. run_matching on the deterministic-only path (unique high-conf, no LLM
     call) does not touch report_bucket — the bucket-auto-apply is strictly
     an LLM_MATCH-source feature.
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
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


def _seed_ambiguous_starbucks(
    session: Session, *, receipt_bucket: str | None = None
) -> tuple[ReceiptDocument, list[StatementTransaction]]:
    """Two equally-good Starbucks transactions + one receipt — same shape as
    test_llm_matching's fixture so the LLM disambiguation path actually fires.
    """
    tx_a = StatementTransaction(
        statement_import_id=1,
        transaction_date=date(2026, 4, 2),
        supplier_raw="STARBUCKS OTG POYRAZKOY",
        supplier_normalized="starbucks otg poyrazkoy",
        local_amount=Decimal("75.0"),
        local_currency="TRY",
        usd_amount=Decimal("2.5"),
        source_row_ref="a",
    )
    tx_b = StatementTransaction(
        statement_import_id=1,
        transaction_date=date(2026, 4, 2),
        supplier_raw="STARBUCKS IST OTG",
        supplier_normalized="starbucks ist otg",
        local_amount=Decimal("75.0"),
        local_currency="TRY",
        usd_amount=Decimal("2.5"),
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
        extracted_local_amount=Decimal("75.0"),
        extracted_currency="TRY",
        status="extracted",
        report_bucket=receipt_bucket,
    )
    session.add(receipt)
    session.commit()
    session.refresh(receipt)
    return receipt, [tx_a, tx_b]


def _seed_unambiguous(session: Session) -> tuple[ReceiptDocument, StatementTransaction]:
    """One receipt + one statement transaction with identical date+amount,
    nothing else competing. Deterministic scorer should pick a unique high.
    """
    tx = StatementTransaction(
        statement_import_id=1,
        transaction_date=date(2026, 4, 5),
        supplier_raw="GOKHAN BUFE",
        supplier_normalized="gokhan bufe",
        local_amount=Decimal("170.0"),
        local_currency="TRY",
        usd_amount=Decimal("5.0"),
        source_row_ref="x",
    )
    session.add(tx)
    session.commit()
    session.refresh(tx)

    receipt = ReceiptDocument(
        original_file_name="gokhan.jpg",
        storage_path="(memory)",
        extracted_date=date(2026, 4, 5),
        extracted_supplier="GOKHAN BUFE",
        extracted_local_amount=Decimal("170.0"),
        extracted_currency="TRY",
        status="extracted",
        report_bucket=None,
    )
    session.add(receipt)
    session.commit()
    session.refresh(receipt)
    return receipt, tx


# ---------------------------------------------------------------------------
# 1. match_disambiguate returns valid closed-set bucket+category
# ---------------------------------------------------------------------------


def run_match_disambiguate_returns_bucket_when_in_closed_set() -> None:
    def fake_text_call(model, prompt, payload):
        return {
            "transaction_id": 1,
            "confidence": "high",
            "reasoning": "supplier text matches",
            "suggested_bucket": "Meals/Snacks",
            "suggested_category": "Meals & Entertainment",
        }

    original = model_router._text_call
    model_router._text_call = fake_text_call
    try:
        result = model_router.match_disambiguate(
            receipt={"supplier": "Starbucks", "local_amount": Decimal("75.0")},
            candidates=[{"transaction_id": 1, "supplier": "STARBUCKS"}],
        )
    finally:
        model_router._text_call = original

    assert result is not None
    assert result.transaction_id == 1
    assert result.confidence == "high"
    assert result.suggested_bucket == "Meals/Snacks"
    assert result.suggested_category == "Meals & Entertainment"
    print("match_disambiguate-returns-bucket: OK")


# ---------------------------------------------------------------------------
# 2. match_disambiguate drops unknown / wrong-type values
# ---------------------------------------------------------------------------


def run_match_disambiguate_drops_unknown_bucket() -> None:
    """Hallucinated bucket name → field is None. Same for unknown category.
    Wrong-type values (int, list) also dropped.
    """
    def fake_text_call(model, prompt, payload):
        return {
            "transaction_id": 1,
            "confidence": "high",
            "reasoning": "x",
            "suggested_bucket": "Lobster Tax",  # not in EDT_BUCKETS
            "suggested_category": 42,           # wrong type
        }

    original = model_router._text_call
    model_router._text_call = fake_text_call
    try:
        result = model_router.match_disambiguate(
            receipt={"supplier": "x"},
            candidates=[{"transaction_id": 1, "supplier": "y"}],
        )
    finally:
        model_router._text_call = original

    assert result is not None
    # Transaction id and reasoning still come through — only the bucket/category
    # validators reject; the rest of the row is unaffected.
    assert result.transaction_id == 1
    assert result.suggested_bucket is None, (
        f"unknown bucket should be dropped to None; got {result.suggested_bucket!r}"
    )
    assert result.suggested_category is None, (
        f"wrong-type category should be dropped to None; got {result.suggested_category!r}"
    )
    print("match_disambiguate-drops-unknown-bucket: OK")


# ---------------------------------------------------------------------------
# 3. run_matching auto-applies bucket when receipt empty
# ---------------------------------------------------------------------------


def run_run_matching_auto_applies_bucket_when_receipt_empty() -> None:
    session = _setup_session()
    try:
        receipt, (tx_a, tx_b) = _seed_ambiguous_starbucks(session, receipt_bucket=None)

        def fake_text_call(model, prompt, payload):
            # Single fake handles both prompt types: respond shape-appropriately
            # based on which prompt arrived. Disambiguation prompt asks for
            # transaction_id + confidence; classify prompt asks for bucket+category only.
            if "Pick the single" in prompt:
                # Disambiguation call — return bucket via the disambig path so
                # the classify call does NOT fire (decision.suggested_bucket
                # is already non-None on the LLM-promoted row).
                return {
                    "transaction_id": tx_a.id,
                    "confidence": "high",
                    "reasoning": "supplier text matches A",
                    "suggested_bucket": "Meals/Snacks",
                    "suggested_category": "Meals & Entertainment",
                }
            # Should not reach here — but if classify is called, return None
            # so the test still asserts disambiguation was the source.
            return None

        original = model_router._text_call
        model_router._text_call = fake_text_call
        try:
            stats = matching.run_matching(session, statement_import_id=1)
        finally:
            model_router._text_call = original

        # LLM-disambiguated, auto-approved, bucket auto-applied via the
        # disambiguation path — classify call should NOT fire because
        # decision.suggested_bucket is already populated.
        assert stats.llm_disambiguated == 1, stats
        assert stats.auto_approved == 1, stats
        assert stats.bucket_auto_applied == 1, stats
        assert stats.llm_classification_calls == 0, (
            f"classify should not fire when disambiguation already gave a bucket; "
            f"got {stats.llm_classification_calls}"
        )

        # Receipt got the bucket value.
        session.refresh(receipt)
        assert receipt.report_bucket == "Meals/Snacks", receipt.report_bucket

        # MatchDecision row also carries the suggestion.
        approved = [d for d in session.query(MatchDecision).all() if d.approved]
        assert len(approved) == 1
        promoted = approved[0]
        assert promoted.suggested_bucket == "Meals/Snacks"
        assert promoted.suggested_category == "Meals & Entertainment"
        # Non-promoted demoted-rival rows do NOT carry the bucket suggestion;
        # that anchors to the LLM's chosen transaction, not the alternates.
        non_promoted = [
            d for d in session.query(MatchDecision).all()
            if d.statement_transaction_id != tx_a.id
        ]
        for d in non_promoted:
            assert d.suggested_bucket is None, d
            assert d.suggested_category is None, d

        print("run_matching-auto-applies-bucket: OK")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 4. run_matching does NOT clobber an existing operator-set bucket
# ---------------------------------------------------------------------------


def run_run_matching_does_not_clobber_existing_bucket() -> None:
    session = _setup_session()
    try:
        receipt, (tx_a, tx_b) = _seed_ambiguous_starbucks(
            session, receipt_bucket="Other"  # operator already set this
        )

        def fake_text_call(model, prompt, payload):
            if "Pick the single" in prompt:
                return {
                    "transaction_id": tx_a.id,
                    "confidence": "high",
                    "reasoning": "x",
                    "suggested_bucket": "Meals/Snacks",
                    "suggested_category": "Meals & Entertainment",
                }
            return None

        original = model_router._text_call
        model_router._text_call = fake_text_call
        try:
            stats = matching.run_matching(session, statement_import_id=1)
        finally:
            model_router._text_call = original

        # LLM still ran and the decision row still carries the suggestion —
        # but the receipt's existing bucket is preserved.
        assert stats.llm_disambiguated == 1, stats
        assert stats.auto_approved == 1, stats
        assert stats.llm_classification_calls == 0, (
            f"classify should not fire when disambiguation gave a bucket; "
            f"got {stats.llm_classification_calls}"
        )
        assert stats.bucket_auto_applied == 0, (
            f"expected zero bucket-applies on pre-set receipt, got "
            f"{stats.bucket_auto_applied}"
        )

        session.refresh(receipt)
        assert receipt.report_bucket == "Other", (
            f"existing operator bucket clobbered: {receipt.report_bucket!r}"
        )

        # MatchDecision DOES still get the suggestion — the audit record
        # is preserved even though the receipt wasn't auto-applied. M3 UI
        # can show "LLM suggested Meals/Snacks but operator picked Other."
        approved = [d for d in session.query(MatchDecision).all() if d.approved]
        assert len(approved) == 1
        assert approved[0].suggested_bucket == "Meals/Snacks"

        print("run_matching-does-not-clobber-existing-bucket: OK")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 5. run_matching deterministic path — Scope C: classifier still fires, NOT
#    disambiguation, and bucket gets auto-applied if receipt is empty.
# ---------------------------------------------------------------------------


def run_run_matching_classifies_bucket_on_deterministic_path() -> None:
    """Scope C: every approved match calls classify_match_bucket, even on
    unambiguous deterministic high-confidence matches. Disambiguation does
    NOT fire (only one plausible candidate), but classification does.

    Distinguishes the two prompt types via prompt-content sniff so the
    fake_text_call can answer correctly. If disambiguation were somehow
    called, the assertion in the disambig branch would fire.
    """
    session = _setup_session()
    try:
        receipt, tx = _seed_unambiguous(session)

        calls_by_prompt_type: dict[str, int] = {"disambig": 0, "classify": 0}

        def fake_text_call(model, prompt, payload):
            # Prompt-content sniff: disambiguation prompt mentions "Pick the
            # single best candidate"; classify prompt mentions "EDT expense-
            # template classifier" and a single transaction.
            if "Pick the single" in prompt:
                calls_by_prompt_type["disambig"] += 1
                raise AssertionError(
                    "disambiguation called on deterministic-only path; "
                    "expected only the classify path to fire"
                )
            if "EDT expense-template classifier" in prompt:
                calls_by_prompt_type["classify"] += 1
                return {
                    "bucket": "Meals/Snacks",
                    "category": "Meals & Entertainment",
                    "reasoning": "supplier 'GOKHAN BUFE' is a snack/meal vendor",
                }
            return None

        original = model_router._text_call
        model_router._text_call = fake_text_call
        try:
            stats = matching.run_matching(session, statement_import_id=1)
        finally:
            model_router._text_call = original

        # Deterministic high-conf, auto-approved. NO disambiguation call.
        # YES classify call. Bucket auto-applied because receipt was empty.
        assert stats.llm_disambiguated == 0, stats
        assert stats.llm_abstained == 0, stats
        assert stats.auto_approved == 1, stats
        assert stats.llm_classification_calls == 1, (
            f"classify must fire once on deterministic auto-approve; "
            f"got {stats.llm_classification_calls}"
        )
        assert stats.bucket_auto_applied == 1, stats
        assert calls_by_prompt_type == {"disambig": 0, "classify": 1}, (
            calls_by_prompt_type
        )

        # Receipt got the classifier's bucket.
        session.refresh(receipt)
        assert receipt.report_bucket == "Meals/Snacks", receipt.report_bucket

        # MatchDecision row carries both the deterministic match_method AND
        # the classifier's bucket suggestion. The classifier's reasoning is
        # appended to the existing reason for audit.
        approved = [d for d in session.query(MatchDecision).all() if d.approved]
        assert len(approved) == 1
        d = approved[0]
        assert d.match_method == "date_amount_merchant_v1"
        assert d.suggested_bucket == "Meals/Snacks"
        assert d.suggested_category == "Meals & Entertainment"
        assert "classify(" in d.reason and "snack/meal" in d.reason

        print("run_matching-classifies-bucket-on-deterministic-path: OK")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 6. classify-call fires once per approved match across multiple receipts
# ---------------------------------------------------------------------------


def run_run_matching_classify_call_fires_for_every_approved_match() -> None:
    """With multiple unambiguous deterministic-high-confidence matches, the
    classifier must fire EXACTLY once per approved match — not per candidate
    score, not per receipt-considered, but per auto_approved increment.

    This pins the Scope C contract: every approved match gets one bucket
    classification, no more no less.
    """
    session = _setup_session()
    try:
        # Three unambiguous receipt-transaction pairs, each different supplier
        # and amount so deterministic scorer produces a unique high per pair
        # without any LLM disambiguation.
        from datetime import date as _date
        from decimal import Decimal as _Dec

        pairs = [
            ("ALPHA STORE", _Dec("100.00"), _date(2026, 4, 1)),
            ("BETA CAFE",   _Dec("250.50"), _date(2026, 4, 2)),
            ("GAMMA HOTEL", _Dec("3500.00"), _date(2026, 4, 3)),
        ]
        receipts = []
        txns = []
        for supplier, amount, dt in pairs:
            tx = StatementTransaction(
                statement_import_id=1,
                transaction_date=dt,
                supplier_raw=supplier,
                supplier_normalized=supplier.lower(),
                local_amount=amount,
                local_currency="TRY",
                usd_amount=_Dec("3.0"),
                source_row_ref=supplier,
            )
            session.add(tx)
            session.commit()
            session.refresh(tx)
            txns.append(tx)
            r = ReceiptDocument(
                original_file_name=f"{supplier}.jpg",
                storage_path="(memory)",
                extracted_date=dt,
                extracted_supplier=supplier,
                extracted_local_amount=amount,
                extracted_currency="TRY",
                status="extracted",
                report_bucket=None,
            )
            session.add(r)
            session.commit()
            session.refresh(r)
            receipts.append(r)

        classify_calls = 0

        def fake_text_call(model, prompt, payload):
            nonlocal classify_calls
            if "Pick the single" in prompt:
                raise AssertionError(
                    "disambiguation called on unambiguous fixture"
                )
            if "EDT expense-template classifier" in prompt:
                classify_calls += 1
                return {
                    "bucket": "Other",
                    "category": "Other",
                    "reasoning": f"call#{classify_calls}",
                }
            return None

        original = model_router._text_call
        model_router._text_call = fake_text_call
        try:
            stats = matching.run_matching(session, statement_import_id=1)
        finally:
            model_router._text_call = original

        assert stats.auto_approved == 3, stats
        assert stats.llm_classification_calls == 3, (
            f"expected 3 classify calls (one per approved match); "
            f"got {stats.llm_classification_calls}"
        )
        assert classify_calls == 3, classify_calls
        assert stats.bucket_auto_applied == 3, stats

        # Each receipt should have its bucket auto-applied.
        for r in receipts:
            session.refresh(r)
            assert r.report_bucket == "Other", r

        # Each approved decision carries its own classification reasoning.
        approved = [d for d in session.query(MatchDecision).all() if d.approved]
        assert len(approved) == 3
        reasonings = sorted(d.reason for d in approved)
        # Every approved decision should mention "classify(" somewhere in its
        # reason text — the appended classifier audit line.
        for r in reasonings:
            assert "classify(" in r, r

        print("run_matching-classify-call-fires-for-every-approved-match: OK")
    finally:
        session.close()


def main() -> None:
    run_match_disambiguate_returns_bucket_when_in_closed_set()
    run_match_disambiguate_drops_unknown_bucket()
    run_run_matching_auto_applies_bucket_when_receipt_empty()
    run_run_matching_does_not_clobber_existing_bucket()
    run_run_matching_classifies_bucket_on_deterministic_path()
    run_run_matching_classify_call_fires_for_every_approved_match()
    print("match_llm_bucket_tests=passed")


if __name__ == "__main__":
    main()
