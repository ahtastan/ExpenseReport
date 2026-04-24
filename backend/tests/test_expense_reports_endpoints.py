"""M1 Day 2 Phase 1 — /expense-reports CRUD endpoints.

Covers creation, listing, detail, attach, and detach. Path prefix is
``/expense-reports`` (temporary — Phase 2+ may consolidate with the legacy
``/reports`` router).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

VERIFY_ROOT = Path.cwd() / ".verify_data"
VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{VERIFY_ROOT / f'expense_reports_{uuid4().hex}.db'}"
os.environ["EXPENSE_STORAGE_ROOT"] = str(VERIFY_ROOT)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    AppUser,
    ExpenseReport,
    ReceiptDocument,
    ReportRun,
    ReviewSession,
)


@pytest.fixture
def client(isolated_db):
    # isolated_db (from conftest.py) has already rebound app.db.engine to the
    # per-test SQLite. TestClient triggers the lifespan handler which calls
    # create_db_and_tables() on the new engine — safe because the fixture
    # already created the schema.
    with TestClient(app) as c:
        yield c


def _make_user(isolated_db, telegram_user_id: int = 111) -> int:
    with Session(isolated_db) as session:
        user = AppUser(telegram_user_id=telegram_user_id)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user.id


def _make_receipt(isolated_db, uploader_user_id: int, **overrides) -> int:
    with Session(isolated_db) as session:
        receipt = ReceiptDocument(
            uploader_user_id=uploader_user_id,
            original_file_name="test.jpg",
            status="received",
            **overrides,
        )
        session.add(receipt)
        session.commit()
        session.refresh(receipt)
        return receipt.id


def _valid_payload(owner_user_id: int, **overrides) -> dict:
    payload = {
        "owner_user_id": owner_user_id,
        "report_kind": "diners_statement",
        "title": "April trip",
        "report_currency": "USD",
    }
    payload.update(overrides)
    return payload


# ─── Creation ────────────────────────────────────────────────────────────────

def test_create_report_with_valid_payload_returns_201(client, isolated_db):
    owner_id = _make_user(isolated_db)
    res = client.post("/expense-reports", json=_valid_payload(owner_id))
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["owner_user_id"] == owner_id
    assert body["report_kind"] == "diners_statement"
    assert body["report_currency"] == "USD"
    assert body["status"] == "draft"
    assert body["id"] > 0


def test_create_report_with_invalid_kind_returns_422(client, isolated_db):
    owner_id = _make_user(isolated_db)
    res = client.post(
        "/expense-reports",
        json=_valid_payload(owner_id, report_kind="other"),
    )
    assert res.status_code == 422


def test_create_report_with_invalid_currency_returns_422(client, isolated_db):
    owner_id = _make_user(isolated_db)
    res = client.post(
        "/expense-reports",
        json=_valid_payload(owner_id, report_currency="TRY"),
    )
    assert res.status_code == 422


def test_create_report_with_eur_currency_succeeds(client, isolated_db):
    owner_id = _make_user(isolated_db)
    res = client.post(
        "/expense-reports",
        json=_valid_payload(owner_id, report_currency="EUR"),
    )
    assert res.status_code == 201
    assert res.json()["report_currency"] == "EUR"


def test_create_report_with_unknown_user_returns_404(client, isolated_db):
    res = client.post("/expense-reports", json=_valid_payload(99999))
    assert res.status_code == 404
    assert "owner_user_id" in res.json()["detail"]


# ─── List ────────────────────────────────────────────────────────────────────

def test_list_reports_returns_empty_list_when_none_exist(client, isolated_db):
    owner_id = _make_user(isolated_db)
    res = client.get(f"/expense-reports?owner_user_id={owner_id}")
    assert res.status_code == 200
    assert res.json() == []


def test_list_reports_filters_by_status(client, isolated_db):
    owner_id = _make_user(isolated_db)
    client.post("/expense-reports", json=_valid_payload(owner_id, title="draft one"))
    client.post("/expense-reports", json=_valid_payload(owner_id, title="draft two"))
    # Flip one to submitted directly via DB (no endpoint for this in Phase 1).
    from sqlmodel import select as _select
    with Session(isolated_db) as session:
        any_report = session.exec(_select(ExpenseReport)).first()
        any_report.status = "submitted"
        session.add(any_report)
        session.commit()

    drafts = client.get(f"/expense-reports?owner_user_id={owner_id}&status=draft").json()
    submitted = client.get(
        f"/expense-reports?owner_user_id={owner_id}&status=submitted"
    ).json()
    assert len(drafts) == 1
    assert len(submitted) == 1
    assert drafts[0]["status"] == "draft"
    assert submitted[0]["status"] == "submitted"


def test_list_reports_filters_by_kind(client, isolated_db):
    owner_id = _make_user(isolated_db)
    client.post(
        "/expense-reports",
        json=_valid_payload(owner_id, report_kind="diners_statement", title="d"),
    )
    client.post(
        "/expense-reports",
        json=_valid_payload(owner_id, report_kind="personal_reimbursement", title="p"),
    )
    diners = client.get(
        f"/expense-reports?owner_user_id={owner_id}&report_kind=diners_statement"
    ).json()
    personal = client.get(
        f"/expense-reports?owner_user_id={owner_id}&report_kind=personal_reimbursement"
    ).json()
    assert len(diners) == 1 and diners[0]["report_kind"] == "diners_statement"
    assert len(personal) == 1 and personal[0]["report_kind"] == "personal_reimbursement"


def test_list_reports_scopes_to_owner_user_id(client, isolated_db):
    owner_a = _make_user(isolated_db, telegram_user_id=10)
    owner_b = _make_user(isolated_db, telegram_user_id=20)
    client.post("/expense-reports", json=_valid_payload(owner_a, title="A-1"))
    client.post("/expense-reports", json=_valid_payload(owner_a, title="A-2"))
    client.post("/expense-reports", json=_valid_payload(owner_b, title="B-1"))

    a_reports = client.get(f"/expense-reports?owner_user_id={owner_a}").json()
    b_reports = client.get(f"/expense-reports?owner_user_id={owner_b}").json()
    assert len(a_reports) == 2
    assert len(b_reports) == 1
    assert all(r["owner_user_id"] == owner_a for r in a_reports)
    assert all(r["owner_user_id"] == owner_b for r in b_reports)


# ─── Read detail ─────────────────────────────────────────────────────────────

def test_read_report_returns_detail_with_receipt_count(client, isolated_db):
    owner_id = _make_user(isolated_db)
    create_res = client.post("/expense-reports", json=_valid_payload(owner_id))
    report_id = create_res.json()["id"]

    r1 = _make_receipt(isolated_db, owner_id, expense_report_id=report_id)
    r2 = _make_receipt(isolated_db, owner_id, expense_report_id=report_id)
    _orphan = _make_receipt(isolated_db, owner_id)  # should not count

    # Seed a ReviewSession and ReportRun bound to this report.
    with Session(isolated_db) as session:
        session.add(ReviewSession(expense_report_id=report_id, status="draft"))
        session.add(ReviewSession(expense_report_id=report_id, status="draft"))
        session.add(ReportRun(expense_report_id=report_id, status="draft"))
        session.add(ReportRun(expense_report_id=report_id, status="draft"))
        session.commit()

    res = client.get(f"/expense-reports/{report_id}?owner_user_id={owner_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == report_id
    assert body["receipt_count"] == 2
    assert body["review_session_id"] is not None
    assert len(body["report_run_ids"]) == 2
    assert r1 != r2  # just to exercise vars


def test_read_report_404_when_not_found(client, isolated_db):
    owner_id = _make_user(isolated_db)
    res = client.get(f"/expense-reports/99999?owner_user_id={owner_id}")
    assert res.status_code == 404


def test_read_report_403_when_owner_mismatch(client, isolated_db):
    owner_a = _make_user(isolated_db, telegram_user_id=100)
    owner_b = _make_user(isolated_db, telegram_user_id=200)
    created = client.post("/expense-reports", json=_valid_payload(owner_a)).json()
    res = client.get(f"/expense-reports/{created['id']}?owner_user_id={owner_b}")
    assert res.status_code == 403


# ─── Attach ──────────────────────────────────────────────────────────────────

def test_attach_receipt_succeeds_when_unattached(client, isolated_db):
    owner_id = _make_user(isolated_db)
    report_id = client.post("/expense-reports", json=_valid_payload(owner_id)).json()["id"]
    receipt_id = _make_receipt(isolated_db, owner_id)

    res = client.post(
        f"/expense-reports/{report_id}/receipts/{receipt_id}?owner_user_id={owner_id}"
    )
    assert res.status_code == 200
    body = res.json()
    assert body["receipt_id"] == receipt_id
    assert body["expense_report_id"] == report_id
    assert body["message"] == "Attached"

    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, receipt_id)
        assert receipt.expense_report_id == report_id


def test_attach_receipt_is_idempotent_when_already_on_same_report(client, isolated_db):
    owner_id = _make_user(isolated_db)
    report_id = client.post("/expense-reports", json=_valid_payload(owner_id)).json()["id"]
    receipt_id = _make_receipt(isolated_db, owner_id, expense_report_id=report_id)

    res = client.post(
        f"/expense-reports/{report_id}/receipts/{receipt_id}?owner_user_id={owner_id}"
    )
    assert res.status_code == 200
    assert res.json()["message"] == "Already attached"


def test_attach_receipt_409_when_receipt_on_different_report(client, isolated_db):
    owner_id = _make_user(isolated_db)
    first = client.post("/expense-reports", json=_valid_payload(owner_id, title="first")).json()
    second = client.post("/expense-reports", json=_valid_payload(owner_id, title="second")).json()
    receipt_id = _make_receipt(isolated_db, owner_id, expense_report_id=first["id"])

    res = client.post(
        f"/expense-reports/{second['id']}/receipts/{receipt_id}?owner_user_id={owner_id}"
    )
    assert res.status_code == 409
    assert "already attached" in res.json()["detail"].lower()


def test_attach_receipt_403_when_report_owner_differs_from_receipt_uploader(client, isolated_db):
    owner_a = _make_user(isolated_db, telegram_user_id=100)
    owner_b = _make_user(isolated_db, telegram_user_id=200)
    report_id = client.post("/expense-reports", json=_valid_payload(owner_a)).json()["id"]
    receipt_id = _make_receipt(isolated_db, owner_b)  # uploaded by B

    # Call as A (report owner); receipt uploader is B.
    res = client.post(
        f"/expense-reports/{report_id}/receipts/{receipt_id}?owner_user_id={owner_a}"
    )
    assert res.status_code == 403


def test_attach_receipt_404_when_receipt_missing(client, isolated_db):
    owner_id = _make_user(isolated_db)
    report_id = client.post("/expense-reports", json=_valid_payload(owner_id)).json()["id"]
    res = client.post(
        f"/expense-reports/{report_id}/receipts/99999?owner_user_id={owner_id}"
    )
    assert res.status_code == 404


# ─── Detach ──────────────────────────────────────────────────────────────────

def test_detach_receipt_succeeds(client, isolated_db):
    owner_id = _make_user(isolated_db)
    report_id = client.post("/expense-reports", json=_valid_payload(owner_id)).json()["id"]
    receipt_id = _make_receipt(isolated_db, owner_id, expense_report_id=report_id)

    res = client.delete(
        f"/expense-reports/{report_id}/receipts/{receipt_id}?owner_user_id={owner_id}"
    )
    assert res.status_code == 200
    body = res.json()
    assert body["receipt_id"] == receipt_id
    assert body["message"] == "Detached"

    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, receipt_id)
        assert receipt.expense_report_id is None


def test_detach_receipt_404_when_receipt_not_on_this_report(client, isolated_db):
    owner_id = _make_user(isolated_db)
    report_id = client.post("/expense-reports", json=_valid_payload(owner_id)).json()["id"]
    # Receipt uploaded by owner but not attached to any report.
    receipt_id = _make_receipt(isolated_db, owner_id)

    res = client.delete(
        f"/expense-reports/{report_id}/receipts/{receipt_id}?owner_user_id={owner_id}"
    )
    assert res.status_code == 404
    assert "not attached" in res.json()["detail"].lower()
