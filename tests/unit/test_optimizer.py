from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import importlib.util
import itertools
import unittest

from apex_procurement.candidates import supplier_fingerprint
from apex_procurement.domain import (
    AlertCategory,
    CandidateRoute,
    ComparatorTrace,
    DeadlineSupplyPosition,
    DemandBucket,
    DemandContribution,
    EvidenceBasis,
    EvidenceResult,
    EvidenceScope,
    EvidenceStatus,
    FulfillmentStatus,
    InboundSupply,
    PlanDisposition,
    ResolutionStatus,
    RuleSeverity,
    SolveKind,
    SolverStatus,
    Supplier,
    SupplyLedger,
)
from apex_procurement.optimizer import (
    ConcentrationConstraint,
    EconomicAutonomy,
    ExceptionAllowance,
    IntegerScaledSolver,
    OptimizerProblem,
    OrderApprovalConstraint,
    ProcurementOptimizer,
    ScipyMilpBackend,
    SecondaryShortageKind,
    SolverLimits,
    StdlibBranchAndBoundBackend,
    StdlibSolver,
    SupplierVolume,
    derive_upper_bounds,
)
from apex_procurement.protocols import Solver


CURRENT = date(2025, 9, 1)
DUE = date(2025, 9, 20)


def _supplier(
    key: str,
    *,
    tier: str = "Standard",
    rating: str = "B",
) -> Supplier:
    return Supplier(
        supplier_id=f"supplier-{key}",
        name=f"Supplier {key}",
        country="USA",
        is_domestic=True,
        certifications=(),
        sustainability_rating=rating,
        relationship_tier=tier,
        on_approved_list=True,
    )


def _route(
    supplier: Supplier,
    key: str,
    *,
    price: str = "10",
    moq: str = "1",
    available: date = DUE,
    feasible: tuple[date, ...] = (DUE,),
    exceptions: tuple[str, ...] = (),
    strategic_penalty: tuple[date, ...] = (),
    eligibility: EvidenceStatus = EvidenceStatus.PASS,
    approvals: tuple[str, ...] = (),
    evidence: tuple[EvidenceResult, ...] = (),
) -> CandidateRoute:
    lead = (available - CURRENT).days
    trace = (
        ComparatorTrace(
            3,
            "strategic_retention",
            "penalty:" + ",".join(item.isoformat() for item in strategic_penalty)
            if strategic_penalty
            else "no_penalty",
            "test trace",
        ),
    )
    return CandidateRoute(
        route_id=f"route-{key}",
        component_id="component-test",
        supplier_id=supplier.supplier_id,
        supplier_fingerprint=supplier_fingerprint(supplier),
        route_fingerprint=f"semantic-route-{key}",
        unit_price=Decimal(price),
        minimum_order_quantity=Decimal(moq),
        shipping_method="standard",
        lead_time_days=lead,
        order_date=CURRENT,
        expected_delivery_date=available,
        material_available_date=available,
        eligibility=eligibility,
        feasible_deadlines=feasible,
        evidence=evidence,
        exception_codes=exceptions,
        approval_requirements=approvals,
        comparator_trace=trace,
    )


def _problem(
    routes: tuple[CandidateRoute, ...],
    suppliers: tuple[Supplier, ...],
    *,
    quantities: tuple[str, ...] = ("2",),
    deadlines: tuple[date, ...] | None = None,
    on_hand: str = "0",
    **kwargs: object,
) -> OptimizerProblem:
    deadlines = deadlines or tuple(DUE + timedelta(days=10 * index) for index in range(len(quantities)))
    cumulative = Decimal("0")
    buckets = []
    positions = []
    held = Decimal(on_hand)
    for index, (raw, deadline) in enumerate(zip(quantities, deadlines, strict=True)):
        quantity = Decimal(raw)
        cumulative += quantity
        buckets.append(
            DemandBucket(
                "component-test",
                deadline,
                quantity,
                cumulative,
                (DemandContribution(f"order-{index}", "product-test", quantity),),
            )
        )
        positions.append(
            DeadlineSupplyPosition(
                deadline,
                cumulative,
                held,
                max(Decimal("0"), cumulative - held),
                Decimal("0"),
            )
        )
    ledger = SupplyLedger(
        "component-test",
        cumulative,
        held,
        (),
        held,
        max(Decimal("0"), cumulative - held),
        tuple(positions),
    )
    return OptimizerProblem(
        component_id="component-test",
        unit_of_measure="each",
        net_requirement=ledger.eventual_gap,
        routes=routes,
        demand_buckets=tuple(buckets),
        supply_ledger=ledger,
        suppliers=suppliers,
        **kwargs,
    )


def _stdlib_solver(*, node_limit: int = 500_000) -> IntegerScaledSolver:
    backend = StdlibBranchAndBoundBackend()
    return IntegerScaledSolver(
        backend=backend,
        fallback=backend,
        limits=SolverLimits(node_limit=node_limit),
    )


class OptimizerContractTests(unittest.TestCase):
    def test_solver_satisfies_frozen_protocol(self) -> None:
        self.assertIsInstance(IntegerScaledSolver(), Solver)
        self.assertIsInstance(StdlibSolver(), Solver)

    def test_moq_forced_surplus_executes_and_u_equality_is_legal(self) -> None:
        supplier = _supplier("only")
        route = _route(supplier, "only", moq="5")
        problem = _problem(
            (route,),
            (supplier,),
            moq_rule_id="rule-moq",
            sub_moq_approval_rule_id="rule-sub-moq-approval",
        )
        outcome = ProcurementOptimizer(_stdlib_solver()).optimize(problem)

        self.assertIsNotNone(outcome.selected_plan)
        selected = outcome.selected_plan
        assert selected is not None
        self.assertEqual(selected.disposition, PlanDisposition.EXECUTE)
        self.assertEqual(selected.lines[0].quantity, Decimal("5"))
        self.assertEqual(selected.minimum_compliant_total, Decimal("5"))
        self.assertEqual(selected.forced_surplus, Decimal("3"))
        self.assertEqual(selected.discretionary_surplus, ZERO)
        self.assertEqual(
            selected.objective_vector,
            (
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("50"),
                Decimal("3"),
                Decimal("95"),
                Decimal("1"),
            ),
        )
        self.assertEqual(outcome.calibration.objective_vector, (ZERO, Decimal("5")))
        self.assertEqual(outcome.baseline.objective_vector, (ZERO, Decimal("50")))
        self.assertEqual(dict(outcome.derived_upper_bounds)[route.route_id], Decimal("5"))
        self.assertIn(AlertCategory.FORCED_SURPLUS, {item.category for item in outcome.alerts})
        self.assertEqual(outcome.requirement_state.fulfillment, FulfillmentStatus.FULFILLED)
        self.assertEqual(outcome.requirement_state.resolution, ResolutionStatus.RESOLVED)

        sub_moq = next(
            item
            for item in outcome.alternatives
            if item.relaxed_rule_ids == ("rule-sub-moq-approval",)
        )
        self.assertEqual(sub_moq.disposition, PlanDisposition.RECOMMEND_APPROVAL)
        self.assertEqual(sub_moq.lines[0].quantity, Decimal("2"))
        self.assertEqual(
            sub_moq.objective_vector,
            (
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("20"),
                Decimal("0"),
                Decimal("38"),
                Decimal("1"),
            ),
        )

    def test_review_exposure_counts_unique_assumption_codes_not_evidence_rows(self) -> None:
        supplier = _supplier("review")
        evidence = (
            EvidenceResult(
                "rule-review-one",
                EvidenceStatus.UNKNOWN,
                EvidenceBasis.ROLLING_WINDOW,
                EvidenceScope.RULE,
                RuleSeverity.HARD,
                "Two policy branches share normalized assumptions.",
                assumption_codes=("ASSUMPTION_A", "ASSUMPTION_SHARED"),
                contract_disposition=PlanDisposition.EXECUTE_WITH_ASSUMPTION,
            ),
            EvidenceResult(
                "rule-review-two",
                EvidenceStatus.UNKNOWN,
                EvidenceBasis.ROLLING_WINDOW,
                EvidenceScope.RULE,
                RuleSeverity.HARD,
                "The same source gap also carries one distinct assumption.",
                assumption_codes=("ASSUMPTION_SHARED", "ASSUMPTION_B"),
                contract_disposition=PlanDisposition.EXECUTE_WITH_ASSUMPTION,
            ),
        )
        outcome = ProcurementOptimizer(_stdlib_solver()).optimize(
            _problem((_route(supplier, "review", evidence=evidence),), (supplier,))
        )
        selected = outcome.selected_plan
        assert selected is not None
        self.assertEqual(selected.assumption_codes, ("ASSUMPTION_A", "ASSUMPTION_B", "ASSUMPTION_SHARED"))
        self.assertEqual(selected.objective_vector[3], Decimal("3"))
        self.assertEqual(len(selected.objective_vector), 12)

    def test_stage_six_charges_condition_a_but_not_condition_b_volume(self) -> None:
        supplier = _supplier("international")
        for condition, expected in (("condition_a", Decimal("2")), ("condition_b", ZERO)):
            with self.subTest(condition=condition):
                exception = f"rule-domestic:{condition}"
                route = _route(
                    supplier,
                    condition,
                    exceptions=(exception,),
                )
                outcome = ProcurementOptimizer(_stdlib_solver()).optimize(
                    _problem(
                        (route,),
                        (supplier,),
                        exception_allowances=(
                            ExceptionAllowance(exception, (DUE,), Decimal("2")),
                        ),
                    )
                )
                selected = outcome.selected_plan
                assert selected is not None
                self.assertEqual(selected.objective_vector[5], expected)

    def test_stage_ten_uses_sorted_semantic_tuple_quantity_order(self) -> None:
        first = _supplier("tie-first")
        second = _supplier("tie-second")
        routes = (
            _route(first, "tie-first"),
            _route(second, "tie-second"),
        )
        outcome = ProcurementOptimizer(_stdlib_solver(node_limit=1_000_000)).optimize(
            _problem(
                routes,
                (first, second),
                quantities=("3",),
                minimum_secondary_fraction=Decimal("0.33"),
                minimum_secondary_rule_id="rule-secondary",
            )
        )
        selected = outcome.selected_plan
        assert selected is not None
        semantic_first = min(routes, key=lambda item: (item.supplier_fingerprint, item.route_fingerprint))
        first_quantity = next(
            line.quantity for line in selected.lines if line.route_id == semantic_first.route_id
        )
        self.assertEqual(first_quantity, Decimal("1"))
        self.assertEqual(len(selected.objective_vector), 12)

        renumbered_suppliers = (
            replace(first, supplier_id="renumbered-supplier-z"),
            replace(second, supplier_id="renumbered-supplier-a"),
        )
        supplier_ids = {
            first.supplier_id: renumbered_suppliers[0].supplier_id,
            second.supplier_id: renumbered_suppliers[1].supplier_id,
        }
        renumbered_routes = tuple(
            replace(
                route,
                route_id=f"renumbered-route-{index}",
                supplier_id=supplier_ids[route.supplier_id],
            )
            for index, route in enumerate(reversed(routes))
        )
        renumbered = ProcurementOptimizer(_stdlib_solver(node_limit=1_000_000)).optimize(
            _problem(
                renumbered_routes,
                renumbered_suppliers,
                quantities=("3",),
                minimum_secondary_fraction=Decimal("0.33"),
                minimum_secondary_rule_id="rule-secondary",
            )
        ).selected_plan
        assert renumbered is not None
        fingerprints = {
            supplier.supplier_id: supplier_fingerprint(supplier)
            for supplier in (*renumbered_suppliers, first, second)
        }
        self.assertEqual(
            tuple(
                sorted(
                    (fingerprints[line.supplier_id], line.quantity)
                    for line in selected.lines
                )
            ),
            tuple(
                sorted(
                    (fingerprints[line.supplier_id], line.quantity)
                    for line in renumbered.lines
                )
            ),
        )

    def test_secondary_rule_is_not_silently_dropped_with_one_supplier(self) -> None:
        supplier = _supplier("only")
        problem = _problem(
            (_route(supplier, "only"),),
            (supplier,),
            minimum_secondary_fraction=Decimal("0.20"),
            minimum_secondary_rule_id="rule-secondary",
            secondary_shortage_kind=SecondaryShortageKind.STRUCTURAL,
        )
        outcome = ProcurementOptimizer(_stdlib_solver()).optimize(problem)
        self.assertIsNone(outcome.selected_plan)
        self.assertEqual(outcome.emitted_constraint_rule_ids, ())
        self.assertEqual(outcome.requirement_state.resolution, ResolutionStatus.UNRESOLVED)
        self.assertIn("SECONDARY_ALLOCATION_UNSATISFIABLE", {item.code for item in outcome.alerts})
        self.assertEqual(len(outcome.alternatives), 1)
        self.assertEqual(outcome.alternatives[0].disposition, PlanDisposition.DECISION_REQUIRED)
        self.assertEqual(outcome.alternatives[0].total_cost, Decimal("20"))
        # Regression: a solve-0 diagnostic once carried ``(gap, cost)`` as
        # its CandidatePlan vector, placing cost in the stage-2 lateness slot.
        # The candidate now carries the literal staged vector while solve 0
        # retains its separate cheapest-covering certificate.
        self.assertEqual(len(outcome.alternatives[0].objective_vector), 12)
        self.assertEqual(outcome.alternatives[0].objective_vector[1], ZERO)
        self.assertEqual(outcome.alternatives[0].objective_vector[8], Decimal("20"))
        self.assertEqual(outcome.alternatives[0].objective_vector[9], ZERO)
        self.assertTrue(
            outcome.alternatives[0].summary.startswith(
                "Non-executable compliance-cost diagnostic;"
            )
        )
        self.assertIn(
            "rule-secondary",
            outcome.alternatives[0].relaxed_rule_ids,
        )

        # The underlying exact model must also retain the rule.  Solve Q keeps
        # the zero-order point feasible and certifies the uncovered quantity;
        # it may not silently use the sole supplier.
        calibration = _stdlib_solver().solve(problem)
        self.assertEqual(calibration.status, SolverStatus.OPTIMAL)
        self.assertEqual(calibration.objective_vector, (Decimal("2"), ZERO))
        self.assertIsNone(calibration.candidate_plan)

    def test_relaxable_second_supplier_produces_one_rule_counterfactual(self) -> None:
        first = _supplier("available")
        second = _supplier("approval-blocked")
        gate = "rule-route-approval"
        problem = _problem(
            (
                _route(first, "available", price="1"),
                _route(
                    second,
                    "approval-blocked",
                    price="2",
                    eligibility=EvidenceStatus.UNKNOWN,
                    approvals=(gate,),
                ),
            ),
            (first, second),
            minimum_secondary_fraction=Decimal("0.20"),
            minimum_secondary_rule_id="rule-secondary",
            secondary_shortage_kind=SecondaryShortageKind.RELAXABLE,
        )
        outcome = ProcurementOptimizer(_stdlib_solver(node_limit=1_000_000)).optimize(problem)
        self.assertIsNone(outcome.selected_plan)
        recommendation = next(
            item for item in outcome.alternatives if item.relaxed_rule_ids == (gate,)
        )
        self.assertEqual(recommendation.disposition, PlanDisposition.RECOMMEND_APPROVAL)
        self.assertEqual(
            {item.supplier_id for item in recommendation.lines},
            {first.supplier_id, second.supplier_id},
        )

    def test_exception_aggregate_uses_net_shortage_and_on_hand_reduces_it(self) -> None:
        supplier = _supplier("exception")
        first = DUE
        second = DUE + timedelta(days=10)
        exception = "rule-international:condition_a"
        route = _route(
            supplier,
            "exception",
            available=first,
            feasible=(first,),
            exceptions=(exception,),
        )
        caps = []
        for held in ("0", "1"):
            problem = _problem(
                (route,),
                (supplier,),
                quantities=("5", "5"),
                deadlines=(first, second),
                on_hand=held,
                exception_allowances=(ExceptionAllowance(exception, (first,)),),
            )
            solver = _stdlib_solver()
            solver.solve(problem)
            assert solver.last_context is not None
            caps.append(dict(solver.last_context.exception_caps)[exception])
            for route_index, candidate in enumerate(solver.last_context.routes):
                if exception in candidate.exception_codes:
                    scoped = solver.last_context.model.rows
                    self.assertTrue(any(item.name == f"exception_scope[{route_index},1]" for item in scoped))
        self.assertEqual(caps, [5, 4])

    def test_timeout_is_diagnostic_only_and_unresolved(self) -> None:
        supplier = _supplier("timeout")
        forced = IntegerScaledSolver(
            backend=ScipyMilpBackend(),
            limits=SolverLimits(force_status=SolverStatus.TIMEOUT),
        )
        outcome = ProcurementOptimizer(forced).optimize(
            _problem((_route(supplier, "timeout"),), (supplier,))
        )
        self.assertIsNone(outcome.selected_plan)
        self.assertEqual(outcome.requirement_state.resolution, ResolutionStatus.UNRESOLVED)
        self.assertIn(AlertCategory.SOLVER_UNPROVEN, {item.category for item in outcome.alerts})
        self.assertNotIn(ResolutionStatus.INFEASIBLE, {outcome.requirement_state.resolution})

    def test_stdlib_node_budget_exhaustion_is_unresolved(self) -> None:
        supplier = _supplier("node-budget")
        outcome = ProcurementOptimizer(
            StdlibSolver(limits=SolverLimits(node_limit=1))
        ).optimize(_problem((_route(supplier, "node-budget"),), (supplier,)))
        self.assertIsNone(outcome.selected_plan)
        self.assertEqual(outcome.requirement_state.resolution, ResolutionStatus.UNRESOLVED)
        self.assertIn(AlertCategory.SOLVER_UNPROVEN, {item.category for item in outcome.alerts})


@unittest.skipUnless(importlib.util.find_spec("scipy"), "SciPy is optional on the fallback path")
class HighsDifferentialTests(unittest.TestCase):
    def solver(self, *, gap: Decimal | None = None) -> IntegerScaledSolver:
        return IntegerScaledSolver(
            backend=ScipyMilpBackend(),
            limits=SolverLimits(force_mip_gap=gap),
        )

    def test_solve_q_matches_independent_enumeration(self) -> None:
        first = _supplier("first")
        second = _supplier("second")
        routes = (
            _route(first, "first", price="5", moq="5"),
            _route(second, "second", price="7", moq="2"),
        )
        problem = _problem(
            routes,
            (first, second),
            quantities=("4",),
            minimum_secondary_fraction=Decimal("0.20"),
            minimum_secondary_rule_id="rule-secondary",
        )
        solver = self.solver()
        result = solver.solve(problem)
        self.assertTrue(result.is_certified_optimal)
        self.assertIn("rule-secondary", solver.last_context.emitted_rule_ids)

        upper = int(max(dict(derive_upper_bounds(problem)).values()))
        feasible = []
        for left, right in itertools.product(range(upper + 1), repeat=2):
            if left not in {0, *range(5, upper + 1)} or right not in {0, *range(2, upper + 1)}:
                continue
            total = left + right
            if total and (left > Decimal("0.80") * total or right > Decimal("0.80") * total):
                continue
            coverage = min(4, total)
            feasible.append((-coverage, total, left, right))
        oracle = min(feasible)
        self.assertEqual(result.minimum_compliant_total, Decimal(oracle[1]))
        self.assertEqual(result.candidate_plan.eventual_covered_quantity, Decimal(-oracle[0]))

    def test_generated_small_solve_q_cases_match_enumeration(self) -> None:
        for seed in range(8):
            with self.subTest(seed=seed):
                demand = 1 + seed % 5
                first_moq = 1 + seed % 3
                second_moq = 1 + (seed * 2) % 4
                first = _supplier(f"generated-first-{seed}")
                second = _supplier(f"generated-second-{seed}")
                routes = (
                    _route(first, f"generated-first-{seed}", moq=str(first_moq)),
                    _route(second, f"generated-second-{seed}", moq=str(second_moq)),
                )
                problem = _problem(
                    routes,
                    (first, second),
                    quantities=(str(demand),),
                    minimum_secondary_fraction=Decimal("0.20"),
                    minimum_secondary_rule_id="rule-secondary",
                )
                result = self.solver().solve(problem)
                upper = int(max(dict(derive_upper_bounds(problem)).values()))
                oracle = min(
                    (-min(demand, left + right), left + right)
                    for left, right in itertools.product(range(upper + 1), repeat=2)
                    if (left == 0 or left >= first_moq)
                    and (right == 0 or right >= second_moq)
                    and (
                        left + right == 0
                        or (
                            left <= Decimal("0.80") * (left + right)
                            and right <= Decimal("0.80") * (left + right)
                        )
                    )
                )
                self.assertEqual(result.candidate_plan.eventual_covered_quantity, Decimal(-oracle[0]))
                self.assertEqual(result.minimum_compliant_total, Decimal(oracle[1]))

    def test_u_accounts_for_allocation_driven_surplus_at_high_secondary_fraction(self) -> None:
        first = _supplier("large-moq")
        second = _supplier("small-moq")
        problem = _problem(
            (
                _route(first, "large-moq", moq="100"),
                _route(second, "small-moq", moq="1"),
            ),
            (first, second),
            quantities=("1",),
            minimum_secondary_fraction=Decimal("0.49"),
            minimum_secondary_rule_id="rule-secondary",
        )
        self.assertGreaterEqual(min(dict(derive_upper_bounds(problem)).values()), Decimal("197"))
        result = self.solver().solve(problem)
        self.assertEqual(result.minimum_compliant_total, Decimal("197"))

    def test_all_ten_stages_match_an_independent_small_enumerator(self) -> None:
        strategic = _supplier("strategic", tier="Strategic", rating="A")
        ordinary = _supplier("ordinary", rating="B")
        routes = (
            _route(strategic, "strategic", price="10", strategic_penalty=()),
            _route(ordinary, "ordinary", price="9", strategic_penalty=(DUE,)),
        )
        base = _problem(routes, (strategic, ordinary), quantities=("2",))
        optimizer = ProcurementOptimizer(self.solver())
        outcome = optimizer.optimize(base)
        selected = outcome.selected_plan
        assert selected is not None
        self.assertEqual(len(outcome.executable.stage_results), 10)

        ranked = []
        for strategic_quantity in range(3):
            ordinary_quantity = 2 - strategic_quantity
            quantities = (strategic_quantity, ordinary_quantity)
            vector = (
                0,  # cumulative unresolved
                0,  # unit-late-days
                0,  # discretionary surplus
                0,  # review exposure
                0,  # no named primary
                0,  # domestic routes
                ordinary_quantity,  # strategic-window penalty
                ordinary_quantity,  # one sustainability band below A
                10 * strategic_quantity + 9 * ordinary_quantity,
                0,  # MOQ-driven excess above net requirement
                sum(routes[index].lead_time_days * quantity for index, quantity in enumerate(quantities)),
                sum(quantity > 0 for quantity in quantities),
            )
            ranked.append((vector, quantities))
        _, expected = min(ranked)
        actual = tuple(
            int(next((line.quantity for line in selected.lines if line.route_id == route.route_id), ZERO))
            for route in routes
        )
        self.assertEqual(actual, expected)
        self.assertEqual(actual, (2, 0))
        self.assertEqual(outcome.executable.objective_vector, tuple(Decimal(item) for item in min(ranked)[0]))

    def test_concentration_includes_existing_supplier_history(self) -> None:
        first = _supplier("history-heavy")
        second = _supplier("history-light")
        problem = _problem(
            (
                _route(first, "history-heavy", price="1"),
                _route(second, "history-light", price="2"),
            ),
            (first, second),
            quantities=("4",),
            concentration_constraints=(
                ConcentrationConstraint(
                    "rule-concentration",
                    Decimal("0.60"),
                    Decimal("10"),
                    (
                        SupplierVolume(first.supplier_id, Decimal("6")),
                        SupplierVolume(second.supplier_id, Decimal("4")),
                    ),
                ),
            ),
        )
        outcome = ProcurementOptimizer(self.solver()).optimize(problem)
        selected = outcome.selected_plan
        assert selected is not None
        first_quantity = next(
            (item.quantity for item in selected.lines if item.supplier_id == first.supplier_id),
            ZERO,
        )
        self.assertLessEqual(first_quantity, Decimal("2"))

    def test_solve_zero_nets_late_committed_inbound_eventually(self) -> None:
        supplier = _supplier("baseline")
        route = _route(supplier, "baseline", price="10")
        base = _problem((route,), (supplier,), quantities=("10",), on_hand="2")
        inbound = InboundSupply(
            "po-existing",
            "component-test",
            supplier.supplier_id,
            Decimal("3"),
            DUE + timedelta(days=30),
        )
        ledger = SupplyLedger(
            "component-test",
            Decimal("10"),
            Decimal("2"),
            (inbound,),
            Decimal("5"),
            Decimal("5"),
            (
                DeadlineSupplyPosition(
                    DUE,
                    Decimal("10"),
                    Decimal("2"),
                    Decimal("8"),
                    Decimal("0"),
                ),
            ),
        )
        baseline_problem = replace(
            base,
            solve_kind=SolveKind.BASELINE,
            net_requirement=Decimal("5"),
            supply_ledger=ledger,
        )
        result = self.solver().solve(baseline_problem)
        self.assertTrue(result.is_certified_optimal)
        self.assertEqual(result.candidate_plan.lines[0].quantity, Decimal("5"))
        self.assertEqual(result.cheapest_covering_cost, Decimal("50"))

    def test_doubling_derived_u_does_not_improve_any_objective(self) -> None:
        supplier = _supplier("bound")
        problem = _problem((_route(supplier, "bound", moq="5"),), (supplier,))
        ordinary = ProcurementOptimizer(self.solver()).optimize(problem)
        doubled_solver = IntegerScaledSolver(
            backend=ScipyMilpBackend(),
            upper_bound_multiplier=2,
        )
        doubled = ProcurementOptimizer(doubled_solver).optimize(problem)
        self.assertEqual(
            ordinary.executable.objective_vector,
            doubled.executable.objective_vector,
        )
        self.assertEqual(
            tuple((line.route_id, line.quantity) for line in ordinary.selected_plan.lines),
            tuple((line.route_id, line.quantity) for line in doubled.selected_plan.lines),
        )

    def test_discretionary_surplus_boundary_is_inclusive(self) -> None:
        first_due = DUE
        last_due = DUE + timedelta(days=20)
        primary = _supplier("primary")
        recovery = _supplier("recovery")
        problem = _problem(
            (
                _route(
                    primary,
                    "primary-slow",
                    price="1",
                    moq="10",
                    available=DUE + timedelta(days=10),
                    feasible=(last_due,),
                ),
                _route(
                    recovery,
                    "recovery-fast",
                    price="1",
                    available=DUE,
                    feasible=(first_due, last_due),
                ),
            ),
            (primary, recovery),
            quantities=("1", "9"),
            deadlines=(first_due, last_due),
            named_primary_supplier_id=primary.supplier_id,
            named_primary_rule_id="rule-named-primary",
            autonomy=EconomicAutonomy(max_surplus_fraction=Decimal("0.10")),
        )
        outcome = ProcurementOptimizer(self.solver()).optimize(problem)
        selected = outcome.selected_plan
        assert selected is not None
        self.assertEqual(selected.minimum_compliant_total, Decimal("10"))
        self.assertEqual(selected.discretionary_surplus, Decimal("1"))
        self.assertEqual(sum((item.quantity for item in selected.lines), ZERO), Decimal("11"))

    def test_stage_windows_are_conditional_at_policy_boundaries(self) -> None:
        strategic = _supplier("window-strategic", tier="Strategic", rating="A")
        ordinary = _supplier("window-ordinary", rating="B")
        # A 20% saving lies outside strategic retention and the price gap is
        # outside sustainability's inclusive 10% band, so cost decides.
        problem = _problem(
            (
                _route(strategic, "window-strategic", price="10"),
                _route(ordinary, "window-ordinary", price="8"),
            ),
            (strategic, ordinary),
        )
        selected = ProcurementOptimizer(self.solver()).optimize(problem).selected_plan
        assert selected is not None
        self.assertEqual(selected.lines[0].supplier_id, ordinary.supplier_id)

        domestic = _supplier("domestic")
        international = _supplier("international")
        for condition, expected in (
            ("condition_b", international.supplier_id),
            ("condition_a", domestic.supplier_id),
        ):
            with self.subTest(condition=condition):
                exception = f"rule-domestic:{condition}"
                scoped = _route(
                    international,
                    f"international-{condition}",
                    price="1",
                    exceptions=(exception,),
                )
                current = _problem(
                    (_route(domestic, f"domestic-{condition}", price="2"), scoped),
                    (domestic, international),
                    exception_allowances=(ExceptionAllowance(exception, (DUE,), Decimal("2")),),
                )
                plan = ProcurementOptimizer(self.solver()).optimize(current).selected_plan
                assert plan is not None
                self.assertEqual(plan.lines[0].supplier_id, expected)

        # Exactly 15% savings retains the Strategic supplier.
        exact_strategic = _supplier("exact-strategic", tier="Strategic", rating="B")
        exact_ordinary = _supplier("exact-ordinary", rating="B")
        exact = _problem(
            (
                _route(exact_strategic, "exact-strategic", price="10"),
                _route(
                    exact_ordinary,
                    "exact-ordinary",
                    price="8.5",
                    strategic_penalty=(DUE,),
                ),
            ),
            (exact_strategic, exact_ordinary),
        )
        exact_plan = ProcurementOptimizer(self.solver()).optimize(exact).selected_plan
        assert exact_plan is not None
        self.assertEqual(exact_plan.lines[0].supplier_id, exact_strategic.supplier_id)

        # Sustainability's price and five-business-day boundaries are both
        # inclusive; with earlier stages tied, the better rating wins.
        later_due = DUE + timedelta(days=20)
        high = _supplier("sustainable-high", rating="A")
        low = _supplier("sustainable-low", rating="B")
        sustainability = _problem(
            (
                _route(
                    high,
                    "sustainable-high",
                    price="11",
                    available=DUE + timedelta(days=7),
                    feasible=(later_due,),
                ),
                _route(
                    low,
                    "sustainable-low",
                    price="10",
                    available=DUE,
                    feasible=(later_due,),
                ),
            ),
            (high, low),
            quantities=("2",),
            deadlines=(later_due,),
        )
        sustainable_plan = ProcurementOptimizer(self.solver()).optimize(sustainability).selected_plan
        assert sustainable_plan is not None
        self.assertEqual(sustainable_plan.lines[0].supplier_id, high.supplier_id)

    def test_small_order_regression_orders_every_eligible_component(self) -> None:
        for index, moq in enumerate(("1", "5", "25")):
            with self.subTest(component=index, moq=moq):
                supplier = _supplier(f"small-{index}")
                outcome = ProcurementOptimizer(self.solver()).optimize(
                    _problem((_route(supplier, f"small-{index}", moq=moq),), (supplier,))
                )
                self.assertIsNotNone(outcome.selected_plan)
                self.assertTrue(outcome.selected_plan.disposition.writes_purchase_order)

    def test_nonzero_gap_never_executes(self) -> None:
        supplier = _supplier("gap")
        outcome = ProcurementOptimizer(self.solver(gap=Decimal("0.01"))).optimize(
            _problem((_route(supplier, "gap"),), (supplier,))
        )
        self.assertIsNone(outcome.selected_plan)
        self.assertEqual(outcome.requirement_state.resolution, ResolutionStatus.UNRESOLVED)

    def test_timeout_at_each_lexicographic_stage_never_returns_an_executable(self) -> None:
        supplier = _supplier("stage-timeout")
        base = _problem((_route(supplier, "stage-timeout"),), (supplier,))
        executable = replace(
            base,
            solve_kind=SolveKind.EXECUTABLE,
            minimum_compliant_total=Decimal("2"),
            coverage_target=Decimal("2"),
            cheapest_covering_cost=Decimal("20"),
        )

        class TimeoutBackend(ScipyMilpBackend):
            def __init__(self, stop: int) -> None:
                self.stop = stop
                self.calls = 0

            def optimize(self, model, objective, limits):
                self.calls += 1
                if self.calls == self.stop:
                    limits = replace(limits, force_status=SolverStatus.TIMEOUT)
                return super().optimize(model, objective, limits)

        # One bucket gives calls 1..8 for stages 1..8, calls 9..10 for
        # stage 9, calls 11..12 for stage-10 metrics, then calls 13..14
        # to certify semantic-route membership and its scaled quantity.
        for call in (*range(1, 9), 9, 11, 13, 14):
            with self.subTest(stage_call=call):
                result = IntegerScaledSolver(backend=TimeoutBackend(call)).solve(executable)
                self.assertEqual(result.status, SolverStatus.UNRESOLVED)
                self.assertFalse(result.has_executable_certificate)
                self.assertTrue(
                    result.candidate_plan is None
                    or not result.candidate_plan.disposition.writes_purchase_order
                )

    def test_feasible_incumbent_is_relabelled_diagnostic_only(self) -> None:
        supplier = _supplier("incumbent")
        base = _problem((_route(supplier, "incumbent"),), (supplier,))
        executable = replace(
            base,
            solve_kind=SolveKind.EXECUTABLE,
            minimum_compliant_total=Decimal("2"),
            coverage_target=Decimal("2"),
            cheapest_covering_cost=Decimal("20"),
        )

        class IncumbentBackend(ScipyMilpBackend):
            def optimize(self, model, objective, limits):
                result = super().optimize(model, objective, limits)
                return replace(
                    result,
                    status=SolverStatus.FEASIBLE_INCUMBENT,
                    mip_gap=Decimal("0.01"),
                    certificate_complete=False,
                    hit_resource_limit=True,
                )

        result = IntegerScaledSolver(backend=IncumbentBackend()).solve(executable)
        self.assertEqual(result.status, SolverStatus.UNRESOLVED)
        self.assertIsNotNone(result.candidate_plan)
        self.assertEqual(result.candidate_plan.disposition, PlanDisposition.DECISION_REQUIRED)
        self.assertFalse(result.has_executable_certificate)

    def test_exact_revalidation_rejects_an_invalid_reported_optimum(self) -> None:
        supplier = _supplier("invalid-optimum")
        problem = _problem((_route(supplier, "invalid-optimum"),), (supplier,))

        class CorruptBackend(ScipyMilpBackend):
            def optimize(self, model, objective, limits):
                result = super().optimize(model, objective, limits)
                assert result.values is not None
                values = list(result.values)
                values[0] = model.upper[0] + 1
                return replace(result, values=tuple(values))

        result = IntegerScaledSolver(backend=CorruptBackend()).solve(problem)
        self.assertEqual(result.status, SolverStatus.UNRESOLVED)
        self.assertFalse(result.exact_post_validated)
        self.assertFalse(result.has_executable_certificate)

    def test_quantity_dependent_approval_is_a_non_executable_complete_proposal(self) -> None:
        supplier = _supplier("approval")
        problem = _problem(
            (_route(supplier, "approval", price="100"),),
            (supplier,),
            quantities=("2",),
            order_approval_constraints=(
                OrderApprovalConstraint("rule-order-approval", Decimal("150"), "Approver"),
            ),
            autonomy=EconomicAutonomy(max_surplus_fraction=Decimal("1")),
        )
        outcome = ProcurementOptimizer(self.solver()).optimize(problem)
        self.assertIsNotNone(outcome.selected_plan)
        self.assertEqual(outcome.selected_plan.total_cost, Decimal("100"))
        self.assertEqual(outcome.selected_plan.residual_gap, Decimal("1"))
        proposal = next(
            item
            for item in outcome.alternatives
            if item.relaxed_rule_ids == ("rule-order-approval",)
        )
        self.assertEqual(proposal.disposition, PlanDisposition.RECOMMEND_APPROVAL)
        self.assertEqual(proposal.total_cost, Decimal("200"))
        self.assertEqual(outcome.requirement_state.fulfillment, FulfillmentStatus.PARTIALLY_FULFILLED)
        self.assertEqual(outcome.requirement_state.resolution, ResolutionStatus.UNRESOLVED)


ZERO = Decimal("0")


if __name__ == "__main__":
    unittest.main()
