from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlmodel import Session, select

from app.db import engine
from app.models import AgentReceiptComparison, AgentReceiptRead, AgentReceiptReviewRun, ReviewRow
from app.services import agent_receipt_live_provider
from app.services.review_sessions import session_payload
from test_run_agent_receipt_review_shadow import (
    _agent_counts,
    _canonical_counts,
    _db_path,
    _payload_for_review,
    _seed_matched_review_row,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from run_agent_receipt_review_shadow import main as shadow_main  # noqa: E402


def _run_shadow(*args: str, capsys) -> tuple[int, dict | None, str]:
    rc = shadow_main(list(args))
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip() else None
    return rc, payload, captured.err


def _review_row_id_for_receipt(session: Session, receipt_id: int) -> int:
    row = session.exec(select(ReviewRow).where(ReviewRow.receipt_document_id == receipt_id)).one()
    assert row.id is not None
    return row.id


def _fake_live_result(
    *,
    amount: str = "203.5000",
    currency: str = "TRY",
    date: str = "2025-12-28",
    supplier: str = "A101",
) -> agent_receipt_live_provider.LiveAgentReceiptReviewResult:
    return agent_receipt_live_provider.LiveAgentReceiptReviewResult(
        agent_payload={
            "merchant_name": supplier,
            "merchant_address": None,
            "receipt_date": date,
            "receipt_time": None,
            "total_amount": amount,
            "currency": currency,
            "amount_text": amount,
            "line_items": [],
            "tax_amount": None,
            "payment_method": None,
            "receipt_category": "payment_receipt",
            "confidence": 0.92,
            "raw_text_summary": "mocked live provider result",
        },
        raw_response_json=json.dumps(
            {
                "date": date,
                "amount": amount,
                "currency": currency,
                "supplier": supplier,
                "business_reason": None,
                "attendees": None,
                "notes": "mocked live provider result",
            }
        ),
        prompt_text="strict live prompt",
        model_name="gpt-live-test",
    )


def test_live_provider_refuses_without_explicit_live_ack(isolated_db, capsys):
    with Session(engine) as session:
        receipt_id, _ = _seed_matched_review_row(session)

    rc, payload, err = _run_shadow(
        "--db-path",
        _db_path(),
        "--receipt-id",
        str(receipt_id),
        "--provider",
        "live",
        "--yes",
        capsys=capsys,
    )

    assert rc == 2
    assert payload is None
    assert "--i-understand-live-model-call" in err


def test_live_dry_run_does_not_call_model_or_write(isolated_db, monkeypatch, capsys):
    calls: list[object] = []

    def fail_if_called(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("live provider must not be called during dry-run")

    monkeypatch.setattr(agent_receipt_live_provider, "call_live_agent_receipt_review", fail_if_called)
    with Session(engine) as session:
        receipt_id, _ = _seed_matched_review_row(session)
        before = _agent_counts(session)

    rc, payload, err = _run_shadow(
        "--db-path",
        _db_path(),
        "--receipt-id",
        str(receipt_id),
        "--provider",
        "live",
        "--dry-run",
        capsys=capsys,
    )

    with Session(engine) as session:
        after = _agent_counts(session)
    assert rc == 0
    assert err == ""
    assert payload is not None
    assert payload["provider"] == "live"
    assert payload["dry_run"] is True
    assert payload["model_call_made"] is False
    assert payload["would_write"] is True
    assert calls == []
    assert before == after == (0, 0, 0)


def test_live_missing_yes_does_not_call_model_or_write(isolated_db, monkeypatch, capsys):
    calls: list[object] = []

    def fail_if_called(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("live provider must not be called without --yes")

    monkeypatch.setattr(agent_receipt_live_provider, "call_live_agent_receipt_review", fail_if_called)
    with Session(engine) as session:
        receipt_id, _ = _seed_matched_review_row(session)
        before = _agent_counts(session)

    rc, payload, err = _run_shadow(
        "--db-path",
        _db_path(),
        "--receipt-id",
        str(receipt_id),
        "--provider",
        "live",
        "--i-understand-live-model-call",
        capsys=capsys,
    )

    with Session(engine) as session:
        after = _agent_counts(session)
    assert rc == 0
    assert err == ""
    assert payload is not None
    assert payload["model_call_made"] is False
    assert payload["would_write"] is True
    assert calls == []
    assert before == after == (0, 0, 0)


def test_live_missing_api_key_returns_clear_error(isolated_db, monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with Session(engine) as session:
        receipt_id, _ = _seed_matched_review_row(session)

    rc, payload, err = _run_shadow(
        "--db-path",
        _db_path(),
        "--receipt-id",
        str(receipt_id),
        "--provider",
        "live",
        "--i-understand-live-model-call",
        "--yes",
        capsys=capsys,
    )

    with Session(engine) as session:
        assert _agent_counts(session) == (0, 0, 0)
    assert rc == 2
    assert payload is None
    assert "OPENAI_API_KEY" in err


def test_mocked_live_pass_writes_pass_with_review_row_context(isolated_db, monkeypatch, capsys):
    captured: dict[str, object] = {}

    def fake_live(**kwargs):
        captured.update(kwargs)
        return _fake_live_result()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(agent_receipt_live_provider, "call_live_agent_receipt_review", fake_live)
    with Session(engine) as session:
        receipt_id, _ = _seed_matched_review_row(session)
        review_row_id = _review_row_id_for_receipt(session, receipt_id)

    rc, payload, err = _run_shadow(
        "--db-path",
        _db_path(),
        "--review-row-id",
        str(review_row_id),
        "--provider",
        "live",
        "--i-understand-live-model-call",
        "--yes",
        capsys=capsys,
    )

    with Session(engine) as session:
        run = session.exec(select(AgentReceiptReviewRun)).one()
        comparison = session.exec(select(AgentReceiptComparison)).one()
        assert run.model_provider == "openai"
        assert run.model_name == "gpt-live-test"
        assert run.review_row_id == review_row_id
        assert run.statement_transaction_id is not None
        assert run.statement_snapshot_json is not None
        assert comparison.risk_level == "pass"
        assert json.loads(comparison.differences_json) == []
    assert rc == 0
    assert err == ""
    assert payload is not None
    assert payload["provider"] == "live"
    assert payload["review_row_id"] == review_row_id
    assert payload["risk_level"] == "pass"
    assert payload["differences"] == []
    assert payload["model_call_made"] is True
    assert payload["canonical_mutation"] is False
    assert captured["statement_context"]["amount"] == "203.5000"


def test_mocked_live_warn_writes_date_and_supplier_difference(isolated_db, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        agent_receipt_live_provider,
        "call_live_agent_receipt_review",
        lambda **kwargs: _fake_live_result(date="2025-12-25", supplier="TRANSIT CUP"),
    )
    with Session(engine) as session:
        receipt_id, _ = _seed_matched_review_row(session)

    rc, payload, _ = _run_shadow(
        "--db-path",
        _db_path(),
        "--receipt-id",
        str(receipt_id),
        "--provider",
        "live",
        "--i-understand-live-model-call",
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
    assert payload is not None
    assert payload["risk_level"] == "warn"
    assert "date_mismatch" in payload["differences"]
    assert "supplier_mismatch" in payload["differences"]


def test_mocked_live_block_writes_amount_and_currency_difference(isolated_db, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        agent_receipt_live_provider,
        "call_live_agent_receipt_review",
        lambda **kwargs: _fake_live_result(amount="999.99", currency="EUR"),
    )
    with Session(engine) as session:
        receipt_id, _ = _seed_matched_review_row(session)

    rc, payload, _ = _run_shadow(
        "--db-path",
        _db_path(),
        "--receipt-id",
        str(receipt_id),
        "--provider",
        "live",
        "--i-understand-live-model-call",
        "--yes",
        capsys=capsys,
    )

    with Session(engine) as session:
        comparison = session.exec(select(AgentReceiptComparison)).one()
        assert comparison.risk_level == "block"
        assert json.loads(comparison.differences_json) == ["amount_mismatch", "currency_mismatch"]
    assert rc == 0
    assert payload is not None
    assert payload["risk_level"] == "block"
    assert payload["differences"] == ["amount_mismatch", "currency_mismatch"]


def test_malformed_live_json_writes_failed_run_and_malformed_public_status(
    isolated_db, monkeypatch, capsys
):
    def malformed(**kwargs):
        raise agent_receipt_live_provider.LiveAgentReceiptMalformedResponse(
            "not json",
            prompt_text="strict live prompt",
            model_name="gpt-live-test",
            message="model response was not valid JSON",
        )

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(agent_receipt_live_provider, "call_live_agent_receipt_review", malformed)
    with Session(engine) as session:
        receipt_id, review_id = _seed_matched_review_row(session)

    rc, payload, err = _run_shadow(
        "--db-path",
        _db_path(),
        "--receipt-id",
        str(receipt_id),
        "--provider",
        "live",
        "--i-understand-live-model-call",
        "--yes",
        capsys=capsys,
    )

    with Session(engine) as session:
        run = session.exec(select(AgentReceiptReviewRun)).one()
        assert run.status == "failed"
        assert run.model_provider == "openai"
        assert run.model_name == "gpt-live-test"
        assert run.raw_model_json is None
        assert run.prompt_text is None
        assert _agent_counts(session) == (1, 0, 0)
        row = _payload_for_review(session, review_id)
        assert row["source"]["ai_review"]["status"] == "malformed"
    assert rc == 0
    assert err == ""
    assert payload is not None
    assert payload["risk_level"] == "malformed"
    assert payload["model_call_made"] is True
    assert payload["canonical_mutation"] is False


def test_written_live_result_appears_through_source_ai_review(isolated_db, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        agent_receipt_live_provider,
        "call_live_agent_receipt_review",
        lambda **kwargs: _fake_live_result(amount="999.99"),
    )
    with Session(engine) as session:
        receipt_id, review_id = _seed_matched_review_row(session)
        review_row_id = _review_row_id_for_receipt(session, receipt_id)

    rc, payload, _ = _run_shadow(
        "--db-path",
        _db_path(),
        "--review-row-id",
        str(review_row_id),
        "--provider",
        "live",
        "--i-understand-live-model-call",
        "--yes",
        capsys=capsys,
    )

    with Session(engine) as session:
        row = _payload_for_review(session, review_id)
        ai = row["source"]["ai_review"]
        assert ai["status"] == "block"
        assert ai["risk_level"] == "block"
        assert row["attention_required"] is False
    assert rc == 0
    assert payload is not None
    assert payload["comparison_id"]


def test_live_run_does_not_change_canonical_tables(isolated_db, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        agent_receipt_live_provider,
        "call_live_agent_receipt_review",
        lambda **kwargs: _fake_live_result(amount="999.99"),
    )
    with Session(engine) as session:
        receipt_id, _ = _seed_matched_review_row(session)
        before = _canonical_counts(session)

    rc, _, _ = _run_shadow(
        "--db-path",
        _db_path(),
        "--receipt-id",
        str(receipt_id),
        "--provider",
        "live",
        "--i-understand-live-model-call",
        "--yes",
        capsys=capsys,
    )

    with Session(engine) as session:
        assert _canonical_counts(session) == before
    assert rc == 0


def test_live_public_payload_has_no_private_or_debug_fields(isolated_db, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        agent_receipt_live_provider,
        "call_live_agent_receipt_review",
        lambda **kwargs: _fake_live_result(amount="999.99"),
    )
    with Session(engine) as session:
        receipt_id, review_id = _seed_matched_review_row(session)

    rc, _, _ = _run_shadow(
        "--db-path",
        _db_path(),
        "--receipt-id",
        str(receipt_id),
        "--provider",
        "live",
        "--i-understand-live-model-call",
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


def test_live_runner_does_not_import_telegram_or_ocr_router_when_provider_is_mocked(
    isolated_db, monkeypatch, capsys
):
    before = set(sys.modules)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        agent_receipt_live_provider,
        "call_live_agent_receipt_review",
        lambda **kwargs: _fake_live_result(),
    )
    with Session(engine) as session:
        receipt_id, _ = _seed_matched_review_row(session)

    rc, _, _ = _run_shadow(
        "--db-path",
        _db_path(),
        "--receipt-id",
        str(receipt_id),
        "--provider",
        "live",
        "--i-understand-live-model-call",
        "--yes",
        capsys=capsys,
    )

    newly_loaded = set(sys.modules) - before
    forbidden_prefixes = (
        "app.services.telegram",
        "telegram",
        "app.services.model_router",
        "openai",
        "anthropic",
        "deepseek",
    )
    assert rc == 0
    assert not any(
        module == prefix or module.startswith(f"{prefix}.")
        for module in newly_loaded
        for prefix in forbidden_prefixes
    )
