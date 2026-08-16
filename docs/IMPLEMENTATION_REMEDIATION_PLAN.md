# Apex Procurement Agent Remediation Plan

**Status:** Approved implementation sequence after adversarial review  
**Authority:** `docs/MERGED_PLAN.md` remains the behavioral specification. This document turns verified implementation gaps into mergeable work packages.  
**Execution rule:** Each package is implemented in its own Codex worktree, committed on its own branch, reviewed, tested, and merged independently into `main`. Dependent worktrees start from the newly merged `main`.

## 1. Completion standard

The remediation is complete only when all work packages below are merged and all of these gates pass:

1. `python3 -m pytest -q` passes.
2. A clean install supplies a capable optimization backend; `python3 -S` is no longer presented as a supported installed runtime unless an exact fallback can complete the held-out cases.
3. All six supplied scenarios succeed under the benchmark contract on temporary copies and are idempotent on a second run.
4. All six supplied scenarios complete under the production contract with zero purchase orders and component-specific `DECISION_REQUIRED` alerts for unavailable rolling history; no validator disagreement occurs.
5. The held-out small-order and large-order cases pass. Large approval-gated lines are withheld as complete proposals and are never truncated to a threshold.
6. Every unmet deadline is represented in the decision state and by a `LATE_ARRIVAL` alert. Strictly earlier eligible supply is considered for recovery even when eventual supply already covers demand.
7. Changing an approved policy-pack threshold changes planner and validator behavior without a Python edit.
8. A malformed catalog offer or supplier attribute is quarantined at route scope with `DATA_QUALITY`; valid routes and unrelated components continue. Structural demand corruption remains a global exit-3 failure.
9. Every PO rationale names the deciding comparator and material rejected routes with rule IDs, without embedding the full decision record as an opaque base64 wall.
10. `RUN_ACCOUNTING` categories are disjoint and reconcile to the number of requirements.

Every regression must exercise the CLI on a temporary database where the defect is an end-to-end behavior. Unit tests may supplement but not replace that evidence.

## 2. Work packages

### R01 — Runtime solver and scalable independent validation

**Purpose:** Make the declared installation operational and remove demand-magnitude enumeration failures before changing plan semantics.

**Scope**

- Declare the production optimization dependency in `pyproject.toml`, or provide an exact fallback proven on every acceptance case. The preferred implementation is a declared SciPy/HiGHS dependency.
- Preserve independent validator model construction, but solve larger validator models with a capable MILP backend instead of raw-unit exhaustive enumeration.
- Keep exhaustive enumeration as the differential oracle for bounded tiny cases.
- On resource-limit or unproven status, emit only proof-status diagnostics. Do not emit baseline/calibration/objective mismatch claims derived from an unproven reference.
- Add clean-environment, large-discrete-demand, and incomplete-proof diagnostic tests.

**Acceptance**

- Installed-runtime CLI succeeds on scenario 06 without relying on ambient packages.
- FG-1002 quantities 250, 300, and 400 no longer fail with `INDEPENDENT_SOLVE_UNPROVEN`.
- Tiny generated validator cases still agree with exhaustive enumeration.
- Forced validator resource exhaustion cannot also emit `BASELINE_COST_MISMATCH`, `CALIBRATION_MISMATCH`, or `OBJECTIVE_VECTOR_MISMATCH`.

### R02 — Time-phased lateness and recovery

**Depends on:** R01

**Purpose:** Preserve deadline truth from ledgers through planning, decisions, validation, and alerts.

**Scope**

- Replace the one-pass ledger/candidate ordering with a route-aware recovery pass. The final ledger must receive eligible route material-availability dates.
- Remove the `eventual_gap == 0` optimizer short-circuit only as part of this complete recovery path.
- Represent recovery demand separately from baseline eventual demand so a zero eventual gap does not force recovery quantity and autonomy allowance to zero.
- Enforce strict improvement over committed late receipts, recovery-surplus authorization, and self-termination on rerun.
- Correct bucket allocation and `unit_late_days`; scenario 01 CMP-003 must retain the documented 240 unit-late-day floor.
- Add `LATE_ARRIVAL` decisions/alerts for every positive post-plan deadline gap, including unavoidable lateness.
- Independently validate on-time gaps, recovery quantities, lateness metrics, and required alerts.

**Acceptance**

- A late committed PO plus a strictly earlier route enters recovery planning even when eventual gap is zero.
- Equal or later routes do not create recovery orders.
- Scenario 01 and scenario 03 disclose every missed deadline; no fully covered requirement silently loses its deadline miss.
- Repeating a recovery run performs zero additional writes.

### R03 — Approval classification and complete proposals

**Depends on:** R01, R02

**Purpose:** Prevent the optimizer from reducing quantities to remain under an approval threshold.

**Scope**

- Remove order-value approval thresholds as aggregate component spending caps from solve Q, solve 0, and solve 1.
- Compute complete covering plans under procurement rules, then classify each proposed PO line against active approval thresholds.
- Withhold an approval-gated plan from execution and emit a complete `RECOMMEND_APPROVAL` proposal containing supplier, full quantity, price, total, delivery date, threshold, authority, and material timing impact.
- Apply thresholds per proposed PO line, not as one component-wide cost sum.
- Model nested thresholds correctly; a line above $150,000 must name every required authority implied by the active rules.
- Ensure `cheapest_covering_cost` is actually the cost of the certified coverage target.
- Test repeated runs so a requirement cannot be split across sub-threshold actions or collide on action identity.

**Acceptance**

- A 700-unit CMP-008 requirement at $85 produces no 588-unit executable truncation and includes a complete 700-unit approval proposal.
- A 500-unit scenario-06-derived case does not select $49,984 merely to avoid the $50,000 gate.
- Lines genuinely below the threshold continue to execute.
- Second-run behavior cannot cumulatively bypass approval or produce an action-key collision.

### R04 — Production evidence-contract dispositions

**Depends on:** R01–R03

**Purpose:** Preserve rule-level contract dispositions instead of converting evidence absence into supplier ineligibility.

**Scope**

- Carry hard `UNKNOWN` evidence and its `contract_disposition` through candidate, requirement, explanation, and validator layers.
- Distinguish `EVIDENCE_BLOCKED`/`DECISION_REQUIRED` from true `NO_ELIGIBLE_SUPPLIER`.
- Never render an unavailable production fact as an assumption the agent relied upon.
- Validate non-executable alternatives according to their disposition rather than applying executable-plan checks indiscriminately.
- Add all-six-scenario production CLI tests.

**Acceptance**

- Production contract writes zero POs in all six supplied scenarios.
- Every in-scope missing rolling window yields component-specific `DECISION_REQUIRED` and contract evidence, not `ASSUMPTION` or `NO_ELIGIBLE_SUPPLIER`.
- All six runs exit successfully without validator objective/date/eligibility disagreement.

### R05 — Policy-pack authority

**Depends on:** R03, R04

**Purpose:** Make the compiled policy pack the single source of behavioral thresholds.

**Scope**

- Remove numeric policy matching and duplicated thresholds from `candidates.py`, `optimizer.py`, and `validator.py`.
- Select applicable rules by rule identity/kind and semantic scope, never by testing for known numeric values.
- Add the provisional `economic_autonomy` block to the compiled pack and load it into both planner and validator.
- Source domestic premiums, strategic savings, sustainability windows, approval values, and autonomy values from typed registry accessors.
- Print active provisional autonomy values in decision disclosures as required by the plan.

**Acceptance**

- Mutating a pack threshold in a temporary pack changes both planner and validator outcomes without `StopIteration` or code edits.
- A static contract test rejects duplicated load-bearing policy literals in production Python.
- Planner and validator receive the same typed parameters from the registry while retaining independent calculations.

### R06 — Route-local malformed-data quarantine

**Depends on:** R04

**Purpose:** Apply the blast-radius matrix from `MERGED_PLAN.md`.

**Scope**

- Separate structural snapshot failures from route-local supplier/catalog parse issues.
- Preserve enough raw issue context to quarantine only the malformed offer or affected supplier route.
- Emit stable `DATA_QUALITY` alerts naming table, logical key, field, and remediation without leaking unsafe raw control characters.
- Continue valid components and routes; globally fail demand-distorting BOM, schedule, inventory, and configuration corruption.

**Acceptance**

- One `unit_price='n/a'`, null lead time, or malformed supplier attribute skips that route and the run continues.
- If another valid route covers the component it can still execute.
- Structural demand corruption still exits 3 with no writes.

### R07 — Defensible, usable outputs and repository completion

**Depends on:** R02–R06

**Purpose:** Make outputs reconstructable by humans without drowning operational signals.

**Scope**

- Replace the full base64-encoded `DecisionRecord` ownership payload with a compact, versioned marker plus sufficient stable digests. Reconstruct and revalidate current decisions from source facts and stored business fields.
- Render the comparator that decided selection and material rejected routes with rule IDs and quantified deltas.
- Deduplicate run-global assumption/evidence facts while retaining component traceability.
- Make every alert answer what is wrong, what the agent did, and what a human should do.
- Make `RUN_ACCOUNTING` categories mutually exclusive and assert their sum equals total requirements.
- Add `README.md` with install, command, contracts, exit codes, optional model behavior, and operational limitations.
- Either implement `--recompile-policy` through the reviewed offline compiler path or remove the nonfunctional flag and document compilation separately.

**Acceptance**

- PO rationales remain deterministic and ownership-safe, are materially smaller, and contain comparator/rejection facts.
- Scenario alerts surface late/unmet/approval/decision signals without dozens of duplicate contract statements.
- Accounting counts reconcile exactly.
- Two unchanged runs remain row-level idempotent with stable alert IDs and `sqlite_sequence`.

## 3. Merge and verification protocol

For every package:

1. Start a fresh worktree from current `main` after all dependencies are merged.
2. Require the task agent to read this plan and the cited `MERGED_PLAN.md` sections.
3. Require tests that fail on the pre-change implementation and pass after the change.
4. Require the agent to commit only package-scoped files with a descriptive commit message.
5. Review the branch diff and rerun package tests in the branch.
6. Merge with `--no-ff` so the package boundary remains visible.
7. Run the full test suite on `main` before starting a dependent package.

Unrelated user files, including `docs/PLAIN_PLAN-2x.wav`, must remain untouched.

## 4. Final audit evidence

The final audit records:

- merge commit for every R01–R07 package;
- full pytest result and subtest count;
- benchmark and production results for all six temporary scenario copies;
- idempotency row diffs;
- clean-install solver result;
- late-inbound, unavoidable-lateness, 700-unit approval, 500-unit approval, malformed-route, and pack-mutation regression outputs;
- final `git status`, explicitly distinguishing pre-existing untracked files from implementation changes.
