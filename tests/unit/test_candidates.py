from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import unittest

from apex_procurement.candidates import (
    CandidateBuilder,
    DomesticGateCondition,
    GreedyProblem,
    GreedySolver,
    NoAlternativeProof,
    build_candidate_routes,
    component_fingerprint,
    evaluate_domestic_gate,
    rank_candidate_routes,
    supplier_fingerprint,
)
from apex_procurement.domain import (
    CandidateRoute,
    Component,
    DeadlineSupplyPosition,
    DemandBucket,
    DemandContribution,
    EvidenceScope,
    EvidenceStatus,
    PlanDisposition,
    ScenarioConfiguration,
    ScenarioSnapshot,
    SolverStatus,
    Supplier,
    SupplierCatalogLine,
    SupplyLedger,
)
from apex_procurement.ledgers import LedgerBuildResult, build_ledgers
from apex_procurement.protocols import Solver
from apex_procurement.repository import load_snapshot


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = PROJECT_ROOT / "data" / "scenarios"


def _supplier(supplier_id: str, name: str) -> Supplier:
    return Supplier(
        supplier_id=supplier_id,
        name=name,
        country="USA",
        is_domestic=True,
        certifications=(),
        sustainability_rating="B",
        relationship_tier="Standard",
        on_approved_list=True,
    )


def _small_case(
    suppliers: tuple[Supplier, ...],
    *,
    supplier_ids: tuple[str, ...] | None = None,
) -> tuple[ScenarioSnapshot, LedgerBuildResult]:
    component = Component(
        component_id="component-local",
        name="General Bracket",
        description="General purpose bracket",
        category="Raw Material",
        unit_of_measure="each",
        is_hazardous=False,
    )
    ids = supplier_ids or tuple(item.supplier_id for item in suppliers)
    catalogs = tuple(
        SupplierCatalogLine(
            supplier_id=supplier_id,
            component_id=component.component_id,
            unit_price=Decimal("10"),
            lead_time_days=5,
            minimum_order_quantity=Decimal("1"),
        )
        for supplier_id in ids
    )
    current = date(2025, 9, 1)
    due = date(2025, 9, 20)
    snapshot = ScenarioSnapshot(
        configuration=ScenarioConfiguration(current),
        products=(),
        components=(component,),
        suppliers=suppliers,
        bom_lines=(),
        catalog_lines=catalogs,
        production_orders=(),
        inventory=(),
        purchase_orders=(),
        alerts=(),
        state_digest="small-case",
    )
    bucket = DemandBucket(
        component_id=component.component_id,
        due_date=due,
        bucket_quantity=Decimal("10"),
        cumulative_quantity=Decimal("10"),
        contributions=(
            DemandContribution("order-local", "product-local", Decimal("10")),
        ),
    )
    ledger = SupplyLedger(
        component_id=component.component_id,
        total_demand=Decimal("10"),
        on_hand=Decimal("0"),
        committed_inbound=(),
        eventual_supply=Decimal("0"),
        eventual_gap=Decimal("10"),
        deadline_positions=(
            DeadlineSupplyPosition(
                due_date=due,
                cumulative_demand=Decimal("10"),
                on_time_supply=Decimal("0"),
                on_time_gap=Decimal("10"),
                recoverable_gap=Decimal("0"),
            ),
        ),
    )
    return snapshot, LedgerBuildResult((bucket,), (ledger,), ())


def _route(
    supplier: Supplier,
    *,
    price: str,
    lead_days: int,
    route_key: str,
) -> CandidateRoute:
    order_date = date(2025, 9, 1)
    delivery = order_date + timedelta(days=lead_days)
    return CandidateRoute(
        route_id=f"route-{route_key}",
        component_id="component-local",
        supplier_id=supplier.supplier_id,
        supplier_fingerprint=supplier_fingerprint(supplier),
        route_fingerprint=f"fingerprint-{route_key}",
        unit_price=Decimal(price),
        minimum_order_quantity=Decimal("1"),
        shipping_method="standard",
        lead_time_days=lead_days,
        order_date=order_date,
        expected_delivery_date=delivery,
        material_available_date=delivery,
        eligibility=EvidenceStatus.PASS,
        feasible_deadlines=(date(2025, 10, 1),),
        evidence=(),
    )


class SuppliedScenarioCandidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.baseline = load_snapshot(SCENARIOS / "scenario_01_baseline.sqlite")
        cls.baseline_ledgers = build_ledgers(cls.baseline)
        cls.baseline_candidates = build_candidate_routes(
            cls.baseline, cls.baseline_ledgers
        )

    def supplier(self, prefix: str) -> Supplier:
        return next(item for item in self.baseline.suppliers if item.name.startswith(prefix))

    def component(self, prefix: str) -> Component:
        return next(item for item in self.baseline.components if item.name.startswith(prefix))

    def routes(self, component_prefix: str, supplier_prefix: str):
        component = self.component(component_prefix)
        supplier = self.supplier(supplier_prefix)
        return tuple(
            item
            for item in self.baseline_candidates.routes_for(component.component_id)
            if item.supplier_id == supplier.supplier_id
        )

    def test_every_catalog_offer_has_a_classified_standard_route_and_reasons(self) -> None:
        catalog_pairs = {
            (item.component_id, item.supplier_id)
            for item in self.baseline.catalog_lines
        }
        routed_pairs = {
            (item.component_id, item.supplier_id)
            for item in self.baseline_candidates.routes
            if item.shipping_method == "standard"
        }
        self.assertEqual(routed_pairs, catalog_pairs)
        self.assertTrue(
            all(item.eligibility in set(EvidenceStatus) for item in self.baseline_candidates.routes)
        )
        rejected_ids = {item.route_id for item in self.baseline_candidates.rejections}
        for route in self.baseline_candidates.routes:
            if (
                route.eligibility is not EvidenceStatus.PASS
                or route.approval_requirements
                or not route.feasible_deadlines
            ):
                self.assertIn(route.route_id, rejected_ids)
            self.assertTrue(route.evidence)
            self.assertTrue(all(item.rule_id for item in route.evidence))

    def test_asl_certification_and_unresolved_routes_never_enter_executable_set(self) -> None:
        off_asl = self.routes("PCB Assembly", "Jiangsu")
        self.assertTrue(off_asl)
        self.assertTrue(all(item.eligibility is EvidenceStatus.FAIL for item in off_asl))
        executable = set(self.baseline_candidates.executable_routes)
        self.assertFalse(any(item in executable for item in off_asl))
        self.assertTrue(
            all(item.eligibility is EvidenceStatus.PASS for item in executable)
        )
        self.assertTrue(all(not item.approval_requirements for item in executable))

    def test_electronic_iso_is_structural_but_raw_parts_are_not_guessed_safety_critical(self) -> None:
        copper = self.routes("Copper Wire", "Delta Winding")
        self.assertTrue(copper)
        self.assertTrue(all(item.eligibility is EvidenceStatus.PASS for item in copper))
        self.assertFalse(
            any(
                item.status is EvidenceStatus.FAIL and "certification" in item.rule_id
                for route in copper
                for item in route.evidence
            )
        )

        magnet = self.component("Neodymium")
        proof = NoAlternativeProof(
            component_fingerprint(magnet),
            SolverStatus.INFEASIBLE,
            certificate_complete=True,
            independently_validated=True,
        )
        candidates = CandidateBuilder(
            no_alternative_proofs=(proof,)
        ).build(self.baseline, self.baseline_ledgers)
        nanjing = self.supplier("Nanjing")
        magnet_routes = tuple(
            item
            for item in candidates.routes_for(magnet.component_id)
            if item.supplier_id == nanjing.supplier_id
        )
        self.assertTrue(magnet_routes)
        self.assertTrue(all(item.eligibility is EvidenceStatus.PASS for item in magnet_routes))
        review = tuple(
            evidence
            for route in magnet_routes
            for evidence in route.evidence
            if "below_b_review" in evidence.rule_id
        )
        self.assertTrue(review)
        self.assertTrue(
            all(
                item.scope is EvidenceScope.RULE
                and item.contract_disposition is PlanDisposition.EXECUTE_WITH_ASSUMPTION
                for item in review
            )
        )

    def test_pcb_incumbency_inference_allows_only_supported_benchmark_route(self) -> None:
        incumbent = self.routes("PCB Assembly", "Sterling")
        new_supplier = self.routes("PCB Assembly", "Shenzhen")
        self.assertTrue(incumbent)
        self.assertTrue(all(item.eligibility is EvidenceStatus.PASS for item in incumbent))
        inferred = tuple(
            item
            for route in incumbent
            for item in route.evidence
            if item.rule_id.endswith("pcb_incumbent_only")
        )
        self.assertTrue(inferred)
        self.assertTrue(all(item.scope is EvidenceScope.RULE for item in inferred))
        self.assertTrue(new_supplier)
        self.assertTrue(all(item.eligibility is EvidenceStatus.FAIL for item in new_supplier))

    def test_air_is_approval_gated_and_delivery_date_excludes_receiving_buffer(self) -> None:
        air = tuple(
            item
            for item in self.baseline_candidates.routes
            if item.shipping_method == "air freight"
        )
        self.assertTrue(air)
        self.assertTrue(all(item.approval_requirements for item in air))
        self.assertFalse(any(item.may_enter_executable_model for item in air))
        for route in self.baseline_candidates.routes:
            self.assertEqual(
                route.expected_delivery_date,
                route.order_date + timedelta(days=route.lead_time_days),
            )

        # Approval and a represented within-cap spend make the active air route
        # eligible; its scoped deadlines still require standard lead to miss.
        approved = build_candidate_routes(
            self.baseline,
            self.baseline_ledgers,
            approved_air_route_fingerprints=tuple(item.route_fingerprint for item in air),
            air_period_spend=Decimal("0"),
        )
        approved_air = tuple(
            item for item in approved.routes if item.shipping_method == "air freight"
        )
        self.assertTrue(all(item.eligibility is EvidenceStatus.PASS for item in approved_air))
        self.assertTrue(all(not item.approval_requirements for item in approved_air))
        standard_by_offer = {
            (item.component_id, item.supplier_id): item
            for item in approved.routes
            if item.shipping_method == "standard"
        }
        for route in approved_air:
            standard = standard_by_offer[(route.component_id, route.supplier_id)]
            self.assertTrue(
                all(standard.material_available_date > due for due in route.feasible_deadlines)
            )

    def test_scenario_five_air_authorization_is_expired(self) -> None:
        snapshot = load_snapshot(SCENARIOS / "scenario_05_competing_demand.sqlite")
        result = build_candidate_routes(snapshot, build_ledgers(snapshot))
        self.assertFalse(
            any(item.shipping_method == "air freight" for item in result.routes)
        )
        for route in result.routes:
            self.assertEqual(route.order_date, date(2025, 10, 5))
            self.assertEqual(
                route.expected_delivery_date,
                date(2025, 10, 5) + timedelta(days=route.lead_time_days),
            )

    def test_exception_routes_carry_only_predicate_opened_buckets(self) -> None:
        magnet_routes = self.routes("Neodymium", "Nanjing")
        condition_a = next(
            item
            for item in magnet_routes
            if any(code.endswith("condition_a") for code in item.exception_codes)
        )
        condition_b = next(
            item
            for item in magnet_routes
            if any(code.endswith("condition_b") for code in item.exception_codes)
        )
        self.assertEqual(condition_a.feasible_deadlines, ())
        self.assertEqual(condition_b.feasible_deadlines, (date(2025, 10, 10),))
        trace_a = next(item for item in condition_a.comparator_trace if item.stage == 2)
        trace_b = next(item for item in condition_b.comparator_trace if item.stage == 2)
        self.assertEqual(trace_a.outcome, "moot")
        self.assertEqual(trace_b.outcome, "skipped")


class DomesticGateBoundaryTests(unittest.TestCase):
    def decision(self, domestic: str, international: str, critical: bool):
        return evaluate_domestic_gate(
            domestic_source_exists=True,
            domestic_can_meet_deadline=True,
            best_domestic_price=Decimal(domestic),
            best_international_price=Decimal(international),
            critical_status=critical,
        )

    def test_35_percent_boundary_is_strict(self) -> None:
        self.assertIs(
            self.decision("135", "100", False).condition,
            DomesticGateCondition.SHUT,
        )
        self.assertIs(
            self.decision("135.0001", "100", False).condition,
            DomesticGateCondition.PREMIUM,
        )

    def test_50_percent_boundary_is_strict(self) -> None:
        self.assertIs(
            self.decision("150", "100", True).condition,
            DomesticGateCondition.SHUT,
        )
        self.assertIs(
            self.decision("150.0001", "100", True).condition,
            DomesticGateCondition.PREMIUM,
        )

    def test_gate_condition_controls_preference_state(self) -> None:
        timeline = evaluate_domestic_gate(
            domestic_source_exists=True,
            domestic_can_meet_deadline=False,
            best_domestic_price=Decimal("100"),
            best_international_price=Decimal("90"),
            critical_status=False,
        )
        premium = self.decision("140", "100", False)
        no_source = evaluate_domestic_gate(
            domestic_source_exists=False,
            domestic_can_meet_deadline=False,
            best_domestic_price=None,
            best_international_price=Decimal("90"),
            critical_status=False,
        )
        shut = self.decision("120", "100", False)
        self.assertEqual(timeline.domestic_preference_state, "moot")
        self.assertEqual(premium.domestic_preference_state, "skipped")
        self.assertEqual(no_source.domestic_preference_state, "moot")
        self.assertEqual(shut.domestic_preference_state, "not_reached")

    def test_no_international_offer_has_no_denominator(self) -> None:
        decision = evaluate_domestic_gate(
            domestic_source_exists=True,
            domestic_can_meet_deadline=False,
            best_domestic_price=Decimal("100"),
            best_international_price=None,
            critical_status=False,
        )
        self.assertIs(decision.condition, DomesticGateCondition.SHUT)
        self.assertIsNone(decision.premium_fraction)


class FingerprintAndGreedyTests(unittest.TestCase):
    def test_strategic_retention_boundary_is_inclusive(self) -> None:
        strategic = replace(
            _supplier("strategic", "Strategic Supply"),
            relationship_tier="Strategic",
        )
        standard = _supplier("standard", "Standard Supply")
        retained = rank_candidate_routes(
            (
                _route(strategic, price="100", lead_days=5, route_key="strategic"),
                _route(standard, price="85", lead_days=5, route_key="standard"),
            ),
            (strategic, standard),
        )
        self.assertEqual(retained[0].supplier_id, strategic.supplier_id)

        released = rank_candidate_routes(
            (
                _route(strategic, price="100", lead_days=5, route_key="strategic"),
                _route(standard, price="84.99", lead_days=5, route_key="standard"),
            ),
            (strategic, standard),
        )
        self.assertEqual(released[0].supplier_id, standard.supplier_id)

    def test_sustainability_windows_are_inclusive_and_conditional(self) -> None:
        rated_a = replace(
            _supplier("rated-a", "Rated A Supply"), sustainability_rating="A"
        )
        rated_b = _supplier("rated-b", "Rated B Supply")
        within = rank_candidate_routes(
            (
                _route(rated_a, price="110", lead_days=10, route_key="rated-a"),
                _route(rated_b, price="100", lead_days=5, route_key="rated-b"),
            ),
            (rated_a, rated_b),
        )
        self.assertEqual(within[0].supplier_id, rated_a.supplier_id)

        outside_price = rank_candidate_routes(
            (
                _route(rated_a, price="110.01", lead_days=10, route_key="rated-a"),
                _route(rated_b, price="100", lead_days=5, route_key="rated-b"),
            ),
            (rated_a, rated_b),
        )
        self.assertEqual(outside_price[0].supplier_id, rated_b.supplier_id)

        outside_delivery = rank_candidate_routes(
            (
                _route(rated_a, price="110", lead_days=11, route_key="rated-a"),
                _route(rated_b, price="100", lead_days=3, route_key="rated-b"),
            ),
            (rated_a, rated_b),
        )
        self.assertEqual(outside_delivery[0].supplier_id, rated_b.supplier_id)

    def test_exact_tie_order_is_invariant_under_consistent_id_permutation(self) -> None:
        alpha = _supplier("supplier-one", "Alpha Supply")
        beta = _supplier("supplier-two", "Beta Supply")
        snapshot, ledgers = _small_case((alpha, beta))
        result = build_candidate_routes(snapshot, ledgers)
        ranked = rank_candidate_routes(result.executable_routes, snapshot.suppliers)

        renamed_alpha = replace(alpha, supplier_id="supplier-two")
        renamed_beta = replace(beta, supplier_id="supplier-one")
        renamed_snapshot, renamed_ledgers = _small_case(
            (renamed_alpha, renamed_beta),
            supplier_ids=(renamed_alpha.supplier_id, renamed_beta.supplier_id),
        )
        renamed = build_candidate_routes(renamed_snapshot, renamed_ledgers)
        renamed_ranked = rank_candidate_routes(
            renamed.executable_routes, renamed_snapshot.suppliers
        )
        self.assertEqual(
            tuple(item.supplier_fingerprint for item in ranked),
            tuple(item.supplier_fingerprint for item in renamed_ranked),
        )
        self.assertEqual(
            supplier_fingerprint(alpha), supplier_fingerprint(renamed_alpha)
        )

    def test_ambiguous_supplier_fingerprints_block_autonomy(self) -> None:
        first = _supplier("supplier-one", "Indistinguishable Supply")
        second = _supplier("supplier-two", "Indistinguishable Supply")
        snapshot, ledgers = _small_case((first, second))
        result = build_candidate_routes(snapshot, ledgers)
        self.assertTrue(result.routes)
        self.assertTrue(
            all(item.eligibility is EvidenceStatus.UNKNOWN for item in result.routes)
        )
        self.assertEqual(result.executable_routes, ())
        self.assertIn(
            "AMBIGUOUS_SUPPLIER_FINGERPRINT",
            {item.code for item in result.alerts},
        )

    def test_greedy_solver_is_protocol_compatible_and_never_certifies_claims(self) -> None:
        snapshot = load_snapshot(SCENARIOS / "scenario_01_baseline.sqlite")
        ledgers = build_ledgers(snapshot)
        candidates = build_candidate_routes(snapshot, ledgers)
        component = next(item for item in snapshot.components if item.name.startswith("Copper Wire"))
        ledger = ledgers.ledger_for(component.component_id)
        problem = GreedyProblem(
            component_id=component.component_id,
            unit_of_measure=component.unit_of_measure,
            net_requirement=ledger.eventual_gap,
            routes=candidates.routes_for(component.component_id),
            demand_buckets=ledgers.buckets_for(component.component_id),
            suppliers=snapshot.suppliers,
        )
        solver = GreedySolver()
        self.assertIsInstance(solver, Solver)
        result = solver.solve(problem)
        self.assertIs(result.status, SolverStatus.UNRESOLVED)
        self.assertFalse(result.is_certified_optimal)
        self.assertFalse(result.has_executable_certificate)
        self.assertTrue(all(item.status is not SolverStatus.INFEASIBLE for item in result.stage_results))
        if result.candidate_plan is not None:
            self.assertIs(
                result.candidate_plan.disposition, PlanDisposition.DECISION_REQUIRED
            )
            self.assertEqual(result.candidate_plan.relaxed_rule_ids, ())
            route_by_id = {item.route_id: item for item in problem.routes}
            self.assertTrue(
                all(
                    not route_by_id[line.route_id].exception_codes
                    for line in result.candidate_plan.lines
                )
            )


if __name__ == "__main__":
    unittest.main()
