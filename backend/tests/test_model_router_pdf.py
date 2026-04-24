"""Unit tests for PDF support in the staged vision pipeline (B4).

The router must:
  - rasterize every page of a PDF to base64 PNGs via ``_read_pdf_pages_b64``;
  - return None (not raise) for missing / unopenable files;
  - cap at ``max_pages`` for pathologically large PDFs;
  - send ALL pages as image_url blocks in a single vision call so the model
    sees the whole document at once;
  - skip the vision call entirely when the PDF cannot be rasterized.
"""

from __future__ import annotations

import base64
import io
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import model_router  # noqa: E402


FIXTURE_PDF = Path(__file__).parent / "fixtures" / "minimal_receipt.pdf"


def _make_multipage_pdf(num_pages: int) -> bytes:
    """Build an in-memory multi-page PDF without adding reportlab as a dep.

    Uses pypdfium2's editor API (already a runtime dep) to stamp blank pages.
    """
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument.new()
    for _ in range(num_pages):
        pdf.new_page(612, 792)
    buffer = io.BytesIO()
    pdf.save(buffer)
    pdf.close()
    return buffer.getvalue()


def test_read_pdf_pages_b64_returns_list_for_valid_pdf():
    pages = model_router._read_pdf_pages_b64(str(FIXTURE_PDF))
    assert isinstance(pages, list)
    assert len(pages) >= 1
    for page_b64 in pages:
        assert isinstance(page_b64, str)
        decoded = base64.b64decode(page_b64)
        assert decoded[:8] == b"\x89PNG\r\n\x1a\n"


def test_read_pdf_pages_b64_returns_none_for_missing_file(tmp_path: Path):
    missing = tmp_path / "does_not_exist.pdf"
    assert model_router._read_pdf_pages_b64(str(missing)) is None


def test_read_pdf_pages_b64_respects_max_pages(tmp_path: Path):
    pdf_path = tmp_path / "many.pdf"
    pdf_path.write_bytes(_make_multipage_pdf(15))
    pages = model_router._read_pdf_pages_b64(str(pdf_path), max_pages=5)
    assert isinstance(pages, list)
    assert len(pages) == 5


def test_vision_extract_pdf_uses_multi_image_payload(monkeypatch):
    captured: dict = {}

    def fake_vision_call(model, images):
        captured["model"] = model
        captured["images"] = images
        return {
            "date": "2025-08-29",
            "supplier": "HOTEL TEST CO",
            "amount": 3500.0,
            "currency": "TRY",
        }

    monkeypatch.setattr(model_router, "_vision_call", fake_vision_call)

    result = model_router.vision_extract(str(FIXTURE_PDF))

    assert result is not None
    assert result.fields["supplier"] == "HOTEL TEST CO"
    assert result.fields["amount"] == 3500.0
    # images list has one (media_type, b64) tuple per rasterized page
    assert isinstance(captured["images"], list)
    assert len(captured["images"]) >= 1
    for media_type, b64 in captured["images"]:
        assert media_type == "image/png"
        assert isinstance(b64, str) and len(b64) > 0


def test_vision_extract_pdf_returns_none_if_pdf_unreadable(monkeypatch):
    monkeypatch.setattr(model_router, "_read_pdf_pages_b64", lambda *a, **kw: None)
    vision_call_mock = mock.Mock()
    monkeypatch.setattr(model_router, "_vision_call", vision_call_mock)

    result = model_router.vision_extract("/does/not/matter.pdf")

    assert result is None
    vision_call_mock.assert_not_called()
