from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import builtins
import sys
import unittest

from apex_procurement.config import EvidenceContract
from apex_procurement.domain import (
    AlertCategory,
    BomLine,
    BucketAllocation,
    CandidatePlan,
    Component,
    DeadlineSupplyPosition,
    DecisionRecord,
    DemandBucket,
    DemandContribution,
    EvidenceBasis,
    EvidenceResult,
    EvidenceScope,
    EvidenceStatus,
    ExistingPurchaseOrder,
    FulfillmentStatus,
    InboundSupply,
    PlanDisposition,
    PlanLine,
    ProductionOrder,
    RequirementState,
    ResolutionStatus,
    RuleSeverity,
    ScenarioConfiguration,
    ScenarioSnapshot,
    SolveKind,
    SolverResult,
    SolverStageResult,
    SolverStatus,
    Supplier,
    SupplierCatalogLine,
    SupplyLedger,
)
from apex_procurement.validator import (
    _EXECUTABLE_OBJECTIVE_SUFFIX,
    IndependentPlanValidator,
    NamedEntityOutcome,
)


CURRENT = date(2025, 2, 1)
DUE = date(2025, 2, 20)


def _supplier(
    supplier_id: str = "supplier-a",
    *,
    name: str = "Generated Strategic Supply",
    certifications: tuple[str, ...] = ("ISO-9001",),
    price_rating: str = "A",
) -> Supplier:
    return Supplier(
        supplier_id=supplier_id,
        name=name,
        country="USA",
        is_domestic=True,
        certifications=certifications,
        sustainability_rating=price_rating,
        relationship_tier="Strategic",
        on_approved_list=True,
    )


def _snapshot(
    *,
    demand: Decimal = Decimal("5"),
    on_hand: Decimal = Decimal("0"),
    moq: Decimal = Decimal("1"),
    suppliers: tuple[Supplier, ...] | None = None,
    catalogs: tuple[SupplierCatalogLine, ...] | None = None,
    component: Component | None = None,
    current: date = CURRENT,
) -> ScenarioSnapshot:
    component = component or Component(
        component_id="component-a",
        name="Generated General Bracket",
        description="A general bracket",
        category="Raw Material",
        unit_of_measure="each",
        is_hazardous=False,
    )
    if suppliers is None:
        suppliers = (_supplier(),)
    if catalogs is None:
        catalogs = tuple(
            SupplierCatalogLine(
                supplier_id=item.supplier_id,
                component_id=component.component_id,
                unit_price=Decimal("2") + Decimal(index),
                lead_time_days=3,
                minimum_order_quantity=moq,
            )
            for index, item in enumerate(suppliers)
        )
    from apex_procurement.domain import InventoryPosition, Product

    return ScenarioSnapshot(
        configuration=ScenarioConfiguration(current),
        products=(Product("product-a", "Generated Product", None, "Generated", None),),
        components=(component,),
        suppliers=suppliers,
        bom_lines=(BomLine("product-a", component.component_id, Decimal("1")),),
        catalog_lines=catalogs,
        production_orders=(ProductionOrder("order-a", "product-a", demand, None, DUE),),
        inventory=(InventoryPosition(component.component_id, on_hand),),
        purchase_orders=(),
        alerts=(),
        state_digest="validator-test",
    )


def _decision_and_results(
    snapshot: ScenarioSnapshot,
    *,
    quantity: Decimal | None = None,
    disposition: PlanDisposition = PlanDisposition.EXECUTE_WITH_ASSUMPTION,
) -> tuple[DecisionRecord, tuple[SolverResult, ...], IndependentPlanValidator]:
    validator = IndependentPlanValidator()
    component = snapshot.components[0]
    catalog = snapshot.catalog_lines[0]
    demand = snapshot.production_orders[0].quantity
    on_hand = snapshot.inventory[0].quantity_on_hand
    gap = max(Decimal("0"), demand - on_hand)
    quantity = quantity if quantity is not None else max(gap, catalog.minimum_order_quantity)
    expected = snapshot.configuration.current_date + timedelta(days=catalog.lead_time_days)
    line = PlanLine(
        route_id="route-standard-a",
        component_id=component.component_id,
        supplier_id=catalog.supplier_id,
        quantity=quantity,
        unit_price=catalog.unit_price,
        order_date=snapshot.configuration.current_date,
        expected_delivery_date=expected,
        material_available_date=expected,
        allocation_group_id="group-component-a",
        bucket_allocations=(BucketAllocation(DUE, quantity),),
    )
    rolling = EvidenceResult(
        rule_id="POL-PROC-001.section_4.noncritical_cap",
        status=EvidenceStatus.UNKNOWN,
        basis=EvidenceBasis.ROLLING_WINDOW,
        scope=EvidenceScope.RULE,
        severity=RuleSeverity.HARD,
        summary="Rolling history is absent under the benchmark contract.",
        source_references=("POL-PROC-001",),
        assumption_codes=("ROLLING_HISTORY_UNKNOWN",),
        contract_disposition=PlanDisposition.EXECUTE_WITH_ASSUMPTION,
    )
    covered_by_plan = min(gap, quantity)
    plan = CandidatePlan(
        plan_id="selected-plan",
        component_id=component.component_id,
        disposition=disposition,
        lines=(line,),
        net_requirement=gap,
        eventual_covered_quantity=covered_by_plan,
        residual_gap=gap - covered_by_plan,
        total_cost=line.line_total,
        minimum_compliant_total=quantity,
        cheapest_covering_cost=quantity * catalog.unit_price,
        forced_surplus=max(Decimal("0"), quantity - gap),
        discretionary_surplus=Decimal("0"),
        evidence=(rolling,),
        assumption_codes=("ROLLING_HISTORY_UNKNOWN",),
        summary="Validated source-backed plan",
    )
    bucket = DemandBucket(
        component.component_id,
        DUE,
        demand,
        demand,
        (DemandContribution("order-a", "product-a", demand),),
    )
    ledger = SupplyLedger(
        component.component_id,
        demand,
        on_hand,
        (),
        on_hand,
        gap,
        (
            DeadlineSupplyPosition(
                DUE,
                demand,
                on_hand,
                max(Decimal("0"), demand - on_hand),
                max(Decimal("0"), demand - on_hand),
            ),
        ),
    )
    residual = gap - covered_by_plan
    covered = demand - residual
    state = RequirementState(
        FulfillmentStatus.FULFILLED if residual == 0 else FulfillmentStatus.PARTIALLY_FULFILLED,
        ResolutionStatus.RESOLVED if residual == 0 else ResolutionStatus.UNRESOLVED,
    )
    decision = DecisionRecord(
        requirement_id="requirement-a",
        component_id=component.component_id,
        evidence_contract=EvidenceContract.BENCHMARK,
        demand_buckets=(bucket,),
        supply_ledger=ledger,
        total_requirement=demand,
        initial_eventual_gap=gap,
        covered_quantity=covered,
        residual_gap=residual,
        requirement_state=state,
        selected_plan=plan,
        alternatives=(),
        evidence=(rolling,),
        alert_categories=(AlertCategory.ASSUMPTION,),
        rationale="Selected under POL-PROC-001.section_4.noncritical_cap.",
    )
    requirement = validator._source_requirements(snapshot, type("Sink", (), {"error": lambda *args, **kwargs: None})())[component.component_id]
    offers = validator._offers(snapshot, requirement, EvidenceContract.BENCHMARK)
    objective = validator._objective(snapshot, requirement, plan, offers)
    plan = replace(plan, objective_vector=objective, unit_late_days=objective[1])
    decision = replace(decision, selected_plan=plan)
    q = validator.independently_solve(snapshot, decision, SolveKind.QUANTITY_CALIBRATION)
    baseline = validator.independently_solve(snapshot, decision, SolveKind.BASELINE)
    executable = validator.independently_solve(snapshot, decision, SolveKind.EXECUTABLE)

    def stage(name: str, value: Decimal | None) -> tuple[SolverStageResult, ...]:
        return (SolverStageResult(name, SolverStatus.OPTIMAL, value, Decimal("0"), True, False),)

    results = (
        SolverResult(
            component.component_id,
            SolveKind.QUANTITY_CALIBRATION,
            SolverStatus.OPTIMAL,
            stage("quantity-calibration", q.objective_vector[0]),
            None,
            q.objective_vector,
            minimum_compliant_total=q.minimum_compliant_total,
            exact_post_validated=True,
        ),
        SolverResult(
            component.component_id,
            SolveKind.BASELINE,
            SolverStatus.OPTIMAL,
            stage("baseline", baseline.objective_vector[0]),
            None,
            baseline.objective_vector,
            cheapest_covering_cost=baseline.cheapest_covering_cost,
            exact_post_validated=True,
        ),
        SolverResult(
            component.component_id,
            SolveKind.EXECUTABLE,
            SolverStatus.OPTIMAL,
            stage("executable", executable.objective_vector[0] if executable.objective_vector else None),
            plan,
            executable.objective_vector,
            minimum_compliant_total=q.minimum_compliant_total,
            cheapest_covering_cost=baseline.cheapest_covering_cost,
            exact_post_validated=True,
        ),
    )
    return decision, results, validator


def _codes(result) -> set[str]:
    return {item.code for item in result.issues}


class IndependentValidatorMutationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = _snapshot()
        self.decision, self.results, self.validator = _decision_and_results(self.snapshot)
        baseline = self.validator.validate(self.snapshot, (self.decision,), self.results)
        self.assertTrue(baseline.is_valid, baseline.issues)

    def validate_mutation(self, decision: DecisionRecord) -> set[str]:
        result = self.validator.validate(self.snapshot, (decision,), self.results)
        self.assertFalse(result.is_valid)
        return _codes(result)

    def test_supplier_mutation_is_caught(self) -> None:
        plan = self.decision.selected_plan
        assert plan is not None
        object.__setattr__(plan.lines[0], "supplier_id", "supplier-missing")
        self.assertIn("CATALOG_ROUTE_MISMATCH", self.validate_mutation(self.decision))

    def test_price_mutation_is_caught(self) -> None:
        plan = self.decision.selected_plan
        assert plan is not None
        object.__setattr__(plan.lines[0], "unit_price", Decimal("9"))
        self.assertIn("CATALOG_PRICE_MISMATCH", self.validate_mutation(self.decision))

    def test_moq_mutation_is_caught(self) -> None:
        snapshot = _snapshot(moq=Decimal("6"), demand=Decimal("5"))
        decision, results, validator = _decision_and_results(snapshot, quantity=Decimal("6"))
        assert decision.selected_plan is not None
        object.__setattr__(decision.selected_plan.lines[0], "quantity", Decimal("5"))
        result = validator.validate(snapshot, (decision,), results)
        self.assertIn("MOQ_VIOLATION", _codes(result))

    def test_date_mutation_is_caught(self) -> None:
        plan = self.decision.selected_plan
        assert plan is not None
        object.__setattr__(plan.lines[0], "expected_delivery_date", plan.lines[0].expected_delivery_date + timedelta(days=1))
        self.assertIn("DELIVERY_DATE_MISMATCH", self.validate_mutation(self.decision))

    def test_certification_mutation_is_caught(self) -> None:
        electronic = replace(
            self.snapshot.components[0],
            name="Generated Electronic IC",
            category="Electronic Component",
        )
        bad_supplier = replace(self.snapshot.suppliers[0], certifications=())
        snapshot = replace(self.snapshot, components=(electronic,), suppliers=(bad_supplier,))
        result = self.validator.validate(snapshot, (self.decision,), self.results)
        self.assertIn("SUPPLIER_INELIGIBLE", _codes(result))

    def test_cost_baseline_mutation_is_caught(self) -> None:
        plan = self.decision.selected_plan
        assert plan is not None
        object.__setattr__(plan, "cheapest_covering_cost", Decimal("99"))
        self.assertIn("BASELINE_COST_MISMATCH", self.validate_mutation(self.decision))

    def test_allocation_share_mutation_is_caught(self) -> None:
        component = Component(
            "magnet-component",
            "Generated NdFeB Magnet",
            None,
            "Raw Material",
            "each",
            False,
        )
        primary = _supplier(
            "SUP-107",
            name="Nanjing Rare Earth Co.",
            price_rating="B",
        )
        secondary = _supplier("supplier-secondary", name="Generated Secondary")
        current = date(2025, 6, 1)
        due = date(2025, 7, 1)
        catalogs = (
            SupplierCatalogLine(primary.supplier_id, component.component_id, Decimal("2"), 3, Decimal("1")),
            SupplierCatalogLine(secondary.supplier_id, component.component_id, Decimal("4"), 3, Decimal("1")),
        )
        snapshot = _snapshot(
            demand=Decimal("10"),
            component=component,
            suppliers=(primary, secondary),
            catalogs=catalogs,
            current=current,
        )
        # Align the generated source deadline with the active-memo case.
        snapshot = replace(
            snapshot,
            production_orders=(ProductionOrder("order-a", "product-a", Decimal("10"), None, due),),
        )
        lines = (
            PlanLine(
                "route-primary", component.component_id, primary.supplier_id,
                Decimal("9"), Decimal("2"), current, current + timedelta(days=3),
                current + timedelta(days=3), "magnet-group",
                (BucketAllocation(due, Decimal("9"), ("POL-PROC-001.section_3.critical_premium_threshold:condition_b",)),),
            ),
            PlanLine(
                "route-secondary", component.component_id, secondary.supplier_id,
                Decimal("1"), Decimal("4"), current, current + timedelta(days=3),
                current + timedelta(days=3), "magnet-group",
                (BucketAllocation(due, Decimal("1")),),
            ),
        )
        plan = CandidatePlan(
            "allocation-mutant", component.component_id, PlanDisposition.DECISION_REQUIRED,
            lines, Decimal("10"), Decimal("10"), Decimal("0"), Decimal("22"),
        )
        bucket = DemandBucket(component.component_id, due, Decimal("10"), Decimal("10"), (DemandContribution("order-a", "product-a", Decimal("10")),))
        ledger = SupplyLedger(component.component_id, Decimal("10"), Decimal("0"), (), Decimal("0"), Decimal("10"), (DeadlineSupplyPosition(due, Decimal("10"), Decimal("0"), Decimal("10"), Decimal("10")),))
        decision = DecisionRecord(
            "requirement-magnet", component.component_id, EvidenceContract.BENCHMARK,
            (bucket,), ledger, Decimal("10"), Decimal("10"), Decimal("0"), Decimal("10"),
            RequirementState(FulfillmentStatus.UNFULFILLED, ResolutionStatus.UNRESOLVED),
            None, (plan,), (), (AlertCategory.DECISION_REQUIRED,),
            "Allocation reviewed under MEMO-2025-041.magnet_secondary_allocation.",
        )
        result = IndependentPlanValidator().validate(snapshot, (decision,), ())
        self.assertIn("SECONDARY_ALLOCATION_VIOLATION", _codes(result))

    def test_surplus_mutation_is_caught(self) -> None:
        plan = self.decision.selected_plan
        assert plan is not None
        object.__setattr__(plan, "forced_surplus", Decimal("1"))
        self.assertIn("SURPLUS_SPLIT_MISMATCH", self.validate_mutation(self.decision))

    def test_disposition_mutation_is_caught(self) -> None:
        plan = self.decision.selected_plan
        assert plan is not None
        object.__setattr__(plan, "disposition", PlanDisposition.EXECUTE)
        self.assertIn("EVIDENCE_CONTRACT_DISPOSITION", self.validate_mutation(self.decision))

    def test_requirement_state_mutation_is_caught(self) -> None:
        object.__setattr__(
            self.decision,
            "requirement_state",
            RequirementState(FulfillmentStatus.UNFULFILLED, ResolutionStatus.UNRESOLVED),
        )
        self.assertIn("REQUIREMENT_STATE_MISMATCH", self.validate_mutation(self.decision))

    def test_every_executable_objective_field_mutation_is_caught(self) -> None:
        plan = self.decision.selected_plan
        assert plan is not None
        labels = ("stage_01_unresolved",) + _EXECUTABLE_OBJECTIVE_SUFFIX
        self.assertEqual(len(plan.objective_vector), len(labels))
        for index, label in enumerate(labels):
            with self.subTest(label=label):
                values = list(plan.objective_vector)
                values[index] += Decimal("1")
                mutated = replace(plan, objective_vector=tuple(values))
                decision = replace(self.decision, selected_plan=mutated)
                self.assertIn(
                    "OBJECTIVE_VECTOR_MISMATCH",
                    self.validate_mutation(decision),
                )

    def test_total_quantity_cannot_replace_stage_9_moq_excess(self) -> None:
        plan = self.decision.selected_plan
        assert plan is not None
        index = 1 + _EXECUTABLE_OBJECTIVE_SUFFIX.index("stage_09_moq_excess")
        total_quantity = sum((line.quantity for line in plan.lines), Decimal("0"))
        self.assertNotEqual(plan.objective_vector[index], total_quantity)
        values = list(plan.objective_vector)
        values[index] = total_quantity
        mutated = replace(plan, objective_vector=tuple(values))
        decision = replace(self.decision, selected_plan=mutated)
        self.assertIn("OBJECTIVE_VECTOR_MISMATCH", self.validate_mutation(decision))

    def test_feasible_but_suboptimal_incumbent_is_rejected(self) -> None:
        second = _supplier("supplier-b", name="Generated Cheap Supply")
        snapshot = _snapshot(
            suppliers=(self.snapshot.suppliers[0], second),
            catalogs=(
                replace(self.snapshot.catalog_lines[0], unit_price=Decimal("9")),
                SupplierCatalogLine("supplier-b", "component-a", Decimal("1"), 3, Decimal("1")),
            ),
        )
        decision, results, validator = _decision_and_results(snapshot)
        result = validator.validate(snapshot, (decision,), results)
        self.assertIn("SUBOPTIMAL_INCUMBENT", _codes(result))


class IndependentValidatorEdgeTests(unittest.TestCase):
    def test_represented_sub_moq_proposal_may_share_the_selected_route(self) -> None:
        snapshot = _snapshot(demand=Decimal("1"), moq=Decimal("5"))
        decision, results, validator = _decision_and_results(
            snapshot,
            quantity=Decimal("5"),
        )
        selected = decision.selected_plan
        assert selected is not None
        sub_moq_rule = validator._rules(
            snapshot.configuration.current_date,
            "sub_moq_written_approval",
        )[0].rule_id
        line = replace(
            selected.lines[0],
            quantity=Decimal("1"),
            bucket_allocations=(BucketAllocation(DUE, Decimal("1")),),
        )
        proposal = replace(
            selected,
            plan_id="sub-moq-proposal",
            disposition=PlanDisposition.RECOMMEND_APPROVAL,
            lines=(line,),
            total_cost=line.line_total,
            minimum_compliant_total=None,
            cheapest_covering_cost=None,
            forced_surplus=Decimal("0"),
            relaxed_rule_ids=(sub_moq_rule,),
            unresolved_approval_ids=(sub_moq_rule,),
            summary="Sub-MOQ proposal awaiting written supplier approval.",
        )
        requirement = validator._source_requirements(
            snapshot,
            type("Sink", (), {"error": lambda *args, **kwargs: None})(),
        )[decision.component_id]
        offers = validator._offers(
            snapshot,
            requirement,
            decision.evidence_contract,
            include_unapproved=True,
        )
        proposal = replace(
            proposal,
            objective_vector=validator._objective(
                snapshot,
                requirement,
                proposal,
                offers,
            ),
        )
        result = validator.validate(
            snapshot,
            (replace(decision, alternatives=(proposal,)),),
            results,
        )
        self.assertNotIn("MOQ_VIOLATION", _codes(result))
        self.assertNotIn("DUPLICATE_ACTION", _codes(result))
        self.assertTrue(result.is_valid, result.issues)

    def test_forced_surplus_is_not_ratio_gated(self) -> None:
        snapshot = _snapshot(demand=Decimal("1"), moq=Decimal("5"))
        decision, results, validator = _decision_and_results(snapshot, quantity=Decimal("5"))
        result = validator.validate(snapshot, (decision,), results)
        self.assertTrue(result.is_valid, result.issues)
        assert decision.selected_plan is not None
        self.assertEqual(decision.selected_plan.forced_surplus, Decimal("4"))
        self.assertEqual(decision.selected_plan.discretionary_surplus, Decimal("0"))

    def test_rejecting_only_for_forced_surplus_is_a_validator_failure(self) -> None:
        snapshot = _snapshot(demand=Decimal("1"), moq=Decimal("5"))
        decision, _results, validator = _decision_and_results(snapshot, quantity=Decimal("5"))
        assert decision.selected_plan is not None
        alternative = replace(
            decision.selected_plan,
            disposition=PlanDisposition.DECISION_REQUIRED,
        )
        object.__setattr__(decision, "selected_plan", None)
        object.__setattr__(decision, "alternatives", (alternative,))
        object.__setattr__(decision, "covered_quantity", Decimal("0"))
        object.__setattr__(decision, "residual_gap", Decimal("1"))
        object.__setattr__(decision, "requirement_state", RequirementState(FulfillmentStatus.UNFULFILLED, ResolutionStatus.UNRESOLVED))
        object.__setattr__(decision, "alert_categories", (AlertCategory.DECISION_REQUIRED, AlertCategory.FORCED_SURPLUS))
        result = validator.validate(snapshot, (decision,), ())
        self.assertIn("FORCED_SURPLUS_MISCLASSIFIED", _codes(result))

    def test_partial_physical_coverage_may_be_infeasible(self) -> None:
        snapshot = _snapshot(demand=Decimal("10"), on_hand=Decimal("4"), suppliers=(), catalogs=())
        component = snapshot.components[0]
        bucket = DemandBucket(component.component_id, DUE, Decimal("10"), Decimal("10"), (DemandContribution("order-a", "product-a", Decimal("10")),))
        ledger = SupplyLedger(component.component_id, Decimal("10"), Decimal("4"), (), Decimal("4"), Decimal("6"), (DeadlineSupplyPosition(DUE, Decimal("10"), Decimal("4"), Decimal("6"), Decimal("0")),))
        decision = DecisionRecord(
            "requirement-a", component.component_id, EvidenceContract.BENCHMARK,
            (bucket,), ledger, Decimal("10"), Decimal("6"), Decimal("4"), Decimal("6"),
            RequirementState(FulfillmentStatus.PARTIALLY_FULFILLED, ResolutionStatus.INFEASIBLE),
            None, (), (), (AlertCategory.NO_ELIGIBLE_SUPPLIER,),
            "No route is eligible under POL-PROC-001.section_2.approved_supplier.",
        )
        infeasible = SolverResult(
            component.component_id,
            SolveKind.EXECUTABLE,
            SolverStatus.INFEASIBLE,
            (SolverStageResult("feasibility", SolverStatus.INFEASIBLE, None, None, True, False),),
            None,
            (),
            exact_post_validated=True,
        )
        result = IndependentPlanValidator().validate(snapshot, (decision,), (infeasible,))
        self.assertTrue(result.is_valid, result.issues)

    def test_timeout_cannot_claim_infeasible(self) -> None:
        snapshot = _snapshot(demand=Decimal("10"), on_hand=Decimal("4"), suppliers=(), catalogs=())
        component = snapshot.components[0]
        bucket = DemandBucket(component.component_id, DUE, Decimal("10"), Decimal("10"), (DemandContribution("order-a", "product-a", Decimal("10")),))
        ledger = SupplyLedger(component.component_id, Decimal("10"), Decimal("4"), (), Decimal("4"), Decimal("6"), (DeadlineSupplyPosition(DUE, Decimal("10"), Decimal("4"), Decimal("6"), Decimal("0")),))
        decision = DecisionRecord(
            "requirement-a", component.component_id, EvidenceContract.BENCHMARK,
            (bucket,), ledger, Decimal("10"), Decimal("6"), Decimal("4"), Decimal("6"),
            RequirementState(FulfillmentStatus.PARTIALLY_FULFILLED, ResolutionStatus.INFEASIBLE),
            None, (), (), (AlertCategory.SOLVER_UNPROVEN,),
            "Solver status is unproven under POL-PROC-001.section_2.approved_supplier.",
        )
        timed = SolverResult(
            component.component_id,
            SolveKind.EXECUTABLE,
            SolverStatus.TIMEOUT,
            (SolverStageResult("stage-1", SolverStatus.TIMEOUT, None, None, False, True),),
            None,
            (),
        )
        result = IndependentPlanValidator().validate(snapshot, (decision,), (timed,))
        self.assertIn("UNPROVEN_INFEASIBILITY", _codes(result))

    def test_four_case_named_entity_ladder(self) -> None:
        validator = IndependentPlanValidator()
        supplier = _supplier("source-one", name="Exact Legal Name")
        same = validator.resolve_source_named_entity({"source_id": "source-one", "legal_name": "Exact Legal Name"}, (supplier,))
        stale = validator.resolve_source_named_entity({"source_id": "stale", "legal_name": "Exact Legal Name"}, (supplier,))
        conflict = validator.resolve_source_named_entity(
            {"source_id": "source-one", "legal_name": "Other Legal Name"},
            (supplier, _supplier("source-two", name="Other Legal Name")),
        )
        missing = validator.resolve_source_named_entity({"source_id": "missing", "legal_name": "Missing"}, (supplier,))
        self.assertEqual(same.outcome, NamedEntityOutcome.RESOLVED)
        self.assertEqual(stale.outcome, NamedEntityOutcome.STALE_SOURCE_ID)
        self.assertEqual(conflict.outcome, NamedEntityOutcome.CONFLICT)
        self.assertEqual(missing.outcome, NamedEntityOutcome.MISSING_OR_AMBIGUOUS)

    def test_module_import_and_validation_never_import_optimizer(self) -> None:
        sys.modules.pop("apex_procurement.optimizer", None)
        original = builtins.__import__

        def guarded(name, *args, **kwargs):
            if name == "apex_procurement.optimizer" or name.endswith(".optimizer"):
                raise AssertionError("validator imported optimizer implementation")
            return original(name, *args, **kwargs)

        builtins.__import__ = guarded
        try:
            snapshot = _snapshot()
            decision, results, validator = _decision_and_results(snapshot)
            self.assertTrue(validator.validate(snapshot, (decision,), results).is_valid)
        finally:
            builtins.__import__ = original


class IndependentInvariantRecomputationTests(unittest.TestCase):
    def test_aggregate_critical_inference_remains_unknown_after_child_negative(self) -> None:
        component = Component(
            "component-pcb",
            "PCB Assembly (6-layer)",
            None,
            "Electronic Component",
            "each",
            False,
        )
        self.assertEqual(
            IndependentPlanValidator()._concept("critical_component", component),
            EvidenceStatus.UNKNOWN,
        )

    def test_fractional_increment_executable_skips_redundant_calibration_sweep(self) -> None:
        component = Component(
            "component-liquid",
            "Generated Liquid",
            None,
            "Raw Material",
            "kg",
            False,
        )
        first = _supplier("supplier-a", name="Generated First Supply")
        second = _supplier("supplier-b", name="Generated Second Supply")
        snapshot = _snapshot(
            demand=Decimal("5"),
            component=component,
            suppliers=(first, second),
            catalogs=(
                SupplierCatalogLine(
                    first.supplier_id,
                    component.component_id,
                    Decimal("9.5"),
                    3,
                    Decimal("25"),
                ),
                SupplierCatalogLine(
                    second.supplier_id,
                    component.component_id,
                    Decimal("8.75"),
                    3,
                    Decimal("50"),
                ),
            ),
        )
        decision, _results, _validator = _decision_and_results(
            snapshot,
            quantity=Decimal("25"),
        )
        result = IndependentPlanValidator(
            enumeration_node_limit=10_000
        ).independently_solve(snapshot, decision, SolveKind.EXECUTABLE)
        self.assertEqual(result.status, SolverStatus.OPTIMAL)
        self.assertTrue(result.certificate_complete)

    def test_complete_objective_schema_has_every_documented_subobjective(self) -> None:
        snapshot = _snapshot()
        decision, _results, validator = _decision_and_results(snapshot)
        plan = decision.selected_plan
        assert plan is not None
        self.assertEqual(
            plan.objective_vector,
            (
                Decimal("0"),   # stage 1 deadline gap
                Decimal("0"),   # stage 2 unit-late-days
                Decimal("0"),   # stage 3 discretionary surplus
                Decimal("1"),   # stage 4 review exposure
                Decimal("0"),   # stage 5 named-primary deviation
                Decimal("0"),   # stage 6 international volume
                Decimal("0"),   # stage 7 strategic shift
                Decimal("0"),   # stage 8 sustainability band
                Decimal("10"),  # stage 9 known landed cost
                Decimal("0"),   # stage 9 MOQ-driven excess
                Decimal("15"),  # stage 10 total lead time
                Decimal("1"),   # stage 10 line count
            ),
        )

    def test_stage_4_counts_normalized_assumption_codes_not_reviewed_rules(self) -> None:
        component = Component(
            "component-review",
            "Pressure Transducer",
            "0-100 PSI sensor",
            "Electronic Component",
            "each",
            False,
        )
        snapshot = _snapshot(component=component)
        decision, _results, _validator = _decision_and_results(snapshot)
        plan = decision.selected_plan
        assert plan is not None
        self.assertEqual(plan.objective_vector[3], Decimal("3"))

    def test_stage_10_semantic_tie_excludes_surrogate_supplier_ids(self) -> None:
        first = _supplier("supplier-first", name="Generated Alpha Supply")
        second = _supplier("supplier-second", name="Generated Beta Supply")
        catalogs = (
            SupplierCatalogLine(first.supplier_id, "component-a", Decimal("2"), 3, Decimal("1")),
            SupplierCatalogLine(second.supplier_id, "component-a", Decimal("2"), 3, Decimal("1")),
        )
        snapshot = _snapshot(suppliers=(first, second), catalogs=catalogs)
        decision, _results, validator = _decision_and_results(snapshot)
        plan = decision.selected_plan
        assert plan is not None
        requirement = validator._source_requirements(
            snapshot, type("Sink", (), {"error": lambda *args, **kwargs: None})()
        )["component-a"]
        offers = validator._offers(snapshot, requirement, EvidenceContract.BENCHMARK)
        original_solve = validator.independently_solve(
            snapshot, decision, SolveKind.EXECUTABLE
        )
        self.assertEqual(
            original_solve.allocation,
            ((offers[0].supplier.supplier_id, Decimal("5")),),
        )

        renamed_first = replace(first, supplier_id="renamed-first")
        renamed_second = replace(second, supplier_id="renamed-second")
        renamed = _snapshot(
            suppliers=(renamed_first, renamed_second),
            catalogs=(
                replace(catalogs[0], supplier_id=renamed_first.supplier_id),
                replace(catalogs[1], supplier_id=renamed_second.supplier_id),
            ),
        )
        renamed_decision, _renamed_results, _renamed_validator = _decision_and_results(renamed)
        assert renamed_decision.selected_plan is not None
        renamed_solve = _renamed_validator.independently_solve(
            renamed, renamed_decision, SolveKind.EXECUTABLE
        )
        renamed_requirement = _renamed_validator._source_requirements(
            renamed, type("Sink", (), {"error": lambda *args, **kwargs: None})()
        )["component-a"]
        renamed_offers = _renamed_validator._offers(
            renamed, renamed_requirement, EvidenceContract.BENCHMARK
        )
        self.assertEqual(
            renamed_decision.selected_plan.objective_vector,
            plan.objective_vector,
        )
        self.assertEqual(
            renamed_solve.allocation,
            ((renamed_offers[0].supplier.supplier_id, Decimal("5")),),
        )
        names = {supplier.supplier_id: supplier.name for supplier in snapshot.suppliers}
        renamed_names = {
            supplier.supplier_id: supplier.name for supplier in renamed.suppliers
        }
        self.assertEqual(
            names[original_solve.allocation[0][0]],
            renamed_names[renamed_solve.allocation[0][0]],
        )

    def test_larger_quantity_case_completes_all_three_independent_solves(self) -> None:
        second = _supplier("supplier-b", name="Generated Alternate")
        snapshot = _snapshot(
            demand=Decimal("250"),
            suppliers=(_supplier(), second),
            catalogs=(
                SupplierCatalogLine("supplier-a", "component-a", Decimal("2"), 3, Decimal("1")),
                SupplierCatalogLine("supplier-b", "component-a", Decimal("3"), 3, Decimal("1")),
            ),
        )
        decision, _claims, validator = _decision_and_results(snapshot)
        for kind in (SolveKind.QUANTITY_CALIBRATION, SolveKind.BASELINE, SolveKind.EXECUTABLE):
            with self.subTest(kind=kind):
                result = validator.independently_solve(snapshot, decision, kind)
                self.assertEqual(result.status, SolverStatus.OPTIMAL)
                self.assertTrue(result.certificate_complete)
                self.assertTrue(result.objective_vector)

    def test_inbound_inclusion_uses_delivery_date_not_old_order_date(self) -> None:
        snapshot = _snapshot(demand=Decimal("5"))
        future = CURRENT + timedelta(days=4)
        snapshot = replace(
            snapshot,
            purchase_orders=(
                ExistingPurchaseOrder(
                    "external-po", "component-a", "supplier-a", Decimal("2"),
                    Decimal("2"), CURRENT - timedelta(days=30), future, "external",
                ),
            ),
        )
        decision, results, validator = _decision_and_results(_snapshot(demand=Decimal("5")))
        result = validator.validate(snapshot, (decision,), results)
        self.assertIn("INBOUND_DATE_INCLUSION_MISMATCH", _codes(result))

    def test_no_silent_gap_even_after_partial_selected_plan(self) -> None:
        snapshot = _snapshot(demand=Decimal("10"))
        decision, _results, validator = _decision_and_results(snapshot, quantity=Decimal("5"))
        object.__setattr__(decision, "alert_categories", (AlertCategory.UNMET_DEMAND,))
        result = validator.validate(snapshot, (decision,), ())
        self.assertIn("SILENT_RESIDUAL_GAP", _codes(result))

    def test_unresolved_shaping_subject_drops_directive_and_requires_conflict_alert(self) -> None:
        component = Component(
            "magnet-component", "Generated NdFeB Magnet", None,
            "Raw Material", "each", False,
        )
        current = date(2025, 6, 1)
        due = date(2025, 7, 1)
        snapshot = _snapshot(component=component, current=current)
        snapshot = replace(
            snapshot,
            production_orders=(ProductionOrder("order-a", "product-a", Decimal("5"), None, due),),
        )
        decision, results, validator = _decision_and_results(snapshot)
        result = validator.validate(snapshot, (decision,), results)
        self.assertIn("SHAPING_DEGRADATION_MISSING", _codes(result))
        object.__setattr__(
            decision,
            "alert_categories",
            tuple(sorted((*decision.alert_categories, AlertCategory.POLICY_CONFLICT), key=lambda item: item.value)),
        )
        result = validator.validate(snapshot, (decision,), results)
        self.assertNotIn("SHAPING_DEGRADATION_MISSING", _codes(result))

    def test_capacity_unknown_is_scoped_to_positive_release_subject_allocation(self) -> None:
        component = Component(
            "magnet-component", "Generated NdFeB Magnet", None,
            "Raw Material", "each", False,
        )
        subject = _supplier(
            "SUP-108", name="MagnetPro Inc.", price_rating="B"
        )
        current = date(2025, 6, 1)
        due = date(2025, 7, 1)
        snapshot = _snapshot(component=component, suppliers=(subject,), current=current)
        snapshot = replace(
            snapshot,
            production_orders=(ProductionOrder("order-a", "product-a", Decimal("5"), None, due),),
        )
        decision, results, validator = _decision_and_results(snapshot)
        assert decision.selected_plan is not None
        allocation = decision.selected_plan.lines[0].bucket_allocations[0]
        object.__setattr__(
            allocation,
            "exception_ids",
            ("POL-PROC-001.section_3.critical_premium_threshold:condition_c",),
        )
        result = validator.validate(snapshot, (decision,), results)
        self.assertIn("CAPACITY_UNKNOWN_MISSING", _codes(result))
        object.__setattr__(
            decision,
            "alert_categories",
            tuple(sorted((*decision.alert_categories, AlertCategory.CAPACITY_UNKNOWN), key=lambda item: item.value)),
        )
        result = validator.validate(snapshot, (decision,), results)
        self.assertNotIn("CAPACITY_UNKNOWN_MISSING", _codes(result))

        selected = decision.selected_plan
        assert selected is not None
        object.__setattr__(decision, "selected_plan", None)
        object.__setattr__(decision, "alternatives", (replace(selected, disposition=PlanDisposition.DECISION_REQUIRED),))
        object.__setattr__(decision, "covered_quantity", Decimal("0"))
        object.__setattr__(decision, "residual_gap", Decimal("5"))
        object.__setattr__(decision, "requirement_state", RequirementState(FulfillmentStatus.UNFULFILLED, ResolutionStatus.UNRESOLVED))
        object.__setattr__(decision, "alert_categories", (AlertCategory.CAPACITY_UNKNOWN, AlertCategory.RUN_ACCOUNTING))
        result = validator.validate(snapshot, (decision,), ())
        self.assertIn("CAPACITY_UNKNOWN_DISPOSITIVE", _codes(result))

        generic_snapshot = _snapshot()
        generic_decision, generic_results, generic_validator = _decision_and_results(generic_snapshot)
        object.__setattr__(
            generic_decision,
            "alert_categories",
            tuple(sorted((*generic_decision.alert_categories, AlertCategory.CAPACITY_UNKNOWN), key=lambda item: item.value)),
        )
        generic = generic_validator.validate(generic_snapshot, (generic_decision,), generic_results)
        self.assertIn("CAPACITY_UNKNOWN_UNSCOPED", _codes(generic))

    def test_comparator_2_skips_only_premium_condition(self) -> None:
        international = replace(
            _supplier("supplier-international", name="Generated International"),
            country="China", is_domestic=False, relationship_tier="Standard",
        )
        domestic = replace(
            _supplier("supplier-domestic", name="Generated Domestic"),
            relationship_tier="Standard",
        )
        catalogs_b = (
            SupplierCatalogLine(international.supplier_id, "component-a", Decimal("2"), 3, Decimal("1")),
            SupplierCatalogLine(domestic.supplier_id, "component-a", Decimal("4"), 3, Decimal("1")),
        )
        snapshot_b = _snapshot(suppliers=(international, domestic), catalogs=catalogs_b)
        decision_b, _results, validator_b = _decision_and_results(snapshot_b)
        assert decision_b.selected_plan is not None
        line_b = decision_b.selected_plan.lines[0]
        object.__setattr__(line_b, "supplier_id", international.supplier_id)
        object.__setattr__(line_b, "unit_price", Decimal("2"))
        object.__setattr__(line_b, "expected_delivery_date", CURRENT + timedelta(days=3))
        object.__setattr__(line_b, "material_available_date", CURRENT + timedelta(days=3))
        object.__setattr__(decision_b.selected_plan, "total_cost", Decimal("10"))
        requirement_b = validator_b._source_requirements(snapshot_b, type("Sink", (), {"error": lambda *args, **kwargs: None})())["component-a"]
        objective_b = validator_b._objective(snapshot_b, requirement_b, decision_b.selected_plan, validator_b._offers(snapshot_b, requirement_b, EvidenceContract.BENCHMARK))
        self.assertEqual(objective_b[5], Decimal("0"))
        missing = validator_b.validate(snapshot_b, (decision_b,), ())
        self.assertIn("INTERNATIONAL_JUSTIFICATION_MISSING", _codes(missing))
        allocation_b = line_b.bucket_allocations[0]
        object.__setattr__(allocation_b, "exception_ids", ("POL-PROC-001.section_3.domestic_preference:condition_b",))
        object.__setattr__(allocation_b, "quantity", Decimal("6"))
        object.__setattr__(line_b, "quantity", Decimal("6"))
        object.__setattr__(decision_b.selected_plan, "total_cost", Decimal("12"))
        capped = validator_b.validate(snapshot_b, (decision_b,), ())
        self.assertIn("EXCEPTION_AGGREGATE_CAP", _codes(capped))

        catalogs_a = (
            SupplierCatalogLine(international.supplier_id, "component-a", Decimal("3.5"), 3, Decimal("1")),
            SupplierCatalogLine(domestic.supplier_id, "component-a", Decimal("4"), 30, Decimal("1")),
        )
        snapshot_a = _snapshot(suppliers=(international, domestic), catalogs=catalogs_a)
        decision_a, _results, validator_a = _decision_and_results(snapshot_a)
        assert decision_a.selected_plan is not None
        line_a = decision_a.selected_plan.lines[0]
        object.__setattr__(line_a, "supplier_id", international.supplier_id)
        object.__setattr__(line_a, "unit_price", Decimal("3.5"))
        object.__setattr__(line_a, "expected_delivery_date", CURRENT + timedelta(days=3))
        object.__setattr__(line_a, "material_available_date", CURRENT + timedelta(days=3))
        object.__setattr__(decision_a.selected_plan, "total_cost", Decimal("17.5"))
        requirement_a = validator_a._source_requirements(snapshot_a, type("Sink", (), {"error": lambda *args, **kwargs: None})())["component-a"]
        objective_a = validator_a._objective(snapshot_a, requirement_a, decision_a.selected_plan, validator_a._offers(snapshot_a, requirement_a, EvidenceContract.BENCHMARK))
        self.assertEqual(objective_a[5], Decimal("5"))

    def test_stage_7_and_8_policy_windows_are_inclusive_and_conditional(self) -> None:
        standard_b = replace(
            _supplier("supplier-standard", name="Generated Standard", price_rating="B"),
            relationship_tier="Standard",
        )
        strategic_a = _supplier("supplier-strategic", name="Generated Strategic", price_rating="A")
        within = _snapshot(
            suppliers=(standard_b, strategic_a),
            catalogs=(
                SupplierCatalogLine(standard_b.supplier_id, "component-a", Decimal("8.5"), 3, Decimal("1")),
                SupplierCatalogLine(strategic_a.supplier_id, "component-a", Decimal("10"), 3, Decimal("1")),
            ),
        )
        decision, _results, validator = _decision_and_results(within)
        assert decision.selected_plan is not None
        requirement = validator._source_requirements(within, type("Sink", (), {"error": lambda *args, **kwargs: None})())["component-a"]
        objective = validator._objective(within, requirement, decision.selected_plan, validator._offers(within, requirement, EvidenceContract.BENCHMARK))
        self.assertEqual(objective[6], Decimal("5"))  # exactly 15% strategic window

        sustainability = _snapshot(
            suppliers=(standard_b, replace(strategic_a, relationship_tier="Standard")),
            catalogs=(
                SupplierCatalogLine(standard_b.supplier_id, "component-a", Decimal("10"), 3, Decimal("1")),
                SupplierCatalogLine(strategic_a.supplier_id, "component-a", Decimal("11"), 3, Decimal("1")),
            ),
        )
        decision, _results, validator = _decision_and_results(sustainability)
        assert decision.selected_plan is not None
        requirement = validator._source_requirements(sustainability, type("Sink", (), {"error": lambda *args, **kwargs: None})())["component-a"]
        objective = validator._objective(sustainability, requirement, decision.selected_plan, validator._offers(sustainability, requirement, EvidenceContract.BENCHMARK))
        self.assertEqual(objective[7], Decimal("5"))  # exactly 10%, same delivery date

        outside = replace(
            sustainability,
            catalog_lines=(
                sustainability.catalog_lines[0],
                replace(sustainability.catalog_lines[1], unit_price=Decimal("11.01")),
            ),
        )
        decision, _results, validator = _decision_and_results(outside)
        assert decision.selected_plan is not None
        requirement = validator._source_requirements(outside, type("Sink", (), {"error": lambda *args, **kwargs: None})())["component-a"]
        objective = validator._objective(outside, requirement, decision.selected_plan, validator._offers(outside, requirement, EvidenceContract.BENCHMARK))
        self.assertEqual(objective[7], Decimal("0"))


if __name__ == "__main__":
    unittest.main()
