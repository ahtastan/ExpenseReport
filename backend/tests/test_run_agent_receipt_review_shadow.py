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

from run_agent_receipt_review_shadow import main as shadow_main  # noqa: E402


def _db_path() -> str:
    assert engine.url.database is not None
    return engine.url.database


def _seed_matched_review_row(
    session: Session,
    *,
    amount: Decimal = Decimal("203.5000"),
    currency: str = "TRY",
    receipt_date: date = date(2025, 12, 28),
    supplier: str = "A101",
    business_reason: str | None = "synthetic fixture",
    attendees: str | None = "Hakan",
) -> tuple[int, int]:
    user = AppUser(display_name="shadow-runner-test")
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
        transaction_date=receipt_date,
        supplier_raw=supplier,
        supplier_normalized=supplier.upper(),
        local_currency=currency,
        local_amount=amount,
        usd_amount=amount,
        source_row_ref="row-41",
    )
    receipt = ReceiptDocument(
        uploader_user_id=user.id,
        source="telegram",
        status="extracted",
        content_type="photo",
        original_file_name="synthetic_receipt.jpg",
        storage_path="/private/receipt.jpg",
        extracted_date=receipt_date,
        extracted_supplier=supplier,
        extracted_local_amount=amount,
        extracted_currency=currency,
        receipt_type="payment_receipt",
        business_or_personal="Business",
        report_bucket="Office Supplies",
        business_reason=business_reason,
        attendees=attendees,
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
    assert receipt.id is not None
    assert review.id is not None
    return receipt.id, review.id


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


def _run_shadow(*args: str, capsys) -> tuple[int, dict]:
    rc = shadow_main(list(args))
    captured = capsys.readouterr()
    return rc, json.loads(captured.out)


def test_protected_prod_path_refused_without_ack(capsys):
    rc = shadow_main(
        [
            "--db-path",
            "/var/lib/dcexpense/expense_app.db",
            "--receipt-id",
            "41",
            "--provider",
            "mock",
            "--yes",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 2
    assert "REFUSED" in captured.err


def test_dry_run_writes_nothing(isolated_db, capsys):
    with Session(engine) as session:
        receipt_id, _ = _seed_matched_review_row(session)
        before = _agent_counts(session)

    rc, payload = _run_shadow(
        "--db-path",
        _db_path(),
        "--receipt-id",
        str(receipt_id),
        "--provider",
        "mock",
        "--dry-run",
        capsys=capsys,
    )

    with Session(engine) as session:
        after = _agent_counts(session)
    assert rc == 0
    assert payload["dry_run"] is True
    assert payload["would_write"] is True
    assert before == after == (0, 0, 0)


def test_missing_yes_writes_nothing(isolated_db, capsys):
    with Session(engine) as session:
        receipt_id, _ = _seed_matched_review_row(session)

    rc, payload = _run_shadow(
        "--db-path",
        _db_path(),
        "--receipt-id",
        str(receipt_id),
        "--provider",
        "mock",
        capsys=capsys,
    )

    with Session(engine) as session:
        assert _agent_counts(session) == (0, 0, 0)
    assert rc == 0
    assert payload["would_write"] is True


def test_mock_pass_writes_pass_with_empty_differences(isolated_db, capsys):
    with Session(engine) as session:
        receipt_id, _ = _seed_matched_review_row(session)

    rc, payload = _run_shadow(
        "--db-path",
        _db_path(),
        "--receipt-id",
        str(receipt_id),
        "--provider",
        "mock",
        "--mock-state",
        "pass",
        "--yes",
        capsys=capsys,
    )

    with Session(engine) as session:
        run = session.exec(select(AgentReceiptReviewRun)).one()
        read = session.exec(select(AgentReceiptRead)).one()
        comparison = session.exec(select(AgentReceiptComparison)).one()
        assert run.model_name == "mock"
        assert read.receipt_document_id == receipt_id
        assert comparison.risk_level == "pass"
        assert json.loads(comparison.differences_json) == []
        assert _agent_counts(session) == (1, 1, 1)
    assert rc == 0
    assert payload["provider"] == "mock"
    assert payload["risk_level"] == "pass"
    assert payload["differences"] == []
    assert payload["would_write"] is False


def test_mock_warn_writes_warn_date_and_supplier_differences(isolated_db, capsys):
    with Session(engine) as session:
        receipt_id, _ = _seed_matched_review_row(session)

    rc, payload = _run_shadow(
        "--db-path",
        _db_path(),
        "--receipt-id",
        str(receipt_id),
        "--provider",
        "mock",
        "--mock-state",
        "warn",
        "--mock-date",
        "2025-12-25",
        "--mock-supplier",
        "TRANSIT CUP",
        "--yes",
        capsys=capsys,
    )

    with Session(engine) as session:
        comparison = session.exec(select(AgentReceiptComparison)).one()
        differences = json.loads(comparison.differences_json)
        assert comparison.risk_level == "warn"
        assert "date_mismatch" in differences
        assert "supplier_mismatch" in differences
    assert rc == 0
    assert payload["risk_level"] == "warn"
    assert "date_mismatch" in payload["differences"]
    assert "supplier_mismatch" in payload["differences"]


def test_mock_block_writes_block_amount_mismatch(isolated_db, capsys):
    with Session(engine) as session:
        receipt_id, _ = _seed_matched_review_row(session)

    rc, payload = _run_shadow(
        "--db-path",
        _db_path(),
        "--receipt-id",
        str(receipt_id),
        "--provider",
        "mock",
        "--mock-state",
        "block",
        "--mock-amount",
        "999.99",
        "--yes",
        capsys=capsys,
    )

    with Session(engine) as session:
        comparison = session.exec(select(AgentReceiptComparison)).one()
        assert comparison.risk_level == "block"
        assert json.loads(comparison.differences_json) == ["amount_mismatch"]
    assert rc == 0
    assert payload["risk_level"] == "block"
    assert payload["differences"] == ["amount_mismatch"]


def test_written_result_appears_in_review_payload(isolated_db, capsys):
    with Session(engine) as session:
        receipt_id, review_id = _seed_matched_review_row(session)

    rc, payload = _run_shadow(
        "--db-path",
        _db_path(),
        "--receipt-id",
        str(receipt_id),
        "--provider",
        "mock",
        "--mock-state",
        "block",
        "--mock-amount",
        "999.99",
        "--yes",
        capsys=capsys,
    )
    assert rc == 0
    assert payload["comparison_id"]

    with Session(engine) as session:
        row = _payload_for_review(session, review_id)
        ai = row["source"]["ai_review"]
        assert ai["status"] == "block"
        assert ai["risk_level"] == "block"
        assert row["attention_required"] is False


def test_canonical_tables_unchanged(isolated_db, capsys):
    with Session(engine) as session:
        receipt_id, _ = _seed_matched_review_row(session)
        before = _canonical_counts(session)

    rc, _ = _run_shadow(
        "--db-path",
        _db_path(),
        "--receipt-id",
        str(receipt_id),
        "--provider",
        "mock",
        "--mock-state",
        "block",
        "--yes",
        capsys=capsys,
    )

    with Session(engine) as session:
        assert _canonical_counts(session) == before
    assert rc == 0


def test_public_payload_has_no_private_or_debug_fields(isolated_db, capsys):
    with Session(engine) as session:
        receipt_id, review_id = _seed_matched_review_row(session)

    rc, _ = _run_shadow(
        "--db-path",
        _db_path(),
        "--receipt-id",
        str(receipt_id),
        "--provider",
        "mock",
        "--mock-state",
        "warn",
        "--mock-date",
        "2025-12-25",
        "--yes",
        capsys=capsys,
    )
    assert rc == 0

    with Session(engine) as session:
        payload = _payload_for_review(session, review_id)
    encoded = json.dumps(payload)
    for forbidden in (
        "prompt_text",
        "raw_model_json",
        "storage_path",
        "receipt_path",
        "canonical_snapshot_hash",
        "agent_read_hash",
        "input_hash",
        "debug",
    ):
        assert forbidden not in encoded


def test_no_live_model_or_telegram_modules_imported(isolated_db, capsys):
    before = set(sys.modules)
    with Session(engine) as session:
        receipt_id, _ = _seed_matched_review_row(session)

    rc, _ = _run_shadow(
        "--db-path",
        _db_path(),
        "--receipt-id",
        str(receipt_id),
        "--provider",
        "mock",
        "--dry-run",
        capsys=capsys,
    )
    after = set(sys.modules)
    newly_loaded = after - before
    forbidden_prefixes = (
        "openai",
        "anthropic",
        "deepseek",
        "app.services.telegram",
        "telegram",
    )
    assert rc == 0
    assert not any(
        module == prefix or module.startswith(f"{prefix}.")
        for module in newly_loaded
        for prefix in forbidden_prefixes
    )
