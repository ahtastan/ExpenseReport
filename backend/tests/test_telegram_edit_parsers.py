"""F-AI-Stage1 PR4: tests for the button-driven Edit menu's text parsers.

The parsers in app/services/telegram_edit_parsers.py are the gate between
free-text Telegram replies and the canonical receipt fields. Each parser
returns None on bad input so the dispatcher can re-prompt with a helpful
format reminder."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.telegram_edit_parsers import (
    parse_amount_reply,
    parse_attendees_reason_reply,
    parse_date_reply,
    parse_meal_context_reply,
    parse_single_field_reply,
    parse_supplier_reply,
)


# ─── supplier ────────────────────────────────────────────────────────────────


def test_parse_supplier_reply_strips_whitespace() -> None:
    assert parse_supplier_reply("  Migros  ") == "Migros"


def test_parse_supplier_reply_returns_none_for_empty() -> None:
    assert parse_supplier_reply("") is None
    assert parse_supplier_reply("   ") is None


def test_parse_supplier_reply_passes_through_unicode() -> None:
    # Turkish supplier names commonly contain non-ASCII letters.
    assert parse_supplier_reply("GÜKSOYLAR DAYANIKLI TÜK") == "GÜKSOYLAR DAYANIKLI TÜK"


# ─── date ────────────────────────────────────────────────────────────────────


def test_parse_date_reply_iso_format() -> None:
    assert parse_date_reply("2026-03-19") == date(2026, 3, 19)


def test_parse_date_reply_tr_format() -> None:
    assert parse_date_reply("19.03.2026") == date(2026, 3, 19)


def test_parse_date_reply_strips_whitespace() -> None:
    assert parse_date_reply("  2026-03-19  ") == date(2026, 3, 19)


def test_parse_date_reply_invalid_format_returns_none() -> None:
    # Slash-delimited not supported per spec — keeps the prompt unambiguous.
    assert parse_date_reply("19/03/2026") is None
    assert parse_date_reply("2026/03/19") is None
    assert parse_date_reply("March 19, 2026") is None


def test_parse_date_reply_garbage_returns_none() -> None:
    assert parse_date_reply("not a date") is None
    assert parse_date_reply("") is None
    assert parse_date_reply("9999-99-99") is None


# ─── amount ──────────────────────────────────────────────────────────────────


def test_parse_amount_reply_period_decimal() -> None:
    parsed = parse_amount_reply("755.00 TRY")
    assert parsed is not None
    amount, currency = parsed
    assert amount == Decimal("755.00")
    assert currency == "TRY"


def test_parse_amount_reply_comma_decimal() -> None:
    parsed = parse_amount_reply("755,00 TRY")
    assert parsed is not None
    amount, currency = parsed
    assert amount == Decimal("755.00")
    assert currency == "TRY"


def test_parse_amount_reply_thousand_separator_period_decimal() -> None:
    # 1,234.56 USD — comma is thousand separator, period is decimal.
    parsed = parse_amount_reply("1,234.56 USD")
    assert parsed is not None
    amount, currency = parsed
    assert amount == Decimal("1234.56")
    assert currency == "USD"


def test_parse_amount_reply_thousand_separator_comma_decimal() -> None:
    # 1.234,56 EUR — period is thousand separator, comma is decimal.
    parsed = parse_amount_reply("1.234,56 EUR")
    assert parsed is not None
    amount, currency = parsed
    assert amount == Decimal("1234.56")
    assert currency == "EUR"


def test_parse_amount_reply_lowercase_currency_uppercased() -> None:
    parsed = parse_amount_reply("100 try")
    assert parsed is not None
    _amount, currency = parsed
    assert currency == "TRY"


def test_parse_amount_reply_missing_currency_returns_none() -> None:
    assert parse_amount_reply("755.00") is None


def test_parse_amount_reply_non_iso_currency_returns_none() -> None:
    # Reject 4-letter or non-alpha tokens in the currency slot.
    assert parse_amount_reply("755.00 TRYS") is None
    assert parse_amount_reply("755.00 $") is None


def test_parse_amount_reply_zero_or_negative_returns_none() -> None:
    assert parse_amount_reply("0 TRY") is None
    assert parse_amount_reply("-10.00 TRY") is None


def test_parse_amount_reply_garbage_returns_none() -> None:
    assert parse_amount_reply("") is None
    assert parse_amount_reply("hello world") is None
    assert parse_amount_reply("abc TRY") is None


# ─── attendees + reason ──────────────────────────────────────────────────────


def test_parse_attendees_reason_reply_basic() -> None:
    parsed = parse_attendees_reason_reply("Burak, Ahmet Yılmaz; customer dinner")
    assert parsed == ("Burak, Ahmet Yılmaz", "customer dinner")


def test_parse_attendees_reason_reply_strips_around_semicolon() -> None:
    parsed = parse_attendees_reason_reply("  Hakan only  ;   team lunch  ")
    assert parsed == ("Hakan only", "team lunch")


def test_parse_attendees_reason_reply_no_semicolon_returns_none() -> None:
    assert parse_attendees_reason_reply("Hakan and Burak team lunch") is None


def test_parse_attendees_reason_reply_empty_attendees_returns_none() -> None:
    assert parse_attendees_reason_reply("; team lunch") is None


def test_parse_attendees_reason_reply_empty_reason_returns_none() -> None:
    assert parse_attendees_reason_reply("Hakan; ") is None


def test_parse_attendees_reason_reply_multiple_semicolons_uses_first_split() -> None:
    # Anything past the first ';' belongs to the reason — model can include
    # semicolons in their reason text without breaking the split.
    parsed = parse_attendees_reason_reply("Hakan; lunch; followup chat")
    assert parsed == ("Hakan", "lunch; followup chat")


@pytest.mark.parametrize("bad", [None, 12345, [], {}])
def test_parsers_return_none_for_non_string(bad) -> None:
    assert parse_supplier_reply(bad) is None
    assert parse_date_reply(bad) is None
    assert parse_amount_reply(bad) is None
    assert parse_attendees_reason_reply(bad) is None
    assert parse_single_field_reply(bad) is None
    assert parse_meal_context_reply(bad) == (None, None)


# ─── meal-context greedy parser ──────────────────────────────────────────────


def test_parse_meal_context_reply_with_semicolon_returns_both() -> None:
    assert parse_meal_context_reply("Hakan only; team lunch") == ("Hakan only", "team lunch")


def test_parse_meal_context_reply_without_semicolon_returns_attendees_only() -> None:
    """Greedy fallback: when no ``;`` separator is present, the reply is
    interpreted as attendees only (matching the prompt order). The caller
    follows up for the business reason."""
    attendees, reason = parse_meal_context_reply("Hakan and Burak")
    assert attendees == "Hakan and Burak"
    assert reason is None


def test_parse_meal_context_reply_empty_returns_both_none() -> None:
    assert parse_meal_context_reply("") == (None, None)
    assert parse_meal_context_reply("   ") == (None, None)


def test_parse_meal_context_reply_lone_semicolon_returns_both_none() -> None:
    """Lone ``;`` (both sides empty) is the only ambiguous case — the
    handler should reprompt rather than save an empty field."""
    assert parse_meal_context_reply(";") == (None, None)


def test_parse_meal_context_reply_attendees_with_empty_reason() -> None:
    """``Hakan only;`` keeps attendees but flags reason as missing for
    follow-up rather than rejecting outright (greedy)."""
    attendees, reason = parse_meal_context_reply("Hakan only;")
    assert attendees == "Hakan only"
    assert reason is None


def test_parse_meal_context_reply_empty_attendees_with_reason() -> None:
    attendees, reason = parse_meal_context_reply("; customer dinner")
    assert attendees is None
    assert reason == "customer dinner"


def test_parse_meal_context_reply_extra_semicolons_join_into_reason() -> None:
    attendees, reason = parse_meal_context_reply("Hakan; lunch; with Acme")
    assert attendees == "Hakan"
    assert reason == "lunch; with Acme"


# ─── single-field parser ─────────────────────────────────────────────────────


def test_parse_single_field_reply_strips_whitespace() -> None:
    assert parse_single_field_reply("  Hakan only  ") == "Hakan only"


def test_parse_single_field_reply_rejects_empty() -> None:
    assert parse_single_field_reply("") is None
    assert parse_single_field_reply("   ") is None


def test_parse_single_field_reply_keeps_internal_punctuation() -> None:
    # Edit-menu single-field replies for attendees / reason often include
    # commas and ``+`` separators; these must round-trip unchanged.
    assert parse_single_field_reply("Hakan + customer Ahmet, Burak") == (
        "Hakan + customer Ahmet, Burak"
    )
