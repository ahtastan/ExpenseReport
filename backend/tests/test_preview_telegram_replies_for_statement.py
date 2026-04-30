"""F-AI-TG-3 statement-batch Telegram draft preview tests.

Pin the contract that the new CLI:

  * iterates every ReviewRow under a single ``statement_import_id``,
  * emits the same per-row draft envelope as F-AI-TG-1,
  * filters clean rows when ``--only-with-drafts`` is set,
  * refuses production paths without acknowledgement, and
  * cannot mutate the underlying DB (URI ``mode=ro``).

All fixtures synthetic. No live model calls. No Telegram client. No prod DB.
"""

from __future__ import annotations

import json
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

from preview_telegram_replies_for_statement import preview_statement  # noqa: E402  (sys.path setup above)


def _seed_statement_with_rows(
    isolated_db,
    *,
    row_specs: list[dict],
) -> tuple[int, list[int]]:
    """Seed one statement import with one review session and N review rows.

    Each ``row_specs`` entry is a dict with optional keys:
      receipt_statement_issues  -> list[dict] for source.match
      ai_review                 -> dict for source.ai_review
      business_or_personal      -> "Business"|"Personal"
      business_reason           -> str|None
      attendees                 -> str|None
      report_bucket             -> str

    Returns ``(statement_import_id, [review_row_id, ...])``.
    """
    review_row_ids: list[int] = []
    with Session(isolated_db) as session:
        user = AppUser(display_name="tg3-test")
        session.add(user)
        session.flush()
        statement = StatementImport(
            source_filename="tg3.xlsx",
            row_count=len(row_specs),
            uploader_user_id=user.id,
        )
        session.add(statement)
        session.flush()
        review = ReviewSession(
            statement_import_id=statement.id,
            status="draft",
            created_at=datetime.now(timezone.utc),
        )
        session.add(review)
        session.flush()

        for index, spec in enumerate(row_specs):
            tx = StatementTransaction(
                statement_import_id=statement.id,
                transaction_date=date(2026, 4, 30),
                supplier_raw=f"Smoke {index}",
                supplier_normalized=f"SMOKE {index}",
                local_currency="USD",
                local_amount=Decimal("12.34"),
                usd_amount=Decimal("12.34"),
                source_row_ref=f"row-{index}",
            )
            receipt = ReceiptDocument(
                uploader_user_id=user.id,
                source="test",
                status="imported",
                content_type="photo",
                original_file_name=f"r{index}.jpg",
                extracted_date=date(2026, 4, 30),
                extracted_supplier=f"Smoke {index}",
                extracted_local_amount=Decimal("12.34"),
                extracted_currency="USD",
                business_or_personal=spec.get("business_or_personal", "Business"),
                report_bucket=spec.get("report_bucket", "Hotel/Lodging/Laundry"),
                business_reason=spec.get("business_reason", "Project meeting"),
                attendees=spec.get("attendees", "Hakan"),
                needs_clarification=False,
            )
            session.add(tx)
            session.add(receipt)
            session.commit()
            session.refresh(tx)
            session.refresh(receipt)

            decision = MatchDecision(
                statement_transaction_id=tx.id,
                receipt_document_id=receipt.id,
                confidence="high",
                match_method="test",
                approved=True,
                reason="tg3 fixture",
            )
            session.add(decision)
            session.commit()
            session.refresh(decision)

            match_payload: dict = {
                "status": "matched",
                "match_decision_id": decision.id,
                "confidence": decision.confidence,
                "match_method": decision.match_method,
                "reason": decision.reason,
                "approved": decision.approved,
            }
            issues = spec.get("receipt_statement_issues")
            if issues:
                match_payload["receipt_statement_issues"] = issues

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
            ai_review = spec.get("ai_review")
            if ai_review is not None:
                source["ai_review"] = ai_review

            confirmed: dict = {
                "transaction_id": tx.id,
                "receipt_id": receipt.id,
                "transaction_date": tx.transaction_date.isoformat(),
                "supplier": tx.supplier_raw,
                "amount": "12.3400",
                "currency": "USD",
                "business_or_personal": spec.get("business_or_personal", "Business"),
                "report_bucket": spec.get("report_bucket", "Hotel/Lodging/Laundry"),
                "business_reason": spec.get("business_reason", "Project meeting"),
                "attendees": spec.get("attendees", "Hakan"),
            }

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
            review_row_ids.append(row.id)

        return statement.id, review_row_ids


def _db_path(isolated_db) -> str:
    db_path = isolated_db.url.database
    assert db_path is not None
    return db_path


# ---------------------------------------------------------------------------
# happy-path coverage
# ---------------------------------------------------------------------------


def test_preview_returns_every_row_for_a_statement_import(isolated_db):
    statement_id, row_ids = _seed_statement_with_rows(
        isolated_db,
        row_specs=[
            {"receipt_statement_issues": [{"code": "receipt_statement_amount_mismatch"}]},
            {"ai_review": {"status": "warn"}},
            {},  # clean row
        ],
    )
    code, payload = preview_statement(
        db_path=_db_path(isolated_db),
        statement_import_id=statement_id,
    )
    assert code == 0
    assert payload["statement_import_id"] == statement_id
    assert payload["row_count"] == 3
    assert payload["draft_count"] == 2
    returned_row_ids = [entry["review_row_id"] for entry in payload["rows"]]
    assert returned_row_ids == row_ids

    by_id = {entry["review_row_id"]: entry for entry in payload["rows"]}
    assert by_id[row_ids[0]]["draft"]["kind"] == "amount_mismatch"
    assert by_id[row_ids[0]]["draft"]["severity"] == "blocker"
    assert by_id[row_ids[1]]["draft"]["kind"] == "ai_advisory_warning"
    assert by_id[row_ids[1]]["draft"]["severity"] == "info"
    assert by_id[row_ids[2]]["draft"] is None


def test_only_with_drafts_filters_out_clean_rows(isolated_db):
    statement_id, row_ids = _seed_statement_with_rows(
        isolated_db,
        row_specs=[
            {"receipt_statement_issues": [{"code": "receipt_statement_date_mismatch"}]},
            {},
            {},
            {"business_reason": None},
        ],
    )
    code, payload = preview_statement(
        db_path=_db_path(isolated_db),
        statement_import_id=statement_id,
        only_with_drafts=True,
    )
    assert code == 0
    assert payload["row_count"] == 4
    assert payload["draft_count"] == 2
    returned_row_ids = [entry["review_row_id"] for entry in payload["rows"]]
    assert returned_row_ids == [row_ids[0], row_ids[3]]
    kinds = [entry["draft"]["kind"] for entry in payload["rows"]]
    assert kinds == ["date_mismatch", "missing_business_reason"]


def test_amount_mismatch_row_emits_amount_blocker_draft(isolated_db):
    statement_id, _ = _seed_statement_with_rows(
        isolated_db,
        row_specs=[
            {"receipt_statement_issues": [{"code": "receipt_statement_amount_mismatch"}]},
        ],
    )
    code, payload = preview_statement(
        db_path=_db_path(isolated_db),
        statement_import_id=statement_id,
    )
    assert code == 0
    draft = payload["rows"][0]["draft"]
    assert draft["kind"] == "amount_mismatch"
    assert draft["severity"] == "blocker"
    assert draft["send_allowed"] is False


def test_ai_advisory_row_emits_info_draft(isolated_db):
    statement_id, _ = _seed_statement_with_rows(
        isolated_db,
        row_specs=[{"ai_review": {"status": "warn"}}],
    )
    code, payload = preview_statement(
        db_path=_db_path(isolated_db),
        statement_import_id=statement_id,
    )
    assert code == 0
    draft = payload["rows"][0]["draft"]
    assert draft["kind"] == "ai_advisory_warning"
    assert draft["severity"] == "info"
    assert draft["send_allowed"] is False


def test_clean_row_has_null_draft_when_not_filtered(isolated_db):
    statement_id, _ = _seed_statement_with_rows(
        isolated_db,
        row_specs=[{}],
    )
    code, payload = preview_statement(
        db_path=_db_path(isolated_db),
        statement_import_id=statement_id,
    )
    assert code == 0
    assert payload["row_count"] == 1
    assert payload["draft_count"] == 0
    assert payload["rows"][0]["draft"] is None


def test_unknown_statement_id_returns_empty_preview(isolated_db):
    code, payload = preview_statement(
        db_path=_db_path(isolated_db),
        statement_import_id=999999,
    )
    assert code == 0
    assert payload["row_count"] == 0
    assert payload["draft_count"] == 0
    assert payload["rows"] == []


# ---------------------------------------------------------------------------
# safety: send_allowed always false, no forbidden phrases / leaks
# ---------------------------------------------------------------------------


def test_every_emitted_draft_has_send_allowed_false(isolated_db):
    statement_id, _ = _seed_statement_with_rows(
        isolated_db,
        row_specs=[
            {"receipt_statement_issues": [{"code": "receipt_statement_amount_mismatch"}]},
            {"receipt_statement_issues": [{"code": "receipt_statement_date_mismatch"}]},
            {"ai_review": {"status": "warn"}},
            {"ai_review": {"status": "block"}},
            {"business_reason": None},
            {"report_bucket": "Lunch", "attendees": None},
            {},  # clean
        ],
    )
    code, payload = preview_statement(
        db_path=_db_path(isolated_db),
        statement_import_id=statement_id,
    )
    assert code == 0
    for entry in payload["rows"]:
        if entry["draft"] is not None:
            assert entry["draft"]["send_allowed"] is False


def test_drafts_in_batch_have_no_forbidden_phrases_or_leaks(isolated_db):
    forbidden_phrases = (
        "AI approved",
        "AI rejected",
        "report blocked by AI",
        "sent to Telegram",
        "Send Telegram",
        "Send to Telegram",
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
    statement_id, _ = _seed_statement_with_rows(
        isolated_db,
        row_specs=[
            {"receipt_statement_issues": [{"code": "receipt_statement_amount_mismatch"}]},
            {"ai_review": {"status": "warn"}},
            {"business_reason": None},
            {"report_bucket": "Lunch", "attendees": None},
        ],
    )
    code, payload = preview_statement(
        db_path=_db_path(isolated_db),
        statement_import_id=statement_id,
    )
    assert code == 0
    for entry in payload["rows"]:
        if entry["draft"] is None:
            continue
        text_lower = entry["draft"]["text"].lower()
        for phrase in forbidden_phrases:
            assert phrase.lower() not in text_lower, (
                f"forbidden phrase {phrase!r} appeared in draft kind {entry['draft']['kind']!r}"
            )
        for needle in forbidden_substrings:
            assert needle not in entry["draft"]["text"]


# ---------------------------------------------------------------------------
# protected-path safety
# ---------------------------------------------------------------------------


def test_protected_var_lib_path_refused_without_acknowledgement(capsys):
    with pytest.raises(SystemExit) as excinfo:
        preview_statement(
            db_path="/var/lib/dcexpense/expense_app.db",
            statement_import_id=1,
        )
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "REFUSED" in captured.err
    assert "/var/lib/dcexpense" in captured.err


def test_protected_opt_path_refused_without_acknowledgement(capsys):
    with pytest.raises(SystemExit) as excinfo:
        preview_statement(
            db_path="/opt/dcexpense/app/data/expense_app.db",
            statement_import_id=1,
        )
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "REFUSED" in captured.err
    assert "/opt/dcexpense" in captured.err


def test_protected_path_with_acknowledgement_is_still_read_only():
    """With --i-understand-this-is-prod, the script proceeds but cannot mutate
    the source DB. Pointing at a non-existent prod path simply returns a
    not-found result; no write attempt is made."""
    code, payload = preview_statement(
        db_path="/var/lib/dcexpense/nonexistent_smoke.db",
        statement_import_id=1,
        acknowledged=True,
    )
    assert code == 2
    assert payload["row_count"] == 0
    assert payload["rows"] == []
    assert payload["reason"] == "db_path_not_found"


def test_no_db_writes_during_batch_preview(isolated_db):
    """Sanity: the batch preview must not mutate the DB. Snapshot every
    sqlite_master row count for non-system tables before and after, and
    assert equality."""
    statement_id, _ = _seed_statement_with_rows(
        isolated_db,
        row_specs=[
            {"receipt_statement_issues": [{"code": "receipt_statement_amount_mismatch"}]},
            {"ai_review": {"status": "block"}},
            {},
        ],
    )

    db_path = _db_path(isolated_db)

    def _table_counts() -> dict[str, int]:
        with sqlite3.connect(db_path) as conn:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            return {
                t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in tables
            }

    before = _table_counts()
    code, payload = preview_statement(
        db_path=db_path,
        statement_import_id=statement_id,
    )
    assert code == 0
    assert payload["row_count"] == 3
    after = _table_counts()
    assert before == after


def test_module_does_not_import_live_model_or_telegram_sdk():
    import preview_telegram_replies_for_statement as cli_module

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
            f"batch preview CLI module unexpectedly references {name!r}"
        )
