# Apex Autonomous Procurement Agent — Decision Log

**Document:** `docs/DECISION_LOG.md`
**Companion design:** [MERGED_PLAN.md](./MERGED_PLAN.md)

This document records contested decisions, their tradeoffs and reversal conditions, followed by the
corrections found during adversarial review. The implementation specification remains in the companion
technical design. Section references such as §8.2 point to that design; D-number and correction-number
references point within this log.

---

## Decision log

Each entry records the question, the decision, why, **what it costs us**, and what would reverse it.

### D1 — Model as compiler, not executor

**Decision.** The LLM compiles policy documents into a reviewable rule pack offline; a deterministic
engine plans. Optional bounded runtime roles for entity-resolution recall and narration polish.

**Why.** Procurement planning is exact constraint satisfaction with a legal-style text on top. Models
are strong on the text and weak on the arithmetic. This split gives reproducibility, per-rule unit
testing, sub-second runtime, no per-run token cost scaling with catalog size, and invariant guarantees
that survive held-out data.

**Tradeoff.** Policy changes need a compile-and-review cycle rather than taking effect instantly. A
genuinely novel rule kind the IR cannot express requires a code change, not just a document. The
demo shows less visible "AI" than an agent loop would.

**Reverses if.** Apex wants same-day policy changes with no review gate, or the rule taxonomy proves
too rigid across more of their corpus.

### D2 — Declared evidence contracts

**Decision.** Two named contracts (§3). Benchmark is the shipped default; production is available by
flag. Every run emits the active contract and every assumption it licensed.

**Why.** This dissolves the central disagreement between the source documents. One defaulted to
blocking on missing evidence (rolling history, PCB receipts). Traced through the actual data that is
not "conservative" — it is total: §4 puts a rolling-window cap on every component, critical or not, and
no history exists, so **nothing executes in any scenario** (§3). The other document proceeded on
assumptions without naming them. Making the evidence
policy an explicit, declared, customer-owned choice is better than either default, and it is the kind
of artifact an FDE should put in front of a customer.

**Tradeoff.** A reviewer who ignores the contract alert may mistake benchmark-mode assumptions for
proven compliance. Two contracts also means two behaviours to test and explain.

**Reverses if.** Apex supplies receipt and history tables, at which point the production contract
becomes the only one and the benchmark contract is deleted rather than defaulted.

### D3 — A proven violation is never written to `purchase_orders`

**Decision.** Non-compliant options go to alerts as decision support, never to the orders table.

**Why.** One source document proposed breaching MEMO-2025-041's 50% cap and flagging for VP approval.
Textual check: Policy §4's only exception is genuine global sole-source, §9's VP approval covers
strategic-volume shifts, and the magnet memo contains no waiver clause. That design invented an
approval pathway the documents do not grant. Writing the compliant plan and *recommending* the
alternative preserves the original goal — surface the tradeoff, don't paralyse — without putting a
violating row in the table.

**Tradeoff.** In scenario 05 the executed plan still leaves a large block of units eight days late
(§8.3). That breaks a delivery obligation Policy §10 also directs us to protect, so "compliant" is
doing less work than it sounds:
this is a conflict between two policy obligations. Where a cap is provable it wins, because it is
explicit and numeric while §10 is a "should" — a reason, not a claim that one option is simply legal.

**Scope note after D21.** This entry established that a *proven* violation never reaches the orders
table. It does not license the converse: an *unprovable* constraint is not a violation, and treating
it as one caused the paralysis bug corrected in D21. Under the benchmark contract the rolling cap is
not evaluated at all, so scenario 05's outcome is decided by the ≥20% secondary rule and the autonomy
bound (§8.3), not by the 50% figure.

**Reverses if.** Apex provides an explicit waiver rule with named authority, which the policy pack can
then express as a rule rather than a configuration knob.

### D4 — Exactness is a precondition for any exception

**Decision.** No executable supplier selection, relaxation, exception, or infeasibility claim may rest
on a merely feasible incumbent or any other result lacking proven optimality for the relevant stages.

**Why.** This was the most serious flaw found in review. A greedy-plus-repair allocator that falls back
to a relaxation ladder cannot distinguish "no compliant plan exists" from "my search missed it," so a
solver limitation silently becomes a policy violation. Scenario 02 is a live instance — see D5.

**Tradeoff.** More machinery up front; a solver dependency; timeout paths to handle.

**Reverses if.** Never. This one is structural.

### D5 — MILP via HiGHS, with enumeration as test oracle

**Decision.** Per-component MILP through `scipy.optimize.milp` behind a `Solver` protocol. Exhaustive
enumeration is the differential oracle.

**Why.** Semi-continuous MOQ quantities, concentration ratios, existing history, and multiple deadlines
form a natural MILP that generalises past the supplied cardinalities. Support enumeration plus an LP is
equally exact for small instances, but it needs an LP solver anyway — and `scipy` already bundles
HiGHS, so it is strictly more code for identical results. The earlier argument that MILP was "too heavy"
conflated tooling weight with exactness; only exactness is non-negotiable.

**Tradeoff.** A `scipy` dependency. Lexicographic staging requires pinning each stage's optimum with a
fixed tolerance, or determinism drifts. A timeout path may retain an incumbent for diagnosis but must
not write it: feasibility does not prove that the policy comparator chain selected it.

**Reverses if.** Instances stay provably tiny forever and dependency minimalism matters more than
generality — then support enumeration plus a hand-rolled LP is defensible.

### D6 — Separate requirement state from plan disposition

**Decision.** Candidate plans use four dispositions: `EXECUTE`, `EXECUTE_WITH_ASSUMPTION`,
`RECOMMEND_APPROVAL`, and `DECISION_REQUIRED`. Requirements independently record fulfillment
(`FULFILLED` / `PARTIALLY_FULFILLED` / `UNFULFILLED`) and resolution
(`RESOLVED` / `INFEASIBLE` / `UNRESOLVED`) (§9).

**Why.** A binary write/don't-write forces every hard case into a bad bucket, while using `INFEASIBLE`
as a plan disposition makes a useful partial PO simultaneously executable and non-executable.
`RECOMMEND_APPROVAL` is required because MEMO-2025-072 says the Procurement Manager "must approve each air freight request
individually," so an unapproved air-freight PO is textually unauthorized. `DECISION_REQUIRED` is
required because compliant-but-absurd is a real category — see D7. `EXECUTE_WITH_ASSUMPTION` makes
benchmark-contract licence visible in the data rather than buried in prose. The separate requirement
fields record physical coverage and proof state without changing whether a candidate may be written.

**Tradeoff.** Two orthogonal fields to explain to a customer, and a risk of drift toward recommending everything.
Mitigation: `RECOMMEND_APPROVAL` is scoped strictly to *documented* approval requirements, never to
general uncertainty. Widened, it reproduces the paralysis of D2's rejected default.

**Reverses if.** Apex supplies approval state, collapsing `RECOMMEND_APPROVAL` into a pre-check.

### D7 — `DECISION_REQUIRED` and the economic autonomy bound

**Decision.** An option enters the executable frontier only if its surplus and excess cost are within
customer-set review thresholds. Failing that, execute nothing on the line and emit the full frontier.

**Why.** Scenario 02: residual need is 58 units, and **every** reading of MEMO-2025-041 forces material
overbuy, and the driver depends on which clauses bind (§2.3). Under the shipped allocation-group
reading only the ≥20% secondary rule applies, and the 150-unit floor comes purely from the two MOQs
(SUP-107 at 100 plus SUP-108 at 50) — **not** from any cap. Add a per-order 50% cap (Q18) and
SUP-107's MOQ of 100 forces a ≥200-unit total; evaluate that cap over visible history as well (Q21)
and it reaches 250. The minimum compliant plan therefore overbuys 92, 142, or 192 units at $615, $905,
or $1,195, against $336.40 for the 58-unit order that satisfies coverage but no allocation rule. Both source documents got this wrong in opposite directions — one
declared an infeasibility that does not exist, the other would auto-execute the overbuy because it is
the only compliant coverage plan. A human planner would do neither.

The disposition is if anything *more* clearly correct after the correction in D21: the agent cannot
even determine which compliance target applies without a customer answer, so presenting the frontier
is the only honest action.

**Tradeoff.** Demand goes uncovered while a human decides, which in a tight schedule costs time. The
threshold is a judgement the data cannot supply, so a prototype default is unavoidable — it is
documented and printed in the decision alert rather than hidden.

**Reverses if.** Apex sets a genuine overbuy tolerance in policy, which then simply parameterises the
bound.

### D8 — Threshold lives in policy, and config cannot move options between frontiers

**Decision.** The autonomy bound is a policy-pack parameter, not a config knob. Customer configuration
orders options *within* a frontier and never across.

**Why.** "Configurable preference order over the Pareto set" was the wrong framing: if config can rank
customer timing above the concentration cap, config becomes a waiver mechanism — exactly the authority
D3 established the documents do not grant. Framing the bound as an *admissibility filter* keeps hard
policy out of engineering configuration entirely.

**Tradeoff.** Less flexibility for operators who want to tune behaviour quickly; changes route through
policy review.

### D9 — Four-quantity ledger, not two

**Decision.** Track eventual coverage, on-time coverage, recoverable lateness, and intentional surplus
(§6).

**Why.** A simpler two-ledger model — purchasing ignores arrival dates, dates drive alerts only — was
proposed to make idempotency automatic. It is wrong: if a committed PO covers demand but lands after
the deadline while a faster supplier can still make it, `eventual_gap = 0` and the agent orders nothing.
That is exactly what expediting is, and it is routine practice. The design traded a correctness
property for a bookkeeping property.

**Tradeoff.** Expediting deliberately creates surplus — the late PO becomes dead stock. Deciding whether
that is worth it needs the cost of a day of delay, which no table contains (Q15). Until Apex answers,
recovery surplus is bounded by D7's autonomy threshold and always disclosed.

### D10 — Strict-improvement recovery, with action identity as safety net

**Decision.** Recovery fires only when a new route arrives **strictly earlier** than the best
already-committed arrival for that demand. Idempotency is additionally protected by a stable action key
whose fingerprint **excludes the agent's own output rows**.

**Why.** The strict-improvement rule makes recovery self-terminating: expedite orders arrive on time by
construction and close their own gap, and a late order that was already the best available produces no
improvement on rerun, so nothing is re-ordered. Scenario 01 verifies this — the rerun's best magnet
route is still SUP-108 at 09-15, identical to what is committed. That makes the action key a safety net
for ties, retries, and the commit path rather than the primary mechanism, which is where you want it: a
key that silently stops matching produces duplicate purchase orders with no signal.

The fingerprint exclusion is a general rule, not a one-off fix. Including the input digest, a run
timestamp, or any sequence derived from existing rows breaks stability, because all of those change
after the first write.

**Tradeoff.** "Strictly earlier" needs a defined granularity (whole days here) and could suppress a
marginal same-day improvement that a human would take.

### D11 — Robust both-ways evaluation for unresolved membership

**Decision.** For a restrictive concept, test the action under both membership readings and execute only
if safe under both. For a permissive concept, deny. If nothing survives, `DECISION_REQUIRED`.

**Why.** The alternative — default an unresolved component into every restrictive concept — turns
uncertainty into asserted truth and requires proving, per concept, that *every* rule referencing it
points the same direction. That happens to hold for "critical" in this policy (it tightens both the
concentration cap and the international-sourcing gate), but it is luck, not a property to rely on as
Apex amends the corpus. It would also let one unmatched component become simultaneously critical,
hazardous, power-supply, and memo-scoped.

**Tradeoff.** More conservative: an action safe under only one reading is rejected, so a mis-resolved
component can stall procurement rather than being handled slightly wrongly. The evidence contract and
`DECISION_REQUIRED` keep that visible instead of silent.

### D12 — Runtime entity resolution is optional, not load-bearing

**Decision.** Structured attributes and deterministic lexical resolution run dynamically over unseen
rows. Model classification handles residuals when a server is reachable.

**Why.** The claim that a live model is *necessary* for held-out generalization was overstated:
deterministic semantic predicates already run dynamically, so the model adds **recall** (catching
`NdFeB Magnet, grade N52, axial`), not the dynamic capability itself. And correctness cannot depend on
connectivity — the evaluator may provide no model server. Equally, schema validation proves the model
returned the right shape, not that its classification is true, and caching makes a mistake reproducible
rather than correct.

**Tradeoff.** In offline mode, an unusually named component may resolve to no concept and fall to D11's
both-ways evaluation, which is safe but can stall a line. Maintaining `concepts.yaml` is ongoing work.

### D13 — PCB incumbency as a declared inference

**Decision.** Prior PO evidence, or a relationship demonstrably predating the memo plus a catalog
listing and valid ASL/certification status. Shipped under the benchmark contract with a standing alert;
replaced by receipt records under production.

**Why.** The memo's own gloss is the operative test — "If a PCB supplier is new to us, we cannot order
PCBs from them at this time" — and SUP-101 ("Primary electronics supplier since 2018", Strategic, lists
`CMP-005`, ISO-9001, on the ASL) passes that relationship test comfortably. It does **not** follow
that Apex previously received and accepted PCB shipments from SUP-101 — being a long-standing
electronics supplier is not evidence about PCB lots, and no schema field carries that fact. A strictly
literal
accepted-shipment test makes PCBs unorderable in five of six scenarios for a component in all four
products' BOMs. An earlier version of this rule keyed on relationship *tier*, which was the wrong
proxy; tracking the memo's "new to us" language is closer to the text.

**Tradeoff.** It is an inference, not evidence. If Apex has in fact bought PCBs from SUP-103 before,
the plan is wrong in a way only Apex can detect — which is why it alerts on every run.

### D14 — Policy definitions beat convenience columns

**Decision.** Derive domesticity from `country` per Policy §3 (US and Canada); the stored `is_domestic`
flag is a hint, and disagreement raises `DATA_QUALITY`. Certification requirements are the union of
`components.requires_certification` and policy-derived rules.

**Why.** `SUP-110` is Canadian with `is_domestic=0`, a direct contradiction. Only `CMP-005` carries a
certification value while Policy §2.1 imposes requirements on entire categories.

**Tradeoff.** If the flag encodes something the policy text does not — a customs or entity
consideration — we would be overriding real information. Hence the alert rather than silent override.

### D15 — Air freight is conditional and never auto-executed

**Decision.** Air applies only when the memo is active, the supplier is international, demand is
confirmed production, **and** the standard lead time would miss the date. It always yields
`RECOMMEND_APPROVAL`.

**Why.** The memo authorises air "where ocean freight lead times would cause production delays" and
requires individual Procurement Manager approval. In scenario 01 the magnet demand due 10-10 is met by
SUP-107's standard 35-day ocean lead (arrives 10-06), so air is unnecessary — using it would burn the
memo's $25,000 budget and an approval for nothing.

**Tradeoff.** Air-freight-dependent orders never auto-execute, so a genuinely urgent case waits for a
human. Given air only matters when ocean misses the date, this stays rare.

### D16 — Stored inbound dates are authoritative; overdue inbound is excluded

**Decision.** Net inbound at the stored `expected_delivery_date`. Exclude any PO dated before the
scenario date from new availability, with a reconciliation alert.

**Why.** The four existing POs arrive 3–4 days later than `order_date + catalog lead` (§2.2), so stored
dates carry information the catalog does not. For overdue rows the schema gives no status: counting
them risks double-counting against inventory, which under-orders and stops production; excluding them
over-orders, which only costs money. Production safety wins.

**Tradeoff.** In a real backlog of overdue-but-in-transit orders this systematically over-buys. No
overdue rows exist in the provided data, so this is a held-out concern.

### D17 — Never modify or cancel pre-existing purchase orders

**Decision.** Alert and recommend; never touch rows the agent did not write. `--rerun=replace` is not
offered.

**Why.** Cancelling a supplier commitment is a commercial act with contractual consequences, outside an
autonomous planner's remit. Scenario 02's existing POs put SUP-107 at 66.7% of *visible* magnet volume
against the memo's 50% figure — a reported ratio, not a proven breach, since rolling compliance is
`UNKNOWN` (D21). The memo directs that open orders be updated, so the agent reports the ratio and
recommends re-balancing without asserting a violation it cannot establish.

**Tradeoff.** The agent cannot remediate a pre-existing allocation it believes is wrong — and note it
cannot even *establish* that it is wrong, since rolling compliance is `UNKNOWN` (D21); it reports a
visible ratio and recommends. A genuinely stale PO also distorts netting until a human clears it.

### D18 — Comparator chain over weighted scoring

**Decision.** A lexicographic chain of comparators, each mapped to one policy sentence.

**Why.** A weighted objective needs weights no data can fit, produces decisions nobody can explain, and
makes policy changes untestable. Policy §8 and §9 are literally written as thresholded tie-breakers —
"within 10%", "within 5 business days", "up to 15%". One sentence, one comparator, one unit test, and a
rationale that can name the comparator that decided.

**Tradeoff.** Lexicographic ordering ignores small trades across tiers: a marginally worse arrival that
saves a great deal cannot win unless a policy sentence says so. Consistent with the policy text, but it
is a real loss of nuance.

### D19 — Templates by default for prose

**Decision.** Deterministic rationale and alert templates; model polish opt-in, guarded by a
fact-consistency check with template fallback.

**Why.** The numeric guard prevents fabricated quantities from reaching an order, but it cannot catch
semantic inversion or a dropped caveat. Slightly stiffer prose is the right price.

**Tradeoff.** Less fluent output than a model-written narrative.

### D20 — Alert prose with ownership tagging

**Decision.** Plain prose matching the brief's sample; an ownership marker identifies agent-authored
rows so reruns replace only those; visible category prefixes are configurable and off by default.

**Why.** Ownership is needed for idempotent alert reconciliation without destroying externally written
rows. The brief's sample alert is bare prose, so that is the default rendering.

**Tradeoff.** Downstream tooling must opt into prefixes to parse categories reliably.

### D21 — Absent history is `UNKNOWN`, and rules carry an evidence basis

**Decision.** Rolling-window constraints are emitted only when the active contract can supply real
history; otherwise they are held `UNKNOWN` and the order executes as `EXECUTE_WITH_ASSUMPTION`.
Prospective per-order rules are enforced unconditionally. Every rule declares an `evidence_basis`, and
each **contract** maps unsatisfied bases to dispositions (§5.1) — the disposition belongs to the
contract, not to the rule, since the same rule must execute-with-assumption under one and stop under
the other.

**Why.** The prior draft read "visible POs plus this run are the complete available history," which is
`H = 0` in every scenario but 02. For a component with one eligible supplier the constraint then
collapses to `x ≤ cap · x`, forcing `x = 0`. Six of nineteen components are sole-eligible (§2.4), so
this silently blocked `CMP-001`, `CMP-005`, `CMP-009`, `CMP-014`, `CMP-017` and `CMP-018` in every
scenario — reproducing exactly the paralysis D2 claims to solve, in the document that claims to solve
it. Scenario 06's only critical shortage is one of the six.

The root error was a category confusion: §4 limits a supplier's share of a **rolling 12-month volume**,
and a single planning run is not that object. MEMO-2025-041 had to be split accordingly — its 50% cap
inherits the rolling framing and is unprovable, while its ≥20% secondary-allocation clause is
explicitly per-order and is provable from the order under construction.

**Tradeoff.** Under the benchmark contract the agent can place an order that a full 12-month history
would show to be non-compliant. That is disclosed on every affected line rather than hidden, and it is
the honest position: the alternative is asserting a fact we do not have. It also means scenario 02's
"compliant" plans are compliant *under a stated reading*, not globally proven.

**Reverses if.** Apex supplies order history, at which point the constraint simply becomes provable
and the assumption disappears.

### D22 — Below-B sustainability is a gate, dischargeable by memo

**Decision.** Below-B suppliers require a proven no-alternative counterfactual plus represented
review; a memo explicitly directing that supplier's use for that component discharges the review.

**Why.** §8 grants conditional permission ("should only be used when no alternatives are available")
and imposes a review requirement. The prior draft made it comparator tier 4, where it could be
outvoted by feasibility and no review was recorded anywhere — so scenario 05's magnet split would have
executed 220 units from SUP-107 (rated C) with the §8 review simply absent.

The discharge route matters as much as the gate. MEMO-2025-041 is signed by the VP of Operations and
names SUP-107 as "our primary volume supplier"; that *is* the additional review, performed at a level
above the one §8 contemplates. Without this route the gate would stall every magnet plan in every
scenario, which would be the below-B version of the D21 bug.

**Tradeoff.** The discharge depends on reading a memo as constituting review. If Apex means something
procedurally distinct by "additional review", we have substituted our judgement — so the discharge is
marked **[I]** and orders relying on it carry `EXECUTE_WITH_ASSUMPTION` rather than plain `EXECUTE`
(Q19). Note the alternative is not neutral: without the discharge, SUP-107 is unusable, and since it
is one of only two magnet suppliers, the ≥20% secondary rule becomes unsatisfiable and every magnet
requirement in every scenario falls to `DECISION_REQUIRED`.

### D23 — Emergency procurement is declared unsupported

**Decision.** §7.1's $75,000 threshold bypass is not implemented. Qualifying candidates yield
`RECOMMEND_APPROVAL` with the lateness evidence available.

**Why.** The bypass is conditioned on an order being "required to prevent production stoppage."
Establishing that needs production lead times, WIP state, and line schedules; the schema has none. We
can observe that material arrives after `materials_needed_by`; we cannot observe that production stops.
Implementing a guessed stoppage heuristic would let the agent self-authorise around an approval gate
on evidence it does not have — the most dangerous class of error available to it.

**Tradeoff.** Genuinely urgent orders wait for a human even where the bypass would have applied. Given
that no line in the provided data approaches even the $50k base threshold, the practical cost here is
zero and the cost is entirely in held-out scenarios.

**Reverses if.** Apex supplies production schedules or an explicit stoppage flag on schedule rows.

### D24 — Dual-sourcing diagnoses, `U` is derived, recommendations are counterfactual

**Decision.** Three optimizer-integrity rules: §4's two-qualified-suppliers requirement emits
`SOLE_SOURCE` and never constrains allocation; `U` is derived from demand, authorized surplus and MOQ,
and the validator independently recomputes and checks that **derivation** — an optimum equal to `U` is
legal, since when demand is 100 and `U` is 100 the answer genuinely is 100; recommendation options come
from separate one-rule-relaxation solves with clean variable sets.

**Why.** Each protects D4's exactness guarantee from a different direction. Dual-sourcing as a
constraint would make `CMP-005` unorderable for a reason no purchase order can fix — it is a property
of the qualified supply base, remediable only by qualifying a supplier. A hand-picked `U` that is too
small manufactures false infeasibility with no signal, which then feeds the relaxation machinery.
Sharing variables between executable and counterfactual solves lets a relaxation contaminate the
executable answer.

**Tradeoff.** More solves per component, and the derived `U` can be large enough to slow a
pathological instance. Both are cheap at realistic cardinalities.

---

## Corrections made during review

Recorded because a review that produced corrections should show them, and because two of these were
load-bearing for design decisions.

| # | Source | Error | Correction | Verified by |
|---|---|---|---|---|
| 1 | CLAUDE_PLAN §8.4 | "No compliant plan exists" for scenario 02 magnets | A compliant plan exists: new SUP-107=100, SUP-108=150 → 200/200 of 400, overbuying 192 units at $1,195 vs $336.40. It is also the *minimum*-overbuy compliant plan | Brute-force search over the full space; caps force `b = a+50`, SUP-107 MOQ forces `a ≥ 100` |
| 2 | CLAUDE_PLAN §8.2 | Concentration written `Σx[s] ≤ cap·(existing + Σx)` — omits `H_s` on the left | `H_s + x_s ≤ cap·(H_total + Σx)`; otherwise a supplier already at the cap can take a full new allocation | Inspection; CODEX_PLAN §7.5 had it right |
| 3 | CLAUDE_PLAN §2.2 | "Memos reference … `MFG-5030`" | `MFG-5030` appears only in the assignment brief's sample alert. Memos contain `RM-3003` and `RM-3005` | Text extraction across all memo, policy, and assignment PDFs |
| 4 | CLAUDE review | UL listing contributes to making SUP-101 sole-eligible for PCBs | PCBs require ISO-9001 only; UL is additional for power-supply components. PCB suppliers are eliminated by ASL removal (SUP-113) and the memo freeze (SUP-103, SUP-110) | Policy §2.1 text; supplier certification rows |
| 5 | CLAUDE review | Sole-sourcing `CMP-017`/`CMP-018` breaches §4's dual-source rule | Those are power-supply components, not in §6's critical list, so the dual-source rule does not apply. The real issue is the **85% non-critical concentration cap**, which 100% breaches — and §4's sole-source exception does not fit, because alternatives exist but fail Apex's own certification rule. Whether it breaches the *rolling* cap is history-dependent and `UNKNOWN` outside the benchmark contract | Policy §4 and §6 text; catalog and certification rows |
| 6 | CLAUDE_PLAN §11.3 | Idempotency "falls out of netting" | False for late coverage, and the fix must not be date-blind purchasing — see D9/D10 | Scenario 01 magnets: rerun deficit at 09-12 is unchanged at 80 |
| 7 | CODEX_PLAN §3.2 | Competing-demand shortage count of 18 | 17. `CMP-006` (80 demand vs 80 on hand) and `CMP-019` are covered at every deadline | Time-phased recomputation across all six scenarios |
| 8 | CODEX_PLAN §6.4 | Air-freight conditions omit "where ocean freight would cause delays" | Added as a required condition — see D15 | Memo text |
| 9 | CODEX_PLAN §7.9 | Action hash includes the input digest | The digest changes after the first write, breaking key stability — see D10 | Inspection |
| 10 | Both | Approval thresholds treated as exercised behaviour | No line in the provided data exceeds ~$6,050 and no run exceeds ~$22,300; §7's $50k/$150k gates are covered by synthetic tests only | Spend computation across all six scenarios |

### Second review pass — corrections to the merged design

Found after the merged design was drafted. All six are mine; four were blocking.

| # | Section | Error | Correction | Verified by |
|---|---|---|---|---|
| 11 | §3 benchmark contract | Treated visible POs as the complete rolling history, i.e. `H = 0` | For a sole-eligible component this collapses to `x ≤ cap·x` → `x = 0`. **Six of nineteen components** are sole-eligible, so this blocked them in every scenario — reproducing the paralysis the contract claims to solve. Absent history is now `UNKNOWN` and non-blocking; per-order clauses enforce separately (D21) | Eligibility enumeration across the full catalog after ASL, certification, PCB freeze, and the domestic gate |
| 12 | §7 comparator chain | §8's below-B rule implemented as comparator tier 4 | It is a gate plus a review requirement, not a preference; scenario 05 would have used SUP-107 (rated C) with no review represented. Now gated, with memo discharge (D22) | Policy §8 text; SUP-107 rating |
| 13 | §7 route calculation | `hazmat_receiving_buffer_days` folded into `effective_lead`, and therefore into `expected_delivery_date` | Policy §10 requires that field to reflect the supplier's quoted lead time. Receiving buffers now live in a separate `material_available` used only for feasibility | Policy §10 text |
| 14 | §10.4 commit protocol | "Replace owned alerts" | `alert_id` is `AUTOINCREMENT`, so delete-and-reinsert changes IDs and `sqlite_sequence` even for identical text, breaking the zero-change rerun guarantee. Alerts are now reconciled (preserve / insert / delete-obsolete) | Scenario schema |
| 15 | §2.2, §5.3 | "PCB Assembly = PCB blanks", "Pressure Transducer = sensor IC", "a prior PO proves an accepted shipment", and "SUP-101 is incumbent under any reading" presented as verified facts | All are interpretations. Relabelled **[I]**, routed through both-ways evaluation, and disclosed. The SUP-101 claim is sound under the memo's "new to us" gloss and unsound under its literal accepted-shipment sentence — both readings are now stated | Policy §6 and August memo text |
| 16 | §5.1 | The example `source_quote` contained an ellipsis | It could not have passed the literal-substring verification the same section mandates. Replaced with multiple exact contiguous `source_spans`, each checked against the memo text | Self-inconsistency; spans re-verified against the memo |

Two smaller self-inconsistencies were also fixed: entity resolution defaulted to "on when a server is
reachable" while §14 promised no default network calls (`--llm=off` is now the default), and §12
asserted "byte-identical output" for SQLite, which page layout and `sqlite_sequence` can violate without
any business change (now asserted at row level with zero writes on rerun).

### Third review pass — corrections to the corrections

The D21 fix was made in §3 and §5.1 but had **not propagated** to the worked scenarios or the decision
log, so the document simultaneously said the cap was unenforced and showed outputs that only an
enforced cap produces. That is the characteristic failure of a deep correction, and it is worth
recording as such.

| # | Section | Error | Correction | Verified by |
|---|---|---|---|---|
| 17 | §2.3, §8.3, D3 | Magnet fixtures still assumed an enforced 50% cap: scenario 01 at 104/104, scenario 05 at 220/220 "compliant", D3 saying the cap "wins" | Recomputed under the benchmark contract. **S01 → SUP-108=108, SUP-107=100** ($951.40; SUP-107 at its MOQ floor, secondary 48.1%). **S05 → a two-point frontier**: zero-late (440/110, 110 surplus, $2,909.50) versus zero-surplus (340/100, 800 unit-late-days, $2,297.00) → `DECISION_REQUIRED`. D3 rescoped: unprovable ≠ violated | Lexicographic enumeration over both suppliers under the corrected rule set |
| 18 | §7 comparator tier 2 | "Domestic unless the §3 gate is open" contradicted the same section's "domestic remains the default", and silently changed every magnet allocation | The gate controls **eligibility**; the tier-2 preference is **unconditional**. §3 is titled "Domestic Sourcing Preference" and its lettered conditions are permissions to deviate, not instructions to | Policy §3 text; the two readings produce different S01 plans (108/100 vs 80/128) |
| 19 | §5.1 | `unprovable_disposition` fixed on the rule | The same rule must execute-with-assumption under one contract and block under another. Rules now declare only `evidence_basis`; **contracts** map unsatisfied bases to dispositions | Internal contradiction with §3's production column |
| 20 | §8.2 | Validator rejected any optimum resting on `U` | Equality with a valid bound is legal — when demand is 100 and `U`=100, or when MOQ exceeds demand, `x = U` is the answer. Now: derive a valid Big-M, allow equality, re-solve once with `U` doubled and flag only if the objective improves; a binding **autonomy** cap is `DECISION_REQUIRED`, not an error | Counterexample inspection |
| 21 | §5.1 | Literal-substring checking treated as sufficient | A span containing no number validates against a hallucinated threshold. Now every load-bearing typed value needs a covering span — selector, threshold with unit and window, dates, supersession target, approval authority | A span quoting "is reduced" would accept `0.40` |
| 22 | §5.1 | `scope: per_purchase_order` is unrepresentable — a PO row holds one supplier | Defined an **allocation group** (all new POs for one component in one run), with primary = largest share and secondary allocation = total non-primary share ≥ 20%. Three other groupings are defensible and change the answer → Q21 | Scenario schema |
| 23 | §2.2, §2.4, D13 | `CMP-015` counted as a direct match; §2.4 marked **[V]**; D13 still said "under any reading" | `CMP-015 "Humidity Sensor"` is not labelled an IC — only four of seven mappings are direct. §2.4 is now "mechanically verified under stated assumptions", since it depends on the inferred PCB and critical mappings. D13 now separates the relationship test SUP-101 passes from the shipment-acceptance claim it does not support | Component names; dependency inspection |
| 24 | §7 below-B discharge | Memo-as-review treated as established | Marked **[I]**; orders relying on it are `EXECUTE_WITH_ASSUMPTION`, not plain `EXECUTE` | §8 text does not define "additional review" |

### Fourth review pass — a missed rule, and a lesson about method

| # | Section | Error | Correction | Verified by |
|---|---|---|---|---|
| 25 | §5.1 | **MEMO-2025-041 contains a third rule that four review rounds missed**: "Until MagnetPro's capacity is confirmed sufficient, Nanjing Rare Earth Co. (SUP-107) remains our primary volume supplier" | Compiled as `magnet_named_primary`, with a `supplier_capacity_confirmed` guard defaulting to *not confirmed* (both `suppliers.notes` and the catalog row for SUP-108 read "limited N52 capacity", and no capacity figure exists anywhere). It supersedes §3's domestic preference for magnets only. The design had been using this very sentence to discharge SUP-107's below-B review while ignoring its selection meaning | Memo text; SUP-107/SUP-108 notes |
| 26 | §2.3, §8.3 | Fixtures recomputed without the named-primary rule | **S01 → 104/104 at $941.20** — back to the first draft's number, reached by a completely different route (the named-primary tie, not a 50% cap). **S05 → a three-point frontier**: zero-late 440/110 (110 surplus), zero-surplus 340/100 (800 late-days), memo-faithful 220/220 (1,760 late-days, $1,991) → `DECISION_REQUIRED` — **itself superseded by #34**, which showed this frontier was an incomplete hand-picked subset | Bounded enumeration under the corrected stage ordering |
| 27 | §8.2 | MILP stages ranked cost above preferences while the comparator chain ranked domestic above cost — the two selected different plans (80/128 vs 104/104) | Stages rewritten as a ten-row table mirroring the comparator chain exactly, with a test asserting the two orderings agree | Cost comparison: $880.00 vs $941.20 on identical coverage |
| 28 | §8.3 | The autonomy bound was called "documented" but never given a value or boundary semantics | Explicit default (`max_surplus_fraction: 0.10`, `max_excess_cost_usd: 2500`, `boundary: inclusive`, `provisional: true`), printed in every decision alert. S05's 25% surplus is now determinately above it | Reproducibility check on S05 |
| 29 | §11 validator | Below-B required a no-alternative proof **or** a memo discharge | Both are required. `or` would admit a below-B supplier under a memo discharge even where a B-or-better compliant plan exists | Logic inspection |
| 30 | §8.2, §11 | `U` equality still rejected in the validator and D24 after the main text allowed it | The validator now recomputes and checks the **derivation**; an optimum equal to `U` is correct by construction. Doubling-`U` is retained only as a CI smoke test, and is explicitly not the guarantee | Counterexample: demand 100, `U` = 100 |
| 31 | §5.1 | `window_months: 12` had no covering span; the secondary rule had no linear form; the allocation group had no persisted identity | Added a `derived_from` pointer to §4 for the window; the linear form is `x[s] ≤ 0.80·T_group ∀s`; each grouped line carries an `allocation_group_id` in the rationale and decision record | Memo states no window; construction check |
| 32 | §5.1 contract map | Mixed outcomes, strategies, and evidence properties in one map, and used `BLOCK`, which is not one of the five dispositions | Split into `evidence_bases` (resolution strategy) and `contracts` (disposition, always one of the five). Production maps `rolling_window → DECISION_REQUIRED` and applies wherever such a rule is in scope, including §4's 85% cap | §9 taxonomy |

### Fifth review pass — one rule, four enforcement models

| # | Section | Error | Correction | Verified by |
|---|---|---|---|---|
| 33 | §5.1, §8.2, §8.3 | Named-primary modelled **four incompatible ways at once**: `severity: shaping`, a MILP feasibility constraint, objective stage 5, and something frontier options could violate freely | One model: **shaping, objective stage only, never a constraint** — with any deviation routed to `DECISION_REQUIRED`, because the directive proxies for MagnetPro's unverifiable capacity (Q12) and deviating assumes capacity the source data calls "limited" | The four sites contradicted each other; neither the executable frontier nor S05's disposition followed from the spec |
| 34 | §8.3 | The "three-point S05 frontier" was a hand-picked subset that **omitted strictly better admissible plans** — and its own option A (440/110, 25% surplus) was **inadmissible under the 10% autonomy default declared two sections earlier** | Table replaced by the mechanism (surplus buys lateness back continuously, which is *why* the bound must sit inside the solve) plus two enumerated optima: 242/242 honouring the directive, 384/100 deviating | Enumeration with the bound inside the solve: **12,159** admissible allocations exist |
| 35 | §8.2 | Autonomy bound applied as a post-filter over a candidate list | Moved **into** the solve as a constraint. Post-filtering a hand-built option set is what produced correction #34 | Same enumeration |
| 36 | §5.1 | `evidence_basis: external_system` declared, then overridden by a hardcoded `unresolved_default: not_confirmed` — the contract map was never consulted | Modelled as an **affirmative `release_condition`** in state `not_established`: the directive holds until a confirmation record exists. Absence is not a factual claim about capacity | Contract map in §5.1 |
| 37 | §5.1 | Spans failed the coverage rule the same section mandates: no span tied "MagnetPro" to SUP-108, none carried component scope, and `supersedes: §3` was asserted though the memo never says it | Added `subject` and `scope` spans from the memo's capacity sentence; `supersedes` demoted to `precedence.basis: inferred` with its reasoning recorded | Memo text; all spans re-verified literal |
| 38 | §2.3 | Scenario 02 still attributed all three readings to a ≤50% cap, and had not been checked against the named-primary directive | Each row now names its actual driver — the shipped 150-unit floor is **MOQ 100 + MOQ 50**, no cap involved — and the coverage-only baseline is noted as disfavoured by the directive too | D7 had been corrected; §2.3 had not |
| 39 | §12 | "Nothing keys off identifiers" contradicted a compiled rule containing SUP-107/SUP-108, which CI would reject | Carve-out for **source-named entities**: stored as `{source_id, legal_name}`, resolved at runtime, with `DECISION_REQUIRED` on ambiguous or failed resolution. The ban is on code keying off IDs, not on a rule quoting a document | CI rule vs pack contents |
| 40 | §2.3, §6, D3, D24 | Stale text: S01 shown as "108/104" against a 104/104 cost; the ledger example still said 108; D3 described an executed 220-unit-late plan; D24 still rejected `U` equality; §2.3 called the examples "golden expectations" while §8.3 called them illustrative | All corrected; §2.3 now separates **structural facts** (the 240 unit-late-day floor) from **generated allocations** | Cross-reference sweep |

### Sixth review pass — the narrative did not follow from the objective

| # | Section | Error | Correction | Verified by |
|---|---|---|---|---|
| 41 | §8.2, §8.3, §9 | **The stated objective does not execute 242/242.** Lateness (stage 2) outranks named-primary deviation (stage 5), so a single solve selects the *deviating* 384/100. The executed plan existed only in prose | **Three solves**: solve 0 baseline (yields `cheapest_covering_cost`), solve 1 executable with `named_primary_deviation = 0` pinned as a **frontier-membership condition**, solve 2 counterfactual with it relaxed. 242/242 now follows from the design | 448 < 1,584 unit-late-days at stage 2 |
| 42 | §9 | "Every **requirement** resolves to exactly one disposition" made the intended behaviour unstateable — one requirement cannot both execute a plan and carry a differently-disposed alternative | Dispositions attach to **candidate plans**. A requirement executes at most one plan and may surface any number of `RECOMMEND_APPROVAL`/`DECISION_REQUIRED` alternatives | Direct contradiction with §8.3 |
| 43 | §5.1 | Deviation was justified by claiming 384 units on SUP-108 assumes capacity the data doubts, implying 242 is capacity-safe | False: the directive bounds SUP-107's *relative share*, not SUP-108's *absolute volume*. 242 units assumes no less unverified capacity than 384. Split into a share directive (gates executability) and a `CAPACITY_UNKNOWN` disclosure carried by **every** plan touching SUP-108 | Rule semantics vs the claim made for it |
| 44 | §8.2 | Only the surplus half of the autonomy bound was in the MILP; `max_excess_cost_usd` was configured but never constrained | Added `known_cost − cheapest_covering_cost ≤ max_excess_cost_usd`, with the reference computed by solve 0 under identical hard rules and contract | §8.3 config vs §8.2 formulation |
| 45 | §2.3 | Scenario 02's third row (100/150) deviates from named-primary and so is not an executable "minimum compliant order" | Marked as a solve-2 counterfactual; executable only if Q21 also moves the named-primary grouping to include existing orders. Q23 therefore compounds with Q18 and Q21 on that row | 100 < 150 within the new-order group |
| 46 | §12 | Two monotonicity properties are false under MOQ plus a **relative** autonomy bound: adding inventory can shrink the need until a fixed MOQ split exceeds the 10% allowance (flipping execution to a decision), and adding demand can pull it back inside | Monotonicity asserted only over **physical feasibility**; autonomous **executability** is explicitly non-monotone because it is gated by a ratio | Worked both directions against the shipped 10% default |
| 47 | §12 | Entity resolution said "exact ID match wins" and also that a contradicting name yields `DECISION_REQUIRED` | Resolution requires **agreement**: ID and normalised legal name must identify the same row. A silent ID win in a renumbered database would attach a VP directive to the wrong company | Internal contradiction |
| 48 | §2.3, Q2, Q24 | Stale "three-point frontier" and "two non-dominated points" language, and Q2/Q24 describing superseded outcomes | Rewritten around the curve-plus-bound mechanism and the three-solve structure | Cross-reference sweep |

### Seventh review pass — the reference figure had no objective behind it

| # | Section | Error | Correction | Verified by |
|---|---|---|---|---|
| 49 | §8.2 | **Solve 0 had no objective.** Described only as "no autonomy bounds, no relaxations". Run under the stage table it minimises lateness and returns 440/110 at $2,909.50 (or 440/440 at $3,982.00 with the primary condition) — neither is a *cheapest* covering reference. The quoted $1,654.40 followed only from an unstated cost-minimising objective | Solve 0 given its own explicit lexicographic objective: **max physical service, then minimum known landed cost**, under the same hard rules and contract. Stage 1 keeps the reference **defined when coverage is impossible** — otherwise `cheapest_covering_cost` is undefined exactly on held-out cases with incomplete sourcing | Three objectives evaluated: $2,909.50 / $3,982.00 / $1,654.40 (88/352) |
| 50 | §3, §5.1 | Capacity was called an evidence-contract question, but no `supplier_capacity` basis existed. §8.3's executed plan was `EXECUTE_WITH_ASSUMPTION` by appeal to a contract rule that did not exist | Added `supplier_capacity` as a first-class basis: `EXECUTE_WITH_ASSUMPTION` under benchmark, `DECISION_REQUIRED` under production. Every positive allocation to a supplier without capacity evidence now carries the mapped disposition and `CAPACITY_UNKNOWN` | Contract map had no such key |
| 51 | §11 | The validator had not absorbed the three-solve mechanisms — the narrative again promised guarantees nothing enforced | Six checks added: solve-0 reference semantics; excess-cost bound on executed plans; `named_primary_deviation = 0` on every executed plan; `DECISION_REQUIRED` on every deviating alternative; `CAPACITY_UNKNOWN` on capacity-unevidenced allocations; disposition/frontier-membership agreement and the one-executed-plan rule | Checklist vs §8.2 |
| 52 | §8.2 | "Touching the autonomy cap emits `DECISION_REQUIRED`" contradicted the inclusive boundary under which 242/242 executes at exactly 10% | Corrected: resting *at* a cap is executable; `DECISION_REQUIRED` arises when solve 1 is **infeasible** under the caps | §8.3 boundary semantics |
| 53 | §8.2 | The one-rule-relaxation guarantee could not produce scenario 02's coverage-only plan, which breaches two rules | Added a bounded, explicitly enumerated **compliance-cost diagnostic** category — multi-rule, never executable, labelled non-executable at generation — kept separate from single-rule counterfactual recommendations | 58-from-SUP-108 breaches secondary allocation *and* named-primary |
| 54 | §12 | The ID-renaming property contradicted the source-reference resolver: renaming the database alone is *supposed* to yield `DECISION_REQUIRED` | Split into two tests — rename database and pack together (plan unchanged), rename database alone (assert the decision request). Same trap as the monotonicity properties | §12 carve-out vs test list |
| 55 | §8.2, Q23, Q24 | Stale: "two non-dominated points" before describing a curve; named-primary called absent from a block it now appears in; Q23 still justifying deviation by capacity; Q24 saying the bounds "reduce 12,159 allocations" when 12,159 is the **post**-bound count | All corrected. The bound actually cuts **523,303 → 12,159** | Unbounded enumeration |

### Eighth review pass — an inert constraint and a contract that stops the system

| # | Section | Error | Correction | Verified by |
|---|---|---|---|---|
| 56 | §8.2 | **Solve 0's stage 1 was per-deadline shortfall**, which is lateness-sensitive and contradicted the next sentence claiming solve 0 ignores lateness. It selects 440/110 at $2,909.50, not the quoted 88/352 at $1,654.40 | Stage 1 is **eventual** uncovered quantity over the horizon — the `eventual_gap` the §6 ledger already defines. The tell that the old form was broken: excess cost for the executed plan came out **negative** (−$719.40), i.e. the "cheapest covering" reference cost *more* than the plan it bounded, so `max_excess_cost_usd` could never bind. The configured constraint was inert | Both objectives enumerated |
| 57 | §3, §5.1, §11 | `supplier_capacity` was unscoped — "no supplier has capacity evidence, so every allocation assumes capacity" — and the validator applied it to every positive allocation. Under production that writes **no purchase orders anywhere** | Scoped to suppliers named in a **capacity-dependent rule**; in this corpus exactly SUP-108, via MEMO-2025-041's release condition. Elsewhere capacity is commercial risk, not a policy predicate | Contract map traced through the validator check |
| 58 | §3, D2 | Both still claimed production mode "would place only the sensor-housing order" | Understated. §4 puts a rolling-window cap on **every** component (70% critical / 85% non-critical) and no history exists, so **production is advisory-only: nothing executes in any scenario.** Stated plainly — it is the real argument for the benchmark default | 19 of 19 components carry a §4 cap |
| 59 | §5.1 | Policy pack said "objective stage 5 ONLY — never a constraint" while solve 1 adds `named_primary_deviation = 0`; and the YAML comment still justified deviation by capacity | Wording corrected to "not a base feasibility rule; enforced by objective stage 5 + a solve-1 executability condition", and the capacity justification removed from the comment to match the corrected rationale (#43) | Self-contradiction |
| 60 | §8.3 | Claimed the autonomy bound "makes the admissible set finite" | Integer quantities and the derived `U` already do that — **523,303** allocations pre-bound. The bound makes the *economically executable* region decidable: 523,303 → 12,159 | Unbounded enumeration |
| 61 | §11 | Solve 0's baseline was only checked as "computed under the declared semantics" | Feasibility and arithmetic cannot detect an **inflated** baseline, which silently widens the excess-cost gate. The validator now independently re-solves or enumerates solve 0 and asserts its own optimum equals the planner's | Attack analysis on the gate |
| 62 | §3, §9 | `Block` survived outside the five-disposition taxonomy; the outcome table header was duplicated | Both fixed | Sweep |

### Ninth review pass — feasibility, disposition and identity were still conflated

| # | Section | Error | Correction | Verified by |
|---|---|---|---|---|
| 63 | §0, §8.2, §11, D4–D5 | A timed-out solve could write any feasible incumbent after validation, even though feasibility cannot prove the lexicographic policy objective selected it | Writes now require proven optimality for solve 0 and every solve-1 stage. Timeout incumbents are diagnostic-only; timed-out solve 2 cannot support completeness, infeasibility, no-alternative, or necessary-relaxation claims | Forced-timeout integration tests at every stage |
| 64 | §3, §5.1, §11 | Scoping `supplier_capacity` to "suppliers named in a capacity-dependent rule" still matched both SUP-107 and SUP-108, the validator remained universal, and the memo's release condition was being turned into an unstated allocation gate | Removed capacity from contract-mapped plan dispositions. Absence of confirmation keeps the named-primary directive active; only `release_condition.subject` receives the non-dispositive `CAPACITY_UNKNOWN` alert. A future numeric capacity rule must declare its own evidence basis | Memo grammar: capacity confirmation releases the directive; it does not authorize each order |
| 65 | §8.2, §9, D6 | `INFEASIBLE` was called a candidate disposition that writes a partial PO while the same section said non-executable candidates never write; a one-field replacement still overlapped physical partial coverage with proof state | Candidate dispositions are separate from two requirement fields: fulfillment (`FULFILLED` / `PARTIALLY_FULFILLED` / `UNFULFILLED`) and resolution (`RESOLVED` / `INFEASIBLE` / `UNRESOLVED`). Partial coverage can coexist with a proven infeasible residual; timeout, unlicensed evidence gaps and approval/decision gates are unresolved, never falsely infeasible | State-machine exhaustiveness check |
| 66 | §7, §8.2, §12 | The ID-renaming property said a consistent rename preserves the plan, but the last objective tie-breaker was ascending `supplier_id` | Final tie-break uses an ID-free supplier fingerprint; duplicate fingerprints stop autonomous selection with `DATA_QUALITY`. The metamorphic test includes exact commercial ties | Permuting tied supplier IDs cannot change the semantic objective vector |
| 67 | §8.2, §11 | Solve 0 named `eventual_gap` but its pseudocode subtracted only new procurement, omitting on-hand and committed inbound | Added the complete non-negative `g[c]` equation and made the validator independently reconstruct it from source rows | Held-out cases with on-hand-only and late-inbound-only coverage |
| 68 | §2.3, §8.3 | After fixing finiteness, the text still said the autonomy bound made the problem "decidable"; a negative S05 excess was also overclaimed to prove the cost bound could never bind anywhere | The objective provides decidability; the bound defines delegated execution authority and shrinks the authorized region. The negative result proves only that the old reference was wrong and inert for S05 | Logic check against the finite pre-bound set and arbitrary expensive candidates |
| 69 | §12 | The tail of the CI-grep sentence had been accidentally joined to the source-reference blockquote | Restored the coding rules and loader rules as separate prose blocks | Markdown structure sweep |
| 70 | §3, §5.1, §11–§12 | `state: not_established` was embedded in the compiled rule, so a held-out confirmation record could never release named-primary without recompiling policy | The pack now stores only the release predicate, subject, evidence source and affirmative-record rule. State is resolved per run; an integration test injects confirmation and verifies release without a pack change | Policy/data separation and held-out confirmation test |

**What the fourth pass actually shows.** The first three passes found wrong mechanisms. This one found
a rule that was never read out of a one-page memo we had all quoted repeatedly — and a set of fixtures
that had been hand-derived four times and moved four times. The fixtures were never arithmetic errors;
each was a claim that the rule set was complete, and each was wrong.

That is a fact about method. **A design document should specify the system and let the implementation
compute the outputs.** Worked examples belong in it to illustrate machinery — frontiers, dispositions,
trade-offs — not to assert answers.

The fifth pass then proved the label alone is not the fix: the fourth pass declared the fixtures
illustrative and left a table that contradicted the mechanism, omitted better plans, and included an
option inadmissible under a bound declared two sections earlier. Calling an example illustrative does
not cure an example that contradicts the specification. What actually cures it is what this pass does:
**pick one enforcement model per rule, put every admissibility bound inside the solve, and enumerate
rather than choose.** With the bound inside the solve, scenario 05 has 12,159 admissible allocations —
a number no one was ever going to arrive at by picking three plans that looked interesting.

Structural facts (the 240 unit-late-day floor, the six sole-eligible components, the certification
eliminations) stay in the technical design because they are properties of the data. Allocations appear
only as generated output with their assumptions listed. Golden files come from the implementation and
are reviewed once (§12).

**The pattern is worth naming.** Every one of these is the same failure: the document stated a
guarantee — no paralysis, exact dates, zero-change reruns, verified facts — and the mechanism
underneath did not deliver it. Guarantees are where adversarial review pays and self-review does not,
because the author reads the promise and the reviewer reads the mechanism.
