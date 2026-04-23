"""Regression check for live smoke report-template path selection."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import run_live_model_smoke  # noqa: E402


def main() -> None:
    template = run_live_model_smoke.WORKSPACE_ROOT / "Expense Report Form_Blank.xlsx"
    selected = run_live_model_smoke._first_existing([Path(""), template])

    assert selected == template
    assert selected.is_file()
    assert selected.suffix == ".xlsx"
    print("live_model_smoke_template_path_tests=passed")


if __name__ == "__main__":
    main()
