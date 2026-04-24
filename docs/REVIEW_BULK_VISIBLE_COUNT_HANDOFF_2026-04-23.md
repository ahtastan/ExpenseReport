# Review Bulk Visible-Count Handoff

## 1. Current Architecture State
- FastAPI backend review APIs remain unchanged for this step.
- `frontend/review-table.html` is still the single-file React SPA served to `/review`.
- The bulk-classify toolbar already supports `attention_required`, `all`, and `selected (visible)` scopes.

## 2. Latest Completed Implementation Step
- Added a small operator-facing UI aid in the bulk toolbar:
  - when `selected (visible)` is active, the toolbar now shows how many rows are currently visible;
  - the Apply button is disabled if the visible set is empty.

## 3. What Is Verified
- The new visible-row count text is present in the review table HTML.
- The existing bulk scope label and selected-scope plumbing remain present.

## 4. What Is Not Verified
- Live browser interaction in `/review` after filtering and bulk applying.
- The exact empty-filter UX in a real session with current statement data.

## 5. Exact Next Safest UI-Only Step
- Run a browser smoke on `/review` and confirm:
  - `selected (visible)` shows the visible-row count;
  - Apply stays disabled when the filtered set is empty;
  - selecting a non-empty filter still allows a bulk update to proceed.

## 6. Likely Risks If Continued Carelessly
- Expanding the toolbar without guarding the selected-scope state could reintroduce accidental updates to hidden rows.
- Touching backend bulk-update behavior is unnecessary for this step and would widen the blast radius.

