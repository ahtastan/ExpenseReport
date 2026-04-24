"""M1 Day 2 Phase 2 — pivot to expense_report_id as primary operational key.

Covers:
- Direct-with-expense_report_id path on get_or_create_review_session
- Backfill path that resolves a statement to an ExpenseReport
- report_generator dispatch by report_kind (diners vs personal)
- HTTP 501 at the /reports/generate endpoint for personal_reimbursement
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

VERIFY_ROOT = Path.cwd() / ".verify_data"
VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{VERIFY_ROOT / f'pivot_{uuid4().hex}.db'}"
os.environ["EXPENSE_STORAGE_ROOT"] = str(VERIFY_ROOT)
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    AppUser,
    ExpenseReport,
    ReviewSession,
    StatementImport,
)
from app.services.report_generator import generate_report_package  # noqa: E402
from app.services.review_sessions import (  # noqa: E402
    _resolve_statement_to_expense_report,
    get_or_create_review_session,
)


@pytest.fixture
def client(isolated_db):
    with TestClient(app) as c:
        yield c


def _seed_user(session: Session) -> int:
    user = AppUser(telegram_user_id=9001, display_name="Pivot Tester")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user.id


def _seed_diners_report(session: Session, user_id: int) -> tuple[int, int]:
    """Seed a statement + diners ExpenseReport wired together. Returns (expense_report_id, statement_id)."""
    statement = StatementImport(
        source_filename="pivot_test.xlsx",
        row_count=0,
        uploader_user_id=user_id,
    )
    session.add(statement)
    session.commit()
    session.refresh(statement)

    report = ExpenseReport(
        owner_user_id=user_id,
        report_kind="diners_statement",
        title="Pivot diners report",
        status="draft",
        report_currency="USD",
        statement_import_id=statement.id,
    )
    session.add(report)
    session.commit()
    session.refresh(report)
    return report.id, statement.id


def test_get_or_create_review_session_by_expense_report_id(isolated_db):
    with Session(isolated_db) as session:
        user_id = _seed_user(session)
        report_id, statement_id = _seed_diners_report(session, user_id)

        review = get_or_create_review_session(session, expense_report_id=report_id)

        assert review.expense_report_id == report_id
        assert review.statement_import_id == statement_id, (
            "diners-kind review session must mirror the report's statement_import_id"
        )
        assert review.status == "draft"

        # Calling again returns the same row (not a second draft).
        again = get_or_create_review_session(session, expense_report_id=report_id)
        assert again.id == review.id


def test_get_or_create_review_session_backfills_from_diners_statement(isolated_db):
    """Legacy statement-keyed caller resolves to (or creates) an ExpenseReport."""
    with Session(isolated_db) as session:
        user_id = _seed_user(session)
        statement = StatementImport(
            source_filename="pivot_backfill.xlsx",
            row_count=0,
            uploader_user_id=user_id,
        )
        session.add(statement)
        session.commit()
        session.refresh(statement)
        statement_id = statement.id

        # No ExpenseReport yet.
        assert session.exec(
            select(ExpenseReport).where(ExpenseReport.statement_import_id == statement_id)
        ).first() is None

        # Backfill creates one.
        expense_report_id = _resolve_statement_to_expense_report(
            session, statement_id, owner_user_id=user_id
        )
        assert expense_report_id > 0

        created = session.get(ExpenseReport, expense_report_id)
        assert created.report_kind == "diners_statement"
        assert created.statement_import_id == statement_id
        assert created.owner_user_id == user_id

        # Subsequent backfill call reuses the existing report.
        second = _resolve_statement_to_expense_report(
            session, statement_id, owner_user_id=user_id
        )
        assert second == expense_report_id

        # And the review-session path works off the resolved id.
        review = get_or_create_review_session(session, expense_report_id=expense_report_id)
        assert review.expense_report_id == expense_report_id
        assert review.statement_import_id == statement_id


def test_report_generator_dispatches_by_kind(isolated_db):
    """personal_reimbursement must raise NotImplementedError;
    diners_statement follows the existing validation/generation path."""
    with Session(isolated_db) as session:
        user_id = _seed_user(session)

        personal = ExpenseReport(
            owner_user_id=user_id,
            report_kind="personal_reimbursement",
            title="Personal pivot",
            status="draft",
            report_currency="USD",
        )
        session.add(personal)
        session.commit()
        session.refresh(personal)

        with pytest.raises(NotImplementedError) as exc:
            generate_report_package(
                session,
                expense_report_id=personal.id,
                employee_name="Tester",
                title_prefix="Irrelevant",
                allow_warnings=True,
            )
        assert "Personal reimbursement" in str(exc.value)
        assert "M1 Day 8-9" in str(exc.value)

        # Diners kind: validation currently fails (no statement transactions,
        # no confirmed review). This is NOT NotImplementedError — it's a
        # regular ValueError from the validator, meaning the dispatcher
        # routed into the diners branch as expected.
        diners_report_id, _ = _seed_diners_report(session, user_id)
        with pytest.raises((ValueError, FileNotFoundError)) as diners_exc:
            generate_report_package(
                session,
                expense_report_id=diners_report_id,
                employee_name="Tester",
                title_prefix="Diners",
                allow_warnings=True,
            )
        # Explicitly assert it's NOT NotImplementedError — dispatch hit diners.
        assert not isinstance(diners_exc.value, NotImplementedError)


def test_reports_generate_returns_501_for_personal_reimbursement(
    client, isolated_db, monkeypatch
):
    """POST /reports/generate must map NotImplementedError → HTTP 501.

    The existing /reports/generate URL takes a statement_import_id and the
    route's resolver always creates (or finds) a diners_statement-kind
    ExpenseReport. So a direct end-to-end personal_reimbursement call is
    not possible through THIS route surface today — that wiring arrives
    with the personal-reimbursement template in M1 Day 8-9. For Phase 2
    we pin the exception-mapping itself: if the generator raises
    NotImplementedError, the route returns 501 with the error message.
    """
    with Session(isolated_db) as session:
        user_id = _seed_user(session)
        statement = StatementImport(
            source_filename="pivot_501.xlsx",
            row_count=0,
            uploader_user_id=user_id,
        )
        session.add(statement)
        session.commit()
        session.refresh(statement)
        statement_id = statement.id

    # Monkeypatch the generator used by the route to raise the same
    # NotImplementedError the real personal_reimbursement branch raises.
    from app.routes import reports as reports_route_module

    def _fake_generator(*args, **kwargs):
        raise NotImplementedError(
            "Personal reimbursement report template coming in M1 Day 8-9"
        )

    monkeypatch.setattr(
        reports_route_module, "generate_report_package", _fake_generator
    )

    response = client.post(
        "/reports/generate",
        json={
            "statement_import_id": statement_id,
            "employee_name": "Tester",
            "title_prefix": "Irrelevant",
            "allow_warnings": True,
        },
    )
    assert response.status_code == 501, response.text
    assert "M1 Day 8-9" in response.json()["detail"]
