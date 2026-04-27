"""Bug 5: trip-purpose title in Week 1A G3 should be LLM-summarized from
the per-receipt business_reason text instead of the generic operator-typed
title_prefix ("November 2025 Diners Club Expense Report").

Reference behavior: TASTAN's hand-typed reports show titles like
"Jim's Turkey visit, Visiting customers (Hayat, Kartonsan, Sanipak, Tezol)"
— specific customer names and trip purpose. ``generate_travel_reason_summary()``
asks the matching/synthesis model to produce that style of summary.

Best-effort: returns None on any failure; caller falls back to title_prefix.
Never blocks report generation.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import model_router  # noqa: E402


# ---------------------------------------------------------------------------
# happy path — LLM returns a usable summary
# ---------------------------------------------------------------------------


def run_returns_summary_when_llm_succeeds() -> None:
    captured: dict[str, object] = {}

    def fake_text_call(model, prompt, payload):
        import json
        captured["model"] = model
        captured["payload"] = json.loads(payload)
        return {"summary": "Customer visit Manisa, Kartonsan and Hayat factories"}

    original = model_router._text_call
    model_router._text_call = fake_text_call
    try:
        result = model_router.generate_travel_reason_summary([
            "Customer visit Manisa October 2025 — hotel stay",
            "Solo dinner Kocaeli during customer visit",
            "Bus travel Sakarya-Kocaeli — Kamil Koç to customer site",
        ])
    finally:
        model_router._text_call = original

    assert result == "Customer visit Manisa, Kartonsan and Hayat factories"
    # Sanity: payload carried the receipts the model needed
    assert isinstance(captured["payload"], dict)
    assert captured["payload"]["receipt_count"] == 3
    assert len(captured["payload"]["business_reasons"]) == 3
    print("returns-summary-when-llm-succeeds: OK")


# ---------------------------------------------------------------------------
# fallback — empty business_reasons, no LLM call
# ---------------------------------------------------------------------------


def run_returns_none_when_business_reasons_empty() -> None:
    """No receipts have business_reason text → don't call LLM, return None.
    Caller will fall back to title_prefix.
    """
    called = {"count": 0}

    def fake_text_call(model, prompt, payload):
        called["count"] += 1
        return {"summary": "should not be called"}

    original = model_router._text_call
    model_router._text_call = fake_text_call
    try:
        # All-empty input → skip LLM
        assert model_router.generate_travel_reason_summary([]) is None
        assert model_router.generate_travel_reason_summary(["", "  ", ""]) is None
    finally:
        model_router._text_call = original

    assert called["count"] == 0, (
        f"LLM should not be called when no business_reasons present; "
        f"got {called['count']} calls"
    )
    print("returns-none-when-business-reasons-empty: OK")


# ---------------------------------------------------------------------------
# fallback — LLM unavailable / returns None / returns malformed
# ---------------------------------------------------------------------------


def run_returns_none_when_llm_unavailable() -> None:
    """OpenAI not reachable (no API key, SDK missing, transient fail) →
    _text_call returns None → generate_travel_reason_summary returns None.
    """
    def fake_text_call(model, prompt, payload):
        return None

    original = model_router._text_call
    model_router._text_call = fake_text_call
    try:
        result = model_router.generate_travel_reason_summary(["Customer visit"])
    finally:
        model_router._text_call = original

    assert result is None
    print("returns-none-when-llm-unavailable: OK")


def run_returns_none_when_llm_returns_malformed() -> None:
    """Model returns a dict missing 'summary' key, or empty string, or
    non-string value → drop to None."""
    def fake_returning_no_summary(_m, _p, _payload):
        return {"other_key": "wrong shape"}

    def fake_returning_empty(_m, _p, _payload):
        return {"summary": ""}

    def fake_returning_whitespace(_m, _p, _payload):
        return {"summary": "   \t\n  "}

    def fake_returning_int(_m, _p, _payload):
        return {"summary": 42}

    original = model_router._text_call
    for fake in (fake_returning_no_summary, fake_returning_empty,
                 fake_returning_whitespace, fake_returning_int):
        model_router._text_call = fake
        try:
            assert model_router.generate_travel_reason_summary(["x"]) is None, (
                f"fake {fake.__name__} should yield None"
            )
        finally:
            model_router._text_call = original
    print("returns-none-when-llm-returns-malformed: OK")


# ---------------------------------------------------------------------------
# truncation — model exceeded the 100-char limit, we clip safely
# ---------------------------------------------------------------------------


def run_truncates_at_sentence_boundary_when_over_max_len() -> None:
    """When the model returns text longer than TRAVEL_REASON_MAX_LEN, we
    prefer to clip at a sentence boundary (. or ; or ,) so the truncated
    string still reads naturally.
    """
    long_summary = (
        "Customer visit to Manisa, visiting Kartonsan and Hayat. "
        "Also covered Sanipak in Izmir and a bus trip to Sakarya for follow-up."
    )
    assert len(long_summary) > model_router.TRAVEL_REASON_MAX_LEN

    def fake_text_call(_m, _p, _payload):
        return {"summary": long_summary}

    original = model_router._text_call
    model_router._text_call = fake_text_call
    try:
        result = model_router.generate_travel_reason_summary(["x"])
    finally:
        model_router._text_call = original

    assert result is not None
    assert len(result) <= model_router.TRAVEL_REASON_MAX_LEN
    # Sentence-boundary clip should preserve the lead clause cleanly.
    assert result.startswith("Customer visit to Manisa, visiting Kartonsan and Hayat")
    print("truncates-at-sentence-boundary: OK")


def main() -> None:
    run_returns_summary_when_llm_succeeds()
    run_returns_none_when_business_reasons_empty()
    run_returns_none_when_llm_unavailable()
    run_returns_none_when_llm_returns_malformed()
    run_truncates_at_sentence_boundary_when_over_max_len()
    print("travel_reason_summary_tests=passed")


if __name__ == "__main__":
    main()
