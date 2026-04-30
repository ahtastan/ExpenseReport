from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import inspect
from sqlmodel import Session, select

from app.models import (
    AgentReceiptComparison,
    AgentReceiptRead,
    AgentReceiptReviewRun,
    MatchDecision,
    PolicyDecision,
    ReceiptDocument,
    ReviewRow,
    ReviewSession,
    StatementImport,
    StatementTransaction,
)
from app.services.agent_receipt_review_persistence import (
    build_canonical_receipt_snapshot,
    canonical_receipt_snapshot_hash,
    get_latest_agent_receipt_comparison,
)
import pytest

from migrations.f_ai_0b1_agent_receipt_review_tables import migrate
from migrations import f_ai_0b1_agent_receipt_review_tables as agentdb_migration


REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "scripts" / "run_agent_receipt_review.py"


def _db_path(engine) -> str:
    assert engine.url.database is not None
    return engine.url.database


def _cli_env(**overrides: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(overrides)
    return env


def _canonical_payload() -> dict:
    return {
        "date": "2025-11-20",
        "supplier": "MEMOLIFE",
        "amount": "770.00",
        "currency": "TRY",
        "business_or_personal": "Business",
        "business_reason": "Kocaeli customer visit",
        "attendees": "Hakan",
    }


def _agent_payload(**overrides) -> dict:
    payload = {
        "merchant_name": "MEMOLIFE",
        "merchant_address": "Kocaeli, Turkey",
        "receipt_date": "2025-11-20",
        "receipt_time": None,
        "total_amount": "770.00",
        "currency": "TRY",
        "amount_text": "770,00 TL",
        "line_items": [{"description": "meal/snack", "quantity": "1", "amount": "770.00"}],
        "tax_amount": None,
        "payment_method": "card",
        "receipt_category": "meal",
        "confidence": 0.92,
        "raw_text_summary": "Meal/snack receipt with visible total 770,00 TL.",
    }
    payload.update(overrides)
    return payload


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _create_receipt(session: Session) -> ReceiptDocument:
    receipt = ReceiptDocument(
        source="telegram",
        status="extracted",
        content_type="photo",
        extracted_date=date(2025, 11, 20),
        extracted_supplier="MEMOLIFE",
        extracted_local_amount=Decimal("770.0000"),
        extracted_currency="TRY",
        receipt_type="itemized",
        business_or_personal="Business",
        business_reason="Kocaeli customer visit",
        attendees="Hakan",
        needs_clarification=False,
    )
    session.add(receipt)
    session.commit()
    session.refresh(receipt)
    return receipt


def _create_canonical_neighbors(session: Session, receipt: ReceiptDocument) -> dict[str, int]:
    statement = StatementImport(source_filename="statement.xlsx")
    session.add(statement)
    session.commit()
    session.refresh(statement)

    tx = StatementTransaction(
        statement_import_id=statement.id,
        transaction_date=date(2025, 11, 20),
        supplier_raw="MEMOLIFE",
        supplier_normalized="memolife",
        local_currency="TRY",
        local_amount=Decimal("770.0000"),
        source_row_ref="row-1",
    )
    session.add(tx)
    session.commit()
    session.refresh(tx)

    match = MatchDecision(
        statement_transaction_id=tx.id,
        receipt_document_id=receipt.id,
        confidence="high",
        match_method="test",
        approved=False,
        rejected=False,
        reason="fixture",
    )
    policy = PolicyDecision(
        statement_transaction_id=tx.id,
        business_or_personal="Business",
        include_in_report=True,
        justification="fixture",
    )
    review = ReviewSession(statement_import_id=statement.id, status="draft")
    session.add(match)
    session.add(policy)
    session.add(review)
    session.commit()
    session.refresh(match)
    session.refresh(policy)
    session.refresh(review)

    row = ReviewRow(
        review_session_id=review.id,
        statement_transaction_id=tx.id,
        receipt_document_id=receipt.id,
        match_decision_id=match.id,
        status="suggested",
        attention_required=False,
        source_json='{"fixture": true}',
        suggested_json='{"fixture": true}',
        confirmed_json="{}",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return {
        "statement_transaction_id": tx.id,
        "match_decision_id": match.id,
        "policy_decision_id": policy.id,
        "review_row_id": row.id,
    }


def _run_cli(args: list[str], *, env: dict[str, str] | None = None, check: bool = True):
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=check,
        env=env,
    )


def test_agentdb_tables_create_successfully(isolated_db):
    inspector = inspect(isolated_db)

    assert "agent_receipt_review_run" in inspector.get_table_names()
    assert "agent_receipt_read" in inspector.get_table_names()
    assert "agent_receipt_comparison" in inspector.get_table_names()


def test_agentdb_migration_creates_shadow_tables(tmp_path):
    db_path = tmp_path / "migration.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE receiptdocument (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE reviewsession (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE reviewrow (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE statementtransaction (id INTEGER PRIMARY KEY)")

    migrate(str(db_path), apply=True)

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {
        "agent_receipt_review_run",
        "agent_receipt_read",
        "agent_receipt_comparison",
    }.issubset(tables)


def test_agentdb_migration_dry_run_does_not_create_shadow_tables(tmp_path):
    db_path = tmp_path / "migration_dry_run.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE receiptdocument (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE reviewsession (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE reviewrow (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE statementtransaction (id INTEGER PRIMARY KEY)")

    migrate(str(db_path), apply=False)

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "agent_receipt_review_run" not in tables
    assert "agent_receipt_read" not in tables
    assert "agent_receipt_comparison" not in tables


def test_agentdb_migration_apply_is_idempotent(tmp_path):
    db_path = tmp_path / "migration_idempotent.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE receiptdocument (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE reviewsession (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE reviewrow (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE statementtransaction (id INTEGER PRIMARY KEY)")

    first = migrate(str(db_path), apply=True)
    second = migrate(str(db_path), apply=True)

    assert set(first.tables_created) == {
        "agent_receipt_review_run",
        "agent_receipt_read",
        "agent_receipt_comparison",
    }
    assert second.tables_created == []
    with sqlite3.connect(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE 'agent_receipt_%'"
        ).fetchone()[0]
    assert count == 3


def test_agentdb_migration_apply_leaves_agent_tables_empty_and_canonical_rows_unchanged(tmp_path):
    db_path = tmp_path / "migration_canonical.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE receiptdocument (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE reviewsession (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE reviewrow (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE statementtransaction (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO receiptdocument (id) VALUES (1), (2), (3)")
        conn.execute("INSERT INTO reviewsession (id) VALUES (10), (11)")
        conn.execute("INSERT INTO reviewrow (id) VALUES (100)")
        conn.execute("INSERT INTO statementtransaction (id) VALUES (1000), (1001)")

    pre_counts = {}
    with sqlite3.connect(db_path) as conn:
        for t in ("receiptdocument", "reviewsession", "reviewrow", "statementtransaction"):
            pre_counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]

    migrate(str(db_path), apply=True)

    with sqlite3.connect(db_path) as conn:
        for agent_table in (
            "agent_receipt_review_run",
            "agent_receipt_read",
            "agent_receipt_comparison",
        ):
            count = conn.execute(f"SELECT COUNT(*) FROM {agent_table}").fetchone()[0]
            assert count == 0
        for t, expected in pre_counts.items():
            actual = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            assert actual == expected


def test_agentdb_migration_refuses_protected_production_path():
    with pytest.raises(SystemExit) as excinfo:
        migrate("/var/lib/dcexpense/expense_app.db", apply=True)
    assert excinfo.value.code == 2

    with pytest.raises(SystemExit) as excinfo:
        migrate("/opt/dcexpense/app/data/expense_app.db", apply=False)
    assert excinfo.value.code == 2


def test_agentdb_migration_apply_failure_surfaces_failing_sql_to_stderr(
    tmp_path, monkeypatch, capsys
):
    db_path = tmp_path / "migration_fail.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE receiptdocument (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE reviewsession (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE reviewrow (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE statementtransaction (id INTEGER PRIMARY KEY)")

    bogus_sql = (
        "CREATE INDEX ix_agent_receipt_bogus_repro_marker "
        "ON nonexistent_table(nope)"
    )
    monkeypatch.setattr(
        agentdb_migration,
        "INDEX_SQL",
        [bogus_sql, *agentdb_migration.INDEX_SQL],
    )

    with pytest.raises(SystemExit) as excinfo:
        migrate(str(db_path), apply=True)
    assert excinfo.value.code == 3

    captured = capsys.readouterr()
    assert "ERROR: migration failed and was rolled back." in captured.err
    assert "CREATE INDEX" in captured.err
    assert "nonexistent_table" in captured.err
    assert "OperationalError" in captured.err

    with sqlite3.connect(db_path) as conn:
        agent_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'agent_%'"
        ).fetchall()
    assert agent_rows == []
    with sqlite3.connect(db_path) as conn:
        index_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ix_agent_%'"
        ).fetchall()
    assert index_rows == []

    log_files = sorted(tmp_path.glob("migration_fail.db.pre-f-ai-0b1-*.migration.log"))
    assert log_files, "expected migration log file to be written"
    content = log_files[-1].read_text(encoding="utf-8")
    assert "CREATE INDEX" in content
    assert "nonexistent_table" in content
    assert "OperationalError" in content


def test_agentdb_migration_apply_failure_preserves_existing_canonical_rows(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "migration_fail_preserve.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE receiptdocument (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE reviewsession (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE reviewrow (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE statementtransaction (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO receiptdocument (id) VALUES (1), (2), (3)")

    bogus_sql = "CREATE TABLE agent_receipt_review_run (id INTEGER REFERENCES nope(missing) WITHOUT ROWID)"
    monkeypatch.setattr(
        agentdb_migration,
        "CREATE_TABLE_SQL",
        [bogus_sql, *agentdb_migration.CREATE_TABLE_SQL],
    )

    with pytest.raises(SystemExit):
        migrate(str(db_path), apply=True)

    with sqlite3.connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM receiptdocument").fetchone()[0]
    assert count == 3


def test_successful_mock_write_db_writes_one_shadow_record_each_and_preserves_canonical_tables(
    isolated_db, tmp_path
):
    with Session(isolated_db) as session:
        receipt = _create_receipt(session)
        neighbors = _create_canonical_neighbors(session, receipt)
        before_receipt = session.get(ReceiptDocument, receipt.id).model_dump()
        before_tx = session.get(
            StatementTransaction, neighbors["statement_transaction_id"]
        ).model_dump()
        before_match = session.get(MatchDecision, neighbors["match_decision_id"]).model_dump()
        before_policy = session.get(PolicyDecision, neighbors["policy_decision_id"]).model_dump()
        before_row = session.get(ReviewRow, neighbors["review_row_id"]).model_dump()

    agent_path = tmp_path / "agent.json"
    out_path = tmp_path / "result.json"
    _write_json(agent_path, _agent_payload())

    _run_cli(
        [
            "--agent-json",
            str(agent_path),
            "--out",
            str(out_path),
            "--db",
            _db_path(isolated_db),
            "--receipt-id",
            str(receipt.id),
            "--write-db",
            "--mock",
        ],
        env=_cli_env(AI_AGENT_DB_WRITE_ENABLED="true"),
    )

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "0a"
    with Session(isolated_db) as session:
        runs = session.exec(select(AgentReceiptReviewRun)).all()
        reads = session.exec(select(AgentReceiptRead)).all()
        comparisons = session.exec(select(AgentReceiptComparison)).all()
        assert len(runs) == 1
        assert len(reads) == 1
        assert len(comparisons) == 1
        assert runs[0].status == "completed"
        assert runs[0].receipt_document_id == receipt.id
        assert runs[0].raw_model_json is None
        assert runs[0].prompt_text is None
        assert reads[0].receipt_document_id == receipt.id
        assert comparisons[0].risk_level == "pass"
        assert comparisons[0].recommended_action == "accept"

        assert session.get(ReceiptDocument, receipt.id).model_dump() == before_receipt
        assert (
            session.get(StatementTransaction, neighbors["statement_transaction_id"]).model_dump()
            == before_tx
        )
        assert session.get(MatchDecision, neighbors["match_decision_id"]).model_dump() == before_match
        assert session.get(PolicyDecision, neighbors["policy_decision_id"]).model_dump() == before_policy
        assert session.get(ReviewRow, neighbors["review_row_id"]).model_dump() == before_row


def test_raw_model_json_and_prompt_text_flags_store_optional_payloads(isolated_db, tmp_path):
    with Session(isolated_db) as session:
        receipt = _create_receipt(session)

    agent_payload = _agent_payload()
    agent_path = tmp_path / "agent.json"
    out_path = tmp_path / "result.json"
    _write_json(agent_path, agent_payload)

    _run_cli(
        [
            "--agent-json",
            str(agent_path),
            "--out",
            str(out_path),
            "--db",
            _db_path(isolated_db),
            "--receipt-id",
            str(receipt.id),
            "--write-db",
            "--mock",
        ],
        env=_cli_env(
            AI_AGENT_DB_WRITE_ENABLED="true",
            AI_STORE_RAW_MODEL_JSON="true",
            AI_STORE_PROMPT_TEXT="true",
        ),
    )

    with Session(isolated_db) as session:
        run = session.exec(select(AgentReceiptReviewRun)).one()
        assert json.loads(run.raw_model_json) == agent_payload
        assert run.raw_model_json_redacted is False
        assert run.prompt_text is not None
        assert "strict JSON" in run.prompt_text


def test_failed_mock_write_db_persists_failed_run_without_read_or_comparison_or_canonical_mutation(
    isolated_db, tmp_path
):
    with Session(isolated_db) as session:
        receipt = _create_receipt(session)
        before_receipt = session.get(ReceiptDocument, receipt.id).model_dump()

    bad_agent_path = tmp_path / "bad_agent.json"
    bad_agent_path.write_text("{not-json", encoding="utf-8")
    out_path = tmp_path / "result.json"

    completed = _run_cli(
        [
            "--agent-json",
            str(bad_agent_path),
            "--out",
            str(out_path),
            "--db",
            _db_path(isolated_db),
            "--receipt-id",
            str(receipt.id),
            "--write-db",
            "--mock",
        ],
        env=_cli_env(AI_AGENT_DB_WRITE_ENABLED="true"),
        check=False,
    )

    assert completed.returncode != 0
    assert not out_path.exists()
    with Session(isolated_db) as session:
        runs = session.exec(select(AgentReceiptReviewRun)).all()
        assert len(runs) == 1
        assert runs[0].status == "failed"
        assert runs[0].error_code == "agent_review_failed"
        assert runs[0].error_message
        assert session.exec(select(AgentReceiptRead)).all() == []
        assert session.exec(select(AgentReceiptComparison)).all() == []
        assert session.get(ReceiptDocument, receipt.id).model_dump() == before_receipt


def test_get_latest_agent_receipt_comparison_returns_latest_successful_comparison(isolated_db, tmp_path):
    with Session(isolated_db) as session:
        receipt = _create_receipt(session)

    agent_path = tmp_path / "agent.json"
    _write_json(agent_path, _agent_payload())
    env = _cli_env(AI_AGENT_DB_WRITE_ENABLED="true")
    base_args = [
        "--agent-json",
        str(agent_path),
        "--db",
        _db_path(isolated_db),
        "--receipt-id",
        str(receipt.id),
        "--write-db",
        "--mock",
    ]
    _run_cli([*base_args, "--out", str(tmp_path / "first.json")], env=env)
    _write_json(agent_path, _agent_payload(total_amount="999.00"))
    _run_cli([*base_args, "--out", str(tmp_path / "second.json")], env=env)

    with Session(isolated_db) as session:
        latest = get_latest_agent_receipt_comparison(session, receipt.id)
        assert latest is not None
        assert latest.risk_level == "block"
        assert "amount_mismatch" in json.loads(latest.differences_json)
        assert get_latest_agent_receipt_comparison(session, receipt.id + 9999) is None


def test_canonical_snapshot_hash_changes_when_receipt_fields_change(isolated_db):
    with Session(isolated_db) as session:
        receipt = _create_receipt(session)
        original_snapshot = build_canonical_receipt_snapshot(receipt)
        original_hash = canonical_receipt_snapshot_hash(original_snapshot)
        receipt.extracted_local_amount = Decimal("771.0000")
        changed_snapshot = build_canonical_receipt_snapshot(receipt)
        changed_hash = canonical_receipt_snapshot_hash(changed_snapshot)

    assert original_hash != changed_hash


def test_file_only_cli_behavior_still_works_without_db(tmp_path):
    canonical_path = tmp_path / "canonical.json"
    agent_path = tmp_path / "agent.json"
    out_path = tmp_path / "result.json"
    _write_json(canonical_path, _canonical_payload())
    _write_json(agent_path, _agent_payload())

    _run_cli(
        [
            "--canonical-json",
            str(canonical_path),
            "--agent-json",
            str(agent_path),
            "--out",
            str(out_path),
        ]
    )

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "0a"
    assert payload["comparison"]["risk_level"] == "pass"


def test_write_db_requires_mock_and_write_feature_flag(isolated_db, tmp_path):
    with Session(isolated_db) as session:
        receipt = _create_receipt(session)
    agent_path = tmp_path / "agent.json"
    _write_json(agent_path, _agent_payload())

    without_mock = _run_cli(
        [
            "--agent-json",
            str(agent_path),
            "--out",
            str(tmp_path / "result.json"),
            "--db",
            _db_path(isolated_db),
            "--receipt-id",
            str(receipt.id),
            "--write-db",
        ],
        env=_cli_env(AI_AGENT_DB_WRITE_ENABLED="true"),
        check=False,
    )
    assert without_mock.returncode != 0
    assert "requires --mock" in without_mock.stderr

    without_flag = _run_cli(
        [
            "--agent-json",
            str(agent_path),
            "--out",
            str(tmp_path / "result.json"),
            "--db",
            _db_path(isolated_db),
            "--receipt-id",
            str(receipt.id),
            "--write-db",
            "--mock",
        ],
        env=_cli_env(AI_AGENT_DB_WRITE_ENABLED="false"),
        check=False,
    )
    assert without_flag.returncode != 0
    assert "AI_AGENT_DB_WRITE_ENABLED" in without_flag.stderr
