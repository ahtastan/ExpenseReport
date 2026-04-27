"""Tests for the single-tier OCR vision pipeline (post-F1.3 rollback).

The router must:
  - call the full vision model exactly once on the happy path;
  - retry with the stricter merchant-only prompt when the first-pass
    supplier is missing — the ``UNREADABLE_MERCHANT`` sentinel,
    ``None``, or an empty/whitespace string — since all three shapes
    mean the model couldn't read the merchant masthead;
  - on retry, swap supplier from the retry response while preserving
    first-pass date / amount / currency / receipt_type;
  - NOT retry on missing date or missing amount — the merchant-only
    retry can't recover those fields, and re-extracting them would
    risk overwriting valid data with a second guess;
  - return ``None`` when the first-pass call itself produced no
    parseable response (no retry on transient API failure — retry is
    scoped to merchant ambiguity, per the F1.3 PM directive).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import model_router  # noqa: E402


def _fake_image(tmpdir: Path) -> Path:
    # The router only reads bytes for base64 encoding; a tiny file suffices.
    path = tmpdir / "receipt.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    return path


class _Recorder:
    """Stand-in for ``_call_openai`` that records calls and replays queued responses."""

    def __init__(self, responses: list[dict | None]):
        self._responses = list(responses)
        self.calls: list[str] = []

    def __call__(self, model, images, *args, **kwargs):  # matches the real signature
        self.calls.append(model)
        if not self._responses:
            return None
        return self._responses.pop(0)


def test_clean_first_pass_returns_without_retry(tmp_path, monkeypatch):
    """A clear receipt — supplier present and non-sentinel — must extract
    in a single model call. No merchant-only retry should fire."""
    rec = _Recorder([
        {"date": "2026-04-01", "supplier": "Migros", "amount": 42.5,
         "currency": "TRY", "receipt_type": "payment_receipt"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    result = model_router.vision_extract(str(_fake_image(tmp_path)))
    assert result is not None
    assert result.escalated is False
    assert rec.calls == [model_router.VISION_MODEL]
    assert result.fields["date"] == "2026-04-01"
    assert result.fields["amount"] == 42.5
    assert result.fields["supplier"] == "Migros"


def test_missing_amount_does_not_trigger_retry(tmp_path, monkeypatch):
    """Per F1.3: amount absence is NOT merchant ambiguity. The router
    must accept a null amount from the first pass and not run the
    merchant-only retry — the retry would not re-extract the amount
    anyway, and we'd rather report an honest null than a hallucinated
    figure from a second guess."""
    rec = _Recorder([
        {"date": "2026-04-01", "supplier": "Migros", "amount": None},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    result = model_router.vision_extract(str(_fake_image(tmp_path)))
    assert result is not None
    assert result.escalated is False
    assert rec.calls == [model_router.VISION_MODEL]
    assert result.fields["amount"] is None
    assert result.fields["supplier"] == "Migros"


def test_missing_date_does_not_trigger_retry(tmp_path, monkeypatch):
    """Same scoping rule as missing amount — date absence is not the
    merchant ambiguity the retry exists to fix."""
    rec = _Recorder([
        {"date": None, "supplier": "Migros", "amount": 42.5, "currency": "TRY"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    result = model_router.vision_extract(str(_fake_image(tmp_path)))
    assert result is not None
    assert result.escalated is False
    assert rec.calls == [model_router.VISION_MODEL]
    assert result.fields["date"] is None


def test_null_supplier_triggers_merchant_only_retry(tmp_path, monkeypatch):
    """A null supplier means the model couldn't read the merchant — the
    same condition the explicit sentinel signals. F1.3 patch: retry on
    null supplier as well as on the sentinel. The retry is merchant-only
    and preserves first-pass date / amount / currency, so it cannot
    blank good fields — making it safe to fire on the broader
    "supplier missing" signal."""
    rec = _Recorder([
        {"date": "2026-04-01", "supplier": None, "amount": 42.5,
         "currency": "TRY", "receipt_type": "payment_receipt"},
        {"supplier": "Migros"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    result = model_router.vision_extract(str(_fake_image(tmp_path)))
    assert result is not None
    assert result.escalated is True
    assert rec.calls == [model_router.VISION_MODEL, model_router.VISION_MODEL]
    # Supplier comes from the retry; date/amount/currency/receipt_type
    # all preserved from the first pass.
    assert result.fields["supplier"] == "Migros"
    assert result.fields["date"] == "2026-04-01"
    assert result.fields["amount"] == 42.5
    assert result.fields["currency"] == "TRY"
    assert result.fields["receipt_type"] == "payment_receipt"


def test_empty_string_supplier_triggers_merchant_only_retry(tmp_path, monkeypatch):
    """An empty (or whitespace-only) supplier string is the same kind of
    "couldn't read the masthead" signal as null. F1.3 patch: trigger the
    merchant-only retry. Whitespace-only strings are tested too because
    a model that emits a literal space character is functionally
    identical to one that emits nothing."""
    for empty_supplier in ("", "   ", "\t"):
        rec = _Recorder([
            {"date": "2026-04-01", "supplier": empty_supplier, "amount": 42.5,
             "currency": "TRY"},
            {"supplier": "Migros"},
        ])
        monkeypatch.setattr(model_router, "_vision_call", rec)
        result = model_router.vision_extract(str(_fake_image(tmp_path)))
        assert result is not None, f"empty supplier {empty_supplier!r} returned None"
        assert result.escalated is True, (
            f"empty supplier {empty_supplier!r} did not trigger retry"
        )
        assert rec.calls == [model_router.VISION_MODEL, model_router.VISION_MODEL]
        assert result.fields["supplier"] == "Migros"
        # First-pass date/amount/currency preserved across retry.
        assert result.fields["date"] == "2026-04-01"
        assert result.fields["amount"] == 42.5
        assert result.fields["currency"] == "TRY"


def test_first_pass_unavailable_returns_none_without_retry(tmp_path, monkeypatch):
    """If the first call returns ``None`` (no API key, parse failure,
    transient error), the router must surface ``None`` rather than
    burning a second LLM call. The merchant-only retry would not help
    and would just double the latency penalty for an already-failed
    extraction."""
    rec = _Recorder([None])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    result = model_router.vision_extract(str(_fake_image(tmp_path)))
    assert result is None
    assert rec.calls == [model_router.VISION_MODEL]


def test_unsupported_file_extension_makes_no_model_calls(tmp_path, monkeypatch):
    rec = _Recorder([])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    unsupported = tmp_path / "receipt.txt"
    unsupported.write_text("not an image")
    result = model_router.vision_extract(str(unsupported))
    assert result is None
    assert rec.calls == []
