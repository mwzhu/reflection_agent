# R16 Final Integration and Correctness Audit

**Package audited:** R16 final integration package, committed with subject `Complete R16 final integration audit`

**Baseline audited:** `main` at `3c56df7cf0793f106b52ec92ce90b761055f1594` (`Merge R15 safe component isolation`)

**Specification:** `docs/MERGED_PLAN.md` plus R16 in `docs/POST_R07_CORRECTNESS_PLAN.md`

**Environment:** Python 3.14.6, SciPy 1.18.0, deterministic `--llm=off`/fallback path, no network provider

**Result:** acceptance criteria pass with zero xfails and zero runtime skips.

## Composition result

R16 exercises the actual CLI, optimizer, independent validator, decision renderer, and atomic SQLite commit boundary. Every supplied database and database mutation is copied to a disposable temporary path. No test writes a supplied source fixture. Second-pass assertions compare canonical business rows and exact write state, including alert IDs and `sqlite_sequence`.

The integration sweep found and fixed one composition gap: when production rolling evidence and a separate below-B review were both unknown, the optimizer could omit the required non-executable diagnostic. The diagnostic-only solver may now quantify that combined unresolved proposal, but it restores every approval fact before returning it. The independent validator separately reconstructs the route, requires the no-better-alternative proof and complete review marker, and still forbids every production write. Proven failures remain excluded. The executable optimizer, commit implementation, atomic transaction, and survivor-revalidation boundary were not relaxed.

## Six-scenario benchmark/production matrix

Command:

```text
/usr/bin/time -p python3 -m pytest -q tests/integration/test_r16_final_matrix.py
```

Result: **12 passed in 36.29s** (real 36.46s). Each row below represents a first CLI commit pass and a second unchanged CLI pass. `New POs` excludes pre-existing external POs; `Final POs` includes them. Every second pass returned exit 0, `no_op=true`, zero committed PO numbers, zero inserted alerts, zero deleted alerts, and byte-stable canonical business/write state. Source hashes and all logical source rows remained unchanged.

| Scenario | Contract | First exit | New POs | Final POs | Final alerts | Selected executable dispositions | Second pass |
|---|---|---:|---:|---:|---:|---|---|
| 01 baseline | benchmark | 0 | 15 | 15 | 37 | 13 `EXECUTE_WITH_ASSUMPTION` | exit 0, exact no-op |
| 01 baseline | production | 0 | 0 | 0 | 56 | none | exit 0, exact no-op |
| 02 partial procurement | benchmark | 0 | 13 | 17 | 37 | 11 `EXECUTE_WITH_ASSUMPTION` | exit 0, exact no-op |
| 02 partial procurement | production | 0 | 0 | 4 | 49 | none | exit 0, exact no-op |
| 03 tight timeline | benchmark | 0 | 19 | 19 | 40 | 17 `EXECUTE_WITH_ASSUMPTION` | exit 0, exact no-op |
| 03 tight timeline | production | 0 | 0 | 0 | 80 | none | exit 0, exact no-op |
| 04 low inventory | benchmark | 0 | 23 | 23 | 41 | 19 `EXECUTE_WITH_ASSUMPTION` | exit 0, exact no-op |
| 04 low inventory | production | 0 | 0 | 0 | 83 | none | exit 0, exact no-op |
| 05 competing demand | benchmark | 0 | 18 | 18 | 39 | 17 `EXECUTE_WITH_ASSUMPTION` | exit 0, exact no-op |
| 05 competing demand | production | 0 | 0 | 0 | 60 | none | exit 0, exact no-op |
| 06 simple | benchmark | 0 | 2 | 2 | 16 | 2 `EXECUTE_WITH_ASSUMPTION` | exit 0, exact no-op |
| 06 simple | production | 0 | 0 | 0 | 14 | none | exit 0, exact no-op |

The counts above are audit observations, not disposition oracles. The permanent test derives each expected disposition from active evidence:

- any selected benchmark plan whose hard rule evidence includes rolling-window `UNKNOWN` with the benchmark contract disposition must be `EXECUTE_WITH_ASSUMPTION` and carry named assumption codes;
- `EXECUTE` is permitted only when no hard rule requirement remains `UNKNOWN`; other reviewed assumptions may still require `EXECUTE_WITH_ASSUMPTION`;
- any production decision with required hard `UNKNOWN` evidence has no selected plan, carries `DECISION_REQUIRED`, and commits no PO.

The principal CLI suite also contains a real scenario-06 shortage. It asserts positive initial gaps, certified executable solver outcomes, independent solver verification, rendered owned rationales/alerts, and actual committed rows. The covered-demand setup remains only an offline/dry-run/no-op smoke fixture.

## R08 mutation and strictness audit

Command:

```text
/usr/bin/time -p python3 -m pytest -q tests/integration/test_r08_date_mutations.py tests/integration/test_r08_decision_mutations.py tests/integration/test_r08_policy_mutations.py tests/unit/test_r08_fixture_builders.py tests/unit/test_r08_renderer_mutation.py tests/integration/test_r16_mutation_matrix.py
```

Result: **54 passed in 66.88s** (real 67.10s). The R16 matrix runs each database mutation under benchmark/production and standard/strict modes on separate copies, compares standard/strict business rows, and preserves the package-specific focused assertions.

| Mutation | Benchmark standard/strict | Production standard/strict | Focused result |
|---|---|---|---|
| unknown country (`Freedonia`) | exit 0/0; 2/2 new POs | exit 0/0; 0/0 new POs | both-ways route facts cross independent validation |
| renamed magnet | exit 0/0; 13/13 non-magnet POs | exit 0/0; 0/0 new POs | magnet withheld conservatively; unrelated components proceed |
| both named magnet suppliers replaced | exit 0/0; 13/13 new POs | exit 0/0; 0/0 new POs | shaping conflict stays component-scoped; production diagnostic retains review evidence |
| pre-memo date | exit 0/0; 13/13 new POs | exit 0/0; 0/0 new POs | active rules, dates, leads, and rationale agree independently |
| pre-policy date | exit 4/4; 0/0 new POs | exit 4/4; 0/0 new POs | clean business refusal; no internal registry assertion and no writes |
| MOQ 25 / net need 5 | exit 0/0; 1/1 new PO | exit 0/0; 0/0 new POs | coherent MOQ execution; no live mutually exclusive sub-MOQ request; rerun no-op |
| stale named supplier ID | exit 0/0; 15/15 new POs | exit 0/0; 0/0 new POs | legal-name resolution plus scoped `DATA_QUALITY` disclosure |
| unknown UoM | exit 0/0; 15/15 new POs | exit 0/0; 0/0 new POs | discrete ceiling disclosed; no pack-size guess |
| unrendered withholding policy | exit 4 in all four modes | exit 4 in all four modes | rejected at CLI policy-pack load boundary; no writes |

The separate negative diagnostic mutation removes the restored below-B review marker and proves that the validator raises `BELOW_B_REVIEW_UNREPRESENTED`. No xfail, skip, or scratch-only mutation is used.

## Model-mode seam

Command:

```text
/usr/bin/time -p python3 -m pytest -q tests/integration/test_optional_model_cli.py
```

Result: **2 passed in 0.88s** (real 1.05s).

- `--llm=off`: `model_status=disabled`.
- `--llm=auto`: exactly `model_status=unavailable_deterministic_fallback` in result JSON and the success audit.
- Auto and off run with socket construction prohibited and produce identical purchase-order and alert business rows plus identical non-model decision content.
- `--llm=required`: exit 4 before writes; failure audit reports `model_mode=required` and `model_status=required_unavailable`.
- No provider probing, residual-model resolution, or new network dependency exists in live planning.

README, `--help`, human output, JSON, and audit output now agree on evidence contracts, exit codes, alert-only/no-op/partial outcomes, strict all-or-nothing behavior, and model fallback.

## Recorded verification commands

| Layer | Command | Result |
|---|---|---|
| Baseline before R16 | `/usr/bin/time -p python3 -m pytest -q` | 316 passed, 199 subtests; 194.48s (real 194.68s) |
| Unit | `/usr/bin/time -p python3 -m pytest -q tests/unit` | 248 passed, 162 subtests; 61.37s (real 61.56s) |
| Integration | `/usr/bin/time -p python3 -m pytest -q tests/integration` | 101 passed, 37 subtests; 192.14s (real 192.31s) |
| Generated/differential | `/usr/bin/time -p python3 -m pytest -q tests/unit/test_generator.py tests/unit/test_optimizer.py::HighsDifferentialTests::test_generated_small_solve_q_cases_match_enumeration tests/unit/test_validator.py::IndependentInvariantRecomputationTests::test_tiny_exhaustive_oracle_agrees_with_independent_milp` | 14 passed, 31 subtests; 1.45s (real 1.62s) |
| Performance | `/usr/bin/time -p python3 -m pytest -q tests/performance/test_operational_budget.py` | 1 passed; 3.60s (real 3.73s); two immediate repeats passed in 3.60s and 3.64s, within the unchanged 10s/128 MiB budget |
| Scenario matrix | command above | 12 passed; 36.29s (real 36.46s) |
| Mutation/focused | command above | 54 passed; 66.88s (real 67.10s) |
| Model parity | command above | 2 passed; 0.88s (real 1.05s) |
| Final full suite | `/usr/bin/time -p python3 -m pytest -q` | **350 passed, 199 subtests; 248.03s (real 248.23s)** |

The first attempted final full run completed 349 tests but failed the performance assertion at 10.032s against the unchanged 10.0s budget. Profiling found repeated Python-level character scans over already-safe large ASCII rationales. A printable-ASCII fast path moved those checks into CPython's C predicates while preserving the original character-by-character path for Unicode, controls, bidi formatting, and malformed surrogates. The budget was not raised. Serialization tests passed, three performance reruns measured 3.60s/3.60s/3.64s, and the final full run above passed.

## Marker, source, boundary, and package audit

Marker command:

```text
rg -n "pytest\.mark\.(xfail|skip|skipif)|@unittest\.skip|@unittest\.skipIf|@unittest\.skipUnless" tests
```

Result: no xfail marker and one intentional conditional marker at `tests/unit/test_optimizer.py:594`, which guards the SciPy-specific differential class when only the stdlib fallback is installed. `scipy_available=True` in this audit environment, so it executed. Final pytest output contains zero xfails and zero skips.

Additional inspection:

- production Python contains no supplied entity-ID literals matching `\b(CMP|SUP|FG|RM|MFG|PO)-\d+\b`;
- the only runtime `httpx` import remains lazy inside `policy/model_adapter.py`; the CLI never imports or invokes it, and socket-prohibited auto/off tests pass;
- validator independence remains intact: `validator.py` does not import optimizer implementation code and independently reconstructs diagnostic eligibility/review evidence;
- commit code was not changed; transaction/digest/rollback, R15 global-failure, strict, and second-pass survivor validation tests pass;
- `find` reports no package-owned SQLite journal/WAL/SHM, `.tmp`, or `.log` artifacts; generated databases and results stayed in system temporary directories;
- all nine frozen R08 observations map to tracked deterministic fixture builders and focused tests.

Final package status distinction:

- after the required single R16 commit, `git status --short --untracked-files=all` in this dedicated worktree is empty;
- the separate main workspace at `/Users/abcd/Downloads/Take-Home Assignment` retains the pre-existing **untracked** `docs/PLAIN_PLAN-2x.wav` (`?? docs/PLAIN_PLAN-2x.wav`);
- that WAV is not present in this dedicated worktree, is not part of the R16 package, and was not edited or deleted.

## Residual limitations and deferred items

No R16 acceptance gap remains. The plan's intentional deferrals remain:

- approval ingestion awaits an authoritative approval source and proposal-binding contract;
- optional residual model resolution remains non-load-bearing and is not wired into live planning;
- runtime memo ingestion and a general constraint DSL remain outside this reviewed fixed-pack prototype;
- complete rolling history, receipt/acceptance provenance, supplier capacity, freight cost/spend, and lead-time-drift semantics require new authoritative data;
- existing schema limitations still prevent proof of PCB receipts, safety-stock state, supplier throughput, holiday calendars, and production-stoppage eligibility.

These limitations are disclosed behavior, not hidden execution fallbacks. Production continues to withhold any plan whose required evidence remains `UNKNOWN`.
