"""Smoke test for Meals & Entertainment detail wiring.

Verifies:
- meal detail fields are accepted through review rows and confirmed snapshots
- B-page detail cells are populated on the row for the meal type/date
- EG/MR selections write x markers while the template amount formula is preserved
"""

import os
from datetime import date
from pathlib import Path
from uuid import uuid4

VERIFY_ROOT = Path.cwd() / ".verify_data"
VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{VERIFY_ROOT / f'meals_entertainment_{uuid4().hex}.db'}"
os.environ["EXPENSE_STORAGE_ROOT"] = str(VERIFY_ROOT)
_default_template = Path.cwd().parent / "Expense Report Form_Blank.xlsx"
if not _default_template.exists():
    _default_template = Path.cwd().parent.parent / "Expense Report Form_Blank.xlsx"
os.environ["EXPENSE_REPORT_TEMPLATE_PATH"] = str(_default_template)
os.environ.pop("ANTHROPIC_API_KEY", None)

from openpyxl import load_workbook  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.db import create_db_and_tables, engine  # noqa: E402
from app.models import StatementImport, StatementTransaction  # noqa: E402
from app.services.report_generator import _confirmed_lines, _fill_workbook  # noqa: E402
from app.services.review_sessions import (  # noqa: E402
    confirm_review_session,
    get_or_create_review_session,
    review_rows,
    update_review_row,
)


def main() -> None:
    create_db_and_tables()
    with Session(engine) as session:
        imp = StatementImport(source_filename="meals_smoke.xlsx", storage_path="(memory)")
        session.add(imp)
        session.commit()
        session.refresh(imp)

        tx = StatementTransaction(
            statement_import_id=imp.id,
            transaction_date=date(2026, 3, 12),
            supplier_raw="DINNER HOUSE",
            supplier_normalized="dinner house",
            local_amount=86.25,
            local_currency="USD",
            usd_amount=86.25,
            source_row_ref="row-1",
        )
        tx2 = StatementTransaction(
            statement_import_id=imp.id,
            transaction_date=date(2026, 3, 12),
            supplier_raw="CAFE ADDON",
            supplier_normalized="cafe addon",
            local_amount=4.85,
            local_currency="USD",
            usd_amount=4.85,
            source_row_ref="row-2",
        )
        session.add(tx)
        session.add(tx2)
        session.commit()

        review = get_or_create_review_session(session, imp.id)
        rows = review_rows(session, review.id)
        row = next(r for r in rows if r.statement_transaction_id == tx.id)
        update_review_row(
            session,
            row_id=row.id,
            fields={
                "business_or_personal": "Business",
                "report_bucket": "Lunch",
                "business_reason": "Project planning",
                "attendees": "A. Tester, EDT",
                "meal_place": "Dinner House",
                "meal_location": "Istanbul",
                "meal_eg": True,
                "meal_mr": True,
            },
        )
        row2 = next(r for r in rows if r.statement_transaction_id == tx2.id)
        duplicate_fields = {
            "business_or_personal": "Business",
            "report_bucket": "Lunch",
            "business_reason": "Project planning",
            "attendees": "A. Tester, EDT",
            "meal_place": "Cafe Addon",
            "meal_location": "Istanbul",
            "meal_eg": False,
            "meal_mr": False,
        }
        try:
            update_review_row(session, row_id=row2.id, fields=duplicate_fields)
            raise AssertionError("Duplicate same-date same-meal rows should be rejected")
        except ValueError as exc:
            message = str(exc)
            assert "Only one Lunch expense is allowed on 2026-03-12" in message, message
            assert "Try Meals/Snacks, Breakfast, Dinner, or Entertainment" in message, message

        update_review_row(
            session,
            row_id=row2.id,
            fields={**duplicate_fields, "report_bucket": "Meals/Snacks"},
        )
        try:
            update_review_row(session, row_id=row.id, fields={**duplicate_fields, "report_bucket": "Meals/Snacks"})
            raise AssertionError("Duplicate Meals/Snacks rows should be rejected")
        except ValueError as exc:
            message = str(exc)
            assert "Only one Meals/Snacks expense is allowed on 2026-03-12" in message, message
            assert "Meals/Snacks" not in message.split("Try ", 1)[1], message
            assert "Try Breakfast, Lunch, Dinner, or Entertainment" in message, message

        update_review_row(
            session,
            row_id=row2.id,
            fields={**duplicate_fields, "report_bucket": "Meals/Snacks"},
        )
        confirm_review_session(session, review.id, confirmed_by_label="meals-smoke")

        lines = _confirmed_lines(session, imp.id)
        assert len(lines) == 2
        line = next(ln for ln in lines if ln.supplier == "DINNER HOUSE")
        assert line.meal_place == "Dinner House"
        assert line.meal_location == "Istanbul"
        assert line.meal_eg is True
        assert line.meal_mr is True

        output_path = VERIFY_ROOT / f"meals_smoke_{uuid4().hex}.xlsx"
        _fill_workbook(Path(os.environ["EXPENSE_REPORT_TEMPLATE_PATH"]), output_path, "Smoke Tester", "Meals Smoke", lines)

        wb = load_workbook(output_path, data_only=False)
        ws1a = wb["Week 1A"]
        ws1b = wb["Week 1B"]
        assert ws1a["E29"].value == 4.85, ws1a["E29"].value
        assert ws1a["E31"].value == 86.25, ws1a["E31"].value
        # First date + Lunch maps to row 10 on the B page.
        assert ws1b["C10"].value == "Dinner House", ws1b["C10"].value
        assert ws1b["D10"].value == "Istanbul", ws1b["D10"].value
        assert ws1b["E10"].value == "A. Tester, EDT", ws1b["E10"].value
        assert ws1b["F10"].value == "Project planning", ws1b["F10"].value
        assert ws1b["H10"].value == "x", ws1b["H10"].value
        assert ws1b["I10"].value == "x", ws1b["I10"].value
        assert isinstance(ws1b["J10"].value, str) and ws1b["J10"].value.startswith("="), ws1b["J10"].value
        print("MEALS ENTERTAINMENT SMOKE TEST PASSED")


if __name__ == "__main__":
    main()
