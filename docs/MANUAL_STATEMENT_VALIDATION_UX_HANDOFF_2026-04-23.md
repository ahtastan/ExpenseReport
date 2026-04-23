# Manual Statement Validation UX Handoff

## Current UI Architecture State
- `/review` remains the single-file React 18 SPA in `frontend/review-table.html`.
- Add Statement remains a narrow modal flow beside Import Statement.
- Backend behavior was not changed in this step.

## Latest Completed Implementation Step
- Added field-level Add Statement validation feedback for missing transaction date, missing supplier, and missing positive amount.
- Added a clearer extraction banner when extraction completes but no usable statement fields are available.

## Exact Behavior Changed
- Clicking Save with missing required fields highlights the affected fields and shows:
  - `Transaction date is required.`
  - `Supplier is required.`
  - `Positive amount is required.`
- If extraction returns no usable date/supplier/amount prefill, the modal tells the operator to enter those fields manually.
- Extracted values remain editable before save.

## Verified
- Static UI test verifies the Add Statement validation messages and existing selected-visible toolbar strings.
- Manual statement backend regression still passes.
- Live review smoke verifies:
  - no-usable-prefill banner;
  - field-level required messages;
  - editable extracted values;
  - successful Add Statement save;
  - visible manual row;
  - selected-visible bulk behavior;
  - validation screen path.

## Still Unverified
- PDF-specific operator messaging.
- Real receipt OCR quality.

## Exact Next Recommended Step
- Stop this Add Statement validation slice unless a PDF-specific UI requirement is requested.

