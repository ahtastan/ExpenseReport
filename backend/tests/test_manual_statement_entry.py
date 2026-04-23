import asyncio
import os
from datetime import date
from io import BytesIO
from pathlib import Path
from uuid import uuid4

VERIFY_ROOT = Path.cwd() / ".verify_data"
VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{VERIFY_ROOT / f'manual_statement_{uuid4().hex}.db'}"
os.environ["EXPENSE_STORAGE_ROOT"] = str(VERIFY_ROOT)
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

from fastapi import UploadFile  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.db import create_db_and_tables, engine  # noqa: E402
from app.models import MatchDecision, ReviewRow, StatementImport, StatementTransaction  # noqa: E402
from app.routes.statements import create_manual_statement_transaction, upload_manual_statement_receipt  # noqa: E402
from app.schemas import ManualStatementCreate  # noqa: E402
from app.services.review_sessions import confirm_review_session, get_or_create_review_session, session_payload, update_review_row  # noqa: E402


def upload_file(name: str) -> UploadFile:
    file = UploadFile(file=BytesIO(b"not-real-image-bytes"), filename=name)
    file.headers = {"content-type": "image/jpeg"}
    return file


def main() -> None:
    create_db_and_tables()
    with Session(engine) as session:
        statement = StatementImport(source_filename="manual-base", row_count=0)
        session.add(statement)
        session.commit()
        session.refresh(statement)

        draft = asyncio.run(
            upload_manual_statement_receipt(
                file=upload_file("merchant=Migros_total_419.58TRY_2026-03-11.jpg"),
                session=session,
            )
        )
        assert draft.receipt_id is not None
        assert draft.extracted_date.isoformat() == "2026-03-11"
        assert draft.extracted_supplier == "Migros"
        assert draft.extracted_local_amount == 419.58
        assert draft.extracted_currency == "TRY"

        created = create_manual_statement_transaction(
            ManualStatementCreate(
                statement_import_id=statement.id,
                receipt_id=draft.receipt_id,
                transaction_date=draft.extracted_date,
                supplier=draft.extracted_supplier,
                amount=draft.extracted_local_amount,
                currency=draft.extracted_currency,
                business_reason="Customer visit supplies",
            ),
            session=session,
        )
        assert created.transaction.source_kind == "manual"
        assert created.transaction.supplier_raw == "Migros"
        assert created.transaction.local_amount == 419.58
        assert created.transaction.local_currency == "TRY"
        assert created.review_session.status == "draft"

        session.refresh(statement)
        assert statement.row_count == 1

        match = session.exec(select(MatchDecision)).one()
        assert match.approved is True
        assert match.receipt_document_id == draft.receipt_id
        assert match.statement_transaction_id == created.transaction.id
        assert match.match_method == "manual_statement_entry"

        review = get_or_create_review_session(session, statement.id)
        payload = session_payload(session, review)
        rows = payload["rows"]
        assert len(rows) == 1
        assert rows[0]["confirmed"]["receipt_id"] == draft.receipt_id
        assert rows[0]["confirmed"]["transaction_date"] == "2026-03-11"
        assert rows[0]["confirmed"]["supplier"] == "Migros"
        assert rows[0]["source"]["match"]["status"] == "matched"

        row = session.exec(select(ReviewRow)).one()
        tx = session.get(StatementTransaction, row.statement_transaction_id)
        assert tx.id == created.transaction.id

        update_review_row(
            session,
            row.id,
            fields={"business_or_personal": "Business", "report_bucket": "Other"},
        )
        confirmed_review = confirm_review_session(session, review.id, confirmed_by_label="manual-test")

        second_created = create_manual_statement_transaction(
            ManualStatementCreate(
                statement_import_id=statement.id,
                transaction_date=date(2026, 3, 12),
                supplier="Uber Trip",
                amount=120.00,
                currency="TRY",
            ),
            session=session,
        )
        assert second_created.review_session.status == "draft"
        assert second_created.review_session.id != confirmed_review.id
        assert len(second_created.review_session.rows) == 2
        assert second_created.transaction.source_kind == "manual"
        assert second_created.transaction.id is not None

    print("manual_statement_entry_tests=passed")


if __name__ == "__main__":
    main()
