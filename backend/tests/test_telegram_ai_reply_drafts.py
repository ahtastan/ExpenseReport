"""F-AI-TG-0 Telegram reply draft contract tests.

Pin the deterministic templates and the draft-only invariants:

  * every draft has ``send_allowed=False``
  * draft text never contains forbidden phrases (no "AI approved", no
    "report blocked by AI", no "sent to Telegram", etc.)
  * draft text never leaks storage paths, prompt text, raw model JSON,
    or model-debug fields
  * the function returns ``None`` when no draft is warranted

No live model calls. No prod DB. No actual Telegram client.
"""

from __future__ import annotations

import pytest

from app.services.telegram_ai_reply_drafts import (
    DRAFT_KINDS,
    build_receipt_reply_draft,
    build_review_row_reply_draft,
)


def _all_drafts() -> list[dict]:
    """Generate one draft per non-none kind, for forbidden-phrase / privacy
    checks that need to inspect every template's actual text."""
    drafts: list[dict] = []
    drafts.append(
        build_receipt_reply_draft(
            {
                "business_or_personal": "Business",
                "business_reason": None,
                "attendees": "Hakan",
                "report_bucket": "Hotel/Lodging/Laundry",
            }
        )
    )
    drafts.append(
        build_receipt_reply_draft(
            {
                "business_or_personal": "Business",
                "business_reason": "Customer dinner",
                "attendees": None,
                "report_bucket": "Meals/Snacks",
            }
        )
    )
    drafts.append(
        build_review_row_reply_draft(
            {
                "receipt_statement_issues": [
                    {"code": "receipt_statement_amount_mismatch"}
                ],
            }
        )
    )
    drafts.append(
        build_review_row_reply_draft(
            {
                "receipt_statement_issues": [
                    {"code": "receipt_statement_date_mismatch"}
                ],
            }
        )
    )
    drafts.append(
        build_review_row_reply_draft(
            {
                "ai_review": {"status": "warn"},
            }
        )
    )
    return [d for d in drafts if d is not None]


# ---------------------------------------------------------------------------
# happy-path templates
# ---------------------------------------------------------------------------


def test_missing_business_reason_returns_warning_draft():
    draft = build_receipt_reply_draft(
        {
            "business_or_personal": "Business",
            "business_reason": None,
            "attendees": "Hakan",
            "report_bucket": "Hotel/Lodging/Laundry",
        }
    )
    assert draft is not None
    assert draft["kind"] == "missing_business_reason"
    assert draft["severity"] == "warning"
    assert draft["send_allowed"] is False
    assert "business purpose" in draft["text"].lower()


def test_missing_attendees_returns_warning_draft():
    draft = build_receipt_reply_draft(
        {
            "business_or_personal": "Business",
            "business_reason": "Project meeting",
            "attendees": None,
            "report_bucket": "Lunch",
        }
    )
    assert draft is not None
    assert draft["kind"] == "missing_attendees"
    assert draft["severity"] == "warning"
    assert draft["send_allowed"] is False
    assert "attendees" in draft["text"].lower()


def test_amount_mismatch_returns_blocker_draft():
    draft = build_review_row_reply_draft(
        {
            "receipt_statement_issues": [
                {
                    "code": "receipt_statement_amount_mismatch",
                    "message": "ignored by template",
                }
            ],
        }
    )
    assert draft is not None
    assert draft["kind"] == "amount_mismatch"
    assert draft["severity"] == "blocker"
    assert draft["send_allowed"] is False
    assert "review queue" in draft["text"].lower()
    assert "diners statement" in draft["text"].lower()


def test_date_mismatch_returns_warning_draft():
    draft = build_review_row_reply_draft(
        {
            "receipt_statement_issues": [
                {"code": "receipt_statement_date_mismatch"}
            ],
        }
    )
    assert draft is not None
    assert draft["kind"] == "date_mismatch"
    assert draft["severity"] == "warning"
    assert draft["send_allowed"] is False
    assert "review queue" in draft["text"].lower()


def test_ai_advisory_warning_returns_info_draft():
    draft = build_review_row_reply_draft({"ai_review": {"status": "warn"}})
    assert draft is not None
    assert draft["kind"] == "ai_advisory_warning"
    assert draft["severity"] == "info"
    assert draft["send_allowed"] is False
    assert "ai second read" in draft["text"].lower()
    assert "advisory" in draft["text"].lower()


def test_ai_block_status_also_produces_advisory_draft():
    """``block`` is the loudest AI advisory state but still advisory only,
    so the Telegram template stays at ``info`` severity. The wording must
    not imply a real blocker."""
    draft = build_review_row_reply_draft({"ai_review": {"status": "block"}})
    assert draft is not None
    assert draft["kind"] == "ai_advisory_warning"
    assert draft["severity"] == "info"
    assert "advisory" in draft["text"].lower()


# ---------------------------------------------------------------------------
# no-draft / no-op cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "receipt",
    [
        None,
        {},
        {"business_or_personal": None},
        {"business_or_personal": "Personal", "business_reason": None},
        # Business with everything filled in.
        {
            "business_or_personal": "Business",
            "business_reason": "Customer dinner",
            "attendees": "Alice; Bob",
            "report_bucket": "Lunch",
        },
        # Business non-meal bucket without attendees: fine, attendees not required.
        {
            "business_or_personal": "Business",
            "business_reason": "Project meeting",
            "attendees": None,
            "report_bucket": "Hotel/Lodging/Laundry",
        },
    ],
)
def test_receipt_with_no_issue_returns_none(receipt):
    assert build_receipt_reply_draft(receipt) is None


@pytest.mark.parametrize(
    "row",
    [
        None,
        {},
        {"receipt_statement_issues": []},
        {"receipt_statement_issues": [{"code": "receipt_statement_currency_mismatch"}]},
        {"ai_review": {"status": "pass"}},
        {"ai_review": {"status": "stale"}},
        {"ai_review": {"status": "malformed"}},
    ],
)
def test_review_row_with_no_actionable_issue_returns_none(row):
    assert build_review_row_reply_draft(row) is None


# ---------------------------------------------------------------------------
# row-payload nesting + priority
# ---------------------------------------------------------------------------


def test_review_row_accepts_nested_source_payload_shape():
    """The function accepts the full review-row payload shape used by the
    API (source.match.receipt_statement_issues + source.ai_review) without
    requiring the caller to flatten it first."""
    row = {
        "source": {
            "match": {
                "receipt_statement_issues": [
                    {"code": "receipt_statement_amount_mismatch"}
                ]
            },
            "ai_review": {"status": "warn"},
        }
    }
    draft = build_review_row_reply_draft(row)
    assert draft is not None
    assert draft["kind"] == "amount_mismatch"


def test_amount_issue_outranks_date_issue():
    row = {
        "receipt_statement_issues": [
            {"code": "receipt_statement_date_mismatch"},
            {"code": "receipt_statement_amount_mismatch"},
        ],
    }
    draft = build_review_row_reply_draft(row)
    assert draft is not None
    assert draft["kind"] == "amount_mismatch"


def test_deterministic_issue_outranks_ai_advisory():
    row = {
        "receipt_statement_issues": [
            {"code": "receipt_statement_amount_mismatch"}
        ],
        "ai_review": {"status": "warn"},
    }
    draft = build_review_row_reply_draft(row)
    assert draft is not None
    assert draft["kind"] == "amount_mismatch"


def test_review_row_falls_back_to_receipt_only_checks():
    """When no row-level issue applies, the row-level helper consults the
    embedded receipt for missing-reason / missing-attendees drafts."""
    row = {
        "receipt": {
            "business_or_personal": "Business",
            "business_reason": None,
            "attendees": "Hakan",
            "report_bucket": "Lunch",
        },
    }
    draft = build_review_row_reply_draft(row)
    assert draft is not None
    assert draft["kind"] == "missing_business_reason"


def test_missing_business_reason_outranks_missing_attendees():
    """When a Business meal receipt is missing both business_reason and
    attendees, the more general ``missing_business_reason`` draft wins
    so the operator gets the higher-leverage prompt first."""
    draft = build_receipt_reply_draft(
        {
            "business_or_personal": "Business",
            "business_reason": None,
            "attendees": None,
            "report_bucket": "Lunch",
        }
    )
    assert draft is not None
    assert draft["kind"] == "missing_business_reason"


# ---------------------------------------------------------------------------
# safety contract
# ---------------------------------------------------------------------------


def test_send_allowed_is_always_false_for_every_draft_kind():
    drafts = _all_drafts()
    assert len(drafts) == 5
    kinds = {d["kind"] for d in drafts}
    assert kinds == {
        "missing_business_reason",
        "missing_attendees",
        "amount_mismatch",
        "date_mismatch",
        "ai_advisory_warning",
    }
    for d in drafts:
        assert d["send_allowed"] is False


def test_drafts_never_contain_forbidden_phrases():
    forbidden = (
        "AI approved",
        "AI rejected",
        "report blocked by AI",
        "sent to Telegram",
    )
    for draft in _all_drafts():
        for phrase in forbidden:
            assert phrase.lower() not in draft["text"].lower(), (
                f"forbidden phrase {phrase!r} appeared in draft {draft['kind']!r}"
            )


def test_drafts_never_leak_storage_paths_or_model_internals():
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
    for draft in _all_drafts():
        text = draft["text"]
        for needle in forbidden_substrings:
            assert needle not in text, (
                f"forbidden substring {needle!r} appeared in draft {draft['kind']!r}"
            )


def test_severity_vocabulary_is_only_info_warning_blocker():
    allowed = {"info", "warning", "blocker"}
    for draft in _all_drafts():
        assert draft["severity"] in allowed


def test_draft_kinds_constant_lists_every_kind_plus_none():
    assert "none" in DRAFT_KINDS
    for draft in _all_drafts():
        assert draft["kind"] in DRAFT_KINDS


def test_module_has_no_live_model_or_telegram_dependencies():
    """Light defensive check: the draft module must not import openai,
    anthropic, deepseek, or the telegram client. If any of those creep in
    via a refactor, this test fails immediately."""
    import app.services.telegram_ai_reply_drafts as drafts_module

    forbidden_modules = (
        "openai",
        "anthropic",
        "deepseek",
        "httpx",
        "requests",
    )
    module_globals = vars(drafts_module)
    for name in forbidden_modules:
        assert name not in module_globals, (
            f"draft module unexpectedly imports {name!r}"
        )
    # Telegram service must not be wired in either.
    for name in ("telegram", "telegram_send", "send_message"):
        assert name not in module_globals, (
            f"draft module unexpectedly references telegram sender {name!r}"
        )
