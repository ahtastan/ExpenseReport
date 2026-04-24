"""Tests for the M1 Day 1 schema migration.

The conftest fixture builds a fresh SQLite DB per test with the POST-migration
schema already in place (SQLModel.metadata.create_all). That is fine: the
migration is designed to be idempotent and its DDL phase is a sequence of
IF-NOT-EXISTS / column-guard no-ops on such a DB. What we really exercise
here is the BACKFILL phase: seed the pre-migration-shaped rows
(StatementImport, ReviewSession, ReportRun, ReceiptDocument, MatchDecision),
run migrate(), and assert the derived ExpenseReport + FK linkages.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlmodel import Session, select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import (  # noqa: E402
    AppUser,
    ExpenseReport,
    MatchDecision,
    ReceiptDocument,
    ReportRun,
    ReviewSession,
    StatementImport,
    StatementTransaction,
)
from migrations.m1_day1_expensereport_schema import migrate  # noqa: E402


def _db_path(engine) -> str:
    # The autouse fixture builds sqlite:///<path>; SQLAlchemy's url.database is the path.
    return engine.url.database


def _seed_app_user_with_id_one(session: Session) -> AppUser:
    """Every test needs appuser.id=1 so the orphan-owner guard doesn't fire."""
    user = AppUser(id=1, display_name="migration test owner")
    session.add(user)
    session.commit()
    session.refresh(user)
    assert user.id == 1
    return user


def _seed_statement(
    session: Session,
    uploader_user_id: int | None,
    source_filename: str,
    cardholder_name: str | None = "AHMET TASTAN",
    period_start: date | None = date(2025, 8, 1),
    period_end: date | None = date(2025, 8, 31),
) -> StatementImport:
    statement = StatementImport(
        uploader_user_id=uploader_user_id,
        source_filename=source_filename,
        storage_path=f"(memory)/{source_filename}",
        period_start=period_start,
        period_end=period_end,
        cardholder_name=cardholder_name,
        row_count=1,
    )
    session.add(statement)
    session.commit()
    session.refresh(statement)
    return statement


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_migration_is_idempotent(isolated_db):
    with Session(isolated_db) as session:
        _seed_app_user_with_id_one(session)
        _seed_statement(session, uploader_user_id=1, source_filename="one.xlsx")
        _seed_statement(session, uploader_user_id=1, source_filename="two.xlsx")

    db_path = _db_path(isolated_db)

    first = migrate(db_path)
    assert first.already_migrated is False
    assert first.statements_backfilled == 2

    second = migrate(db_path)
    assert second.already_migrated is True
    assert second.statements_backfilled == 0

    # No duplicate expensereport rows after the second run.
    with Session(isolated_db) as session:
        reports = session.exec(select(ExpenseReport)).all()
        assert len(reports) == 2
        statement_ids = {r.statement_import_id for r in reports}
        assert len(statement_ids) == 2  # one per statement


def test_migration_backfills_one_expensereport_per_statementimport(isolated_db):
    with Session(isolated_db) as session:
        _seed_app_user_with_id_one(session)
        _seed_statement(session, 1, "stmt1.xlsx", cardholder_name="A ONE")
        _seed_statement(session, 1, "stmt2.xlsx", cardholder_name="B TWO")
        _seed_statement(session, 1, "stmt3.xlsx", cardholder_name="C THREE")

    result = migrate(_db_path(isolated_db))
    assert result.statements_backfilled == 3

    with Session(isolated_db) as session:
        reports = session.exec(
            select(ExpenseReport).order_by(ExpenseReport.statement_import_id)
        ).all()
        assert len(reports) == 3
        for report in reports:
            assert report.report_kind == "diners_statement"
            assert report.status == "submitted"
            assert report.report_currency == "USD"
            assert report.owner_user_id == 1
            assert report.statement_import_id is not None
            # Title formula: cardholder + period, falling back to source_filename.
            assert "Diners statement" in report.title


def test_migration_preserves_existing_reviewsession_rows(isolated_db):
    with Session(isolated_db) as session:
        _seed_app_user_with_id_one(session)
        statement = _seed_statement(session, 1, "preserve.xlsx")
        review = ReviewSession(statement_import_id=statement.id, status="draft")
        report_run = ReportRun(statement_import_id=statement.id, status="draft")
        session.add(review)
        session.add(report_run)
        session.commit()
        session.refresh(review)
        session.refresh(report_run)
        original_review_id = review.id
        original_report_run_id = report_run.id
        original_statement_import_id = review.statement_import_id

    migrate(_db_path(isolated_db))

    with Session(isolated_db) as session:
        review_after = session.get(ReviewSession, original_review_id)
        report_run_after = session.get(ReportRun, original_report_run_id)
        assert review_after is not None
        assert report_run_after is not None
        # Original statement_import_id retained (we only ADDED expense_report_id).
        assert review_after.statement_import_id == original_statement_import_id
        assert report_run_after.statement_import_id == original_statement_import_id


def test_migration_links_reviewsession_to_expensereport(isolated_db):
    with Session(isolated_db) as session:
        _seed_app_user_with_id_one(session)
        statement = _seed_statement(session, 1, "link_review.xlsx")
        review = ReviewSession(statement_import_id=statement.id, status="draft")
        report_run = ReportRun(statement_import_id=statement.id, status="draft")
        session.add(review)
        session.add(report_run)
        session.commit()
        session.refresh(review)
        session.refresh(report_run)
        review_id = review.id
        report_run_id = report_run.id
        statement_id = statement.id

    result = migrate(_db_path(isolated_db))
    assert result.reviewsessions_linked == 1
    assert result.reportruns_linked == 1

    with Session(isolated_db) as session:
        report = session.exec(
            select(ExpenseReport).where(ExpenseReport.statement_import_id == statement_id)
        ).one()
        review_after = session.get(ReviewSession, review_id)
        report_run_after = session.get(ReportRun, report_run_id)
        assert review_after.expense_report_id == report.id
        assert report_run_after.expense_report_id == report.id


def test_migration_links_approved_receipts_to_expensereport(isolated_db):
    with Session(isolated_db) as session:
        _seed_app_user_with_id_one(session)
        statement = _seed_statement(session, 1, "link_receipts.xlsx")
        transaction = StatementTransaction(
            statement_import_id=statement.id,
            transaction_date=date(2025, 8, 14),
            supplier_raw="TEST HOTEL",
            supplier_normalized="test hotel",
            local_currency="TRY",
            local_amount=3500.0,
        )
        session.add(transaction)
        session.commit()
        session.refresh(transaction)

        approved_receipt = ReceiptDocument(
            source="telegram",
            content_type="document",
            original_file_name="approved.pdf",
            storage_path="(memory)/approved.pdf",
        )
        unmatched_receipt = ReceiptDocument(
            source="telegram",
            content_type="photo",
            original_file_name="unmatched.jpg",
            storage_path="(memory)/unmatched.jpg",
        )
        session.add(approved_receipt)
        session.add(unmatched_receipt)
        session.commit()
        session.refresh(approved_receipt)
        session.refresh(unmatched_receipt)

        decision = MatchDecision(
            statement_transaction_id=transaction.id,
            receipt_document_id=approved_receipt.id,
            confidence="high",
            match_method="exact",
            approved=True,
            rejected=False,
            reason="approved by operator",
        )
        session.add(decision)
        session.commit()

        approved_id = approved_receipt.id
        unmatched_id = unmatched_receipt.id
        statement_id = statement.id

    result = migrate(_db_path(isolated_db))
    assert result.receipts_linked == 1
    assert result.receipts_ambiguous == 0

    with Session(isolated_db) as session:
        report = session.exec(
            select(ExpenseReport).where(ExpenseReport.statement_import_id == statement_id)
        ).one()
        approved_after = session.get(ReceiptDocument, approved_id)
        unmatched_after = session.get(ReceiptDocument, unmatched_id)
        assert approved_after.expense_report_id == report.id
        assert unmatched_after.expense_report_id is None


def test_migration_refuses_protected_path(tmp_path):
    # Simulate production path without ever pointing at a real file.
    protected = "/var/lib/dcexpense/expense_app.db"
    with pytest.raises(SystemExit) as excinfo:
        migrate(protected)
    assert excinfo.value.code == 2


def test_migration_aborts_on_orphan_statement_without_user_one(isolated_db):
    """uploader_user_id NULL + no appuser.id=1 => hard abort, no mutation."""
    with Session(isolated_db) as session:
        # Deliberately: no appuser.id=1, and statement with NULL uploader.
        _seed_statement(session, uploader_user_id=None, source_filename="orphan.xlsx")

    db_path = _db_path(isolated_db)

    with pytest.raises(SystemExit) as excinfo:
        migrate(db_path)
    assert excinfo.value.code == 2

    # No expensereport rows were created.
    with Session(isolated_db) as session:
        reports = session.exec(select(ExpenseReport)).all()
        assert reports == []
