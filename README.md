# Apex Procurement Agent

Apex Procurement is a deterministic, offline-first planner for the supplied SQLite procurement scenarios. It reconstructs time-phased component demand, evaluates the checked-in policy pack, solves exact procurement quantities, independently validates the result, and atomically reconciles agent-owned purchase orders and alerts.

## Install

Python 3.11 or newer is required. From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e '.[test]'
python3 -m pytest -q
```

SciPy is the only runtime dependency. No network service, API key, or model is required for normal planning.

## Run

The required command is:

```bash
python3 agent.py \
  --scenario data/scenarios/scenario_06_simple.sqlite \
  --contract benchmark \
  --llm off
```

An installed equivalent is `apex-procurement --scenario SCENARIO.sqlite`. A normal run writes verified purchase orders and alerts to the supplied database. Start with `--dry-run --json` when inspecting an unfamiliar scenario.

### Evidence contracts

- `--contract benchmark` is the default. It applies the assignment's explicit benchmark missing-evidence assumptions and labels them.
- `--contract production` never converts missing rolling evidence into an assumption. It retains `UNKNOWN`, emits component-specific `DECISION_REQUIRED` signals, and records one run-global `EVIDENCE_CONTRACT` alert with component traceability.

The default execution is deterministic and offline. `--llm off` makes that contract explicit and reports `model_status=disabled`. This package configures no live planning provider: `--llm auto` therefore performs no provider or network probe, uses the same deterministic plan and business rows as `--llm off`, and reports exactly `model_status=unavailable_deterministic_fallback` in result JSON and the audit event. `--llm required` exits with code 4 before planning or writes. Model output can never replace policy evaluation, exact optimization, independent validation, or the commit boundary.

### Flags

- `--scenario PATH` selects one readable, regular SQLite snapshot and is required.
- `--contract {benchmark,production}` selects the missing-evidence contract.
- `--llm {off,auto,required}` controls the non-load-bearing model seam; `off` is the default, `auto` is an explicitly reported deterministic fallback in this package, and `required` exits 4 because no provider is configured.
- `--dry-run` validates and renders without database writes.
- `--explain COMPONENT_ID` prints the canonical rationale for one demanded component.
- `--strict` turns independent-validation warnings into a failed run.
- `--alert-prefixes` includes visible category prefixes in alert prose.
- `--json` writes a deterministic result object to stdout.

Every run writes one structured success or failure audit line to stderr. Human output or the result JSON goes to stdout. Successful JSON/audit output names the evidence contract, model mode, model status, commit accounting, and any partial-run exclusions; post-parse failures retain the contract and model fields in the failure audit. In commit mode, independently verified POs are inserted and owned alerts are reconciled in one transaction. External rows are never updated or deleted. A safe component-local internal failure may produce a structured partial result only after all affected actions are removed and the survivors pass a fresh independent validation; `--strict` instead preserves all-or-nothing behavior.

### Exit codes

| Code | Meaning |
|---:|---|
| 0 | Run validated and completed (including alert-only production outcomes, partial survivor commits, no-ops, and dry runs). |
| 2 | Invalid CLI scope or unsafe/missing scenario path. |
| 3 | Scenario schema or source data cannot be read safely. |
| 4 | Reviewed policy pack is invalid, or a required optional model is unavailable. |
| 5 | Planning, exact solve, or independent validation did not complete safely. |
| 6 | The database changed twice during planning; no duplicate write was made. |
| 7 | Ownership validation or the atomic commit failed. |

## Idempotency and ownership

Human-facing business columns now contain only readable summaries. `purchase_orders.rationale` explains the order, shortage, timing, and material assumptions; `alerts.description` states the issue and the required action. Agent ownership, hashes, and exhaustive audit facts are stored separately in three agent-owned tables:

- `apex_po_metadata` holds purchase-order ownership and idempotency fields.
- `apex_alert_metadata` holds alert ownership plus the full diagnostic description.
- `apex_decision_audit` holds one structured decision record per evaluated requirement and component.

The `alerts` table is intentionally operational and sparse: each agent-owned row must describe a problem that merits attention or recommend a human action. Successful-run accounting and raw assumption codes are audit facts, so they are excluded from `alerts`. When missing evidence materially affects procurement, the agent emits one consolidated, plain-language evidence alert while retaining component-level assumption details in `apex_decision_audit`.

New managed POs use metadata version 5. Before a managed PO is temporarily removed for fresh reconstruction, the planner must reproduce the complete component source fingerprint, including relevant supplier, catalog, external-PO evidence, contract, and policy facts. If any competing route or source fact changed, the old PO remains physical inbound and cannot be silently replaced by a duplicate full-demand order.

Embedded markers from versions 1 through 4 remain strictly parseable and fail closed on malformed markers, APX prefix collisions, forged payloads, incomplete line groups, or changed stored business fields. Eligible version 4 rows are migrated in place to version 5 metadata and concise prose; older rows without the all-candidate source digest remain physical commitments. Exact unchanged version 5 reruns preserve PO rows, alert IDs, metadata, audit records, and SQLite sequences. Dry runs do not create or modify these tables.

## Reviewed policy-pack workflow

`src/apex_procurement/policy/compiled_policy.json` and `concepts.json` are reviewed, checked-in runtime inputs. Their schema, provenance, hashes, effective dates, and typed parameters are validated every run. This repository does not contain a reviewed offline policy compiler, so there is no `--recompile-policy` flag. Editing source policy documents, compiling them, reviewing the diff, and checking in a new artifact is a separate controlled process; runtime planning never recompiles or waives policy.

## Operational limitations

- Catalog lead times are calendar days. Comparator windows use Monday–Friday business days with no holiday calendar.
- The snapshot has no supplier capacity history; capacity remains disclosed as unknown rather than invented.
- No safety-stock policy or safety-stock source data is represented.
- Rolling supplier history, approved exception spend, and other evidence may be absent. Benchmark assumptions are explicit; production defers decisions that require missing evidence.
- Known catalog unit price is used where no freight, tax, tariff, or other landed-cost fact exists.
- Existing PO delivery dates are trusted only after repository validation; the system has no external shipment-status feed.
- The planner operates on one SQLite snapshot and performs at most one optimistic-concurrency replan.
