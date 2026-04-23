"""Regression tests for OpenAI request parameters used by the model router."""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import model_router  # noqa: E402


class _FakeCompletions:
    def __init__(self, calls: list[dict]):
        self._calls = calls

    def create(self, **kwargs):
        self._calls.append(kwargs)
        return types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(
                        content='{"date":"2026-04-01","supplier":"Migros","amount":42.5,"currency":"TRY","summary_md":"ok"}'
                    )
                )
            ]
        )


class _FakeOpenAI:
    calls: list[dict] = []

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.chat = types.SimpleNamespace(
            completions=_FakeCompletions(self.calls),
        )


def main() -> None:
    fake_module = types.ModuleType("openai")
    fake_module.OpenAI = _FakeOpenAI

    old_openai_module = sys.modules.get("openai")
    old_key = os.environ.get("OPENAI_API_KEY")
    sys.modules["openai"] = fake_module
    os.environ["OPENAI_API_KEY"] = "test-key"
    _FakeOpenAI.calls = []
    try:
        vision = model_router._call_openai(model_router.MINI_MODEL, "image/png", "ZmFrZQ==")
        text = model_router._call_openai_text(model_router.SYNTHESIS_MODEL, "prompt", "{}")
    finally:
        if old_openai_module is None:
            sys.modules.pop("openai", None)
        else:
            sys.modules["openai"] = old_openai_module
        if old_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = old_key

    assert vision is not None
    assert text is not None
    assert len(_FakeOpenAI.calls) == 2
    for call in _FakeOpenAI.calls:
        assert "max_completion_tokens" in call
        assert "max_tokens" not in call
    print("model_router_openai_param_tests=passed")


if __name__ == "__main__":
    main()
