"""Tests for LLM-backed report package synthesis."""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

VERIFY_ROOT = Path.cwd() / ".verify_data"
VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{VERIFY_ROOT / f'report_synthesis_{uuid4().hex}.db'}"
os.environ["EXPENSE_STORAGE_ROOT"] = str(VERIFY_ROOT)
os.environ["EXPENSE_REPORT_TEMPLATE_PATH"] = str(Path.cwd().parent / "Expense Report Form_Blank.xlsx")
os.environ.pop("OPENAI_API_KEY", None)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlmodel import Session  # noqa: E402

from app.db import create_db_and_tables, engine  # noqa: E402
from app.models import StatementImport, StatementTransaction  # noqa: E402
from app.services import model_router  # noqa: E402
from app.services.report_generator import generate_report_package  # noqa: E402
from app.services.review_sessions import (  # noqa: E402
    confirm_review_session,
    get_or_create_review_session,
    review_rows,
    update_review_row,
)


def main() -> None:
    create_db_and_tables()
    with Session(engine) as session:
        statement = StatementImport(source_filename="synthesis_statement.xlsx", row_count=1)
        session.add(statement)
        session.commit()
        session.refresh(statement)

        tx = StatementTransaction(
            statement_import_id=statement.id,
            transaction_date=date(2026, 4, 1),
            supplier_raw="Istanbul Airport",
            supplier_normalized="istanbul airport",
            local_amount=Decimal("500.0"),
            local_currency="TRY",
            source_row_ref="synthesis-1",
        )
        session.add(tx)
        session.commit()

        review = get_or_create_review_session(session, statement.id)
        row = review_rows(session, review.id)[0]
        update_review_row(
            session,
            row.id,
            fields={
                "business_or_personal": "Business",
                "report_bucket": "Airfare/Bus/Ferry/Other",
                "business_reason": "Sanipak Visit",
                "air_travel_total_tkt_cost": 500.0,
            },
        )
        confirm_review_session(session, review.id, confirmed_by_label="synthesis-test")

        calls: list[tuple[str, str, str]] = []

        def fake_text_call(model, prompt, payload):
            calls.append((model, prompt, payload))
            return {
                "summary_md": (
                    "# Expense Report Summary\n\n"
                    "Trip purpose: Sanipak Visit.\n\n"
                    "Totals by bucket: Airfare/Bus/Ferry/Other = 500.00 TRY.\n\n"
                    "Flagged anomalies: none."
                )
            }

        original = model_router._text_call
        model_router._text_call = fake_text_call
        try:
            run = generate_report_package(session, statement.id, "Tester", "Synthesis Report", True)
        finally:
            model_router._text_call = original

        assert run.output_workbook_path
        assert len(calls) == 1
        assert calls[0][0] == model_router.SYNTHESIS_MODEL
        assert "totals_by_bucket" in calls[0][2]
        with ZipFile(run.output_workbook_path) as zf:
            assert "summary.md" in zf.namelist()
            summary = zf.read("summary.md").decode("utf-8")
        assert "Trip purpose: Sanipak Visit." in summary
        assert "Airfare/Bus/Ferry/Other" in summary

    print("report_synthesis_tests=passed")


if __name__ == "__main__":
    main()
