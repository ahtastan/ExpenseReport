"""API-boundary precision test for the Decimal migration (M1 Day 2.5).

Asserts the API preserves Decimal precision end-to-end: a value inserted
into the DB column comes back as the exact decimal string in the JSON
response body (not a JSON number, which would lose precision through
float64 representation).
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db as app_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AppUser, ReceiptDocument, StatementTransaction  # noqa: E402


@pytest.fixture
def client(isolated_db):
    with TestClient(app) as test_client:
        yield test_client


def test_get_receipt_preserves_decimal_as_string(client):
    with Session(app_db.engine) as session:
        receipt = ReceiptDocument(extracted_local_amount=Decimal("123.4567"))
        session.add(receipt)
        session.commit()
        session.refresh(receipt)
        receipt_id = receipt.id

    response = client.get(f"/receipts/{receipt_id}")
    assert response.status_code == 200
    body = response.json()

    # Post-step-5 contract: amount comes back as an exact decimal string.
    assert body["extracted_local_amount"] == "123.4567", (
        f"expected exact string '123.4567', got {body['extracted_local_amount']!r} "
        f"(type {type(body['extracted_local_amount']).__name__})"
    )


def _post_manual_transaction(client, amount_value):
    """POST to the manual statement endpoint with the given amount shape."""
    return client.post(
        "/statements/manual/transactions",
        json={
            "transaction_date": "2026-04-01",
            "supplier": "Test Vendor",
            "amount": amount_value,
            "currency": "TRY",
        },
    )


def _persisted_amount(transaction_id: int) -> Decimal:
    with Session(app_db.engine) as session:
        tx = session.get(StatementTransaction, transaction_id)
        assert tx is not None
        assert tx.local_amount is not None
        return tx.local_amount


def test_manual_transaction_accepts_string_amount(client):
    response = _post_manual_transaction(client, "123.45")
    assert response.status_code == 200, response.text
    tx_id = response.json()["transaction"]["id"]
    assert _persisted_amount(tx_id) == Decimal("123.45")


def test_manual_transaction_accepts_numeric_amount(client):
    response = _post_manual_transaction(client, 123.45)
    assert response.status_code == 200, response.text
    tx_id = response.json()["transaction"]["id"]
    assert _persisted_amount(tx_id) == Decimal("123.45")


def test_manual_transaction_string_and_numeric_produce_identical_decimals(client):
    string_resp = _post_manual_transaction(client, "123.45")
    numeric_resp = _post_manual_transaction(client, 123.45)
    assert string_resp.status_code == 200
    assert numeric_resp.status_code == 200
    string_amount = _persisted_amount(string_resp.json()["transaction"]["id"])
    numeric_amount = _persisted_amount(numeric_resp.json()["transaction"]["id"])
    assert string_amount == numeric_amount == Decimal("123.45")
    # Wire form must also be identical between both inputs.
    assert string_resp.json()["transaction"]["local_amount"] == numeric_resp.json()["transaction"]["local_amount"]
