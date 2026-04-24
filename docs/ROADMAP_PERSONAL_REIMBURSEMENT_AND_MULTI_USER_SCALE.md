# Roadmap — Personal-Reimbursement Reporting + Multi-User Scale

**Status:** Draft, 2026-04-24
**Prepared after:** validated end-to-end Telegram + Diners-statement flow for a single operator.
**Motivation:** Today's app reconciles receipts against a Diners corporate-card statement. In practice employees also pay out of pocket (personal credit card, debit, cash) for business expenses and submit a **reimbursement report** where no corporate statement exists. The application must also scale from one operator to an entire company with employees, approvers, and finance.

---

## 1. Current state (what already works)

**Single-user pipeline, Telegram-first:**
- `AppUser` is keyed by `telegram_user_id`; web UI has **no auth** (`routes/*` only depend on `get_session`).
- Receipts arrive via Telegram (`services/telegram.py` → `handle_update`). Photos/PDFs go through staged OCR (`model_router.py`: mini → escalate → deterministic fallback), then `clarifications.py` seeds follow-up questions (`business_reason`, `attendees`, etc.) when `business_or_personal == 'Business'`.
- A single Diners `.xlsx` statement anchors reporting: `StatementImport → StatementTransaction → MatchDecision → ReviewSession → ReviewRow → ReportRun`.
- **Every report is tied to a statement** — `ReviewSession.statement_import_id` and `ReportRun.statement_import_id` are `NOT NULL`.
- Output: Excel workbook + annotated PDF per `ReportRun`, written to `storage_path`.

**Validated in production (2026-04-24):**
- 11 receipts, 1 Diners statement, 13 transactions, 10 expected auto-matches.
- 3 statement rows with no receipts (hotel, lunch, parking — out-of-pocket recoverable via web `/review`).
- 1 receipt (airport taxi, 750 TL) paid on a **personal card** — not on the Diners statement. Currently impossible to report cleanly in the app.

## 2. Gap analysis — what blocks company-wide rollout

### 2.1 Data-model gaps (personal-reimbursement path)
| Gap | Where it lives | Impact |
|-----|----------------|--------|
| `ReviewSession.statement_import_id` is `NOT NULL` | `models.py:118` | Can't create a review session without a statement. |
| `ReportRun.statement_import_id` is `NOT NULL` | `models.py:147` | Can't produce a report without a statement. |
| No concept of "report type" (statement-led vs. reimbursement) | everywhere | Downstream summaries, totals, and accounting treatment differ. |
| No "expense report" parent entity grouping receipts directly | n/a | Employee submits ad-hoc bundle; no stable identifier. |
| `ReceiptDocument.report_bucket` is a freeform string | `models.py:42` | No validated category list, no FK to a policy table. |

### 2.2 Auth / multi-tenancy gaps
| Gap | Where it lives | Impact |
|-----|----------------|--------|
| No login, no session, no CSRF | `routes/*.py` (every route only depends on `get_session`) | Anyone who can reach the URL can mutate anything. |
| `AppUser` has no `email`, `role`, `manager_id`, `department`, `active` flag | `models.py:11` | Can't identify employees, route approvals, or disable leavers. |
| Telegram allowlist is a global env var | `config.py` / settings | Doesn't scale; onboarding new users = redeploy. |
| No company/tenant boundary | — | Single-tenant hard-baked; fine now, may matter later. |
| `uploader_user_id` is nullable and unused in auth checks | `models.py:24`, routes | Users can see/edit each other's data. |

### 2.3 Workflow / governance gaps
| Gap | Impact |
|-----|--------|
| No submit → approve → reimburse state machine | Reports can't be closed-loop with finance. |
| No policy engine (per-category caps, receipt-required thresholds, FX rules) | Every receipt is trusted as-is. |
| No audit trail of who edited/approved what | Cannot pass internal-control audits. |
| No finance export (ERP-friendly CSV / SAP XML / NetSuite CSV / QuickBooks IIF) | Finance still rekeys data. |
| No email/Slack/Teams notifications | Approvers don't know work is queued. |

### 2.4 Operational gaps
| Gap | Impact |
|-----|--------|
| SQLite on a single VPS | Concurrent writes across 50 users will contend; no PITR backup. |
| No background-job runner | OCR + matching run synchronously in the webhook; slow uploads will time out. |
| No observability (metrics, log aggregation) | Can't diagnose at scale. |
| Secrets hot-rotated manually via `/etc/dcexpense/env` | No rotation policy, no secret scanning. |

## 3. Target architecture (end state)

- **One unified "Expense Report" entity**, with a `report_kind` discriminator (`diners_statement` vs. `personal_reimbursement`). Both kinds produce the same downstream `ReportRun` artifact.
- **Receipts are first-class** and attach to a report, optionally linked to a `StatementTransaction` when applicable.
- **SSO login** (Google Workspace is the likely first target, given the existing Microsoft email on file — Azure AD is the alternative). Every request carries an identified user.
- **RBAC**: `employee` (default), `manager` (sees direct reports), `finance` (sees everything, exports, closes reports), `admin` (manages users, policies).
- **Approval state machine** on each report: `draft → submitted → approved → reimbursed` (or `rejected`).
- **Policy engine** runs at submit time: flags over-cap items, missing receipts above threshold, disallowed categories.
- **ERP export** is a first-class `ReportRun` output alongside the workbook/PDF.
- **Background workers** handle OCR, matching, and report generation so webhook replies stay snappy.

## 4. Phased roadmap

Each milestone is independently shippable and leaves the existing Diners flow working. No Big Bang.

### Milestone 1 — Personal-reimbursement report (2–3 weeks)
**Why first:** unblocks the 750-TL airport-taxi case today; proves the data-model refactor without adding auth complexity.

**Data model**
- New `ExpenseReport` table: `id, owner_user_id, report_kind ('diners_statement'|'personal_reimbursement'), title, period_start, period_end, status ('draft'), notes, created_at, updated_at, statement_import_id NULLABLE`.
- Migrate `ReviewSession` / `ReportRun` to FK `expense_report_id` instead of `statement_import_id`. Backfill script creates one `ExpenseReport(report_kind='diners_statement')` per existing `StatementImport`.
- Make `statement_import_id` nullable on `ReviewSession` and `ReportRun`; existing rows keep both.
- Add `ReceiptDocument.expense_report_id` nullable FK; receipts can now live in a report without a statement row.

**Backend**
- `POST /reports` → create empty personal-reimbursement report (title, period).
- `POST /reports/{id}/receipts/{receipt_id}` → attach an existing receipt.
- `POST /reports/{id}/submit` → (sets `status='submitted'`, runs policy validation).
- Adapt `report_generator.py` to take an `ExpenseReport` (not a `StatementImport`) and branch on `report_kind`: statement-led renders the reconciliation view; personal-reimbursement renders a receipts-only ledger with reimbursable total.

**Telegram**
- `/report new "Taxi reimbursements Aug 2025"` command creates a personal-reimbursement report.
- On receipt upload, if the user has exactly one open draft report, auto-attach; otherwise ask once ("Attach to Taxi reimbursements Aug 2025 or start new?").

**Output artifacts**
- Separate workbook template `personal_reimbursement_report.xlsx` with cover sheet, itemized receipts, FX summary, reimbursable total, signature line.

**FX conversion (promoted into M1 per decision 3)**
- Each receipt keeps `(local_amount, local_currency)`. New columns on the report line: `report_amount`, `report_currency` ('USD' | 'EUR'), `fx_rate`, `fx_date`, `fx_source`.
- Report header stores the chosen `report_currency` (defaults to `USD`). UI forbids TRY / other currencies at the report level.
- FX source: start with OpenExchangeRates free tier keyed by `transaction_date`; cache per (date, pair) in a new `FxRate` table to avoid re-fetching.
- Acceptance addendum: a TRY receipt on a USD report converts correctly at the transaction date and the Excel output shows both local and converted amounts side by side.

**Acceptance**
- Operator uploads 3 out-of-pocket receipts, types `/report new "April reimbursements"`, gets back a submittable report with correct totals and an annotated-receipts PDF.
- Existing Diners flow still works unchanged.

### Milestone 2 — Authenticated multi-user foundation (2–3 weeks)
**Why second:** M1 refactor makes ownership explicit; now we enforce it.

**Auth (Microsoft 365 / Entra ID, per decision 1)**
- MSAL for Python on the FastAPI side, OAuth 2.0 Authorization Code flow with PKCE, tenant-restricted to EDT's Microsoft tenant (single-tenant app registration).
- Session cookie (httpOnly, SameSite=Lax, Secure); CSRF tokens on state-changing routes.
- `AppUser` gains: `email` (unique, indexed), `entra_object_id` (Microsoft stable user ID), `role` (enum: `employee`|`manager`|`finance`|`admin`), `manager_user_id` FK, `is_active`.
- First login auto-provisions an `AppUser` if the email domain matches EDT's tenant domain(s) in settings. Unknown tenants get 403.
- `require_user(min_role=...)` dependency replaces bare `get_session` on every mutating route.
- App registration scopes: `openid`, `profile`, `email`, `User.Read` (optional: `User.Read.All` if we auto-discover manager hierarchy from Microsoft Graph instead of storing it manually).

**Ownership enforcement**
- Every `ReceiptDocument`, `ExpenseReport`, `ReviewSession`, `ReportRun` query scopes to `owner_user_id == current_user.id` (or manager-of, or finance role).
- Admin-only routes to list/disable users.

**Telegram linking**
- New web-UI page: "Link Telegram" — generates a short-lived token; user pastes it to the bot via `/link <token>`. Bot stores `telegram_user_id` on the authenticated `AppUser`. Global allowlist env var becomes irrelevant for user onboarding.

**Acceptance**
- Two employees log in, each sees only their own receipts/reports in `/review`.
- Manager sees team's submitted reports, can approve/reject.
- Telegram bot still works for each, isolated by user.

### Milestone 3 — Approval workflow & finance close (2 weeks)
**Why third:** with ownership in place, add the governance loop.

**State machine**
- `ExpenseReport.status`: `draft → submitted → approved_by_manager → approved_by_finance → reimbursed` (plus `rejected` with reason).
- `ApprovalEvent` table: `report_id, actor_user_id, from_status, to_status, note, created_at` — full audit trail.
- Manager approve/reject UI on `/approvals` page.
- Finance close-out: sets `reimbursed_at`, attaches payment reference.

**COO pre-approval workflow (per Code of Conduct)**
- Two explicit pre-approval gates from the Code of Conduct: (a) **client-entertainment expenses** require COO approval *before* the spend; (b) **gifts from clients valued over $25** must be disclosed to the COO.
- New model `PreApprovalRecord`: `kind ('client_entertainment'|'gift_over_25'), requester_user_id, approver_user_id, approval_reference, approved_at, notes, storage_path (optional approval screenshot/email)`.
- Submit-time validation: any report line tagged as `client_entertainment` must be linked to an `approved` `PreApprovalRecord` of that kind — hard block otherwise.
- Employees can pre-file the approval via `/preapprovals/new` (web) or `/preapproval client_entertainment <reference>` (Telegram) before submitting the report.

**Notifications**
- Minimal: email via SMTP on every state transition. Templates in `app/services/notifications/`.
- Deferred: Slack/Teams webhook per user.

**Finance export**
- `GET /reports/{id}/export/netsuite.csv` (and/or whatever ERP the company uses — needs decision, see §6).
- `GET /reports/{id}/export/quickbooks.iif` as second format if used.
- Column schema defined in `docs/finance-export-schema.md`.

**Acceptance**
- Employee submits → manager approves → finance exports CSV → status moves to `reimbursed`. All events visible on the report's audit-trail panel.

### Milestone 4 — Policy engine + admin console (2–3 weeks)
**Why fourth:** policy rules are only useful when many users hit them.

**Policies**
- New `ExpensePolicy` table: `category, max_amount_local, currency, requires_receipt_above, requires_attendees_above, allowed_roles`.
- Admin UI under `/admin/policies` to CRUD rules.
- `policy_engine.validate(report)` runs on submit; fails with structured violations the UI renders inline on the offending rows.
- Pre-seeded policy set: per-category daily meal caps, hotel nightly caps, ground-transport cap, fuel reimbursement rate.

**Admin console**
- `/admin/users`: list, activate/deactivate, change role, reassign reports on offboarding.
- `/admin/audit`: search the full audit trail.

**Acceptance**
- Admin creates a "Meals: max 3000 TRY/day" policy. An employee report with a 3048 TRY dinner triggers a violation that the approver sees.

### Milestone 5 — Operational hardening (parallel, ongoing)
Not blocking any of the above; do as load grows.
- Move DB from SQLite to Postgres (single `alembic upgrade head`; code already uses SQLModel).
- Move OCR + report-generation off the webhook into a worker queue (Dramatiq on Redis, or RQ).
- Add Prometheus `/metrics`, ship logs to Loki or Datadog.
- Nightly `pg_dump` to S3; 30-day retention.
- Secret rotation policy: Telegram tokens quarterly, OpenAI key on compromise, Google OAuth client yearly.

## 5. Concrete next steps (if you start tomorrow)

Order of operations for M1:
1. Write an `alembic` (or raw-SQL) migration: create `expenserreport`, nullable FKs on `reviewsession` / `reportrun`, new FK on `receiptdocument`, backfill one `ExpenseReport` per existing `StatementImport`.
2. Port `review_sessions.get_or_create_review_session(session, statement_id)` → `get_or_create_review_session(session, report_id)` — statement_id lookup goes through `ExpenseReport.statement_import_id`.
3. Add `POST /reports` and `POST /reports/{id}/receipts/{receipt_id}`.
4. Branch `report_generator.build_outputs` on `report_kind`.
5. Write the personal-reimbursement template.
6. Add `/report` Telegram commands.
7. Verify M1 acceptance with the 750 TL airport slip: attach to a new personal-reimbursement report, submit, open the rendered workbook.

## 6. Decisions locked in (2026-04-24)

- [x] **Auth provider**: **Microsoft 365 / Entra ID** (Azure AD). Implementation: MSAL for Python on backend, OAuth 2.0 Authorization Code flow with PKCE. Tenant-restricted to EDT's Microsoft tenant. Confirmed by `EDT-Travel-Tips-and-Expense-Guidelines.docx` referencing Teams as primary comms.
- [ ] **ERP target for finance export**: **TBD** — user to confirm with EDT IT/finance. Reminder scheduled for 2026-05-08. Export format deferred to M3 scope refinement once ERP is known.
- [x] **Currencies**: **Reports must be expensed in USD (default) or EUR.** TRY and all other local currencies are NOT allowed as a report currency. Receipts arrive in any local currency (TRY, CAD, GBP, etc.) but are **converted to USD or EUR at submit time**. FX lookup is required in M1 (cannot defer to M2 as originally scoped). Use `transaction_date` as the rate date; record both `local_amount+local_currency` and `report_amount+report_currency+fx_rate+fx_date` on each row. Primary FX source: OpenExchangeRates, ECB, or internal rate sheet (to decide at implementation).
- [x] **Policy rules**: **Pre-seed from `EDT-Travel-Tips-and-Expense-Guidelines-2025-01-14.docx` and `EDT-Code-of-Conduct.pdf`.** See §8 below for the extracted rule set. Treat cap violations as **soft audit flags**, not hard blocks — the guideline explicitly says "These are automatic audit triggers and not target maximums." User to confirm final topology with HR.
- [ ] **Approval topology**: **TBD pending HR conversation.** EDT Code of Conduct is explicit about two pre-approval gates (COO must pre-approve client entertainment; gifts > $25 must be reported to COO), but does not document the routine expense-report approval chain. Default assumption remains hierarchical: employee → manager → finance → reimbursement.
- [x] **Data residency**: **US.** No Turkey / EU data-localization constraint. Hosting and DB can remain in US region. GDPR minimum still applies for any EU-based employees traveling, but does not force EU-region hosting.

## 7. Out of scope (call out now, don't scope-creep)

- Mobile apps (iOS/Android native). Telegram + web is enough for v1.
- Mileage logging with GPS. Keep receipts-only.
- Credit-card direct feeds (Plaid / bank OFX). Statement upload remains manual.
- Multi-entity/multi-company tenancy. Single-company for now; can revisit later.
- Full-text receipt search. Defer until corpus > 10k.
- Auto-categorization ML beyond what the current vision prompt already does.

## 8. EDT-specific policy rule set (extracted from source docs)

This is the pre-seed data for the M4 policy engine. Every rule is modeled as `(trigger_condition, severity, message)`. Soft flags attach to a report/line but do not block submission; hard blocks must be cleared or justified before submit.

### 8.0 EG / MR workbook flags (user-judgement → M4 auto-suggest)
The EDT workbook's `Week 1B`/`Week 2B` meals-and-entertainment detail sheet has two single-letter columns:
- **EG** — *Exceeds EDT spending guidelines.* User flags voluntarily when an expense crosses any of the §8.1 per-head caps.
- **MR** — *Missing receipt.* User flags when an expense appears on the Diners statement but no paper/PDF receipt exists.

Today (M1 scope) both are **user-judgement toggles** — surfaced in the review UI (pill toggles inside the Meals & Entertainment expanded panel; glance badges on the main row) and written straight to `meal_eg` / `meal_mr` on the `ReviewRow.confirmed_json`, which the report generator emits as the literal `x` the template expects.

Under M4 the **policy engine will auto-suggest `EG=true`** whenever an expense line crosses a threshold in §8.1 (per-head meal caps, 18% tip ceiling, unusual-items list). The suggestion lands as a soft flag on the row — the user can accept it (persisting `meal_eg=true`) or dismiss it with a justification that becomes part of the audit trail. MR stays a pure user toggle since it's a factual statement about receipt availability, not a policy violation.

### 8.1 Meal caps (per head, soft flags)
Source: *EDT Travel Tips — Food, Drink, and Entertainment*.

| Category | With customer | Without customer | Notes |
|----------|---------------|------------------|-------|
| Dinner | $60 / €60 / CAD 80 | $30 / €30 / CAD 40 | Per person |
| After-hours (daily) | $30 / €30 / CAD 40 | $15 / €15 / CAD 20 | Per team per day |
| After-hours (weekly) | $90 / €90 / CAD 120 | $45 / €45 / CAD 60 | Per team per week |
| Customer entertainment (daily) | $100 / €100 / CAD 130 | — | Per day |
| Customer entertainment (weekly) | $300 / €300 / CAD 400 | — | Per week |
| Tip / gratuity | > 18% of subtotal flags | > 18% flags | — |

Audit-trigger rules (flag, don't block):
- Same client dined with **2+ times in a single week** → flag.
- Meal with missing **attendees** list → flag (matches existing `attendees` clarification).
- Spend applies to the **full EDT team** regardless of who paid — team-wide aggregation needed on the report.

### 8.2 Unusual items (soft flag)
Source: *Travel Tips — Food, Drink, and Entertainment*.
- Movies, massages, in-room entertainment, or any single item not in the standard category list → flag for reviewer justification.

### 8.3 Gift / client-entertainment pre-approval (hard block + workflow)
Source: *EDT Code of Conduct — Conduct at Client's Office*.
- **Any gift accepted from a client with declared value > $25** → require COO disclosure record attached to the report before submit.
- **Any client-entertainment expense (meal/event where client is attendee)** → require prior COO approval. App must let the employee record the COO-approval reference (date + approver email) at submit time and surface missing approvals as a hard block on the report.

### 8.4 Currency handling (hard rule)
Source: *Travel Tips — Foreign Currency Expenses and Cash* + user decision 3 (2026-04-24).
- Report currency must be **USD or EUR**. No TRY, no CAD, no GBP on a submitted report.
- FX conversion at submit time using `transaction_date` rate. Store rate + source + fetched_at on each line.
- Same-currency-as-card preference: warn (soft flag) if a USD card was used to pay a EUR-denominated vendor or vice versa, citing the 3% conversion-fee note in the guideline.

### 8.5 Air travel (soft flags)
Source: *Travel Tips — Air Travel*.
- Skiplagging: employee self-attests "not a skiplag purchase" at submit time for air-travel receipts.
- Priceline / Hotwire bookings → hard block ("EDT policy prohibits these vendors").
- Delta SkyBonus number is **US582147525** — auto-fill on air-travel supplier detection so it's captured for corporate credit.

### 8.6 Hotel policy (soft flags)
Source: *Travel Tips — Hotels*.
- Preferred chains (US): Wingate, Holiday Inn Express, Hampton Inn, Comfort Inn.
- Preferred chains (International): Ibis, Holiday Inn Express, Novotel.
- Prohibited luxury chains: Ritz Carlton, Intercontinental, Marriott → flag for justification if detected in supplier.

### 8.7 Partial-expense pattern (feature, not a rule)
Source: *Travel Tips — Food, Drink, and Entertainment* ("EDT share = $X").
- Receipt-level field `edt_share_amount` (local currency). If set, the report line uses that value instead of `local_amount`. UI lets the employee note "EDT share = $X" on the receipt attachment, surfacing the delta in the summary.

### 8.8 Booking-source disclosure (soft flag)
- Any booking made via Priceline or Hotwire (even historical) → hard block per §8.5.
- Any booking routed through a third-party travel site (Expedia, Travelocity, TripAdviser, Booking.com, Kayak) → soft flag suggesting direct-booking for next trip, per guideline preference.

### 8.9 Audit justifications
All soft flags in §8.1–§8.8 require a **one-line justification** from the employee at submit time. The justification flows into the ReviewRow's `confirmed_json.justifications[]` and appears in the approver's audit panel.

---

## Appendix A — Minimal data-model diff for M1

```diff
 class ExpenseReport(SQLModel, table=True):              # NEW
     id: int | None = Field(default=None, primary_key=True)
     owner_user_id: int = Field(foreign_key="appuser.id", index=True)
     report_kind: str = Field(index=True)                # 'diners_statement' | 'personal_reimbursement'
     title: str
     period_start: date | None = None
     period_end: date | None = None
     status: str = Field(default="draft", index=True)
     statement_import_id: int | None = Field(
         default=None, foreign_key="statementimport.id", index=True,
     )
     notes: str | None = None
     created_at: datetime = Field(default_factory=utc_now)
     updated_at: datetime = Field(default_factory=utc_now)

 class ReceiptDocument(SQLModel, table=True):
     ...
+    expense_report_id: int | None = Field(
+        default=None, foreign_key="expenserreport.id", index=True,
+    )

 class ReviewSession(SQLModel, table=True):
-    statement_import_id: int = Field(foreign_key="statementimport.id", index=True)
+    expense_report_id: int = Field(foreign_key="expenserreport.id", index=True)
+    statement_import_id: int | None = Field(
+        default=None, foreign_key="statementimport.id", index=True,
+    )

 class ReportRun(SQLModel, table=True):
-    statement_import_id: int = Field(foreign_key="statementimport.id", index=True)
+    expense_report_id: int = Field(foreign_key="expenserreport.id", index=True)
+    statement_import_id: int | None = Field(
+        default=None, foreign_key="statementimport.id", index=True,
+    )
```

## Appendix B — Files that will definitely change in M1

- `backend/app/models.py` — new table, FK additions.
- `backend/app/db.py` or new `backend/app/migrations/` — schema migration.
- `backend/app/services/review_sessions.py` — pivot from `statement_id` to `report_id`.
- `backend/app/services/report_generator.py` — branch on `report_kind`.
- `backend/app/services/report_validation.py` — validate both kinds.
- `backend/app/routes/reports.py` — new endpoints.
- `backend/app/routes/receipts.py` — attach-to-report endpoint.
- `backend/app/services/telegram.py` — new `/report` command handlers; auto-attach logic.
- `expense-ui/src/pages/Review.tsx` (or equivalent) — show `report_kind` in the header.
- `backend/tests/test_personal_reimbursement_flow.py` — new end-to-end regression.
