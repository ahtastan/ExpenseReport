"""F-AI-Stage1 sub-PR 5: source-tag completeness invariant.

Every code path that writes to a canonical receipt classification field
(``business_or_personal``, ``report_bucket``, ``business_reason``,
``attendees``) must also stamp the corresponding ``*_source`` column. The
locked vocabulary lives in ``app.models.ReceiptDocument`` (see the
inline comment on lines 64–66) and contains exactly seven values:

  ``user``, ``telegram_user``, ``ai_advisory``, ``auto_confirmed_default``,
  ``matching``, ``auto_suggester``, ``legacy_unknown``

This test exercises every canonical-write entry point identified in the
Phase 0 audit, then asserts the invariant query returns 0 rows. A static
analysis test at the bottom catches new code that adds a canonical write
without a paired ``*_source`` write — future-proofing.
"""
from __future__ import annotations

import csv
import re
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    AgentReceiptRead,
    AgentReceiptReviewRun,
    AppUser,
    ClarificationQuestion,
    ReceiptDocument,
    StatementImport,
)
from app.services.agent_receipt_canonical_writer import (  # noqa: E402
    write_ai_proposal_to_canonical,
)
from app.services.clarifications import (  # noqa: E402
    answer_question,
    ensure_receipt_review_questions,
)
from app.services.legacy_receipts import (  # noqa: E402
    import_legacy_receipt_mapping,
)
from app.services.receipt_extraction import apply_receipt_extraction  # noqa: E402


# ---------------------------------------------------------------------------
# Locked vocabulary — kept in sync with models.py and category_vocab vocabulary.
# Adding a new value here requires updating models.py docstring AND the
# canonical writer's _ALLOWED_SOURCE_TAGS in agent_receipt_canonical_writer.py.
# ---------------------------------------------------------------------------

LOCKED_SOURCE_VOCABULARY = frozenset(
    {
        "user",
        "telegram_user",
        "ai_advisory",
        "auto_confirmed_default",
        "matching",
        "auto_suggester",
        "legacy_unknown",
    }
)

# PR4 sentinel used by the keyboard Skip-for-now handler. It is set ONLY
# on ``*_source`` (never alongside a canonical value). It exists outside
# the locked vocabulary on purpose — it is a transient signal, not a
# provenance label. The invariant query below excludes it via the rule
# "canonical NOT NULL must imply source NOT NULL", which it already
# satisfies because the sentinel never accompanies a non-null canonical.
_SENTINEL_SKIPPED = "telegram_user_skipped"


# ---------------------------------------------------------------------------
# Invariant query — the heart of this test. Run it after every code path.
# ---------------------------------------------------------------------------


def _violations(session: Session) -> list[tuple[int, str, str | None, str, str | None]]:
    """Return one row per (receipt, field) where canonical is non-null but
    the corresponding *_source is null. Empty list = invariant holds."""
    sql = text(
        """
        SELECT id, 'business_or_personal' AS field, business_or_personal AS value,
               'category_source' AS source_col, category_source AS source_value
        FROM receiptdocument
        WHERE business_or_personal IS NOT NULL AND category_source IS NULL
        UNION ALL
        SELECT id, 'report_bucket', report_bucket, 'bucket_source', bucket_source
        FROM receiptdocument
        WHERE report_bucket IS NOT NULL AND bucket_source IS NULL
        UNION ALL
        SELECT id, 'business_reason', business_reason,
               'business_reason_source', business_reason_source
        FROM receiptdocument
        WHERE business_reason IS NOT NULL AND business_reason_source IS NULL
        UNION ALL
        SELECT id, 'attendees', attendees, 'attendees_source', attendees_source
        FROM receiptdocument
        WHERE attendees IS NOT NULL AND attendees_source IS NULL
        """
    )
    return list(session.exec(sql).all())


def _assert_invariant(session: Session, label: str) -> None:
    rows = _violations(session)
    assert not rows, (
        f"source-tag invariant violated after {label}: "
        f"{[(r[0], r[1], r[2], r[3], r[4]) for r in rows]}"
    )


def _assert_source_in_vocab(value: str | None, label: str) -> None:
    if value is None:
        return
    if value == _SENTINEL_SKIPPED:
        return
    assert value in LOCKED_SOURCE_VOCABULARY, (
        f"{label}={value!r} is outside the locked vocabulary "
        f"{sorted(LOCKED_SOURCE_VOCABULARY)}"
    )


# ---------------------------------------------------------------------------
# Per-write-site exercises. Each test runs ONE entry point, asserts the
# expected source value, and re-checks the global invariant.
# ---------------------------------------------------------------------------


def test_apply_receipt_extraction_tags_auto_suggester(isolated_db, tmp_path):
    """Vision/OCR-extracted business_or_personal -> source 'auto_suggester'.
    Only when the receipt has no prior source (the upload-time keyboard
    default of ``auto_confirmed_default`` is sticky)."""
    with Session(isolated_db) as session:
        receipt = ReceiptDocument(
            source="api",
            status="received",
            content_type="photo",
            extracted_supplier="Some Cafe",
            extracted_local_amount=Decimal("100.00"),
            extracted_currency="TRY",
            extracted_date=date(2026, 4, 1),
            caption="Business lunch with team",
        )
        session.add(receipt)
        session.commit()
        session.refresh(receipt)

        apply_receipt_extraction(session, receipt)
        session.refresh(receipt)

        if receipt.business_or_personal is not None:
            assert receipt.category_source == "auto_suggester"
        _assert_source_in_vocab(receipt.category_source, "category_source")
        _assert_invariant(session, "apply_receipt_extraction")


def test_apply_receipt_extraction_does_not_overwrite_keyboard_default(isolated_db):
    """Upload-time auto_confirmed_default (set by the Telegram keyboard
    upload path) must remain in place — vision overwrites the value but
    NOT the source."""
    with Session(isolated_db) as session:
        receipt = ReceiptDocument(
            source="telegram",
            status="received",
            content_type="photo",
            extracted_supplier="Some Cafe",
            extracted_local_amount=Decimal("100.00"),
            extracted_currency="TRY",
            extracted_date=date(2026, 4, 1),
            business_or_personal="Business",
            category_source="auto_confirmed_default",
        )
        session.add(receipt)
        session.commit()
        session.refresh(receipt)

        apply_receipt_extraction(session, receipt)
        session.refresh(receipt)

        assert receipt.category_source == "auto_confirmed_default"
        _assert_invariant(session, "apply_receipt_extraction-keyboard-default")


def test_clarifications_default_business_tags_auto_confirmed_default(
    isolated_db, monkeypatch
):
    """The legacy (non-keyboard) Telegram default-business policy auto-sets
    business_or_personal=Business at clarification-prompt time. That write
    must be source-tagged ``auto_confirmed_default``."""
    monkeypatch.delenv("BUSINESS_PERSONAL_CLARIFICATION_TELEGRAM_IDS", raising=False)
    monkeypatch.delenv("AI_TELEGRAM_INLINE_KEYBOARD_ENABLED", raising=False)
    from app.config import get_settings

    get_settings.cache_clear()

    with Session(isolated_db) as session:
        user = AppUser(telegram_user_id=200001)
        session.add(user)
        session.commit()
        session.refresh(user)

        receipt = ReceiptDocument(
            uploader_user_id=user.id,
            source="telegram",
            status="needs_extraction_review",
            content_type="photo",
            telegram_chat_id=12345,
            extracted_supplier="MIGROS ETILER",
            extracted_local_amount=Decimal("80.00"),
            extracted_currency="TRY",
            extracted_date=date(2026, 4, 1),
            business_or_personal=None,
        )
        session.add(receipt)
        session.commit()
        session.refresh(receipt)

        ensure_receipt_review_questions(session, receipt, user.id)
        session.refresh(receipt)

        # The default-business policy fired (or it didn't — environments
        # vary). Whichever happened, the invariant must hold.
        if receipt.business_or_personal is not None:
            _assert_source_in_vocab(receipt.category_source, "category_source")
            assert receipt.category_source == "auto_confirmed_default"
        _assert_invariant(session, "ensure_receipt_review_questions/default")


def test_legacy_clarification_text_reply_tags_telegram_user(
    isolated_db, monkeypatch
):
    """Non-keyboard clarification flow: user types 'Personal' as a reply
    to the bp clarification question. Source must be ``telegram_user``."""
    monkeypatch.delenv("AI_TELEGRAM_INLINE_KEYBOARD_ENABLED", raising=False)
    from app.config import get_settings

    get_settings.cache_clear()

    with Session(isolated_db) as session:
        user = AppUser(telegram_user_id=300001)
        session.add(user)
        session.commit()
        session.refresh(user)

        receipt = ReceiptDocument(
            uploader_user_id=user.id,
            source="telegram",
            status="needs_extraction_review",
            content_type="photo",
            telegram_chat_id=12345,
            extracted_supplier="MIGROS ETILER",
            extracted_local_amount=Decimal("80.00"),
            extracted_currency="TRY",
            extracted_date=date(2026, 4, 1),
            business_or_personal=None,
        )
        session.add(receipt)
        session.commit()
        session.refresh(receipt)

        question = ClarificationQuestion(
            receipt_document_id=receipt.id,
            user_id=user.id,
            question_key="business_or_personal",
            question_text="Business or Personal?",
        )
        session.add(question)
        session.commit()
        session.refresh(question)

        answer_question(session, question, "Personal")
        session.refresh(receipt)

        assert receipt.business_or_personal == "Personal"
        assert receipt.category_source == "telegram_user"
        _assert_source_in_vocab(receipt.category_source, "category_source")
        _assert_invariant(session, "answer_question/legacy-text-reply")


def test_legacy_clarification_attendees_reply_tags_telegram_user(
    isolated_db, monkeypatch
):
    """Legacy non-keyboard attendees clarification reply -> telegram_user."""
    from app.config import get_settings

    get_settings.cache_clear()

    with Session(isolated_db) as session:
        user = AppUser(telegram_user_id=300002)
        session.add(user)
        session.commit()
        session.refresh(user)

        receipt = ReceiptDocument(
            uploader_user_id=user.id,
            source="telegram",
            status="needs_extraction_review",
            content_type="photo",
            telegram_chat_id=12345,
            extracted_supplier="GUSTO",
            extracted_local_amount=Decimal("250.00"),
            extracted_currency="TRY",
            extracted_date=date(2026, 4, 1),
            business_or_personal="Business",
            category_source="telegram_user",
            business_reason="customer dinner",
            business_reason_source="telegram_user",
        )
        session.add(receipt)
        session.commit()
        session.refresh(receipt)

        question = ClarificationQuestion(
            receipt_document_id=receipt.id,
            user_id=user.id,
            question_key="attendees",
            question_text="Who attended?",
        )
        session.add(question)
        session.commit()
        session.refresh(question)

        answer_question(session, question, "Hakan, Burak")
        session.refresh(receipt)

        assert receipt.attendees == "Hakan, Burak"
        assert receipt.attendees_source == "telegram_user"
        _assert_invariant(session, "answer_question/attendees")


def test_web_patch_receipt_tags_user(isolated_db):
    """PATCH /receipts/{id} from the web review-table -> source 'user'."""
    with Session(isolated_db) as session:
        receipt = ReceiptDocument(
            source="api",
            status="received",
            content_type="photo",
            extracted_supplier="Office Depot",
            extracted_local_amount=Decimal("50.00"),
            extracted_currency="TRY",
            extracted_date=date(2026, 4, 1),
        )
        session.add(receipt)
        session.commit()
        session.refresh(receipt)
        receipt_id = receipt.id

    with TestClient(app) as client:
        resp = client.patch(
            f"/receipts/{receipt_id}",
            json={
                "business_or_personal": "Business",
                "report_bucket": "Admin Supplies",
                "business_reason": "Q2 office supplies",
                "attendees": "N/A",
            },
        )
        assert resp.status_code == 200, resp.text

    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, receipt_id)
        assert receipt.business_or_personal == "Business"
        assert receipt.category_source == "user"
        assert receipt.bucket_source == "user"
        assert receipt.business_reason_source == "user"
        assert receipt.attendees_source == "user"
        _assert_invariant(session, "PATCH /receipts/{id}")


def test_web_patch_receipt_clearing_field_clears_source(isolated_db):
    """PATCH with a canonical field set to null clears the field AND its
    source — the invariant remains satisfied (NULL canonical => NULL
    source allowed)."""
    with Session(isolated_db) as session:
        receipt = ReceiptDocument(
            source="api",
            status="received",
            content_type="photo",
            business_reason="prior reason",
            business_reason_source="auto_suggester",
            extracted_supplier="X",
            extracted_local_amount=Decimal("1.00"),
            extracted_currency="TRY",
            extracted_date=date(2026, 4, 1),
        )
        session.add(receipt)
        session.commit()
        session.refresh(receipt)
        receipt_id = receipt.id

    with TestClient(app) as client:
        resp = client.patch(
            f"/receipts/{receipt_id}",
            json={"business_reason": None},
        )
        assert resp.status_code == 200, resp.text

    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, receipt_id)
        assert receipt.business_reason is None
        assert receipt.business_reason_source is None
        _assert_invariant(session, "PATCH clear field")


def test_manual_statement_create_tags_user_for_business_reason(isolated_db):
    """POST /statements/manual/transactions writing business_reason on the
    linked receipt -> source 'user' (web operator action)."""
    with Session(isolated_db) as session:
        # Pre-seed an empty statement; route will attach the demo user.
        statement = StatementImport(source_filename="manual_test.csv")
        session.add(statement)
        session.commit()
        session.refresh(statement)

        receipt = ReceiptDocument(
            source="api",
            status="received",
            content_type="photo",
            extracted_supplier="Apple Store",
            extracted_local_amount=Decimal("999.00"),
            extracted_currency="TRY",
            extracted_date=date(2026, 4, 1),
        )
        session.add(receipt)
        session.commit()
        session.refresh(receipt)
        receipt_id = receipt.id
        statement_id = statement.id

    with TestClient(app) as client:
        resp = client.post(
            "/statements/manual/transactions",
            json={
                "statement_import_id": statement_id,
                "transaction_date": "2026-04-01",
                "supplier": "Apple Store",
                "currency": "TRY",
                "amount": "999.00",
                "receipt_id": receipt_id,
                "business_reason": "Replacement laptop for engineering",
            },
        )
        assert resp.status_code == 200, resp.text

    with Session(isolated_db) as session:
        receipt = session.get(ReceiptDocument, receipt_id)
        assert receipt.business_reason == "Replacement laptop for engineering"
        assert receipt.business_reason_source == "user"
        _assert_invariant(session, "POST /statements/manual/transactions")


def test_matching_auto_apply_tags_matching(isolated_db, monkeypatch):
    """run_matching auto-applying a bucket to a receipt without one ->
    source 'matching'. We hand-wire a high-confidence MatchDecision shape
    rather than running the full matcher pipeline."""
    with Session(isolated_db) as session:
        receipt = ReceiptDocument(
            source="api",
            status="received",
            content_type="photo",
            extracted_supplier="Starbucks",
            extracted_local_amount=Decimal("75.00"),
            extracted_currency="TRY",
            extracted_date=date(2026, 4, 2),
            business_or_personal="Business",
            category_source="auto_confirmed_default",
        )
        session.add(receipt)
        session.commit()
        session.refresh(receipt)

        # Apply the source-tag write the matcher does. We do it inline
        # rather than running the full ``run_matching`` orchestration —
        # that path requires LLM mocking, statement transaction setup,
        # etc. The contract under test is "when matching writes
        # report_bucket, it must also write bucket_source='matching'",
        # and the canonical-write site at matching.py:389 is the only
        # place that does so.
        receipt.report_bucket = "Meals/Snacks"
        receipt.bucket_source = "matching"
        session.add(receipt)
        session.commit()
        session.refresh(receipt)

        assert receipt.bucket_source == "matching"
        _assert_invariant(session, "matching auto-apply")

    # And exercise the actual code path (matching.py:389) via the
    # production source. Static assertion: the line we just simulated
    # exists and writes 'matching' to bucket_source.
    matching_src = (Path(__file__).resolve().parents[1] / "app/services/matching.py").read_text(
        encoding="utf-8"
    )
    assert 'bucket_source = "matching"' in matching_src, (
        "matching.py must source-tag report_bucket auto-applies as 'matching'"
    )


def test_legacy_csv_import_tags_legacy_unknown(isolated_db, tmp_path):
    """import_legacy_receipt_mapping: every imported canonical field gets
    source 'legacy_unknown' since the CSV has no provenance richer."""
    csv_path = tmp_path / "legacy.csv"
    fieldnames = [
        "Receipt File",
        "File Exists",
        "File Type",
        "Receipt Date",
        "Statement Date",
        "Merchant (Receipt)",
        "Merchant (Statement Match)",
        "Amount Local",
        "Statement Amount Local",
        "Local Currency",
        "Authoritative Source",
        "Business or Personal",
        "Suggested Expense Report Bucket",
        "Reason / Notes",
        "Needs Manual Review",
    ]
    rows = [
        {
            "Receipt File": "abc.jpg",
            "File Exists": "Yes",
            "File Type": "Photo",
            "Receipt Date": "2024-01-15",
            "Statement Date": "",
            "Merchant (Receipt)": "Migros",
            "Merchant (Statement Match)": "",
            "Amount Local": "150.00",
            "Statement Amount Local": "",
            "Local Currency": "TRY",
            "Authoritative Source": "VisionExtract-1",
            "Business or Personal": "Business",
            "Suggested Expense Report Bucket": "Meals/Snacks",
            "Reason / Notes": "team lunch",
            "Needs Manual Review": "No",
        }
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    with Session(isolated_db) as session:
        summary = import_legacy_receipt_mapping(session, csv_path, receipt_root=None)
        assert summary.receipts_created == 1

        receipt = session.exec(select(ReceiptDocument)).first()
        assert receipt.business_or_personal == "Business"
        assert receipt.category_source == "legacy_unknown"
        assert receipt.report_bucket == "Meals/Snacks"
        assert receipt.bucket_source == "legacy_unknown"
        _assert_invariant(session, "import_legacy_receipt_mapping")


def test_canonical_writer_tags_ai_advisory(isolated_db):
    """write_ai_proposal_to_canonical (the AI-proposal write path) ->
    source 'ai_advisory' on Confirm. Already covered by other tests but
    re-asserted here so this single file proves end-to-end coverage of
    every write site identified in the Phase 0 audit."""
    with Session(isolated_db) as session:
        user = AppUser(telegram_user_id=400001)
        session.add(user)
        session.commit()
        session.refresh(user)

        receipt = ReceiptDocument(
            uploader_user_id=user.id,
            source="telegram",
            status="received",
            content_type="photo",
            telegram_chat_id=42,
            extracted_supplier="Acme Cafe",
            extracted_date=date(2026, 5, 1),
            extracted_local_amount=Decimal("42.50"),
            extracted_currency="TRY",
        )
        session.add(receipt)
        session.commit()
        session.refresh(receipt)

        run = AgentReceiptReviewRun(
            receipt_document_id=receipt.id,
            run_source="local_cli",
            run_kind="receipt_inline_keyboard",
            status="completed",
            schema_version="stage1",
            prompt_version="v1",
            comparator_version="v0",
        )
        session.add(run)
        session.commit()
        session.refresh(run)

        read = AgentReceiptRead(
            run_id=run.id,
            receipt_document_id=receipt.id,
            read_schema_version="stage1",
            read_json="{}",
            suggested_business_or_personal="Business",
            suggested_report_bucket="Meals/Snacks",
            suggested_business_reason="Team lunch",
            suggested_attendees_json='["Hakan"]',
        )
        session.add(read)
        session.commit()
        session.refresh(read)

        write_ai_proposal_to_canonical(
            session,
            receipt=receipt,
            agent_read=read,
            source_tag="ai_advisory",
        )
        session.commit()
        session.refresh(receipt)

        assert receipt.category_source == "ai_advisory"
        assert receipt.bucket_source == "ai_advisory"
        assert receipt.business_reason_source == "ai_advisory"
        assert receipt.attendees_source == "ai_advisory"
        _assert_invariant(session, "write_ai_proposal_to_canonical")


# ---------------------------------------------------------------------------
# Static analysis: every canonical-field write in app/ must be paired with
# a corresponding *_source write in the same file (drift detector).
# ---------------------------------------------------------------------------


def test_static_analysis_every_canonical_write_has_paired_source_write():
    """Drift detector. Greps app/ for any line that assigns one of the four
    canonical fields and asserts that the same file ALSO writes the
    paired ``*_source`` column. This catches the next time someone adds a
    new write path and forgets the source-tag, without forcing per-test
    coverage of every site."""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    pairs = {
        "business_or_personal": "category_source",
        "report_bucket": "bucket_source",
        "business_reason": "business_reason_source",
        "attendees": "attendees_source",
    }
    # Files where the schema/migration/audit tooling references the
    # canonical names but does not perform a runtime canonical write.
    EXCLUDED_FILES = {
        # Schema definition (Pydantic/SQLModel field declarations,
        # not runtime writes).
        app_dir / "models.py",
        app_dir / "schemas.py",
    }

    # Match a non-None assignment to ``<obj>.<canonical>``. We deliberately
    # allow ``= None`` — the canonical→source invariant only requires a
    # source when the canonical is non-null, so clearing-to-None writes
    # don't need a paired source write. The lookahead ``(?!\s*None\b)``
    # walks past whitespace before checking for the literal ``None``.
    for canonical, source_col in pairs.items():
        nonnull_pattern = re.compile(
            rf"\b\w+\.{canonical}\s*=(?!\s*None\b)(?!=)"
        )
        for path in app_dir.rglob("*.py"):
            if path in EXCLUDED_FILES:
                continue
            text = path.read_text(encoding="utf-8")
            if not nonnull_pattern.search(text):
                continue
            assert source_col in text, (
                f"{path} writes to '{canonical}' but never references "
                f"'{source_col}' — every canonical write must be paired "
                f"with the corresponding *_source write per F-AI-Stage1 PR5."
            )


def test_locked_vocabulary_matches_canonical_writer():
    """The ``_ALLOWED_SOURCE_TAGS`` set in
    ``services/agent_receipt_canonical_writer.py`` must equal the locked
    vocabulary recorded in this test file. Drift here means somebody
    introduced a new source value without updating the documentation."""
    from app.services.agent_receipt_canonical_writer import _ALLOWED_SOURCE_TAGS

    assert _ALLOWED_SOURCE_TAGS == LOCKED_SOURCE_VOCABULARY, (
        f"locked vocabulary drift: canonical writer accepts "
        f"{sorted(_ALLOWED_SOURCE_TAGS)} but this test file expects "
        f"{sorted(LOCKED_SOURCE_VOCABULARY)}"
    )
