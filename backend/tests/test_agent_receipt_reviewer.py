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
