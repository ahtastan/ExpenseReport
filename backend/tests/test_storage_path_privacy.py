from __future__ import annotations

from datetime import date
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlmodel import Session

from app.config import get_settings
from app.main import app
from app.models import (
    AppUser,
    MatchDecision,
    ReceiptDocument,
    ReportRun,
    StatementImport,
    StatementTransaction,
)
from app.schemas import ReportRunRead


@pytest.fixture
def client(isolated_db):
    with TestClient(app) as test_client:
        yield test_client


def _receipt_upload() -> tuple[str, tuple[str, bytes, str]]:
    return (
        "file",
        ("demo_receipt.txt", b"demo receipt bytes", "text/plain"),
    )


def _statement_upload() -> tuple[str, tuple[str, bytes, str]]:
    wb = Workbook()
    ws = wb.active
    ws.append(["Tran Date", "Supplier", "Source Amount", "Amount Incl"])
    ws.append(["04/01/2026", "Demo Supplier", "100.00 TRY", 2.50])
    buffer = BytesIO()
    wb.save(buffer)
    wb.close()
    buffer.seek(0)
    return (
        "file",
        (
            "demo_statement.xlsx",
            buffer.read(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
    )


FORBIDDEN_PATH_KEYS = {
    "storage_path",
    "receipt_path",
    "output_workbook_path",
    "output_pdf_path",
    "storage_root",
}


def _assert_no_public_path_leaks(payload, forbidden_values=()):
    if isinstance(payload, list):
        assert payload, "expected at least one response item"
        for item in payload:
            _assert_no_public_path_leaks(item, forbidden_values)
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            assert key not in FORBIDDEN_PATH_KEYS
            _assert_no_public_path_leaks(value, forbidden_values)
        return
    if isinstance(payload, str):
        for forbidden_value in forbidden_values:
            assert forbidden_value not in payload


def _seed_matched_review_payload(engine, raw_receipt_path: str) -> int:
    with Session(engine) as session:
        user = AppUser(telegram_user_id=88001, display_name="Privacy Tester")
        session.add(user)
        session.flush()

        statement = StatementImport(
            source_filename="privacy_statement.xlsx",
            row_count=1,
            uploader_user_id=user.id,
        )
        session.add(statement)
        session.flush()

        tx = StatementTransaction(
            statement_import_id=statement.id,
            transaction_date=date(2026, 4, 1),
            supplier_raw="Privacy Cafe",
            supplier_normalized="PRIVACY CAFE",
            local_currency="USD",
            local_amount=12.34,
            usd_amount=12.34,
        )
        receipt = ReceiptDocument(
            uploader_user_id=user.id,
            source="test",
            status="imported",
            content_type="photo",
            original_file_name="privacy_receipt.jpg",
            mime_type="image/jpeg",
            storage_path=raw_receipt_path,
            extracted_date=date(2026, 4, 1),
            extracted_supplier="Privacy Cafe",
            extracted_local_amount=12.34,
            extracted_currency="USD",
            business_or_personal="Business",
            report_bucket="Meals/Snacks",
            business_reason="Demo meal",
            attendees="Demo Team",
            needs_clarification=False,
        )
        session.add(tx)
        session.add(receipt)
        session.flush()

        decision = MatchDecision(
            statement_transaction_id=tx.id,
            receipt_document_id=receipt.id,
            confidence="high",
            match_method="test_privacy",
            approved=True,
            reason="privacy regression fixture",
        )
        session.add(decision)
        session.commit()
        session.refresh(statement)
        return statement.id


def test_receipt_read_does_not_expose_storage_path(client):
    uploaded = client.post("/receipts/upload", files=[_receipt_upload()])
    assert uploaded.status_code == 200, uploaded.text
    uploaded_body = uploaded.json()
    receipt_id = uploaded_body["id"]
    _assert_no_public_path_leaks(uploaded_body)

    listed = client.get("/receipts")
    assert listed.status_code == 200, listed.text
    _assert_no_public_path_leaks(listed.json())

    fetched = client.get(f"/receipts/{receipt_id}")
    assert fetched.status_code == 200, fetched.text
    _assert_no_public_path_leaks(fetched.json())

    patched = client.patch(
        f"/receipts/{receipt_id}",
        json={"business_or_personal": "Business"},
    )
    assert patched.status_code == 200, patched.text
    _assert_no_public_path_leaks(patched.json())


def test_statement_read_does_not_expose_storage_path(client):
    imported = client.post("/statements/import-excel", files=[_statement_upload()])
    assert imported.status_code == 200, imported.text
    _assert_no_public_path_leaks(imported.json())

    listed = client.get("/statements")
    assert listed.status_code == 200, listed.text
    _assert_no_public_path_leaks(listed.json())

    latest = client.get("/statements/latest")
    assert latest.status_code == 200, latest.text
    _assert_no_public_path_leaks(latest.json())


def test_review_session_payload_does_not_expose_receipt_paths(client, isolated_db, tmp_path):
    raw_receipt_path = str(tmp_path / "private-storage" / "receipt.jpg")
    statement_id = _seed_matched_review_payload(isolated_db, raw_receipt_path)

    response = client.get(f"/reviews/report/{statement_id}")

    assert response.status_code == 200, response.text
    _assert_no_public_path_leaks(response.json(), forbidden_values=(raw_receipt_path,))


def test_health_does_not_expose_storage_root(client):
    response = client.get("/health")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert "telegram_configured" in body
    _assert_no_public_path_leaks(body)


def test_report_list_does_not_expose_output_paths(client, isolated_db, tmp_path):
    workbook_path = str(tmp_path / "private-storage" / "report.xlsx")
    pdf_path = str(tmp_path / "private-storage" / "receipts.pdf")
    with Session(isolated_db) as session:
        run = ReportRun(
            statement_import_id=None,
            template_name="privacy_template",
            status="completed",
            output_workbook_path=workbook_path,
            output_pdf_path=pdf_path,
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        schema_payload = ReportRunRead.model_validate(run).model_dump(mode="json")

        _assert_no_public_path_leaks(schema_payload, forbidden_values=(workbook_path, pdf_path))

    response = client.get("/reports/")

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 1
    _assert_no_public_path_leaks(body, forbidden_values=(workbook_path, pdf_path))


def test_report_download_endpoint_still_serves_file(client, isolated_db, tmp_path, monkeypatch):
    storage_root = tmp_path / "storage-root"
    package_path = storage_root / "reports" / "report_package.zip"
    package_path.parent.mkdir(parents=True)
    package_path.write_bytes(b"report package bytes")
    monkeypatch.setenv("EXPENSE_STORAGE_ROOT", str(storage_root))
    get_settings.cache_clear()

    with Session(isolated_db) as session:
        run = ReportRun(
            statement_import_id=None,
            template_name="privacy_template",
            status="completed",
            output_workbook_path=str(package_path),
            output_pdf_path=str(storage_root / "reports" / "receipts.pdf"),
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        report_run_id = run.id

    response = client.get(f"/reports/{report_run_id}/download")

    assert response.status_code == 200, response.text
    assert response.content == b"report package bytes"


def test_receipt_file_endpoint_still_serves_file(client):
    uploaded = client.post("/receipts/upload", files=[_receipt_upload()])
    assert uploaded.status_code == 200, uploaded.text
    receipt_id = uploaded.json()["id"]

    file_response = client.get(f"/receipts/{receipt_id}/file")

    assert file_response.status_code == 200, file_response.text
    assert file_response.content == b"demo receipt bytes"
    assert file_response.headers["content-type"].startswith("text/plain")
