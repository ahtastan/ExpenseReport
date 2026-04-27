"""Bug 1: period-ending header should reflect the BMO statement_date, not the
template's projected 14-column date drift.

Symptom on the November 2025 demo: Week 1A!M3 showed 2025-10-29 because the
template's formula chain extends 9 days past the last transaction. Fix:
override M3 with statement.statement_date when set, fall back to
max(transaction_date) when not.
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.report_generator import (  # noqa: E402
    ReportLine,
    _resolve_period_ending,
)


def _line(d: date) -> ReportLine:
    return ReportLine(
        transaction_id=1,
        review_row_id=1,
        receipt_id=1,
        receipt_path=None,
        receipt_file_name="r.jpg",
        transaction_date=d,
        supplier="X",
        amount=Decimal("1.00"),
        currency="USD",
        business_or_personal="Business",
        report_bucket="Other",
        business_reason="",
        attendees="",
    )


def test_resolve_period_ending_prefers_statement_date_when_present() -> None:
    statement_date = date(2025, 11, 10)
    lines = [_line(date(2025, 10, 10)), _line(date(2025, 10, 20))]
    assert _resolve_period_ending(statement_date, lines) == date(2025, 11, 10)


def test_resolve_period_ending_falls_back_to_max_transaction_date() -> None:
    """Manual-entry statements have no statement_date; use latest tx date."""
    statement_date = None
    lines = [_line(date(2025, 10, 10)), _line(date(2025, 10, 20)), _line(date(2025, 10, 17))]
    assert _resolve_period_ending(statement_date, lines) == date(2025, 10, 20)


def test_resolve_period_ending_returns_none_when_no_signal() -> None:
    """Both statement_date and transaction_dates absent → None.

    Caller leaves the template's existing formula in place rather than
    writing a misleading date.
    """
    assert _resolve_period_ending(None, []) is None
