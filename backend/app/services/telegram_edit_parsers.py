"""F-AI-Stage1 PR4: free-text reply parsers for the button-driven Edit menu.

Each parser returns ``None`` on bad input. The Telegram text handler in
``telegram.py`` checks the user_response state, calls the appropriate
parser, and re-prompts on parse failure.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation


_AMOUNT_QUANT = Decimal("0.01")


def parse_supplier_reply(text: str) -> str | None:
    """Plain text. Strip whitespace. Reject empty."""
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    return cleaned or None


def parse_date_reply(text: str) -> date | None:
    """Accept ISO ``YYYY-MM-DD`` and TR ``DD.MM.YYYY``. Return ``date`` or
    ``None`` on failure. Other formats rejected to keep the parser tight
    and the prompt unambiguous."""
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def parse_amount_reply(text: str) -> tuple[Decimal, str] | None:
    """Accept ``<amount> <currency>``.

    Amount accepts both ``.`` and ``,`` as decimal separator, with thousand
    separators in the OTHER form (e.g. ``1.234,56`` or ``1,234.56``).
    Currency must be a 3-letter ISO-like code (alphabetic, uppercased).

    Returns ``(amount, currency)`` quantized to 2 decimal places, or
    ``None`` on any parse failure.
    """
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    parts = cleaned.split()
    if len(parts) < 2:
        return None
    # Currency is the last whitespace-separated token; everything before is
    # the amount (in case of thin spaces inside the number).
    raw_currency = parts[-1].upper()
    if not raw_currency.isalpha() or len(raw_currency) != 3:
        return None
    raw_amount = " ".join(parts[:-1]).replace(" ", "")
    if not raw_amount:
        return None

    if "," in raw_amount and "." in raw_amount:
        # Whichever separator appears last is the decimal separator.
        last_comma = raw_amount.rfind(",")
        last_dot = raw_amount.rfind(".")
        if last_comma > last_dot:
            normalized = raw_amount.replace(".", "").replace(",", ".")
        else:
            normalized = raw_amount.replace(",", "")
    elif "," in raw_amount:
        normalized = raw_amount.replace(",", ".")
    else:
        normalized = raw_amount

    try:
        amount = Decimal(normalized).quantize(_AMOUNT_QUANT)
    except (InvalidOperation, ValueError):
        return None
    if amount <= 0:
        return None
    return amount, raw_currency


def parse_attendees_reason_reply(text: str) -> tuple[str, str] | None:
    """Split on the first ``;`` and return ``(attendees, reason)``. Both
    sides must be non-empty after stripping. Returns ``None`` when the
    semicolon is missing or either side is empty."""
    if not isinstance(text, str):
        return None
    if ";" not in text:
        return None
    left, _, right = text.partition(";")
    attendees = left.strip()
    reason = right.strip()
    if not attendees or not reason:
        return None
    return attendees, reason
