import asyncio
import os
from datetime import date
from pathlib import Path
from uuid import uuid4

VERIFY_ROOT = Path.cwd() / ".verify_data"
VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{VERIFY_ROOT / f'review_confirmation_{uuid4().hex}.db'}"
os.environ["EXPENSE_STORAGE_ROOT"] = str(VERIFY_ROOT)
os.environ["EXPENSE_REPORT_TEMPLATE_PATH"] = str(Path.cwd().parent / "Expense Report Form_Blank.xlsx")
os.environ.pop("ANTHROPIC_API_KEY", None)

from sqlmodel import Session  # noqa: E402

from app.db import create_db_and_tables, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import MatchDecision, ReceiptDocument, ReviewSession, StatementImport, StatementTransaction  # noqa: E402
from app.services.report_generator import ReportLine, _allocate, _confirmed_lines, generate_report_package  # noqa: E402
from app.services.review_sessions import (  # noqa: E402
    bulk_update_review_rows,
    confirm_review_session,
    get_or_create_review_session,
    review_rows,
    session_payload,
    update_review_row,
)


async def asgi_get(path: str) -> tuple[int, dict[str, str], str]:
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"host", b"testserver")],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        },
        receive,
        send,
    )
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    headers = {key.decode().lower(): value.decode() for key, value in start["headers"]}
    return start["status"], headers, body.decode()


async def asgi_get_bytes(path: str) -> tuple[int, dict[str, str], bytes]:
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"host", b"testserver")],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        },
        receive,
        send,
    )
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    headers = {key.decode().lower(): value.decode() for key, value in start["headers"]}
    return start["status"], headers, body


def asgi_get_json(path: str) -> dict:
    status, _headers, body = asyncio.run(asgi_get(path))
    assert status == 200
    import json

    return json.loads(body)


def seed(session: Session) -> int:
    statement = StatementImport(source_filename="statement.xlsx", row_count=1)
    session.add(statement)
    session.commit()
    session.refresh(statement)

    tx = StatementTransaction(
        statement_import_id=statement.id,
        transaction_date=date(2026, 3, 11),
        supplier_raw="Migros",
        supplier_normalized="MIGROS",
        local_currency="TRY",
        local_amount=419.58,
    )
    receipt = ReceiptDocument(
        source="test",
        status="imported",
        content_type="photo",
        original_file_name="migros.jpg",
        extracted_date=date(2026, 3, 11),
        extracted_supplier="Migros",
        extracted_local_amount=419.58,
        extracted_currency="TRY",
        business_or_personal="Personal",
        report_bucket="Personal",
        needs_clarification=False,
    )
    session.add(tx)
    session.add(receipt)
    session.commit()
    session.refresh(tx)
    session.refresh(receipt)

    decision = MatchDecision(
        statement_transaction_id=tx.id,
        receipt_document_id=receipt.id,
        confidence="high",
        match_method="test",
        approved=True,
        reason="test approved match",
    )
    session.add(decision)
    session.commit()
    return statement.id


def seed_statement_only(session: Session) -> int:
    statement = StatementImport(source_filename="statement_only.xlsx", row_count=2)
    session.add(statement)
    session.commit()
    session.refresh(statement)

    for supplier, amount in [("Migros", 419.58), ("Uber Trip", 120.00)]:
        tx = StatementTransaction(
            statement_import_id=statement.id,
            transaction_date=date(2026, 3, 11),
            supplier_raw=supplier,
            supplier_normalized=supplier.upper(),
            local_currency="TRY",
            local_amount=amount,
        )
        session.add(tx)
    session.commit()
    return statement.id


def seed_mixed_statement(session: Session) -> int:
    statement = StatementImport(source_filename="mixed_statement.xlsx", row_count=2)
    session.add(statement)
    session.commit()
    session.refresh(statement)

    matched_tx = StatementTransaction(
        statement_import_id=statement.id,
        transaction_date=date(2026, 3, 11),
        supplier_raw="Migros",
        supplier_normalized="MIGROS",
        local_currency="TRY",
        local_amount=419.58,
    )
    unmatched_tx = StatementTransaction(
        statement_import_id=statement.id,
        transaction_date=date(2026, 3, 12),
        supplier_raw="Uber Trip",
        supplier_normalized="UBER TRIP",
        local_currency="TRY",
        local_amount=120.00,
    )
    receipt = ReceiptDocument(
        source="test",
        status="imported",
        content_type="photo",
        original_file_name="migros.jpg",
        extracted_date=date(2026, 3, 11),
        extracted_supplier="Migros",
        extracted_local_amount=419.58,
        extracted_currency="TRY",
        business_or_personal="Personal",
        report_bucket="Personal",
        needs_clarification=False,
    )
    session.add(matched_tx)
    session.add(unmatched_tx)
    session.add(receipt)
    session.commit()
    session.refresh(matched_tx)
    session.refresh(unmatched_tx)
    session.refresh(receipt)

    decision = MatchDecision(
        statement_transaction_id=matched_tx.id,
        receipt_document_id=receipt.id,
        confidence="high",
        match_method="test",
        approved=True,
        reason="test approved match",
    )
    session.add(decision)
    session.commit()
    return statement.id


def test_report_bucket_allocation_uses_template_categories() -> None:
    from collections import defaultdict

    day_totals = defaultdict(lambda: defaultdict(list))
    detail_lines = defaultdict(list)
    base = dict(
        transaction_id=1,
        receipt_id=None,
        receipt_path=None,
        receipt_file_name="missing_receipt",
        transaction_date=date(2026, 4, 1),
        supplier="Test Supplier",
        currency="USD",
        business_or_personal="Business",
        business_reason="Test reason",
        attendees="",
    )

    _allocate(ReportLine(**base, amount=10.0, report_bucket="Business"), day_totals, detail_lines)
    _allocate(ReportLine(**base, amount=20.0, report_bucket="Taxi/Parking/Tolls/Uber"), day_totals, detail_lines)
    _allocate(ReportLine(**base, amount=30.0, report_bucket="Hotel/Lodging/Laundry"), day_totals, detail_lines)

    totals = day_totals[date(2026, 4, 1)]
    assert totals["other"] == [10.0]
    assert totals["ground"] == [20.0]
    assert totals["hotel"] == [30.0]
    assert totals["airfare"] == []


def main() -> None:
    create_db_and_tables()
    test_report_bucket_allocation_uses_template_categories()
    status, headers, body = asyncio.run(asgi_get("/review"))
    assert status == 200
    assert "text/html" in headers["content-type"]
    assert "Review Queue" in body
    assert "ExpenseReport" in body
    assert "/statements/latest" in body

    with Session(engine) as session:
        older = StatementImport(source_filename="older_statement.xlsx", row_count=0)
        session.add(older)
        session.commit()
        statement_id = seed(session)
        latest = asgi_get_json("/statements/latest")
        assert latest["id"] == statement_id
        assert latest["source_filename"] == "statement.xlsx"

        try:
            generate_report_package(session, statement_id, "Tester", "Test Report", True)
            raise AssertionError("Report generation should require a confirmed review snapshot")
        except ValueError as exc:
            assert "confirmed review data" in str(exc)

        review = get_or_create_review_session(session, statement_id)
        payload = session_payload(session, review)
        assert payload["status"] == "draft"
        assert len(payload["rows"]) == 1
        row_payload = payload["rows"][0]
        assert "statement" in row_payload["source"]
        assert "receipt" in row_payload["source"]
        assert "match" in row_payload["source"]
        assert row_payload["suggested"]["amount"] == 419.58
        assert row_payload["confirmed"]["business_or_personal"] == "Personal"

        review = confirm_review_session(session, review.id, confirmed_by_label="test")
        rows = review_rows(session, review.id)
        assert review.status == "confirmed"
        assert rows[0].status == "confirmed"

        tx = session.get(StatementTransaction, rows[0].statement_transaction_id)
        receipt = session.get(ReceiptDocument, rows[0].receipt_document_id)
        tx.local_amount = 999.99
        receipt.report_bucket = "Changed Live Bucket"
        session.add(tx)
        session.add(receipt)
        session.commit()

        confirmed_line = _confirmed_lines(session, statement_id)[0]
        assert confirmed_line.amount == 419.58
        assert confirmed_line.report_bucket == "Personal"

        run = generate_report_package(session, statement_id, "Tester", "Test Report", True)
        assert run.status == "completed"
        assert run.output_workbook_path

        update_review_row(session, rows[0].id, fields={"amount": 420.00})
        try:
            generate_report_package(session, statement_id, "Tester", "Test Report", True)
            raise AssertionError("Report generation should require reconfirmation after edit")
        except ValueError as exc:
            assert "confirmed review data" in str(exc)

        review = confirm_review_session(session, review.id, confirmed_by_label="test")
        assert review.status == "confirmed"
        assert _confirmed_lines(session, statement_id)[0].amount == 420.00

        statement_only_id = seed_statement_only(session)
        statement_only_review = get_or_create_review_session(session, statement_only_id)
        statement_only_payload = session_payload(session, statement_only_review)
        assert len(statement_only_payload["rows"]) == 2
        assert all(row["status"] == "needs_review" for row in statement_only_payload["rows"])
        assert all(row["attention_required"] for row in statement_only_payload["rows"])
        assert all(row["source"]["match"]["status"] == "unmatched" for row in statement_only_payload["rows"])
        assert all(row["confirmed"]["receipt_id"] is None for row in statement_only_payload["rows"])

        first_statement_only_row = review_rows(session, statement_only_review.id)[0]
        updated_first_row = update_review_row(
            session,
            first_statement_only_row.id,
            fields={"business_or_personal": "Business", "report_bucket": "Business"},
            attention_required=True,
            attention_note="Marked for attention by reviewer",
        )
        assert updated_first_row.attention_required is False
        assert updated_first_row.attention_note in (None, "")
        assert updated_first_row.status == "edited"

        existing_empty_statement_id = seed_statement_only(session)
        existing_empty_review = ReviewSession(statement_import_id=existing_empty_statement_id, status="draft")
        session.add(existing_empty_review)
        session.commit()
        session.refresh(existing_empty_review)
        rebuilt_existing_review = get_or_create_review_session(session, existing_empty_statement_id)
        rebuilt_existing_payload = session_payload(session, rebuilt_existing_review)
        assert rebuilt_existing_review.id == existing_empty_review.id
        assert len(rebuilt_existing_payload["rows"]) == 2

        mixed_statement_id = seed_mixed_statement(session)
        mixed_review = get_or_create_review_session(session, mixed_statement_id)
        mixed_payload = session_payload(session, mixed_review)
        assert len(mixed_payload["rows"]) == 2
        matched_rows = [row for row in mixed_payload["rows"] if row["confirmed"]["receipt_id"] is not None]
        unmatched_rows = [row for row in mixed_payload["rows"] if row["confirmed"]["receipt_id"] is None]
        assert len(matched_rows) == 1
        assert len(unmatched_rows) == 1
        assert matched_rows[0]["source"]["match"]["approved"] is True
        assert matched_rows[0]["confirmed"]["business_or_personal"] == "Personal"
        assert unmatched_rows[0]["source"]["match"]["status"] == "unmatched"
        assert unmatched_rows[0]["status"] == "needs_review"

        try:
            generate_report_package(session, mixed_statement_id, "Tester", "Test Report", True)
            raise AssertionError("Report generation should still require confirmation for statement-led review rows")
        except ValueError as exc:
            assert "confirmed review data" in str(exc)

        bulk_result = bulk_update_review_rows(
            session,
            review_session_id=mixed_review.id,
            fields={"business_or_personal": "Business", "report_bucket": "Business"},
            scope="attention_required",
        )
        assert bulk_result["updated_rows"] == 1
        mixed_rows_after_bulk = review_rows(session, mixed_review.id)
        assert sum(1 for row in mixed_rows_after_bulk if row.attention_required) == 0

        for row in review_rows(session, statement_only_review.id):
            update_review_row(
                session,
                row.id,
                fields={"business_or_personal": "Personal", "report_bucket": "Personal"},
                attention_required=False,
                attention_note="",
            )
        statement_only_review = confirm_review_session(session, statement_only_review.id, confirmed_by_label="test")
        assert statement_only_review.status == "confirmed"
        run = generate_report_package(session, statement_only_id, "Tester", "Statement Only Report", True)
        assert run.status == "completed"
        assert run.output_workbook_path
        download_status, download_headers, download_body = asyncio.run(asgi_get_bytes(f"/reports/{run.id}/download"))
        assert download_status == 200
        assert "attachment" in download_headers["content-disposition"]
        assert download_body.startswith(b"PK")

    print("review_confirmation_tests=passed")


if __name__ == "__main__":
    main()
