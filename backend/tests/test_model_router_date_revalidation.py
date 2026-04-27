from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlmodel import Session

from app.models import StatementImport
from app.services import model_router


def _fake_image(tmpdir: Path) -> Path:
    path = tmpdir / "yeni-truva.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    return path


def _seed_statement(session: Session) -> None:
    session.add(
        StatementImport(
            source_filename="diners-october.xlsx",
            period_start=date(2025, 10, 10),
            period_end=date(2025, 11, 9),
            row_count=1,
        )
    )
    session.commit()


class _Recorder:
    def __init__(self, responses: list[dict | None]):
        self._responses = list(responses)
        self.calls: list[str] = []
        self.prompts: list[str] = []

    def __call__(self, model, images, prompt=None):
        self.calls.append(model)
        self.prompts.append(prompt if prompt is not None else "<default>")
        if not self._responses:
            return None
        return self._responses.pop(0)


def test_invalid_first_pass_date_retries_with_date_context_and_keeps_retry_fields(
    isolated_db,
    monkeypatch,
    tmp_path: Path,
) -> None:
    with Session(isolated_db) as session:
        _seed_statement(session)
    recorder = _Recorder(
        [
            {
                "date": "2022-05-01",
                "supplier": "Yeni Truva Tur Pet",
                "amount": 580.0,
                "currency": "TRY",
            },
            {
                "date": "2025-11-15",
                "supplier": "Yeni Truva Tur Pet",
                "amount": 580.0,
                "currency": "TRY",
            },
        ]
    )
    monkeypatch.setattr(model_router, "_vision_call", recorder)

    result = model_router.vision_extract(str(_fake_image(tmp_path)))

    assert result is not None
    assert result.fields["date"] == "2025-11-15"
    assert result.fields["supplier"] == "Yeni Truva Tur Pet"
    assert recorder.calls == [model_router.MINI_MODEL, model_router.FULL_MODEL]
    assert len(recorder.prompts) == 2
    assert recorder.prompts[1].startswith(model_router._VISION_PROMPT_STRICT)
    assert "DATE CONTEXT: today is" in recorder.prompts[1]
    assert "Do not output dates more than 90 days in the past" in recorder.prompts[1]
    assert any("outside_statement_window" in note for note in result.notes)


def test_second_invalid_date_is_cleared_without_looping(
    isolated_db,
    monkeypatch,
    tmp_path: Path,
) -> None:
    with Session(isolated_db) as session:
        _seed_statement(session)
    recorder = _Recorder(
        [
            {
                "date": "2022-05-01",
                "supplier": "Yeni Truva Tur Pet",
                "amount": 580.0,
                "currency": "TRY",
            },
            {
                "date": "2023-05-01",
                "supplier": "Yeni Truva Tur Pet",
                "amount": 580.0,
                "currency": "TRY",
            },
        ]
    )
    monkeypatch.setattr(model_router, "_vision_call", recorder)

    result = model_router.vision_extract(str(_fake_image(tmp_path)))

    assert result is not None
    assert result.fields["date"] is None
    assert result.fields["supplier"] == "Yeni Truva Tur Pet"
    assert recorder.calls == [model_router.MINI_MODEL, model_router.FULL_MODEL]
    assert sum("outside_statement_window" in note for note in result.notes) == 2
