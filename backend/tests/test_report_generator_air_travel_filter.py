"""Bus/ferry receipts under the EDT 'Airfare/Bus/Ferry/Other' bucket must
NOT populate the AIR TRAVEL RECONCILIATION footer block; that block is
template-designed for actual flights with airline + ticket cost columns.

Bug surfaced on the November 2025 demo report where Kamil Koç + Nar Tur
bus tickets landed in rows 47-48 with empty airline / RT-oneway / ticket-#
columns, looking malformed to EDT auditors.

Fix: ``is_real_flight_line()`` is the discriminator. A line lands in the
reconciliation block only when its bucket is Airfare AND it carries either
an airline name or an explicit total_tkt_cost. Buses/ferries lack both
signals and stay confined to row 7 daily totals (via _allocate's
day["airfare"] path).
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
    AIRFARE_BUCKET,
    ReportLine,
    is_real_flight_line,
)


def _bus_line() -> ReportLine:
    """Bus ticket: Airfare bucket but no airline, no ticket_cost."""
    return ReportLine(
        transaction_id=1,
        review_row_id=1,
        receipt_id=10,
        receipt_path=None,
        receipt_file_name="kamil_koc.jpg",
        transaction_date=date(2025, 10, 17),
        supplier="Kamil Koç Sakarya Otogar",
        amount=Decimal("250.00"),
        currency="TRY",
        business_or_personal="Business",
        report_bucket=AIRFARE_BUCKET,
        business_reason="Bus travel Sakarya-Kocaeli — Kamil Koç to customer site",
        attendees="self",
        air_travel_airline=None,
        air_travel_total_tkt_cost=None,
        air_travel_rt_or_oneway=None,
    )


def _flight_line(*, airline: str | None = "Turkish Airlines",
                 ticket_cost: Decimal | None = None) -> ReportLine:
    """Flight: Airfare bucket WITH airline (or ticket_cost) — should populate
    the reconciliation block."""
    return ReportLine(
        transaction_id=2,
        review_row_id=2,
        receipt_id=1,
        receipt_path=None,
        receipt_file_name="thy.pdf",
        transaction_date=date(2025, 11, 18),
        supplier="Turkish Airlines",
        amount=Decimal("180.10"),
        currency="USD",
        business_or_personal="Business",
        report_bucket=AIRFARE_BUCKET,
        business_reason="Customer visit Sarajevo November 2025",
        attendees="self",
        air_travel_airline=airline,
        air_travel_total_tkt_cost=ticket_cost,
        air_travel_rt_or_oneway="RT" if airline else None,
    )


# ---------------------------------------------------------------------------
# 1. Bus line (no airline, no ticket cost) → NOT a real flight
# ---------------------------------------------------------------------------


def test_bus_line_under_airfare_bucket_is_not_treated_as_flight() -> None:
    bus = _bus_line()
    assert is_real_flight_line(bus) is False, (
        "bus ticket with empty airline + null ticket_cost must NOT populate "
        "the AIR TRAVEL RECONCILIATION block; daily totals (row 7) only"
    )


# ---------------------------------------------------------------------------
# 2. Flight line with airline → real flight
# ---------------------------------------------------------------------------


def test_flight_line_with_airline_is_treated_as_flight() -> None:
    flight = _flight_line(airline="Turkish Airlines")
    assert is_real_flight_line(flight) is True


# ---------------------------------------------------------------------------
# 3. Flight line with ticket_cost only (no airline filled) → still real flight
# ---------------------------------------------------------------------------


def test_flight_line_with_ticket_cost_only_is_treated_as_flight() -> None:
    flight = _flight_line(airline=None, ticket_cost=Decimal("180.10"))
    assert is_real_flight_line(flight) is True


# ---------------------------------------------------------------------------
# Edge cases — must not regress
# ---------------------------------------------------------------------------


def test_non_airfare_bucket_with_airline_metadata_is_not_a_flight() -> None:
    """Some other bucket (e.g. Hotel) with stray airline metadata is NOT
    a flight. The bucket guard comes first.
    """
    weird = _flight_line(airline="Turkish Airlines")
    weird = ReportLine(
        **{**weird.__dict__, "report_bucket": "Hotel/Lodging/Laundry"}
    )
    assert is_real_flight_line(weird) is False


def test_empty_string_airline_treated_as_no_airline() -> None:
    """Whitespace-only airline string is NOT a flight signal."""
    line = _flight_line(airline="   ")
    assert is_real_flight_line(line) is False


def test_zero_ticket_cost_is_treated_as_real_flight() -> None:
    """Decimal('0') is NOT None, so an explicit zero ticket cost still counts
    as 'operator filled in' — a flight where the entire amount was reimbursed
    against a prior ticket value (T-PRIOR-TKT-VALUE). This matches EDT's
    reconciliation expectation.
    """
    line = _flight_line(airline=None, ticket_cost=Decimal("0"))
    assert is_real_flight_line(line) is True
