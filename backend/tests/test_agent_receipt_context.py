"""F-AI-Stage1 sub-PR 2: tests for the per-user context window builder.

Pin the read-only contract of ``build_context_window``: scope by user,
filter by classification + recency + non-cancelled status, dedupe
attendees, and stay within the documented caps.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlmodel import Session

from app.models import AppUser, ReceiptDocument
from app.services.agent_receipt_context import build_context_window


def _seed_user(
    session: Session,
    *,
    telegram_user_id: int = 1,
    display_name: str | None = "Hakan",
    first_name: str | None = None,
    username: str | None = None,
) -> AppUser:
    user = AppUser(
        telegram_user_id=telegram_user_id,
        display_name=display_name,
        first_name=first_name,
        username=username,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _seed_receipt(
    session: Session,
    *,
    user: AppUser,
    business_or_personal: str | None = "Business",
    extracted_date: date | None = None,
    supplier: str = "Acme Cafe",
    bucket: str | None = "Meals/Snacks",
    amount: Decimal | None = Decimal("42.50"),
    currency: str | None = "TRY",
    attendees: str | None = None,
    status: str = "received",
    created_at: datetime | None = None,
) -> ReceiptDocument:
    receipt = ReceiptDocument(
        uploader_user_id=user.id,
        source="telegram",
        status=status,
        content_type="photo",
        extracted_date=extracted_date or date(2026, 5, 1),
        extracted_supplier=supplier,
        extracted_local_amount=amount,
        extracted_currency=currency,
        business_or_personal=business_or_personal,
        report_bucket=bucket,
        attendees=attendees,
    )
    if created_at is not None:
        receipt.created_at = created_at
        receipt.updated_at = created_at
    session.add(receipt)
    session.commit()
    session.refresh(receipt)
    return receipt


def test_empty_db_returns_empty_lists(isolated_db):
    with Session(isolated_db) as session:
        result = build_context_window(session, user_id=1)

    assert result["employees"] == []
    assert result["recent_receipts"] == []
    assert result["recent_attendees"] == []
    assert result["lookback_days"] == 2
    assert isinstance(result["fetched_at"], str)
    assert result["fetched_at"].endswith("+00:00")


def test_single_user_no_receipts(isolated_db):
    with Session(isolated_db) as session:
        user = _seed_user(session)
        result = build_context_window(session, user_id=user.id)

    assert result["employees"] == ["Hakan"]
    assert result["recent_receipts"] == []
    assert result["recent_attendees"] == []


def test_single_classified_receipt(isolated_db):
    with Session(isolated_db) as session:
        user = _seed_user(session)
        _seed_receipt(
            session,
            user=user,
            attendees="Hakan, Burak Yilmaz",
            extracted_date=date(2026, 5, 1),
            supplier="Acme Cafe",
            bucket="Meals/Snacks",
            amount=Decimal("42.50"),
            currency="TRY",
            business_or_personal="Business",
        )
        result = build_context_window(session, user_id=user.id)

    assert len(result["recent_receipts"]) == 1
    summary = result["recent_receipts"][0]
    assert summary["date"] == "2026-05-01"
    assert summary["supplier"] == "Acme Cafe"
    assert summary["bucket"] == "Meals/Snacks"
    assert summary["business_or_personal"] == "Business"
    assert summary["amount"] == 42.50
    assert summary["currency"] == "TRY"
    assert result["recent_attendees"] == ["Hakan", "Burak Yilmaz"]


def test_unclassified_receipt_excluded(isolated_db):
    with Session(isolated_db) as session:
        user = _seed_user(session)
        # No business_or_personal — must NOT appear in recent_receipts (scope B).
        _seed_receipt(session, user=user, business_or_personal=None)
        result = build_context_window(session, user_id=user.id)

    assert result["recent_receipts"] == []
    assert result["recent_attendees"] == []


def test_cancelled_receipt_excluded(isolated_db):
    with Session(isolated_db) as session:
        user = _seed_user(session)
        # The 'cancelled' status value is introduced in sub-PR 3, but the
        # DB schema accepts any string today. The forward-compatible
        # filter must already exclude it.
        _seed_receipt(session, user=user, status="cancelled")
        result = build_context_window(session, user_id=user.id)

    assert result["recent_receipts"] == []


def test_lookback_boundary(isolated_db):
    with Session(isolated_db) as session:
        user = _seed_user(session)
        anchor = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
        # 1 day old → inside the 2-day window
        inside = _seed_receipt(
            session,
            user=user,
            supplier="Inside Cafe",
            extracted_date=date(2026, 5, 1),
            attendees="Inside Person",
            created_at=anchor - timedelta(days=1),
        )
        # 3 days old → outside the 2-day window
        _seed_receipt(
            session,
            user=user,
            supplier="Outside Cafe",
            extracted_date=date(2026, 4, 28),
            attendees="Outside Person",
            created_at=anchor - timedelta(days=3),
        )
        result = build_context_window(session, user_id=user.id, now=anchor)

    suppliers = [item["supplier"] for item in result["recent_receipts"]]
    assert suppliers == ["Inside Cafe"]
    assert "Outside Person" not in result["recent_attendees"]
    assert "Inside Person" in result["recent_attendees"]
    # Sanity-check the inside row's date came through correctly.
    assert result["recent_receipts"][0]["date"] == inside.extracted_date.isoformat()


def test_attendees_dedupe(isolated_db):
    with Session(isolated_db) as session:
        user = _seed_user(session)
        # Three receipts with overlapping attendee fragments — case-
        # insensitive dedupe key, original casing preserved (first wins).
        _seed_receipt(
            session,
            user=user,
            supplier="A",
            extracted_date=date(2026, 5, 3),
            attendees="Burak Yilmaz, Ahmet",
        )
        _seed_receipt(
            session,
            user=user,
            supplier="B",
            extracted_date=date(2026, 5, 2),
            attendees="burak yilmaz",
        )
        _seed_receipt(
            session,
            user=user,
            supplier="C",
            extracted_date=date(2026, 5, 1),
            attendees="Hakan + Ahmet",
        )
        result = build_context_window(session, user_id=user.id)

    assert sorted(result["recent_attendees"]) == sorted(["Burak Yilmaz", "Ahmet", "Hakan"])
    # First-occurrence casing wins, confirms ordering preserved.
    assert "Burak Yilmaz" in result["recent_attendees"]
    assert "burak yilmaz" not in result["recent_attendees"]


def test_other_user_receipts_excluded(isolated_db):
    with Session(isolated_db) as session:
        user_a = _seed_user(session, telegram_user_id=1, display_name="Alice")
        user_b = _seed_user(session, telegram_user_id=2, display_name="Bob")
        _seed_receipt(
            session,
            user=user_b,
            supplier="Bob's Receipt",
            attendees="Mallory",
        )
        result = build_context_window(session, user_id=user_a.id)

    assert result["recent_receipts"] == []
    assert "Mallory" not in result["recent_attendees"]
    # Both users still appear in employees (workplace roster, not per-user).
    assert "Alice" in result["employees"]
    assert "Bob" in result["employees"]


def test_attendees_limit(isolated_db):
    with Session(isolated_db) as session:
        user = _seed_user(session)
        # Generate >30 distinct names across receipts (cap at 30 per spec).
        # Use a single receipt with a long attendee list to keep the
        # recent_receipts cap from interfering.
        names = [f"Person{i:02d}" for i in range(40)]
        _seed_receipt(
            session,
            user=user,
            supplier="Big Meeting",
            attendees=", ".join(names),
        )
        result = build_context_window(session, user_id=user.id)

    assert len(result["recent_attendees"]) == 30
    # First 30 wins (preserves order of appearance).
    assert result["recent_attendees"] == names[:30]


def test_recent_receipts_limit(isolated_db):
    with Session(isolated_db) as session:
        user = _seed_user(session)
        # Seed 25 receipts on consecutive days so ordering is determinate.
        for i in range(25):
            _seed_receipt(
                session,
                user=user,
                supplier=f"R{i:02d}",
                extracted_date=date(2026, 5, 1) - timedelta(days=i),
            )
        # All 25 are within the lookback window if we pretend "now" is
        # just after the most recent ``created_at``. Use a wide lookback
        # to remove the boundary as a confound.
        result = build_context_window(session, user_id=user.id, lookback_days=365)

    assert len(result["recent_receipts"]) == 20
