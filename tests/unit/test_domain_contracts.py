from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime
from decimal import Decimal
from itertools import product
import unittest

from apex_procurement.domain import (
    BucketAllocation,
    CandidatePlan,
    EvidenceBasis,
    EvidenceResult,
    EvidenceScope,
    EvidenceStatus,
    FulfillmentStatus,
    PlanDisposition,
    PlanLine,
    RequirementState,
    ResolutionStatus,
    RuleSeverity,
    SolveKind,
    SolverResult,
    SolverStageResult,
    SolverStatus,
    VALID_REQUIREMENT_STATES,
)


def make_line() -> PlanLine:
    planning_date = date(2031, 2, 3)
    return PlanLine(
        route_id="route-alpha",
        component_id="component-alpha",
        supplier_id="supplier-alpha",
        quantity=Decimal("2.00"),
        unit_price=Decimal("1.25"),
        order_date=planning_date,
        expected_delivery_date=date(2031, 2, 6),
        material_available_date=date(2031, 2, 6),
        allocation_group_id="group-alpha",
        bucket_allocations=(
            BucketAllocation(due_date=date(2031, 2, 10), quantity=Decimal("2.00")),
        ),
    )


def make_executable_plan(**overrides: object) -> CandidatePlan:
    values: dict[str, object] = {
        "plan_id": "plan-alpha",
        "component_id": "component-alpha",
        "disposition": PlanDisposition.EXECUTE,
        "lines": (make_line(),),
        "net_requirement": Decimal("2.00"),
        "eventual_covered_quantity": Decimal("2.00"),
        "residual_gap": Decimal("0.00"),
        "total_cost": Decimal("2.5000"),
        "minimum_compliant_total": Decimal("2.00"),
        "cheapest_covering_cost": Decimal("2.5000"),
        "forced_surplus": Decimal("0.00"),
        "discretionary_surplus": Decimal("0.00"),
    }
    values.update(overrides)
    return CandidatePlan(**values)  # type: ignore[arg-type]


class RequirementStateTests(unittest.TestCase):
    def test_exactly_the_five_specified_pairs_are_valid(self) -> None:
        observed_valid = set()
        observed_invalid = set()

        for fulfillment, resolution in product(FulfillmentStatus, ResolutionStatus):
            pair = (fulfillment, resolution)
            with self.subTest(fulfillment=fulfillment, resolution=resolution):
                if pair in VALID_REQUIREMENT_STATES:
                    state = RequirementState(fulfillment, resolution)
                    self.assertEqual((state.fulfillment, state.resolution), pair)
                    observed_valid.add(pair)
                else:
                    with self.assertRaises(ValueError):
                        RequirementState(fulfillment, resolution)
                    observed_invalid.add(pair)

        self.assertEqual(observed_valid, set(VALID_REQUIREMENT_STATES))
        self.assertEqual(len(observed_valid), 5)
        self.assertEqual(len(observed_invalid), 4)

    def test_state_is_immutable(self) -> None:
        state = RequirementState(FulfillmentStatus.FULFILLED, ResolutionStatus.RESOLVED)
        with self.assertRaises(FrozenInstanceError):
            state.resolution = ResolutionStatus.UNRESOLVED  # type: ignore[misc]


class SafetyContractTests(unittest.TestCase):
    def test_float_quantity_is_rejected(self) -> None:
        from apex_procurement.domain import DemandContribution

        with self.assertRaises(TypeError):
            DemandContribution("order-alpha", "product-alpha", 1.5)  # type: ignore[arg-type]

    def test_datetime_is_not_accepted_as_domain_date(self) -> None:
        from apex_procurement.domain import ScenarioConfiguration

        with self.assertRaises(TypeError):
            ScenarioConfiguration(datetime(2031, 2, 3, 4, 5))  # type: ignore[arg-type]

    def test_proven_hard_failure_cannot_have_executable_disposition(self) -> None:
        failed = EvidenceResult(
            rule_id="rule-alpha",
            status=EvidenceStatus.FAIL,
            basis=EvidenceBasis.PROSPECTIVE_ORDER,
            scope=EvidenceScope.CANDIDATE,
            severity=RuleSeverity.HARD,
            summary="A proven eligibility gate failed.",
        )
        with self.assertRaises(ValueError):
            make_executable_plan(evidence=(failed,))

    def test_unapproved_plan_cannot_have_executable_disposition(self) -> None:
        with self.assertRaises(ValueError):
            make_executable_plan(
                lines=(
                    replace(
                        make_line(),
                        approval_rule_ids=("approval-alpha",),
                    ),
                ),
                unresolved_approval_ids=("approval-alpha",),
            )

    def test_production_contract_decision_unknown_cannot_execute(self) -> None:
        unknown = EvidenceResult(
            rule_id="rule-history",
            status=EvidenceStatus.UNKNOWN,
            basis=EvidenceBasis.ROLLING_WINDOW,
            scope=EvidenceScope.RULE,
            severity=RuleSeverity.HARD,
            summary="Required history is absent.",
            contract_disposition=PlanDisposition.DECISION_REQUIRED,
        )
        with self.assertRaises(ValueError):
            make_executable_plan(evidence=(unknown,))

    def test_benchmark_rule_unknown_requires_assumption_disposition(self) -> None:
        unknown = EvidenceResult(
            rule_id="rule-history",
            status=EvidenceStatus.UNKNOWN,
            basis=EvidenceBasis.ROLLING_WINDOW,
            scope=EvidenceScope.RULE,
            severity=RuleSeverity.HARD,
            summary="Required history is absent.",
            contract_disposition=PlanDisposition.EXECUTE_WITH_ASSUMPTION,
        )
        plan = make_executable_plan(
            disposition=PlanDisposition.EXECUTE_WITH_ASSUMPTION,
            evidence=(unknown,),
            assumption_codes=("history-incomplete",),
        )
        self.assertIs(plan.disposition, PlanDisposition.EXECUTE_WITH_ASSUMPTION)

    def test_execute_with_assumption_requires_a_named_assumption(self) -> None:
        with self.assertRaises(ValueError):
            make_executable_plan(disposition=PlanDisposition.EXECUTE_WITH_ASSUMPTION)

    def test_feasible_incumbent_must_be_non_executable(self) -> None:
        stage = SolverStageResult(
            stage_name="coverage",
            status=SolverStatus.FEASIBLE_INCUMBENT,
            objective_value=Decimal("0"),
            mip_gap=Decimal("0.2"),
            certificate_complete=False,
            hit_resource_limit=True,
        )
        with self.assertRaises(ValueError):
            SolverResult(
                component_id="component-alpha",
                solve_kind=SolveKind.EXECUTABLE,
                status=SolverStatus.FEASIBLE_INCUMBENT,
                stage_results=(stage,),
                candidate_plan=make_executable_plan(),
                objective_vector=(Decimal("0"),),
            )

    def test_completed_zero_gap_result_has_executable_certificate_after_exact_check(self) -> None:
        stage = SolverStageResult(
            stage_name="coverage",
            status=SolverStatus.OPTIMAL,
            objective_value=Decimal("0"),
            mip_gap=Decimal("0"),
            certificate_complete=True,
            hit_resource_limit=False,
        )
        result = SolverResult(
            component_id="component-alpha",
            solve_kind=SolveKind.EXECUTABLE,
            status=SolverStatus.OPTIMAL,
            stage_results=(stage,),
            candidate_plan=make_executable_plan(),
            objective_vector=(Decimal("0"),),
            minimum_compliant_total=Decimal("2"),
            cheapest_covering_cost=Decimal("2.5"),
            exact_post_validated=True,
        )
        self.assertTrue(result.is_certified_optimal)
        self.assertTrue(result.has_executable_certificate)


if __name__ == "__main__":
    unittest.main()
