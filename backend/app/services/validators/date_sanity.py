from __future__ import annotations

from datetime import date, timedelta


def validate_extracted_date(
    extracted_date: date | None,
    statement_period: tuple[date, date] | None,
    today: date,
) -> tuple[bool, str | None]:
    if extracted_date is None:
        return True, None

    future_limit = today + timedelta(days=7)
    if extracted_date > future_limit:
        return False, "future_date"

    if statement_period is not None:
        period_start, period_end = statement_period
        window_start = period_start - timedelta(days=60)
        window_end = period_end + timedelta(days=60)
        if extracted_date < window_start or extracted_date > window_end:
            return False, "outside_statement_window"
        return True, None

    window_start = today - timedelta(days=90)
    if extracted_date < window_start or extracted_date > future_limit:
        return False, "too_far_from_today"
    return True, None
