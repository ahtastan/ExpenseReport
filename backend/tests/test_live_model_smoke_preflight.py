"""Regression check for live model smoke dependency preflight."""

from __future__ import annotations

import builtins
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import run_live_model_smoke  # noqa: E402


def main() -> None:
    original_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "openai" or name.startswith("openai."):
            raise ModuleNotFoundError("No module named 'openai'")
        return original_import(name, *args, **kwargs)

    builtins.__import__ = blocked_import
    try:
        status = run_live_model_smoke._openai_sdk_status()
    finally:
        builtins.__import__ = original_import

    assert status["ok"] is False
    assert status["step"] == "preflight"
    assert "openai" in status["reason"].lower()
    assert "pip install" in status["hint"]
    print("live_model_smoke_preflight_tests=passed")


if __name__ == "__main__":
    main()
