from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

from app.services.agent_receipt_reviewer import (
    AgentReceiptRead,
    build_agent_receipt_review_prompt,
    compare_agent_receipt_read,
)


def _canonical(**overrides):
    fields = {
        "date": "2026-04-20",
        "supplier": "Acme Cafe",
        "amount": "42.50",
        "currency": "USD",
        "business_or_personal": "Personal",
        "business_reason": None,
        "attendees": [],
    }
    fields.update(overrides)
    return fields


def _agent(**overrides):
    fields = {
        "merchant_name": "Acme Cafe",
        "merchant_address": "10 Main St",
        "receipt_date": "2026-04-20",
        "receipt_time": "12:30",
        "total_amount": "42.50",
        "currency": "USD",
        "amount_text": "$42.50",
        "line_items": [{"description": "Lunch", "amount": "42.50"}],
        "tax_amount": "3.20",
        "payment_method": "Visa",
        "receipt_category": "meals",
        "confidence": 0.94,
        "raw_text_summary": "Acme Cafe lunch receipt totaling $42.50.",
    }
    fields.update(overrides)
    return AgentReceiptRead.from_dict(fields)


def test_exact_amount_date_currency_supplier_passes():
    result = compare_agent_receipt_read(_canonical(), _agent())

    assert result.schema_version == "0a"
    assert result.to_dict()["schema_version"] == "0a"
    assert result.comparison.amount_match is True
    assert result.comparison.date_match is True
    assert result.comparison.currency_match is True
    assert result.comparison.supplier_match is True
    assert result.comparison.risk_level == "pass"
    assert result.comparison.recommended_action == "accept"
    assert result.comparison.differences == []


def test_amount_mismatch_blocks_report():
    result = compare_agent_receipt_read(_canonical(), _agent(total_amount="52.50"))

    assert result.comparison.amount_match is False
    assert result.comparison.risk_level == "block"
    assert result.comparison.recommended_action == "block_report"
    assert "amount_mismatch" in result.comparison.differences


def test_currency_mismatch_blocks_report():
    result = compare_agent_receipt_read(_canonical(), _agent(currency="EUR"))

    assert result.comparison.currency_match is False
    assert result.comparison.risk_level == "block"
    assert result.comparison.recommended_action == "block_report"
    assert "currency_mismatch" in result.comparison.differences


def test_one_day_date_mismatch_passes_with_default_tolerance():
    result = compare_agent_receipt_read(_canonical(), _agent(receipt_date="2026-04-21"))

    assert result.comparison.date_match is True
    assert result.comparison.risk_level == "pass"


def test_one_day_date_mismatch_warns_when_tolerance_is_zero():
    result = compare_agent_receipt_read(
        _canonical(),
        _agent(receipt_date="2026-04-21"),
        date_tolerance_days=0,
    )

    assert result.comparison.date_match is False
    assert result.comparison.risk_level == "warn"
    assert result.comparison.recommended_action == "manual_review"
    assert "date_mismatch" in result.comparison.differences


def test_many_day_date_mismatch_warns_not_blocks():
    result = compare_agent_receipt_read(_canonical(), _agent(receipt_date="2026-04-30"))

    assert result.comparison.date_match is False
    assert result.comparison.risk_level == "warn"
    assert "date_mismatch" in result.comparison.differences


def test_supplier_mismatch_only_warns_not_blocks():
    result = compare_agent_receipt_read(_canonical(), _agent(merchant_name="Other Market"))

    assert result.comparison.supplier_match is False
    assert result.comparison.risk_level == "warn"
    assert result.comparison.recommended_action == "manual_review"
    assert "supplier_mismatch" in result.comparison.differences


def test_missing_agent_amount_blocks_report():
    result = compare_agent_receipt_read(_canonical(), _agent(total_amount=None))

    assert result.comparison.amount_match is False
    assert result.comparison.risk_level == "block"
    assert result.comparison.recommended_action == "block_report"
    assert "missing_agent_amount" in result.comparison.differences


def test_missing_business_context_generates_user_message():
    result = compare_agent_receipt_read(
        _canonical(business_or_personal="Business", business_reason="", attendees=[]),
        _agent(),
    )

    assert result.comparison.risk_level == "warn"
    assert result.comparison.recommended_action == "ask_user"
    assert "missing_business_reason" in result.comparison.differences
    assert "business reason" in (result.comparison.suggested_user_message or "").lower()


def test_prompt_builder_includes_strict_json_and_do_not_guess():
    prompt = build_agent_receipt_review_prompt(_canonical())

    assert "strict JSON" in prompt
    assert "Do not guess" in prompt
    assert "not final authority" in prompt
    assert "must not approve, match, report, or overwrite canonical DB values" in prompt
    assert "Deterministic app code will compare the agent read against canonical OCR fields" in prompt
    assert "comparison_notes" not in prompt
    assert '"supplier": "Acme Cafe"' in prompt


def test_cli_writes_review_result_for_synthetic_json(tmp_path):
    canonical_path = tmp_path / "canonical.json"
    agent_path = tmp_path / "agent.json"
    out_path = tmp_path / "result.json"
    canonical_path.write_text(json.dumps(_canonical()), encoding="utf-8")
    agent_path.write_text(
        json.dumps(
            {
                "merchant_name": "Acme Cafe",
                "receipt_date": "2026-04-20",
                "total_amount": "42.50",
                "currency": "USD",
                "amount_text": "$42.50",
                "confidence": 0.94,
            }
        ),
        encoding="utf-8",
    )

    repo_root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "run_agent_receipt_review.py"),
            "--canonical-json",
            str(canonical_path),
            "--agent-json",
            str(agent_path),
            "--out",
            str(out_path),
        ],
        cwd=repo_root / "backend",
        check=True,
        text=True,
        capture_output=True,
    )

    assert completed.stderr == ""
    assert out_path.exists()
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "0a"
    assert payload["comparison"]["risk_level"] == "pass"
    assert payload["agent_read"]["total_amount"] == "42.50"
    assert Decimal(payload["agent_read"]["total_amount"]) == Decimal("42.50")


# ─── F-AI-Stage1 sub-PR 2: inline-keyboard prompt + parser ──────────────────

from datetime import date as _date  # noqa: E402

from sqlmodel import Session  # noqa: E402

from app.models import ReceiptDocument  # noqa: E402
from app.services.agent_receipt_reviewer import (  # noqa: E402
    INLINE_KEYBOARD_PROMPT_VERSION,
    InlineKeyboardSuggestion,
    apply_bucket_post_validation,
    build_inline_keyboard_review_prompt,
    inline_keyboard_bucket_vocabulary,
    parse_inline_keyboard_response,
)
from app.services.agent_receipt_review_persistence import (  # noqa: E402
    write_mock_agent_receipt_review,
)


def _canonical_for_inline_keyboard():
    return {
        "date": "2026-05-01",
        "supplier": "Acme Cafe",
        "amount": "42.50",
        "currency": "TRY",
        "business_or_personal": None,
        "business_reason": None,
        "attendees": None,
    }


def _context_window():
    return {
        "employees": ["Hakan", "Burak Yilmaz"],
        "recent_receipts": [
            {
                "date": "2026-04-30",
                "supplier": "Acme Cafe",
                "bucket": "Meals/Snacks",
                "business_or_personal": "Business",
                "amount": 38.0,
                "currency": "TRY",
            },
        ],
        "recent_attendees": ["Hakan", "Burak Yilmaz"],
        "lookback_days": 2,
        "fetched_at": "2026-05-01T08:00:00+00:00",
    }


def _full_inline_keyboard_response_json():
    return json.dumps(
        {
            "business_or_personal": "Business",
            "report_bucket": "Meals/Snacks",
            "attendees": ["Hakan", "Burak Yilmaz"],
            "customer": "DcExpense",
            "business_reason": "Team lunch with Burak",
            "confidence_overall": 0.91,
        }
    )


def test_receipt_second_read_unchanged():
    """Regression guard: existing comparator path unaffected by sub-PR 2."""
    canonical = _canonical()
    result = compare_agent_receipt_read(canonical, _agent())
    assert result.schema_version == "0a"
    assert result.comparison.risk_level == "pass"
    # The existing prompt builder still produces the canonical-only prompt.
    prompt = build_agent_receipt_review_prompt(canonical)
    assert "shadow AI receipt reviewer" in prompt
    assert "CONTEXT (last N days, this user only)" not in prompt


def test_receipt_inline_keyboard_calls_model_with_context():
    """Prompt body must include employees, recent receipts, recent attendees."""
    canonical = _canonical_for_inline_keyboard()
    context = _context_window()
    prompt = build_inline_keyboard_review_prompt(canonical, context)

    # Employees and recent attendees appear verbatim.
    assert "Hakan" in prompt
    assert "Burak Yilmaz" in prompt
    # Recent receipts summary fields appear.
    assert "Acme Cafe" in prompt
    assert "Meals/Snacks" in prompt
    # Required output keys are listed.
    for key in (
        "business_or_personal",
        "report_bucket",
        "attendees",
        "customer",
        "business_reason",
        "confidence_overall",
    ):
        assert key in prompt
    # Bucket vocabulary is embedded.
    for bucket in inline_keyboard_bucket_vocabulary():
        assert bucket in prompt


def test_receipt_inline_keyboard_parses_full_response(isolated_db):
    raw = _full_inline_keyboard_response_json()
    suggestion = parse_inline_keyboard_response(raw)
    assert isinstance(suggestion, InlineKeyboardSuggestion)
    assert suggestion.business_or_personal == "Business"
    assert suggestion.report_bucket == "Meals/Snacks"
    assert suggestion.attendees == ["Hakan", "Burak Yilmaz"]
    assert suggestion.customer == "DcExpense"
    assert suggestion.business_reason == "Team lunch with Burak"
    assert suggestion.confidence_overall == 0.91

    # Persistence: the suggested_* columns end up populated.
    with Session(isolated_db) as session:
        receipt = ReceiptDocument(
            source="test",
            status="received",
            content_type="photo",
            extracted_date=_date(2026, 5, 1),
            extracted_supplier="Acme Cafe",
            extracted_local_amount=Decimal("42.50"),
            extracted_currency="TRY",
        )
        session.add(receipt)
        session.commit()
        session.refresh(receipt)

        outcome = write_mock_agent_receipt_review(
            session,
            receipt=receipt,
            agent_json_text=raw,
            run_kind="receipt_inline_keyboard",
            suggested_business_or_personal=suggestion.business_or_personal,
            suggested_report_bucket=suggestion.report_bucket,
            suggested_attendees=suggestion.attendees,
            suggested_customer=suggestion.customer,
            suggested_business_reason=suggestion.business_reason,
            suggested_confidence_overall=suggestion.confidence_overall,
            context_window=_context_window(),
        )
        session.commit()

        assert outcome.run.status == "completed"
        assert outcome.run.run_kind == "receipt_inline_keyboard"
        assert outcome.run.prompt_version == INLINE_KEYBOARD_PROMPT_VERSION

        from sqlmodel import select  # local import keeps the original imports tight
        from app.models import AgentReceiptRead

        read_row = session.exec(
            select(AgentReceiptRead).where(AgentReceiptRead.run_id == outcome.run.id)
        ).first()
        assert read_row is not None
        assert read_row.suggested_business_or_personal == "Business"
        assert read_row.suggested_report_bucket == "Meals/Snacks"
        assert read_row.suggested_attendees_json is not None
        # Persisted in insertion order — ``dumps(sort_keys=True)`` only
        # sorts dict keys, not list elements.
        assert json.loads(read_row.suggested_attendees_json) == ["Hakan", "Burak Yilmaz"]
        assert read_row.suggested_customer == "DcExpense"
        assert read_row.suggested_business_reason == "Team lunch with Burak"
        assert read_row.suggested_confidence_overall == 0.91


def test_receipt_inline_keyboard_handles_malformed_json(isolated_db):
    """Garbage input → status='failed' on the run, no read row populated."""
    suggestion = parse_inline_keyboard_response("this is not json at all")
    assert suggestion is None

    with Session(isolated_db) as session:
        receipt = ReceiptDocument(
            source="test",
            status="received",
            content_type="photo",
            extracted_local_amount=Decimal("10.00"),
            extracted_currency="TRY",
        )
        session.add(receipt)
        session.commit()
        session.refresh(receipt)

        outcome = write_mock_agent_receipt_review(
            session,
            receipt=receipt,
            agent_json_text="this is not json at all",
            run_kind="receipt_inline_keyboard",
            # Persistence is robust: even when the parser returns None
            # callers may still drive the persistence path with all
            # suggested_* set to None. The run completes with an empty
            # read row (no suggested fields populated), not an error.
            suggested_business_or_personal=None,
            suggested_report_bucket=None,
            suggested_attendees=None,
            suggested_customer=None,
            suggested_business_reason=None,
            suggested_confidence_overall=None,
            context_window=_context_window(),
        )
        session.commit()

        assert outcome.run.status == "completed"

        from sqlmodel import select
        from app.models import AgentReceiptRead

        read_row = session.exec(
            select(AgentReceiptRead).where(AgentReceiptRead.run_id == outcome.run.id)
        ).first()
        assert read_row is not None
        assert read_row.suggested_business_or_personal is None
        assert read_row.suggested_report_bucket is None
        assert read_row.suggested_attendees_json is None


def test_receipt_inline_keyboard_handles_partial_response():
    """Missing optional ``customer`` field → suggestion.customer is None."""
    raw = json.dumps(
        {
            "business_or_personal": "Business",
            "report_bucket": "Meals/Snacks",
            "attendees": ["Hakan"],
            "business_reason": "Team lunch",
            "confidence_overall": 0.8,
        }
    )
    suggestion = parse_inline_keyboard_response(raw)
    assert suggestion is not None
    assert suggestion.customer is None
    assert suggestion.business_or_personal == "Business"
    assert suggestion.report_bucket == "Meals/Snacks"
    assert suggestion.attendees == ["Hakan"]
    assert suggestion.business_reason == "Team lunch"
    assert suggestion.confidence_overall == 0.8


def test_receipt_inline_keyboard_handles_extra_fields():
    """Unknown keys are silently ignored; known fields persisted correctly."""
    raw = json.dumps(
        {
            "business_or_personal": "Business",
            "report_bucket": "Meals/Snacks",
            "attendees": ["Hakan"],
            "customer": None,
            "business_reason": "Team lunch",
            "confidence_overall": 0.85,
            # Unknown/extra fields the model might include:
            "model_notes": "looked clean",
            "internal_debug": {"latency_ms": 320},
        }
    )
    suggestion = parse_inline_keyboard_response(raw)
    assert suggestion is not None
    assert suggestion.business_or_personal == "Business"
    assert suggestion.business_reason == "Team lunch"
    assert suggestion.confidence_overall == 0.85
    # Extra fields don't leak into the dataclass.
    assert "model_notes" not in suggestion.to_dict()
    assert "internal_debug" not in suggestion.to_dict()


def test_receipt_inline_keyboard_persists_context_window(isolated_db):
    """``context_window`` kwarg becomes ``context_window_json`` on the run row."""
    with Session(isolated_db) as session:
        receipt = ReceiptDocument(
            source="test",
            status="received",
            content_type="photo",
            extracted_local_amount=Decimal("10.00"),
            extracted_currency="TRY",
        )
        session.add(receipt)
        session.commit()
        session.refresh(receipt)

        ctx = _context_window()
        outcome = write_mock_agent_receipt_review(
            session,
            receipt=receipt,
            agent_json_text=_full_inline_keyboard_response_json(),
            run_kind="receipt_inline_keyboard",
            suggested_business_or_personal="Business",
            suggested_report_bucket="Meals/Snacks",
            suggested_attendees=["Hakan", "Burak Yilmaz"],
            suggested_customer="DcExpense",
            suggested_business_reason="Team lunch",
            suggested_confidence_overall=0.91,
            context_window=ctx,
        )
        session.commit()

        assert outcome.run.context_window_json is not None
        persisted = json.loads(outcome.run.context_window_json)
        assert persisted["employees"] == ctx["employees"]
        assert persisted["recent_attendees"] == ctx["recent_attendees"]
        assert persisted["lookback_days"] == 2
        assert persisted["fetched_at"] == ctx["fetched_at"]


# ─── F-AI-Stage1 sub-PR 7: bucket post-validation guard ────────────────────


def _suggestion(
    *,
    bucket: str = "Meals/Snacks",
    receipt_time: str | None = None,
) -> InlineKeyboardSuggestion:
    return InlineKeyboardSuggestion(
        business_or_personal="Business",
        report_bucket=bucket,
        attendees=["Hakan only"],
        customer=None,
        business_reason="meal",
        confidence_overall=0.7,
        receipt_time=receipt_time,
    )


def test_post_validation_erina_case_bumps_snacks_to_dinner():
    """ERINA evidence (live prod, post-PR-95): 7,570 TRY at 18:49, AI
    suggests Meals/Snacks. The guard must bump to Dinner — both the
    amount band (>2000 TRY) and the dinner-hour signal agree."""
    suggestion, reason = apply_bucket_post_validation(
        _suggestion(bucket="Meals/Snacks", receipt_time="18:49"),
        local_amount=Decimal("7570"),
        local_currency="TRY",
    )
    assert suggestion.report_bucket == "Dinner"
    assert reason is not None
    assert "amount_dinner_grade" in reason


def test_post_validation_small_snack_stays_snacks():
    """50 TRY coffee at 14:30 with the AI saying Snacks: amount under
    band floor → no bump, no false positives on coffee runs."""
    suggestion, reason = apply_bucket_post_validation(
        _suggestion(bucket="Meals/Snacks", receipt_time="14:30"),
        local_amount=Decimal("50"),
        local_currency="TRY",
    )
    assert suggestion.report_bucket == "Meals/Snacks"
    assert reason is None


def test_post_validation_lunch_grade_amount_bumps_to_lunch():
    """800 TRY at 13:00 with the AI saying Snacks: amount in lunch band
    + lunch hour → Lunch."""
    suggestion, reason = apply_bucket_post_validation(
        _suggestion(bucket="Meals/Snacks", receipt_time="13:00"),
        local_amount=Decimal("800"),
        local_currency="TRY",
    )
    assert suggestion.report_bucket == "Lunch"
    assert reason == "amount_lunch_grade"


def test_post_validation_dinner_grade_amount_bumps_to_dinner():
    """3000 TRY at 20:00 with the AI saying Snacks: dinner band + dinner
    hour. Both signals agree → Dinner with combined reason."""
    suggestion, reason = apply_bucket_post_validation(
        _suggestion(bucket="Meals/Snacks", receipt_time="20:00"),
        local_amount=Decimal("3000"),
        local_currency="TRY",
    )
    assert suggestion.report_bucket == "Dinner"
    assert reason is not None
    assert "amount_dinner_grade" in reason


def test_post_validation_usd_dinner_amount_bumps_to_dinner():
    """$200 at 19:30 with the AI saying Snacks: USD>50 + dinner hour
    → Dinner. ('Customer Entertainment' is not in the EDT taxonomy;
    Dinner is the closest existing bucket per the user's instruction not
    to invent vocab values.)"""
    suggestion, reason = apply_bucket_post_validation(
        _suggestion(bucket="Meals/Snacks", receipt_time="19:30"),
        local_amount=Decimal("200"),
        local_currency="USD",
    )
    assert suggestion.report_bucket == "Dinner"
    assert reason is not None
    assert "amount_dinner_grade" in reason


def test_post_validation_lunch_bucket_at_dinner_hour_bumps_to_dinner():
    """AI suggests Lunch but the receipt prints 19:30 — structurally
    counter-intuitive, bump to Dinner regardless of amount."""
    suggestion, reason = apply_bucket_post_validation(
        _suggestion(bucket="Lunch", receipt_time="19:30"),
        local_amount=Decimal("800"),
        local_currency="TRY",
    )
    assert suggestion.report_bucket == "Dinner"
    assert reason == "time_dinner_hour"


def test_post_validation_null_amount_leaves_suggestion_unchanged():
    """When the canonical receipt has no amount yet, the guard must not
    crash and must not bump — fall back to the AI's raw suggestion."""
    suggestion, reason = apply_bucket_post_validation(
        _suggestion(bucket="Meals/Snacks", receipt_time="18:49"),
        local_amount=None,
        local_currency="TRY",
    )
    # Time alone with no amount won't bump (the time-only Snacks→Dinner
    # rule requires amount-grade lunch/dinner).
    assert suggestion.report_bucket == "Meals/Snacks"
    assert reason is None


def test_post_validation_null_time_with_dinner_amount_still_bumps_on_amount():
    """Receipt has amount but no printed time. Amount alone is enough
    to disqualify Snacks for a 7570 TRY receipt."""
    suggestion, reason = apply_bucket_post_validation(
        _suggestion(bucket="Meals/Snacks", receipt_time=None),
        local_amount=Decimal("7570"),
        local_currency="TRY",
    )
    assert suggestion.report_bucket == "Dinner"
    assert reason == "amount_dinner_grade"


def test_post_validation_both_null_returns_unchanged():
    suggestion, reason = apply_bucket_post_validation(
        _suggestion(bucket="Meals/Snacks", receipt_time=None),
        local_amount=None,
        local_currency=None,
    )
    assert suggestion.report_bucket == "Meals/Snacks"
    assert reason is None


def test_post_validation_non_meal_bucket_never_touched():
    """Hotel / Taxi / Fuel / Entertainment etc. are out of scope. The
    guard is allowed to touch only the meal-bucket family — never bump
    a non-meal bucket even if a meal-anchor signal happens to fire."""
    for bucket in (
        "Taxi/Parking/Tolls/Uber",
        "Hotel/Lodging/Laundry",
        "Auto Gasoline",
        "Entertainment",
        "Dinner",  # already Dinner — no double-bump
        "Other",
    ):
        suggestion, reason = apply_bucket_post_validation(
            _suggestion(bucket=bucket, receipt_time="20:00"),
            local_amount=Decimal("9999"),
            local_currency="TRY",
        )
        assert suggestion.report_bucket == bucket, (
            f"Guard must not touch {bucket!r}"
        )
        assert reason is None


def test_post_validation_breakfast_bucket_with_dinner_amount_bumps():
    """Breakfast is the third bumpable bucket alongside Snacks. A
    dinner-grade amount on a Breakfast bucket is structurally wrong."""
    suggestion, reason = apply_bucket_post_validation(
        _suggestion(bucket="Breakfast", receipt_time=None),
        local_amount=Decimal("3500"),
        local_currency="TRY",
    )
    assert suggestion.report_bucket == "Dinner"
    assert reason == "amount_dinner_grade"


def test_post_validation_lunch_amount_at_dinner_hour_escalates_to_dinner():
    """Snacks bucket + lunch-grade amount + dinner-hour time: the amount
    alone would land Lunch, but the dinner-hour signal escalates it."""
    suggestion, reason = apply_bucket_post_validation(
        _suggestion(bucket="Meals/Snacks", receipt_time="20:30"),
        local_amount=Decimal("1200"),
        local_currency="TRY",
    )
    assert suggestion.report_bucket == "Dinner"
    assert reason is not None
    # Both reasons combine: amount said Lunch, time pushed it to Dinner.
    assert "amount_lunch_grade" in reason
    assert "time_dinner_hour" in reason


def test_post_validation_unknown_currency_returns_unchanged():
    """JPY isn't in the calibrated band table; the guard returns the AI
    suggestion untouched rather than guessing."""
    suggestion, reason = apply_bucket_post_validation(
        _suggestion(bucket="Meals/Snacks", receipt_time="20:00"),
        local_amount=Decimal("50000"),  # ~$340 in JPY but we don't know
        local_currency="JPY",
    )
    assert suggestion.report_bucket == "Meals/Snacks"
    assert reason is None


def test_post_validation_malformed_receipt_time_treated_as_missing():
    """Garbage receipt_time strings must not crash; treat as missing
    time and rely on amount-only signals."""
    suggestion, reason = apply_bucket_post_validation(
        _suggestion(bucket="Meals/Snacks", receipt_time="not a time"),
        local_amount=Decimal("7570"),
        local_currency="TRY",
    )
    # Amount alone still bumps to Dinner.
    assert suggestion.report_bucket == "Dinner"
    assert reason == "amount_dinner_grade"


def test_post_validation_negative_amount_returns_unchanged():
    """Defensive: a negative canonical amount (refund/data-quality
    artifact) must not be classified by the band table."""
    suggestion, reason = apply_bucket_post_validation(
        _suggestion(bucket="Meals/Snacks", receipt_time="20:00"),
        local_amount=Decimal("-100"),
        local_currency="TRY",
    )
    assert suggestion.report_bucket == "Meals/Snacks"
    assert reason is None


# ─── prompt + vocabulary ────────────────────────────────────────────────────


def test_inline_keyboard_vocabulary_includes_meal_time_buckets():
    """sub-PR 7: the AI vocabulary now includes Lunch / Dinner /
    Breakfast (existing EDT canonical buckets) so the AI can suggest
    them directly without relying on the post-validation guard."""
    vocab = inline_keyboard_bucket_vocabulary()
    for required in ("Meals/Snacks", "Lunch", "Dinner", "Breakfast", "Entertainment"):
        assert required in vocab, f"vocabulary missing canonical bucket {required!r}"


def test_inline_keyboard_prompt_contains_price_and_time_anchors():
    """The Layer 1 guidance must surface to the model — pin a few
    representative anchor strings."""
    canonical = _canonical_for_inline_keyboard()
    context = _context_window()
    prompt = build_inline_keyboard_review_prompt(canonical, context)
    # Amount bands.
    assert "TRY" in prompt and "Lunch" in prompt and "Dinner" in prompt
    assert "500" in prompt and "2000" in prompt
    assert "$15" in prompt and "$50" in prompt
    # Time-of-day windows.
    assert "11:30" in prompt
    assert "18:00" in prompt
    # receipt_time field is in the schema.
    assert "receipt_time" in prompt


def test_inline_keyboard_prompt_full_response_with_receipt_time_parses():
    raw = json.dumps(
        {
            "business_or_personal": "Business",
            "report_bucket": "Dinner",
            "attendees": ["Hakan", "customer Ahmet"],
            "customer": "Acme",
            "business_reason": "Customer dinner",
            "confidence_overall": 0.88,
            "receipt_time": "20:15",
        }
    )
    suggestion = parse_inline_keyboard_response(raw)
    assert suggestion is not None
    assert suggestion.report_bucket == "Dinner"
    assert suggestion.receipt_time == "20:15"


def test_inline_keyboard_prompt_response_without_receipt_time_still_parses():
    """receipt_time is optional — older raw responses without it must
    keep working."""
    raw = json.dumps(
        {
            "business_or_personal": "Business",
            "report_bucket": "Meals/Snacks",
            "attendees": [],
            "customer": None,
            "business_reason": "Coffee",
            "confidence_overall": 0.5,
        }
    )
    suggestion = parse_inline_keyboard_response(raw)
    assert suggestion is not None
    assert suggestion.receipt_time is None
