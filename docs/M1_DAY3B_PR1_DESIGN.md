# M1 Day 3b PR-1 — Merge logic refactor + vision reproducibility metadata

**Status:** DESIGN — pending PM review. No implementation has begun.
**Foundation:** Day 3a deployed at `83726f8`, `fieldprovenanceevent` table live in production with 108 backfilled events, cross-write invariant test passes against the post-backfill state.
**This PR's scope:** wrap every tracked-column write path inside the Day 3a service-layer wrapper + add reproducibility metadata for vision-source events.
**Explicitly NOT in this PR:** §8.7 partial-expense fields (`claimed_local_amount`, `receipt_total_local_amount`) — covered separately in Day 3b PR-2.

---

## 0. TL;DR

PR-1 is a refactor of every code path that mutates one of the 9 tracked `ReceiptDocument` columns. After PR-1 merges:

- Every tracked-column write is wrapped in `with session.begin():` alongside a `record_field_event(...)` call.
- An `app.services.field_provenance.write_tracked_field()` helper encodes that pattern in one place.
- `apply_receipt_extraction` becomes the canonical example: it emits one `decision_group_id` per OCR pass, writes `PROPOSED` events for every candidate from every source, and emits `ACCEPTED` (or `OVERRIDDEN` on a re-extract that produces a different value) for the merge winner.
- Vision-source events carry six required metadata keys (`model_name`, `model_version`, `prompt_version`, `extraction_run_id`, `input_file_hash`, `escalated`); the wrapper raises `TypeError` when any required key is missing.
- The cross-write invariant test (`test_invariant_column_equals_latest_event`) is promoted from a one-shot post-backfill check to an `autouse` fixture so any test that mutates a tracked column without going through the wrapper fails immediately.

No schema migration. The 108 legacy `legacy_unknown_current` events stay as-is; new events accumulate alongside them.

---

## 1. Inventory of tracked-column write paths

Exhaustive audit of every code path that mutates one of the 9 tracked `ReceiptDocument` columns. Bare-attribute assignment, `setattr`-loop, ORM bulk-update, and direct SQL `UPDATE` patterns were all searched.

### Summary table

| # | Path | Function | Tracked cols touched | Trigger |
|---|---|---|---|---|
| 1 | `backend/app/services/receipt_extraction.py:252–269` | `apply_receipt_extraction` | 6: `extracted_date`, `extracted_supplier`, `extracted_local_amount`, `extracted_currency`, `business_or_personal`, `receipt_type` | OCR on receipt upload + manual `POST /receipts/{id}/extract` |
| 2 | `backend/app/services/legacy_receipts.py:70–116` | `import_legacy_receipt_mapping` | All 9 | One-shot CLI: legacy CSV → DB import |
| 3 | `backend/app/services/clarifications.py:193–368` | `answer_question` | 6: `business_or_personal`, `business_reason`, `attendees`, `extracted_date`, `extracted_local_amount`, `extracted_currency`, `extracted_supplier` | User answers a clarification question (Telegram or web) |
| 4 | `backend/app/routes/statements.py:189–191` | `create_manual_statement_transaction` | 1: `business_reason` | Operator enters a manual statement transaction with a receipt linkage |
| 5 | `backend/app/routes/receipts.py:59–73` | `update_receipt` (PATCH) | Any of 9 via generic `setattr` loop | `PATCH /receipts/{id}` web call |
| 6 | `backend/scripts/classify_existing_receipts.py:41–143` | `main` | 1: `receipt_type` | One-shot CLI: re-classify old receipts via vision |
| 7 | `backend/app/services/review_sessions.py:326–423` | `_sync_review_rows` (read-only) | 0 — reads tracked cols, writes only `ReviewRow.*_json` | Initial review session creation / re-sync |

Path #7 is included for completeness but is read-only against the 9 tracked columns: it serializes their current values into JSON snapshots on `ReviewRow`. No event needed.

### Path-by-path detail

#### 1. `apply_receipt_extraction` (the load-bearing path)

- **Lines:** `receipt_extraction.py:252–269`
- **Trigger:** receipt upload (`POST /receipts/upload` → ingestion → extraction) or operator-driven re-extract (`POST /receipts/{id}/extract`).
- **Current shape:** computes `ReceiptExtraction` (deterministic regex over caption/filename + vision OCR escalation), then assigns 5 columns unconditionally and `receipt_type` conditionally (`if receipt.receipt_type is None`). Calls `session.add(receipt); session.commit(); session.refresh(receipt)`. No `session.begin()`.
- **Source attribution per field:** mixed — for any one column the ACCEPTED winner could be deterministic, vision, or a previously-stored user override. The merge function picks; see §2.
- **Actor attribution:** `ActorType.SYSTEM_JOB` for the merge wrapper itself; the per-candidate `PROPOSED` events use `ActorType.DETERMINISTIC_PIPELINE` and `ActorType.VISION_PIPELINE` respectively. `actor_label` = `"system:apply_receipt_extraction"` for the merge winner (the ACCEPTED event), `"deterministic:apply_receipt_extraction"` / `"vision:apply_receipt_extraction"` for the per-source PROPOSED events.
- **Refactored shape (sketch):**

  ```
  decision_group_id = uuid.uuid4().hex
  with session.begin():
      for field in tracked_fields:
          for candidate in deterministic_candidates(field):
              record_field_event(
                  session, ..., field_name=field,
                  event_type=PROPOSED, source=DETERMINISTIC,
                  decision_group_id=decision_group_id,
                  value=candidate.value,
                  actor_type=DETERMINISTIC_PIPELINE,
                  actor_label="deterministic:apply_receipt_extraction",
              )
          for candidate in vision_candidates(field):
              record_field_event(
                  session, ..., field_name=field,
                  event_type=PROPOSED, source=VISION,
                  decision_group_id=decision_group_id,
                  value=candidate.value,
                  confidence=candidate.confidence,
                  actor_type=VISION_PIPELINE,
                  actor_label="vision:apply_receipt_extraction",
                  metadata=vision_reproducibility_metadata,
              )
          winner = pick_merge_winner(field, prior_accepted, deterministic, vision)
          if winner is not None and winner != prior_accepted_value:
              prior_event = get_current_event(session, ..., field_name=field)
              event_type = OVERRIDDEN if prior_event is not None else ACCEPTED
              write_tracked_field(
                  session, receipt, field_name=field,
                  new_value=winner.value, source=winner.source,
                  event_type=event_type,
                  decision_group_id=decision_group_id,
                  actor_type=ActorType.SYSTEM_JOB,
                  actor_label="system:apply_receipt_extraction",
                  metadata=winner.metadata,
              )
          # If winner is the prior accepted value, NO new event is written.
          # Per Decision #4: "stored is not a source" — preserve the prior
          # event's lineage instead of writing a new event claiming
          # source='stored'.
      receipt.ocr_confidence = result.confidence  # not tracked
      receipt.status = result.status              # not tracked
      receipt.needs_clarification = ...           # not tracked
      receipt.updated_at = utc_now()              # not tracked
  ```

#### 2. `import_legacy_receipt_mapping`

- **Lines:** `legacy_receipts.py:70–116`
- **Trigger:** one-shot CLI script run by operator/finance to backfill historical CSV rows.
- **Current shape:** loops over CSV rows, for each row runs `setattr(receipt, col, value)` for every tracked column, then `session.add` per row, single `session.commit()` at the end.
- **Source attribution:** `Source.LEGACY_UNKNOWN_CURRENT` — by the time this script runs, the original lineage is unrecoverable; same convention as Day 3a's backfill.
- **Actor attribution:** `ActorType.SYSTEM_MIGRATION`, `actor_label = "system:legacy-csv-import"`.
- **Refactored shape:** wrap each receipt's worth of writes in one `with session.begin():` block, one `decision_group_id` per receipt (matches Day 3a's per-receipt grouping). Use `write_tracked_field(...)` for each non-None column.

#### 3. `answer_question` (the user-edit path)

- **Lines:** `clarifications.py:193–368`
- **Trigger:** user replies to a clarification question. Source channel determined by `question.user_id` lookup (Telegram-bound vs web-bound user). Both web and Telegram funnel into this single function.
- **Current shape:** large branch on `question.question_key`. Each branch potentially mutates one tracked column (sometimes two — `local_amount` branch sets both `extracted_local_amount` and `extracted_currency`). Single `session.commit()` at line 365 covers everything.
- **Source attribution:** `Source.USER_TELEGRAM` if the upstream channel was Telegram, `Source.USER_WEB` otherwise. Caller (route handler) decides; passed into the function.
- **Actor attribution:** for Telegram, `ActorType.TELEGRAM_USER` with `actor_label = f"telegram:{telegram_user_id}"`; for web (pre-SSO), `ActorType.UNAUTHENTICATED_USER` with `actor_label = f"web:{ip_or_session_id}"` per design §4. Web actor labels need a stable identifier — current operator UI doesn't have SSO yet. **See open question Q3.**
- **Refactored shape:** add `source` and `actor_type` / `actor_label` parameters to the function signature; wrap the body in `with session.begin():`; replace each direct attribute assignment with a `write_tracked_field(...)` call. The branches that write *two* columns (e.g., `local_amount` answer parses into `extracted_local_amount` + `extracted_currency`) emit two events under one shared `decision_group_id` — both ACCEPTED (or OVERRIDDEN if the field had a prior current value).

#### 4. `create_manual_statement_transaction`

- **Lines:** `routes/statements.py:189–191`
- **Trigger:** operator creates a manual statement transaction in the web UI; if the form includes a `business_reason` linked to a receipt, the receipt's `business_reason` column is written.
- **Current shape:** conditional update inside a larger transaction-handling block. Calls `session.add` + `session.commit` at the end.
- **Source attribution:** `Source.USER_WEB`.
- **Actor attribution:** `ActorType.UNAUTHENTICATED_USER`, `actor_label = f"web:{operator_session_id}"` — same as path 3.
- **Refactored shape:** trivial — single tracked field, single `write_tracked_field(...)` call inside the existing transaction wrapped as `with session.begin():`.

#### 5. `update_receipt` (PATCH endpoint) — **highest-risk path**

- **Lines:** `routes/receipts.py:59–73`
- **Trigger:** `PATCH /receipts/{id}` with a `ReceiptUpdate` schema. Currently used by the operator web UI to amend any field on a receipt.
- **Current shape:** generic `setattr` loop over `payload.model_dump(exclude_unset=True).items()`. Any of the 9 tracked columns can be written; non-tracked fields can also be written via the same loop.
- **Source attribution:** `Source.USER_WEB`.
- **Actor attribution:** `ActorType.UNAUTHENTICATED_USER`, `actor_label = f"web:{operator_session_id}"`.
- **Risk:** this is the worst case for the invariant — a generic loop that doesn't know which fields are tracked. Easy to forget the corresponding event when adding a new field to `ReceiptUpdate`. Refactored shape must enumerate the 9 tracked fields explicitly and route them through `write_tracked_field`; non-tracked fields stay on the plain `setattr` path.
- **Refactored shape (sketch):**

  ```
  payload_dict = payload.model_dump(exclude_unset=True)
  with session.begin():
      decision_group_id = uuid.uuid4().hex
      for field, value in payload_dict.items():
          if field in TRACKED_FIELDS:
              write_tracked_field(
                  session, receipt, field_name=FieldName(field),
                  new_value=value, source=Source.USER_WEB,
                  decision_group_id=decision_group_id,
                  actor_type=ActorType.UNAUTHENTICATED_USER,
                  actor_label=actor_label,
              )
          else:
              setattr(receipt, field, value)
      receipt.updated_at = utc_now()
  ```

#### 6. `classify_existing_receipts.py` (CLI script)

- **Lines:** `scripts/classify_existing_receipts.py:41–143`
- **Trigger:** one-shot CLI; runs `vision_extract` on receipts that have `receipt_type IS NULL` and writes the classification result.
- **Current shape:** loops over candidate receipts, conditionally assigns `receipt.receipt_type = result`, single `session.commit()` at end.
- **Source attribution:** `Source.VISION` — the script's whole point is to populate the column from the vision pipeline.
- **Actor attribution:** `ActorType.SYSTEM_JOB`, `actor_label = "system:classify-existing-receipts"`.
- **Refactored shape:** identical to path 1's vision-side. Wrap each receipt in `with session.begin():`, emit a `PROPOSED` event with full vision metadata, then ACCEPTED for the merge winner. Receipts where `vision_extract` returns `None` produce no events.

#### 7. `_sync_review_rows` (read-only against tracked cols)

- **Lines:** `review_sessions.py:326–423`
- **Trigger:** initial review session creation or re-sync when statement transactions change.
- **Current shape:** reads tracked-column values from receipts, serializes into `ReviewRow.source_json` / `suggested_json`. Writes go to `ReviewRow.*`, not to `ReceiptDocument` tracked columns.
- **No refactor needed for the invariant.** The tracked-column reads are not mutations. (When Day 3c snapshot logic lands, the *snapshot itself* will produce `event_type=SNAPSHOTTED` events — but that's Day 3c, not 3b.)

---

## 2. Merge logic refactor — `apply_receipt_extraction`

### 2.1 Current state (today, post-Day-3a)

`extract_receipt_fields` produces a `ReceiptExtraction` dataclass by:

1. Running deterministic regex parsers (`_parse_date`, `_parse_amount`, `_parse_merchant`, `_parse_business_or_personal`) over `caption + filename + storage path stem`.
2. If any of `(date, amount, supplier)` is still missing, calling `model_router.vision_extract(storage_path)` — the staged mini→full pipeline.
3. Computing per-field merge per the docstring rule: **previously-stored value > deterministic > vision**, with two corrections:
   - For `content_type=="document"` (PDF), vision wins over deterministic for `supplier` (filenames are upload IDs / customer names, not merchants).
   - For `receipt_type`, vision only fills when the column is `NULL`; existing classifications are preserved unconditionally.

`apply_receipt_extraction` then assigns the merged values to the receipt and commits.

### 2.2 Refactored shape

Single `decision_group_id` per call. Every candidate from every source produces a `PROPOSED` event under that group. The merge winner produces either:

- `ACCEPTED` — first time the field acquired a current value, OR field was previously NULL.
- `OVERRIDDEN` — a prior `ACCEPTED`/`OVERRIDDEN` event exists for this `(receipt, field)` AND the new winning value differs from the prior accepted value.
- *(no event)* — the prior accepted value is still the merge winner. Per Decision #4, "stored is not a source"; the prior event's lineage is preserved instead of writing a new event claiming `source='stored'`.

#### Per-field algorithm

For each tracked field that the merge function considers:

```
prior_event   = get_current_event(session, RECEIPT, receipt.id, field)
prior_value   = prior_event.value (deserialized) if prior_event else None
det_value     = deterministic candidate or None
vision_value  = vision candidate or None

# Emit PROPOSED for every candidate. (The prior accepted value is NOT
# re-proposed — it's already on the ledger as ACCEPTED/OVERRIDDEN, and
# proposing it again would be noise. "Stored is not a source.")
if det_value is not None:
    record_field_event(... event_type=PROPOSED, source=DETERMINISTIC, value=det_value ...)
if vision_value is not None:
    record_field_event(... event_type=PROPOSED, source=VISION, value=vision_value, metadata=vision_meta ...)

# Pick winner per the existing precedence rule.
winner = pick_winner(prior_value, det_value, vision_value, field, content_type)

# Emit ACCEPTED/OVERRIDDEN only when the winner is a NEW value.
if winner is None:
    pass  # no candidates produced a value
elif prior_value is None:
    # First time the field gets a current value.
    write_tracked_field(... new_value=winner.value, source=winner.source, event_type=ACCEPTED ...)
elif winner.value != prior_value:
    # Re-extract picked a different value than what's stored. Override.
    write_tracked_field(... new_value=winner.value, source=winner.source, event_type=OVERRIDDEN ...)
else:
    # Winner == prior accepted value. NO new event.
    # The column is already correct; the prior event's lineage stands.
    pass
```

#### "Stored is not a source"

When merge sees a non-NULL existing column value, it consults `get_current_event` to load the prior `ACCEPTED`/`OVERRIDDEN` event. The current value's *lineage* (who originally accepted it, when, with what metadata) is what matters — the column itself isn't a "source" because there's no provenance attached to a column read.

**Query used:** `app.services.field_provenance.get_current_event(session, entity_type=RECEIPT, entity_id=receipt.id, field_name=field)`. Returns the most recent `ACCEPTED`/`OVERRIDDEN` event ordered by `(created_at DESC, id DESC)`.

For backfilled receipts the prior event will be the `legacy_unknown_current` event from Day 3a — its presence is enough to know "the current value has lineage on the ledger, don't re-write it." For post-Day-3b receipts the prior event will be a real source-attributed event.

### 2.3 Worked example (production scenario)

Receipt id=5, content_type="photo". Vision extracts amount=419.58 TRY with confidence 0.92. Deterministic regex over the caption/filename also produces amount=420.00 TRY. The receipt is fresh (no prior accepted event for `extracted_local_amount`).

```
decision_group_id = "f4c9..."  # one fresh UUID hex per call

with session.begin():
    # PROPOSED — deterministic
    record_field_event(
        session,
        entity_type=RECEIPT, entity_id=5,
        field_name=EXTRACTED_LOCAL_AMOUNT,
        event_type=PROPOSED, source=DETERMINISTIC,
        value=Decimal("420.0000"),
        decision_group_id="f4c9...",
        actor_type=DETERMINISTIC_PIPELINE,
        actor_label="deterministic:apply_receipt_extraction",
    )
    # PROPOSED — vision
    record_field_event(
        session,
        entity_type=RECEIPT, entity_id=5,
        field_name=EXTRACTED_LOCAL_AMOUNT,
        event_type=PROPOSED, source=VISION,
        value=Decimal("419.5800"),
        confidence=0.92,
        decision_group_id="f4c9...",
        actor_type=VISION_PIPELINE,
        actor_label="vision:apply_receipt_extraction",
        metadata={
            "model_name": "gpt-5.4-mini",
            "model_version": "2026-04-15",
            "prompt_version": "v3",
            "extraction_run_id": "<uuid>",
            "input_file_hash": "<sha256-hex>",
            "escalated": False,
        },
    )
    # Winner: deterministic (420.00) per merge precedence (det > vision).
    # No prior event for this field on this receipt → ACCEPTED.
    write_tracked_field(
        session, receipt,
        field_name=EXTRACTED_LOCAL_AMOUNT,
        new_value=Decimal("420.0000"),
        source=DETERMINISTIC,
        event_type=ACCEPTED,
        decision_group_id="f4c9...",
        actor_type=SYSTEM_JOB,
        actor_label="system:apply_receipt_extraction",
    )
```

Result: 3 events on the ledger sharing `decision_group_id="f4c9..."`:
- 1 PROPOSED from DETERMINISTIC (val=420.00)
- 1 PROPOSED from VISION (val=419.58, confidence=0.92, full metadata)
- 1 ACCEPTED from DETERMINISTIC (val=420.00) — the merge winner

`receipt.extracted_local_amount` is set to `Decimal("420.0000")`. The M3 approval UI can render the decision group to show "deterministic chose 420.00 over vision's 419.58 with 0.92 confidence" — full audit lineage.

### 2.4 Why we don't write `REJECTED` events for the losers

Decision: PR-1 does not write `REJECTED` events for losing candidates in `apply_receipt_extraction`. The losing PROPOSED events are sufficient to render lineage in the M3 UI (the UI knows: "if there's a PROPOSED with no matching ACCEPTED winner, the candidate was passed over"). Adding a `REJECTED` event per loser would double the event count without adding information.

The `REJECTED` event type stays in the enum and is reserved for cases where a candidate is *explicitly* discarded by a human (e.g., M3 approval UI: operator looks at a vision proposal and says "no, that's wrong"). That use case lands in M3.

---

## 3. Vision reproducibility metadata schema

### 3.1 Required keys (all six)

For every `record_field_event(..., source=VISION, ...)` call, `metadata` must be a dict containing all six keys. Wrapper enforces this; missing key → `TypeError` listing the missing keys.

| Key | Type | Example | Source of value | When determined |
|---|---|---|---|---|
| `model_name` | `str` | `"gpt-5.4-mini"` | `model_router.MINI_MODEL` or `FULL_MODEL` constant — whichever tier produced the fields | OCR call time |
| `model_version` | `str` | `"2026-04-15"` | OpenAI API response `system_fingerprint` field, OR a vendor-supplied version string. **See open question Q1.** | OCR call time |
| `prompt_version` | `str` | `"v3"` | Module-level constant `model_router.VISION_PROMPT_VERSION` | OCR call time (compile-time) |
| `extraction_run_id` | `str` (UUID hex) | `"a3f1...c2e9"` | `uuid.uuid4().hex` generated once per `vision_extract()` call | OCR call time |
| `input_file_hash` | `str` (SHA-256 hex) | `"7e3a...09f2"` | SHA-256 over the receipt file's bytes | OCR call time (computed inside `vision_extract` before the API call). **See open question Q2.** |
| `escalated` | `bool` | `False` | `VisionResult.escalated` | OCR call time |

### 3.2 Per-key rationale

- **`model_name`** — without it, "we re-ran this and got a different answer" is unattributable. Use the actual tier that produced the result (mini or full), not the configured default.
- **`model_version`** — needed because OpenAI ships incremental updates under the same model_name. Reproducibility requires the exact build identifier when available. If the API does not return one, store the empty string `""` (still a present key, just an empty value) and accept that this is a known reproducibility gap.
- **`prompt_version`** — when we change the prompt, every event written before the change becomes a different "model" for audit purposes. Internal-versioning means we own the version namespace; we don't need to depend on git SHAs or commit dates.
- **`extraction_run_id`** — one OCR pass produces multiple field events (date, supplier, amount, etc.). Linking them via a single run id makes "show me everything vision saw in this single pass" a one-query operation. Distinct from `decision_group_id` because the run id stays consistent even if merge logic later considers each field in a separate decision (today's merge is per-pass, but future merge logic might be per-field).
- **`input_file_hash`** — receipts get re-uploaded, names change, paths rotate. The content hash is the only stable identity. Two receipts with the same file hash and the same `(model_name, model_version, prompt_version)` should produce the same fields; if they don't, it's a model-stochasticity bug, not an input-divergence bug.
- **`escalated`** — needed to attribute confidence properly. A field accepted from the full model carries different confidence than the same field from the mini model.

### 3.3 Wrapper validation

In `app.services.field_provenance.py`, add to `record_field_event`:

```
if source is Source.VISION:
    _validate_vision_metadata(metadata)
```

Helper:

```
_REQUIRED_VISION_METADATA_KEYS = frozenset({
    "model_name", "model_version", "prompt_version",
    "extraction_run_id", "input_file_hash", "escalated",
})

def _validate_vision_metadata(metadata: dict | None) -> None:
    if not isinstance(metadata, dict):
        raise TypeError(
            "vision-source events require metadata dict with required keys; "
            f"got {type(metadata).__name__}"
        )
    missing = _REQUIRED_VISION_METADATA_KEYS - metadata.keys()
    if missing:
        raise TypeError(
            f"vision-source event metadata missing required keys: {sorted(missing)}"
        )
```

The check is `keys() ⊇ required` (extra keys allowed). Type-checking each value is **not** done at the wrapper layer — that is what per-source Pydantic schemas would buy, and they are explicitly out of scope per Decision #4 of Day 3a (deferred to M3+). The keys-present check is the smallest enforceable contract that catches the most common write-side mistake: forgetting one of the keys.

### 3.4 `model_router.vision_extract` return-shape change

`VisionResult` currently exposes `(fields, model, escalated, notes)`. PR-1 extends it with the four reproducibility fields:

```
@dataclass(frozen=True)
class VisionResult:
    fields: dict[str, Any]
    model: str
    escalated: bool
    notes: list[str]
    # NEW (Day 3b PR-1):
    model_version: str            # vendor-supplied or "" if unavailable
    prompt_version: str           # module-level VISION_PROMPT_VERSION
    extraction_run_id: str        # uuid4().hex, fresh per call
    input_file_hash: str          # sha256 hex of file bytes
```

Callers (`apply_receipt_extraction`, `classify_existing_receipts.py`) build the metadata dict from these four fields plus `model` and `escalated`:

```
vision_meta = {
    "model_name": vision_result.model,
    "model_version": vision_result.model_version,
    "prompt_version": vision_result.prompt_version,
    "extraction_run_id": vision_result.extraction_run_id,
    "input_file_hash": vision_result.input_file_hash,
    "escalated": vision_result.escalated,
}
```

This dict is then passed verbatim into every PROPOSED/ACCEPTED vision event for fields that vision proposed in this run.

### 3.5 `prompt_version` placement

**Decision: module-level constant in `model_router.py`.**

```
# backend/app/services/model_router.py
VISION_PROMPT_VERSION = "v3"
_VISION_PROMPT = (
    "You are extracting structured fields from a receipt image. "
    ...
)
```

Rationale:
- Co-locating the version constant with the prompt string forces a developer who edits the prompt to consider the version bump. PR review catches the omission.
- A separate `prompts/` module would be cleaner if we had multiple prompts under independent versioning (we don't — one vision prompt today; a matching prompt under a separate version namespace; a synthesis prompt under another). When that count grows past ~3, revisit.
- A git-SHA-based version would change every PR, making `prompt_version` useless as a grouping key. An explicit version string changes only when the prompt semantics change.

The matching and synthesis prompts (`_MATCH_PROMPT`, `_SYNTHESIS_PROMPT`) get their own version constants in PR-1 (`MATCH_PROMPT_VERSION`, `SYNTHESIS_PROMPT_VERSION`) for future-proofing, but only `VISION_PROMPT_VERSION` is consumed by Day 3b. The other two are stubs reserved for M3 (matching events) and M5 (report-generation events).

### 3.6 Hash strategy (`input_file_hash`)

**Decision: SHA-256 over the file bytes, computed inside `vision_extract`, just before the API call.**

```
def vision_extract(storage_path: str) -> VisionResult | None:
    path = Path(storage_path)
    if not path.exists():
        return None
    file_bytes = path.read_bytes()
    file_hash = hashlib.sha256(file_bytes).hexdigest()
    extraction_run_id = uuid.uuid4().hex
    # ... rest of existing pipeline ...
```

Rationale: every current call site for `vision_extract` happens while the file is alive on disk. The PM steer "should be at file ingestion, not at OCR call — file may have been deleted by then in some paths" is a forward-looking concern; today there is no path where the file is deleted before OCR runs. **See open question Q2** for the alternative (hash at ingestion, store on a new column).

Hashing is also cheap: receipts are at most a few MB; SHA-256 over 5 MB is sub-millisecond. Re-reading the file inside `vision_extract` is fine because the function already reads the file (for image encoding or PDF rasterization); extracting one extra `read_bytes()` call (or hashing the existing buffer) is negligible.

For PDFs, the hash is over the *original PDF bytes*, not the rasterized PNG bytes. Rationale: the PDF is the canonical input artifact; the rasterization is a reproducible derivation from the PDF + the PDF rasterizer version. Storing the post-rasterization hash would conflate "input changed" with "rasterizer changed."

---

## 4. Tracked-column write helper

**Decision: extract the helper.** PM lean confirmed.

### 4.1 Signature

```
# In backend/app/services/field_provenance.py (alongside record_field_event)

def write_tracked_field(
    session: Session,
    receipt: ReceiptDocument,
    *,
    field_name: FieldName,
    new_value: Any,
    source: Source,
    event_type: EventType,             # ACCEPTED or OVERRIDDEN
    decision_group_id: str,            # required — caller owns grouping
    actor_type: ActorType,
    actor_user_id: int | None = None,
    actor_label: str,
    confidence: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Write a tracked field on a ReceiptDocument and emit the matching event.

    Atomicity contract: caller MUST be inside `with session.begin():`. The
    helper does session.add(receipt) + record_field_event() — both inside
    the caller's transaction.
    """
```

Helper body sketch:

```
column_name = field_name.value  # FieldName values match column names
setattr(receipt, column_name, new_value)
receipt.updated_at = utc_now()
session.add(receipt)
return record_field_event(
    session,
    entity_type=EntityType.RECEIPT,
    entity_id=receipt.id,
    field_name=field_name,
    event_type=event_type,
    source=source,
    value=new_value,
    confidence=confidence,
    decision_group_id=decision_group_id,
    actor_type=actor_type,
    actor_user_id=actor_user_id,
    actor_label=actor_label,
    metadata=metadata,
)
```

### 4.2 Why the helper

- **One place to encode the invariant.** Adding a new tracked field is now: (1) add `FieldName.X`, (2) callers pass `FieldName.X` into the helper. No "and don't forget to add the event" footgun.
- **Atomicity is harder to forget.** If a caller forgets `with session.begin():`, the underlying `record_field_event` will raise `RuntimeError` from `session.flush()` — but the column write would still happen. The helper documents the contract explicitly in its docstring; tests pin it.
- **Decision_group_id required.** The helper takes `decision_group_id` as a required keyword (no default). Caller must allocate one explicitly per logical decision. This is more work than letting `record_field_event` auto-generate a group id, but it forces callers to think about grouping — and grouping is what makes audit lineage queryable.

### 4.3 What the helper does NOT do

- It does **not** look up the prior event to decide ACCEPTED vs OVERRIDDEN. The caller decides that. Rationale: in `apply_receipt_extraction`, the merge function already needs to consult the prior event to compare values and detect "winner == prior" (no-op) cases; computing ACCEPTED-vs-OVERRIDDEN at the same time is free. Pushing that decision into the helper would duplicate the lookup.
- It does **not** validate `actor_type`/`actor_user_id` consistency (e.g., enforcing `actor_user_id IS NULL` for `SYSTEM_*` actors and `actor_user_id IS NOT NULL` for `*_USER` actors). That's deferred-backlog item #5 from the PR #27 review.
- It does **not** auto-detect "value unchanged → skip event." Caller decides whether to call the helper or skip. Rationale per §5 below.

### 4.4 Placement

`backend/app/services/field_provenance.py`, alongside `record_field_event`. Same module so callers import once: `from app.services.field_provenance import write_tracked_field, record_field_event, get_current_event`.

---

## 5. Edge cases (per path)

### Same column written twice in one transaction

**Behavior:** allowed, produces two events under the same `decision_group_id`. The second one is OVERRIDDEN (since the first one made the column non-NULL).

**Realistic case:** the `local_amount` clarification answer parses both `extracted_local_amount` and `extracted_currency` in one go. These are two *different* columns, so two events at most — not two events on the same column. True same-column-twice would be a programming error; tests don't need to assert this is rejected, but `test_invariant_column_equals_latest_event` will still pass (latest event wins).

### User edit produces same value as current column (no-op edit)

**Decision: skip the event.** If `new_value == prior_value`, do not call `write_tracked_field`. The column is already correct; the prior event's lineage stands. This is the same "stored is not a source" principle as in merge logic.

**Test:** `test_user_edit_with_same_value_emits_no_event`.

**Caller responsibility:** every user-edit path (paths 3, 4, 5) compares the new value to `receipt.<column>` before calling the helper. The helper itself does NOT do this comparison — see §4.3 rationale.

### Vision extracts but every field is None/null

**Behavior:** `vision_extract` returns `VisionResult(fields={}, ...)` or `None`. No PROPOSED events written for vision (no candidates). No ACCEPTED for fields where deterministic also has nothing. If deterministic produces values, those flow through normally as PROPOSED + ACCEPTED. If neither produces values, no events written and the receipt's columns stay NULL. The receipt is marked `needs_clarification=True` (existing behavior).

**Test:** `test_apply_receipt_extraction_no_vision_candidates_emits_only_deterministic_events`.

### Merge decides to keep existing value

**Behavior:** no new ACCEPTED or OVERRIDDEN event. The PROPOSED events from this run are still written (so the audit trail shows "vision proposed X, deterministic proposed Y, but the stored value was preserved"). The prior accepted event stays current.

**Test:** `test_apply_receipt_extraction_preserves_prior_accepted_value_writes_no_acceptance_event`.

### `apply_receipt_extraction` runs on a receipt with zero events (pre-Day-3a state)

**Hard assumption:** cannot happen. Production data was backfilled at Day 3a deploy; every existing receipt with non-NULL tracked fields has a `legacy_unknown_current` accepted event. Any new receipt created post-Day-3b will get its first events via the refactored extraction path.

**Defensive code:** none. If a receipt somehow has a non-NULL tracked column but no prior event, `get_current_event` returns `None` → merge treats the column as "first time" → emits ACCEPTED. The column-vs-event invariant catches the inconsistency post-write.

**Test (defensive):** `test_apply_receipt_extraction_on_unbackfilled_receipt_treats_as_first_acceptance` — exercises a synthetic receipt with non-NULL columns but no events. Confirms the merge function doesn't crash; the invariant fixture catches the inconsistency on the next test that touches it.

### PATCH with unknown field in payload

**Behavior:** `payload.model_dump(exclude_unset=True)` is the source of truth. If a key isn't in `TRACKED_FIELDS`, route it to plain `setattr`. If `TRACKED_FIELDS` is correctly defined, this is safe. The risk is that an attacker (or a buggy caller) sends a payload with an unexpected key — Pydantic's `ReceiptUpdate` schema rejects unknown fields by default, so this is constrained.

### `legacy_receipts.py` re-run on a partially-imported DB

**Behavior:** out of scope for PR-1. Path 2 is a one-shot script. If it's re-run, behavior depends on the script's existing dedup logic (out of scope here). The provenance refactor doesn't change re-run safety; it inherits whatever guarantees (or absence of guarantees) the script provides today.

### Concurrency: two writers on the same receipt

**Out of scope.** SQLite serializes writes; the app is single-process today. When concurrent writers become a concern (M2+ deployment), the invariant fixture will catch double-writes that violate the column-event correspondence.

---

## 6. Test plan

### 6.1 Promote `test_invariant_column_equals_latest_event` to autouse fixture

Today the invariant test runs once after the Day 3a backfill and asserts the column-event correspondence on the synthetic 13-receipt DB.

PR-1 promotes it to a per-test `autouse` fixture in `backend/tests/conftest.py`:

```
@pytest.fixture(autouse=True)
def assert_provenance_invariant_after(isolated_db, request):
    yield
    # Skip the post-test check for tests that are explicitly testing
    # invariant violations or working with the DB pre-migration.
    if request.node.get_closest_marker("skip_invariant"):
        return
    with Session(isolated_db) as session:
        violations = check_invariant(session)
    assert violations == [], (
        f"INVARIANT VIOLATIONS in {request.node.name}: "
        f"{len(violations)} (receipt, field) pairs broke the column⇆event "
        f"correspondence. Most likely a tracked-column write happened "
        f"outside write_tracked_field() / record_field_event(). "
        f"Sample: {violations[:5]}"
    )
```

This fixture catches *every* missed write path during PR-1 implementation. If a refactored function still does a bare `receipt.column = X` without an event, the next test that touches that receipt fails the invariant check post-yield.

`@pytest.mark.skip_invariant` exists for tests that intentionally produce invariant-violating state (e.g., the test that exercises the partial-state branch of the migration).

### 6.2 New test files

| File | Tests | Purpose |
|---|---|---|
| `test_apply_receipt_extraction_records_events.py` | ~10 | Per merge-precedence scenario: vision-only, deterministic-only, both-with-vision-winning, both-with-deterministic-winning, user-edit-overrides-existing, value-unchanged-no-event. Each asserts the correct PROPOSED + ACCEPTED/OVERRIDDEN events under shared `decision_group_id`. |
| `test_vision_metadata_required_on_vision_events.py` | ~8 | Parametrized over the 6 required metadata keys: omit each one, assert `TypeError` listing the missing key. Plus: extra keys allowed; `None` metadata rejected; wrong type for metadata rejected. |
| `test_decision_group_lineage_preserved.py` | ~5 | "Vision sets amount → user overrides via web → user re-overrides via Telegram"; assert `get_field_history` returns the chain newest-first with correct `(source, actor)` pairs. |
| `test_write_tracked_field_helper.py` | ~6 | Helper-specific: ACCEPTED on first write, OVERRIDDEN on second write, atomicity (rollback drops both column and event), metadata round-trip, kw-only signature, missing `decision_group_id` raises. |

### 6.3 Updated test files

| File | Change |
|---|---|
| `test_clarifications_*` (existing) | Each test now lives under the autouse invariant fixture. Tests that previously asserted "column was set" now also assert "matching event was written." Source/actor attribution checked. |
| `test_review_rows_*` (existing) | No production code change for path 7 (read-only), but tests that mutate via the test setup must use `write_tracked_field` if they touch tracked columns. |
| `test_telegram_handlers_*` (existing) | Same — the handlers funnel into `answer_question`, so the contract is enforced at that layer. Add at least one test asserting the event has `actor_type=TELEGRAM_USER` and a real `actor_label`. |
| `test_receipt_extraction.py` (existing) | Replace direct attribute assertions with `get_current_event`-style assertions. The tests that exercised the merge precedence rule survive with stronger checks. |
| `test_field_provenance_invariant.py` (existing) | Keep the explicit-call test as a smoke test, but the load-bearing detection moves to `conftest.py`. |

### 6.4 Estimated test delta

- New tests: ~30
- Tests that pick up implicit invariant enforcement via the autouse fixture: ~150 existing tests across the suite that mutate a Receipt
- Net suite size after PR-1: ~225 (was 195 + ~30 new). Some existing assertions are replaced with stronger ones; net is +30.

### 6.5 Test-side gotchas to watch for during implementation

- The autouse fixture queries the entire DB every test. With 195 tests in the suite, that's 195 × O(receipts × fields) queries. Past 1000 tests / 100 receipts this becomes a measurable suite-time cost (probably 1–2 seconds added to total runtime). Mitigation: index the (entity_type, entity_id) lookup — already covered by the composite index from Day 3a. No action needed for PR-1.
- Tests that intentionally manipulate the DB outside the model (e.g., raw `conn.execute("UPDATE receiptdocument SET ...")`) need `@pytest.mark.skip_invariant` to bypass the check.
- The migration test (`test_m1_day3a_migration.py`) builds a synthetic DB with raw SQL; its tests that *don't* run the migration are inherently invariant-violating (table doesn't exist, no events). Those need `@pytest.mark.skip_invariant`.

---

## 7. Migration strategy

PR-1 is **code-only**. The `fieldprovenanceevent` table already exists in production (from Day 3a). New events written by refactored code accumulate in the existing table. No schema migration script needed.

### 7.1 Pre-Day-3b lineage gap (documented, not addressed)

The 13 production receipts existing as of Day 3a deploy each have one `legacy_unknown_current` event per non-NULL tracked field — 108 events total. Their original lineage (which vision model proposed what; which deterministic regex matched first; etc.) is unrecoverable.

Post-Day-3b PR-1 deploy:
- New receipts get full lineage from upload onward.
- Existing 13 receipts stay locked into their `legacy_unknown_current` accepted events. If any of these receipts is edited via a user-edit path (clarification answer, PATCH), the edit produces a new OVERRIDDEN event with real lineage — but everything *before* that edit remains "we don't know."
- A re-extract via `POST /receipts/{id}/extract` on an existing receipt produces fresh PROPOSED events from the current vision/deterministic pipeline. Whether this changes the accepted value depends on the merge precedence: if the existing column value still wins, no ACCEPTED event is written; if the new run produces a different value, OVERRIDDEN is written with real metadata.

### 7.2 What M3 approval UI must know

Surface this gap in the M3 design pass. UX recommendation: when rendering provenance for an event whose only ledger entry is a `legacy_unknown_current` event, the badge reads "imported from pre-Day-3b state — original lineage not captured" rather than "vision: <model_name>".

### 7.3 No backfill of "what vision said in 2026-04"

Reconstructing pre-Day-3b vision proposals is explicitly out of scope (per §8). The cost of re-running vision on every existing receipt (13 today, will grow) just to write fictional `PROPOSED` events would attribute lineage to a *new* OCR pass, not the original — which is exactly the lie we're trying not to tell. The honest answer remains `legacy_unknown_current`.

---

## 8. Out of scope (explicit)

- **§8.7 partial-expense fields** (`claimed_local_amount`, `receipt_total_local_amount`) → Day 3b PR-2 (separate design pass).
- **UI badges for provenance display** → Day 3c.
- **XLSX Audit Trail sheet** → Day 3c.
- **`ExpenseReport.submit()` snapshot logic** (`event_type=SNAPSHOTTED`) → Day 3c.
- **Per-source formal Pydantic schemas for `metadata_json`** → M3+ if ever (current freeform validation is sufficient for PR-1).
- **Linter / static-analysis rule** enforcing "no tracked-column write outside `write_tracked_field`" → M2 (pylint plugin or custom AST rule).
- **Reconstructing pre-Day-3b lineage** from existing `legacy_unknown_current` events → never (the absent data is acceptable loss).
- **Concurrency / write-lock semantics** → M2+ deployment.
- **`actor_type`/`actor_user_id` cross-validation** (e.g., enforcing `actor_user_id IS NULL` for `SYSTEM_*` actors) → deferred-backlog item from PR #27 review.
- **The `legacy_receipts.py` script** is touched only enough to use `write_tracked_field`; its broader refactor (idempotency, dry-run flag, etc.) is its own follow-up.

---

## 9. Open questions for PM

### Q1. `model_version` source — vendor fingerprint or hardcoded?

The OpenAI Chat Completions API returns a `system_fingerprint` field per response (e.g., `"fp_44709d6fcb"`). It changes when OpenAI rolls out a model update, so it is the closest-to-canonical version identifier we can get.

**Recommended answer:** thread `system_fingerprint` through from the API response into `VisionResult.model_version`. If the field is missing or `None` (some endpoints/responses don't return it), fall back to the empty string `""` and accept the gap. Document the fallback.

**Alternative:** hardcode a manually-maintained version string (`"2026-04-15"`) — accurate at PR-1 merge time, stale within a week. **Don't recommend.**

### Q2. `input_file_hash` — compute at OCR time or at file ingestion?

PM steer in the prompt: "Should be at file ingestion, not at OCR call — file may have been deleted by then in some paths."

**Trade-off:**
- **Compute at OCR time** (recommended for PR-1): no schema change, simpler. Risk: hypothetical future paths where the file is deleted before OCR. Today no such path exists.
- **Compute at ingestion**: requires a new `file_sha256` column on `ReceiptDocument`. Schema migration. Robust against future file-deletion paths.

**Recommended answer:** compute at OCR time for PR-1 to keep PR-1 code-only. Add `file_sha256` column in PR-2 alongside §8.7 fields if file deletion ever becomes a real concern, OR if M3 approval UI wants to display "this is the hash of the file that produced the lineage" without re-reading the file.

### Q3. Web user `actor_label` — what's the stable identifier today?

Pre-SSO web users have no real identity. Current operator UI uses cookie-based session ids (no auth). Options for `actor_label` on USER_WEB events:

1. `f"web:session:{cookie_session_id}"` — stable per-session, no PII, easy.
2. `f"web:ip:{request.client.host}"` — stable per-source-IP, leaks PII (the operator's office IP).
3. `f"web:anon"` — single bucket; loses per-user attribution.

**Recommended answer:** option 1 (`web:session:{cookie_session_id}`). When SSO lands in M2, the migration story is "new events use `web:user:{sso_id}`, old events keep their `web:session:*` labels." Past SSO migrations have proven this is fine — the label is a durable identifier, not a foreign key.

If the operator UI doesn't currently set a session cookie, PR-1 adds one (single line in the FastAPI middleware). Implementation note, not blocking on PM ack.

### Q4. PR-1 size ballpark — split further?

Estimated diff: 6 production code paths refactored, 1 helper added, 1 conftest change, ~30 new tests, ~150 tests get implicit invariant enforcement.

Probably ~+1500/-300 lines. Comparable to PR #27.

**Recommended answer:** ship as one PR. Splitting "refactor merge logic" from "refactor user-edit paths" creates a temporary state where some paths emit events and others don't, which makes the invariant fixture useless during the gap. Land all six paths together; the autouse fixture is only safe when every path is wrapped.

### Q5. Where do we host the `TRACKED_FIELDS` set?

Currently `app.provenance_enums.FieldName` enumerates all tracked field values. The PATCH endpoint (path 5) needs to ask "is this column name a tracked column?" — i.e., a string-keyed lookup against the enum.

**Recommended answer:** add a module-level `TRACKED_RECEIPT_FIELDS: frozenset[str] = frozenset({f.value for f in [...]})` in `provenance_enums.py`, listing the 9 receipt-side `FieldName` members (excluding the reserved future values like `VAT_AMOUNT`). The migration script's `MONEY_FIELD_NAMES` already follows this pattern; same trick applied to the receipt-tracked subset.

### Q6. Should `write_tracked_field` infer `event_type` (ACCEPTED vs OVERRIDDEN) automatically?

§4.3 says no. But there's an alternative: helper consults `get_current_event` and decides. Simpler caller code at the cost of one extra DB lookup per write.

**Recommended answer:** keep it caller-decided per §4.3. The merge function in `apply_receipt_extraction` already needs the prior event for its value-comparison check, so it gets the event-type-decision for free. User-edit paths *also* benefit from caller-side knowledge (they often want to skip the write entirely if value unchanged — an inferred ACCEPTED would write an event the caller doesn't want).

A future v2 helper `write_tracked_field_auto(...)` that does the lookup internally could land in M3 if usage patterns argue for it. Not in PR-1.

---

**END OF DESIGN — awaiting PM review.**
