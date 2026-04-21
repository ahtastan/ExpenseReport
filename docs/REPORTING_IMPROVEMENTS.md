# Reporting Improvements Needed

## Current pain
Manual fixes were needed before sending the report.

## Root causes
1. Report generation was downstream of several partially-correct tables
2. Statement data and receipt data were mixed inconsistently
3. Some corrected rows were patched manually after reports were already generated
4. Annotation/PDF rebuilds and workbook rebuilds could drift apart
5. Review-needed matches were sometimes treated like final matches

## Product fixes

### 1. Statement-ledger first
Every report row must originate from a single `StatementTransaction` record.

### 2. Explicit review queue
Do not allow medium/low confidence rows into a final report without approval.

### 3. Locked generation inputs
A report run should freeze:
- statement version
- receipt extraction version
- match decisions
- policy decisions
- template version

### 4. Deterministic bucketing
Bucketing should come from a policy engine, not scattered script rules.

### 5. Validation layer before export
Before generating final Excel/PDF:
- no duplicate statement rows
- no missing USD for included rows
- no orphan receipt annotations
- all included rows have business/personal decision
- all business rows have valid bucket

### 6. Review screens to add
- unmatched receipts
- one-to-many candidate matches
- suspicious supplier mismatches
- same-date same-amount duplicates
- included rows without receipts
- receipt rows outside statement period

## Definition of done for a reliable export
A final report should only be generated when:
- every included row is statement-backed
- every included row has a policy decision
- every medium/low confidence match is resolved
- totals reconcile back to the statement source
