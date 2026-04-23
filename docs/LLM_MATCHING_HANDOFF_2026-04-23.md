# LLM Matching Handoff — 2026-04-23

## Objective Of This Step
Add LLM-assisted disambiguation to receipt↔statement matching for hard cases where deterministic scoring cannot pick a unique winner. The LLM tier is the one defined by the routing policy: `MATCHING_MODEL` (default `gpt-5.4-mini`). Cost control: the model is only invoked when deterministic scoring is ambiguous.

## Files Changed
- `backend/app/services/model_router.py` — added `MatchDisambiguation`, `match_disambiguate()`, `_MATCH_PROMPT`, `_call_openai_text`, indirection `_text_call`.
- `backend/app/services/matching.py` — imports router + `replace`; `MatchRunStats` gains `llm_disambiguated` and `llm_abstained`; `run_matching()` runs a disambiguation pass when `unique_high` is false and there are ≥2 plausible (high/medium) candidates; promoted decision uses `match_method="llm_disambiguated_v1"`.
- `backend/tests/test_llm_matching.py` — new: confident-pick, abstain, unavailable paths.
- `docs/current_progress.md` — single new bullet under Latest Implementation Steps.

## Exact Behavior Changed
- `run_matching` is no longer purely deterministic. After scoring, any receipt with no unique-high but ≥2 plausible candidates triggers one `match_disambiguate` call.
- A confident pick (`confidence == "high"` from the model) promotes the chosen candidate's `MatchScore` to `high`, demotes rival `high` scores to `medium`, and appends `; llm(<model>): <reasoning>` to `MatchDecision.reason`. That decision's `match_method` becomes `llm_disambiguated_v1`; auto-approval fires because the LLM-promoted pick is treated as uniquely high even when the per-transaction receipt count would otherwise block approval.
- Any other model outcome (abstain / non-high confidence / hallucinated id / `None`) leaves scores untouched; no auto-approval. `MatchRunStats.llm_abstained` is incremented.
- The router never invents a `transaction_id` — a hallucinated id is coerced to abstain with `reasoning="model returned an id that was not in the candidate list"`.
- No DB schema change. `MatchDecision.match_method` now has a new valid value `llm_disambiguated_v1` alongside the existing `date_amount_merchant_v1`.

## Tests Run And Results
Run with the codex-primary-runtime Python (has sqlmodel + openpyxl):
- `backend/tests/test_model_router.py` — passed (mini-only, escalation, mini-unavailable, both-fail, partial-fallback, unsupported-file).
- `backend/tests/test_llm_matching.py` — passed (confident-pick, abstain, unavailable).
- `backend/tests/test_review_confirmation.py` — passed.
- `backend/tests/smoke_air_travel.py` — passed.
- `backend/tests/smoke_meals_entertainment.py` — passed.
- `backend/tests/test_review_ui_static.py` — passed.
- `backend/tests/test_db_migration.py` — passed.
- `backend/tests/test_statement_import.py` — passed.
- `python -m compileall backend/app` — passed.

## Real-Data Verification Status
Not performed. No live `OPENAI_API_KEY` call was made in this step. The router falls through to `None` when the key is unset, so the live backend behaves identically to the prior deterministic baseline until a key is configured. The test suite covers the disambiguation logic by monkey-patching `model_router._text_call`.

## Open Assumptions
- Model names `gpt-5.4` / `gpt-5.4-mini` are treated as opaque strings and only bind at the HTTP boundary. If those identifiers change upstream, override via env (`OCR_MINI_MODEL`, `OCR_FULL_MODEL`, `MATCHING_MODEL`, `SYNTHESIS_MODEL`, `CHAT_MODEL`) without code changes.
- `_call_openai_text` assumes the OpenAI chat-completions interface (`messages=[{role,content}]`, `response.choices[0].message.content`). If the provider moves to the Responses API, only this helper needs replacing.
- "Plausible" is defined as `confidence in {"high","medium"}`. Low-confidence scores are never shown to the model — cost vs. recall tradeoff; a future step may widen this.
- LLM-promoted auto-approval bypasses the per-transaction uniqueness guard (`unique_transaction_high`). This is intentional for single-receipt ambiguity but may need revisiting if one statement transaction ever has multiple receipts competing for it.
- Prompt is English-only. Turkish supplier strings pass through as-is; results on pure Turkish receipts are untested.

## Next Recommended Step
Wire `SYNTHESIS_MODEL` into the report package: generate a short narrative summary (trip purpose, totals by bucket, any anomalies) and include it as `summary.md` alongside `expense_report_part_*.xlsx` + `annotated_receipts.pdf`. Narrow scope: one call per package, no prompt chaining, no changes to the Excel generator.

## Commands To Rerun
Absolute paths used below are Windows-style; adjust to POSIX as needed.

```bash
# Python with project deps
PY="C:/Users/CASPER/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe"
cd C:/Users/CASPER/.openclaw/workspace/Expense/expense-reporting-app

# Syntax + full test suite
"$PY" -m compileall backend/app -q
"$PY" backend/tests/test_model_router.py
"$PY" backend/tests/test_llm_matching.py
"$PY" backend/tests/test_review_confirmation.py
"$PY" backend/tests/smoke_air_travel.py
"$PY" backend/tests/smoke_meals_entertainment.py
"$PY" backend/tests/test_review_ui_static.py
"$PY" backend/tests/test_db_migration.py
"$PY" backend/tests/test_statement_import.py

# Live API smoke (requires key and a seeded ambiguous receipt)
OPENAI_API_KEY=sk-... \
MATCHING_MODEL=gpt-5.4-mini \
"$PY" -c "from app.services.model_router import match_disambiguate; print(match_disambiguate({'supplier':'Starbucks OTG Poyrazkoy','date':'2026-04-02','local_amount':75.0,'local_currency':'TRY'}, [{'transaction_id':1,'supplier':'STARBUCKS OTG POYRAZKOY','date':'2026-04-02','local_amount':75.0,'local_currency':'TRY','deterministic_reason':'exact'}, {'transaction_id':2,'supplier':'STARBUCKS IST OTG','date':'2026-04-02','local_amount':75.0,'local_currency':'TRY','deterministic_reason':'exact'}]))"

# Relevant commits
git log --oneline -5
# 73d375f Add LLM disambiguation for ambiguous receipt-to-statement matches
# 8f823b3 Add staged OCR model router with mini-first escalation
# 06a1724 Allow return date earlier than travel date
# c094328 Add air-travel validation context, bulk-classify, live smoke harness
# efe6b0d Hide empty-bucket categories from category dropdown
```
