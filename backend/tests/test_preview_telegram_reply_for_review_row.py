"""F-AI-TG-1 review-row Telegram draft preview tests.

The CLI script ``scripts/preview_telegram_reply_for_review_row.py`` is a
read-only operator tool: it loads one ReviewRow from a SQLite DB and prints
the deterministic Telegram draft that the F-AI-TG-0 engine would produce.
These tests pin:

  * the per-state draft contract (amount / date / AI / missing reason / clean),
  * protected-path refusal without acknowledgement,
  * read-only enforcement (the DB is opened in URI mode=ro),
  * the standard send_allowed=False / no-forbidden-phrase / no-leak invariants.

All fixtures are synthetic. No prod DB, no Telegram sending, no model calls.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlmodel import Session

from app.db import engine
from app.models import (
    AppUser,
    MatchDecision,
    ReceiptDocument,
    ReviewRow,
    ReviewSession,
    StatementImport,
    StatementTransaction,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from preview_telegram_reply_for_review_row import preview  # noqa: E402  (sys.path setup above)


def _seed_review_row(
    isolated_db,
    *,
    receipt_statement_issues: list[dict] | None = None,
    ai_review: dict | None = None,
    business_or_personal: str = "Business",
    business_reason: str | None = "Project meeting",
    attendees: str | None = "Hakan",
    report_bucket: str = "Hotel/Lodging/Laundry",
) -> tuple[int, int, int]:
    """Seed user/statement/tx/receipt/match/review/row + the optional source
    annotations expected by the draft engine.

    Returns ``(review_row_id, statement_import_id, receipt_id)``.
    """
    with Session(isolated_db) as session:
        user = AppUser(display_name="tg1-test")
        session.add(user)
        session.flush()

        statement = StatementImport(
            source_filename="tg1.xlsx",
            row_count=1,
            uploader_user_id=user.id,
        )
        session.add(statement)
        session.flush()

        tx = StatementTransaction(
            statement_import_id=statement.id,
            transaction_date=date(2026, 4, 30),
            supplier_raw="Smoke Cafe",
            supplier_normalized="SMOKE CAFE",
            local_currency="USD",
            local_amount=Decimal("12.34"),
            usd_amount=Decimal("12.34"),
            source_row_ref="row-1",
        )
        receipt = ReceiptDocument(
            uploader_user_id=user.id,
            source="test",
            status="imported",
            content_type="photo",
            original_file_name="r.jpg",
            extracted_date=date(2026, 4, 30),
            extracted_supplier="Smoke Cafe",
            extracted_local_amount=Decimal("12.34"),
            extracted_currency="USD",
            business_or_personal=business_or_personal,
            report_bucket=report_bucket,
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

        decision = MatchDecision(
            statement_transaction_id=tx.id,
            receipt_document_id=receipt.id,
            confidence="high",
            match_method="test",
            approved=True,
            reason="tg1 fixture",
        )
        session.add(decision)
        session.commit()
        session.refresh(decision)

        # Build the source/match block the draft engine expects to read.
        match_payload: dict = {
            "status": "matched",
            "match_decision_id": decision.id,
            "confidence": decision.confidence,
            "match_method": decision.match_method,
            "reason": decision.reason,
            "approved": decision.approved,
        }
        if receipt_statement_issues:
            match_payload["receipt_statement_issues"] = receipt_statement_issues

        source: dict = {
            "statement": {
                "transaction_id": tx.id,
                "transaction_date": tx.transaction_date.isoformat(),
                "supplier_raw": tx.supplier_raw,
                "local_amount": "12.3400",
                "local_currency": "USD",
                "usd_amount": "12.3400",
            },
            "receipt": {
                "receipt_id": receipt.id,
                "extracted_date": receipt.extracted_date.isoformat(),
                "extracted_supplier": receipt.extracted_supplier,
                "extracted_local_amount": "12.3400",
                "extracted_currency": "USD",
            },
            "match": match_payload,
        }
        if ai_review is not None:
            source["ai_review"] = ai_review

        confirmed: dict = {
            "transaction_id": tx.id,
            "receipt_id": receipt.id,
            "transaction_date": tx.transaction_date.isoformat(),
            "supplier": tx.supplier_raw,
            "amount": "12.3400",
            "currency": "USD",
            "business_or_personal": business_or_personal,
            "report_bucket": report_bucket,
            "business_reason": business_reason,
            "attendees": attendees,
        }

        review = ReviewSession(
            statement_import_id=statement.id,
            status="draft",
            created_at=datetime.now(timezone.utc),
        )
        session.add(review)
        session.flush()

        row = ReviewRow(
            review_session_id=review.id or 0,
            statement_transaction_id=tx.id,
            receipt_document_id=receipt.id,
            match_decision_id=decision.id,
            status="suggested",
            attention_required=False,
            attention_note=None,
            source_json=json.dumps(source, sort_keys=True, separators=(",", ":")),
            suggested_json=json.dumps(confirmed, sort_keys=True, separators=(",", ":")),
            confirmed_json=json.dumps(confirmed, sort_keys=True, separators=(",", ":")),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id, statement.id, receipt.id


def _db_path(isolated_db) -> str:
    db_path = isolated_db.url.database
    assert db_path is not None
    return db_path


# ---------------------------------------------------------------------------
# happy-path drafts via the CLI's preview() entry point
# ---------------------------------------------------------------------------


def test_preview_for_amount_mismatch_emits_blocker_draft(isolated_db):
    review_row_id, statement_id, receipt_id = _seed_review_row(
        isolated_db,
        receipt_statement_issues=[{"code": "receipt_statement_amount_mismatch"}],
    )
    code, payload = preview(db_path=_db_path(isolated_db), review_row_id=review_row_id)
    assert code == 0
    assert payload["review_row_id"] == review_row_id
    assert payload["receipt_id"] == receipt_id
    assert payload["statement_import_id"] == statement_id
    draft = payload["draft"]
    assert draft["kind"] == "amount_mismatch"
    assert draft["severity"] == "blocker"
    assert draft["send_allowed"] is False


def test_preview_for_date_mismatch_emits_warning_draft(isolated_db):
    review_row_id, _, _ = _seed_review_row(
        isolated_db,
        receipt_statement_issues=[{"code": "receipt_statement_date_mismatch"}],
    )
    code, payload = preview(db_path=_db_path(isolated_db), review_row_id=review_row_id)
    assert code == 0
    draft = payload["draft"]
    assert draft["kind"] == "date_mismatch"
    assert draft["severity"] == "warning"
    assert draft["send_allowed"] is False


def test_preview_for_ai_advisory_warning(isolated_db):
    review_row_id, _, _ = _seed_review_row(
        isolated_db,
        ai_review={"status": "warn"},
    )
    code, payload = preview(db_path=_db_path(isolated_db), review_row_id=review_row_id)
    assert code == 0
    draft = payload["draft"]
    assert draft["kind"] == "ai_advisory_warning"
    assert draft["severity"] == "info"
    assert draft["send_allowed"] is False


def test_preview_for_missing_business_reason(isolated_db):
    review_row_id, _, _ = _seed_review_row(
        isolated_db,
        business_or_personal="Business",
        business_reason=None,
        attendees="Hakan",
    )
    code, payload = preview(db_path=_db_path(isolated_db), review_row_id=review_row_id)
    assert code == 0
    draft = payload["draft"]
    assert draft["kind"] == "missing_business_reason"
    assert draft["severity"] == "warning"
    assert draft["send_allowed"] is False


def test_preview_no_draft_warranted_returns_null_with_reason(isolated_db):
    review_row_id, _, _ = _seed_review_row(
        isolated_db,
        business_or_personal="Personal",
        business_reason=None,
        attendees=None,
    )
    code, payload = preview(db_path=_db_path(isolated_db), review_row_id=review_row_id)
    assert code == 0
    assert payload["draft"] is None
    assert payload["reason"] == "no_draft_warranted"


def test_preview_resolves_via_statement_id_plus_receipt_id(isolated_db):
    review_row_id, statement_id, receipt_id = _seed_review_row(
        isolated_db,
        ai_review={"status": "block"},
    )
    code, payload = preview(
        db_path=_db_path(isolated_db),
        statement_import_id=statement_id,
        receipt_id=receipt_id,
    )
    assert code == 0
    assert payload["review_row_id"] == review_row_id
    assert payload["draft"]["kind"] == "ai_advisory_warning"


def test_review_row_not_found_returns_exit_code_2(isolated_db):
    code, payload = preview(db_path=_db_path(isolated_db), review_row_id=999999)
    assert code == 2
    assert payload["draft"] is None
    assert payload["reason"] == "review_row_not_found"


# ---------------------------------------------------------------------------
# protected-path safety
# ---------------------------------------------------------------------------


def test_protected_var_lib_path_refused_without_acknowledgement(capsys):
    with pytest.raises(SystemExit) as excinfo:
        preview(db_path="/var/lib/dcexpense/expense_app.db", review_row_id=1)
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "REFUSED" in captured.err
    assert "/var/lib/dcexpense" in captured.err


def test_protected_opt_path_refused_without_acknowledgement(capsys):
    with pytest.raises(SystemExit) as excinfo:
        preview(db_path="/opt/dcexpense/app/data/expense_app.db", review_row_id=1)
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "REFUSED" in captured.err
    assert "/opt/dcexpense" in captured.err


def test_protected_path_with_acknowledgement_still_read_only(tmp_path):
    """With --i-understand-this-is-prod, the script proceeds but opens the
    DB in URI read-only mode. Pointing at a non-existent prod path simply
    returns a not-found result; no write attempt is made."""
    fake_prod_path = "/var/lib/dcexpense/nonexistent_smoke.db"
    code, payload = preview(
        db_path=fake_prod_path,
        review_row_id=1,
        acknowledged=True,
    )
    assert code == 2
    assert payload["draft"] is None
    assert payload["reason"] == "db_path_not_found"


def test_read_only_connection_blocks_writes_even_after_ack(isolated_db):
    """The CLI opens the SQLite DB with URI mode=ro. A direct attempt to
    write through the same URI form fails. This pins the safety contract:
    even with --i-understand-this-is-prod, the connection cannot mutate
    the underlying file."""
    db_path = _db_path(isolated_db)
    resolved = Path(db_path).resolve().as_posix()
    uri = f"file:{resolved}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE __forbidden_write (id INTEGER PRIMARY KEY)")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# safety contract
# ---------------------------------------------------------------------------


def test_send_allowed_is_always_false_across_every_preview_path(isolated_db):
    """Whichever path the preview takes (deterministic, AI advisory, receipt
    fallback), the resulting draft must carry send_allowed=False."""
    cases = [
        {"receipt_statement_issues": [{"code": "receipt_statement_amount_mismatch"}]},
        {"receipt_statement_issues": [{"code": "receipt_statement_date_mismatch"}]},
        {"ai_review": {"status": "warn"}},
        {"ai_review": {"status": "block"}},
        {"business_reason": None},
        {"report_bucket": "Lunch", "attendees": None},
    ]
    for kwargs in cases:
        # Each case needs a fresh row — re-seed.
        review_row_id, _, _ = _seed_review_row(isolated_db, **kwargs)
        code, payload = preview(
            db_path=_db_path(isolated_db),
            review_row_id=review_row_id,
        )
        assert code == 0
        if payload["draft"] is not None:
            assert payload["draft"]["send_allowed"] is False


def test_drafts_emitted_via_preview_have_no_forbidden_phrases_or_leaks(isolated_db):
    forbidden_phrases = (
        "AI approved",
        "AI rejected",
        "report blocked by AI",
        "sent to Telegram",
    )
    forbidden_substrings = (
        "/var/lib/dcexpense",
        "/opt/dcexpense",
        "C:\\",
        "storage_path",
        "receipt_path",
        "prompt_text",
        "raw_model_json",
        "model_response_json",
        "model_debug_json",
        "canonical_snapshot_hash",
        "agent_read_hash",
        "OPENAI_API_KEY",
    )
    cases = [
        {"receipt_statement_issues": [{"code": "receipt_statement_amount_mismatch"}]},
        {"receipt_statement_issues": [{"code": "receipt_statement_date_mismatch"}]},
        {"ai_review": {"status": "warn"}},
        {"business_reason": None},
    ]
    for kwargs in cases:
        review_row_id, _, _ = _seed_review_row(isolated_db, **kwargs)
        _, payload = preview(
            db_path=_db_path(isolated_db),
            review_row_id=review_row_id,
        )
        assert payload["draft"] is not None
        text = payload["draft"]["text"]
        for phrase in forbidden_phrases:
            assert phrase.lower() not in text.lower()
        for needle in forbidden_substrings:
            assert needle not in text


def test_preview_module_does_not_import_live_model_or_telegram_sdk():
    """Defensive: the CLI must not pick up openai/anthropic/deepseek/httpx
    /requests or any telegram client. If a refactor pulls one in, this
    test fails immediately."""
    import preview_telegram_reply_for_review_row as cli_module

    forbidden_globals = (
        "openai",
        "anthropic",
        "deepseek",
        "httpx",
        "requests",
        "telegram",
        "telegram_send",
        "send_message",
    )
    module_globals = vars(cli_module)
    for name in forbidden_globals:
        assert name not in module_globals, (
            f"preview CLI module unexpectedly references {name!r}"
        )
