from __future__ import annotations

from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.main import app


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


def _assert_no_storage_path(payload):
    if isinstance(payload, list):
        assert payload, "expected at least one response item"
        for item in payload:
            assert "storage_path" not in item
        return
    assert "storage_path" not in payload


def test_receipt_read_does_not_expose_storage_path(client):
    uploaded = client.post("/receipts/upload", files=[_receipt_upload()])
    assert uploaded.status_code == 200, uploaded.text
    uploaded_body = uploaded.json()
    receipt_id = uploaded_body["id"]
    _assert_no_storage_path(uploaded_body)

    listed = client.get("/receipts")
    assert listed.status_code == 200, listed.text
    _assert_no_storage_path(listed.json())

    fetched = client.get(f"/receipts/{receipt_id}")
    assert fetched.status_code == 200, fetched.text
    _assert_no_storage_path(fetched.json())

    patched = client.patch(
        f"/receipts/{receipt_id}",
        json={"business_or_personal": "Business"},
    )
    assert patched.status_code == 200, patched.text
    _assert_no_storage_path(patched.json())


def test_statement_read_does_not_expose_storage_path(client):
    imported = client.post("/statements/import-excel", files=[_statement_upload()])
    assert imported.status_code == 200, imported.text
    _assert_no_storage_path(imported.json())

    listed = client.get("/statements")
    assert listed.status_code == 200, listed.text
    _assert_no_storage_path(listed.json())

    latest = client.get("/statements/latest")
    assert latest.status_code == 200, latest.text
    _assert_no_storage_path(latest.json())


def test_receipt_file_endpoint_still_serves_file(client):
    uploaded = client.post("/receipts/upload", files=[_receipt_upload()])
    assert uploaded.status_code == 200, uploaded.text
    receipt_id = uploaded.json()["id"]

    file_response = client.get(f"/receipts/{receipt_id}/file")

    assert file_response.status_code == 200, file_response.text
    assert file_response.content == b"demo receipt bytes"
    assert file_response.headers["content-type"].startswith("text/plain")
