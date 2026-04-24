"""Tests for the staged OCR model-routing policy.

The router must:
  - try the mini model first;
  - return the mini result when all critical fields are present;
  - escalate to the full model when the mini result is missing critical fields;
  - escalate when the mini call itself returns ``None``;
  - fall back to partial mini data if the full call also fails.
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

    def __call__(self, model, images):  # matches the real signature
        self.calls.append(model)
        if not self._responses:
            return None
        return self._responses.pop(0)


def run(tmp_dir: Path) -> None:
    img = _fake_image(tmp_dir)
    original = model_router._vision_call
    try:
        # Case 1: mini returns complete fields -> no escalation.
        rec = _Recorder([
            {"date": "2026-04-01", "supplier": "Migros", "amount": 42.5, "currency": "TRY"},
        ])
        model_router._vision_call = rec
        result = model_router.vision_extract(str(img))
        assert result is not None
        assert result.model == model_router.MINI_MODEL
        assert result.escalated is False
        assert rec.calls == [model_router.MINI_MODEL]
        print("mini-only path: OK")

        # Case 2: mini missing amount -> escalate to full.
        rec = _Recorder([
            {"date": "2026-04-01", "supplier": "Migros", "amount": None},
            {"date": "2026-04-01", "supplier": "Migros", "amount": 42.5, "currency": "TRY"},
        ])
        model_router._vision_call = rec
        result = model_router.vision_extract(str(img))
        assert result is not None
        assert result.model == model_router.FULL_MODEL
        assert result.escalated is True
        assert rec.calls == [model_router.MINI_MODEL, model_router.FULL_MODEL]
        print("escalation path: OK")

        # Case 3: mini call returns None (unavailable) -> escalate, full succeeds.
        rec = _Recorder([
            None,
            {"date": "2026-04-01", "supplier": "Migros", "amount": 42.5},
        ])
        model_router._vision_call = rec
        result = model_router.vision_extract(str(img))
        assert result is not None
        assert result.model == model_router.FULL_MODEL
        assert result.escalated is True
        print("mini-unavailable path: OK")

        # Case 4: both tiers fail -> returns None.
        rec = _Recorder([None, None])
        model_router._vision_call = rec
        result = model_router.vision_extract(str(img))
        assert result is None
        print("both-fail path: OK")

        # Case 5: mini returns partial, full unavailable -> partial mini result kept.
        rec = _Recorder([
            {"date": "2026-04-01", "supplier": None, "amount": 42.5},
            None,
        ])
        model_router._vision_call = rec
        result = model_router.vision_extract(str(img))
        assert result is not None
        assert result.model == model_router.MINI_MODEL
        assert result.escalated is False
        assert result.fields["supplier"] is None
        print("partial-mini fallback: OK")

        # Case 6: unsupported file type -> no model calls.
        unsupported = tmp_dir / "receipt.txt"
        unsupported.write_text("not an image")
        rec = _Recorder([])
        model_router._vision_call = rec
        result = model_router.vision_extract(str(unsupported))
        assert result is None
        assert rec.calls == []
        print("unsupported-file path: OK")

        print("model_router_tests=passed")
    finally:
        model_router._vision_call = original


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        run(Path(tmp))
