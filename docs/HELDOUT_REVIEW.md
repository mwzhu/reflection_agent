# Held-Out Scenario Review

**Purpose:** stress `docs/MERGED_PLAN.md` against the scenarios Reflection will plausibly run, and
identify what must change before implementation starts.

**Method:** the brief states *"We will also run your agent against held-out scenarios not included
here. Design to generalize."* This document enumerates what a held-out scenario can vary, catalogs
~50 concrete ones, and records what the current design does on each. Findings marked **[C]** were
confirmed by computation against the provided data; the rest are reasoned from the design text.

---

## 1. Scoping: what a held-out scenario can actually vary

The brief pins two things down, and they bound the threat model:

- *"The policies and memos are shared across all scenarios (they're company-wide documents)."* → the
  held-out set almost certainly ships **new SQLite files only**, not new PDFs. The compiled policy
  pack does not need to absorb an unseen memo at runtime.
- *"Each scenario is a self-contained SQLite database representing a snapshot."* → every held-out
  input is one file with the same ten tables.

**[C]** Across the six provided scenarios, `products`, `components`, `bom`, `suppliers`, and
`supplier_catalog` are byte-for-byte identical in content; only `scenario_config`, `inventory`,
`production_schedule`, and `purchase_orders` vary. Two consequences:

1. **Highest-probability variation is in those four tables.** Robustness there is worth more than
   exotic master-data handling, because that is what the scenario generator demonstrably touches.
2. **Master-data extension is the stated generalization test.** "In practice, the customer has dozens
   of product lines" plus "design to generalize" is a broad hint that at least one held-out scenario
   adds products, components, or suppliers.

Budget effort accordingly: harden the four varying tables first, then master-data generality.

---

## 2. Held-out scenario catalog

Verdicts: ✅ design handles it · ⚠️ underspecified or degrades · ❌ produces a materially wrong result.

### Group A — the four tables that already vary (highest probability)

| # | Scenario | Probes | Design behavior | |
|---|---|---|---|---|
| A1 | Inventory covers all demand | Does it hallucinate orders? | Zero POs + explicit "no procurement needed" accounting | ✅ |
| A2 | Empty `production_schedule` | Degenerate input | Alert, exit 0, no crash (§6) | ✅ |
| A3 | **Single small order** (e.g. FG-1002 × 2, empty inventory) | MOQ vs. net need | **13 of 14 components write no PO** | ❌ **F1** |
| A4 | **Single very large order** (FG-1002 × 400) | §7 approval thresholds | Correctly withholds the unapproved line; the alert must preserve a complete, dated proposal for review and later revalidation | ⚠️ **F2** |
| A5 | 30–50 schedule rows across all products | Aggregation, perf, MILP count | Per-component MILPs, parallelisable | ✅ |
| A6 | All `materials_needed_by` in the past | Negative lead windows | Alerted degenerate input (§6); every route infeasible → alert-only run | ⚠️ |
| A7 | `materials_needed_by == current_date` | Zero-day window | Only zero-lead routes feasible; none exist → all late | ✅ |
| A8 | Existing POs fully cover demand | Inbound netting | `eventual_gap = 0` → no new POs | ✅ |
| A9 | Existing PO with `expected_delivery_date < current_date` | Overdue inbound | Right intent, but §6 words the exclusion against the wrong date and would discard all four of scenario 02's POs if read literally | ⚠️ **F14** |
| A10 | Existing PO with NULL delivery date / NULL price | Null-safety | Underspecified: is a null-dated PO inbound at all? | ⚠️ |
| A11 | Existing PO from SUP-113 (off ASL) | Pre-existing violation | `PRE_EXISTING_VIOLATION` alert, never remediated (D17) | ✅ |
| A12 | Existing PO referencing a component/supplier absent from master data | No FK on some tables | Must alert, not crash — `foreign_key_check` passes today so this is untested | ⚠️ |
| A13 | Needed component has no `inventory` row | Sparse inventory | Treated as 0 (§6) | ✅ |
| A14 | Negative `quantity_on_hand` | Data error | Underspecified — clamp to 0 + `DATA_QUALITY`? | ⚠️ |
| A15 | Zero or negative production quantity | Data error | §6 says "alert, never crash", but a negative quantity distorts shared demand — per F7's blast-radius matrix this is structural and must fail globally. The two sections disagree | ⚠️ **F7** |
| A16 | `current_date` = 2025-02-01 (before every memo) | Effective windows | Base policy only: 70% cap, no ≥20% secondary, no PCB freeze, no air freight. Strong test of the pack | ✅ |
| A17 | `current_date` = 2025-09-30 vs 2025-10-01 | Air-freight boundary | Explicitly unit-tested (§12) | ✅ |
| A18 | `current_date` = 2026-06-01 | Long-expired context | PCB freeze has `effective_through: null` → still binding 10 months after its "60–90 day" estimate | ⚠️ **F12** |
| A19 | `scenario_config` empty, multi-row, or NULL date | Malformed config | Should fail fast with exit 3 | ⚠️ |
| A20 | `current_date` in a leap year / non-ISO date format | Date parsing | ISO assumed; format variants unhandled | ⚠️ |

### Group B — master-data extension ("design to generalize")

| # | Scenario | Probes | Design behavior | |
|---|---|---|---|---|
| B1 | New product with new components, unseen names ("Samarium Cobalt Magnet", "MCU (RISC-V)") | Concept recall | An earlier draft called a recall miss "the safe direction." **It is the unsafe direction in all three places it matters:** non-critical means a *35%* premium threshold (international unlocks more easily, not less), an *85%* concentration cap instead of 70%, and no dual-source diagnostic | ❌ **F5** |
| B2 | New supplier, cheapest, no certifications | Certification gate | Correct only if the "electronic components / safety-critical" scope is pinned down | ⚠️ **F5** |
| B3 | Component with zero `supplier_catalog` rows | Empty candidate set | `NO_ELIGIBLE_SUPPLIER` alert (§6) | ✅ |
| B4 | Component whose only supplier is off the ASL | Hard gate | `NO_ELIGIBLE_SUPPLIER` alert | ✅ |
| B5 | **Component whose only supplier holds no certifications** | "Safety-critical" scope | Both-ways evaluation may block it entirely | ❌ **F5** |
| B6 | `country` = "United States", "Mexico", "Vietnam", "UK" | Domestic normalization | §5.3 says "normalised country" but names no alias table or unknown-value fallback | ⚠️ **F8** |
| B7 | `sustainability_rating` = NULL, "AA", "C-", "Gold" | Ordinal parsing | Must not crash; unknown ≠ below-B | ⚠️ **F8** |
| B8 | `certifications` = "ISO 9001; UL Listed" or "iso-9001" | Canonicalization | Case-insensitive split specified; separator/spacing variants are not | ⚠️ **F8** |
| B9 | Suppliers renumbered SUP-2xx, names unchanged | Source-named entity resolution | §12's own test asserts `DECISION_REQUIRED` when the database is renamed alone — so all magnet demand blocks even though the legal name is unambiguous | ❌ **F4** |
| B10 | **Magnet suppliers are different companies entirely** (no Nanjing, no MagnetPro) | Unresolvable memo entity | §12: `DECISION_REQUIRED` for **every requirement the rule scopes** → all magnet demand blocked | ❌ **F4** |
| B11 | Three or more magnet suppliers | ≥20% secondary with n > 2 | Linear 80% bound generalizes correctly | ✅ |
| B12 | A second PCB supplier with a prior PO row | Incumbency ladder | Rung 1 of §5.3 matches | ✅ |
| B13 | Component with `requires_certification` = "AS9100" | Unknown cert string | Union of column + policy rules → no eligible supplier + alert | ✅ |
| B14 | Large hazmat demand (CMP-010/011 or new) | §5 hazmat handling | Flagged for procurement review; receiving buffer defaults 0 with an alert (§7) | ✅ |
| B15 | `bom` row referencing a nonexistent product or component | No FK declared | Application-level validation is mandated (§2.1) | ✅ |
| B16 | Scheduled product with no BOM rows | Silent zero-demand hazard | Alerted degenerate input (§6) | ✅ |
| B17 | `minimum_order_qty` = 0/NULL, `lead_time_days` = 0/negative/NULL, `unit_price` = 0/NULL | Null and zero handling | Listed in §6's degenerate set; arithmetic guards unspecified | ⚠️ |
| B18 | Two suppliers, exactly equal price/lead/tier/rating | Deterministic tie-break | Supplier fingerprint; identical fingerprints → `DATA_QUALITY`, no autonomous pick (§8.2) | ✅ |
| B19 | Strategic supplier vs. a >15% cheaper domestic alternative | §9 comparator | Comparator 3 exists but **never fires in the provided data** — untested path | ⚠️ |
| B20 | Two suppliers within 10% price / 5 business days, ratings A vs. B | §8 comparator | Comparator 4 exists; also never fires locally | ⚠️ |
| B21 | Component with no domestic source at all | §3 condition (c) | Gate opens, international executes with documented justification | ✅ |
| B22 | Component with no international source | §3 premium ratio | `(best_domestic − best_intl)/best_intl` has no denominator — needs an explicit guard | ⚠️ |

### Group C — policy-collision scenarios

| # | Scenario | Probes | Design behavior | |
|---|---|---|---|---|
| C1 | The only on-time supplier is off the ASL | Does policy win over the deadline? | Orders the compliant late supplier (stage 1 coverage ≻ stage 2 lateness) + `LATE_ARRIVAL` alert. **This is the key "does it still act" test and the design passes it** | ✅ |
| C2 | The only on-time supplier lacks UL (scenario 03 generalized) | Cert vs. deadline | Same shape as C1 | ✅ |
| C3 | Magnet demand with exactly one eligible magnet supplier | ≥20% secondary unsatisfiable | Correctly blocks — the rule constrains an order's allocation, so a 100% single-supplier order violates it outright. But the alert must carry a fully-costed, explicitly non-executable one-supplier counterfactual | ⚠️ **F9** |
| C4 | Deadline reachable only from an international supplier | §3 condition (a) | Gate opens; justification and computed premium in the rationale | ✅ |
| C5 | Air-freight window active and needed to hit a deadline | Approval-gated route | Standard-lead route still executes (late) and air freight surfaces as `RECOMMEND_APPROVAL` — no zero-PO hole here | ✅ |
| C6 | PCB shortage where SUP-101 is also ineligible | Freeze + sole-source | `NO_ELIGIBLE_SUPPLIER` + `SOLE_SOURCE` diagnostics | ✅ |
| C7 | Pre-existing POs put one supplier above 50% of visible magnet volume | Rolling-window semantics | Reports the visible ratio, does not assert a breach, does not remediate (§2.2, D17) | ✅ |
| C8 | A shortage whose only route is a below-B supplier not named in any memo | §8 discharge | No memo discharge → `DECISION_REQUIRED`, no PO. Correct but zero-PO — verify the alert is legible | ✅ |

### Group D — adversarial and operational

| # | Scenario | Probes | Design behavior | |
|---|---|---|---|---|
| D1 | Prompt injection in `suppliers.notes` / `components.description` | Instruction/data boundary | `--llm=off` default → no model reads it; §14 treats all DB text as untrusted data | ✅ |
| D2 | SQL metacharacters, control chars, hostile Unicode in text fields | Injection, output sanitation | Bound parameters; control chars sanitised in rationales (§14) | ✅ |
| D3 | NaN / Infinity stored in a REAL column | Numeric guards | Listed in §12 adversarial tests | ✅ |
| D4 | Quantity = 1e12 | Overflow, `U` derivation | `Decimal` throughout; `U` derived from demand — should hold | ⚠️ |
| D5 | `alerts` pre-populated with human-written rows | Reconciliation safety | Never touched, never counted (§10.4) | ✅ |
| D6 | `alerts` pre-populated with rows carrying the agent's ownership marker | Marker spoofing | Agent would delete them as "obsolete owned alerts" | ⚠️ |
| D7 | Database file read-only or locked | Commit failure | Exit 7, no partial write | ✅ |
| D8 | Generated `po_number` collides with an existing purchase order | PK uniqueness | Cross-table reuse of `PO-####` is legal; the real risks are collision with an existing `purchase_orders.po_number` and ambiguous authorship | ⚠️ **F11** |
| D9 | Run the agent twice | Idempotency | Zero writes on the second run (§10.3–10.4) — likely to be tested | ✅ |
| D10 | Extra columns, reordered columns, missing optional column | Schema drift | Read by name, extras ignored, optional degrade with `DATA_QUALITY` (§12) | ✅ |
| D11 | Grading environment lacks `scipy` / `pydantic` / `pyyaml` | Runtime deps | `python3 agent.py --scenario X.sqlite` raises ImportError → **scores zero on every scenario** | ❌ **F3** |

---

## 3. Findings

### F1 — [Critical] The economic autonomy bound blocks routine MOQ-forced orders

**[C] Measured.** Applying §8.3's `max_surplus_fraction: 0.10` to total surplus, on a held-out
scenario of *FG-1002 × 2 with empty inventory*:

```
CMP-001  need 10    MOQ 10   order 10   surplus   0 (  0.0%)  EXECUTE
CMP-002  need 20    MOQ 25   order 25   surplus   5 ( 25.0%)  DECISION_REQUIRED (no PO)
CMP-005  need  4    MOQ 25   order 25   surplus  21 (525.0%)  DECISION_REQUIRED (no PO)
CMP-011  need  1    MOQ  5   order  5   surplus   4 (400.0%)  DECISION_REQUIRED (no PO)
...
→ 1 purchase order written, 13 components blocked
```

*FG-1004 × 3, empty inventory* → 2 written, 5 blocked. The provided six scenarios have at most **one**
component per scenario where MOQ exceeds 1.1 × net need, which is precisely why the bound looks
harmless in the fixtures. The design is calibrated to the fixtures' demand magnitudes.

The design has already written down the symptom and accepted it as correct. §12:

> *"adding inventory can worsen the outcome"* … *"Autonomous executability is not monotone, because it
> is gated by a ratio."*

That non-monotonicity is not a property worth preserving. It is the defect.

**Root cause:** §8.2's `T_group − net_requirement ≤ max_surplus` does not distinguish two different
things. *Discretionary* surplus — buying extra to pull a delivery date in — is a real judgement call
worth bounding. *Forced* surplus — the supplier's MOQ, or the minimum split the ≥20% rule requires —
is not a decision at all. Every procurement planner in the world buys the MOQ.

**Fix:**

1. Compute `forced_surplus` = the surplus of the minimum-surplus plan that covers the requirement
   under all hard rules. Compute `discretionary_surplus = total_surplus − forced_surplus`.
2. Apply `max_surplus_fraction` to **discretionary surplus only**.
3. Make the cash figure an **advisory review threshold, not an executability bound**. An earlier draft
   called `max_forced_surplus_value_usd` a bound and then said to write the order anyway when it was
   exceeded — which is not a bound. Forced surplus is not a decision the agent is making, so it does
   not gate execution at all: crossing $2,500 of forced surplus emits a new `FORCED_SURPLUS` alert and
   nothing more. Do not reuse `RECOVERY_SURPLUS`, which denotes duplicate supply bought to recover
   lateness. Execution remains subject to the *genuine* policy gates (§7's $50k/$150k thresholds on
   the line total), which are Apex's rules rather than ours.
4. **Offer the sub-MOQ route the policy already grants.** Policy §4.1: *"Orders below MOQ require
   written supplier approval."* The plan never models this, so an extreme MOQ currently has only two
   framings — overbuy or nothing. There is a third, and it is the one a human would reach for when
   the need is 1 and the MOQ is 1,000: alert with both options costed — (a) order the MOQ, surplus
   $X; (b) seek written supplier approval for a sub-MOQ order of N units. Add
   `documentation_requirement` handling for §4.1 and a `RECOMMEND_APPROVAL` candidate carrying it.

The claim to avoid overstating is "every planner just buys the MOQ." Usually true, and false at the
extremes, where negotiation or a §4.1 sub-MOQ request is the rational move. The design should offer
both rather than assume either.

This also revises scenario 02: rather than writing no magnet PO against a 58-unit need, write the
150-unit minimum compliant order (`$615`, +92 surplus), alert the overbuy with its cost, and offer
the non-compliant 58-unit alternative as the `DECISION_REQUIRED` counterfactual. That inverts which
outcome is the default and which is the exception, and it is the right way round.

### F2 — [Medium] Approval-gated plans need complete, actionable recommendations

**[C] Measured.** §7's $50k / $150k gates never fire in the provided data (largest line ≈ $6,050).
On plausible held-out volume:

| Held-out scenario | Run total | Lines over $50k |
|---|---:|---:|
| Provided data, largest case | $18,958 | 0 |
| FG-1002 × 400 | $257,904 | 1 (CMP-008 at $66,725) |
| Each product × 500 | $677,343 | 6 |

Under §9, `RECOMMEND_APPROVAL` writes no purchase order. That is the correct commitment behavior:
there is no cheaper compliant fallback, and ordering less fails coverage. The held-out risk is not the
withholding itself but an alert that says only "approval required" and discards the fully worked plan.

**Fix — revised after review.** An earlier draft of this finding recommended defaulting to
`write_and_flag`, reasoning that the `rationale` column plus an `alerts` sibling table look like a
proposal queue. That inference does not survive the brief's own wording: *"purchase_orders — each row
is an order the agent places."* That is commitment language, and Policy §7 makes orders over $50,000
conditional on Procurement Manager approval. Writing an unapproved $66,725 order to buy a grading
hedge would breach the design's first commitment — *a proven policy violation is never written to
`purchase_orders`* — and that commitment is worth more than the hedge.

**Keep `RECOMMEND_APPROVAL` as no-PO by default.** What must change is the alert, not the disposition:

- The `APPROVAL_REQUIRED` alert must carry the **complete proposed order** — component, supplier,
  quantity, unit price, line total, expected delivery date, the threshold crossed, and the named
  approving authority — so a human can evaluate the exact proposal. Approval does **not** make that
  snapshot safe to place later: inventory, demand, prices, and delivery dates may have changed. Feed
  approval evidence back into a fresh run, revalidate the snapshot, and recompute dates before any
  row is committed. A withheld order that is fully specified and dated in an alert is useful; one
  described only as "requires approval" is not.
- **Do not expose a `--approval-gate` flag.** An earlier draft proposed one. It would let a CLI option
  move a `RECOMMEND_APPROVAL` candidate into `purchase_orders`, which is precisely what §8.3 forbids:
  *"Configuration may never move an option between frontiers… config would become a waiver
  mechanism."* The stance is not a knob. If Apex answers Q26 with "rows are proposals," it becomes a
  **declared data contract** in the same shape as §3's evidence contracts — named, alerted on every
  run, and reviewable — not a command-line switch. Until then only the withholding behavior ships.
- State the default and its reversal condition in the README.
- Once F1 is fixed, the residual zero-PO exposure is confined to genuinely large orders — where
  routing a $66k commitment to a human is plainly the right behavior rather than a failure.

### F3 — [High] Dependency surface risks a zero on every scenario

§15 lists `pydantic`, `httpx`, `pypdf`, `jsonschema`, `pyyaml`, `scipy`, `pytest`, `hypothesis`. The
graders will unzip a file and run `python3 agent.py --scenario X.sqlite`. Any missing wheel is a
total failure across the entire held-out set — a strictly larger risk than any modelling error in
this document.

**Fix:** make the default path **stdlib-only**.

- Ship the compiled policy pack as **JSON**, not YAML (`json` is stdlib; `pyyaml` becomes dev-only).
- Replace `pydantic` with `dataclasses` + explicit validators in the core; keep `jsonschema`
  validation as a dev/CI check of the pack, not a runtime import.
- `pypdf` is offline-compile-only — it must not be imported at runtime.
- `httpx` is behind `--llm`, which is off by default — import it lazily inside the adapter.
- **`scipy` is the real one.** §8.2 already notes that "support enumeration plus an LP is equally exact
  for small instances" and that exhaustive enumeration is the differential oracle. Promote that to a
  first-class stdlib solver behind the same `Solver` protocol, used automatically when `scipy` is
  absent. **Do not claim arbitrary enumeration is cheap** — it is tractable at the visible catalog's
  scale (≤ 4 suppliers, `U` in the hundreds) and is not for a held-out component with ten suppliers
  and `U` in the tens of thousands. The fallback should be bounded branch-and-bound over the
  integer-scaled model that reports `UNRESOLVED` on budget exhaustion, exactly as §8.2 already
  requires for a timed-out solve; exhaustive enumeration stays the differential oracle for small
  cases only. **Sequence this last.** A hand-written exact solver is real effort and real correctness
  risk; it must not delay or destabilise the working `scipy` path. The cheap, high-value parts of this
  finding — lazy imports, JSON pack, no runtime PDF parsing, the clean-environment CI job — are
  independent of it and should land first.
- Split clean-environment CI by implementation stage. Before the fallback solver exists, a bare
  `python:3.11-slim` job covers import boundaries, `--help`, policy-pack loading, and snapshot loading
  without pip; it must prove optional/offline packages are not imported. After the bounded fallback
  exists, extend that job to run all six scenarios with no pip install. Do not require the full
  no-pip scenario suite before the only no-pip solver has been implemented.

### F4 — [High] An unresolvable memo-named supplier blocks the whole rule scope

§12: when a source-named entity resolves to nothing, the design emits *"`DECISION_REQUIRED` for every
requirement the rule scopes."* For MEMO-2025-041 the scope is *all neodymium magnet demand*. A
held-out database whose magnet suppliers are different companies therefore writes **zero magnet POs**
— even though the memo's other two clauses (the rolling cap and the ≥20% secondary allocation) key
off the *component* concept and remain perfectly evaluable.

**Fix, part one — resolve more cases before blocking.** §12 requires ID and legal name to "identify
the same row," which leaves the common held-out case (ID matches nothing, name matches exactly one)
undefined and blocking. Replace with an explicit four-case ladder:

| ID match | Legal-name match | Outcome |
|---|---|---|
| row A | row A | Resolve |
| none | exactly one row | **Resolve** + `DATA_QUALITY` naming the stale ID |
| row A | row B | Block — `DECISION_REQUIRED`, never guess |
| any | none, or ambiguous | Block — `DECISION_REQUIRED` |

Note the asymmetry and keep it: a name-only match resolves because legal names are semantically
meaningful, while an ID-only match (the ID hits a row whose name is a different company) must block,
because a surrogate key is exactly the thing that can be silently reassigned to another supplier.

**Fix, part two — make the block non-catastrophic when it does happen.** Degrade by the design's own
`severity` field:

- `severity: hard` + unresolvable entity → `DECISION_REQUIRED` for the scope (current behavior; correct).
- `severity: shaping` + unresolvable entity → the directive is **inapplicable**. Drop objective stage 5
  and the solve-1 frontier condition, plan under base policy, and emit a `POLICY_CONFLICT` alert naming
  the unresolved reference. A directive naming a company that does not exist in this database cannot
  shape an allocation among companies that do — and the same memo's rolling-cap and ≥20% clauses key
  off the *component* concept, so they remain fully evaluable and must keep binding.

Named-primary is declared `severity: shaping` in §5.1, so this is internally consistent — it just
needs saying.

### F5 — [High] "Safety-critical" under both-ways evaluation makes copper wire and magnets unorderable

§7 makes ISO-9001 a hard gate for *"electronics/PCB/safety-critical"*. §5.3 says unresolved membership
in a **restrictive** concept is handled by both-ways evaluation: *"execute only if it is safe under
both."* Requiring a certification is restrictive, and no component in the data is marked
safety-critical, so membership is unresolved for every component. Follow it through:

- **CMP-001 Copper Wire** — SUP-111 holds **no certifications** and is the sole eligible supplier
  (§2.4, because domestic is cheaper so §3's gate never opens). Under the "is safety-critical" reading
  SUP-111 fails, so no plan is safe under both readings → copper wire is unorderable in every scenario.
- **CMP-003 Neodymium Magnets** — SUP-107 holds **no certifications**. Same argument kills it, and with
  only SUP-108 left the ≥20% secondary rule becomes unsatisfiable → all magnet demand blocked.

This contradicts §2.4's own table, which lists SUP-111 as CMP-001's sole *eligible* supplier. The
document is internally inconsistent, and the inconsistency resolves the wrong way on unseen data.

**Fix — revised after review.** An earlier draft proposed making enumerated concepts closed-world, so
that a component not matching §6's list is simply non-critical. **That is unsafe**, and it is worth
being precise about why, because the direction is counterintuitive: non-critical is the *looser*
classification in every place it is used.

| Consequence of "critical" | Critical | Non-critical | Which is looser |
|---|---|---|---|
| Domestic price-premium threshold (§3) | 50% | 35% | non-critical — international unlocks at a *lower* premium |
| Concentration cap (§4) | 70% | 85% | non-critical |
| Dual-source requirement (§4) | required | none | non-critical |

So a lexical miss on a held-out `MCU (RISC-V, 800MHz)` would silently relax all three. The correct
split is between two *different* problems that the earlier draft ran together:

- **§6's category list is closed; membership in those categories is not.** No new critical category
  can be invented, but whether a given component is a "sensor IC" or a "PCB blank" stays a semantic
  question — so **both-ways evaluation continues to apply to §6 membership**, exactly as it does for
  the `CMP-014` case. Unresolved membership therefore yields the conservative intersection: the 50%
  premium threshold *and* the 70% cap *and* the dual-source diagnostic. That is safe in all three
  places at once, which a closed-world default is not.
- **"Safety-critical" is unenumerated and has no data signal at all.** It requires *positive*
  evidence — structured (`components.requires_certification`, `components.category`) or an explicit
  configured mapping. Absent that it is **not established**, under a named benchmark assumption with
  a standing alert; under the production contract the evidence contract decides whether it blocks.
  This is what keeps CMP-001 and CMP-003 orderable without inventing a designation the schema does
  not carry.

Add both to §17 as open questions. `components.category` ("Electronic Component", "Chemical",
"Raw Material") is a clean structured signal for §2.1's "electronic components" clause and should
be tier 1 of the resolver.

### F6 — [Medium] Nothing detects a run that silently did nothing

F1, F2, F4, F5 and C3 can all produce the same surface shape: an alert-only run. Some are defects and
some, such as an unapproved high-value order, are correct safety outcomes. A grader diffing only
`purchase_orders` cannot distinguish them from a broken agent.

**Fix:** add a mandatory **run accounting alert**, emitted every run:

> Planned 19 components: 12 ordered ($8,430 across 14 POs), 4 deferred for decision, 2 have no
> eligible supplier, 1 fully covered by existing stock. Evidence contract: benchmark. Active
> directives: MEMO-2025-041, MEMO-2025-085. Inactive: MEMO-2025-072 (expired 2025-09-30).

Plus two hard self-checks, scoped to scenarios that passed structural loading. Malformed structural
input exits before planning and may not have an alerts table safe to mutate.

1. Every component with a positive **initial** eventual gap receives a `DecisionRecord`. If it has no
   executable PO, it must have a terminal, component-specific alert.
2. Every positive **post-plan residual** gap has a terminal, component-specific alert even when a
   partial PO was executed. A partial PO must never mask an unexplained remainder.

Terminal categories: `NO_ELIGIBLE_SUPPLIER`, `DECISION_REQUIRED`, `APPROVAL_REQUIRED`,
`SOLVER_UNPROVEN`, `POLICY_CONFLICT`, and `DATA_QUALITY` where non-structural evidence prevented
planning. Anything else is an internal error and fails the validator. These checks close the case
where a component or residual falls through silently — the worst outcome and the hardest to notice,
since an empty table looks identical to a correct one.

### F7 — [Medium] Completion risk outweighs modelling risk

The design specifies a three-solve lexicographic MILP with ten objective stages, an independently
implemented validator that re-derives the solve-0 baseline, a policy compiler with span coverage
verification, two evidence contracts, and twelve work packages. That is a large amount of machinery
for an interim prototype, and every one of the failures above is worth less than the risk that the
end-to-end path is not finished and nothing runs at all.

**Fix — build a walking skeleton first, under two constraints.**

1. Snapshot → ledgers → candidate routes with full eligibility gates → **greedy comparator-ordered
   allocation** → rationale → atomic commit.
2. Swap the allocator for the exact MILP behind `Solver`. The comparator chain and objective stages
   are already specified as the same ordering (§8.2), so this is a drop-in.
3. Add solve 0 / solve 2, the counterfactual frontier, and the independent validator.

**Constraint one: the greedy stage is a milestone, not a shippable planner.** §8.1 is right that "a
heuristic that fails to find a plan is indistinguishable from a plan that does not exist." So if the
exact solver is not ready, the greedy path must be *structurally incapable* of the two claims that
require exactness — it may report "no compliant plan found → `UNRESOLVED`", and it may never assert
`INFEASIBLE`, claim no alternative exists, or take a below-B / sole-source exception. With those
outputs disabled it is a safe interim planner; with them enabled it silently converts search failure
into policy relaxation.

**Constraint two: fault isolation must be scoped by blast radius, not applied globally.** An earlier
draft of this finding proposed isolating any per-component exception. That is unsafe for BOM and
schedule data, because those feed *shared* demand — skipping one malformed `bom` row understates
demand for a component used by other products, and the run then commits POs that are quietly too
small. Scope it:

| Failure | Behavior |
|---|---|
| One unparseable catalog row, one supplier attribute, one solver instance | Skip that route/component, alert, continue |
| Malformed `bom`, `production_schedule`, `inventory`, or `scenario_config` | Global failure, no writes, exit 3 — anything that can distort demand is structural |

This is the same severity matrix §6 and T01's success criteria currently disagree about (see F17).

### F8 — [Medium] Attribute normalization is named but not specified

§5.3 says supplier attributes are "derived, not read" but does not say how. Each of these is a
one-line held-out change:

| Field | Held-out variant | Required behavior |
|---|---|---|
| `country` | "United States", "U.S.A.", "Mexico" | Alias table in `concepts.yaml` resolves known names against Policy §3's US+Canada definition. An **unrecognized** country is `UNKNOWN` + `DATA_QUALITY` + both-ways evaluation — *not* a fallback to `is_domestic`, which the Canada/SUP-110 case already proves unreliable. The flag is supporting evidence to disclose, never the authority |
| `certifications` | `"ISO 9001; UL Listed"`, `"iso-9001"` | Split on `[,;|/]`, strip, uppercase, remove non-alphanumerics before comparison |
| `sustainability_rating` | NULL, `"AA"`, `"Gold"` | Parse to ordinal; unparseable → `UNKNOWN` and evaluate both "below B" and "not below B". If the interpretations change executability, keep the route recommendation/decision-only; if the same plan survives both, execute only under the active contract with the assumption disclosed. Never silently pass the below-B review gate |
| `relationship_tier` | `"strategic"`, `"KEY"` | Case-insensitive; unknown tier → evaluate both Strategic and non-Strategic for the shaping preference, and disclose whenever the selected plan or objective vector depends on the interpretation |
| `on_approved_list` | NULL | NULL ≠ approved. Fail closed + alert |

Put every alias list in `concepts.yaml`, not code — same discipline as the policy constants.

### F9 — [Medium] An unsatisfiable per-order allocation rule has no stated handling

If a held-out database leaves exactly one *eligible* magnet supplier, §8.2's secondary-allocation
constraint (`Σx[s] ≤ 0.8·T` for all s) is infeasible for any positive order, and every magnet
requirement falls over with no stated handling.

It is tempting to reach for §8.2's treatment of §4's dual-sourcing requirement — a diagnostic rather
than a constraint, *"because it cannot be satisfied by allocating differently, only by qualifying
another supplier."* **That analogy does not transfer**, and an earlier draft of this finding wrongly
relied on it. §4's rule is a property of the *supply base*; MEMO-2025-041's ≥20% is a property of *an
order's allocation*, and a 100% single-supplier order violates it on its face.

Dropping the constraint and ordering anyway would write a proven violation — the one thing the design
forbids. So the rule stays hard, and the handling splits on *why* the second supplier is missing:

- **Structurally unsatisfiable** — no second supplier can be reached without waiving a rule the agent
  has no authority over: only one supplier is in the catalog, *or* every alternative fails a
  non-relaxable gate (off the ASL, missing a required certification). Write no PO. Emit
  `DECISION_REQUIRED` plus `SOLE_SOURCE`/`POLICY_CONFLICT`, carrying the fully-costed one-supplier
  plan explicitly labelled non-executable. The remediation must name the authority actually available:
  qualify or add another supplier, or formally change the secondary-allocation policy. An off-ASL
  route is never described as ordinarily approvable — Policy §2 says it may not be used under any
  circumstances. The agent is reporting the cost of the current infeasibility, not asking to be
  waived through.
- **Relaxably unsatisfiable** — a second supplier exists but is blocked by a relaxable gate (below-B
  review, an approval-dependent route). That is an ordinary solve-2 counterfactual: *"relaxing X makes
  a compliant split available at cost Y."*

Apply the same split to any `minimum_secondary_fraction` rule generically, not just the magnet one.

### F10 — [Low] The `CMP-014` "load-bearing" claim is stale

§2.2 states *"The `CMP-014` mapping is load-bearing: it sets the domestic premium threshold to 50%
rather than 35%, which is what makes `SUP-112` sole-eligible in scenario 06."* **[C]** The premium is
45.5%, so the critical reading closes the gate. But after correction #18 resolved comparator tier 2 to
*unconditional* domestic preference, an open gate no longer changes the selection — SUP-112 still
wins, with SUP-103 admitted as a lower-ranked candidate. The mapping then affects only alert content
(`COST_OPPORTUNITY`) and infeasibility reasoning, not which supplier is chosen.

**This finding is contingent on F15.** If §3's domestic preference is resolved the other way — price
decides once the premium exception opens — then `CMP-014` at 45.5% clears a 35% bar but not a 50% one,
the mapping flips the chosen supplier, and §2.2's claim is correct exactly as written. Resolve F15
first; F10 only applies if the plan's current reading survives.

### F11 — [Low] PO number generation needs a collision check

An earlier draft overstated this as a cross-namespace collision. It is not: `production_schedule.
order_id` and `purchase_orders.po_number` are different tables, so `PO-5001` appearing in both
violates no constraint. The real risk is narrower — a held-out database that uses `PO-####` for
*actual* purchase orders, where a naive sequential scheme collides on the primary key. Generate
`APX-<8-hex-of-action-key>` (also clearer in the rationale about which rows the agent authored) and
check against existing `po_number` values inside the write transaction.

### F12 — [Low] No staleness signal on directives with a stated duration

MEMO-2025-085 says the PCB freeze lasts *"estimated 60-90 days."* With `effective_through: null` it
binds forever. On a held-out scenario dated 2026, the agent would suppress two PCB suppliers on the
strength of a directive that expected to lapse 10 months earlier.

**Fix:** add an optional `estimated_duration_days` to the pack schema with its own covering span.
Keep the rule active — no memo has rescinded it — but emit a `POLICY_CONFLICT` alert: *"MEMO-2025-085
has been in force 312 days against a stated 60–90 day estimate; confirm it remains current."* Cheap,
and exactly the judgement a Head of Operations would want surfaced.

### F13 — [Low] Ownership marker cosmetics

§10.2 makes the ownership marker mandatory in `description` because the table has no other column.
That is correct reasoning. Keep it short and terminal (` [apex-agent]`) so alerts still read like the
brief's sample — a grader, or an LLM judge, reading *"Cannot meet May 20 start date…"* should not trip
over a prefix first. Note it in the README.

### F14 — [Medium] The inbound-PO exclusion rule is worded backwards

§6 says *"A PO dated before the scenario date is **excluded** from new availability pending
reconciliation."* Read literally against `order_date`, that discards **all four** of scenario 02's
existing POs, whose order dates are 2025-08-01 through 2025-08-20.

**[C] The plan's intent is provably the opposite**, because its own fixture table only reproduces if
those POs are counted:

```
s02, counting existing POs as inbound   -> 11 components short, CMP-003 gap = 58
s02, excluding them (literal reading)   -> 13 components short, CMP-003 gap = 208
```

§2.3 claims exactly 11 and 58. So this is a **wording bug, not a design error** — but a dangerous one,
because T04's implementing agent could take the sentence literally and nothing downstream would catch
it. Every arithmetic check in the plan would still pass; only the fixture counts would move.

**Fix:** state the rule against the delivery date, with explicit boundaries.

- Count a pre-existing PO as committed inbound when `expected_delivery_date >= current_date`, netted
  at its **stored** delivery date.
- When `expected_delivery_date < current_date`, receipt status is unknown — exclude it and alert
  (the existing §6 reasoning about over-ordering vs. starving the line is correct and unchanged).
- Delivery exactly on `current_date` counts as available; arrival exactly on `materials_needed_by`
  counts as on time (§7 already uses `<=`; add the boundary test and keep Q8 open).
- A NULL `expected_delivery_date` is not inbound: exclude and raise `DATA_QUALITY`.

### F15 — [Critical] Unconditional domestic preference makes Policy §3(b) inert

§7 resolves comparator tier 2 as *"the gate controls eligibility, the preference is unconditional."*
Follow that through every path:

| §3 condition opens the gate | Who wins | Why |
|---|---|---|
| shut | domestic | international is ineligible |
| (a) domestic misses the deadline | international | comparator 1 (on-time feasibility) already decided |
| (b) **price premium exceeds 35% / 50%** | **domestic** | comparator 2 outranks cost |
| (c) no domestic source | international | the domestic set is empty |

**Condition (b) can never change a selection.** A policy clause that, correctly implemented, is
provably never decisive is strong evidence the interpretation is wrong — and it is the clause the
policy took the trouble to give two different thresholds.

The commercial reading is the natural one: a price-premium threshold *is* the preference, quantified.
"We prefer domestic, and international may be used when the premium exceeds 35%" means *we will pay up
to 35% extra to buy domestic, and past that we won't.* Reading the preference as also persisting
unconditionally on top of the threshold double-counts it.

**[C] Measured impact** on the provided catalog (ASL suppliers, policy's US+Canada definition):

| Component | Best domestic | Best international | Premium | Threshold | Gate | Flips? |
|---|---|---|---:|---:|---|---|
| CMP-003 Magnets | SUP-108 $5.80 | SUP-107 $3.25 | 78.5% | 50% | open | **yes** |
| CMP-005 PCB | SUP-101 $12.00 | SUP-103 $7.50 | 60.0% | 50% | open | moot — PCB freeze |
| CMP-013 Temp Sensor | SUP-112 $7.25 | SUP-103 $4.50 | 61.1% | 50% | open | **yes** |
| CMP-015 Humidity | SUP-112 $5.50 | SUP-103 $3.25 | 69.2% | 50% | open | **yes** |
| CMP-007 MOSFET | SUP-101 $2.25 | SUP-103 $1.50 | 50.0% | 50% | shut | no (strict >) |
| CMP-014 Transducer | SUP-112 $32.00 | SUP-103 $22.00 | 45.5% | 50% | shut | only if not critical |

Three real supplier flips on any held-out scenario whose deadlines both routes can meet — and the
magnet split moves too. This is Q22 in §17, so the plan knows the question is open; it picked the
branch that makes a policy clause do no work.

**Fix:** make comparator 2 conditional on *which* §3 condition opened the gate. Under (a) and (c) the
preference is moot — domestic cannot do the job. Under (b), the premium test has already adjudicated
price, so the remaining comparators (strategic retention, sustainability, cost, lead time) decide.
Where the two readings produce materially different plans, the losing plan is a `COST_OPPORTUNITY`
alert with its delta, so the customer sees the money either way.

### F16 — [High] Sourcing exceptions are not scoped to the demand that justifies them

§7 evaluates both the air-freight predicate and the §3(a) timing exception against
`bucket.due_date` — correctly, per bucket. But routes and optimizer variables are built **per
component**, and no constraint ties exception-derived quantity to the bucket that opened the
exception. So an early deadline that domestic or ocean supply cannot meet silently authorizes
international or air-freight allocation for *later* demand that ordinary supply covers fine.

**Correction to an earlier draft of this finding.** It claimed the air route would win solve 1 on
lateness (stage 2 outranks stage 4) and strand the whole component behind an approval gate. That is
wrong: §8.2 constrains solve 1 directly — *"executable variables cannot use routes with unresolved
approval gates"* — so an unapproved air route never enters the executable solve at all. C5 in the
catalog above had this right and the finding contradicted it. The ocean route executes late and air
surfaces as a recommendation, as designed.

The leak is real but splits into two smaller, differently-shaped problems:

- **Executable path — §3(a) international over-allocation.** A timing exception carries *no* approval
  gate; it is pure eligibility. So an early deadline domestic supply cannot meet makes an
  international route eligible for the component, and nothing caps its quantity at that bucket. Later
  demand that domestic supply covers comfortably can end up sourced internationally, inside solve 1,
  against §3's stated preference. **This one writes wrong POs.**
- **Recommendation path — air-freight over-allocation.** The over-allocation lands in solve 2 and
  corrupts the counterfactual: *"approving air freight buys you N late-days"* overstates N by
  air-freighting quantity that never needed it. A planner approving on that basis authorizes more air
  spend than required — against a $25,000 period cap the agent cannot track (§18). **This one gives
  wrong advice.**

**Fix:** carry the justifying bucket on the route and constrain it against the shortage that actually
opened the exception, not gross demand.

- Preferred: introduce route-to-bucket allocation variables `z[r,t]`, link
  `x[r] = Σ_t z[r,t]`, and permit `z[r,t] > 0` for an exception-bearing route only when that route's
  predicate is true for bucket `t`.
- Additionally cap the **aggregate across all routes using the same exception** at the incremental
  on-time gap remaining for the qualifying buckets after on-hand and committed inbound are allocated.
  A per-route cap would let several exception routes each consume the full allowance.

Add a metamorphic test: a component with an early unmeetable deadline and a later comfortably-meetable
one must allocate exception-route quantity no greater than the net unresolved early shortage, and
adding on-hand inventory to that early bucket must reduce the exception allowance by the same amount.

### F17 — [Medium] Specification cleanups that change behavior

Grouped because each is small, but none is cosmetic:

- **Degenerate-input severity is self-contradictory.** §6 says broken references *"produce an alert,
  never a crash"*; T01's success criteria say dangling BOM/schedule references must *"fail with
  deterministic messages."* Resolve with the blast-radius matrix in F7 and state it once.
- **No unit-of-measure contract.** §6 says discrete units round up and continuous units round to a
  configured precision, but never says which UoM is which. The data has `each`, `kg`, `meter`, `tube`,
  `can`; a held-out `box`, `roll`, or `liter` has no defined behavior. Declare the classification in
  `concepts.yaml`, define unknown UoM as discrete with an `ASSUMPTION` alert, and fix the order of
  operations: aggregate per component/deadline → round → apply MOQ.
- **Strategic and sustainability stages are underspecified at the MILP level.** §7's comparator prose
  *does* carry the policy windows — Strategic retention "unless the alternative saves >15%",
  sustainability preference "when price is within 10% and delivery within 5 business days". §8.2's
  stage table restates them as bare objective terms ("volume shifted away from Strategic suppliers",
  "sustainability-band penalty") with no window. Since §8.2 asserts the two representations must be
  the same ordering, the quantity-level form needs the conditions written in explicitly, or an
  implementer will encode a preference the policy does not grant outside its window.
- **"Exact optimization" overclaims what HiGHS provides.** It is a floating-point solver; "proven
  optimal" means optimal to tolerance. Integer-scale quantities and costs, re-verify the selected plan
  in `Decimal`, bound coefficients, and soften §8's language to "optimal under the integer-scaled
  model." This also makes the stdlib fallback in F3 genuinely exact rather than merely equivalent.
- **"Pareto frontier" is a misnomer.** §8.3 says the system computes a Pareto frontier and splits it;
  §8.2 actually produces one executable optimum plus single-rule counterfactuals. Either enumerate a
  real nondominated set or rename it "selected plan and counterfactual alternatives." An interviewer
  who knows optimization will ask to see the frontier.
- **Concept matching needs token boundaries and negative tests.** A substring match for `magnet`
  against `neodymium_magnet` false-positives on a held-out "magnetic reed switch." Match on token
  boundaries and ship negative fixtures for each concept.
- **No policy is effective before 2025-01-15.** The base policy carries an effective date. A held-out
  scenario dated 2024 has no applicable policy at all. ASL membership and catalog existence are data
  facts and still gate; everything policy-derived must alert loudly rather than procure silently.
- **The policy pack must be located relative to `agent.py`, not the working directory.** Graders will
  run from elsewhere. Add a test that runs from `/`.
- **`data/scenario_01_baseline.sqlite` is a zero-byte file.** **[C]** Confirmed empty; the valid
  fixture is `data/scenarios/scenario_01_baseline.sqlite`. Do not ship it or reference it in the
  README — and add it as an adversarial fixture, since a zero-byte database is exactly the malformed
  input O7 should handle.

---

## 4. What holds up well

Worth stating plainly, because most of the design survives this exercise:

- **Effective-date gating on `scenario_config."current_date"`.** The single highest-probability
  held-out variation, and the design gates every rule on it. Scenario 05 already exercises an expired
  memo, and A16/A17 are handled by construction.
- **Absent history ≠ zero history (§3).** §2.4's demonstration that the known-zero reading makes six of
  nineteen components unorderable in every scenario is the sharpest analysis in the document, and it
  is correct. Any held-out scenario short on `CMP-001`/`005`/`009`/`014`/`017`/`018` — which is most of
  them — separates a design that got this right from one that did not.
- **Nothing keys off identifiers.** Directly answers the `RM-3003`/`RM-3005`/`MFG-5030` namespace
  evidence, which is the clearest signal in the provided data that the graders test ID robustness.
- **Coverage ranked above lateness (stage 1 ≻ stage 2).** Means C1/C2 order compliant-but-late material
  rather than freezing. This is the behavior most likely to be probed and the design gets it right.
- **`expected_delivery_date` vs. `material_available` kept separate (§7).** Prevents falsifying a field
  the supplier is measured against while still planning against a realistic dock date.
- **Alert reconciliation rather than delete-and-reinsert (§10.4).** Correct reasoning about
  `AUTOINCREMENT`, and rerunning the agent is a cheap thing for a grader to try.
- **Row-level determinism rather than byte-level (§12).** Right assertion for SQLite.
- **Orthogonal fulfillment / resolution / disposition (§9).** Partial coverage with an infeasible
  residual is common in held-out data and most designs cannot express it.
- **`--llm=off` by default.** Immunity to injection through `notes`/`description` is free, and the
  required invocation makes no network call.

---

## 5. Recommended change list

Ordered by effect on held-out outcomes, not by effort.

| | Change | Finding | Where | Effort |
|---|---|---|---|---|
| 1 | Ratio-gate discretionary expedite surplus only; forced MOQ/allocation surplus is advisory-alerted as `FORCED_SURPLUS`, never gated; add the §4.1 sub-MOQ counterfactual | F1 | §8.2, §8.3, §12 | M |
| 2 | Make comparator 2 conditional on which §3 condition opened the gate; allocate exception-bearing routes only to qualifying buckets and cap their aggregate at the net justified shortage | F15, F16 | §7, §8.2 | M |
| 3 | Four-case ID/name resolver ladder **plus** severity-aware degradation for shaping directives | F4 | §5.1, §12 | S |
| 4 | Keep both-ways evaluation for §6 membership (conservative intersection); require positive evidence for "safety-critical" | F5 | §5.3, §7, §17 | S |
| 5 | Lazy-load/reduce runtime dependencies; JSON pack; staged clean-environment CI; pack located relative to `agent.py`. Bounded stdlib solver **last**, and only if it does not destabilise the `scipy` path | F3, F17 | §8.2, §15 | M |
| 6 | Run accounting alert + DecisionRecord coverage for every initial gap + terminal alert for every positive post-plan residual | F6 | §10.2, §11 | S |
| 7 | Reword the inbound rule against `expected_delivery_date` with explicit boundary semantics | F14 | §6 | S |
| 8 | Specify normalization and robust UNKNOWN handling (country / certifications / rating / tier / ASL) plus a unit-of-measure contract, all in `concepts.yaml` | F8, F17 | §5.3, §6 | S |
| 9 | Walking-skeleton build order with the greedy stage barred from infeasibility/exception claims; blast-radius fault isolation | F7 | §16 | M |
| 10 | Keep approvals withheld; put the complete proposed order in the alert; **no CLI flag** — a proposal-queue reading becomes a declared contract or nothing | F2 | §9, §15 | S |
| 11 | Keep ≥20% secondary hard; split structurally vs. relaxably unsatisfiable (structural includes all-alternatives-fail-non-relaxable-gates) | F9 | §8.2 | S |
| 12 | Write the policy windows into the strategic and sustainability objective stages explicitly | F17 | §8.2 | S |
| 13 | Integer-scale the model, soften "exact" to "optimal under the integer-scaled model", rename the "Pareto frontier" | F17 | §8, §8.3 | S |
| 14 | `APX-<hash>` PO numbers with in-transaction collision check | F11 | §10.1, §10.3 | S |
| 15 | `estimated_duration_days` + staleness alert; token-boundary concept matching; pre-2025-01-15 fallback; drop the zero-byte fixture | F12, F17 | §5.1, §5.3 | S |
| 16 | Resolve the `CMP-014` load-bearing claim — **after** item 2, which determines whether it is true | F10 | §2.2 | S |

Items 1–6 change what the agent writes on held-out data. Items 7–16 are correctness, honesty, and
operational robustness.

**Findings revised during review, recorded rather than silently changed.** Each earlier position and
why it failed:

| Finding | Earlier position | Why it was wrong |
|---|---|---|
| F2 | `write_and_flag` default, later softened to a CLI flag | Traded a stated safety commitment for a grading hedge; the flag itself was a config waiver, which §8.3 forbids |
| F9 | Drop ≥20% when a second supplier is unavailable | Borrowed §4's supply-base reasoning for an order-allocation rule; the analogy does not transfer |
| F5 / B1 | Enumerated concepts closed-world; a recall miss is "the safe direction" | Backwards — non-critical is the *looser* classification in all three places it is used |
| F16 | An air route wins solve 1 and strands the component | §8.2 already bars unapproved routes from executable variables; the leak is narrower and splits in two |
| F1 | `$2,500` a bound that is then exceeded without effect | Not a bound. It is a review threshold |
| F7 | Isolate any per-component fault | Unsafe for BOM/schedule data, which feeds shared demand |
| F8 | Unknown country falls back to `is_domestic` | That column is already proven wrong for Canada |
| F3 | Enumeration is a cheap universal fallback | Tractable at this catalog's scale only |
| B9 / F11 | Renumbering resolves by name; PO numbers collide across tables | Both were descriptions of what *should* happen, not of what the plan or the schema actually does |

The pattern worth carrying into implementation: every one of these traded a stated guarantee for a
convenience, and each was caught by checking the claim against the plan's own text or the data rather
than against intuition.

---

## 6. New open questions for §17

- **Q25** — Is an MOQ-driven overbuy a procurement decision at Apex, or is it simply the cost of doing
  business? We currently treat it as a decision, which suppresses orders on small production runs.
- **Q26** — Does a `purchase_orders` row over $50,000 represent a commitment we must not create without
  approval, or a proposal the Procurement Manager approves in your system of record? (Sharpens Q1 with
  the specific consequence.)
- **Q27** — Is any component designated "safety-critical" under Policy §2.1, and where is that recorded?
  No field in the schema carries it.
- **Q28** — MEMO-2025-085 states an estimated 60–90 day duration with no end date. Should directives
  carrying an estimated duration expire, or remain in force until explicitly rescinded?
- **Q29** — Sharpens Q22 with its consequence: once §3's price-premium exception opens, does domestic
  preference still apply, or has the threshold already settled the price question? Under the current
  reading, condition (b) can never change a supplier selection — which suggests we have read it wrong.
  Three components flip on the provided catalog.
- **Q30** — When an early deadline opens a timing or air-freight exception, may the resulting
  international or expedited allocation also serve later demand that ordinary supply can meet, or is
  the permission confined to the demand that justified it? We assume confined.
- **Q31** — Which units of measure are discrete and which are continuous, and what pack increments
  apply? The schema supplies `each`, `kg`, `meter`, `tube`, and `can` with no classification.
