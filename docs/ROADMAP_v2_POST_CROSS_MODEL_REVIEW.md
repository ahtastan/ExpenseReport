# Roadmap v2 — Post Cross-Model Review

**Status:** Active roadmap. Supersedes `ROADMAP_PERSONAL_REIMBURSEMENT_AND_MULTI_USER_SCALE.md` (v1), which remains in repo for historical context.
**Created:** 2026-04-24 (session end)
**Trigger:** Independent architectural review by ChatGPT-5.4 (high reasoning) against v1 roadmap + shipped code, surfacing 10+ concrete changes.

---

## 1. What changed vs v1

### Accepted from the review (10 changes)

| # | Change | Reasoning |
|---|--------|-----------|
| 1 | Reverse-proxy auth (Caddy) ships BEFORE M1 Day 2 | App is currently unauthenticated; web UI reachable at `https://app.dcexpense.com` with zero gate. Cannot add second user until this is closed. |
| 2 | Telegram webhook idempotency (B10) — dedupe by `telegram_file_unique_id` | Real correctness gap; retries create duplicate `ReceiptDocument` rows. |
| 3 | Provenance tracking on extracted receipt fields | Merge-order fixes (B1, B9) were symptoms; underlying rule "stored wins forever" is wrong. Every extracted field needs `provenance` + `confidence`; merge follows priority over provenance. |
| 4 | VAT/KDV fields on receipts + vision prompt | Turkish receipts carry KDV breakdowns; app is currently blind to half the receipt. |
| 5 | M1.5 Turkey ledger redesigned: `SettlementAccount` + immutable postings, not `settlement_mode` on AppUser with balance column | Balance MUST be derived, not stored. My v1 sketch was "good enough for 1 user"; revised model is proper double-entry-style. |
| 6 | M1.8 — GİB e-document verification — new milestone | Turkish e-Arşiv/e-Fatura receipts have GİB-verifiable identifiers; we should validate + flag reuse. |
| 7 | M2.5 — KVKK operational controls — new milestone | Retention, access logs, redaction, data export on request. Moved forward in sequence. |
| 8 | Period close / reopen / reversal workflow — promoted to M3 first-class | Finance-grade immutability for submitted reports. |
| 9 | Statement re-import diffing — added between M1 and M2 | Card statements get revised; app must handle without losing prior decisions. |
| 10 | FX lock policy — originally scoped as simple `FxRate` lookup, expanded to capture rate source/date + rationale per line | TRY volatility creates disputes; rate provenance must be permanent. |

### Rejected from the review (with rationale)

| Recommendation | Why rejected |
|----------------|--------------|
| Build proper SPA with build boundary before M2 | Current single-page HTML + Babel works and serves one user fine. SPA migration is 1-2 weeks of no-user-facing value. Stays at M5+ or later. |
| Convert to Alembic immediately | Will convert at the third schema migration, not before. Two one-off scripts is less work than setting up Alembic machinery now. |
| "Statement_import_id in operations" as a top-3 risk (ChatGPT's #1) | This is the plan for M1 Day 2 anyway (flip to `expense_report_id`-primary). Downgraded from "risk" to "next task." |
| `confirmed_json` as top risk | Used only at report-gen time. Typed-column additions alongside it are additive, not migration-heavy. |
| KVKK counsel "alongside M2" | At 1-2 internal users, engaged counsel is overkill. Counsel before EDT-wide rollout, not before M2. |

### Open concerns not yet decided

- **Approval topology** — still awaiting HR conversation (carried from v1)
- **ERP target** — reminder May 8 (carried from v1)
- **Whether to split `ReceiptDocument` into `Receipt` + `ReceiptAttachment`** — ChatGPT flagged as "tables that should be split"; deferring until M4 policy engine forces the question

---

## 2. Revised milestone sequence

### Immediate (before M1 Day 2) — 2-3 days

**M0.5 — Security gate + correctness baseline**

Non-negotiable work before adding more endpoints.

- **M0.5.1** — Caddy reverse-proxy auth for `/review`, `/receipts/*`, `/reports/*`. Basic auth as stopgap; upgradeable to Entra ID in M2 without rework.
- **M0.5.2** — Telegram webhook idempotency (B10). Reject duplicate `telegram_file_unique_id` within 24h.
- **M0.5.3** — Remove `storage_path` from API responses. Serve files via authenticated `/receipts/{id}/file` only.

**Acceptance:** second user could be added to the system tomorrow without seeing your data.

### M1 — Personal reimbursement reports (continues)

**M1 Day 1** — ✅ DONE (schema migration + FxRate table + backfill)

**M1 Day 2** — `POST /reports`, `POST /reports/{id}/receipts/{receipt_id}`. Key off `expense_report_id` throughout; `statement_import_id` becomes optional backing reference on diners_statement reports only.

**M1 Day 3** — Provenance refactor. Add `provenance` + `confidence` columns to extracted fields OR a dedicated `ReceiptFieldProvenance` sub-table. Merge logic becomes priority-over-provenance. Backfill existing rows as `provenance='legacy_unknown'`.

**M1 Day 4-5** — Telegram `/report new "<title>" --diners|--personal` command + inline keyboard for sticky 30-min session UX.

**M1 Day 6** — VAT/KDV field extraction. Add `vat_amount`, `vat_rate`, `invoice_type`, `gib_invoice_identifier`, `is_vat_reclaimable` to `ReceiptDocument`. Vision prompt updated to extract when present.

**M1 Day 7** — FX lookup service + `FxRate` cache-or-fetch. OpenExchangeRates primary, ECB fallback. Rate provenance captured per line, permanent.

**M1 Day 8-9** — `personal_reimbursement_report.xlsx` template + `report_generator` branching on `report_kind`.

**M1 Day 10** — Statement re-import diffing. New statement upload detects changed transactions, preserves prior match decisions where still valid.

### M1.5 — Turkey ledger (new milestone, post-M1)

Settlement via running Diners-card balance instead of payroll. Tax-driven, currently for 1 user, soon 2.

**M1.5 Day 1** — Schema: `SettlementAccount` (user FK, account type, currency, opened_at, status) + `LedgerEntry` (append-only, signed amounts, FX at entry time, reason, source FKs, reversal support).
**M1.5 Day 2** — Balance-derivation query + tests. Balance NEVER stored.
**M1.5 Day 3** — Report submission writes ledger postings for `diners_ledger` mode reports.
**M1.5 Day 4** — Reconciliation view: employee sees current balance + entry history.
**M1.5 Day 5** — Admin manual-adjustment UI (stubbed; wired to M4's admin console when it exists).

### M1.8 — GİB e-document verification (new milestone)

**M1.8 Day 1-2** — Parse e-Arşiv/e-Fatura identifiers from vision output.
**M1.8 Day 3-4** — Validation against GİB public endpoints where available.
**M1.8 Day 5** — Duplicate invoice detection across all receipts (same GİB identifier = hard flag).

### M2 — Entra ID auth (continues)

Unchanged from v1. Upgrades M0.5 basic auth to SSO. Scope: single-tenant Microsoft, MSAL + OAuth2 PKCE, RBAC basics (employee/manager/finance/admin).

### M2.5 — KVKK operational controls (new milestone)

**M2.5 Day 1-2** — Retention policies per entity type; soft-delete + purge job.
**M2.5 Day 3** — Access logs for sensitive endpoints.
**M2.5 Day 4-5** — Subject export (user downloads all their data) + subject delete (on departure).
**M2.5 Day 6-7** — Cross-border transfer inventory document + KVKK Article 9 compliance notes.

### M3 — Approval workflow + finance close

Adds:
- **Period close / reopen / reversal** (promoted from implicit to first-class)
- Report state machine: `draft → submitted → approved_by_manager → approved_by_finance → reimbursed` + `rejected`
- COO pre-approval records (client entertainment, gifts > $25) — unchanged from v1
- ERP export (format TBD per 2026-05-08 reminder)

### M4 — Policy engine + admin console

Unchanged from v1. Pre-seeded with rules from EDT-Travel-Tips + Code of Conduct. Admin UI for user management, policy CRUD, audit trail.

### M5 — Ops hardening (parallel, ongoing)

Unchanged from v1. Postgres migration, background workers, metrics, nightly backups, secret rotation.

### M6+ — SPA migration, mobile apps, advanced analytics

Deferred indefinitely. Current surface works for current user counts.

---

## 3. Risk register (updated post-review)

| # | Risk | Impact | Mitigation |
|---|------|--------|------------|
| R1 | Second user added before M0.5 ships → data leak | Severe | M0.5 is blocking for second-user onboarding. |
| R2 | Provenance refactor breaks existing receipts | Medium | Backfill as `legacy_unknown`; change is additive. |
| R3 | Turkey ledger math has silent bug (double-entry violation) | Severe | Derived balance + immutable postings + tests pinning every accounting invariant. |
| R4 | GİB endpoint unavailable when verifying e-document | Low | Validation is best-effort; non-availability ≠ rejection. |
| R5 | KDV extraction by vision is unreliable | Medium | VAT fields nullable; operator can fill manually at review time. |
| R6 | SQLite concurrency limits hit before Postgres migration | Medium | M5 monitors `database is locked` errors as signal to accelerate. |
| R7 | Webhook timeout on slow OCR causes Telegram retry → duplicate receipts | Medium | B10 idempotency covers the duplicate path; background worker is M5. |

---

## 4. Decisions log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-04-24 | Microsoft Entra ID for M2 auth | EDT already on M365; matches existing comms via Teams |
| 2026-04-24 | Currency: USD default, EUR optional, no TRY | Matches EDT finance practice; reimbursement to US HQ |
| 2026-04-24 | FX conversion in M1, not M2 | Real need surfaced by 750 TRY airport taxi case |
| 2026-04-24 | Per-user report ownership (multiple open drafts OK) | Matches EDT trip-level grouping for policy rules |
| 2026-04-24 | Telegram UX: 30-min sticky session (a2-i) | First upload asks, subsequent stick for 30 min |
| 2026-04-24 | M0.5 gate added after cross-model review | Auth is more urgent than v1 assumed |
| 2026-04-24 | Settlement via `SettlementAccount` + postings | Balance derived, never stored |
| 2026-04-24 | SPA migration deferred to M6+ | No user-facing value at current scale |
| 2026-04-24 | Alembic conversion at third schema migration | Not before |

---

## 5. Immediate next action

**M0.5.1 — Reverse-proxy auth via Caddy.**

Rationale: nothing else in this roadmap is safe to ship while the web UI is unauthenticated. This is the gate.
