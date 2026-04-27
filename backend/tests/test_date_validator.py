from __future__ import annotations

from datetime import date, timedelta

from app.services.validators.date_sanity import validate_extracted_date


def test_none_extracted_date_is_valid() -> None:
    assert validate_extracted_date(None, None, date(2026, 4, 27)) == (True, None)


def test_date_inside_statement_window_is_valid() -> None:
    statement_period = (date(2025, 10, 10), date(2025, 11, 9))

    assert validate_extracted_date(date(2025, 9, 1), statement_period, date(2026, 4, 27)) == (
        True,
        None,
    )


def test_2022_date_outside_2025_statement_window_is_invalid() -> None:
    statement_period = (date(2025, 10, 10), date(2025, 11, 9))

    assert validate_extracted_date(date(2022, 5, 1), statement_period, date(2026, 4, 27)) == (
        False,
        "outside_statement_window",
    )


def test_date_inside_today_window_without_statement_is_valid() -> None:
    today = date(2026, 4, 27)

    assert validate_extracted_date(today - timedelta(days=45), None, today) == (True, None)


def test_date_two_years_ago_without_statement_is_invalid() -> None:
    today = date(2026, 4, 27)

    assert validate_extracted_date(date(2024, 4, 27), None, today) == (
        False,
        "too_far_from_today",
    )


def test_future_date_more_than_seven_days_ahead_is_invalid() -> None:
    today = date(2026, 4, 27)

    assert validate_extracted_date(today + timedelta(days=30), None, today) == (
        False,
        "future_date",
    )
