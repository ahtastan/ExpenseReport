# Source-tag vocabulary

The source-tag columns (`category_source`, `bucket_source`,
`business_reason_source`, `attendees_source`) on `ReceiptDocument` record
**who** set the corresponding canonical value
(`business_or_personal`, `report_bucket`, `business_reason`, `attendees`).

The vocabulary is **locked** — these are the only seven values any code path
may write. Adding an eighth value requires updating:

1. The doc-string on `app/models.py` lines 64–66
2. `_ALLOWED_SOURCE_TAGS` in `app/services/agent_receipt_canonical_writer.py`
3. `LOCKED_SOURCE_VOCABULARY` in `backend/tests/test_source_tag_invariants.py`
4. This file

The drift detector
`test_locked_vocabulary_matches_canonical_writer` fails if any of those
fall out of sync.

## Vocabulary

| Source value | When it's written |
|---|---|
| `user` | Web UI direct edit. PATCH `/receipts/{id}` from the review-table dropdown, and the manual-statement-entry route writing `business_reason` (`POST /statements/manual/transactions`). |
| `telegram_user` | Any Telegram-driven user choice: keyboard-Edit text reply, keyboard category/bucket pick, keyboard Type→Personal, **and** legacy text-clarification reply (non-keyboard flow). The user is the source whether they tapped a button or typed an answer. |
| `ai_advisory` | The AgentDB live-model proposal lands on Confirm. Written exclusively by `write_ai_proposal_to_canonical(... source_tag="ai_advisory")`. |
| `auto_confirmed_default` | Telegram upload-time default when `business_or_personal` is unspecified. Set both for the keyboard-flow upload path (`services/telegram.py` upload handler) and the legacy non-keyboard policy (`services/clarifications._should_default_business_for_telegram_receipt`). Also used by `auto_confirm_pending_inline_keyboards` on timeout/supersede. |
| `matching` | A confirmed Diners-statement match auto-applies a `report_bucket` from disambiguation/classification (`services/matching.py:run_matching`). Receipt has no operator-set bucket at the time of the write. |
| `auto_suggester` | Deterministic supplier→classification proposal applied at extraction time (`services/receipt_extraction.apply_receipt_extraction`). Only fires when `category_source` is currently `NULL` — never overwrites a sticky upload-time default or a later user/AI tag. |
| `legacy_unknown` | Backfill values written by the migration in sub-PR 1 and the legacy CSV import (`services/legacy_receipts.import_legacy_receipt_mapping`). Means "the canonical value pre-dates the source-tag system." |

## Why source-tagging matters

The source tag is the only audit trail for **how** a receipt got its
classification. Without it, the AgentDB live-model reviewer cannot tell
whether a `business_or_personal == "Business"` came from the user, from
the AI, from a backfill, or from an upload-time default — and the
"respect existing user source" rule used by the Confirm callback
(`respect_existing_user_source=True`) cannot be enforced. Lose the source
column and Confirm will silently overwrite user-edited fields with the AI
proposal.

## The invariant

**Every canonical write must record a source.** Concretely:

> For every receipt row, if a canonical column is non-`NULL`, the
> corresponding `*_source` column must also be non-`NULL`.

This is enforced by `test_every_canonical_field_has_corresponding_source`
(via the per-write-site exercises in
`backend/tests/test_source_tag_invariants.py`) and the static-analysis
drift detector
`test_static_analysis_every_canonical_write_has_paired_source_write`.

Clearing a canonical field to `NULL` should also clear the corresponding
source (`PATCH /receipts/{id}` does this). Setting a source without
setting the canonical (the PR4 `telegram_user_skipped` sentinel on Skip-
for-now) is allowed — it does not violate the canonical→source rule
because the canonical is still `NULL`.

## References

- Schema: `backend/app/models.py` lines 60–66
- Tier-2 vocabulary (separate, EDT category/bucket vocab): `backend/app/category_vocab.py`
- Canonical writer: `backend/app/services/agent_receipt_canonical_writer.py`
- Invariant test: `backend/tests/test_source_tag_invariants.py`
- F-AI-Stage1 sub-PRs: 1 (schema + backfill), 3 (Telegram canonical writer),
  4 (button-Edit), 5 (this audit + invariant test)
