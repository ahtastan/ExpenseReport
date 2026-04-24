"""Test-only helpers for the M1 Day 2 Phase 2 pivot.

The service layer was flipped to key on ``expense_report_id`` instead of
``statement_import_id``. Tests whose fixtures pre-date that pivot need a
way to resolve a statement to an ExpenseReport without re-engineering
every fixture. These helpers live at the tests/ layer so production
code stays strict (no auto-user-creation fallback).
"""

from __future__ import annotations


def seed_app_user(session, *, display_name: str = "test-owner"):
    """Create and return an AppUser. Used where fixtures didn't seed one."""
    from app.models import AppUser

    user = AppUser(display_name=display_name)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def ensure_expense_report_for_statement(session, statement_import_id: int) -> int:
    """Resolve (or create) an ExpenseReport.id for a given statement.

    Reads the uploader from the StatementImport row. Raises if the
    statement has no uploader — callers must seed one first.
    """
    from app.models import StatementImport
    from app.services.review_sessions import _resolve_statement_to_expense_report

    statement = session.get(StatementImport, statement_import_id)
    if statement is None:
        raise ValueError(f"StatementImport {statement_import_id} not found")
    if statement.uploader_user_id is None:
        raise ValueError(
            f"StatementImport {statement_import_id} has no uploader_user_id; "
            "add uploader_user_id to the fixture"
        )
    return _resolve_statement_to_expense_report(
        session, statement_import_id, owner_user_id=statement.uploader_user_id
    )
