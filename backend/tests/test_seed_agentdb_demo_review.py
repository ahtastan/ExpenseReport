from __future__ import annotations

import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlmodel import Session, select

from app.db import engine
from app.models import (
    AgentReceiptComparison,
    AgentReceiptRead,
    AgentReceiptReviewRun,
    AppUser,
    MatchDecision,
    ReceiptDocument,
    ReviewRow,
    ReviewSession,
    StatementImport,
    StatementTransaction,
)
from app.services.review_sessions import session_payload

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from seed_agentdb_demo_review import main as seed_main  # noqa: E402


def _db_path() -> str:
    assert engine.url.database is not None
    return engine.url.database


def _seed_matched_review_row(session: Session) -> tuple[int, int, int]:
    user = AppUser(display_name="agentdb-demo-test")
    session.add(user)
    session.flush()

    statement = StatementImport(
        source_filename="statement.xlsx",
        row_count=1,
        uploader_user_id=user.id,
    )
    session.add(statement)
    session.flush()

    tx = StatementTransaction(
        statement_import_id=statement.id,
        transaction_date=date(2025, 12, 28),
        supplier_raw="A101",
        supplier_normalized="A101",
        local_currency="TRY",
        local_amount=Decimal("223.0000"),
        usd_amount=Decimal("223.0000"),
        source_row_ref="row-42",
    )
    receipt = ReceiptDocument(
        uploader_user_id=user.id,
        source="telegram",
        status="extracted",
        content_type="photo",
        original_file_name="synthetic_receipt.jpg",
        storage_path="/private/receipt.jpg",
        extracted_date=date(2025, 12, 28),
        extracted_supplier="A101",
        extracted_local_amount=Decimal("203.5000"),
        extracted_currency="TRY",
        receipt_type="payment_receipt",
        business_or_personal="Business",
        report_bucket="Office Supplies",
        business_reason="synthetic fixture",
        attendees="Hakan",
        needs_clarification=False,
    )
    session.add(tx)
    session.add(receipt)
    session.commit()
    session.refresh(statement)
    session.refresh(tx)
    session.refresh(receipt)

    match = MatchDecision(
        statement_transaction_id=tx.id,
        receipt_document_id=receipt.id,
        confidence="high",
        match_method="test",
        approved=True,
        reason="fixture",
    )
    review = ReviewSession(statement_import_id=statement.id, status="draft")
    session.add(match)
    session.add(review)
    session.commit()
    session.refresh(match)
    session.refresh(review)

    row = ReviewRow(
        review_session_id=review.id,
        statement_transaction_id=tx.id,
        receipt_document_id=receipt.id,
        match_decision_id=match.id,
        status="suggested",
        attention_required=False,
        source_json="{}",
        suggested_json="{}",
        confirmed_json="{}",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    assert statement.id is not None
    assert receipt.id is not None
    assert review.id is not None
    return statement.id, receipt.id, review.id


def _agent_counts(session: Session) -> tuple[int, int, int]:
    return (
        len(session.exec(select(AgentReceiptReviewRun)).all()),
        len(session.exec(select(AgentReceiptRead)).all()),
        len(session.exec(select(AgentReceiptComparison)).all()),
    )


def _canonical_counts(session: Session) -> dict[str, int]:
    return {
        "receiptdocument": len(session.exec(select(ReceiptDocument)).all()),
        "statementtransaction": len(session.exec(select(StatementTransaction)).all()),
        "matchdecision": len(session.exec(select(MatchDecision)).all()),
        "reviewsession": len(session.exec(select(ReviewSession)).all()),
        "reviewrow": len(session.exec(select(ReviewRow)).all()),
    }


def _payload_for_review(session: Session, review_id: int) -> dict:
    review = session.get(ReviewSession, review_id)
    assert review is not None
    rows = session_payload(session, review)["rows"]
    assert len(rows) == 1
    return rows[0]


def test_protected_prod_path_is_refused_without_prod_ack(capsys):
    rc = seed_main(
        [
            "--db-path",
            "/var/lib/dcexpense/expense_app.db",
            "--receipt-id",
            "42",
            "--state",
            "warn",
            "--amount",
            "223.00",
            "--currency",
            "TRY",
            "--date",
            "2025-12-28",
            "--supplier",
            "A101 YENI MAGAZACILIK",
            "--yes",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 2
    assert "REFUSED" in captured.err


def test_dry_run_writes_nothing(isolated_db, capsys):
    with Session(engine) as session:
        _, receipt_id, _ = _seed_matched_review_row(session)
        before = _agent_counts(session)

    rc = seed_main(
        [
            "--db-path",
            _db_path(),
            "--receipt-id",
            str(receipt_id),
            "--state",
            "warn",
            "--amount",
            "223.00",
            "--currency",
            "TRY",
            "--date",
            "2025-12-28",
            "--supplier",
            "A101 YENI MAGAZACILIK",
            "--dry-run",
        ]
    )

    with Session(engine) as session:
        after = _agent_counts(session)
    summary = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert summary["dry_run"] is True
    assert before == after == (0, 0, 0)


def test_missing_yes_writes_nothing(isolated_db, capsys):
    with Session(engine) as session:
        _, receipt_id, _ = _seed_matched_review_row(session)

    rc = seed_main(
        [
            "--db-path",
            _db_path(),
            "--receipt-id",
            str(receipt_id),
            "--state",
            "warn",
            "--amount",
            "223.00",
            "--currency",
            "TRY",
            "--date",
            "2025-12-28",
            "--supplier",
            "A101 YENI MAGAZACILIK",
        ]
    )

    with Session(engine) as session:
        assert _agent_counts(session) == (0, 0, 0)
    summary = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert summary["would_write"] is True


def test_warn_state_inserts_agentdb_rows(isolated_db, capsys):
    with Session(engine) as session:
        _, receipt_id, _ = _seed_matched_review_row(session)

    rc = seed_main(
        [
            "--db-path",
            _db_path(),
            "--receipt-id",
            str(receipt_id),
            "--state",
            "warn",
            "--amount",
            "223.00",
            "--currency",
            "TRY",
            "--date",
            "2025-12-28",
            "--supplier",
            "A101 YENI MAGAZACILIK",
            "--yes",
        ]
    )
    summary = json.loads(capsys.readouterr().out)

    with Session(engine) as session:
        assert _agent_counts(session) == (1, 1, 1)
        comparison = session.exec(select(AgentReceiptComparison)).one()
        assert comparison.risk_level == "warn"
        assert json.loads(comparison.differences_json) == ["supplier_mismatch"]
    assert rc == 0
    assert summary["state"] == "warn"
    assert summary["differences"] == ["supplier_mismatch"]


def test_block_state_inserts_amount_mismatch(isolated_db, capsys):
    with Session(engine) as session:
        _, receipt_id, _ = _seed_matched_review_row(session)

    rc = seed_main(
        [
            "--db-path",
            _db_path(),
            "--receipt-id",
            str(receipt_id),
            "--state",
            "block",
            "--amount",
            "223.00",
            "--currency",
            "TRY",
            "--date",
            "2025-12-28",
            "--supplier",
            "A101 YENI MAGAZACILIK",
            "--yes",
        ]
    )
    summary = json.loads(capsys.readouterr().out)

    with Session(engine) as session:
        comparison = session.exec(select(AgentReceiptComparison)).one()
        assert comparison.risk_level == "block"
        assert json.loads(comparison.differences_json) == ["amount_mismatch"]
    assert rc == 0
    assert summary["differences"] == ["amount_mismatch"]


def test_pass_state_omits_differences_from_ai_review(isolated_db, capsys):
    with Session(engine) as session:
        _, receipt_id, review_id = _seed_matched_review_row(session)

    rc = seed_main(
        [
            "--db-path",
            _db_path(),
            "--receipt-id",
            str(receipt_id),
            "--state",
            "pass",
            "--amount",
            "203.50",
            "--currency",
            "TRY",
            "--date",
            "2025-12-28",
            "--supplier",
            "A101",
            "--yes",
        ]
    )
    assert rc == 0
    json.loads(capsys.readouterr().out)

    with Session(engine) as session:
        payload = _payload_for_review(session, review_id)
        ai = payload["source"]["ai_review"]
        assert ai["status"] == "pass"
        assert "differences" not in ai


def test_stale_state_surfaces_as_stale_without_mutating_receipt(isolated_db, capsys):
    with Session(engine) as session:
        _, receipt_id, review_id = _seed_matched_review_row(session)
        before = _canonical_counts(session)
        before_receipt = session.get(ReceiptDocument, receipt_id)
        assert before_receipt is not None
        before_amount = before_receipt.extracted_local_amount

    rc = seed_main(
        [
            "--db-path",
            _db_path(),
            "--receipt-id",
            str(receipt_id),
            "--state",
            "stale",
            "--amount",
            "203.50",
            "--currency",
            "TRY",
            "--date",
            "2025-12-28",
            "--supplier",
            "A101",
            "--yes",
        ]
    )
    assert rc == 0
    json.loads(capsys.readouterr().out)

    with Session(engine) as session:
        payload = _payload_for_review(session, review_id)
        ai = payload["source"]["ai_review"]
        receipt_after = session.get(ReceiptDocument, receipt_id)
        assert receipt_after is not None
        assert ai["status"] == "stale"
        assert _canonical_counts(session) == before
        assert receipt_after.extracted_local_amount == before_amount


def test_seeded_row_appears_through_review_payload_and_does_not_leak(isolated_db, capsys):
    with Session(engine) as session:
        _, receipt_id, review_id = _seed_matched_review_row(session)

    rc = seed_main(
        [
            "--db-path",
            _db_path(),
            "--receipt-id",
            str(receipt_id),
            "--state",
            "warn",
            "--amount",
            "223.00",
            "--currency",
            "TRY",
            "--date",
            "2025-12-28",
            "--supplier",
            "A101 YENI MAGAZACILIK",
            "--yes",
        ]
    )
    assert rc == 0
    json.loads(capsys.readouterr().out)

    with Session(engine) as session:
        payload = _payload_for_review(session, review_id)
        ai = payload["source"]["ai_review"]
        assert ai["status"] == "warn"
        forbidden = {
            "prompt_text",
            "raw_model_json",
            "storage_path",
            "receipt_path",
            "canonical_snapshot_hash",
            "agent_read_hash",
            "prompt_hash",
            "input_hash",
            "model_name",
            "model_provider",
        }
        assert forbidden.isdisjoint(ai.keys())
        assert "storage_path" not in json.dumps(payload)


def test_canonical_tables_are_unchanged(isolated_db, capsys):
    with Session(engine) as session:
        _, receipt_id, _ = _seed_matched_review_row(session)
        before = _canonical_counts(session)

    rc = seed_main(
        [
            "--db-path",
            _db_path(),
            "--receipt-id",
            str(receipt_id),
            "--state",
            "block",
            "--amount",
            "223.00",
            "--currency",
            "TRY",
            "--date",
            "2025-12-28",
            "--supplier",
            "A101 YENI MAGAZACILIK",
            "--yes",
        ]
    )
    assert rc == 0
    json.loads(capsys.readouterr().out)

    with Session(engine) as session:
        assert _canonical_counts(session) == before
