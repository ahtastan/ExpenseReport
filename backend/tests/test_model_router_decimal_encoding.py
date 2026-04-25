"""Lock-in tests for model_router's Decimal-aware JSON encoding.

After M1 Day 2.5 the matching/synthesis prompts receive payloads built
from Decimal-typed model fields (StatementTransaction.local_amount,
ReceiptDocument.extracted_local_amount, etc.). Both ``json.dumps`` call
sites in ``model_router`` must pass ``cls=DecimalEncoder`` or they raise
``TypeError: Object of type Decimal is not JSON serializable`` at
runtime — a regression that wouldn't show up in any other unit test
since those code paths require a live model.

These tests pin that behavior so a future refactor can't silently
re-introduce the TypeError.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import model_router  # noqa: E402


class _RecordedTextCall:
    """Stand-in for ``_text_call`` that records the rendered payload string."""

    def __init__(self, response: dict | None):
        self.response = response
        self.calls: list[tuple[str, str, str]] = []  # (model, prompt, payload)

    def __call__(self, model, prompt, payload):
        self.calls.append((model, prompt, payload))
        return self.response


def test_match_disambiguate_serializes_decimal_payload(monkeypatch):
    """A Decimal-bearing receipt + candidates dict must serialize without TypeError."""
    recorder = _RecordedTextCall(
        response={"transaction_id": 7, "confidence": "high", "reasoning": "match"}
    )
    monkeypatch.setattr(model_router, "_text_call", recorder)

    receipt = {
        "supplier": "Migros",
        "date": "2026-04-01",
        "local_amount": Decimal("419.5800"),  # the value SQLAlchemy returns post-migration
        "local_currency": "TRY",
    }
    candidates = [
        {
            "transaction_id": 7,
            "supplier": "MIGROS",
            "date": "2026-04-01",
            "local_amount": Decimal("419.5800"),
            "local_currency": "TRY",
            "deterministic_reason": "exact amount + same date",
        }
    ]

    result = model_router.match_disambiguate(receipt, candidates)

    assert result is not None
    assert result.transaction_id == 7
    assert recorder.calls, "_text_call was not invoked"
    _model, _prompt, payload = recorder.calls[0]
    # The payload must contain the amount as a fixed-point string per the
    # M1 Day 2.5 JSON convention (not a float, not "Decimal('...')").
    # Use a whitespace-tolerant check since json.dumps() defaults add a space
    # after the key separator.
    assert '"local_amount": "419.5800"' in payload, (
        f"Decimal must serialize as fixed-point string in matching payload; "
        f"got: {payload}"
    )


def test_synthesize_report_summary_serializes_decimal_payload(monkeypatch):
    """A Decimal-bearing synthesis report must serialize without TypeError."""
    recorder = _RecordedTextCall(response={"summary_md": "# OK"})
    monkeypatch.setattr(model_router, "_text_call", recorder)

    report = {
        "statement_import_id": 1,
        "totals_by_bucket": {
            "Hotel/Lodging/Laundry": Decimal("1234.5678"),
            "Meals/Snacks": Decimal("89.0100"),
        },
        "line_count": 13,
    }

    summary = model_router.synthesize_report_summary(report)

    assert summary == "# OK"
    assert recorder.calls
    _model, _prompt, payload = recorder.calls[0]
    assert '"Hotel/Lodging/Laundry": "1234.5678"' in payload
    assert '"Meals/Snacks": "89.0100"' in payload
