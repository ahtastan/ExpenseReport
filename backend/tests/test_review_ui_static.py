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
    assert "atReturn" in html
    assert "air_travel_return_date" in html
    assert "Return date" in html
    assert "flexWrap:'nowrap'" in html
    # Return date earlier than travel date is intentionally allowed now.
    assert "Return date cannot be before travel date." not in html
    assert "f.atReturn < f.atDate" not in html
    assert 'data-testid="air-travel-panel"' in html

    print("review_ui_static_tests=passed")


if __name__ == "__main__":
    main()
