"""Tests for the single-tier OCR vision pipeline (post-F1.3 rollback).

The router must:
  - call the full vision model exactly once on the happy path;
  - retry with the stricter merchant-only prompt when the first-pass
    supplier is missing — the ``UNREADABLE_MERCHANT`` sentinel,
    ``None``, or an empty/whitespace string — since all three shapes
    mean the model couldn't read the merchant masthead;
  - on retry, swap supplier from the retry response while preserving
    first-pass date / amount / currency / receipt_type;
  - run focused date-only and amount-only retries when those fields are
    missing, without overwriting clean first-pass values;
  - return ``None`` when the first-pass call itself produced no
    parseable response (focused retries require a valid first-pass payload).
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
        self.prompts: list[str] = []

    def __call__(self, model, images, *args, **kwargs):  # matches the real signature
        self.calls.append(model)
        prompt = args[0] if args else kwargs.get("prompt")
        self.prompts.append(prompt if prompt is not None else "<default>")
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


def test_missing_amount_triggers_amount_only_retry(tmp_path, monkeypatch):
    """F1.4: amount absence gets one focused amount/currency retry."""
    rec = _Recorder([
        {"date": "2026-04-01", "supplier": "Migros", "amount": None},
        {"amount": 42.5, "currency": "TRY"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    result = model_router.vision_extract(str(_fake_image(tmp_path)))
    assert result is not None
    assert result.escalated is True
    assert rec.calls == [model_router.VISION_MODEL, model_router.VISION_MODEL]
    assert rec.prompts[1] == model_router._VISION_PROMPT_AMOUNT_ONLY
    assert result.fields["amount"] == 42.5
    assert result.fields["currency"] == "TRY"
    assert result.fields["date"] == "2026-04-01"
    assert result.fields["supplier"] == "Migros"


def test_missing_date_triggers_date_only_retry(tmp_path, monkeypatch):
    """F1.4: date absence gets one focused date retry."""
    rec = _Recorder([
        {"date": None, "supplier": "Migros", "amount": 42.5, "currency": "TRY"},
        {"date": "2026-04-01"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    result = model_router.vision_extract(str(_fake_image(tmp_path)))
    assert result is not None
    assert result.escalated is True
    assert rec.calls == [model_router.VISION_MODEL, model_router.VISION_MODEL]
    assert rec.prompts[1] == model_router._VISION_PROMPT_DATE_ONLY
    assert result.fields["date"] == "2026-04-01"
    assert result.fields["amount"] == 42.5
    assert result.fields["currency"] == "TRY"
    assert result.fields["supplier"] == "Migros"


def test_missing_date_does_not_trigger_amount_or_supplier_retry(tmp_path, monkeypatch):
    rec = _Recorder([
        {"date": None, "supplier": "Migros", "amount": 42.5, "currency": "TRY"},
        {"date": "2026-04-01"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    result = model_router.vision_extract(str(_fake_image(tmp_path)))
    assert result is not None
    assert rec.calls == [model_router.VISION_MODEL, model_router.VISION_MODEL]
    assert rec.prompts == ["<default>", model_router._VISION_PROMPT_DATE_ONLY]


def test_missing_amount_does_not_trigger_date_or_supplier_retry(tmp_path, monkeypatch):
    rec = _Recorder([
        {"date": "2026-04-01", "supplier": "Migros", "amount": None, "currency": "TRY"},
        {"amount": 42.5, "currency": "TRY"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    result = model_router.vision_extract(str(_fake_image(tmp_path)))
    assert result is not None
    assert rec.calls == [model_router.VISION_MODEL, model_router.VISION_MODEL]
    assert rec.prompts == ["<default>", model_router._VISION_PROMPT_AMOUNT_ONLY]


def test_missing_currency_retries_without_overwriting_first_pass_amount(tmp_path, monkeypatch):
    rec = _Recorder([
        {"date": "2026-04-01", "supplier": "Migros", "amount": 42.5, "currency": None},
        {"amount": 9999.99, "currency": "TRY", "date": "1999-01-01", "supplier": "Wrong"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    result = model_router.vision_extract(str(_fake_image(tmp_path)))
    assert result is not None
    assert result.escalated is True
    assert rec.calls == [model_router.VISION_MODEL, model_router.VISION_MODEL]
    assert rec.prompts[1] == model_router._VISION_PROMPT_AMOUNT_ONLY
    assert result.fields["date"] == "2026-04-01"
    assert result.fields["supplier"] == "Migros"
    assert result.fields["amount"] == 42.5
    assert result.fields["currency"] == "TRY"


def test_focused_retries_only_fill_missing_fields(tmp_path, monkeypatch):
    rec = _Recorder([
        {"date": None, "supplier": None, "amount": None, "currency": None,
         "receipt_type": "payment_receipt"},
        {"supplier": "Migros", "date": "1999-01-01", "amount": 9999.99},
        {"date": "2026-04-01", "supplier": "Wrong", "amount": 9999.99},
        {"amount": 42.5, "currency": "TRY", "date": "1999-01-01", "supplier": "Wrong"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    result = model_router.vision_extract(str(_fake_image(tmp_path)))
    assert result is not None
    assert result.escalated is True
    assert rec.prompts == [
        "<default>",
        model_router._VISION_PROMPT_STRICT,
        model_router._VISION_PROMPT_DATE_ONLY,
        model_router._VISION_PROMPT_AMOUNT_ONLY,
    ]
    assert result.fields["supplier"] == "Migros"
    assert result.fields["date"] == "2026-04-01"
    assert result.fields["amount"] == 42.5
    assert result.fields["currency"] == "TRY"
    assert result.fields["receipt_type"] == "payment_receipt"


def test_failed_focused_retries_keep_first_pass_fields(tmp_path, monkeypatch):
    rec = _Recorder([
        {"date": None, "supplier": "Migros", "amount": 42.5, "currency": "TRY"},
        {"date": None, "amount": 9999.99, "supplier": "Wrong"},
    ])
    monkeypatch.setattr(model_router, "_vision_call", rec)
    result = model_router.vision_extract(str(_fake_image(tmp_path)))
    assert result is not None
    assert result.escalated is False
    assert rec.calls == [model_router.VISION_MODEL, model_router.VISION_MODEL]
    assert rec.prompts == ["<default>", model_router._VISION_PROMPT_DATE_ONLY]
    assert result.fields["date"] is None
    assert result.fields["supplier"] == "Migros"
    assert result.fields["amount"] == 42.5
    assert result.fields["currency"] == "TRY"


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
