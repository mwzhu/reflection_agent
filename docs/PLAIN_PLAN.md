# Apex Procurement Agent — Plain-English Design

**Document:** `docs/PLAIN_PLAN.md`
**Explains:** [MERGED_PLAN.md](./MERGED_PLAN.md), which stays the authoritative specification
**Command:** `python3 agent.py --scenario <scenario.sqlite>`

---

## How to read this

This document says the same things as `MERGED_PLAN.md` in shorter sentences. Every section
points back to the section it explains, like this: **(§8.2)**. When the two documents disagree,
`MERGED_PLAN.md` wins.

Three things in the original are **not** repeated here:

| Left out | Why | Where it lives |
|---|---|---|
| The "an earlier draft was wrong about X" passages | They record how the design was reviewed, not what it does | [DECISION_LOG.md](./DECISION_LOG.md) |
| The full JSON policy rules and the 13 coding-agent prompts | They are literal build inputs, not concepts to understand | `MERGED_PLAN.md` §5.1 and §16.4 |
| Exact fixture allocations | The design deliberately stopped hand-writing them. The implementation generates them as golden files | `MERGED_PLAN.md` §12 |

Read sections 1 to 5 for the idea. Read 6 to 15 for the machine. Read 16 to 20 for the work.

---

## 1. What the agent does

You give it one SQLite file. It reads the production schedule, the parts list, the inventory, and
the open purchase orders. It also reads the company policy and the memos. It then writes new
purchase orders and alerts back into the same file.

```
scenario.sqlite  +  policy PDFs  →  [ agent ]  →  purchase_orders rows
                                               →  alerts rows
```

Nobody prompts it. It runs once, decides everything, and explains itself in writing.

**Success means seven things (§1):**

| Goal | Test |
|---|---|
| Correctness | Exact arithmetic and dates. Every order obeys every hard rule |
| Generalization | It runs on parts, suppliers, and dates it has never seen. No hardcoded IDs |
| Defensibility | A planner can rebuild any decision, including why each rejected supplier lost |
| Honesty | When the data cannot support a rule, the agent says so |
| Safety | Policy is never broken quietly. Exceptions are explicit and never automatic |
| Determinism | Two runs on the same input give the same output |
| Idempotence | A second run adds no duplicate orders and no duplicate alerts |

**It does not do (§1):** demand forecasting, safety stock, negotiation, sending orders to
suppliers, freight cost, multi-level parts explosion, or changes to orders that already exist.

---

## 2. Glossary

Read this once and the rest of the document opens up.

| Term | Meaning |
|---|---|
| **Component** | A part Apex buys, such as `CMP-003` Neodymium Magnets |
| **BOM** | Bill of materials. How many of each component one finished product needs |
| **MOQ** | Minimum order quantity. The smallest amount a supplier will sell |
| **ASL** | Approved supplier list. A supplier not on it cannot be used |
| **Lead time** | Days between the order date and the delivery date |
| **Inbound** | Material already on order and not yet received |
| **Bucket** | One deadline. Demand is grouped by the date the material is needed |
| **Net requirement** | Demand minus on-hand stock minus inbound |
| **Forced surplus** | Overbuy the supplier or the policy imposed. Nobody chose it |
| **Discretionary surplus** | Overbuy the agent chose, to recover lateness |
| **Unit-late-day** | One unit that arrives one day late. Ten units three days late equals 30 |
| **Route** | One way to buy one component: a supplier, a price, a lead time, a shipping mode |
| **Rolling window** | A measure over the last 12 months of orders |
| **Sole-eligible** | Exactly one supplier survives the rules for that component |
| **Disposition** | What the agent decided to do with a candidate plan. See §12 |
| **Executable set** | Plans the agent may write on its own. See §11 |
| **Recommendation set** | Plans it may only report to a human. See §11 |
| **MILP** | Mixed-integer linear program. The model used to pick quantities |
| **Evidence contract** | The declared answer to "what does the agent do when the proof is missing?" See §5 |

---

## 3. Five rules the design never breaks (§0)

**1. No language model touches a number.**
A model reads the policy PDFs once, offline, and turns them into a reviewable rule file. At run
time the model never does arithmetic, never picks a supplier, and never writes to the database.
The reason is simple. The model is good at English and bad at exact math, so it gets the English
job only.

**2. A proven policy violation is never written as a purchase order.**
Non-compliant options still appear, but they appear in `alerts` as advice for a human.

**3. The search is certified and independently checked, never heuristic.**
A heuristic that fails to find a plan looks exactly like a plan that does not exist. That
confusion is dangerous, because "my search failed" would quietly become "the policy got relaxed."
So the agent may not pick a supplier, claim infeasibility, or take an exception unless the solver
finished with a certificate **and** the answer survives an exact recheck (§8.1).

**4. Missing evidence is declared, not guessed.**
Absent history is not zero history. See section 5, which is the heart of the design.

**5. Nothing keys off an ID.**
Policy concepts resolve to whatever components and suppliers exist in the database being planned.
The memos and the databases use different ID namespaces, so ID matching would break on unseen data.

---

## 4. What the data told us (§2)

Findings are marked **[V]** when the files confirm them, and **[I]** when they are readings of
policy text that the data cannot settle. Every **[I]** finding is tested both ways and disclosed
in an alert.

### 4.1 The scenarios

**[V]** All six databases share identical master data: products, components, suppliers, BOM, and
supplier catalog. Only the config, inventory, schedule, and existing orders change.

| Scenario | Date | Existing POs | Components short | What it exercises |
|---|---|---:|---:|---|
| 01 baseline | 2025-09-01 | 0 | 13 | Ordinary planning |
| 02 partial | 2025-09-01 | 4 | 11 | Netting inbound. MOQ against allocation rules |
| 03 tight | 2025-09-01 | 0 | 17 | Real infeasibility. Certification against deadline |
| 04 low inventory | 2025-09-01 | 0 | 19 | Breadth and MOQ rounding |
| 05 competing | 2025-10-05 | 0 | 17 | Shared parts. The air-freight memo has expired |
| 06 simple | 2025-09-01 | 0 | 2 | Control case and idempotency check |

> **The main risk of overfitting.** These six files never vary the master data. The held-out
> scenarios almost certainly will. Any logic that assumes this exact catalog passes all six tests
> and then fails the real one. Section 15 lists the held-out cases the test suite invents for
> itself as a result.

### 4.2 Traps in the data

**`current_date` is a reserved word in SQLite.** Unquoted, the query returns today's date on the
host machine instead of the scenario date. It must be written as `scenario_config."current_date"`.
A unit test guards this.

**Memos and databases name parts differently.** The April memo says `RM-3003` and the August memo
says `RM-3005`. Neither ID exists in any database. The parts are `CMP-003` and `CMP-005`. The
assignment brief adds a third namespace, `MFG-5030`. Rules therefore match on meaning, not on IDs.

**The critical-parts list is written as English categories, not IDs (§6 of policy).** It names
microcontroller ICs, power MOSFETs, PCB blanks, neodymium magnets, and all sensor ICs.

**[I] Three of the seven part mappings are inferences.** `CMP-005` is a PCB *assembly*, and the
policy says PCB *blank*. A blank is bare and an assembly is populated, so they are different
articles. `CMP-014` Pressure Transducer and `CMP-015` Humidity Sensor are not labelled ICs, and
only the parenthetical in the policy connects them. Four mappings are direct name matches:
`CMP-006`, `CMP-007`, `CMP-003`, and `CMP-013`.

**An inference does not get to decide the `CMP-014` case.** Its domestic premium is **45.5%**.
That clears the non-critical bar of 35% but not the critical bar of 50%. While membership stays
unresolved, the agent may only take an action that is valid under **both** readings, so the cheaper
international route cannot execute merely because the looser reading would allow it. It appears as
a non-executable alternative that a configured classification would settle.

> **Non-membership is the looser reading, and this is counterintuitive enough to state plainly.**
> Calling a part non-critical means a *35%* premium threshold instead of 50%, so international
> unlocks at a **lower** premium. It also means an *85%* concentration cap instead of 70%, and no
> dual-source diagnostic. A classification miss is therefore unsafe in three places at once. That
> is why unmatched parts are not simply defaulted to non-critical.

**The `is_domestic` column contradicts the policy.** Policy defines domestic as the United States
and Canada. `SUP-110` sits in Canada with `is_domestic=0`. Policy wins, and the disagreement
raises a data-quality alert. The column is never used as a fallback, because this case already
proves it unreliable.

**Certification rules are stricter than they look (§2.1 of policy).** ISO-9001 is required for
electronics, PCBs, and safety-critical parts. UL listing is an **extra** requirement, and only for
power-supply parts. That extra rule alone makes three components sole-eligible.

**The August PCB memo asks a question the schema cannot answer.** It allows only suppliers "from
whom we have previously received and accepted PCB shipments." There is no receipts table, and
`purchase_orders` is empty in five of six scenarios. Section 7.5 explains the inference we adopt.

**Memo windows already matter.** The air-freight authorisation runs 2025-07-01 to 2025-09-30.
Scenario 05 is dated 2025-10-05, so the memo is expired there. Every rule reads the scenario date,
never the host clock.

**Existing deliveries do not match catalog lead times.** All four pre-existing orders arrive three
or four days later than `order_date + lead_time`. Business days do not explain it either. Stored
dates are treated as authoritative for netting. Our own generated dates follow the policy formula.
This is an open question for Apex (Q8).

**Quantities can be fractional.** `CMP-011` Conformal Coating is measured in cans and has BOM
factors of 0.25 and 0.5. Scenario 01 needs exactly 15.25 cans. All quantity and money math uses
`Decimal`, never floating point.

**The approval thresholds never fire in this data, but they would on a larger order.** The largest
possible single line here is about **$6,050** and the largest whole run about **$22,300**. Policy
sets gates at $50,000 and $150,000. **[C]** A 400-unit order of one product produces a single
$66,725 line, and a 500-unit run across all four products produces six lines above $50,000. So the
gate is untested by the fixtures and very much live on held-out data.

### 4.3 Six parts have exactly one legal supplier (§2.4)

After the approved list, certifications, the PCB freeze, and the domestic gate:

| Component | Only supplier | What removed the others |
|---|---|---|
| `CMP-001` Copper Wire | SUP-111 | Domestic is cheaper, so the international gate never opens |
| `CMP-005` PCB Assembly | SUP-101 | SUP-113 is off the ASL. The August memo freezes SUP-103 and SUP-110 |
| `CMP-009` Sealed Bearing | SUP-102 | Domestic is cheaper |
| `CMP-014` Pressure Transducer | SUP-112 | Premium of 45.5% does not clear the 50% critical gate |
| `CMP-017` Capacitor Bank | SUP-101 | SUP-106 holds ISO-9001 but not UL |
| `CMP-018` Transformer Core | SUP-101 | SUP-104 holds ISO-9001 but not UL |

Hold on to this table. It is the test case for two separate design decisions.

**First, it is why "absent history is not zero history" is a correctness issue.** Section 5
explains that failure.

**Second, two of these six suppliers hold no certifications at all.** `SUP-111` for copper wire
and `SUP-107`, one of only two magnet sources, both have an empty `certifications` field. If the
policy's "safety-critical parts" clause were read permissively, meaning any part *might* be
safety-critical and therefore needs ISO-9001, both suppliers vanish. Copper wire becomes
unorderable in every scenario, and magnets drop to one source, which makes the 20% secondary rule
impossible and blocks all magnet demand. Section 7.4 explains why that concept needs positive
evidence rather than both-ways treatment.

---

## 5. Evidence contracts — the central idea (§3)

### The problem

Several policy rules ask about facts the database does not hold:

- 12 months of rolling order volume, for the concentration caps.
- PCB receipt history, for the August freeze.
- Approval records, for the spending gates.

So the agent must answer one question before anything else: **what do I do when I cannot prove
a rule either way?**

### Why a default answer is wrong

Suppose the agent treats the visible orders as the complete 12-month history. For a part with one
eligible supplier, the concentration cap then reads `x ≤ cap · x`, which forces `x = 0`. All six
parts in the table above become unorderable, in every scenario. Scenario 04 needs all six at once.

The underlying mistake is a category error. The policy limits a supplier's share of a **rolling
12-month volume**. A single planning run is not that object. Reading it as a per-run split also
contradicts the policy's own sole-source exception.

So this is a correctness bug, not a matter of taste. **Absent history is not zero history.**

### The answer: name the contract and run under it

The agent runs under one of two declared contracts. Apex chooses, and the agent states its choice
in an alert on every run.

| Question | **Benchmark** (the default) | **Production** |
|---|---|---|
| 12-month history missing | Hold the rule `UNKNOWN`. Order anyway, under a named assumption, and report the visible ratio for information | Refuse to decide. Emit `DECISION_REQUIRED` |
| Per-order allocation rules | Enforced | Enforced |
| PCB receipt history | A documented inference is allowed | Real receipt records required |
| Approval state | Missing, so the agent recommends approval | An approval service is consulted |
| Any other unknown fact | Proceed under a named assumption plus a standing alert | Emit `DECISION_REQUIRED` |

**One rule type survives in both contracts:** a rule that is prospective and per-order. Such a rule
is provable from the order being written, so no history is needed. The April memo shows both types
in one document. Its "at least 20% to a secondary supplier" clause binds in both contracts. Its
"50% cap" clause does not, because that cap inherits the rolling-window framing.

**Why benchmark ships as the default.** The assignment supplies each SQLite file as the complete
snapshot. Under the production contract, **the agent writes no purchase orders at all**, in any
scenario, because the policy sets a rolling cap on every component and no history exists. That is
correct behaviour for a strict contract, and it is exactly why it is not the default.

**Every run states which contract it used.** Without that, an empty `purchase_orders` table cannot
be told apart from "nothing was needed." Section 13.2 describes the run-accounting alert that
carries it.

---

## 6. How the system is built (§4)

```
        agent.py --scenario X.sqlite
                    │
   ┌────────────────┼────────────────┐
   │                │                │
 load DB      compiled policy    evidence
 + validate   rules + sources    contract
   │                │                │
   └──────► policy evaluator ◄───────┘
            PASS / FAIL / UNKNOWN
                    │
   ┌────────────────▼──────────────────────────────┐
   │      DETERMINISTIC CORE — no model here       │
   │  ledgers → routes → certified solver → outcome│
   └────────────────┬──────────────────────────────┘
                    │
        independent validator (shares no code)
                    │
        rationale and alert text (templates)
                    │
        atomic commit: lock, re-check, write or roll back
```

**Why a fixed pipeline instead of a model in a loop.** "Autonomous" here means it runs with no
human prompting. It does not mean a model in a `while` loop. Procurement planning is constraint
satisfaction over exact numbers and dates, with legal-style text on top. The model is good at the
text and bad at the arithmetic, so the design splits the job along that line. The split buys
reproducibility, per-rule unit tests, sub-second runtime, and no token cost per run.

---

## 7. Policy as data (§5)

### 7.1 Rules live in a file, not in Python

Every behavioural rule is a row in a checked-in JSON pack. Each row carries its source document,
the exact quoted text, its effective dates, its severity, and its links to other rules. **No policy
constant appears in Python code.** Adding a memo changes behaviour with no code change. The runtime
files are `compiled_policy.json` and `concepts.json`, both loaded with the standard library.

### 7.2 One memo clause becomes one rule

The April magnet memo contains three separate instructions, and collapsing them caused a real bug.
They compile as three rules:

| Rule | What it says | Evidence it needs |
|---|---|---|
| Rolling cap | No supplier above 50% of 12-month magnet volume | 12 months of history. The contract decides the outcome |
| Secondary allocation | Every magnet order gives at least 20% to a second supplier | None. Provable from the order itself |
| Named primary | SUP-107 stays the primary volume supplier until MagnetPro's capacity is confirmed | None. It shapes the choice |

Two more rules deserve naming, because both were nearly missed:

**Sub-MOQ orders have a policy route (§4.1).** The policy says orders below MOQ require written
supplier approval. Without that rule the planner has only two framings for an extreme MOQ, overbuy
or nothing, and neither is what a human does when the need is 1 and the MOQ is 1,000. So whenever
MOQ exceeds the need, the agent produces **two** candidates: the MOQ order, which is executable
with the surplus disclosed, and the sub-MOQ order, which needs written approval. The alert costs
both and lets Apex choose.

**A directive with an estimated duration does not expire on its own.** The August PCB memo says
"estimated 60-90 days" and sets no end date, so it binds indefinitely. On a scenario dated a year
later it would still suppress two PCB suppliers. The agent keeps it in force, because no memo
rescinded it, and raises a `POLICY_CONFLICT` alert once the date passes the stated estimate: *"in
force 312 days against a stated 60-90 day estimate. Confirm it remains current."* This is Q28.

### 7.3 How each rule is verified against its source

Quoting the source is not enough. A quote such as "the concentration limit is reduced" would
happily support an invented figure of 0.40, because the quote contains no number. So the compiler
demands a **covering quote for every load-bearing value**: the selector, the number with its unit
and window, the dates, and any approval authority. A rule whose `0.50` is not witnessed by a quote
containing "50%" fails to compile.

Where a memo genuinely inherits a value it does not state, the rule must carry an explicit pointer
to the rule it inherits from. Inheritance is allowed. Silent invention is not.

### 7.4 How policy words find real parts (§5.3)

Cheapest method first:

1. **Structured columns** — category, certifications, country, hazard flags.
2. **A synonym table in config** — for example `neodymium_magnet: [neodymium, ndfeb, n52, rare earth magnet]`. This is data, not code, and it covers the supplied files with no model call.
3. **An optional model call**, for leftovers only. It returns a member flag, a confidence, and a reason, and every answer is cached with an evidence trace.

The model tier adds recall for names the synonym table misses. It is deliberately **not**
load-bearing. There may be no model server, and correctness cannot depend on the network.

**Matching is on word boundaries, and every concept ships negative fixtures.** A plain substring
test for `magnet` matches a held-out *"magnetic reed switch"* and silently drags an unrelated part
into the rare-earth rules. The negative cases are part of the generalization suite, not an
afterthought.

**Two kinds of unresolved concept, handled in opposite ways.** Collapsing them turns a safety rule
into a paralysis bug in one direction and a silent relaxation in the other.

| | **Enumerated** — the policy's critical list | **Unenumerated** — "safety-critical parts" |
|---|---|---|
| What is closed | The category list. No new critical category can be invented | Nothing. The policy names no members and the schema has no column for it |
| What is open | Membership. Whether a part is a "sensor IC" is a question of meaning | Everything |
| How it resolves | **Both ways.** Take the conservative intersection: the 50% premium threshold **and** the 70% cap **and** the dual-source diagnostic | **Positive evidence required.** A structured column or a configured mapping. Absent that it is not established, under a named assumption |

The reason for the split is in section 4.3. Defaulting unmatched parts to non-critical relaxes
three rules at once. Treating every part as possibly safety-critical eliminates every uncertified
supplier and makes copper wire and magnets unorderable. Neither is guessed, and both are disclosed.

**Supplier attributes are derived, not read.** "Normalised" is not a specification, and each of
these is a one-line change on held-out data, so the rules are written down. All alias lists live in
`concepts.json`.

| Field | How it is derived | When it cannot be resolved |
|---|---|---|
| `country` → domestic | An alias table against the policy's US-and-Canada definition. Disagreement with the stored flag raises `DATA_QUALITY`, and policy wins | `UNKNOWN` plus both-ways. **Never falls back to `is_domestic`** |
| `certifications` | Split on separators, strip, uppercase, remove punctuation, so `"ISO 9001; UL Listed"` and `"iso-9001"` match | Empty means *holds none*, which is a fact, not an unknown |
| `sustainability_rating` | Parsed to a number so "below B" is computable | Evaluate both readings. If they differ, the route is recommendation-only |
| `relationship_tier` | Case-insensitive match | Evaluate both, and disclose whenever the answer depends on it |
| `on_approved_list` | Read directly | NULL is **not** approved. Fail closed and alert |

### 7.5 The PCB incumbency inference [I]

The memo's own gloss is the operative test: if a PCB supplier is new to us, we cannot order PCBs
from them. The ladder is:

1. A prior order row for that part and supplier. Note the limit: an order is not a receipt.
2. A relationship that clearly predates the memo, plus a catalog listing and valid certifications. SUP-101 fits, since it has been the primary electronics supplier since 2018.
3. Otherwise, not an incumbent.

This passes the gloss reading and fails the memo's stricter first sentence. No data in the schema
can satisfy the strict reading for anyone. We ship the inference under the benchmark contract, with
a standing alert, because choosing between the two readings is Apex's call (Q5).

### 7.6 Three-valued evaluation and precedence

Every check returns `PASS`, `FAIL`, or `UNKNOWN`, with its evidence. `UNKNOWN` is never a silent
pass and never an automatic block. The active contract decides what happens next.

Two different kinds of `UNKNOWN` must not be confused. A **rule-level** unknown is an evidence gap,
and the contract may license execution. A **candidate-level** unknown is an unresolved eligibility
test on one supplier, and it may only enter the recommendation set. A supplier we cannot prove is
certified never ships an order, under any contract.

Precedence is fixed and deterministic:

1. Drop rules not in force on the scenario date.
2. An explicit "this supersedes that" link wins inside its scope.
3. A narrower rule beats a broader one.
4. A later date wins only when specificity and authority are equal.

An unresolved conflict between two hard rules is `UNKNOWN`. It blocks the affected action and
raises an alert. It is never averaged.

### 7.7 What "per purchase order" has to mean

The memo says every purchase order must include a secondary allocation. But a `purchase_orders`
row holds exactly one supplier, so no single row can satisfy that literally.

The rule is therefore measured over an **allocation group**: all new orders for one component
created in one planning run.

- The **primary** is the supplier with the largest share of the group.
- The **secondary allocation** is everything held by the other suppliers. It must be at least 20%.
- Existing open orders are reported but stay out of the denominator, since they belong to earlier runs.

Each grouped line records its group ID, so a reviewer can reconstruct the exact set the 20% rule
was applied to. Three other groupings are defensible and would change the answer materially. This
is open question Q21.

---

## 8. The four numbers per component (§6)

Total demand alone is not enough, because a late delivery cannot repair an early shortage. But
date-blind buying is also wrong, because it can never expedite. So the planner tracks four
quantities:

| Quantity | Meaning | What it drives |
|---|---|---|
| `eventual` | On-hand plus every inbound order, whenever it lands | Baseline buying |
| `on_time[t]` | On-hand plus inbound that lands by deadline `t` | Lateness |
| `eventual_gap` | Demand minus `eventual`, floored at zero | How much to buy |
| `recoverable[t]` | The late portion a **strictly faster** new route could still save | Whether to expedite |

**Baseline buying uses `eventual_gap`.** If material is already on order, buying more does not make
it arrive sooner.

**Expediting uses `recoverable`.** Sometimes a committed order covers the demand but lands after
the deadline. If a faster eligible supplier can still make it, the agent may buy a bridge quantity.
The late order then becomes disclosed surplus. This is routine procurement practice, and a
date-blind planner cannot do it.

**The word "strictly" makes reruns safe.** Scenario 01 buys 104 magnets that arrive 09-15 against
a 09-12 deadline. On a rerun the gap is still open, but the best available route is still the same
09-15 arrival. No strict improvement exists, so nothing new is ordered.

### 8.1 Which existing orders count, and the date that decides it

The test is on the **delivery** date, never the order date.

| Condition | Treatment |
|---|---|
| Delivery date on or after the scenario date | **Counted** as inbound, at its stored delivery date. The boundary is inclusive |
| Delivery date before the scenario date | **Excluded** pending reconciliation, with an alert. Receipt status is unknown |
| Delivery date is NULL | Excluded, with `DATA_QUALITY`. An undated commitment cannot be time-phased |

> **Why the wording matters this much.** An earlier draft said "a PO dated before the scenario date
> is excluded." Read against the *order* date, that discards all four of scenario 02's existing
> orders, which were placed in August. Scenario 02 would then show 13 short components and a
> 208-unit magnet gap instead of the verified 11 and 58. Every arithmetic check in the document
> would still have passed.

The asymmetry behind the exclusion rule is worth keeping in mind. Counting an overdue order risks
double-counting against inventory, which under-orders and stops production. Excluding it
over-orders, which only costs money.

**No safety stock is planned.** The July memo excludes "speculative or safety-stock orders", which
shows safety stock exists at Apex but is not this agent's job.

### 8.2 Quantity discipline

`Decimal` everywhere. The order of operations is fixed, because changing it changes the answer:
**aggregate per component and deadline → round → apply MOQ.**

**Units of measure are classified in config, never inferred.** The supplied data uses `each`, `kg`,
`meter`, `tube`, and `can`. A held-out `box`, `roll`, or `liter` has no defined behaviour
otherwise. Discrete units round up to whole units. Continuous units round to a configured
precision. An **unrecognised unit is treated as discrete** with an alert, because rounding a
fractional box up is recoverable and shipping a fractional one is not. Pack increments are Q11.

### 8.3 Bad input is handled by blast radius, not uniformly

"Alert, never crash" is right for a fault confined to one route. It is wrong for anything that
distorts demand. Skipping a malformed BOM row silently understates a component shared across
products, and the run then commits orders that are quietly too small.

| Fault | Behaviour |
|---|---|
| Missing inventory row, missing catalog row, one unparseable offer, a null price or lead time | Skip that route or component, alert, continue |
| Empty schedule, product with no BOM, deadlines in the past | Alert. The run proceeds and is legitimately alert-only |
| Malformed BOM, schedule, inventory, or config. Non-positive quantities. Duplicate keys. Broken references | **Global failure. No writes. Exit 3** |

---

## 9. Choosing suppliers: gates first, then preferences (§7)

### 9.1 Hard gates — never relaxed

- Membership of the approved supplier list.
- ISO-9001 for electronics, PCBs, and safety-critical parts. UL as well for power-supply parts.
- Scoped memo restrictions, such as the PCB freeze.
- The part must exist in that supplier's catalog.

A route that fails a hard gate never reaches the optimizer. A route whose gate result is `UNKNOWN`
may only appear as a recommendation.

### 9.2 Two dates, never merged

```
expected_delivery  = order_date + shipping_lead        ← written to the purchase order
material_available = expected_delivery + receiving_buffer   ← used to test the deadline
feasible           = material_available <= deadline
```

Policy requires `expected_delivery_date` to reflect the supplier's quoted lead time. Folding an
inspection buffer into it would falsify a field the supplier is measured against. The buffer
belongs only in the feasibility test. It defaults to zero pending Q9, and any non-zero value is
alerted.

### 9.3 Air freight has four conditions, and the fourth is easy to miss

Air freight applies only when the memo window is open, the supplier is international, the demand
is confirmed production, **and** the standard lead time would miss the deadline. In scenario 01 the
ocean route already arrives four days early, so air freight is not used even though it is
authorised. That conserves the memo's $25,000 budget and avoids an unnecessary approval. Air
freight always yields `RECOMMEND_APPROVAL` and never enters the executable variable set.

### 9.4 An exception belongs only to the demand that justified it

This is subtle and it leaks in two directions. Eligibility is decided against one deadline, but
routes are built per component. So without a further rule, quantity that serves a *later*,
comfortably-served deadline can ride on a permission an *earlier* deadline opened.

- **Bad orders.** The "no domestic supplier meets the deadline" condition carries no approval gate. One tight early deadline would make an international route eligible for the whole component, and later demand that domestic supply covers fine would be sourced internationally against the policy's stated preference.
- **Bad advice.** Air freight leaks into the counterfactual. "Approving air freight buys you N late-days" overstates N, because it air-freights quantity that never needed it. A planner approving on that basis authorises more air spend than the situation required.

The fix is structural. Allocation is tracked per route **and per deadline**, positive only where
that route's exception actually applies to that deadline. A single cap then applies **across all
routes sharing one exception**, set at the net unresolved shortage of the deadlines that opened it.
Capping each route separately would let three exception routes each consume the whole allowance.
This is Q30.

### 9.5 The domestic gate, and the reading that changed

International sourcing is *permitted*, not required, when one of three things holds:

1. No domestic supplier meets the deadline, or
2. The domestic premium is **strictly greater** than the threshold (50% for critical parts, 35% otherwise), or
3. No domestic source exists.

Strict inequality matters. `CMP-007` sits at exactly 50.0% and stays domestic. `CMP-006` at 25.9%
stays domestic. `CMP-003` at 78%, `CMP-005` at 60%, `CMP-013` at 61%, and `CMP-015` at 69% unlock
the option. With no international offer at all there is no ratio to compute, and condition 3
governs instead.

**Whether the domestic preference still applies depends on which condition opened the gate.** This
reading changed, and the reasoning is worth following, because it is the kind of bug that hides
forever.

An earlier draft made the preference unconditional once eligibility was granted. Trace that through
every path and condition 2 becomes provably unable to change any outcome:

| Gate opened by | Result under an unconditional preference | Why |
|---|---|---|
| nothing | domestic | International is ineligible |
| 1, the deadline | international | On-time feasibility already decided it |
| **2, the premium** | **domestic** | The preference outranks cost, so the premium test changes nothing |
| 3, no domestic source | international | There is no domestic option |

A clause that can never change an outcome is not a clause we implemented. It is one we read wrong.
And it is the clause the policy bothered to give two different thresholds.

The commercial meaning of a price-premium threshold **is** the preference, quantified: *we will pay
up to 35% extra, or 50% for critical parts, to buy domestic, and past that we will not.* Applying a
separate unconditional preference on top double-counts it. So:

- **Gate shut** — international is ineligible. Domestic wins on eligibility.
- **Gate opened by 1 or 3** — the preference is moot. Domestic cannot do the job.
- **Gate opened by 2** — the premium test already settled price. Both compete, and the remaining preferences decide. The loser becomes a `COST_OPPORTUNITY` alert carrying the money, whichever way it went.

**[C]** On this catalog that changes the selected supplier for `CMP-003`, `CMP-013`, and `CMP-015`
wherever both routes can meet the deadline. It is a declared interpretation, not a literal reading,
so every affected run discloses it. This is Q22 and Q29.

### 9.6 Low-rated suppliers pass through a gate, not a scoreboard

Policy says suppliers rated below B are subject to additional review and should only be used when
no alternative is available. That is conditional permission plus a review requirement. It cannot be
reduced to a score that a higher-ranked preference can quietly outvote. A below-B supplier is
usable only when both of these hold:

1. A completed, independently checked counterfactual solve finds no compliant plan using only
   B-or-better suppliers.
2. The required review is represented, either as a `DECISION_REQUIRED` disposition, or **[I]** as
   discharged by a memo that directs that supplier's use for that part.

**[I]** The second route is what keeps magnet planning alive. The April memo is signed by the VP of
Operations and names SUP-107 as the primary volume supplier. Reading that as satisfying the review
requirement is plausible but not proven, so those orders carry `EXECUTE_WITH_ASSUMPTION` and an
alert (Q19). Without this route, SUP-107 is unusable. Since it is one of only two magnet suppliers,
the 20% secondary rule would then be unsatisfiable and every magnet requirement would stall.

### 9.7 Preference order, not a weighted score

Applied in strict order to whatever survives the gates. Each step maps to one policy sentence, so
each is unit-testable and the rationale can name the step that decided the outcome.

1. On-time feasibility.
2. Domestic before international, **except where condition 2 opened the gate**, in which case this step is skipped.
3. Keep strategic suppliers, **unless the alternative saves more than 15%**.
4. Prefer sustainability rating A or better, **only when price is within 10% and delivery within 5 business days**.
5. Known landed cost.
6. Shorter lead time.
7. A stable supplier fingerprint, for determinism.

**Steps 3 and 4 are conditional, and their conditions are part of the step.** The policy grants
strategic retention only up to 15% savings, and the sustainability preference only inside that
comparability window. Outside those windows the policy expresses no preference at all, and a step
that ranks anyway invents one. A bare version would prefer an A-rated supplier over one 40%
cheaper, which no sentence in the policy supports.

Business days are Monday to Friday with no holiday calendar. Catalog lead times are calendar days.
Both are disclosed assumptions.

---

## 10. Picking quantities (§8)

### 10.1 Why certified search is a precondition

Scenario 02 shows the danger. A greedy allocator finds the 58-unit bare-coverage plan but can miss
the compliant 150-unit plan, and would then request an exception that was never required. So the
system may not claim infeasibility, and may not take an exception, on heuristic evidence.

### 10.2 What "certified" actually claims, and what it does not

This claim is deliberately narrow, because overstating it would be the same kind of error the
design is built to avoid.

HiGHS is a floating-point solver. A completed run is described as **solver-certified optimal to the
configured tolerances, under the integer-scaled model**. It is not an exact proof of global
optimality over the real numbers. Three things make that safe enough to act on:

1. Quantities and costs are scaled to integers before the model is built, and coefficients are bounded.
2. The selected plan's feasibility and its full objective vector are recomputed exactly in `Decimal` from the source facts. This catches an invalid or misreported answer.
3. Small generated cases must additionally agree with exhaustive enumeration.

A **standard-library fallback solver** sits behind the same interface for environments without
`scipy`. It is a bounded branch-and-bound over the same model, and it reports `UNRESOLVED` when it
runs out of budget, exactly as a timeout does. Exhaustive enumeration is *not* offered as a general
fallback. It is tractable at this catalog's scale and not for a held-out component with ten
suppliers, so it stays the test oracle rather than a production path.

### 10.3 One small model per component

Variables are the quantity per supplier and route, **the part of each route allocated to each
deadline**, a used-or-not switch per line, and any unresolved demand at each deadline.

| Constraint | Says |
|---|---|
| MOQ | A line is either zero or at least the minimum order quantity |
| Cumulative coverage | Stock plus inbound plus new arrivals must meet demand at every deadline |
| Concentration | Existing volume plus new volume stays under the cap. **Emitted only when provable** |
| Secondary allocation | No supplier holds more than 80% of the group. Always emitted when two or more suppliers are eligible |
| Exception scoping | A route may serve a deadline only if its exception applies there, with one shared cap per exception (§9.4) |
| Discretionary surplus | Optional overbuy stays inside the authorized fraction. See §11 |
| Excess cost | Cost stays within a fixed amount above the cheapest covering plan |
| Eligibility | Variables exist only for routes that already passed the gates |

Three details carry weight:

- The concentration constraint must include the supplier's **existing** volume on its left side. Otherwise a supplier already at the cap could take a full new allocation.
- Bounding **every** supplier at 80% is enough to guarantee the 20% secondary rule, with no extra binary variable. If the largest holds at most 80%, the rest hold at least 20%.
- **The upper bound must be derived, never chosen.** A hand-picked "safe" bound that is too small recreates false infeasibility and invalidates the certificate with no signal. It is derived from demand plus authorized surplus, and the validator re-derives it independently. An optimum sitting exactly at the bound is legal. When demand is 100 and the bound is 100, the answer genuinely is 100.

### 10.4 When fewer than two suppliers are eligible

The 20% secondary constraint is then impossible for any positive order. Dropping it would write a
proven violation, so the agent does not.

It is tempting to reuse the dual-sourcing argument below, which says an allocation cannot fix a
supply-base problem. That analogy does not transfer. The dual-sourcing rule is about the *supply
base*. The 20% rule is about *an order's allocation*, and a single-supplier order breaches it on
its face. The handling splits on why the second supplier is missing:

| Cause | Outcome |
|---|---|
| **Structurally** impossible: only one supplier in the catalog, or every alternative fails a gate that cannot be relaxed | No order. `DECISION_REQUIRED` plus `SOLE_SOURCE`, carrying the fully costed one-supplier plan **labelled non-executable**. The remediation names a real authority: qualify a supplier, or change the allocation policy. An off-ASL supplier is never presented as approvable |
| **Relaxably** impossible: a second supplier exists but is blocked by a gate that can be relaxed | An ordinary counterfactual: "relaxing X makes a compliant split available at cost Y" |

**Dual sourcing itself is a diagnostic, not a constraint.** The policy asks for two qualified
suppliers per critical part. No allocation can fix that, only qualifying another supplier can.
Encoding it as a constraint would make `CMP-005` unorderable for a reason no purchase order can
address. It emits `SOLE_SOURCE` and never blocks.

### 10.5 One calibration solve and three decision solves

| Solve | Question it answers | Produces |
|---|---|---|
| **Q — calibration** | What is the smallest compliant order the rules force on me? | `q_min`, the minimum compliant total |
| **0 — baseline** | What is the least this much material could cost? | The reference for the excess-cost bound |
| **1 — executable** | What is the best plan I am allowed to execute? | The plan that gets written |
| **2 — counterfactual** | What would relaxing one named rule buy? | Alternatives, each reported as an alert |

**Solve Q is the new piece, and section 11 explains why it matters.** It runs in three steps:

1. Find the maximum coverage available, and pin it.
2. Minimise the total quantity under every non-autonomy condition required for execution. That means MOQ, allocation rules, exception scoping, approval eligibility, and the named-primary directive.
3. Ignore lateness preferences and both autonomy bounds entirely.

So the gap between `q_min` and the net requirement is exactly the overbuy the policy and the
suppliers **forced**, with no discretion involved.

**Solve 0 needs its own objective.** Run under the main ranking it would minimise lateness first
and return an expensive plan, which is not a *cheapest* reference at all. So solve 0 minimises
eventual uncovered quantity first, then cost. It also stays defined when full coverage is
impossible, which matters for parts no supplier can cover in time.

**Solve 2 relaxes exactly one named rule** and rebuilds from a clean variable set, so a relaxation
can never contaminate the executable answer. It keeps every other solve-1 condition, including the
named-primary pin, unless that is the rule being relaxed.

There is one declared exception. Some plans worth reporting break several rules at once, so no
single-rule solve can find them. These form a small fixed set, labelled non-executable at the point
of creation. Their only job is to let an alert say what compliance *costs*, not just what it
requires.

### 10.6 The ranking used by solves 1 and 2

Each stage is optimised, then pinned as a constraint before the next stage runs.

| Stage | Minimise | Mirrors |
|---:|---|---|
| 1 | Unresolved quantity at the earliest deadline, then each later one | Coverage |
| 2 | Unit-late-days | Preference 1 |
| 3 | **Discretionary** surplus, meaning quantity above `q_min` | Economic autonomy |
| 4 | Approval and assumption exposure | Disposition quality |
| 5 | Deviation from a memo-named primary supplier | The April memo |
| 6 | International volume, **on routes whose gate was not opened by the premium condition** | Preference 2 |
| 7 | Volume moved away from strategic suppliers, **counted only where the alternative saves 15% or less** | Preference 3 |
| 8 | Sustainability penalty, **applied only inside the 10%-price and 5-day window** | Preference 4 |
| 9 | Known landed cost, then MOQ-driven excess | Preference 5 |
| 10 | Lead time, then line count, then a stable fingerprint | Preferences 6 and 7 |

**Stages 6 to 8 carry their conditions in bold because the policy sentences do.** Stated as bare
penalties they would rank plans outside the windows the policy grants, and would then disagree with
the preference order they are supposed to mirror. A differential test builds boundary cases for
every preference and asserts that route-level ordering and quantity-level optimization pick the
same plan. Divergence fails the build.

Stages 7 and 8 use coefficients computed **before** the solve, per route and deadline, rather than
comparisons between decision variables. Candidate construction works out which alternatives could
serve the same part and deadline, and stores both the coefficient and the comparison that produced
it. The validator recomputes them independently.

**Ties are broken without IDs.** A supplier fingerprint hashes the normalised legal name, country,
certifications, tier, and rating. A route fingerprint covers the part's semantic fingerprint plus
price, MOQ, lead time, and shipping mode. Neither includes a surrogate ID, so renumbering a database
cannot change a tie. Two suppliers with the same fingerprint are indistinguishable, so that
collision raises `DATA_QUALITY` and stops autonomous selection.

Stage 5 sits above stage 6 because a memo naming a supplier for a specific part outranks the
general domestic preference. Stage 3 sits above both because unneeded inventory is real spent cash,
while the rest are orderings among plans that all cover demand.

**Certified, or nothing is written.** A plan may be written only when solve Q, solve 0, and every
stage of solve 1 clear three bars: optimal status at a zero-gap request, no resource limit hit, and
a passing exact `Decimal` recheck. On a timeout the component writes no order and emits a solver
alert. A merely feasible plan may be shown as context, clearly labelled, but never executed.

---

## 11. What the agent may do on its own (§8.3)

### 11.1 Two sets, and the name that was wrong

The system produces one selected plan plus a labelled set of alternatives, split two ways:

- **Executable set** — compliant, authorized, and inside the economic autonomy bounds.
- **Recommendation set** — prohibited, approval-dependent, or evidence-dependent options, written to alerts with their costs.

> An earlier draft called these "frontiers" and said a Pareto frontier was computed. It is not one.
> Section 10 produces one executable optimum plus single-rule relaxations. A nondominated set over
> compliance, lateness, surplus, and cost is never enumerated. The old name invited a question the
> implementation cannot answer, so it is gone.

**Configuration may never move an option between the two sets.** If config could rank customer
timing above a hard cap, config would become a waiver mechanism, and no document grants that
authority.

### 11.2 Forced surplus and discretionary surplus are different things

This is the distinction between a planner and a system that stops working, and getting it wrong is
what made an earlier draft unusable.

```
forced surplus         = max(0, q_min − net_requirement)     ← the rules imposed it
discretionary surplus  = max(0, chosen_total − q_min)        ← the agent chose it
```

Only the **discretionary** part is ratio-bounded. Here is what happens when that split is missing
and the bound is applied to total surplus instead:

```
Held-out scenario: one product × 2, empty inventory
CMP-002  need 20   MOQ 25   surplus  5 ( 25.0%)  → blocked
CMP-005  need  4   MOQ 25   surplus 21 (525.0%)  → blocked
CMP-011  need  1   MOQ  5   surplus  4 (400.0%)  → blocked
→ 1 purchase order written, 13 components blocked
```

The six supplied scenarios have at most one component each where MOQ meaningfully exceeds the need.
That is exactly why the bug looked harmless: the bound had been calibrated against these fixtures'
demand sizes. On a small production run it converts an $88 overbuy of conformal coating into a
missed customer delivery.

A MOQ floor, or a secondary-allocation minimum, is imposed by the supplier and the policy. The
agent did not choose it, so gating on it is wrong. Crossing the forced-surplus review figure emits
a `FORCED_SURPLUS` alert and nothing more. Execution stays subject to the *genuine* gates, which
are the policy's own $50,000 and $150,000 approval thresholds.

Where forced surplus is extreme, the agent offers a third option rather than picking one. The
policy's sub-MOQ clause (§7.2) means the alert can cost both routes and let Apex choose. Assuming
"every planner just buys the MOQ" is usually right, and wrong at exactly the extremes where it
matters.

### 11.3 The autonomy bound

It is a **policy parameter Apex must supply**, not an engineering knob. Until they supply theirs,
the prototype ships these defaults and prints them in every decision alert:

```yaml
max_surplus_fraction:  "0.10"   # DISCRETIONARY surplus ≤ 10% of net requirement
max_excess_cost_usd:   "2500"   # spend up to $2,500 above the cheapest covering plan
boundary: inclusive             # exactly 10% is allowed
provisional: true               # emits an assumption alert until Apex sets it

forced_surplus_review_usd: "2500"   # ADVISORY ONLY — alerts, never blocks
```

The boundary is stated because "within the bound" is ambiguous. A plan exactly at the threshold
executes. Only a plan strictly above it becomes `DECISION_REQUIRED`. The 10% figure is deliberately
tight, because a prototype that buys extra inventory *by choice* and quietly is worse than one that
asks. Forced quantity stays outside the ratio either way.

### 11.4 Scenario 02: the outcome that reversed

Residual need is 58 magnets. The 20% secondary rule forces a second supplier onto the order, and
the two minimum order quantities of 100 and 50 set a 150-unit floor at $615. That is 92 units of
surplus against a 58-unit need.

**The old behaviour wrote no magnet order at all**, because 92 units is far past a 10% bound. But
that overbuy is forced, not chosen. So the agent now:

1. Writes the 150-unit minimum compliant order.
2. Emits `FORCED_SURPLUS` with the 92 units and the $615.
3. Offers the non-compliant 58-unit coverage-only plan as the `DECISION_REQUIRED` counterfactual.

The default and the exception have swapped places. The alert still carries every alternative with
its numbers, including an 8-unit shortfall option and rebalancing the existing commitments.

It also reports that the existing orders put SUP-107 at 66.7% of *visible* magnet volume against
the memo's 50% figure. That is a reported ratio, not a proven breach, because 66.7% of two orders
is not a rolling-12-month measure. The agent does not remediate it.

### 11.5 Scenario 05: the trade-off, without the fragile numbers

Need is 440 units by 2025-11-01. SUP-108 arrives 10-19. SUP-107 arrives 11-09, late, because the
air-freight memo expired on 2025-09-30. No zero-late plan exists at zero surplus.

But surplus buys lateness back **continuously**: every extra unit from the fast supplier converts
overbuy into fewer late days. So the authorized choices form a curve, not a shortlist. The three
solves each pick one point on it:

- **Solve Q** establishes the forced quantity floor.
- **Solve 1** selects the best point inside the inclusive autonomy bounds while honouring named-primary. This is what gets written.
- **Solve 2** recomputes the best alternative when that one directive is relaxed, and reports it as `DECISION_REQUIRED` with its exact timing and cost delta.

Both the written plan and the counterfactual disclose `CAPACITY_UNKNOWN` for every positive SUP-108
allocation. The alert also names the operational lever: renewing the expired air-freight
authorisation changes the available route and may remove the conflict entirely.

> **Why no allocation table appears here.** Earlier drafts hand-derived scenario 05's split four
> times across five review rounds, and it moved every time. Not because the arithmetic was wrong,
> but because each round surfaced a rule the previous derivation had not encoded. A design document
> should specify the system and let the implementation compute the outputs. The golden files come
> from the optimizer, and are accepted only after the independent validator and the enumeration
> oracle agree.

### 11.6 Why named-primary is shaping, not hard

The memo names SUP-107 as primary. The agent models that as a shaping directive, which means:

- It is never a base feasibility constraint, so it does not erase the deviating plan from the search space.
- Solve Q and solve 1 pin deviation at zero, which is how it gates membership of the executable set.
- A solve-2 run that explicitly relaxes it finds, costs, and reports the best deviating plan rather than executing it.

The reason is one sentence: **overriding an explicit VP directive is Apex's decision, not the
planner's.** A hard constraint could not express this, because it would make the deviating plan
disappear, and there would be nothing left to recommend.

Capacity is tracked as a separate concern. Every positive allocation to SUP-108 carries a
`CAPACITY_UNKNOWN` disclosure, because both the supplier notes and the catalog row say "limited N52
capacity" and no field quantifies it. That disclosure never changes a disposition by itself. The
directive bounds SUP-107's *share*, and says nothing about SUP-108's *absolute volume*, so a
compliant plan relies on exactly as much unverified capacity as a deviating one. Treating unknown
throughput as an execution gate would invent a policy rule that no document states.

---

## 12. How outcomes are reported (§9)

Three questions are answered separately, because mixing them makes partial supply impossible to
state clearly.

**First, what physically arrives:**

| Fulfillment | Meaning |
|---|---|
| `FULFILLED` | Stock, inbound, and the new plan together close total demand |
| `PARTIALLY_FULFILLED` | They cover something, but a measured gap remains |
| `UNFULFILLED` | Nothing covers it |

**Second, what the planner proved about the gap:**

| Resolution | Meaning |
|---|---|
| `RESOLVED` | No gap remains |
| `INFEASIBLE` | A completed solver run certifies, and the independent validator reproduces, that no compliant plan can close the gap |
| `UNRESOLVED` | Approval, a customer decision, missing evidence, or a timeout prevents a safe conclusion |

Only five pairs are valid. A positive gap can never be `RESOLVED`, and an unproven solve can never
be `INFEASIBLE`. That second rule matters. "I could not find one" must never be reported as "one
does not exist."

**Third, what the agent decided to do with each candidate plan:**

| Disposition | Meaning | Writes an order? |
|---|---|---|
| `EXECUTE` | Compliant, authorized, inside the autonomy bounds | Yes |
| `EXECUTE_WITH_ASSUMPTION` | Allowed only by the named benchmark contract | Yes, plus a standing alert |
| `RECOMMEND_APPROVAL` | A documented approval is missing | No. Alert carrying the complete proposal |
| `DECISION_REQUIRED` | Materially different outcomes exist and policy names no safe winner | No. Alert with the options |

A requirement executes at most one plan, and may raise any number of alternatives as alerts.

### 12.1 A withheld order must still be an actionable one

The brief describes `purchase_orders` as rows the agent *places*, and the policy makes orders above
$50,000 conditional on approval. So withholding is the correct commitment behaviour. But an alert
reading only "approval required" throws away all the work.

Every `RECOMMEND_APPROVAL` alert therefore carries the **complete proposed order**: component,
supplier, quantity, unit price, line total, expected delivery date, the threshold crossed, and the
named approving authority.

**Approval does not make that snapshot safe to place later.** Inventory, demand, prices, and lead
times move. So approval evidence is fed back into a *fresh run*, which revalidates the snapshot and
recomputes the dates before any row is committed. The alert is a dated proposal, not a
pre-authorised commitment.

**There is no flag to switch this off.** A `--approval-gate` option that moved a
`RECOMMEND_APPROVAL` candidate into `purchase_orders` would be precisely the waiver mechanism
section 11.1 forbids. If Apex answers Q26 with "rows are proposals", that becomes a declared data
contract in the same shape as the evidence contracts: named, alerted on every run, reviewable. Not
before.

### 12.2 Emergency procurement is explicitly unsupported

The policy allows an approval bypass up to $75,000 for orders required to prevent a production
stoppage. Proving that condition needs production lead time, work-in-progress state, and line
schedules. None of these exist in the schema. The agent can see that material arrives late. It
cannot see that production stops.

So the bypass cannot be claimed, and such candidates yield `RECOMMEND_APPROVAL` carrying the
lateness evidence we do have. This is a decision, not a gap: a guessed stoppage heuristic would let
the agent authorise its way around an approval gate on evidence it does not have.

---

## 13. What gets written (§10)

### 13.1 Purchase orders

`unit_price` is copied from the catalog and never invented. `order_date` is the scenario date.
`expected_delivery_date` is the route's approved lead-time calculation.

**The order number is derived, not sequential.** It is `APX-` plus 8 hex characters of the action
key, and it is checked against existing numbers **inside the write transaction**. A held-out
database may already use any scheme, including the same `PO-####` shape the schedule uses, so a
sequential scheme risks a primary-key collision on data we have not seen.

**The prefix is convenient, not proof of ownership.** Every agent-written rationale begins with a
machine-readable marker holding the format version, the full action-key digest, and the demand
fingerprint. A row counts as owned only when that marker validates against its stored business
fields. An `APX-*` row with no valid marker is external data and raises `DATA_QUALITY`.

The **rationale** is generated from structured facts, never from free text. Each one states:

- The production orders and deadlines that created the need.
- The stock and inbound quantities netted against it.
- The quantity ordered, and any surplus with its reason.
- The supplier's eligibility, route, price, and arrival arithmetic.
- The preference step that decided the choice, and each material rejection with its rule ID.
- The domestic or sole-source justification, where one applies.
- Any approvals required, and every assumption relied on.
- The requirement's fulfillment and resolution status, including any residual quantity.

### 13.2 Alerts

Plain prose, matching the sample in the brief. Each alert answers three questions: what is wrong,
what the agent did, and what a human should do. Eighteen categories exist, including `UNMET_DEMAND`,
`LATE_ARRIVAL`, `POLICY_CONFLICT`, `SOLE_SOURCE`, `DATA_QUALITY`, `ASSUMPTION`, `EVIDENCE_CONTRACT`,
`CAPACITY_UNKNOWN`, and `SOLVER_UNPROVEN`.

**Two surplus categories are deliberately distinct.** `FORCED_SURPLUS` is overbuy a MOQ or an
allocation rule imposed. `RECOVERY_SURPLUS` is duplicate supply the agent chose to buy to recover
lateness. Only the second is a decision, and only the second is bounded by the ratio in section 11.

**Every run writes exactly one `RUN_ACCOUNTING` alert**, because an alert-only run and a broken
agent are otherwise indistinguishable from the output tables. Several correct outcomes are
alert-only. It reads like this:

> Managed 19 component requirements: 12 covered by agent orders ($8,430 across 14 POs), 4 deferred
> for decision, 2 have no eligible supplier, 1 fully covered by existing stock. Evidence contract:
> benchmark. Active directives: MEMO-2025-041, MEMO-2025-085. Inactive: MEMO-2025-072 (expired
> 2025-09-30).

**Authorship is embedded in the description text and is never optional.** The `alerts` table holds
only an ID and a description, so there is nowhere else to record it. Without the marker,
reconciliation could not tell an agent-written row from a human-written one, and would delete
someone else's alert.

### 13.3 Running twice changes nothing

Each action gets a deterministic key covering the demand it serves, the component, the supplier,
the route, the planning date, and the policy pack version.

**The demand fingerprint must be exact, not a coarse label.** It hashes the sorted set of order IDs,
deadlines, and quantities the action serves, plus the netting inputs. A coarse label such as "the
09-12 magnet cohort" collides. If inventory changed externally, the label stays the same while the
correct action changed, and the rerun would silently skip a legitimately different order.

**The fingerprint must exclude the agent's own output rows.** Including a run timestamp or any
sequence derived from written rows breaks stability, because those all change after the first
write. The agent's own past orders still count as physical inbound in the ledgers. Excluding them
here only stops an action's identity from depending on its own output.

**On a rerun the agent reconstructs rather than re-derives.** The validated marker reattaches each
owned row to its source demand. The decisions layer then rebuilds the record and revalidates its
fields, and only a genuine residual gap gets a new action. That is what makes idempotency
substantive rather than accidental. A key match with different stored fields is a hard error, never
a silent skip.

`--rerun=replace` is not offered. Deleting agent-written orders would contradict the refusal to
cancel commitments. Reruns append or do nothing.

### 13.4 The commit

Plan outside the write lock, over an immutable snapshot with a state digest. Then:

1. `BEGIN IMMEDIATE`.
2. Re-check the schema and the digest.
3. Insert purchase orders.
4. Reconcile alerts.
5. Re-read and re-run the postconditions.
6. Commit everything, or roll back everything.

**Alerts are reconciled, never replaced.** The alert ID is an autoincrement key, so a
delete-and-reinsert cycle changes IDs even when the text is byte-identical. That alone would break
the zero-change guarantee. Instead the agent computes the target set and applies the difference:

| Case | Action |
|---|---|
| Owned alert present, text identical | Keep it. No write |
| Target alert missing | Insert |
| Owned alert no longer needed | Delete |
| Alert not owned by the agent | Never touched |

Never modify source tables. Never modify or cancel a pre-existing purchase order, because
cancelling a supplier commitment is a commercial act outside an autonomous planner's remit.

---

## 14. An independent second opinion (§11)

A separate implementation recomputes every invariant from the source rows and the policy pack. It
**shares no code with the optimizer**. Hard violations block the write in every mode.

It checks catalog prices, MOQ, rounding, dates, the approved list, every certification, the
allocation groups, the derived upper bound, the autonomy bounds, requirement state, action
uniqueness, and rationale citations. It also confirms that the delivery date carries no receiving
buffer, and that feasibility was tested against material availability instead.

**Checks that exist because the solve structure needs enforcing:**

- Solve 0's baseline is **independently re-derived**, not merely re-checked. Arithmetic checks cannot detect an *inflated* baseline, and an inflated baseline silently widens the one gate that bounds how much the agent may overspend.
- Solve Q's answer is independently reproduced **before** surplus is split into forced and discretionary. Only the discretionary part may be tested against the ratio. A plan rejected for *forced* surplus is a validator failure, not a business outcome.
- Every stage of solve 1 must report completed optimal status before an order is written. Any timeout, non-zero gap, or objective disagreement blocks the write.
- Every exception-bearing allocation has its predicate true for that deadline, and each exception's total stays within the shortage that opened it.
- The domestic comparator was skipped exactly where the premium condition opened the gate, and applied everywhere else.
- Stages 7 and 8 were evaluated only inside their policy windows.
- Every counted inbound order was netted on its delivery date, never its order date.
- Every `RECOMMEND_APPROVAL` alert carries a complete proposal.
- Exactly one run-accounting alert exists, and its counts reconcile.
- No timed-out solve supports a claim that no alternative exists.

**No silent gaps.** These run only on scenarios that loaded cleanly, since malformed structural
input exits before planning:

- Every component with a positive **initial** gap has a decision record, and a terminal alert if it got no order.
- Every positive **post-plan residual** gap has a terminal alert, even when a partial order executed. A partial order must never mask an unexplained remainder.
- The terminal categories are a fixed list. Anything else is an internal error and blocks the write.

---

## 15. Making it work on data we have never seen (§12)

### 15.1 Rules the build enforces

CI fails the build on any of these:

- An identifier literal such as `CMP-003` in production code.
- A policy constant outside the policy pack.
- `datetime.now()` or `date.today()` outside logging.
- An unquoted `current_date` in SQL.

### 15.2 The one place an ID may appear, and how it resolves

The April memo names SUP-107 and SUP-108 directly, so the compiled rule must carry them. The ban is
on *code* keying off IDs, not on a rule quoting a document. Those references store an ID **and** a
legal name, and resolve at run time through this deliberately asymmetric ladder:

| ID matches | Legal name matches | Outcome |
|---|---|---|
| row A | row A | Resolve row A |
| nothing | exactly one row | Resolve the name match, and raise `DATA_QUALITY` naming the stale ID |
| row A | a different row B | Block with `DECISION_REQUIRED`. Never guess |
| anything | nothing, or several rows | Block with `DECISION_REQUIRED` |

The asymmetry is the point. A unique legal name can survive a database renumbering. An ID-only
match may instead be a **reused key now attached to another company**, which is exactly where a
silent ID win would staple a VP directive to the wrong supplier.

**A block degrades according to the rule's own severity.** An unresolved *hard* reference blocks
that rule's scope. An unresolved *shaping* reference only makes that directive inapplicable: drop
its objective term, raise `POLICY_CONFLICT`, and carry on evaluating the memo's other rules. So
replacing the named magnet suppliers cannot disable the rolling cap and the 20% clause, which
resolve from the component concept rather than from a supplier name.

Note the asymmetry the data already shows. The same memo's *component* references match nothing in
any database, while its *supplier* references match exactly. Suppliers resolve by ID or name.
Components must resolve by meaning.

### 15.3 Test layers

| Layer | Covers |
|---|---|
| Unit | Each preference step and rule kind, date math, MOQ rounding, `Decimal`, the reserved word, effective-date edges, the strict 35% and 50% boundaries |
| Integration | All six fixtures on temporary copies. A second run makes zero changes. A forced failure leaves both tables unchanged. An injected capacity confirmation releases named-primary with no recompile |
| Property and metamorphic | Row order changes nothing. Renaming behaves per section 15.2. Adding a disapproved cheap supplier changes no executable action |
| Adversarial | Zero suppliers, everyone off the list, past deadlines, MOQ above demand, conflicting memos, malformed dates, NaN, a zero-byte database, a scenario dated before any policy took effect |
| Differential | Exhaustive enumeration as an oracle on tiny generated cases |
| Model eval | Labelled concept and component pairs, with accuracy thresholds, so swapping the model is a measured change |

### 15.4 The held-out suite: testing what the fixtures cannot

Five of nine tables are identical across all six scenarios, so the supplied data cannot exercise the
variation that matters most. Each of these asserts a **behaviour**, never a row set.

| Case | What must happen |
|---|---|
| One small order, empty inventory | Every component with an eligible supplier and a gap gets an order. This is the direct regression guard for the forced-surplus split, which the old design failed 13 times over |
| One very large order | Lines above $50,000 withhold with a complete proposal in the alert. Lines below execute normally |
| New products, components, and suppliers added | Nothing keys off fixture identifiers. Unseen names resolve or degrade with disclosure |
| Suppliers renumbered, legal names unchanged | Memo-named entities resolve by name with `DATA_QUALITY`, and magnet planning proceeds |
| Magnet suppliers replaced by different companies | Named-primary drops as inapplicable. The rolling cap and 20% clauses still bind. Magnets still procure |
| Exactly one eligible supplier under a secondary-allocation rule | No order. `DECISION_REQUIRED` plus `SOLE_SOURCE`, with the one-supplier plan costed and labelled non-executable |
| An early unmeetable deadline plus a later comfortable one | Exception quantity stays within the early shortage. Adding stock to the early bucket reduces the allowance by the same amount |
| A date before the memos, then before any policy at all | Memo rules go inactive. Then no policy is in force, which alerts loudly rather than procuring silently |
| Country written `"United States"`, `"U.S.A."`, `"Freedonia"` | Known aliases resolve. Unknown is `UNKNOWN` plus both-ways, never a silent fallback to the column |
| A part named "magnetic reed switch" | Does **not** match the neodymium-magnet concept |
| An unknown unit of measure | Treated as discrete with an alert. No crash, no guessed pack size |
| Inventory covers all demand | Zero orders plus a run-accounting alert saying so. Never silence |

### 15.5 Three intuitive test properties are false, and each failure teaches something

> **"Adding a supplier cannot increase the quantity ordered"** is false. A faster route makes
> lateness recoverable and creates a bridge order plus surplus that would not otherwise exist.
> Restate it over outcome quality: adding an eligible supplier cannot worsen on-time coverage.
>
> **"Adding inventory cannot make the outcome worse"** is false for *discretionary* recovery. More
> stock shrinks the net requirement, so the authorized amount of optional bridge inventory shrinks
> with it. It must **not**, however, make the minimum compliant order fail merely because a MOQ now
> represents a larger fraction of a smaller need. That quantity is forced, and its discretionary
> surplus is zero by definition.
>
> The properties therefore separate three claims. Physical feasibility is monotone. The minimum
> compliant order stays executable regardless of its forced-surplus ratio. Optional recovery
> quantity obeys the ratio against the current net requirement. A regression test adds inventory
> around a MOQ discontinuity and asserts that only a recovery alternative may disappear, never the
> minimum order itself.

Written the naive way, the suite fails correct behaviour. A red test on correct code usually gets
"fixed" in the code.

### 15.6 Determinism, and a staged clean-environment check

Byte-identical SQLite output is the wrong assertion, because page layout and sequence counters can
differ with no business change. The real assertions are that two runs produce identical business
rows, and that a rerun on unchanged input performs zero writes.

**A dedicated regression test** asserts that each of the six sole-eligible parts is orderable when
short. It guards two separate bugs at once: the zero-history reading, and an over-broad
certification gate, since two of the six suppliers hold no certifications at all.

**The clean-environment CI job is staged, not all-or-nothing.** Before the fallback solver exists, a
bare Python image with no `pip install` covers imports, `--help`, and loading the policy pack and a
snapshot. That already proves the optional packages are never imported on the default path. Once
the fallback lands, the same job extends to running all six scenarios with no `pip`. Demanding the
full no-pip suite before a no-pip solver exists would just block the build.

---

## 16. The optional AI parts (§13)

Three call sites exist. None is load-bearing, and all three are off by default.

| Site | What guards it | Default |
|---|---|---|
| Offline policy compilation | Schema validation, literal quote checks, covering quotes for every value, human review before approval | Compile once, ship the file |
| Entity resolution leftovers | Schema validation, caching, evidence trace, both-ways evaluation | **Off** |
| Rationale polish | Every number must appear in the decision record, and required caveats must survive | **Off**, templates instead |

The polish guard stops fabricated quantities from reaching an order. It does not catch a reversed
meaning or a dropped caveat, which is exactly why templates are the default.

`--llm=off` is the default, so the required command makes no network call. `--llm=auto` opts in and
falls back silently to the synonym table if no server answers. **The agent never fails because a
model server is unavailable.** The client targets an OpenAI-compatible endpoint, which is the
de-facto serving standard for open models, so swapping models is a config change.

---

## 17. Running it (§14, §15)

**Exit codes:** `0` committed, `2` CLI or path error, `3` invalid scenario data, `4` invalid policy
pack, `5` solver or validator failure, `6` concurrent modification, `7` commit failure.

A validated `INFEASIBLE` result is exit code 0. It is a successful business outcome, not a technical
failure.

**Flags,** all optional so the required command works as written:
`--contract={benchmark,production}`, `--llm={off,auto,required}`, `--recompile-policy`, `--dry-run`,
`--explain <component_id>`, `--strict`, `--alert-prefixes`, `--json`.

### 17.1 Dependencies are minimised on purpose

An `ImportError` is a zero on every held-out scenario at once. That is a larger risk than any
modelling error in the design, because the graders unzip a file and run one command.

| Package | Where it may be imported | Why |
|---|---|---|
| `sqlite3`, `decimal`, `json`, `dataclasses` | Default path | Standard library |
| `scipy` (HiGHS) | Default path, **guarded**. Falls back to the bounded stdlib solver if absent | The only third-party package the numeric path can want |
| `pypdf` | Offline policy compilation only. **Never at run time** | The compiled pack is checked in |
| `httpx` | Lazily, inside the model adapter, only when `--llm` is not `off` | Default is off |
| `jsonschema`, `pyyaml` | Development and CI only | The shipped pack is JSON. Schema validation is a build-time check |
| `pydantic` | Not used in the core | Replaced by `dataclasses` plus explicit validators |
| `pytest`, `hypothesis` | Tests only | — |

The policy pack is located **relative to `agent.py`**, never the working directory. A test runs the
agent from `/` to prove it.

### 17.2 Security and logging

Only the explicit scenario path is opened. SQL uses bound parameters and no interpolated
identifiers. No SQLite extensions load. All database text and document content is treated as
untrusted data, never as instructions. No network calls happen in default mode.

JSON logs carry the run ID, input and pack hashes, the active contract, active and inactive memo
IDs, demand and supply by component, rejection reasons, solver stages, validation results, and
timings. No secrets and no full document text.

---

## 18. Build order (§16)

The deterministic path is the product. The model-backed features are an optional final workstream.

**Critical path:** `T00 → T01/T02 → T04/T05 → T06 → T07/T08 → T10`.

| Wave | Tasks | What they build |
|---|---|---|
| 0 | T00 | Frozen contracts, types, protocols, scaffold. One owner, sequential |
| 1 | T01, T02, T03 | Database loading, the compiled policy pack, the test generator |
| 2 | T04, T05, T09 | Ledgers, policy evaluation, decision records and commit |
| 3 | T06 | Candidate routes, gates, and preference traces |
| 4 | T07, T08 | The optimizer and the independent validator |
| 5 | T10 | CLI and end-to-end integration |
| 6 | T11, T12 | Operational hardening, then the optional model boundary |

**Get end to end before hardening the optimizer.** The largest risk here is not a modelling error.
It is an unfinished pipeline that cannot run at all. So T06 ships a **greedy allocator** behind the
solver interface, which gives a working path from snapshot to commit, and T07 replaces it with the
real model. The preference order and the objective stages are specified as the same ordering, so
the swap is a drop-in.

**The greedy stage is a milestone, never a shippable planner.** A heuristic that fails to find a
plan is indistinguishable from a plan that does not exist. So the greedy allocator is built
*structurally incapable* of the claims that need certainty. It may report "no compliant plan found,
unresolved." It may never assert infeasibility, claim no alternative exists, take a below-B or
sole-source exception, or support a "this relaxation is necessary" alert. With those outputs
unavailable it is a safe interim planner. With them enabled it silently converts search failure
into policy relaxation.

**Two collaboration rules matter more than the schedule.**

1. T07 and T08 must be built by **different agents with no shared implementation code**. The
   validator's whole value is its independence. Reviewed together, merged separately.
2. Nobody edits a frozen shared contract to make their branch compile. They report the smallest
   required change to the integration owner, who applies it once and rebases the affected work.

Merge in dependency order, not completion order. Run the full accumulated suite after every wave.
The literal per-task prompts and the file-ownership table are in `MERGED_PLAN.md` §16.2 to §16.4.

---

## 19. Questions for Apex, ranked (§17)

There are 31. They change what the agent may **do**, not just how it explains itself. Seven move
real numbers today.

**The seven that change plans:**

1. **Does the April memo's 50% cap apply to a rolling 12 months, or to each purchase order?** (Q18)
   The memo reduces a rolling limit, which implies the window. But its neighbouring clause is
   explicitly per-order. This one answer changes scenario 02's minimum overbuy from 92 to 142 units.
2. **What is the unit of a "secondary supplier allocation"?** (Q21) A purchase order row holds one
   supplier, so the memo cannot be read literally. Our assumption is all new orders for one
   component in one run. Three other readings are defensible and change the answer materially.
3. **What does "primary volume supplier" require, and what releases it?** (Q23) We model it as a
   share directive that shapes the plan, with deviation routed to a human. If it means an incumbent
   relationship, or a hard floor, or if MagnetPro's capacity has since been confirmed, the model
   changes and so do the generated allocations.
4. **Is the price-premium threshold the quantified domestic preference, or only an eligibility
   gate?** (Q22, Q29) We read it as the preference itself, so the separate domestic comparator is
   skipped when the premium opens the gate. The alternative makes the premium clause unable to
   affect any selection.
5. **Is an MOQ-driven overbuy a procurement decision, or the cost of doing business?** (Q25) We now
   treat it as the latter, executed and disclosed, because gating on it suppressed orders on small
   production runs entirely.
6. **Does a row above $50,000 represent a commitment, or a proposal your system of record
   approves?** (Q26, Q1) We withhold and put the complete proposal in an alert.
7. **When an early deadline opens an exception, may the resulting allocation also serve later
   demand that ordinary supply can meet?** (Q30) We confine the permission to the demand that
   justified it.

**The rest, grouped:**

- *Meaning of the data.* Is `purchase_orders` complete history, open orders, or a mixture (Q4)? Is
  any on-hand stock reserved or quarantined (Q13)?
- *Missing facts.* Where do approvals live (Q3)? What is the authoritative prior-shipment list for
  the PCB freeze (Q5)? Where is supplier capacity recorded (Q12)? What are component weights and
  shipping rates (Q10)?
- *Conventions.* Are catalog lead times calendar or business days, and why do the four existing
  orders arrive three to four days late (Q8)? What receiving buffer applies to hazmat and PCB
  inspection (Q9)? Which units are discrete, and what pack increments apply (Q11, Q31)?
- *Readings we have assumed.* Does the US-and-Canada definition override the `is_domestic` column
  (Q7, we say yes)? Does a VP memo satisfy the below-B review requirement (Q19, we say yes)? Is a
  PCB assembly a critical "PCB blank", and are the transducer and humidity sensor "sensor ICs"
  (Q20)? Is any component designated safety-critical, and where is that recorded (Q27)? Should a
  directive with an estimated duration expire on its own (Q28, we say no)? Should the memo part
  numbers be formally aliased (Q6)?
- *Business judgement we cannot infer.* What is a day of production delay worth (Q15)? How should
  orders sharing a deadline be prioritised (Q14)? What counts as a "significant" strategic shift
  (Q16)? What is your tolerance for discretionary surplus (Q24)? Which evidence contract governs
  (Q17)? When a memo directive and a customer date collide, which yields (Q2)?

---

## 20. What this system cannot do (§18)

Stated plainly, because a limitation the agent hides is worse than one it declares.

- **Rolling concentration cannot be evaluated at all.** Twelve months of history does not exist. The benchmark contract does not fill the gap with a zero. It holds the rule `UNKNOWN`, executes under a named assumption, and reports the visible ratio. Any such order may prove non-compliant against real history, and it says so on its face.
- **The optimality claim is bounded.** The answer is solver-certified to configured tolerances under an integer-scaled model, then rechecked exactly in `Decimal`. That is not a proof of global optimality over the real numbers through a floating-point solver.
- **Air-freight cost is not computable.** There are no component weights, and the $25,000 budget cannot be tracked across isolated snapshots. Usage is flagged but not costed.
- **Supplier capacity is not modelled.** The agent can allocate more to a supplier than it can ship.
- **No production lead time.** The materials-needed date is treated as a hard dock date. The hazmat buffer defaults to zero pending Q9, rather than inventing a number.
- **Total cost is approximated by unit price times quantity.** No shipping model, volume breaks, tariffs, or currency handling.
- **PCB incumbency is an inference**, and the expedite-versus-late trade cannot be decided without Q15.
- **Approval thresholds are untested against real data.** Synthetic coverage only.
- **Single-level BOM only**, per the brief. Multi-level would need recursive explosion with lead-time offsetting, which is a real change and not a flag.
- **Business days exclude weekends only.** No holiday calendar exists.
- **Model classification errors remain possible.** The guards bound the damage. They do not remove it, and a wrong resolution is a config fix rather than a code change.
- **The agent plans one run at a time.** No cross-run memory, supplier scorecards, or forecasting.
- **Several correct outcomes write no purchase order** — an unapproved high-value line, an unsatisfiable allocation rule, an unproven solve. The run-accounting alert and the no-silent-gap checks exist so these stay distinguishable from a failure. But that distinction lives in `alerts`, not in `purchase_orders`.
- **Exact fixture allocations live only in generated golden files**, accepted after the optimizer, the validator, and the enumeration oracle agree. This design records mechanisms, not a second hand-maintained source of expected rows.

---

The strongest demonstration is not one that always writes a purchase order. It is one that can show,
for every component and every deadline: what was needed, what supply existed, which rules were in
force on that date, why each supplier was eligible or rejected, what was ordered, what was
deliberately not ordered, and which missing fact prevented safe autonomous action.
