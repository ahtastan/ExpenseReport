# M1 Day 3a — FieldProvenanceEvent foundation: design pass

**Status:** Design pass for PM review. No code written yet.
**Branch:** `design/m1-day3a`. Implementation begins only after PM sign-off.
**Depends on:** M1 Day 2.5 (merged 2026-04-25, commit `a04cdf0`) — Decimal columns and `app.json_utils.DecimalEncoder` are prerequisites.
**Downstream:** M3 (approvals), M4 (policy), M5 (ERP export) all read from this table. Wrong shape now ⇒ re-migration before M3.

---

## 1. `FieldProvenanceEvent` schema

```python
# backend/app/models.py — append to existing models

class FieldProvenanceEvent(SQLModel, table=True):
    """Append-only audit ledger for tracked-field writes.

    Invariant (enforced in service layer): every write to a tracked field
    on receiptdocument / reviewrow / expensereport produces at least one
    event in the same DB transaction. Current state continues to live on
    the product columns; this table is the lineage record.
    """
    __tablename__ = "fieldprovenanceevent"

    id: int | None = Field(default=None, primary_key=True)

    # — entity reference (generic; integrity at app layer) —
    entity_type: str = Field(index=True)            # EntityType enum value
    entity_id: int = Field(index=True)              # FK pattern, no SQL FK
                                                    # because entity_type varies

    # — what changed —
    field_name: str = Field(index=True)             # FieldName enum value
    event_type: str                                 # EventType enum value
    source: str = Field(index=True)                 # Source enum value

    # — value (TEXT-shaped; Decimals via DecimalEncoder convention) —
    value: str | None = Field(default=None, sa_column=Column(Text))
    value_decimal: Decimal | None = Field(           # denormalized for SUM/range
        default=None, sa_column=Column(Numeric(18, 4))
    )
    confidence: float | None = None                  # vision/deterministic only

    # — grouping —
    decision_group_id: str = Field(index=True)       # UUID stored as TEXT;
                                                     # generated per merge run
                                                     # (see §7). Always set,
                                                     # never NULL — see §12.

    # — actor (pre-SSO; see §4) —
    actor_type: str                                  # ActorType enum value
    actor_user_id: int | None = Field(
        default=None, foreign_key="appuser.id", index=True
    )
    actor_label: str                                 # durable identifier;
                                                     # e.g. "telegram:12345",
                                                     # "system:m1-day3a-backfill"

    # — extra structured detail —
    metadata_json: str | None = Field(default=None, sa_column=Column(Text))
    # Stores: vision model id + escalation flag + raw response checksum;
    # source-specific fields like {"row_ref": "diners-2025-08-26-row-7"};
    # backfill marker {"original_created_at": ..., "backfill_reason": ...}.
    # Serialized via app.json_utils.dumps so Decimal-bearing payloads
    # round-trip safely.

    created_at: datetime = Field(default_factory=utc_now, index=True)
```

### Column rationale

| Column | Why |
|---|---|
| `entity_type` + `entity_id` | Generic FK pattern. `entity_type` carries which table the `entity_id` references. We don't use a real SQL FK because the target varies; the trade-off is integrity at the app layer instead of CASCADE-on-delete. Acceptable because we never delete receipts/reports — soft-delete or archive is the M5 pattern. |
| `field_name` | Indexed for the load-bearing query: "give me the most recent accepted event for (entity, field)." |
| `event_type` | Five values (see §5) covering candidate / current / replacement / report-snapshot semantics. Not indexed alone — always queried alongside entity+field. |
| `source` | Indexed because audit reports filter by source ("show me everything vision proposed last week"). |
| `value` (TEXT) | Universal serialization. Decimals via `format(d, 'f')` (matches `app.json_utils` convention). Dates as ISO-8601. Strings as-is. NULL only when the event semantically has no value (e.g., a `rejected` event whose value is "no candidate produced" — rare). |
| `value_decimal` (Numeric(18,4)) | Redundant with `value` for money-shape fields, but enables SQL `SUM(value_decimal) WHERE field_name='extracted_local_amount'` for audit dashboards. Money fields populate both; non-money fields leave it NULL. |
| `confidence` | Only meaningful for `source IN ('vision', 'deterministic')`. NULL for user/system events. |
| `decision_group_id` | Stable group id for "everything that came out of one merge run." Always set (auto-generate UUID even for single-event writes — keeps the query side simple, doesn't cost meaningfully). |
| `actor_type` + `actor_user_id` + `actor_label` | Three fields, one role: identify the responsible party without faking IDs pre-SSO. See §4. |
| `metadata_json` | Catch-all for source-specific reproducibility data (vision model + escalation flag, FX provider response timestamp, manual-finance ticket reference). Avoids sparse-column waste rejected in PM decision #5. |
| `created_at` (indexed) | Time-range queries. Indexed because audit reports always have a "since" filter. |

### Indexes

Primary lookup ("current accepted event for a field"):
```sql
CREATE INDEX ix_fpe_lookup
  ON fieldprovenanceevent (entity_type, entity_id, field_name, created_at DESC);
```

Decision-group retrieval (M3 approval workflow):
```sql
CREATE INDEX ix_fpe_decision_group
  ON fieldprovenanceevent (decision_group_id);
```

Plus the per-column indexes already declared (`entity_type`, `entity_id`, `field_name`, `source`, `actor_user_id`, `created_at`).

**Indexes deliberately NOT added until proven needed:**
- `(actor_type)` — covered by audit needs that don't yet exist.
- `(value_decimal)` — no current "find events with amount > X" use case.
- `(event_type)` alone — always queried with entity context.

### EntityType enum

```python
class EntityType(str, Enum):
    # Initial values used by Day 3a + 3b + 3c
    RECEIPT         = "receipt"          # receiptdocument.id
    REVIEW_ROW      = "review_row"       # reviewrow.id
    EXPENSE_REPORT  = "expense_report"   # expensereport.id

    # — RESERVED FUTURE VALUES (not yet emitted by any code path) —
    # M3 (match-decision provenance): statementtransaction matches
    # STATEMENT_TRANSACTION = "statement_transaction"
    # M3 (approval workflow): which match was accepted/rejected
    # MATCH_DECISION        = "match_decision"
    # M4 (policy decisions): per-row policy verdicts
    # POLICY_DECISION       = "policy_decision"
```

**Day 3a writes only `receipt` events** (the backfill targets receiptdocument). `review_row` and `expense_report` are reserved for Day 3c snapshot work and any user-edit pipeline that targets ReviewRow directly. The reserved values stay commented out in the enum until the code that emits them is written — adding the value before its first writer creates dead enum members that confuse readers.

---

## 2. Field-name enum

```python
class FieldName(str, Enum):
    # Money (current)
    EXTRACTED_LOCAL_AMOUNT = "extracted_local_amount"

    # Categorical (current)
    EXTRACTED_CURRENCY     = "extracted_currency"
    RECEIPT_TYPE           = "receipt_type"
    BUSINESS_OR_PERSONAL   = "business_or_personal"
    REPORT_BUCKET          = "report_bucket"

    # Identity / freeform (current)
    EXTRACTED_DATE         = "extracted_date"
    EXTRACTED_SUPPLIER     = "extracted_supplier"
    BUSINESS_REASON        = "business_reason"
    ATTENDEES              = "attendees"

    # — RESERVED FUTURE VALUES (not yet present in the codebase) —
    # M1 Day 6 (VAT/KDV)
    VAT_AMOUNT             = "vat_amount"
    VAT_RATE               = "vat_rate"

    # M1 Day 7 (FX architecture)
    FX_RATE                = "fx_rate"
    FX_SOURCE              = "fx_source"
    FX_DATE                = "fx_date"

    # M3 (approval/match decisions)
    MATCH_DECISION_ID      = "match_decision_id"
    MANUAL_FINANCE_OVERRIDE = "manual_finance_override"
```

**Total: 9 current + 7 reserved future = 16.** PM said ~12; the actual number depends on whether VAT/FX get split into multiple sub-fields. Listed here so the reader sees the full surface area, but Day 3a only needs to *track* the 9 current ones — the rest are reserved enum values that will start producing events when their columns/logic land.

**Money-field membership** (used by `record_field_event` to auto-populate `value_decimal`):
```python
MONEY_FIELDS = {FieldName.EXTRACTED_LOCAL_AMOUNT, FieldName.VAT_AMOUNT, FieldName.FX_RATE}
```

---

## 3. Source enum

```python
class Source(str, Enum):
    DETERMINISTIC          = "deterministic"
    VISION                 = "vision"
    USER_TELEGRAM          = "user_telegram"
    USER_WEB               = "user_web"
    DINERS_STATEMENT       = "diners_statement"
    ECB                    = "ecb"
    MANUAL_FINANCE         = "manual_finance"
    SYSTEM_MIGRATION       = "system_migration"
    LEGACY_UNKNOWN_CURRENT = "legacy_unknown_current"
```

| Source | When it fires | Typical actor_type | Policy-relevant? |
|---|---|---|---|
| `deterministic` | Regex/parsing extraction in `receipt_extraction.py:_parse_amount` etc. | `deterministic_pipeline` | Yes — high-confidence baseline |
| `vision` | OCR vision model in `model_router.vision_extract` | `vision_pipeline` | Yes — flagged for review when low-confidence |
| `user_telegram` | User answer to a clarification question | `telegram_user` | Yes — operator override |
| `user_web` | Edit via the review-table UI | `web_user` (post-SSO) or `unauthenticated_user` (pre-SSO) | Yes |
| `diners_statement` | Excel import of monthly statement | `system_job` (the import handler) | Yes — provides `local_amount` / `usd_amount` ground truth |
| `ecb` | FX rate fetched from European Central Bank API (M1 Day 7) | `system_job` | Yes — drives USD conversion correctness |
| `manual_finance` | Finance-role override on a closed report (M3+) | `web_user` with finance role | Yes — escapes normal merge precedence |
| `system_migration` | Schema/data migration scripts | `system_migration` | No — bookkeeping |
| `legacy_unknown_current` | Backfill of pre-Day-3a current state | `system_migration` | No — sentinel meaning "we don't know how this got here, but it's the current value as of M1 Day 3a" |

**"Stored" is intentionally absent** (per PM decision #7). Pro's reframe: a "stored" event would be lying about lineage. When merge logic preserves a previously-accepted value (because user edited it, etc.), it should look up the prior accepted event and *preserve* it as current — not write a new event claiming the value came from "stored".

---

## 4. Actor-type enum

```python
class ActorType(str, Enum):
    TELEGRAM_USER          = "telegram_user"
    WEB_USER               = "web_user"
    UNAUTHENTICATED_USER   = "unauthenticated_user"   # pre-SSO browser
    SYSTEM_MIGRATION       = "system_migration"
    SYSTEM_JOB             = "system_job"             # cron, import, FX fetch
    VISION_PIPELINE        = "vision_pipeline"
    DETERMINISTIC_PIPELINE = "deterministic_pipeline"
```

**Confirmed Pro's list as-is.** The two pipeline types feel redundant with `source` (vision/deterministic), but they're not the same thing semantically:
- `source` = where the *value* came from (the data origin).
- `actor_type` = what *process* wrote the row (the responsible code path).

These overlap most of the time but diverge in edge cases — e.g., a backfill script writing legacy vision values has `source=legacy_unknown_current` and `actor_type=system_migration`, not `vision_pipeline`. Keeping them separate preserves audit clarity.

**Pre-SSO mapping** for the existing app:
- Telegram bot writes → `actor_type=telegram_user`, `actor_user_id=<appuser.id from telegram_user_id lookup>`, `actor_label="telegram:<telegram_user_id>"`.
- Web review-table writes (current cookie-only auth) → `actor_type=unauthenticated_user`, `actor_user_id=None`, `actor_label="web:<browser-cookie-hash>"` or `"web:demo"`.
- Vision pipeline → `actor_type=vision_pipeline`, `actor_user_id=None`, `actor_label="vision:<model-id>"`.

**Post-SSO migration** (M2): new `web_user` writes get `actor_type=web_user`, `actor_user_id=<sso user>`, `actor_label="web:<email>"`. Old `unauthenticated_user` events stay as-is. **No re-migration needed** (per PM decision #6).

---

## 5. Event-type enum

```python
class EventType(str, Enum):
    PROPOSED    = "proposed"
    ACCEPTED    = "accepted"
    REJECTED    = "rejected"
    OVERRIDDEN  = "overridden"
    SNAPSHOTTED = "snapshotted"
```

| Event type | Semantics | Typical writer | Cardinality per (entity, field) |
|---|---|---|---|
| `proposed` | "A candidate value was considered." Doesn't change current state on its own. | Any extractor that contributed a candidate to the merge run. | 0..N per merge run; many over an entity's lifetime. |
| `accepted` | "This is now the current value of the field." Always paired with a column write. | Whoever wins the merge. | Exactly 1 per merge run when the field changes; 0 if no candidate produced a value. |
| `rejected` | "A candidate was explicitly considered and discarded." Optional bookkeeping for audit clarity. | Merge logic, when it wants to record *why* a value lost (low-confidence, off-by-too-much, conflicting source priority). | 0..N per merge run. **Optional in Day 3b** — auditor can also infer "rejected" from "any proposed in the same decision group whose value differs from the accepted value." Adding rejected events is encouraged for non-obvious losses. |
| `overridden` | "A previously accepted value is being explicitly replaced." Distinct from a fresh `accepted` because the field already had a current value, and someone (user, finance, system) deliberately changed it. The lineage matters: M3 approval shows "vision said X, user changed to Y on date Z." | User edits, manual_finance corrections. | 0..N over the entity's lifetime. |
| `snapshotted` | "This event was frozen into a report line at submission time." Pointer-style: the snapshot row in the report references this event_id, and the event gets a `snapshotted` marker so it can't be silently mutated later. M3 work. | `ExpenseReport.submit()` — Day 3c, not 3a. | At most 1 per (event_id) — each event is snapshotted at most once. |

**`accepted` vs `overridden` distinction** is important for the audit story:
- `accepted` = first time a field acquired a current value, OR the field was previously NULL.
- `overridden` = field already had a current value (i.e., a previous `accepted` event exists for the same entity+field), and we're replacing it.

The merge logic in Day 3b decides which to write by checking `get_current_event(entity, field)` — if it returns None, write `accepted`; if it returns a prior event, write `overridden`.

---

## 6. Service-layer wrapper API

New module: `backend/app/services/field_provenance.py`

```python
def record_field_event(
    session: Session,
    *,
    entity_type: EntityType,
    entity_id: int,
    field_name: FieldName,
    event_type: EventType,
    source: Source,
    value: Any,                                 # serialized via DecimalEncoder
    confidence: float | None = None,
    decision_group_id: str | None = None,       # auto-generate UUID if None
    actor_type: ActorType,
    actor_user_id: int | None = None,
    actor_label: str,
    metadata: dict[str, Any] | None = None,     # see metadata contract below
) -> int:                                       # returns event id
    """Write one provenance event. Caller owns the surrounding transaction.

    Atomicity contract: this function does session.add() + session.flush()
    only — it does NOT commit. The caller MUST be inside a transaction
    that also includes the corresponding column write to the product
    table (or a no-write event like 'rejected'/'snapshotted'). Day 3b
    refactors the merge logic to honor this contract.

    The required caller pattern is `with session.begin():` (see
    "Atomicity model" below) — NOT manual session.commit() / rollback().

    value_decimal is auto-populated from value when value is a Decimal
    AND field_name is in MONEY_FIELDS. Otherwise NULL.

    metadata contract:
      - Type: dict[str, Any] | None. The wrapper validates this at write
        time and raises TypeError on any other input (including raw
        strings, lists, or pre-serialized JSON). This prevents callers
        from accidentally storing inconsistent encodings.
      - Serialization: the wrapper internally serializes via
        app.json_utils.dumps before storing in metadata_json. Decimal
        values inside metadata round-trip safely; arbitrary nested
        structures are accepted as long as they are JSON-encodable
        through the DecimalEncoder.
      - Read side: callers reading metadata_json should use
        app.json_utils.loads (or json.loads) to deserialize, then pass
        any money-shaped keys through decode_decimal to restore Decimal
        precision. A future helper get_metadata(event) -> dict can
        encapsulate this if reads become common.
    """


def get_current_event(
    session: Session,
    *,
    entity_type: EntityType,
    entity_id: int,
    field_name: FieldName,
) -> FieldProvenanceEvent | None:
    """Return the most recent event of event_type IN ('accepted',
    'overridden') for the given (entity, field). The current value of
    the column should equal this event's value (Day 3b enforces).

    Returns None if no accepted/overridden event has ever been written
    for this field on this entity. Pre-backfill rows return None;
    post-backfill rows return the legacy_unknown_current event.
    """


def get_field_history(
    session: Session,
    *,
    entity_type: EntityType,
    entity_id: int,
    field_name: FieldName,
    limit: int | None = None,
) -> list[FieldProvenanceEvent]:
    """Return all events for (entity, field), newest first."""


def get_decision_group(
    session: Session,
    *,
    decision_group_id: str,
) -> list[FieldProvenanceEvent]:
    """Return every event sharing the decision_group_id, ordered by
    created_at ASC. Used by M3 approval UI to show 'what alternatives
    existed at extraction time.'"""
```

### Atomicity model

The wrapper does **not** open transactions. The caller passes their existing session, and the **required pattern** is `with session.begin():`:

```python
with Session(engine) as session:
    with session.begin():
        receipt.extracted_local_amount = new_value     # column write
        record_field_event(session, ...)               # event write
    # session.begin() commits on context exit; rolls back on any exception.
```

**This is required, not a suggestion.** Manual `session.commit()` / `session.rollback()` is not equivalent — it leaves the column write and the event write in an autoflush-vulnerable window:

| Failure mode without `session.begin()` | Result |
|---|---|
| Column written, then `record_field_event` raises (bad enum, bad metadata, etc.). Caller catches the exception above. | SQLModel autoflush already pushed the column write to the DB on the way INTO `record_field_event`. The column write persists; no event was written. **Invariant violated.** |
| Column written, then `record_field_event` succeeds, then caller code raises before `session.commit()`. | Both writes lost together — invariant holds. But the caller has to remember to call `commit()` and the autoflush window above is still a risk for any flush that happened before commit. |
| `with session.begin():` block + any of the above | Block's context manager calls `rollback()` on exception, re-raises. Both writes are atomic w.r.t. the surrounding transaction. **Invariant holds.** |

The invariant survives all paths only when the column write and the event write are inside the same `session.begin()` block. Day 3b's refactor of `apply_receipt_extraction` will use exactly this pattern; tests in `test_field_provenance_atomicity.py` verify it (see §10).

Forgetting to use `with session.begin():` is a code-review item until M2 adds a linter rule.

### Day 3b enforcement (out of scope for Day 3a but sketched here)

The receipt-extraction merge will become:

```python
def apply_receipt_extraction(session, receipt):
    decision_group = uuid.uuid4().hex
    candidates = []  # list of (source, value, confidence)

    # Run extractors as today
    if det_amount is not None:
        candidates.append((Source.DETERMINISTIC, det_amount, None))
    if vision_amount is not None:
        candidates.append((Source.VISION, vision_amount, vision_confidence))

    with session.begin():
        # Record proposed events for every candidate
        for source, value, conf in candidates:
            record_field_event(
                session,
                entity_type=EntityType.RECEIPT,
                entity_id=receipt.id,
                field_name=FieldName.EXTRACTED_LOCAL_AMOUNT,
                event_type=EventType.PROPOSED,
                source=source,
                value=value,
                confidence=conf,
                decision_group_id=decision_group,
                actor_type=ActorType.DETERMINISTIC_PIPELINE if source == Source.DETERMINISTIC else ActorType.VISION_PIPELINE,
                actor_label=f"{source.value}:auto",
            )

        # Apply merge precedence (today's logic, unchanged)
        extracted = det_amount if det_amount is not None else vision_amount

        # Write the column AND the accepted/overridden event in the same block
        if extracted is not None and extracted != receipt.extracted_local_amount:
            prior = get_current_event(session, ...)
            receipt.extracted_local_amount = extracted
            record_field_event(
                session,
                entity_type=EntityType.RECEIPT,
                entity_id=receipt.id,
                field_name=FieldName.EXTRACTED_LOCAL_AMOUNT,
                event_type=EventType.OVERRIDDEN if prior else EventType.ACCEPTED,
                source=winning_source,
                value=extracted,
                confidence=winning_confidence,
                decision_group_id=decision_group,
                actor_type=...,
                actor_label=...,
            )
    # session.begin() commits on exit, rolls back on any exception above.
```

A code-review checklist (and ideally a linter rule in M2) will verify "no write to a tracked column outside `record_field_event`-aware code." Day 3a doesn't add the linter; Day 3b refactors the merge to use the wrapper.

---

## 7. Decision-group semantics: worked example

PM's scenario: vision proposes amount=419.58 with confidence 0.92, deterministic proposes amount=420.00 with confidence 0.78, vision wins the merge.

Three events written under shared `decision_group_id = 'a1b2c3d4-…'`:

| event_type | source | value | confidence | actor_type | actor_label |
|---|---|---|---|---|---|
| `proposed` | `vision` | `"419.58"` | 0.92 | `vision_pipeline` | `vision:gpt-5.4-mini` |
| `proposed` | `deterministic` | `"420.00"` | 0.78 | `deterministic_pipeline` | `deterministic:_parse_amount` |
| `accepted` | `vision` | `"419.58"` | 0.92 | `vision_pipeline` | `vision:gpt-5.4-mini` |

(All three: `entity_type='receipt'`, `entity_id=42`, `field_name='extracted_local_amount'`, same `decision_group_id`.)

Optionally a fourth event:

| `rejected` | `deterministic` | `"420.00"` | 0.78 | … | `metadata={"reason": "vision had higher confidence"}` |

The `rejected` event is **optional** — the same information is recoverable from "any proposed in this decision_group whose value differs from the accepted." Day 3b can add rejected events when the merge logic has a non-obvious reason to record (e.g., "deterministic value was outside acceptable range").

### M3 approval read-back

```python
events = get_decision_group(session, decision_group_id="a1b2c3d4-…")
# Returns the 3 events above.
# UI groups by event_type and renders:
#   ✓ ACCEPTED   vision         419.58  (conf 0.92, gpt-5.4-mini)
#   ○ proposed   deterministic  420.00  (conf 0.78, _parse_amount)
# Operator can click to override: writes a new event with event_type='overridden',
# source='user_web', new decision_group_id (or reuses this one — see §12 Q3).
```

---

## 8. Backfill plan

### Strategy

For every existing receiptdocument row (13 in production today), for every tracked field that has a non-NULL value, write **one** backfill event:

```python
{
  "entity_type": "receipt",
  "entity_id": <receipt.id>,
  "field_name": <field name from §2>,
  "event_type": "accepted",
  "source": "legacy_unknown_current",
  "value": <serialized current column value>,
  "value_decimal": <same value if money field, else NULL>,
  "confidence": NULL,
  "decision_group_id": <new UUID per (receipt, backfill run)>,
  "actor_type": "system_migration",
  "actor_user_id": NULL,
  "actor_label": "system:m1-day3a-backfill",
  "metadata_json": '{"original_created_at": "<receipt.created_at ISO>", "backfill_reason": "M1 Day 3a foundation"}',
  "created_at": <utc_now at migration time>
}
```

### Row-count estimate

Production currently has 13 receipts × 9 trackable current fields = up to **117 events**. Many fields are NULL on many receipts (e.g., `business_reason`, `attendees`, `report_bucket` are populated only on confirmed rows), so the realistic count is closer to **40-70** events. Exact number reported by the migration script's verification step.

### Decision-group semantics for backfill

Each receipt gets **one** new decision_group_id, shared by all backfill events for that receipt. Rationale: the backfill is "one event group per entity that captures its current state at the moment we started tracking provenance." Aggregating per-receipt makes the backfill queryable as a single audit unit.

### Why `event_type='accepted'` (not `'overridden'`)

This is the **first** event ever written for these fields on these entities. There's no prior event to override. `accepted` is correct; `overridden` would imply a prior accepted event existed.

### Why no provenance-from-`ocr_confidence` inference

PM decision #9 explicitly forbids it. The `ocr_confidence` value on a row tells us how confident the vision model was, but it doesn't tell us whether the current value came from vision (could have been overwritten by a user). Inferring `source=vision` from confidence presence would create false history. `legacy_unknown_current` is the honest answer: "we don't know how this got here, but it's current as of the migration timestamp."

### Exit invariant

After backfill, this query returns 0:

```sql
SELECT receipt.id, '<field_name>'
FROM receiptdocument AS receipt
WHERE receipt.<column> IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM fieldprovenanceevent fpe
    WHERE fpe.entity_type = 'receipt'
      AND fpe.entity_id = receipt.id
      AND fpe.field_name = '<field_name>'
      AND fpe.source = 'legacy_unknown_current'
      AND fpe.event_type = 'accepted'
  );
```

(Run for each tracked field. Migration script aggregates the checks.)

---

## 9. Migration script outline

`backend/migrations/m1_day3a_field_provenance.py` — same shape as `m1_day25_money_decimal.py`.

### Phases

1. **Protected-path guard** — refuse `/var/lib/dcexpense` / `/opt/dcexpense` (reuse pattern from Day 2.5; consider extracting `_refuse_protected_path` + `_check_sqlite_version` to a shared `migrations/_common.py` to avoid copy-paste — flagging as a nit, not a Day-3a blocker).

2. **CREATE TABLE** + indexes.

3. **Idempotency probe** — if `fieldprovenanceevent` table exists AND every receipt with a non-NULL tracked field has at least one `legacy_unknown_current` event, exit 0 as no-op.

4. **Backfill loop** — for each receipt × tracked field, write the backfill event. All inside one transaction.

5. **Verification** — five checks, all inside the same transaction; rollback on any failure:
   - Table exists with expected columns and types.
   - All required indexes present.
   - `COUNT(*) FROM fieldprovenanceevent WHERE source='legacy_unknown_current' AND event_type='accepted'` equals the sum of non-NULL values across tracked columns × receipts (computed pre-backfill, asserted post-backfill).
   - For each tracked field: every receipt with a non-NULL column value has exactly one matching backfill event (the §8 exit invariant SQL).
   - No backfill event has `actor_user_id` set (all NULL — they're system_migration events).

6. **Backup + audit log** — same pattern as Day 2.5: `.pre-m1-day3a-{ts}.backup` and `.pre-m1-day3a-{ts}.migration.log` written alongside the DB on `--apply`.

7. **CLI** — `--db-path` required, `--dry-run` (default) / `--apply`. Dry-run reports the projected row count without writing.

### Rollback procedure

(Mirrored into the script docstring per the Day 2.5 process.)

```
1. sudo systemctl stop dcexpense.service
2. sudo cp /var/lib/dcexpense/expense_app.db.pre-m1-day3a-{ts}.backup \
          /var/lib/dcexpense/expense_app.db
3. cd /opt/dcexpense/app && git revert <m1-day3a merge sha>
4. sudo systemctl start dcexpense.service
5. Verify /health returns 200 and one receipt loads via /review
```

Notably, dropping the table (without restoring the pre-Day-3a code) is also a valid partial rollback since no other code depends on `fieldprovenanceevent` until Day 3b lands. But the full procedure is the safe default.

---

## 10. Test plan

New tests (target file naming follows existing convention):

### `backend/tests/test_field_provenance_service.py`
- `test_record_field_event_writes_row` — basic happy path.
- `test_record_field_event_auto_populates_value_decimal_for_money_fields`
- `test_record_field_event_leaves_value_decimal_null_for_non_money_fields`
- `test_record_field_event_auto_generates_decision_group_id_when_omitted`
- `test_record_field_event_serializes_decimal_value_via_decimal_encoder` — tie back to M1 Day 2.5 convention.
- `test_record_field_event_rejects_bogus_enum_value` — passing `field_name="not_a_field"` should raise (Pydantic/SQLModel validation OR explicit `assert isinstance(field_name, FieldName)` in the wrapper).
- `test_get_current_event_returns_most_recent_accepted_or_overridden`
- `test_get_current_event_ignores_proposed_and_rejected`
- `test_get_current_event_returns_none_when_no_history`
- `test_get_field_history_orders_newest_first`
- `test_get_decision_group_returns_all_events_with_matching_id_ordered_by_created_at_asc`

### `backend/tests/test_field_provenance_atomicity.py`
- `test_session_rollback_drops_event_alongside_column_write` — start a transaction with `with session.begin()`, write column + event, raise inside the block, verify on a fresh session that neither persisted.
- `test_record_field_event_rejects_non_dict_metadata` — passing `metadata="some json string"` or `metadata=[1,2,3]` raises TypeError. Only `dict[str, Any]` or `None` accepted.
- `test_metadata_with_decimal_value_round_trips_through_decimal_encoder` — write event with `metadata={"raw_amount": Decimal("123.4567")}`, read back, assert exact Decimal preserved (relies on M1 Day 2.5 `app.json_utils.dumps`). — start a transaction, write column + event, rollback, verify neither persists.

### `backend/tests/test_m1_day3a_migration.py`
- `test_apply_creates_table_and_indexes`
- `test_apply_backfills_one_event_per_non_null_tracked_field`
- `test_apply_does_not_backfill_null_columns` — receipts with NULL `extracted_local_amount` get no event for that field.
- `test_apply_preserves_original_created_at_in_metadata_json`
- `test_apply_uses_legacy_unknown_current_source_and_system_migration_actor`
- `test_apply_is_idempotent` — re-running on already-migrated DB is a no-op.
- `test_dry_run_does_not_mutate_database` (mirror Day 2.5 pattern)
- `test_refuses_protected_path` (mirror Day 2.5 pattern)
- `test_exit_invariant_holds` — run the §8 exit-invariant SQL programmatically, assert 0 rows.

### `backend/tests/test_field_provenance_invariant.py`
- `test_invariant_column_equals_latest_event` — for every receipt and every tracked field, after backfill, the column value equals `get_current_event(...).value` (deserialized to the column's native type). This is the **load-bearing detector** for "someone wrote a column without going through the wrapper" once Day 3b's merge-logic refactor lands. Day 3a runs it post-backfill against the synthetic 13-receipt DB; Day 3b promotes it to a per-test fixture for ongoing enforcement across the suite.

### Total
~24 new tests. No changes to existing tests in Day 3a (no merge-logic refactor yet).

---

## 11. Out of scope (explicit)

Day 3a does **not** do the following:

- Refactor `receipt_extraction.py`, `clarifications.py`, or any merge logic to populate events. → **Day 3b**.
- Modify `apply_receipt_extraction` to call `record_field_event`. → **Day 3b**.
- UI changes in `frontend/review-table.html` (no provenance display). → **Day 3c**.
- XLSX template changes to surface provenance per cell. → **Day 3c** or **M5**.
- §8.7 partial-expense fields `claimed_local_amount` / `receipt_total_local_amount`. → **Day 3b**.
- `ExpenseReport.submit()` snapshot logic that writes `event_type='snapshotted'` events. → **Day 3c**.
- Deprecation, migration, or modification of `ReviewRow.source_json` / `suggested_json` / `confirmed_json`. → They coexist with FieldProvenanceEvent for now; convergence (if any) is a later milestone decision.
- Linter rule enforcing "no tracked-column write outside `record_field_event`-aware code." → **M2** at earliest.
- Post-SSO actor migration (`unauthenticated_user` → `web_user` with real `actor_user_id`). → **M2 SSO work**.
- Querying / dashboard / reporting on provenance. → **M3+**.

---

## 12. PM-confirmed answers

PM reviewed the original recommendations below and confirmed each. Implementation proceeds against these answers.

### Q1. `decision_group_id` always-set vs nullable — **CONFIRMED: always set**

Auto-generate UUID for single-event writes. NULL would simplify some queries but complicates the M3 approval UI which always wants `get_decision_group()` to show context. Auto-generating a UUID per write costs microseconds and keeps the data model uniform.

### Q2. `replaces_event_id` column on `overridden` events — **CONFIRMED: NO for Day 3a**

The `(entity_type, entity_id, field_name, created_at DESC)` lookup gives "previous event" trivially. Adding an explicit pointer column duplicates that information and creates a denormalization invariant to maintain. Add later if M3 audit UI makes the lookup pattern unergonomic.

### Q3. Decision-group reuse vs new-per-override — **CONFIRMED: new group per override**

Reuse causes decision groups to accumulate indefinitely as receipts are edited and re-edited; new-per-override gives each "decision moment" a clean boundary. The entity+field history is still walkable via `get_field_history` across decision groups.

### Q4. `value_decimal` for non-money Decimal-typed fields (e.g., `vat_rate`) — **CONFIRMED: MONEY_FIELDS gates value_decimal**

Only populate `value_decimal` for fields in `MONEY_FIELDS` (set defined in §2). `vat_rate` is a multiplier, not money — putting it in `value_decimal` would skew any `SUM(value_decimal) WHERE field_name='extracted_local_amount'` query that forgot to filter by field. Other Decimal-typed fields serialize to `value` only.

### Q5. `metadata_json` schema — **CONFIRMED: freeform PLUS dict-or-null validation at write time**

Freeform per-source schema in Day 3a (documented in code comments). The actual fields will settle out during Day 3b/3c; M3+ can formalize per-source Pydantic schemas if needed.

**Additional PM requirement:** the wrapper validates at write time that `metadata` is `dict[str, Any]` or `None`. Any other type (string, list, pre-serialized JSON, etc.) raises `TypeError`. This prevents callers from sneaking in inconsistent encodings. The §6 record_field_event docstring documents this contract; `test_record_field_event_rejects_non_dict_metadata` (in `test_field_provenance_atomicity.py`, §10) enforces it.

### Q6. Migration script consolidation — **CONFIRMED: extract to `migrations/_common.py` in Day 3a**

`_refuse_protected_path`, `_check_sqlite_version`, and the backup-naming helper move to `backend/migrations/_common.py` as part of Day 3a. The Day 2.5 migration script gets a follow-up patch in the same Day 3a PR to import from the shared module (touching only the imports, not the migration logic). Cost ~80 lines in a new shared file; benefit is the Day 3a migration script imports rather than copy-pastes those guards, and a future Day 7 (FX) migration starts from the shared baseline.

---

## Document end

Once PM signs off on the design (or returns notes), Day 3a implementation lands as a new branch `feat/m1-day3a-field-provenance` with:
- The schema + enums + service wrapper
- The migration script + tests
- The backfill verified against a synthetic DB and (per the Day 2.5 procedure) a scp'd copy of production

No code lands until this design doc is approved.
