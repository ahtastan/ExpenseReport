from pathlib import Path


def main() -> None:
    html = (Path(__file__).resolve().parents[2] / "frontend" / "review-table.html").read_text(encoding="utf-8")

    assert "Bulk classify" in html
    assert "bulk-update" in html
    assert "setBulkScope" in html
    assert "attention_required" in html
    assert "visible row" in html
    assert "/statements/import-excel" in html
    assert "/statements/import', fd" not in html
    assert "Add Statement" in html
    assert "/statements/manual/receipt" in html
    assert "/statements/manual/transactions" in html
    assert "Transaction date is required." in html
    assert "Supplier is required." in html
    assert "Positive amount is required." in html
    assert "no usable statement fields" in html
    assert "All rows" in html
    assert "Apply to" in html
    assert "issue.review_row_id" in html
    assert "issue.supplier" in html
    assert "issue.transaction_date" in html
    assert "issue.report_bucket" in html
    assert "issue.air_travel_date" in html
    assert "issue.air_travel_return_date" in html
    assert "issue.air_travel_rt_or_oneway" in html
    assert "Open in Review Queue" in html
    assert "onNavigateReview?.(issue.review_row_id)" in html
    assert "atReturn" in html
    assert "air_travel_return_date" in html
    assert "Return date" in html
    assert "flexWrap:'nowrap'" in html
    # Return date earlier than travel date is intentionally allowed now.
    assert "Return date cannot be before travel date." not in html
    assert "f.atReturn < f.atDate" not in html
    assert 'data-testid="air-travel-panel"' in html
    # B16 follow-up: manual "Run Matching" button on /review toolbar
    # so the operator can re-fire matching after import or after editing
    # receipts without dropping to curl.
    assert "Run Matching" in html
    assert "/matching/run" in html
    assert "runMatching" in html

    # F-AI-0b-2 / F-AI-0b-3: AI second-read advisory display markers.
    # Components and helpers must be present in the bundle.
    assert "AiReviewBadge" in html
    assert "AiReviewDifferencesPanel" in html
    assert "AI_DIFFERENCE_LABEL" in html
    assert "AI_BADGE_PRESENTATION" in html
    # Status copy must be advisory; no "Report blocked by AI"-style wording.
    assert "AI second read: pass" in html
    assert "AI second read: warning" in html
    assert "AI second read: block (advisory)" in html
    assert "AI second read: stale" in html
    assert "AI second read unavailable" in html
    assert "Report blocked by AI" not in html
    assert "AI rejected" not in html
    # Filter chips wired through the same advisory state machine.
    assert "ai_warn" in html
    assert "ai_block" in html
    assert "ai_stale" in html
    assert "AI ◎ warn" in html
    assert "AI ◉ block" in html
    assert "AI ⌛ stale" in html
    # Tooltip must explicitly call out advisory-only nature.
    assert "AI second read is advisory only" in html
    # Local row mapping copies source.ai_review into row.aiReview.
    assert "src.ai_review" in html
    assert "aiReview" in html
    # Difference codes mapped to plain English, several of the common ones.
    for code in (
        "amount_mismatch",
        "currency_mismatch",
        "date_mismatch",
        "supplier_mismatch",
        "missing_business_reason",
        "missing_attendees",
    ):
        assert code in html, f"expected difference code {code!r} in HTML"

    # F-AI-TG-2: Telegram draft preview markers. Display-only component
    # rendered inside the expanded row. Must clearly say "Not sent" and
    # must NOT include any send-button copy.
    assert "TelegramDraftPreview" in html
    assert "Telegram draft preview" in html
    assert "Not sent" in html
    assert "src.telegram_draft" in html
    assert "telegramDraft" in html
    assert "TELEGRAM_DRAFT_SEVERITY_PRESENTATION" in html
    for forbidden in (
        "Send Telegram",
        "Send to Telegram",
        "Send draft",
        "Send message",
        "Send reply",
    ):
        assert forbidden not in html, (
            f"forbidden send copy {forbidden!r} appeared in HTML"
        )

    print("review_ui_static_tests=passed")


def test_review_ui_static_markers() -> None:
    main()


if __name__ == "__main__":
    main()
