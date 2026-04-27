"""F1.5 hardening — clarification queue must be receipt-scoped.

Pre-F1.5, ``next_open_question_for_user`` returned the oldest open
clarification across *all* the user's receipts. The Telegram handler
displays the just-uploaded receipt's ``questions[0].question_text``
to the user (newest receipt's first question), but routed the
user's text reply through ``next_open_question_for_user`` for
dispatch. When any older receipt had a stale open question, those
two paths diverged: the bot would say "what is the amount?" for
receipt N, the user would type ``"680.00 TRY"``, and the answer
would dispatch against receipt M's ``supplier`` question — the
exclusive branch in ``answer_question`` would write
``extracted_supplier="680.00 TRY"`` on the wrong receipt and leave
the right receipt's ``extracted_local_amount`` NULL.

Post-F1.5 the queue is scoped to the *most recently active*
receipt: whichever receipt has the most-recently-created open
question is the one whose oldest open question gets the next
answer. The display ("we just asked Q for receipt N") and the
dispatch (the answer goes to that same Q) now line up.

These tests pin both the unit-level ordering contract on
``next_open_question_for_user`` *and* the end-to-end Telegram
flow that the production incident exhibited.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

VERIFY_ROOT = Path.cwd() / ".verify_data"
VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = (
    f"sqlite:///{VERIFY_ROOT / f'clar_queue_scope_{uuid4().hex}.db'}"
)
os.environ["EXPENSE_STORAGE_ROOT"] = str(VERIFY_ROOT)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlmodel import Session, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import create_db_and_tables, engine  # noqa: E402
from app.models import AppUser, ClarificationQuestion, ReceiptDocument  # noqa: E402
from app.services import telegram as telegram_service  # noqa: E402
from app.services.clarifications import next_open_question_for_user  # noqa: E402


class _FakeTelegramClient:
    def __init__(self, token: str | None = None) -> None:
        self.token = token
        self.messages: list[str] = []

    def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append(text)

    def download_file(self, *args, **kwargs):  # pragma: no cover
        return None


def _seed_user(session: Session, telegram_user_id: int) -> AppUser:
    user = AppUser(telegram_user_id=telegram_user_id)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _seed_receipt(session: Session, user_id: int, name: str) -> ReceiptDocument:
    receipt = ReceiptDocument(
        uploader_user_id=user_id,
        original_file_name=name,
        status="received",
    )
    session.add(receipt)
    session.commit()
    session.refresh(receipt)
    return receipt


def _seed_question(
    session: Session,
    receipt: ReceiptDocument,
    user: AppUser,
    key: str,
    text: str,
    created_at: datetime,
) -> ClarificationQuestion:
    q = ClarificationQuestion(
        receipt_document_id=receipt.id,
        user_id=user.id,
        question_key=key,
        question_text=text,
        created_at=created_at,
    )
    session.add(q)
    session.commit()
    session.refresh(q)
    return q


def test_routes_to_most_recently_active_receipt_not_global_fifo() -> None:
    """The pre-F1.5 bug, in unit form.

    User has an older receipt R1 with a stale open ``supplier``
    question, and a newer receipt R2 with an open ``local_amount``
    question. Pre-F1.5 the global FIFO returns R1's supplier (oldest
    overall). Post-F1.5 the queue is scoped to R2 (most recently
    active receipt) and returns R2's local_amount.
    """
    create_db_and_tables()
    with Session(engine) as session:
        user = _seed_user(session, telegram_user_id=900001)
        r1 = _seed_receipt(session, user.id, "old_receipt.jpg")
        r2 = _seed_receipt(session, user.id, "new_receipt.jpg")

        t0 = datetime(2026, 4, 20, 10, 0, 0, tzinfo=timezone.utc)
        _seed_question(
            session, r1, user,
            key="supplier",
            text="I could not read the merchant name. Which store...?",
            created_at=t0,
        )
        _seed_question(
            session, r2, user,
            key="local_amount",
            text="I could not read the receipt amount. What is the total?",
            created_at=t0 + timedelta(hours=2),
        )

        nxt = next_open_question_for_user(session, user.id)
        assert nxt is not None
        assert nxt.receipt_document_id == r2.id, (
            "queue must scope to most recently active receipt; "
            f"got receipt_id={nxt.receipt_document_id}, expected {r2.id}"
        )
        assert nxt.question_key == "local_amount"


def test_within_receipt_fifo_preserved() -> None:
    """Within the active receipt, oldest open question still wins."""
    create_db_and_tables()
    with Session(engine) as session:
        user = _seed_user(session, telegram_user_id=900002)
        r = _seed_receipt(session, user.id, "single.jpg")

        t0 = datetime(2026, 4, 21, 10, 0, 0, tzinfo=timezone.utc)
        first = _seed_question(
            session, r, user,
            key="local_amount",
            text="amount?",
            created_at=t0,
        )
        _seed_question(
            session, r, user,
            key="supplier",
            text="merchant?",
            created_at=t0 + timedelta(seconds=1),
        )

        nxt = next_open_question_for_user(session, user.id)
        assert nxt is not None
        assert nxt.id == first.id
        assert nxt.question_key == "local_amount"


def test_drains_active_receipt_then_falls_back_to_older_receipt() -> None:
    """After active receipt's queue is drained, next call moves to the
    next most-recently-active receipt's oldest open question."""
    create_db_and_tables()
    with Session(engine) as session:
        user = _seed_user(session, telegram_user_id=900003)
        r1 = _seed_receipt(session, user.id, "older.jpg")
        r2 = _seed_receipt(session, user.id, "newer.jpg")

        t0 = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        r1_supplier = _seed_question(
            session, r1, user,
            key="supplier",
            text="merchant on older?",
            created_at=t0,
        )
        r2_amount = _seed_question(
            session, r2, user,
            key="local_amount",
            text="amount on newer?",
            created_at=t0 + timedelta(hours=1),
        )

        # First call returns r2's amount (most recent receipt).
        first = next_open_question_for_user(session, user.id)
        assert first is not None
        assert first.id == r2_amount.id

        # Drain r2 by marking it answered.
        first.status = "answered"
        first.answered_at = datetime.now(timezone.utc)
        session.add(first)
        session.commit()

        # Now next call falls back to r1's supplier — older receipt
        # gets attention, but only after newer one is fully drained.
        second = next_open_question_for_user(session, user.id)
        assert second is not None
        assert second.id == r1_supplier.id


def test_answered_questions_do_not_define_active_receipt() -> None:
    """If the most recent question on a receipt is *answered*, that
    receipt is no longer 'active' — we should look at the next-newest
    *open* question. Catches a regression where ``status="open"``
    filtering was forgotten on the active-receipt lookup."""
    create_db_and_tables()
    with Session(engine) as session:
        user = _seed_user(session, telegram_user_id=900004)
        r1 = _seed_receipt(session, user.id, "older.jpg")
        r2 = _seed_receipt(session, user.id, "newer.jpg")

        t0 = datetime(2026, 4, 23, 10, 0, 0, tzinfo=timezone.utc)
        r1_open = _seed_question(
            session, r1, user,
            key="supplier",
            text="merchant on older?",
            created_at=t0,
        )
        r2_answered = _seed_question(
            session, r2, user,
            key="local_amount",
            text="amount on newer?",
            created_at=t0 + timedelta(hours=1),
        )
        r2_answered.status = "answered"
        r2_answered.answered_at = datetime.now(timezone.utc)
        session.add(r2_answered)
        session.commit()

        nxt = next_open_question_for_user(session, user.id)
        assert nxt is not None
        assert nxt.id == r1_open.id, (
            "newest question is answered; lookup must skip it and "
            "land on the older receipt's open question"
        )


def test_returns_none_when_no_open_questions() -> None:
    create_db_and_tables()
    with Session(engine) as session:
        user = _seed_user(session, telegram_user_id=900005)
        assert next_open_question_for_user(session, user.id) is None


def test_telegram_amount_answer_lands_on_amount_not_older_receipts_supplier() -> None:
    """End-to-end F1.5 incident regression.

    Recreate the exact prod symptom: user has a stale ``supplier``
    question on an older receipt R1 and a fresh ``local_amount``
    question on a newer receipt R2. They reply ``"680.00 TRY"`` to
    the bot, intending it as the amount for R2. Pre-F1.5 the bot
    would route the answer to R1.supplier (writing "680.00 TRY"
    into ``extracted_supplier`` on R1, leaving R2.amount NULL).
    Post-F1.5 the answer lands on R2.amount, parsed as a Decimal,
    and R1.supplier remains untouched.
    """
    get_settings.cache_clear()
    create_db_and_tables()

    fake_client = _FakeTelegramClient("test-token")
    original_client = telegram_service.TelegramClient
    telegram_service.TelegramClient = lambda token: fake_client
    try:
        with Session(engine) as session:
            user = _seed_user(session, telegram_user_id=900006)

            r1 = ReceiptDocument(
                uploader_user_id=user.id,
                original_file_name="r1_older.jpg",
                extracted_date=date(2026, 4, 1),
                extracted_local_amount=Decimal("100.0"),
                extracted_currency="TRY",
                extracted_supplier=None,  # stale supplier question still open
                business_or_personal="Business",
                business_reason="prior trip",
                attendees="self",
                status="needs_extraction_review",
            )
            session.add(r1)
            session.commit()
            session.refresh(r1)

            r2 = ReceiptDocument(
                uploader_user_id=user.id,
                original_file_name="r2_newer.jpg",
                extracted_date=date(2026, 4, 27),
                extracted_local_amount=None,  # the question we want answered
                extracted_currency=None,
                extracted_supplier="Ziraat / Fermaki Meat",
                business_or_personal="Business",
                business_reason="customer dinner",
                attendees="self",
                status="needs_extraction_review",
            )
            session.add(r2)
            session.commit()
            session.refresh(r2)

            t0 = datetime(2026, 4, 20, 10, 0, 0, tzinfo=timezone.utc)
            _seed_question(
                session, r1, user,
                key="supplier",
                text="I could not read the merchant name. Which store, restaurant, or vendor is this?",
                created_at=t0,
            )
            _seed_question(
                session, r2, user,
                key="local_amount",
                text="I could not read the receipt amount. What is the total amount and currency?",
                created_at=t0 + timedelta(hours=6),
            )

        payload = {
            "message": {
                "message_id": 99,
                "from": {"id": 900006, "first_name": "Op"},
                "chat": {"id": 555},
                "text": "680.00 TRY",
            }
        }
        with Session(engine) as session:
            result = telegram_service.handle_update(session, payload)
            assert result["ok"] is True
            assert result["action"] == "answered_clarification"

        with Session(engine) as session:
            r1_after = session.exec(
                select(ReceiptDocument).where(ReceiptDocument.original_file_name == "r1_older.jpg")
            ).first()
            r2_after = session.exec(
                select(ReceiptDocument).where(ReceiptDocument.original_file_name == "r2_newer.jpg")
            ).first()

            assert r1_after is not None and r2_after is not None
            # The bug: amount-shaped reply landed in supplier column on R1.
            assert r1_after.extracted_supplier is None, (
                "F1.5 regression: amount answer wrote to older receipt's supplier "
                f"(value={r1_after.extracted_supplier!r}); answer must route to "
                "the newer receipt's amount question instead."
            )
            # The fix: amount answer parses into R2's amount as Decimal.
            assert r2_after.extracted_local_amount == Decimal("680.0000"), (
                "F1.5 fix: amount answer must populate the newer receipt's "
                f"local_amount; got {r2_after.extracted_local_amount!r}"
            )
            assert r2_after.extracted_currency == "TRY"

    finally:
        telegram_service.TelegramClient = original_client
        get_settings.cache_clear()
