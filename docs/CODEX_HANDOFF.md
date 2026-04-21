# Codex Handoff

## Mission
Implement Phase A of the expense-reporting app backend.

## Repo root
`Expense/expense-reporting-app`

## Read first
- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/REPORTING_IMPROVEMENTS.md`
- `docs/ROADMAP.md`
- `docs/IMPLEMENTATION_BRIEF_PHASE_A.md`

## Deliverables
1. Real backend DB/config/model files
2. Statement Excel import service for `Diners Club Statement.xlsx` format
3. API routes to import and inspect statements
4. Clean local run instructions in README or backend README

## Constraints
- Keep statement-ledger as source of truth
- Keep code simple and local-first
- Prefer clean abstractions over cleverness
- SQLite first, Postgres-ready later
- Do not break current file structure outside this repo

## Definition of done
- backend starts successfully
- statement import endpoint works for the Diners Excel shape
- transactions are persisted and retrievable
- code is readable and easy to extend for receipt matching later
