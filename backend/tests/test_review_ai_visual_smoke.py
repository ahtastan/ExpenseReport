"""F-AI-0b-3 end-to-end visual smoke for the AI second-read display.

These tests exercise the actual FastAPI routes (not just the service layer)
and the static review HTML, asserting that:

  * /reviews/report/{statement_id} returns an ai_review block in the expected
    shape per state (pass / warn / block / stale / malformed),
  * /review returns the static React bundle with all advisory copy and
    badge components present,
  * the empty-AgentDB baseline is intact -- no ai_review key, no UI break.

The tests reuse the production-only ``scripts/seed_ai_review_smoke.py``
helpers, which the operator can also run by hand from PowerShell.

No live model calls. No prod DB. No AI flag toggling.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine
from app.main import app
from app.models import (
    AppUser,
    MatchDecision,
    ReceiptDocument,
    StatementImport,
    StatementTransaction,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from seed_ai_review_smoke import seed_ai_review_smoke  # noqa: E402  (path setup above)


@pytest.fixture
def client(isolated_db):
    with TestClient(app) as test_client:
        yield test_client


def _ai_review_for_state(client: TestClient, isolated_db, *, state: str) -> dict:
    db_path = engine.url.database
    assert db_path is not None
    seeded = seed_ai_review_smoke(db_path, state=state)
    response = client.get(f"/reviews/report/{seeded['statement_import_id']}")
    assert response.status_code == 200, response.text
    rows = response.json()["rows"]
    assert len(rows) == 1, rows
    return rows[0]


def test_ai_review_pass_state_renders_minimal_advisory_block(client, isolated_db):
    row = _ai_review_for_state(client, isolated_db, state="pass")
    ai = row["source"]["ai_review"]
    assert ai["status"] == "pass"
    assert ai["risk_level"] == "pass"
    assert ai["label"] == "AI second read"
    assert ai["recommended_action"] == "accept"
    # Pass: no differences, no summary, no suggested_user_message leakage.
    assert "differences" not in ai
    assert "summary" not in ai
    # Advisory only: never sets attention_required.
    assert row["attention_required"] is False


def test_ai_review_warn_state_includes_rich_differences_and_summary(client, isolated_db):
    row = _ai_review_for_state(client, isolated_db, state="warn")
    ai = row["source"]["ai_review"]
    assert ai["status"] == "warn"
    assert ai["risk_level"] == "warn"
    assert ai["recommended_action"] == "review"  # manual_review -> public review
    assert "Date and supplier appear to differ" in ai["summary"]
    codes = [d["code"] for d in ai["differences"]]
    assert "date_mismatch" in codes
    assert "supplier_mismatch" in codes
    for diff in ai["differences"]:
        assert "field" in diff
        assert "severity" in diff
    # Advisory-only: no attention_required, no validation issue, no confirm block.
    assert row["attention_required"] is False


def test_ai_review_block_state_is_advisory_not_a_real_blocker(client, isolated_db):
    row = _ai_review_for_state(client, isolated_db, state="block")
    ai = row["source"]["ai_review"]
    assert ai["status"] == "block"
    assert ai["risk_level"] == "block"
    assert ai["recommended_action"] == "block_report"
    diff = ai["differences"][0]
    assert diff["code"] == "amount_mismatch"
    assert diff["severity"] == "block"
    assert diff["agent_value"] == "999.99"
    # CRITICAL: AI block must NOT propagate to attention_required.
    assert row["attention_required"] is False
    # The advisory-only invariant for confirmation/readiness is pinned by
    # test_review_session_ai_review_does_not_block.py. Here we only assert
    # that the rendered row stays advisory at the API surface.


def test_ai_review_stale_state_strips_unsafe_fields(client, isolated_db):
    row = _ai_review_for_state(client, isolated_db, state="stale")
    ai = row["source"]["ai_review"]
    assert ai["status"] == "stale"
    assert ai["label"] == "AI second read"
    # Stale must NOT expose risk_level / differences / summary / agent_read /
    # canonical, because the comparison no longer reflects current canonical.
    for forbidden in ("risk_level", "differences", "summary", "agent_read", "canonical"):
        assert forbidden not in ai


def test_ai_review_malformed_state_keeps_status_only(client, isolated_db):
    row = _ai_review_for_state(client, isolated_db, state="malformed")
    ai = row["source"]["ai_review"]
    assert ai["status"] == "malformed"
    assert "risk_level" not in ai
    assert "differences" not in ai
    assert "summary" not in ai


def test_review_html_page_returns_200_and_contains_advisory_markers(client, isolated_db):
    response = client.get("/review")
    assert response.status_code == 200
    body = response.text
    # The static bundle must include advisory copy and the badge wiring.
    for marker in (
        "AiReviewBadge",
        "AiReviewDifferencesPanel",
        "AI second read: pass",
        "AI second read: warning",
        "AI second read: block (advisory)",
        "AI second read: stale",
        "AI second read unavailable",
        "AI second read is advisory only",
        "src.ai_review",
    ):
        assert marker in body, f"expected marker {marker!r} on /review"
    # No copy that would imply a real blocker.
    assert "Report blocked by AI" not in body
    assert "AI rejected" not in body


def test_empty_agentdb_does_not_emit_ai_review(client, isolated_db):
    """Baseline: with no AgentDB rows, the queue still loads and contains no
    ai_review key. This is the actual production state today on prod.
    """
    with Session(isolated_db) as session:
        user = AppUser(display_name="empty-agent-baseline")
        session.add(user)
        session.flush()
        statement = StatementImport(
            source_filename="empty_agent_baseline.xlsx",
            row_count=1,
            uploader_user_id=user.id,
        )
        session.add(statement)
        session.flush()
        from datetime import date
        from decimal import Decimal

        tx = StatementTransaction(
            statement_import_id=statement.id,
            transaction_date=date(2026, 4, 30),
            supplier_raw="Plain Vendor",
            supplier_normalized="PLAIN VENDOR",
            local_currency="USD",
            local_amount=Decimal("9.99"),
            usd_amount=Decimal("9.99"),
            source_row_ref="row-1",
        )
        receipt = ReceiptDocument(
            uploader_user_id=user.id,
            source="test",
            status="imported",
            content_type="photo",
            original_file_name="plain_receipt.jpg",
            extracted_date=date(2026, 4, 30),
            extracted_supplier="Plain Vendor",
            extracted_local_amount=Decimal("9.99"),
            extracted_currency="USD",
            business_or_personal="Business",
            report_bucket="Meals/Snacks",
            business_reason="baseline",
            attendees="Hakan",
            needs_clarification=False,
        )
        session.add(tx)
        session.add(receipt)
        session.commit()
        session.refresh(statement)
        session.refresh(tx)
        session.refresh(receipt)
        session.add(
            MatchDecision(
                statement_transaction_id=tx.id,
                receipt_document_id=receipt.id,
                confidence="high",
                match_method="test",
                approved=True,
                reason="baseline",
            )
        )
        session.commit()

        statement_id = statement.id

    response = client.get(f"/reviews/report/{statement_id}")
    assert response.status_code == 200, response.text
    rows = response.json()["rows"]
    assert len(rows) == 1
    assert "ai_review" not in rows[0]["source"]
    assert rows[0]["attention_required"] is False
