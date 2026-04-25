"""API-boundary precision test for the Decimal migration (M1 Day 2.5).

Distinguishes "column is Decimal but the API still float-shapes it" from
"column is Decimal AND the API preserves Decimal exactly."

This test is xfailed today (post step 3): models.py uses Decimal columns,
but schemas.py still declares ``extracted_local_amount: float | None``,
so Pydantic coerces Decimal -> float at the response boundary and emits
a JSON number with possible binary-precision drift.

After step 5 (schemas migrated to Decimal + DecimalEncoder on the JSON
response), the response should be the string "123.4567" exactly. At that
point, drop the xfail marker.
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
from app.models import ReceiptDocument  # noqa: E402


@pytest.fixture
def client(isolated_db):
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.xfail(
    reason="Pending step 5 schema migration: schemas.ReceiptRead still "
    "declares extracted_local_amount as float, so Pydantic coerces the "
    "Decimal back to a float at response time."
)
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
