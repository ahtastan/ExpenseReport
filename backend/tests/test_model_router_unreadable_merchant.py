"""F1 / F1.3 hardening — OCR merchant hallucination guard.

When the OCR model can't confidently read the printed merchant name
(e.g. it's only seeing address text, district names, or VAT-line
fragments) it must emit the literal sentinel ``UNREADABLE_MERCHANT``
instead of inventing a name. The router must then retry the SAME
model with a stricter merchant-only prompt and surface the sentinel
to downstream callers as a plain ``None`` so a literal
``"UNREADABLE_MERCHANT"`` string never lands in the supplier field.

Post-F1.3 the second pass is a merchant-only stricter-prompt retry
against the same single tier (``gpt-5.4`` full by default). The
``MINI_MODEL`` / ``FULL_MODEL`` constants are kept as aliases (both
default to ``VISION_MODEL``) so existing tests' naming is preserved
without forcing a rename. The retry rewrites supplier ONLY — the
first-pass date / amount / currency / receipt_type are preserved
verbatim. A merchant-side problem must never blank a date or amount
that the first pass extracted cleanly. That preservation contract is
pinned in ``test_unreadable_merchant_retry_preserves_first_pass_*``
below.

These tests mock ``_vision_call`` so they exercise the prompt-retry
contract without hitting the live OpenAI API. A real-OCR integration
test against the user's October receipts is a separate, gated
acceptance check (see ``test_model_router_live_ocr.py``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import model_router  # noqa: E402
from app.services.model_router import UNREADABLE_MERCHANT_SENTINEL  # noqa: E402


def _fake_image(tmpdir: Path) -> Path:
    path = tmpdir / "receipt.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    return path


class _Recorder:
    """Stand-in for ``_call_openai`` that records (model, prompt) per call
    and replays queued responses. The prompt is captured so tests can
    verify the second pass uses the stricter retry variant rather than
    the standard prompt — that's the F1-era replacement for the prior
    mini→full tier upgrade.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[str] = []
        self.prompts: list[str] = []

    def __call__(self, model, images, prompt=None):
        self.calls.append(model)
        self.prompts.append(prompt if prompt is not None else "<default>")
        if not self._responses:
            return None
        return self._responses.pop(0)


def _patch_vision_call(monkeypatch, recorder):
    monkeypatch.setattr(model_router, "_vision_call", recorder)


# ---------------------------------------------------------------------------
# Sentinel constant + prompt contract
# ---------------------------------------------------------------------------


def test_unreadable_merchant_sentinel_is_stable_keyword() -> None:
    """The sentinel string is the contract between the prompt and the
    router. If you rename it, both sides must change in lockstep — this
    test pins the value so a silent prompt-only edit can't drift apart
    from the router's escalation logic."""
    assert UNREADABLE_MERCHANT_SENTINEL == "UNREADABLE_MERCHANT"


def test_vision_prompt_instructs_model_to_emit_sentinel_on_ambiguous_merchant() -> None:
    """The prompt must explicitly tell the model to abstain with the
    sentinel rather than guessing — this is what stops hallucinations
    like 'Yeni Truva Tur Pet' becoming 'Meydan Cafe Market'.
    """
    prompt = model_router._VISION_PROMPT
    assert "UNREADABLE_MERCHANT" in prompt
    assert "EXACTLY as printed" in prompt
    # The prompt must explicitly forbid composing names from address /
    # VAT / context — that's the hallucination class we're guarding.
    assert "address" in prompt.lower()
    assert "DO NOT guess" in prompt or "DO NOT infer" in prompt


# ---------------------------------------------------------------------------
# _count_missing — sentinel must be treated like a missing supplier
# ---------------------------------------------------------------------------


def test_count_missing_treats_unreadable_merchant_as_missing_supplier() -> None:
    """The sentinel value must trigger the same escalation path as a
    null supplier. If this stays unchecked, the router would happily
    return a ``VisionResult`` whose supplier is the literal string
    'UNREADABLE_MERCHANT'."""
    fields = {
        "date": "2026-04-15",
        "supplier": UNREADABLE_MERCHANT_SENTINEL,
        "amount": 42.5,
        "currency": "TRY",
    }
    missing = model_router._count_missing(fields)
    assert "supplier" in missing


def test_count_missing_treats_lowercased_sentinel_as_missing_supplier() -> None:
    """Tolerate model-side casing drift — 'unreadable_merchant' or
    ' UNREADABLE_MERCHANT \\n' must still be recognized."""
    for variant in ("unreadable_merchant", "  UNREADABLE_MERCHANT  ", "Unreadable_Merchant"):
        fields = {"date": "2026-04-15", "supplier": variant, "amount": 1.0}
        assert "supplier" in model_router._count_missing(fields), (
            f"variant {variant!r} did not trigger missing-supplier"
        )


def test_count_missing_does_not_flag_normal_supplier() -> None:
    fields = {"date": "2026-04-15", "supplier": "Migros", "amount": 42.5}
    assert "supplier" not in model_router._count_missing(fields)


# ---------------------------------------------------------------------------
# vision_extract — sentinel forces escalation; final fields surface as None
# ---------------------------------------------------------------------------


def test_first_pass_returning_unreadable_merchant_triggers_stricter_retry(monkeypatch) -> None:
    """The headline F1 invariant: if the first pass abstains with the
    sentinel, the router must retry — same model, stricter prompt — and
    return the retry's result.
    """
    recorder = _Recorder([
        {"date": "2025-10-15", "supplier": UNREADABLE_MERCHANT_SENTINEL,
         "amount": 580.0, "currency": "TRY"},
        {"date": "2025-10-15", "supplier": "Yeni Truva Tur Pet",
         "amount": 580.0, "currency": "TRY"},
    ])
    _patch_vision_call(monkeypatch, recorder)
    with TemporaryDirectory() as tmp:
        img = _fake_image(Path(tmp))
        result = model_router.vision_extract(str(img))
    assert result is not None
    assert len(recorder.calls) == 2, "expected exactly two passes (first + stricter retry)"
    assert result.escalated is True
    assert result.fields["supplier"] == "Yeni Truva Tur Pet"


_HISTORICAL_HALLUCINATION = "Meydan Cafe Market"
_REAL_MASTHEAD = "Yeni Truva Tur Pet"


def test_historical_hallucination_never_returned_under_new_prompt_flow(monkeypatch) -> None:
    """Pin the named regression: the broken pre-F1 pipeline turned the
    receipt at ``tests/fixtures/receipts/yeni_truva_misread.jpg`` into
    'Meydan Cafe Market' — composed from neighborhood/address text
    rather than read from the masthead. Under the new flow, when the
    first pass abstains with the sentinel and the stricter retry returns
    the real masthead, the historical hallucination string must never
    appear in the supplier field.

    A live-OCR counterpart in ``test_model_router_live_ocr.py`` runs the
    same assertion against the actual model — this unit test guards the
    contract at the router-logic layer, independent of model behavior.
    """
    recorder = _Recorder([
        {"date": "2025-10-12", "supplier": UNREADABLE_MERCHANT_SENTINEL,
         "amount": 580.0, "currency": "TRY"},
        {"date": "2025-10-12", "supplier": _REAL_MASTHEAD,
         "amount": 580.0, "currency": "TRY"},
    ])
    _patch_vision_call(monkeypatch, recorder)
    with TemporaryDirectory() as tmp:
        img = _fake_image(Path(tmp))
        result = model_router.vision_extract(str(img))
    assert result is not None
    supplier = (result.fields.get("supplier") or "").upper()
    assert _HISTORICAL_HALLUCINATION.upper() not in supplier, (
        f"F1 regression: supplier {supplier!r} contains the historical "
        f"hallucination string {_HISTORICAL_HALLUCINATION!r}"
    )
    assert "TRUVA" in supplier, (
        f"expected the real masthead substring; got {supplier!r}"
    )


def test_stricter_retry_uses_strict_prompt_not_default_prompt(monkeypatch) -> None:
    """The new F1 mechanism is a *prompt swap*, not a tier upgrade —
    pin that contract here. First pass uses ``_VISION_PROMPT``; on
    retry, ``_VISION_PROMPT_STRICT`` must be passed. Without this guard
    the stricter rules in the retry prompt could silently regress to the
    standard prompt and the hallucination antipatterns would re-emerge.
    """
    recorder = _Recorder([
        {"date": "2025-10-15", "supplier": UNREADABLE_MERCHANT_SENTINEL,
         "amount": 580.0, "currency": "TRY"},
        {"date": "2025-10-15", "supplier": "Yeni Truva Tur Pet",
         "amount": 580.0, "currency": "TRY"},
    ])
    _patch_vision_call(monkeypatch, recorder)
    with TemporaryDirectory() as tmp:
        img = _fake_image(Path(tmp))
        model_router.vision_extract(str(img))
    assert len(recorder.prompts) == 2
    first_prompt, retry_prompt = recorder.prompts
    # First pass: default prompt (or explicit standard).
    assert first_prompt in (model_router._VISION_PROMPT, "<default>")
    # Retry: must be the strict variant.
    assert retry_prompt == model_router._VISION_PROMPT_STRICT, (
        "stricter retry must pass _VISION_PROMPT_STRICT — silent fallback "
        "to the default prompt would regress F1's hallucination guard"
    )


def test_full_model_also_returning_sentinel_surfaces_as_null_supplier(monkeypatch) -> None:
    """If both tiers abstain, downstream callers must see ``None`` —
    never the literal 'UNREADABLE_MERCHANT' string — for supplier."""
    recorder = _Recorder([
        {"date": "2025-10-15", "supplier": UNREADABLE_MERCHANT_SENTINEL,
         "amount": 580.0, "currency": "TRY"},
        {"date": "2025-10-15", "supplier": UNREADABLE_MERCHANT_SENTINEL,
         "amount": 580.0, "currency": "TRY"},
    ])
    _patch_vision_call(monkeypatch, recorder)
    with TemporaryDirectory() as tmp:
        img = _fake_image(Path(tmp))
        result = model_router.vision_extract(str(img))
    assert result is not None
    assert result.escalated is False
    assert result.fields["supplier"] is None, (
        "literal UNREADABLE_MERCHANT must never reach downstream callers"
    )


def test_real_supplier_runs_header_retry_before_returning(monkeypatch) -> None:
    """F1.9: normal image receipts get supplier/header retry too, so
    wrong-but-confident merchant OCR can be corrected before save."""
    recorder = _Recorder([
        {"date": "2025-10-15", "supplier": "Migros", "amount": 42.5,
         "currency": "TRY"},
        {"supplier": "Migros"},
    ])
    _patch_vision_call(monkeypatch, recorder)
    with TemporaryDirectory() as tmp:
        img = _fake_image(Path(tmp))
        result = model_router.vision_extract(str(img))
    assert result is not None
    assert recorder.calls == [model_router.VISION_MODEL, model_router.VISION_MODEL]
    assert recorder.prompts == ["<default>", model_router._VISION_PROMPT_STRICT]
    assert result.escalated is True
    assert result.fields["supplier"] == "Migros"


def test_mini_sentinel_full_unavailable_returns_partial_with_null_supplier(monkeypatch) -> None:
    """Edge: mini abstains, full call fails entirely. We keep the partial
    mini fields so date/amount still flow into matching, but the supplier
    must be normalized to ``None`` (not the sentinel string)."""
    recorder = _Recorder([
        {"date": "2025-10-15", "supplier": UNREADABLE_MERCHANT_SENTINEL,
         "amount": 580.0, "currency": "TRY"},
        None,
    ])
    _patch_vision_call(monkeypatch, recorder)
    with TemporaryDirectory() as tmp:
        img = _fake_image(Path(tmp))
        result = model_router.vision_extract(str(img))
    assert result is not None
    assert result.model == model_router.MINI_MODEL
    assert result.escalated is False
    assert result.fields["supplier"] is None
    assert result.fields["amount"] == 580.0


# ---------------------------------------------------------------------------
# F1.3 — merchant-only retry must preserve first-pass date/amount/currency
# ---------------------------------------------------------------------------


def test_unreadable_merchant_retry_preserves_first_pass_date_amount_currency(monkeypatch) -> None:
    """F1.3 invariant: the merchant-only retry rewrites supplier ONLY.

    A previous version of the retry path re-extracted every field from
    the second response, which meant a supplier-side problem on a
    receipt could silently blank a perfectly-good date or amount the
    first pass already captured. PM directive: never blank date/amount
    just because the merchant masthead was ambiguous.

    Here the retry deliberately returns DIFFERENT date/amount values to
    prove they are ignored — only supplier from the retry survives.
    """
    recorder = _Recorder([
        # First pass: clean date+amount, abstained on merchant.
        {"date": "2025-10-15", "supplier": UNREADABLE_MERCHANT_SENTINEL,
         "amount": 580.0, "currency": "TRY", "receipt_type": "payment_receipt"},
        # Retry: hostile values for date/amount/currency to confirm the
        # merge ignores them. Only supplier should propagate.
        {"date": "1999-01-01", "supplier": "Yeni Truva Tur Pet",
         "amount": 9999.99, "currency": "EUR", "receipt_type": "invoice"},
    ])
    _patch_vision_call(monkeypatch, recorder)
    with TemporaryDirectory() as tmp:
        img = _fake_image(Path(tmp))
        result = model_router.vision_extract(str(img))
    assert result is not None
    assert result.escalated is True
    assert result.fields["supplier"] == "Yeni Truva Tur Pet"
    # Date / amount / currency / receipt_type all from the FIRST pass.
    assert result.fields["date"] == "2025-10-15"
    assert result.fields["amount"] == 580.0
    assert result.fields["currency"] == "TRY"
    assert result.fields["receipt_type"] == "payment_receipt"


def test_unreadable_merchant_retry_returning_only_supplier_still_merges(monkeypatch) -> None:
    """The merchant-only stricter prompt asks for ``{"supplier": ...}``
    alone — nothing else. A retry response that obeys that contract
    (no date / amount keys at all) must still merge cleanly: the
    first-pass date/amount stay, and supplier comes from the retry.
    """
    recorder = _Recorder([
        {"date": "2025-10-15", "supplier": UNREADABLE_MERCHANT_SENTINEL,
         "amount": 580.0, "currency": "TRY"},
        {"supplier": "Yeni Truva Tur Pet"},  # merchant-only response shape
    ])
    _patch_vision_call(monkeypatch, recorder)
    with TemporaryDirectory() as tmp:
        img = _fake_image(Path(tmp))
        result = model_router.vision_extract(str(img))
    assert result is not None
    assert result.escalated is True
    assert result.fields["supplier"] == "Yeni Truva Tur Pet"
    assert result.fields["date"] == "2025-10-15"
    assert result.fields["amount"] == 580.0
    assert result.fields["currency"] == "TRY"


def test_strict_retry_prompt_is_merchant_only(monkeypatch) -> None:
    """The F1.3 stricter retry prompt must be scoped to merchant
    extraction only. It must not re-instruct the model on amount or
    date selection — those fields were already captured cleanly on the
    first pass. Re-instructing them risks the model second-guessing a
    valid date/amount and returning a different value, which the merge
    is supposed to ignore — but the cheapest defense is a prompt that
    doesn't even bring those fields up."""
    prompt = model_router._VISION_PROMPT_STRICT
    # The merchant rules must still be present.
    assert "MERCHANT NAME" in prompt
    assert UNREADABLE_MERCHANT_SENTINEL in prompt
    # The retry prompt must NOT instruct on amount or date selection —
    # those fields are owned by the first pass under F1.3.
    lowered = prompt.lower()
    assert "total amount" not in lowered, (
        "F1.3 retry prompt must not re-instruct on amount selection — "
        "the first pass already captured amount; a retry is supposed to "
        "rewrite supplier only."
    )
    assert "tutar" not in lowered, (
        "F1.3 retry prompt must not reference amount labels — same reason."
    )
    # The prompt must explicitly state it's asking for supplier only.
    assert "supplier" in lowered
