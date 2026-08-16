# Apex Procurement Agent — Technical Design

**Document:** `docs/CLAUDE_plan.md`
**Status:** Design for interim prototype (milestone check-in with Apex Head of Operations)
**Author:** Forward Deployed Engineering, Reflection AI

---

## 0. TL;DR

We build an **autonomous procurement planner** that reads a scenario SQLite snapshot plus the
company's policy corpus (PDF policy + memos) and writes `purchase_orders` and `alerts`.

The core design decision is **LLM-as-compiler, not LLM-as-executor**:

- An LLM reads the policy/memo PDFs **once** and compiles them into a versioned, cited,
  machine-checkable **Policy IR** (JSON). This artifact is cached, diffable, and human-reviewable.
- A **deterministic engine** does all arithmetic, date math, netting, eligibility filtering, and
  constrained allocation. No model is in the numeric path.
- An LLM is used again only for (a) **semantic entity resolution** — mapping policy phrases like
  "neodymium magnets" or "power supply components" onto whatever component IDs exist in *this*
  database — and (b) **narration** of rationale/alert prose, guarded by a fact-consistency check.
- An **independent compliance verifier** re-checks every proposed PO against the IR on a separate
  code path before anything is committed.

This shape is what makes the system generalize: policy knowledge lives in documents, entity
knowledge is derived per-scenario, and nothing in the code knows that `CMP-003` means magnets.

---

## 1. Problem framing and success criteria

### What the agent must do

Given `python3 agent.py --scenario <scenario.sqlite>`, with no other input and no human prompting:

1. Read the production schedule and explode it through the BOM into component demand.
2. Net demand against on-hand inventory and inbound (pre-existing) purchase orders, **time-phased**
   against each order's `materials_needed_by`.
3. For each residual requirement, pick supplier(s) that satisfy the procurement policy as amended by
   management memos, and place orders that arrive in time where physically possible.
4. Write every order to `purchase_orders` with a defensible `rationale`.
5. Write every problem, exception, assumption, and approval requirement to `alerts` in plain English.

### How we will judge ourselves

| Criterion | Test |
|---|---|
| **Correctness** | Every PO satisfies hard policy rules; arithmetic is exact; dates are exact. |
| **Generalization** | Runs unmodified on a scenario with new components, suppliers, products, dates, and a new memo. Zero hardcoded IDs (CI-enforced). |
| **Defensibility** | A procurement manager can read any `rationale` and reconstruct the decision, including which suppliers were rejected and under which policy section. |
| **Honesty** | Where the data cannot support a policy rule, the agent says so in an alert rather than silently inventing an answer. |
| **Safety** | The agent never silently violates policy. Relaxations are explicit, ranked, and alerted. |
| **Determinism** | Two runs on identical inputs produce identical output. |

### Explicit non-goals for the interim prototype

- Not a full MRP system: no multi-level BOM, no capacity planning, no production scheduling.
- Not an approval workflow: we *flag* approval requirements; we do not model sign-off state.
- Not a supplier negotiation or RFQ agent.
- We do not modify or cancel pre-existing purchase orders (see §11.4 — deliberate stance).

---

## 2. Data reconnaissance — what the provided data actually contains

This section documents what we found by inspecting the six scenarios and the four documents. Several
of these findings are load-bearing for the design.

### 2.1 Scenario structure

All six scenario databases share **byte-identical** `products`, `components`, `suppliers`, `bom`, and
`supplier_catalog` tables (verified by checksum). Only four tables vary:

| Table | Varies? |
|---|---|
| `scenario_config` | ✅ `current_date` is `2025-09-01` for five scenarios, `2025-10-05` for scenario 05 |
| `inventory` | ✅ scenario 04 is a ~90% drawdown of the common baseline |
| `production_schedule` | ✅ 1–5 orders, differing quantities and deadlines |
| `purchase_orders` | ✅ empty except scenario 02 (4 pre-existing rows) |

> **This is the single biggest overfitting hazard.** The provided scenarios exercise only demand,
> inventory, dates, and inbound supply. The master data (19 components, 13 suppliers, 4 products,
> 44 catalog rows) never changes. Held-out scenarios will almost certainly vary it. Any logic that
> implicitly assumes this catalog — a hardcoded critical-component list, an assumption that every
> component has ≥2 suppliers, that every component has an inventory row, that PCBs are `CMP-005` —
> will pass all six local scenarios and fail the held-out set. §10 is entirely about defending
> against this.

### 2.2 Landmines found in the data

**`current_date` is a SQLite reserved word.** `SELECT current_date FROM scenario_config` silently
returns *today's system date*, not the column. We hit this during reconnaissance and it produced
completely wrong feasibility results. All access must be `SELECT "current_date" FROM scenario_config`.
This will be a unit test.

**Memos reference identifiers that do not exist in the databases.** The memos speak of `RM-3003`
(neodymium magnets), `RM-3005` (PCB components), and `MFG-5030`; suppliers `SUP-107`/`SUP-108` *do*
match. The databases use `CMP-003` and `CMP-005`. The document corpus and the transactional data are
from different ID namespaces. **Therefore the agent cannot key policy rules off IDs.** It must
resolve policy concepts to components semantically (name/category/description). This is not an
inconvenience — it is a direct instruction from the data about how the real system has to work.

**The policy's critical-component list is expressed as English categories**, not IDs:
"microcontroller ICs, power MOSFETs, PCB blanks, neodymium magnets, and all sensor ICs (temperature,
pressure, humidity)". Note "PCB blanks" vs. the database's "PCB Assembly (6-layer)" — near-miss
naming that a naive string match would drop.

**`suppliers.is_domestic` contradicts the policy.** Policy §3 defines domestic as "United States and
Canada". `SUP-110 EcoBoard Solutions` is `country='Canada'` with `is_domestic=0`. The two disagree.
We treat the policy text as authoritative, derive domesticity from `country`, and raise a
data-quality alert whenever the derived value disagrees with the flag.

**`components.requires_certification` is sparse.** Only `CMP-005` carries `ISO-9001`. Policy §2.1
imposes ISO-9001 on all electronic components/PCBs/safety-critical parts and *additionally* UL
listing on power-supply components (capacitor banks, transformer cores). The DB field is a hint, not
the requirement set — requirements are the **union** of DB-declared and policy-derived.

**The UL rule is binding and eliminates a supplier.** Only `SUP-101` and `SUP-112` hold UL-Listed.
`SUP-112` does not stock `CMP-017` (Capacitor Bank) or `CMP-018` (Transformer Core). So **`SUP-101`
is the sole eligible supplier for both**, even though `SUP-106` is cheaper on lead time and appears
in the catalog. Any agent that ignores §2.1 will produce a plan that looks better and is wrong.

**The PCB memo asks a question the data cannot answer.** MEMO-2025-085 permits only suppliers "from
whom we have previously received and accepted PCB shipments". There is no receipts table, no
shipment history, and in five of six scenarios `purchase_orders` is empty. A literal reading makes
PCBs unorderable everywhere. See §7.3 for the inference rule we adopt and the alert we raise.

**Memo effective windows matter and are already exercised.** MEMO-2025-072 authorises air freight
(international lead time −14 days, floor 7) **only from 2025-07-01 through 2025-09-30**. Scenario 05
runs at `2025-10-05` — *outside* the window. Five scenarios are inside it. Every rule must therefore
carry an effective window evaluated against `scenario_config.current_date`, not against wall-clock
time. Held-out scenarios dated before 2025-08-20 would also switch off the PCB memo.

**Pre-existing POs already violate a memo.** In scenario 02, `EXIST-003` (100 units from `SUP-107`)
and `EXIST-004` (50 from `SUP-108`) put `SUP-107` at **66.7%** of magnet volume against the memo's
**50%** cap. The agent must notice this, because the memo says "update all open and future purchase
orders", and must decide what to do about someone else's commitments.

**Fractional demand with discrete units of measure.** `CMP-011` (Conformal Coating, UoM `can`) has
BOM quantities of 0.25 and 0.5 per unit. Scenario 01 demand is exactly **15.25 cans** against 5 on
hand → 10.25 short → must round **up** to 11 cans, then apply MOQ. Money and quantity math will use
`Decimal`, not `float`.

**No purchase order in the provided data comes close to an approval threshold.** Largest possible
single line is ≈ $6,050 (scenario 05, `CMP-008`); largest whole-run spend is ≈ $22,300. The §7
thresholds ($50k / $150k) and the §7.1 emergency ceiling ($75k) are **never exercised by the given
scenarios**. We implement them anyway and cover them with synthetic tests, and we say so plainly
rather than pretending they were validated.

### 2.3 Structural feasibility facts (used as design fixtures)

Computed from the data; these become golden-test expectations.

| Scenario | Notable fact |
|---|---|
| 01 baseline | `CMP-003` demand 328 vs 120 on hand. `FG-1001` needs 200 by 2025-09-12; fastest magnet source `SUP-108` @14d arrives 09-15. **80 units are unavoidably late.** |
| 02 partial | Inbound POs (150 magnets) cover the 09-12 bucket. Residual 58 units. Concentration + MOQ interact and are jointly infeasible (see §8.4). |
| 03 tight | `PO-5005` (FG-1003 ×50, due 09-10) drives `CMP-005` short 60, `CMP-017` short 20, `CMP-018` short 30. `SUP-101` @10d arrives 09-11 → 1 day late on all three. The one on-time option for `CMP-017` (`SUP-106` @5d) **lacks UL listing**. Deliberate policy-vs-deadline collision. |
| 04 low inventory | Every component short; exercises breadth, MOQ rounding, and total-spend reporting. |
| 05 competing demand | Dated 2025-10-05 → **air freight expired**. `CMP-003` short 440 due 11-01; `SUP-107` @35d arrives 11-09 (late), so the only on-time source is `SUP-108` → 100% concentration vs. a 50% cap. Hard conflict between a memo and a customer commitment. |
| 06 simple | Single order, comfortable timeline, only `CMP-014` (20) and `CMP-016` (15) short. The smoke test. |

---

## 3. Architecture

```
                    ┌─────────────────────────────────────────────┐
                    │  agent.py --scenario X.sqlite [--llm=...]   │
                    └──────────────────┬──────────────────────────┘
                                       │
   ┌───────────────────────────────────┼────────────────────────────────────┐
   │                                   │                                    │
┌──▼───────────────┐        ┌──────────▼──────────┐            ┌────────────▼────────────┐
│ ScenarioRepo     │        │ DocumentCorpus      │            │ Config / CLI flags      │
│ (SQLite, RO)     │        │ policy + memos PDF  │            │ thresholds, toggles     │
└──┬───────────────┘        └──────────┬──────────┘            └────────────┬────────────┘
   │                                   │                                    │
   │                        ┌──────────▼──────────┐  LLM #1                 │
   │                        │ PolicyCompiler      │◄────────────────────────┤
   │                        │  → Policy IR (JSON) │  cached by doc hash     │
   │                        │  + provenance       │  fallback: checked-in   │
   │                        └──────────┬──────────┘                         │
   │                                   │                                    │
   │                        ┌──────────▼──────────┐  LLM #2                 │
   ├───────────────────────►│ EntityResolver      │◄────────────────────────┤
   │                        │ concept → IDs in    │  cached by (concept,    │
   │                        │ *this* scenario     │  component, doc hash)   │
   │                        └──────────┬──────────┘                         │
   │                                   │                                    │
┌──▼───────────────────────────────────▼────────────────────────────────────▼──┐
│                       DETERMINISTIC PLANNING CORE (no LLM)                   │
│                                                                              │
│  RequirementsEngine  →  SourcingEngine  →  Allocator  →  ApprovalEngine      │
│  BOM explosion,         eligibility        MOQ +          thresholds,        │
│  time-phased netting    filters +          concentration  emergency,         │
│  (EDD allocation)       comparator chain   + splits       documentation      │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │  DecisionRecord[] (fully structured)
                     ┌──────────────▼──────────────┐
                     │ ComplianceVerifier          │  independent re-check
                     │ (separate code path)        │  of every hard rule
                     └──────────────┬──────────────┘
                                    │
                     ┌──────────────▼──────────────┐  LLM #3 (optional)
                     │ Narrator                    │  template → LLM polish
                     │ rationale + alert prose     │  → numeric consistency
                     └──────────────┬──────────────┘     check → fallback
                                    │
                     ┌──────────────▼──────────────┐
                     │ Writer (single transaction) │
                     │ purchase_orders + alerts    │
                     └─────────────────────────────┘
```

**Why a deterministic core rather than an LLM agent loop.** "Autonomous" in this brief means *runs
without human prompting*, not *an LLM in a while-loop calling tools*. Procurement planning is a
constraint-satisfaction problem over exact numbers and dates, with a legal-ish rule text on top.
LLMs are excellent at the rule text and poor at the arithmetic; the split above puts each where it
is strong. Concretely this buys us: reproducibility, unit-testability of every rule, sub-second
runtime, no per-run token cost that scales with catalog size, and — most importantly — the ability
to make strong invariant guarantees that hold on held-out scenarios.

We still expose the planning capabilities as callable tools (`get_requirements`,
`list_candidates`, `explain_rejection`, `simulate_order`) behind a thin, framework-agnostic
function-calling loop, for interactive what-if use by a planner. That path is a **read-mostly
assistant**, not the production write path.

---

## 4. Policy IR — policy as data, not as code

### 4.1 Design intent

Every behavioural rule the agent obeys is a row in a versioned JSON document, carrying its own
provenance and effective window. **No policy constant is written in Python.** Adding a memo PDF
changes agent behaviour with zero code change — that is the generalization property the held-out
scenarios will test if they ship a new memo, and it is what makes the system maintainable when Apex
issues MEMO-2025-091 next month.

### 4.2 Rule schema

```jsonc
{
  "ir_version": "1.0",
  "compiled_from": [
    {"doc_id": "POL-PROC-001", "version": "3.2", "effective": "2025-01-15", "sha256": "…"},
    {"doc_id": "MEMO-2025-041", "effective": "2025-04-15", "sha256": "…"},
    {"doc_id": "MEMO-2025-072", "effective": "2025-07-01", "expires": "2025-09-30", "sha256": "…"},
    {"doc_id": "MEMO-2025-085", "effective": "2025-08-20", "sha256": "…"}
  ],
  "concepts": [
    {
      "id": "concept.critical_component",
      "definition": "microcontroller ICs, power MOSFETs, PCB blanks, neodymium magnets, and all sensor ICs (temperature, pressure, humidity)",
      "source": {"doc_id": "POL-PROC-001", "section": "6"}
    },
    {
      "id": "concept.power_supply_component",
      "definition": "power supply components such as capacitor banks and transformer cores",
      "source": {"doc_id": "POL-PROC-001", "section": "2.1"}
    }
  ],
  "rules": [
    {
      "id": "ASL.exclude_removed",
      "kind": "supplier_eligibility",
      "severity": "hard",
      "effect": "exclude",
      "scope": {"components": "*"},
      "condition": {"field": "supplier.on_approved_list", "op": "eq", "value": 0},
      "effective_from": "2025-01-15", "effective_to": null,
      "message": "Supplier is not on the Approved Supplier List (Policy §2).",
      "source": {"doc_id": "POL-PROC-001", "section": "2",
                 "quote": "Suppliers that have been removed from the ASL may not be used under any circumstances"}
    },
    {
      "id": "CERT.ul_for_power_supply",
      "kind": "supplier_eligibility",
      "severity": "hard",
      "effect": "require_certification",
      "scope": {"concept": "concept.power_supply_component"},
      "params": {"certifications": ["ISO-9001", "UL-Listed"]},
      "effective_from": "2025-01-15",
      "source": {"doc_id": "POL-PROC-001", "section": "2.1", "quote": "must additionally hold UL listing certification"}
    },
    {
      "id": "CONC.critical_default",
      "kind": "allocation_constraint",
      "severity": "shaping", "relaxation_rank": 40,
      "effect": "max_share",
      "scope": {"concept": "concept.critical_component"},
      "params": {"max_supplier_share": 0.70, "window_months": 12},
      "effective_from": "2025-01-15",
      "source": {"doc_id": "POL-PROC-001", "section": "4"}
    },
    {
      "id": "CONC.neodymium_memo",
      "kind": "allocation_constraint",
      "severity": "shaping", "relaxation_rank": 40,
      "effect": "max_share",
      "scope": {"concept": "concept.neodymium_magnet"},
      "params": {"max_supplier_share": 0.50, "min_secondary_share": 0.20, "window_months": 12},
      "supersedes": ["CONC.critical_default"],
      "effective_from": "2025-04-15",
      "source": {"doc_id": "MEMO-2025-041",
                 "quote": "reduced from 70% to 50% … This supersedes the general critical component concentration limit stated in Procurement Policy Section 4"}
    },
    {
      "id": "LEAD.air_freight_intl",
      "kind": "lead_time_modifier",
      "severity": "enabling",
      "scope": {"suppliers": {"domestic": false}},
      "params": {"delta_days": -14, "floor_days": 7,
                 "requires": ["confirmed_production_demand"],
                 "excludes": ["safety_stock", "speculative"],
                 "per_request_approval": "Procurement Manager",
                 "period_budget_usd": 25000, "cost_per_kg_usd": [8, 12]},
      "effective_from": "2025-07-01", "effective_to": "2025-09-30",
      "source": {"doc_id": "MEMO-2025-072"}
    },
    {
      "id": "SUP.pcb_incumbent_only",
      "kind": "supplier_eligibility",
      "severity": "hard",
      "effect": "restrict_to_incumbents",
      "scope": {"concept": "concept.pcb_component"},
      "params": {"evidence": ["prior_purchase_order", "established_relationship"],
                 "also_requires_document": "Certificate of Conformance with cross-section analysis"},
      "effective_from": "2025-08-20",
      "source": {"doc_id": "MEMO-2025-085"}
    }
  ]
}
```

### 4.3 Rule kinds

| Kind | Meaning | Examples |
|---|---|---|
| `supplier_eligibility` | Hard include/exclude filter on candidates | ASL, ISO-9001, UL, PCB incumbency |
| `component_classification` | Defines a concept membership test | critical, hazardous, power-supply, PCB |
| `sourcing_preference` | Ordered comparator with thresholds | domestic preference, sustainability, strategic tier |
| `allocation_constraint` | Shapes the split across suppliers | concentration cap, min secondary share, dual-source |
| `quantity_constraint` | Shapes order size | MOQ |
| `lead_time_modifier` | Adjusts effective lead time | air freight, hazmat receiving dwell |
| `approval_threshold` | Attaches a required approver | $50k PM, $150k VP, $75k emergency, air freight |
| `documentation_requirement` | Attaches required text/artifact | international justification, sole-source justification, CoC |

### 4.4 Severity and precedence

Each rule has a **severity**:

- `hard` — never violated. If it makes a requirement unsatisfiable, we place no order and alert.
- `shaping` — a constraint we satisfy if we can; relaxable in ranked order with an explicit alert.
- `preference` — a tie-breaker; never blocks.
- `enabling` — grants an option (air freight, international sourcing) rather than restricting.

**Memo-over-policy precedence** is resolved by, in order: (1) an explicit `supersedes` link that the
compiler extracts from language like "This supersedes…"; (2) later `effective_from`; (3) narrower
scope beats broader scope. Two active rules of the same kind and overlapping scope with no
supersession link is a **compile-time error** surfaced to the operator, not something we silently
average.

### 4.5 Compilation and caching

- Input: PDF text + a compact schema description + few-shot examples of well-formed rules.
- Output: schema-validated JSON. Every rule **must** carry a verbatim `source.quote` that is checked
  to be a literal substring of the source document — a cheap, effective hallucination guard.
- Cached at `apex/policy/compiled/policy_ir.json`, keyed by the SHA-256 of the concatenated document
  set. The compiled artifact is **committed to the repo**, so `--llm=off` is fully functional and
  the default `agent.py --scenario X` run needs no model server at all.
- A `--recompile-policy` flag regenerates it. The diff is the review artifact a compliance officer
  would actually sign off on.

---

## 5. Entity resolution — bridging documents and database

This is the layer that makes the agent survive a held-out scenario with a different catalog.

### 5.1 The problem

Policy says "power MOSFETs are critical". The database says
`CMP-007 | Power MOSFET (100V) | Electronic Component`. Memo says "neodymium magnets (RM-3003)".
The database says `CMP-003 | Neodymium Magnets (N52)`. A held-out scenario might say
`PART-88 | NdFeB Magnet, grade N52, axial`. All three must resolve to the same concept.

### 5.2 Three-tier resolution, cheapest first

1. **Structured signals.** `components.category`, `components.requires_certification`,
   `components.is_hazardous`, `suppliers.certifications`, `suppliers.country`. Free and exact where
   available.
2. **Deterministic lexical matcher.** Normalised token/stem matching against a **synonym table in
   config** (`concepts.yaml`), not in code: e.g. `neodymium_magnet: [neodymium, ndfeb, n52, rare earth magnet]`,
   `pcb_component: [pcb, printed circuit board, circuit board, board blank]`,
   `power_supply_component: [capacitor bank, transformer core, power supply]`. Covers the provided
   data with no model call.
3. **LLM classifier for residuals only.** For components neither matched nor confidently excluded,
   ask: given this concept definition (with the policy quote) and this component row
   (`id, name, description, category, uom`), does it belong? Batched, JSON-schema-constrained,
   returns `{member: bool, confidence: float, reason: str}`. Cached by
   `(concept_id, component_fingerprint, doc_hash)` so cost is one-time per catalog, not per run.

### 5.3 Fail-safe direction

Ambiguity resolves toward the **stricter** outcome, which depends on the concept's polarity:

- **Restrictive concepts** (critical, hazardous, certification-required, power-supply): unresolved →
  **treated as a member** (apply the stricter rule) + `ASSUMPTION` alert.
- **Permissive concepts** (e.g. "supplier from whom we have previously received PCBs"): unresolved →
  **treated as a non-member** (deny) + `ASSUMPTION` alert.

Every low-confidence resolution (`< 0.75`) writes an alert naming the component, the concept, the
chosen fail-safe, and the resulting effect, so a planner can correct it in one pass.

### 5.4 Supplier attribute derivation

| Derived attribute | Rule |
|---|---|
| `is_domestic_effective` | `country` normalised ∈ {United States, USA, US, U.S.A., Canada} per Policy §3, **not** the `is_domestic` column |
| `certs` | Split `certifications` on `,`/`;`/`/`, upper-case, strip. Null → empty set. |
| `sustainability_grade` | Parse `A+ > A > A- > B+ > B > B- > C+ > C > …` into an ordinal so "below B" is computable |
| `is_strategic` | `relationship_tier == 'Strategic'` (case-insensitive) |

Any disagreement between `is_domestic` and the derived value emits a `DATA_QUALITY` alert
(`SUP-110` triggers this in every provided scenario).

---

## 6. Requirements engine — time-phased net requirements

### 6.1 Algorithm

```
gross[c][due_date] += order.quantity * bom.quantity_per      for each schedule order × bom row
supply_events[c]    = [(current_date, inventory.quantity_on_hand)]
                    + [(po.expected_delivery_date, po.quantity) for pre-existing POs]

for each component c:
    buckets = sorted(gross[c].items(), key=due_date)          # earliest-deadline-first
    available = 0
    for (due, qty) in buckets:
        available += sum(q for (t, q) in supply_events[c] if t <= due and not yet consumed)
        take = min(available, qty); available -= take
        net[c][due] = qty - take                              # residual requirement at this date
```

Key properties:

- **Earliest-deadline-first (EDD) allocation** of scarce inventory. This is what "competing demand"
  requires: with 120 magnets on hand and two orders needing 200 (Sep 12) and 128 (Oct 10), inventory
  goes to the earlier commitment, and the shortfalls are dated correctly.
- **Inbound POs count as supply at their `expected_delivery_date`.** Scenario 02 depends on this:
  120 on hand + 150 inbound = 270 available by Sep 12, so the Sep 12 bucket is fully covered and only
  58 units remain. If we treated pre-existing POs as noise we would over-order by 150 units.
- A pre-existing PO whose `expected_delivery_date < current_date` is treated as arriving on
  `current_date` **and** emits an `OVERDUE_INBOUND` alert. (Assumption: `inventory` does not already
  include open-PO quantities. Three of scenario 02's four POs are unambiguously future-dated, so this
  reading is well supported; it is nonetheless listed as an open question in §12.)
- **No safety stock.** We buy only against confirmed production demand. MEMO-2025-072 explicitly
  denies air freight to "speculative or safety-stock orders", which tells us safety stock is a
  concept at Apex but not one this agent is chartered to manage. Flagged as an open question.

### 6.2 Quantity discipline

- All money and quantity arithmetic in `Decimal`. No floats. (`0.25 × 25 + 0.5 × 10 + 0.5 × 8` must
  be exactly `15.25`, not `15.249999999999998`.)
- **Discrete UoM** (`each`, `can`, `tube`, and any UoM not in a configured continuous set) → net
  requirement rounds **up** to the next whole unit.
- **Continuous UoM** (`kg`, `meter`, `liter`) → fractional quantities preserved, then rounded up to
  the nearest configured increment (default: 2 decimal places).
- MOQ is applied **after** rounding, never before.

### 6.3 Degenerate inputs (all produce an alert, never a crash)

Empty production schedule · product in schedule with no BOM rows · component with no inventory row
(treated as 0) · component with no `supplier_catalog` rows · `materials_needed_by` in the past ·
non-positive quantities · duplicate BOM rows · unknown `product_id` / `component_id` /`supplier_id`
foreign keys · null `unit_price` or `lead_time_days` in the catalog · `materials_needed_by` null.

---

## 7. Sourcing engine — eligibility then preference

For each `(component, due-date bucket)` the engine builds a candidate list and reduces it in a fixed,
documented order. Every rejection is recorded with its rule ID so the rationale can explain *why the
losers lost*, which is what makes the output defensible.

### 7.1 Stage 1 — hard eligibility filters (never relaxed)

| Filter | Rule | Effect in provided data |
|---|---|---|
| Approved Supplier List | §2 | Removes `SUP-113` from all PCB sourcing |
| ISO-9001 for electronics / PCB / safety-critical | §2.1 | No effect here (all electronics suppliers hold it) but binding in general |
| UL-Listed for power-supply components | §2.1 | **Removes `SUP-106` from `CMP-017`, and `SUP-106`/`SUP-104` from `CMP-018` → `SUP-101` is sole-source for both** |
| PCB incumbency | MEMO-2025-085 | Removes `SUP-110` (new, Canada) and `SUP-103` from PCB sourcing → `SUP-101` sole-source |
| Catalog existence | — | Supplier must actually list the component |

Sole-source outcomes additionally trip Policy §4's "maintain at least two qualified suppliers for
every critical component" and emit a `SOLE_SOURCE` alert — the agent should tell Apex that its own
rules have painted it into a corner on PCBs, capacitor banks and transformer cores.

### 7.2 Stage 2 — feasibility

```
use_air = air_rule_active(order_date)          # MEMO-2025-072 window
        ∧ supplier.is_international
        ∧ demand_is_confirmed_production        # never for safety stock / speculative
        ∧ standard_lead_would_miss(bucket.due_date)   # "where ocean freight would cause delays"

effective_lead_days = catalog.lead_time_days
                    + (air_freight_delta if use_air else 0)
                    + hazmat_receiving_buffer_days   (config, default 0, alerted)
effective_lead_days = max(effective_lead_days, air_freight_floor) if use_air else effective_lead_days
expected_delivery   = order_date + effective_lead_days      # calendar days
feasible            = expected_delivery <= bucket.due_date
```

`order_date` is always `scenario_config."current_date"`. Policy §10 requires
`expected_delivery_date` to reflect the quoted lead time from the order date — this formula *is* that
rule, and it is verified independently in §9.

### 7.3 The PCB incumbency inference (a documented judgement call)

The memo requires prior accepted shipments; no such record exists. Our evidence ladder, strictest
first:

1. **Explicit**: a row in `purchase_orders` for that `(component, supplier)` pair → incumbent.
   (Scenario 02's `EXIST-002` establishes `SUP-101` this way.)
2. **Relationship inference**: `relationship_tier ∈ {Strategic, Preferred}` **and** the supplier lists
   the component in `supplier_catalog` → treated as incumbent. Rationale: Apex's own data model uses
   tier to encode an established trading relationship; `SUP-101`'s note reads "Primary electronics
   supplier since 2018".
3. Otherwise → **not** an incumbent (fail-safe deny).

Under this rule `SUP-101` is the only eligible PCB supplier in every provided scenario. We emit an
`ASSUMPTION` alert on every run stating the inference and asking for a receipts/GRN table. We reject
the purely literal reading because it makes PCBs unorderable in five of six scenarios, which cannot
be the memo's intent.

We also attach the CoC requirement to every PCB purchase order rationale and raise a
`DOCUMENTATION_REQUIRED` alert, since we cannot enforce it at receiving.

### 7.4 Stage 3 — the domestic gate (Policy §3)

International suppliers are *permitted* — not mandated — when **any** of:

- (a) no domestic supplier can meet the bucket's due date, **or**
- (b) `(best_domestic_price − best_international_price) / best_international_price > threshold`,
  where threshold = **50%** for critical components, **35%** otherwise, **or**
- (c) no domestic source exists for the component.

Strict inequality matters: `CMP-007` sits at exactly 50.0% ($2.25 vs $1.50) and therefore stays
domestic. `CMP-006` at 25.9% stays domestic. `CMP-003` (78%), `CMP-005` (60%), `CMP-013` (61%) and
`CMP-015` (69%) unlock the international option.

**Unlocking is not choosing.** The default remains domestic; international is then simply admitted to
the comparator chain. When the international option would save materially (configurable: `> 15%`
**and** `> $2,500` on the line) we emit an advisory `COST_OPPORTUNITY` alert instead of silently
switching. With the provided data these thresholds correctly suppress noise — e.g. moving `CMP-013`
to `SUP-103` saves only ≈$96 on scenario 01's 35-unit shortfall while shifting volume away from a
Strategic supplier.

Any PO that does go international carries the §3 justification clause verbatim in its rationale
(which of (a)/(b)/(c) applied, with the computed premium).

### 7.5 Stage 4 — comparator chain (not a weighted score)

Candidates are ordered by a **lexicographic chain of comparators**, each traceable to a policy
section:

| # | Comparator | Source |
|---|---|---|
| 1 | On-time feasibility (feasible ≻ infeasible) | §10, customer commitment |
| 2 | Domestic ≻ international, unless the §3 gate is open | §3 |
| 3 | Strategic-tier retention: an incumbent Strategic supplier wins unless the alternative saves **>15%** | §9 |
| 4 | Sustainability: when prices are within **10%** and delivery within **5 business days**, prefer rating ≥ A; a supplier rated **below B** loses to any alternative | §8 |
| 5 | Total landed cost (unit price × qty, plus air-freight surcharge where quantifiable) | §7 |
| 6 | Shorter lead time (schedule risk buffer) | §10 |
| 7 | Stable tie-break: `supplier_id` ascending (guarantees determinism) | — |

> **Why not a weighted score?** A weighted objective would need weights we have no data to fit, would
> produce decisions no one can explain ("it scored 0.71"), and would make policy changes untestable.
> The policy document itself is written as thresholded tie-breakers — §8 and §9 literally specify
> "within 10%", "within 5 business days", "up to 15%". Encoding it as a comparator chain means each
> policy sentence maps to exactly one comparator, each is unit-testable in isolation, and the
> rationale text can name the comparator that decided the outcome.

Business-day arithmetic (§8, §7.1) counts Mon–Fri and excludes no holidays — we have no holiday
calendar. Documented assumption; catalog `lead_time_days` are treated as **calendar** days.

---

## 8. Allocation — MOQ, concentration, and splits

### 8.1 Why this is a joint optimisation, not a greedy per-bucket choice

Scenario 01, `CMP-003`: total residual 208 units across two buckets (80 due Sep 12, 128 due Oct 10).
Suppliers: `SUP-108` (domestic, 14d, MOQ 50, $5.80) and `SUP-107` (international, 35d ocean, MOQ 100,
$3.25). The memo caps each at **50%** (=104 units) and requires a secondary allocation ≥20%.

A greedy per-bucket solver assigns the urgent 80 to `SUP-108` (fastest), then the 128 to whoever wins
the second bucket — landing on 61.5% for one supplier and breaching the cap. The correct answer
requires solving both buckets together:

```
SUP-108: 104 units  (80 for the Sep-12 bucket → arrives Sep 15; 24 for Oct 10)  50.0%  MOQ  50 ✓
SUP-107: 104 units  (all for the Oct-10 bucket → arrives Oct 06, ocean freight)  50.0%  MOQ 100 ✓
```

Both caps met, both MOQs met, secondary share 50% ≥ 20%, and the only lateness (80 units, 3 days) is
physically unavoidable and alerted.

Note that air freight is **not** used here even though the authorisation is active on 2025-09-01:
`SUP-107`'s standard 35-day ocean lead time arrives 2025-10-06, four days inside the Oct-10 date, and
MEMO-2025-072 authorises air only "where ocean freight lead times would cause production delays".
The lead-time modifier is therefore evaluated per requirement, not per supplier — it is applied only
when it changes feasibility, which also conserves the memo's $25,000 budget and avoids a needless
Procurement Manager approval.

### 8.2 Formulation

Decision variables `x[s][b]` = quantity of component `c` sourced from supplier `s` for bucket `b`.

```
minimise  lexicographically:
    1. Σ  x[s][b] · max(0, arrival(s) − due(b))        # total unit-late-days
    2. Σ  x[s][b] · price(s)  (+ air surcharge)         # landed cost
    3. Σ  x[s][b] · preference_penalty(s)               # from the §7.5 comparator chain
subject to:
    Σ_s x[s][b]                     ≥ net[c][b]                      (demand coverage)
    Σ_b x[s][b]  ∈  {0} ∪ [MOQ(s), ∞)                                (MOQ, semi-continuous)
    Σ_b x[s][b]  ≤  cap_share · (existing_volume + Σ new)             (concentration)
    secondary_share                 ≥ min_secondary_share             (memo)
    x[s][b] = 0                                        for ineligible s (§7.1)
```

**Solver strategy.** Instances are tiny — the provided data never exceeds 4 suppliers × 5 buckets.
v1 ships a **greedy + repair loop**: seed with the comparator-chain ordering, then repair MOQ and
concentration violations by shifting quantity, with a bounded number of repair passes; if it cannot
reach feasibility it invokes the relaxation ladder (§8.3) rather than returning a violating plan.
v2 adds **exact enumeration** for instances under a size threshold (≤6 suppliers, ≤8 buckets), which
covers essentially all real cases. We deliberately avoid an external MILP dependency; if Apex's real
catalogs turn out to be much larger, `pulp` or OR-Tools drops in behind the same interface.

Whatever the solver, the **verifier in §9 re-checks the result**, so a solver bug becomes an alert,
not a bad purchase order.

### 8.3 The relaxation ladder

When no assignment satisfies every constraint, we relax in this **fixed, published order**, emitting
a `CONSTRAINT_RELAXED` alert at each step naming the rule, the shortfall, and the compliant
alternative we declined:

| Rank | Relaxed | Never relaxed |
|---|---|---|
| 10 | Sustainability preference (§8) | ASL membership (§2) |
| 20 | Strategic-volume retention (§9) | Certification requirements (§2.1) |
| 30 | Domestic preference (§3) | MOQ (§4.1 — we round up instead) |
| 40 | Concentration cap / secondary share (§4, MEMO-041) | Ordering from a supplier that does not stock the part |
| 50 | On-time delivery (order anyway, late) | — |
| 60 | Coverage (order nothing; pure alert) | — |

**The rank-40-before-rank-50 choice is the most consequential judgement in this design and it is a
question for the customer** (§12, Q1). Our default says: a concentration cap is a risk-management
preference that Apex's own §4 already contemplates overriding with documented justification, whereas
a missed customer commitment is an external breach. So we prefer to breach the cap, place the on-time
order, flag it as requiring VP of Operations approval, **and state the compliant alternative
explicitly in the alert** so the Head of Operations can reverse the call in one instruction.

Worked example — scenario 05, `CMP-003`, 440 units due 2025-11-01, air freight expired:

> `SUP-107` at 35 days arrives 2025-11-09, eight days late. `SUP-108` at 14 days arrives 2025-10-19.
> The only on-time plan is 440/440 from `SUP-108` = 100% concentration vs. a 50% cap.
> **Agent action:** place 440 with `SUP-108`; alert:
> *"Ordered 440 units of CMP-003 (Neodymium Magnets N52) from SUP-108 MagnetPro Inc., which puts
> MagnetPro at 100% of magnet volume against the 50% cap set by MEMO-2025-041. This was done to hold
> the 2025-11-01 material dates for PO-5001 (Hartfield Industries) and PO-5002 (Atlas Robotics). The
> compliant alternative — 220 units from SUP-107 Nanjing Rare Earth — would arrive 2025-11-09, eight
> days late. Requires VP of Operations approval. Note that air freight authorisation MEMO-2025-072
> expired 2025-09-30; renewing it would bring Nanjing to 21 days (arrival 2025-10-26) and make a
> compliant 50/50 split feasible."*

That last sentence is the kind of output that earns the agent its seat: it identifies the lever that
dissolves the conflict.

### 8.4 Worked example — MOQ × concentration are jointly infeasible (scenario 02)

Residual magnet requirement is 58 units. Existing commitments are `SUP-107` 100 and `SUP-108` 50.

- Allocate all 58 to `SUP-108` → totals 100 / 108 of 208 → `SUP-108` at **51.9%**, a **1.9 pp**
  breach.
- Allocate any to `SUP-107` → its MOQ is 100, forcing a 100-unit purchase → totals 200 / 108 of 308
  → `SUP-107` at **64.9%**, far worse, plus 100 units of unneeded inventory.
- Buy only 50 from `SUP-108` (exactly 50%) → leaves an 8-unit shortfall against confirmed demand.

There is no compliant plan. The agent takes option 1, emits `CONSTRAINT_RELAXED` with these three
options and their numbers, and separately emits `PRE_EXISTING_VIOLATION`:

> *"Pre-existing purchase orders EXIST-003 and EXIST-004 already place SUP-107 at 66.7% of neodymium
> magnet volume, above the 50% cap set by MEMO-2025-041 (which directs that open orders be updated).
> This agent does not modify orders it did not place. Recommend re-balancing EXIST-003 with the
> supplier."*

### 8.5 Approvals, documentation, and emergency classification

Attached to each PO as structured flags and rendered into the rationale:

| Trigger | Flag |
|---|---|
| line total > $50,000 | Procurement Manager approval (§7) |
| line total > $150,000 | VP of Operations approval (§7) |
| emergency (prevents production stoppage) and ≤ $75,000 | Threshold bypass, retroactive approval within 5 business days (§7.1) |
| air freight used | Per-request Procurement Manager approval (MEMO-2025-072) |
| significant volume shift away from a Strategic supplier | VP of Operations approval (§9) |
| international sourcing | §3 justification clause (which condition, computed premium) |
| sole-source | §4 Sole Source Justification |
| hazardous component | §5 receiving/handling flag, Hazmat Storage, FIFO |
| PCB component | CoC with cross-section analysis required at receiving (MEMO-2025-085) |

**Emergency** is defined as: the requirement's bucket cannot be covered by any eligible supplier's
standard lead time, i.e. failure to order now stops production. This is computed, not guessed.

As noted in §2.2, no line in the provided scenarios exceeds ~$6k, so the dollar thresholds are
covered only by synthetic tests. Stated plainly rather than implied.

---

## 9. Compliance verifier — the guardrail

An **independently implemented** pass that takes the final `DecisionRecord[]` plus the Policy IR and
re-derives compliance from scratch. It shares no code with the planner (different module, different
author-intent, reviewed as an adversarial check). For an agent with write authority over purchase
orders, "the planner says it's fine" is not sufficient assurance.

Checks:

- Every PO's supplier is on the ASL and holds every required certification for that component.
- Every PO's supplier actually lists that component in `supplier_catalog`, at the quoted `unit_price`.
- `expected_delivery_date == order_date + effective_lead_time`, with the air-freight modifier applied
  only if the rule was active on `order_date` and the supplier is international.
- `quantity >= MOQ`, and quantity is whole for discrete UoM.
- Concentration per component ≤ the applicable cap, **or** a matching `CONSTRAINT_RELAXED` alert
  exists.
- Total ordered + on-hand + inbound ≥ demand for every component with at least one eligible supplier,
  **or** a matching shortfall alert exists.
- Every unmet or late requirement has ≥1 alert referencing the production order and the date.
- `po_number` is unique within the database; foreign keys resolve; `rationale` is non-empty and cites
  ≥1 policy source.
- No duplicate `(component, supplier, bucket)` line unless an intentional split is recorded.

A `hard` violation **blocks the write** for that line and converts it into an alert. Any verifier
failure sets a non-zero exit code under `--strict` (used in CI).

---

## 10. Generalization strategy

The provided scenarios vary only four tables (§2.1). Everything below exists to stop us fitting to
the other five.

### 10.1 Enforced coding rules

1. **No hardcoded identifiers.** A CI test greps the source tree for
   `\b(CMP|SUP|FG|RM|MFG|PO)-\d+\b` outside `tests/` and `fixtures/` and **fails the build**. This is
   the single most effective anti-overfit control available and it is trivially demonstrable to the
   customer.
2. **No hardcoded policy constants.** A second grep bans bare thresholds (`0.70`, `50000`, `35`,
   `14`) outside `policy/compiled/` and config. All of them must come from the IR.
3. **No assumptions about cardinality or coverage.** Not every component has an inventory row, a
   catalog row, or two suppliers; not every product in the schedule has a BOM; quantities may be
   fractional; dates may be any year.
4. **Read columns by name, tolerate extras.** Unknown extra columns are ignored. Missing *optional*
   columns (`notes`, `sustainability_rating`, `relationship_tier`) degrade to null-safe defaults with
   a `DATA_QUALITY` alert. Missing *required* columns fail fast with a precise message.
5. **Quote reserved words** in all SQL (`"current_date"`), enforced by a test.
6. **All temporal logic reads `scenario_config."current_date"`.** `datetime.now()` is banned outside
   logging — enforced by grep test. Otherwise the agent's behaviour would change based on when the
   grader runs it.

### 10.2 Behavioural generalization

- **Policy lives in documents.** Test: drop a synthetic memo into `data/memos/` that changes the
  magnet cap from 50% to 30%, re-run with `--recompile-policy`, assert the allocation changes and no
  code was touched.
- **Concepts, not IDs.** Test: rename `CMP-003` to `PART-X99` and its name to "NdFeB Magnet N52
  axial", assert identical decisions.
- **Temporal gating.** Test: same database at `2025-06-15` (before air freight), `2025-08-01`
  (before PCB memo), `2025-10-05` (after air freight expiry) → three different, individually correct
  plans. Scenario 05 already provides one of these for free.
- **Scale.** Complexity is `O(orders × bom_rows + components × suppliers)` with SQL-side aggregation;
  no per-row model calls. "Dozens of product lines" is a few thousand rows — sub-second. Entity
  resolution is cached per catalog fingerprint, so it costs once, not per run.

### 10.3 Test strategy

| Layer | Content |
|---|---|
| **Unit** | Each comparator, each rule kind, date math, MOQ rounding, `Decimal` behaviour, concentration math, business-day counting, the reserved-word query. |
| **Property (Hypothesis)** | Randomly generated scenario databases (random catalogs, suppliers, certs, dates, MOQs, inventory). Assert the invariants below on every generated case. |
| **Golden** | Committed snapshots of the full output for the six provided scenarios, human-reviewed once. These document behaviour; **they are not the specification** — invariants are. |
| **Adversarial fixtures** | Zero suppliers for a component; every supplier off the ASL; deadline in the past; MOQ larger than demand; two memos that conflict without supersession language; a component whose name matches no concept. |
| **LLM eval** | A labelled set of `(concept, component)` pairs for the resolver and a set of policy sentences → expected IR rules, run against each candidate model with accuracy thresholds, so swapping in the Reflection model is a measured change rather than a hope. |

**Invariants asserted on every generated scenario:**

1. No PO from a supplier with `on_approved_list = 0`.
2. No PO from a supplier lacking a required certification for that component.
3. Every PO quantity ≥ that supplier's MOQ, and whole for discrete UoM.
4. `expected_delivery_date == order_date + effective_lead_time` exactly.
5. Concentration ≤ cap, **or** a `CONSTRAINT_RELAXED` alert names that component.
6. For every component with ≥1 eligible supplier, ordered + on-hand + inbound ≥ demand,
   **or** an alert explains the gap.
7. Every unmet/late requirement produces ≥1 alert naming the production order and date.
8. Every PO has a non-empty rationale citing ≥1 policy source.
9. The agent never crashes; malformed input produces alerts and a clean exit code.
10. Two runs on identical copies of a database produce byte-identical `purchase_orders` and `alerts`.

---

## 11. Outputs

### 11.1 `purchase_orders`

| Column | Value |
|---|---|
| `po_number` | `AGT-{short_hash(scenario)}-{NNN}`, checked for collision against existing rows |
| `component_id`, `supplier_id`, `quantity` | From the allocation |
| `unit_price` | From `supplier_catalog` verbatim (never invented) |
| `order_date` | `scenario_config."current_date"` |
| `expected_delivery_date` | `order_date + effective_lead_time` (§7.2) |
| `rationale` | See below |

**Rationale contents** (deterministic template; optional LLM polish):

1. What and why — quantity, component name, which production orders and customers it serves, and the
   net-requirement arithmetic (demand − on hand − inbound).
2. Supplier selection — the comparator that decided it, and the material rejections with their rule
   IDs ("`SUP-106` excluded: no UL listing, Policy §2.1").
3. Policy citations — every rule applied, by section/memo ID.
4. Exceptions taken — relaxations, international justification, sole-source justification.
5. Approvals and documentation required.
6. Delivery assessment — arrives N days before/after the material date.

Example:

> Ordering 104 units of CMP-003 (Neodymium Magnets N52) from SUP-108 MagnetPro Inc. at $5.80/unit.
> Net requirement 208 units across PO-5001 (Hartfield Industries, materials needed 2025-09-12) and
> PO-5004 (Atlas Robotics, 2025-10-10): gross demand 328, on hand 120, none inbound. Split 104/104
> with SUP-107 Nanjing Rare Earth to satisfy the 50% single-supplier cap and ≥20% secondary
> allocation required by MEMO-2025-041, which supersedes Policy §4 for neodymium magnets. MOQ 50 met.
> Expected delivery 2025-09-15 at a 14-day quoted lead time (Policy §10); 80 of these units are
> required by 2025-09-12 and will arrive 3 days late — no supplier can meet that date (see alert).

### 11.2 `alerts`

Plain prose, matching the sample in the brief — self-contained, naming IDs, dates, quantities and the
policy provision. Every alert answers: *what is wrong, what did the agent do, what should a human do*.
A `--alert-prefixes` flag prepends machine-readable severity codes (`[BLOCKER]`, `[ACTION REQUIRED]`,
`[INFO]`) for downstream tooling; default off to match the requested format.

Taxonomy:

| Code | Meaning |
|---|---|
| `INFEASIBLE_DEADLINE` | No supplier can deliver by the material date; nothing ordered or ordered late |
| `LATE_ARRIVAL` | Order placed but arrives N days after the material date |
| `NO_ELIGIBLE_SUPPLIER` | All candidates filtered out by hard rules — names each rule |
| `POLICY_CONFLICT` | Two rules cannot both be satisfied (e.g. UL requirement vs. the only on-time supplier) |
| `CONSTRAINT_RELAXED` | A shaping constraint was breached; states the breach, the reason, and the compliant alternative |
| `APPROVAL_REQUIRED` | Dollar threshold, air freight, strategic shift, below-MOQ |
| `DOCUMENTATION_REQUIRED` | International/sole-source justification, CoC, hazmat receiving |
| `SOLE_SOURCE` | Only one qualified supplier for a critical component (breaches §4's dual-source rule) |
| `PRE_EXISTING_VIOLATION` | Orders the agent did not place violate current policy |
| `DATA_QUALITY` | Contradictory or missing data (`is_domestic` vs. `country`, missing catalog row, overdue inbound PO) |
| `ASSUMPTION` | A judgement call the agent made because the data could not answer (PCB incumbency, hazmat dwell time) |
| `COST_OPPORTUNITY` | A materially cheaper compliant alternative the agent declined, with the reason |
| `CAPACITY_UNKNOWN` | A memo references a limit the data cannot express (MagnetPro N52 capacity, $25k air-freight budget) |

### 11.3 Idempotency and re-runs

Re-running against the same database must not duplicate orders. Idempotency falls out of the netting
logic: agent-written POs are inbound supply on the second run, so net requirement is zero and nothing
new is ordered. Alerts are de-duplicated by exact description match against existing rows.
`--rerun={append|replace}` (default `append`) controls whether agent-authored rows
(`po_number LIKE 'AGT-%'`) are cleared first. All writes occur in a **single transaction**; any
verifier failure rolls the whole thing back.

### 11.4 What the agent will not do

It does not modify or cancel pre-existing purchase orders, even when they violate a memo
(scenario 02). Cancelling a supplier commitment is a commercial act with contractual consequences and
is outside what an autonomous planner should do unprompted. It alerts and recommends instead. This is
a stance, not an oversight, and it is Q10 for the customer.

---

## 12. Open questions for Apex

These are the items the discovery data cannot resolve. They are the substance of the check-in.

1. **Policy vs. commitment.** When a memo constraint and a customer material date collide
   (scenario 05's magnets), which yields by default? Should the agent place the non-compliant on-time
   order with a flag, or hold and escalate? Our default is the former; it is a one-line config change.
2. **Receipt history.** What is the system of record for "previously received and accepted"
   shipments (MEMO-2025-085)? Can we get a receipts/GRN table? Today we infer from relationship tier.
3. **Domestic definition.** Is `suppliers.is_domestic` authoritative or is Policy §3's "United States
   and Canada"? They disagree for `SUP-110` (Canada, flagged non-domestic).
4. **Approval scope.** Do the $50k/$150k thresholds apply per PO line, per supplier per run, or per
   run total? (Nothing in the provided data reaches them, so we cannot infer.)
5. **Air freight economics.** We need component weights to compute the $8–12/kg cost, and a place to
   track the running $25,000 authorisation budget — neither exists in the scenario schema, and a
   per-scenario snapshot cannot carry a cross-run budget.
6. **Supplier capacity.** MEMO-2025-041 says MagnetPro has "limited N52 capacity" but gives no number.
   Where does capacity live? Without it we can allocate more than a supplier can ship.
7. **Rolling 12-month volume.** Concentration limits are defined over a rolling year; the database
   holds only a handful of open POs. Is there an order-history source we should read?
8. **Meaning of `materials_needed_by`.** Dock date or production start? Is there receiving/inspection
   dwell time we should subtract — and specifically, how many days do the §5 hazmat receiving
   procedures add? We currently default to 0 and alert rather than invent a number.
9. **Over-buying tolerance.** Is it acceptable to buy excess to satisfy MOQ or a secondary-allocation
   rule (scenario 02 would need 100 unneeded magnets to comply)? Is there a max overbuy?
10. **Authority over existing POs.** Should the agent ever propose modifying or cancelling
    pre-existing orders, or only add new ones?
11. **Safety stock.** MEMO-2025-072 implies safety stock exists. Are there reorder points/min-max
    levels the agent should maintain, or is it strictly build-to-order?
12. **PO lifecycle.** `purchase_orders` has no status column. Should agent output land in a
    `pending_approval` state? Should we add a column, or write to a staging table?
13. **Lateness tolerance.** Is a 1-day slip (scenario 03's PCBs and transformer cores) an alert or a
    blocker? Is there a customer-specific tolerance?
14. **Partial fulfilment.** If we can cover 80% of an order on time, is that valuable, or is it
    all-or-nothing for the production line?

---

## 13. Honest limitations

Stated as they will be stated to the customer.

- **Concentration is computed over in-database POs plus this run**, not a true rolling 12 months. No
  order history exists in the schema.
- **Air-freight cost is not computable** (no component weights) and the $25,000 period budget is not
  trackable across scenarios (each database is an isolated snapshot). We flag air-freight usage; we
  cannot cost it.
- **Supplier capacity is not modelled** — we may allocate more to a supplier than it can produce.
- **No production lead time.** We treat `materials_needed_by` as a hard dock date with no receiving,
  inspection, or kitting dwell. Hazmat dwell defaults to 0 days pending Q8.
- **Total cost of ownership is approximated by unit price × quantity.** No shipping cost model, no
  volume price breaks, no tariffs or duties, no currency handling (USD assumed throughout).
- **The PCB incumbency rule is an inference**, not a fact. If Apex has actually bought PCBs from
  `SUP-103` before, our plan is wrong in a way only Apex can detect. This is why it is alerted on
  every run.
- **Approval thresholds are untested against real data** — nothing in the six scenarios exceeds $6k
  on a line or $23k on a run. Coverage is synthetic only.
- **Single-level BOM assumed**, per the brief. Multi-level would need recursive explosion with
  lead-time offsetting — a real change, not a config flag.
- **Business days exclude weekends only**; we have no holiday calendar.
- **LLM misclassification is possible.** Fail-safe defaults and alerts bound the damage but do not
  eliminate it. The resolver's decisions are logged and reviewable; a wrong one is a config fix in
  `concepts.yaml`, not a code change.
- **The agent optimises per run, not across runs.** It has no memory of prior planning cycles, no
  supplier scorecard learning, and no forecast.

---

## 14. LLM integration and model portability

### 14.1 Requirement

> "Structure your tech stack so that Reflection's open model can be slotted in once available…
> You may not use proprietary orchestration frameworks locked to a single model provider."

### 14.2 Design

A single narrow interface, three call sites, no framework lock-in:

```python
class LLMClient(Protocol):
    def complete(self, messages: list[Message], *, schema: dict | None = None,
                 temperature: float = 0.0, max_tokens: int = 4096,
                 seed: int | None = 0) -> str | dict: ...
```

- **Default adapter: OpenAI-compatible `/v1/chat/completions`** over plain `httpx`. This is the
  de-facto serving standard for open models — vLLM, SGLang, TGI, Ollama, llama.cpp server, LM Studio,
  Together, Fireworks, OpenRouter and Groq all speak it. Configured by three environment variables:
  `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`. **Slotting in a Reflection model is a config change.**
  If Reflection ships a different wire protocol, it is a ~40-line adapter behind the same Protocol.
- **Optional `litellm` adapter** for teams that want one config for many providers. Kept optional so
  the base install has zero provider-specific dependencies.
- **No agent framework in the write path.** The orchestration is a deterministic pipeline; there is
  nothing for a framework to orchestrate. This also means nothing to port when the model changes.
- **Structured output**: JSON-Schema-constrained decoding where the server supports it
  (`response_format` / `guided_json`); otherwise prompt-and-parse with `jsonschema` validation and up
  to 2 repair round-trips. Model output is **never** trusted unvalidated.
- **Determinism**: `temperature=0`, fixed seed where supported, plus a content-addressed on-disk cache
  keyed by `(prompt, model, params, doc_hash)`. Repeat runs are free and identical.
- **Graceful degradation**: `--llm={off|auto|required}`. `auto` (default) uses the model if reachable
  and falls back to the committed Policy IR and lexical resolver otherwise. **The agent must never
  fail because a model server is down** — a procurement planner that stops working when inference is
  unavailable is not deployable. `required` is for CI evaluation of the model path.

### 14.3 The three call sites

| Call site | Input | Output | Guard |
|---|---|---|---|
| **Policy compiler** | Policy + memo text | Policy IR JSON | Schema validation; every `source.quote` verified as a literal substring of the source document |
| **Entity resolver** | Concept definition + component/supplier row | `{member, confidence, reason}` | Schema validation; confidence floor; fail-safe direction; alert on low confidence |
| **Narrator** | A structured `DecisionRecord` | Prose rationale / alert | **Numeric-consistency check**: every number and identifier in the generated prose must appear in the decision record. On failure, fall back to the deterministic template. |

The narrator guard deserves emphasis: it means a hallucinated quantity, price or date **cannot** reach
a purchase order. The worst case is slightly stiffer prose.

---

## 15. Repository layout

```
apex-procurement-agent/
├── agent.py                       # CLI entrypoint: --scenario <path>
├── README.md
├── pyproject.toml
├── docs/
│   ├── CLAUDE_plan.md             # this document
│   └── DECISIONS.md               # running log of judgement calls
├── apex/
│   ├── cli.py                     # arg parsing, exit codes
│   ├── config.py                  # tunables + concepts.yaml loading
│   ├── concepts.yaml              # synonym table (data, not code)
│   ├── domain/                    # frozen dataclasses: Component, Supplier, CatalogEntry,
│   │                              #   Requirement, Candidate, DecisionRecord, POLine, Alert
│   ├── io/
│   │   ├── scenario_repo.py       # SQLite read + single-transaction write
│   │   └── documents.py           # PDF text extraction, hashing
│   ├── policy/
│   │   ├── ir.py                  # IR dataclasses + JSON schema
│   │   ├── compiler.py            # LLM policy compiler + quote verification + cache
│   │   ├── resolver.py            # concept → entity resolution (3 tiers)
│   │   └── compiled/policy_ir.json  # committed artifact → offline default
│   ├── planning/
│   │   ├── calendar.py            # date math, business days, effective windows
│   │   ├── requirements.py        # BOM explosion + EDD time-phased netting
│   │   ├── sourcing.py            # eligibility filters + comparator chain + domestic gate
│   │   ├── allocation.py          # MOQ + concentration solver + relaxation ladder
│   │   └── approvals.py           # thresholds, emergency, documentation
│   ├── verify/
│   │   ├── compliance.py          # independent guardrail re-check
│   │   └── invariants.py          # shared invariant assertions (also used by tests)
│   ├── narrate/
│   │   ├── rationale.py           # DecisionRecord → prose (template + optional polish)
│   │   └── alerts.py              # taxonomy + emission
│   └── llm/
│       ├── client.py              # Protocol
│       ├── openai_compat.py       # default adapter
│       ├── litellm_adapter.py     # optional
│       └── cache.py
└── tests/
    ├── unit/  golden/  property/  adversarial/
    ├── generator.py               # synthetic scenario builder (the anti-overfit workhorse)
    └── fixtures/
```

**Dependencies** (all permissive-licensed, none provider-locked): `pydantic`, `httpx`, `pypdf`,
`jsonschema`, `pyyaml`, `pytest`, `hypothesis`. Optional: `litellm`. `sqlite3` and `decimal` are
stdlib.

**CLI**

```bash
python3 agent.py --scenario data/scenarios/scenario_01_baseline.sqlite
```

Additional flags, all optional and defaulted so the required invocation works verbatim:
`--llm={off,auto,required}` · `--recompile-policy` · `--dry-run` (print the plan, write nothing) ·
`--explain <component_id>` (full decision trace) · `--rerun={append,replace}` · `--strict` ·
`--alert-prefixes` · `--json` (emit the full decision record set for downstream tooling).

---

## 16. Delivery plan

| Milestone | Content | Demo value |
|---|---|---|
| **M0** | Repo skeleton, scenario read/write, single-transaction commit, golden-file harness | Runs end-to-end, writes nothing wrong |
| **M1** | Deterministic core against a **hand-written** Policy IR: requirements → sourcing → allocation → alerts | Correct plans for all six scenarios without any model |
| **M2** | LLM policy compiler + entity resolver, with quote verification and caching | Drop in a new memo, behaviour changes, no code change |
| **M3** | Compliance verifier + narration with the numeric-consistency guard | Defensible rationale; guardrail demo |
| **M4** | Property tests, scenario generator, adversarial fixtures, model-swap eval | The generalization argument, evidenced |
| **M5** | README, `--explain` trace, open-questions pack for the check-in | Presentation-ready |

**For the interim check-in**, M0–M3 is the working prototype; M4's scenario generator is the evidence
that it will hold up on held-out data. The most valuable ten minutes of the meeting will be §12 —
the open questions — because those answers change the system's behaviour more than any code we write
before hearing them.
