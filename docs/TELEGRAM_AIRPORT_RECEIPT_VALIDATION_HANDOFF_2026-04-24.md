# Telegram Airport Receipt Validation Handoff — 2026-04-24

## Goal
Isolate whether the airport/payment-slip receipt failure is:
- date only
- date + supplier (most likely for a payment slip — no merchant name on slip)
- date + amount
- or purely mixed-chat clarification interleaving (stale open questions from older receipts consuming the user's replies)

## Root Cause Analysis (Code Evidence)

### Interleaving mechanism
`clarifications.py:179–187` — `next_open_question_for_user()` returns the **oldest open
`ClarificationQuestion` across all receipts** for a given `user_id`.  
Question text never identifies which receipt triggered it.  
If older receipts have open questions, the airport receipt's questions queue behind them and the
user's typed answers silently go to the wrong receipt.

### Payment-slip field coverage
`receipt_extraction.py:126–156` — `ensure_receipt_review_questions()` opens one question per
missing field in this order: `receipt_date`, `local_amount`, `supplier`, `business_or_personal`.  
Airport card/payment slips:
- **Date**: usually present as ISLEM `DD/MM/YYYY - HH:MM` → vision prompt now covers this.
- **Amount**: usually clearly visible → likely extracted.
- **Supplier/merchant**: **not printed on payment slips** — only terminal/bank ID appears →
  `extracted_supplier` is very likely `None`, meaning **date + supplier** are both missing.

### What was deployed
- Vision prompt updated: ISLEM lines included, TARIH fallback included.
- `_vision_date()` in `receipt_extraction.py` now falls back to local-date regex if ISO fails.
- `answer_question()` non-answer guard prevents chatter from consuming open questions.
- All three changes compiled and smoke-tested locally before deploy.

## Minimum Clean Validation Sequence

### Pre-conditions (must be met before the Telegram resend)

**Step 0 — Check for stale open questions in the VPS DB**

```bash
ssh root@46.225.103.156
sqlite3 /opt/dcexpense/app/backend/data/expense_app.db \
  "SELECT cq.id, cq.question_key, cq.status, cq.created_at, rd.original_file_name
   FROM clarificationquestion cq
   LEFT JOIN receiptdocument rd ON cq.receipt_document_id = rd.id
   WHERE cq.status = 'open'
   ORDER BY cq.created_at;"
```

**If any rows appear from receipts OTHER than the airport/payment-slip receipt:**
Option A — close them manually so they don't interleave:
```bash
sqlite3 /opt/dcexpense/app/backend/data/expense_app.db \
  "UPDATE clarificationquestion
   SET status='answered', answer_text='[operator-closed]',
       answered_at=datetime('now')
   WHERE status='open' AND receipt_document_id != <airport_receipt_id>;"
```
Option B — use a different Telegram account (fresh `user_id`) to send the airport receipt
as the only pending receipt for that user.

**Step 1 — Record the airport receipt_id in the DB**
```bash
sqlite3 /opt/dcexpense/app/backend/data/expense_app.db \
  "SELECT id, original_file_name, extracted_date, extracted_local_amount,
          extracted_supplier, business_or_personal, status
   FROM receiptdocument
   ORDER BY created_at DESC LIMIT 5;"
```
Note the airport receipt `id` for later checks.

### Telegram resend sequence

**Step 2 — Send ONLY the airport/payment-slip receipt image to the bot**
- Do not send any other image first.
- Do not send any text before the receipt arrives.

**Step 3 — Record the FIRST question the bot asks**
The question text maps directly to what is missing:

| Bot question text contains | Missing field(s) |
|---|---|
| "I could not read the receipt date" | `receipt_date` |
| "I could not read the receipt amount" | `local_amount` |
| "I could not read the merchant name" | `supplier` |
| "Is it Business or Personal?" | `business_or_personal` |

Questions appear one at a time in the order above.

**Step 4 — Answer each question with a clean, parseable value**

| Field | Clean answer format | Example |
|---|---|---|
| receipt_date | `YYYY-MM-DD` | `2025-08-26` |
| local_amount | `<number> TRY` | `345.00 TRY` |
| supplier | Free text name | `Istanbul Airport` |
| business_or_personal | `Business` or `Personal` | `Business` |

Do **not** send chatter, questions, or acknowledgements between answers.
One reply per bot question, using exactly the format above.

**Step 5 — After each answer, check the DB to confirm the field was stored**
```bash
sqlite3 /opt/dcexpense/app/backend/data/expense_app.db \
  "SELECT extracted_date, extracted_local_amount, extracted_supplier,
          business_or_personal, status, needs_clarification
   FROM receiptdocument WHERE id = <airport_receipt_id>;"
```

**Step 6 — Continue until `needs_clarification = 0` in the DB row**

### Failure isolation outcome

After the clean resend, record which questions the bot asked:

| Questions asked | Diagnosis |
|---|---|
| None (extraction succeeded) | Vision OCR + ISLEM prompt fix worked end-to-end |
| Only `receipt_date` | Vision got amount+supplier; ISLEM date still not parsed (image quality or model gap) |
| `receipt_date` + `supplier` | Vision got amount; date and merchant both missing (expected for payment slip) |
| `receipt_date` + `local_amount` + `supplier` | Complete OCR failure on the image |
| Wrong receipt question first | Stale interleaved open questions — Step 0 was not performed correctly |

## Files Changed By This Handoff

None. This is a pure validation checklist. No code was changed.

## What Is Verified

- Vision prompt includes ISLEM date lines (code-verified, committed, deployed).
- `_vision_date()` falls back to local regex for slash dates (test-verified).
- Non-answer guard prevents chatter from closing open questions (test-verified).
- All three deployed files matched local hashes after deploy.
- VPS health checks and webhook status were clean.

## What Is Still Unverified

- Whether the airport receipt OCR now extracts `receipt_date` cleanly on the live VPS.
- Whether `extracted_supplier` is also missing (likely — payment slips lack merchant name).
- Whether old stale open questions existed in the DB and caused the previous reply to be consumed by a different receipt's question.
- Whether the live GPT model returns a slash-format date or null for this specific image.

## No Code Change Justified

Both suspected failure modes (stale interleaving and expected payment-slip field gaps) are
**production state issues**, not code defects. The deployed patch is correct.

A code change would only be justified if Step 3 above shows that the bot asks the wrong receipt's
question even after Step 0 confirms no stale open questions exist — that would indicate a routing
bug in the webhook handler, not an extraction bug.

## Exact Next Recommended Step

Run Step 0 SQL above. If stale open questions exist, close them or switch to a fresh Telegram
user, then execute Steps 1–6 and record the outcome. Do not resend until Step 0 is confirmed clean.

---

## Validation Result (2026-04-24, executed on VPS)

### Actual Root Cause

**Invalid `OPENAI_API_KEY` in `/etc/dcexpense/env`.** The key was set to a literal placeholder
string that OpenAI rejected with 401 for every vision call since deploy. The journalctl evidence:

```
OpenAI vision call failed on gpt-5.4-mini: Error code: 401
  - Incorrect API key provided: PASTE_RE...2oUA
OpenAI vision call failed on gpt-5.4: Error code: 401
  - Incorrect API key provided: PASTE_RE...2oUA
```

Every Telegram upload since deploy fell through the staged vision pipeline with both tiers
returning 401, leaving deterministic filename parsing as the only source of extraction fields.
That is why receipts 1–8 all had `extracted_date = NULL`, `extracted_local_amount = NULL`, and
the only non-null `extracted_supplier` values were filename stems (`IST Sey`, `SokMarket`,
`telegram photo`).

None of the four hypothesized failure modes (date-only / date+supplier / date+amount /
interleaving-alone) described the real cause. The ISLEM prompt change, the local-date fallback,
and the non-answer guard were all correct but had never had a chance to run.

### Interleaving Was Real But Secondary

23 stale open `ClarificationQuestion` rows existed across 8 receipts for one user, all FIFO-queued
by `next_open_question_for_user()`. Even after the key was fixed, those stale rows would have
continued routing user replies to the wrong receipts. This was the *masking* problem, not the
cause.

### Fix Applied (Ops-only, no code changes)

1. Replaced the placeholder `OPENAI_API_KEY` in `/etc/dcexpense/env` with a valid `sk-proj-...` key
   (initial replacement was also rejected with 401 — a revoked key — so a fresh one was generated
   at the OpenAI dashboard and pasted in).
2. Restarted `dcexpense` service.
3. Validated via internal endpoint: `POST /receipts/8/extract` on the airport slip returned
   `extracted_date=2025-08-26`, `extracted_local_amount=750.0`, `extracted_currency=TRY`,
   `extracted_supplier=IST Sey`, `business_or_personal=Business`, `confidence=1.0`,
   `status=extracted`, notes: `Vision extraction succeeded on mini model (gpt-5.4-mini)`.
4. Re-extracted receipts 1–7 — all reached `status=extracted` with populated critical fields.
5. Closed the 22 stale open clarification questions for receipts 1–7 (moot after re-extraction),
   and the 3 for receipt 8. Verified `open_left = 0`.
6. Live Telegram resend with `/start` + airport slip returned:
   `I read: 2025-08-26 | IST Sey | 750.0 TRY. What project, customer, or trip should this
   receipt be attached to?` — confirming end-to-end success on the real Telegram webhook path,
   with auto-detected Business classification skipping the b/p question.

### What Is Verified After The Fix

- Vision OCR reaches both mini and full models on the VPS (no 401).
- ISLEM payment-slip date lines extract to ISO via the updated vision prompt.
- `business_or_personal` is classified by vision for payment slips without an explicit caption.
- Receipt 8 is fully extracted at confidence 1.0 via both the internal rerun endpoint and the
  live Telegram path.
- FIFO clarification queue has no stale rows.

### What Is Still Unverified / Out Of Scope For This Step

- No secret rotation performed yet. The `TELEGRAM_BOT_TOKEN` and `TELEGRAM_WEBHOOK_SECRET`
  were exposed during troubleshooting and should be rotated.
- `extracted_supplier` is literal `"telegram photo"` for receipts 1–5, 7 because deterministic
  filename parsing wins over vision in `receipt_extraction.py:160` and the filename stem
  `telegram_photo_N` is treated as a real merchant name. Flagged as a separate narrow fix.

### Next Recommended Step

Rotate the exposed Telegram secrets (webhook first, then bot token via `@BotFather`). After
rotation, re-register the webhook via `scripts/register_telegram_webhook.py`. Then, as a
separate narrow step, patch `_parse_merchant` in `backend/app/services/receipt_extraction.py`
to skip filename stems matching the literal `telegram_photo[_N]` pattern so vision-extracted
merchant names are used instead.
