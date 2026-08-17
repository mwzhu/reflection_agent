"""Offline command line orchestration for the Apex procurement planner.

The module deliberately contains only application wiring.  Source loading,
policy decisions, allocation, independent validation, rendering, and writes
remain owned by their respective public subsystem boundaries.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import IntEnum
from pathlib import Path
import sys
from time import perf_counter_ns

from .audit import audit_json_line, deterministic_run_id, file_sha256

from .candidates import (
    CandidateBuildResult,
    CandidateRejection,
    NoAlternativeProof,
    build_candidate_routes,
    component_fingerprint,
)
from .config import EvidenceContract, ModelMode, RuntimeConfig
from .decisions import (
    CommitFailure,
    ConcurrentModificationError,
    DecisionError,
    DecisionOutputs,
    build_decision_outputs,
    commit_decisions,
    component_source_fingerprint,
    current_managed_order_numbers,
    demand_fingerprint_from_facts,
    parse_owned_purchase_order,
    reconcile_managed_decisions,
)
from .domain import (
    AlertCategory,
    CandidatePlan,
    CandidateRoute,
    CommitResult,
    DecisionComparatorFact,
    DecisionRecord,
    EvidenceResult,
    EvidenceScope,
    EvidenceStatus,
    FulfillmentStatus,
    InternalFailureExclusion,
    MaterialRouteRejection,
    PlanDisposition,
    RequirementState,
    ResolutionStatus,
    RuleSeverity,
    ScenarioSnapshot,
    SourceEntityNormalizationDisclosure,
    SolveKind,
    SolverResult,
    SolverStatus,
    ValidationResult,
    ValidationIssue,
    ValidationSeverity,
    UnitNormalizationDisclosure,
    ZERO,
)
from .explanations import render_decision_rationale
from .isolation import (
    build_internal_failure_exclusions,
    is_containable_component_error,
)
from .ledgers import (
    LedgerBuildResult,
    RouteAvailability,
    build_ledgers,
    post_plan_deadline_lateness,
    total_recovery_demand,
)
from .optimizer import (
    IntegerScaledSolver,
    OptimizerProblem,
    OrderApprovalConstraint,
    ProcurementOptimizer,
    SolverLimits,
)
from .policy.entity_resolution import EntityResolver
from .policy.evaluator import EvaluationBatch, EvaluationContext, PolicyEvaluator
from .policy.parameters import ApplicablePolicyParameters
from .policy.registry import PolicyRegistry, PolicyRule, load_policy_registry
from .policy.schema import PolicyValidationError
from .repository import (
    RepositoryLoadError,
    SQLiteRepository,
    ScenarioPathError,
    resolve_scenario_path,
)
from .serialization import canonical_dumps, sanitize_control_characters
from .snapshot import build_snapshot
from .validator import IndependentPlanValidator


class ExitCode(IntEnum):
    SUCCESS = 0
    CLI_OR_PATH = 2
    INVALID_SCENARIO = 3
    INVALID_POLICY = 4
    SOLVER_OR_VALIDATOR = 5
    CONCURRENT_MODIFICATION = 6
    COMMIT_FAILURE = 7


class CliUsageError(ValueError):
    """A valid parse produced an unsupported or unknown requested scope."""


class OptionalModelUnavailable(RuntimeError):
    """A requested optional model feature is absent from this runtime."""


class PlanningFailure(RuntimeError):
    """The numeric or independent-validation pipeline did not complete safely."""


@dataclass(frozen=True, slots=True)
class RunArtifacts:
    snapshot: ScenarioSnapshot
    registry: PolicyRegistry
    ledgers: LedgerBuildResult
    candidates: CandidateBuildResult
    decisions: tuple[DecisionRecord, ...]
    solver_results: tuple[SolverResult, ...]
    validation: ValidationResult
    outputs: DecisionOutputs
    commit: CommitResult
    audit_json_lines: tuple[str, ...] = ()
    internal_failure_exclusions: tuple[InternalFailureExclusion, ...] = ()
    validation_pass_count: int = 1


TestValidationIssueInjector = Callable[
    [
        int,
        ScenarioSnapshot,
        tuple[DecisionRecord, ...],
        tuple[SolverResult, ...],
        tuple[InternalFailureExclusion, ...],
    ],
    Iterable[ValidationIssue],
]


def build_parser() -> argparse.ArgumentParser:
    """Build the stable public CLI without reading data or policy files."""

    parser = argparse.ArgumentParser(
        prog="agent.py",
        description="Plan procurement deterministically from a SQLite scenario snapshot.",
    )
    parser.add_argument(
        "--scenario",
        type=Path,
        required=True,
        metavar="SCENARIO.sqlite",
        help="path to the scenario SQLite snapshot",
    )
    parser.add_argument(
        "--contract",
        choices=tuple(item.value for item in EvidenceContract),
        default=EvidenceContract.BENCHMARK.value,
        help="missing-evidence contract (default: benchmark)",
    )
    parser.add_argument(
        "--llm",
        choices=tuple(item.value for item in ModelMode),
        default=ModelMode.OFF.value,
        help="optional model behavior (default: off)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and explain the plan without committing rows",
    )
    parser.add_argument(
        "--explain",
        metavar="COMPONENT_ID",
        help="limit detailed explanation output to one component",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat independent-validation warnings as a failed run",
    )
    parser.add_argument(
        "--alert-prefixes",
        action="store_true",
        help="include human-visible category prefixes in alert prose",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit the run result as deterministic JSON",
    )
    return parser


def parse_config(argv: Sequence[str] | None = None) -> RuntimeConfig:
    """Parse arguments into the frozen runtime configuration contract."""

    args = build_parser().parse_args(argv)
    return RuntimeConfig(
        scenario_path=args.scenario,
        contract=EvidenceContract(args.contract),
        model_mode=ModelMode(args.llm),
        dry_run=args.dry_run,
        explain_component_id=args.explain,
        strict=args.strict,
        alert_prefixes=args.alert_prefixes,
        json_output=args.json_output,
    )


def _rule_body(rule: PolicyRule) -> Mapping[str, object]:
    body = rule.data.get("constraint") or rule.data.get("directive")
    return body if isinstance(body, Mapping) else {}


def _rule_kind(rule: PolicyRule) -> str:
    return str(_rule_body(rule).get("kind", ""))


def _component_evaluations(
    snapshot: ScenarioSnapshot,
    registry: PolicyRegistry,
    contract: EvidenceContract,
    ledgers: LedgerBuildResult,
) -> dict[str, EvaluationBatch]:
    evaluator = PolicyEvaluator(registry, contract)
    components = {item.component_id: item for item in snapshot.components}
    return {
        ledger.component_id: evaluator.evaluate(
            EvaluationContext(
                scenario_date=snapshot.configuration.current_date,
                suppliers=snapshot.suppliers,
                component=components[ledger.component_id],
                purchase_orders=snapshot.purchase_orders,
            )
        )
        for ledger in ledgers.supply_ledgers
    }


def _route_aware_ledgers(
    snapshot: ScenarioSnapshot,
    candidates: CandidateBuildResult,
) -> LedgerBuildResult:
    """Rebuild physical ledgers with policy-eligible route availability.

    Approval-gated routes remain physical recovery possibilities for solve-2,
    but failed or unresolved eligibility never authorizes duplicate supply.
    """

    physical = build_ledgers(snapshot)
    return build_ledgers(
        snapshot,
        route_availabilities=tuple(
            RouteAvailability(
                route.route_id,
                route.component_id,
                route.material_available_date,
                (
                    route.exception_scope_deadlines
                    if route.exception_codes
                    else tuple(
                        bucket.due_date
                        for bucket in physical.buckets_for(
                            route.component_id
                        )
                    )
                ),
            )
            for route in candidates.routes
            if route.eligibility is EvidenceStatus.PASS
        ),
    )


def _merge_component_rule_evidence(
    candidates: CandidateBuildResult,
    evaluations: Mapping[str, EvaluationBatch],
) -> CandidateBuildResult:
    """Attach evaluator-owned rule-scope evidence to route facts.

    Candidate evaluation owns route-local gates.  This merge is solely the
    application join for component-wide rule evidence, such as an unavailable
    rolling window under the selected evidence contract.
    """

    routes: list[CandidateRoute] = []
    contract_rejections: list[CandidateRejection] = []
    for route in candidates.routes:
        batch = evaluations.get(route.component_id)
        if batch is None:
            routes.append(route)
            continue
        component_evidence = tuple(
            evaluation.evidence
            for evaluation in batch.active
            if evaluation.applicable is not False
            and evaluation.evidence is not None
            and evaluation.evidence.scope is EvidenceScope.RULE
        )
        merged = {item.rule_id: item for item in (*route.evidence, *component_evidence)}
        blocked = any(
            item.severity is RuleSeverity.HARD
            and item.status is EvidenceStatus.UNKNOWN
            and item.contract_disposition
            in {PlanDisposition.DECISION_REQUIRED, PlanDisposition.RECOMMEND_APPROVAL}
            for item in component_evidence
        )
        eligibility = (
            EvidenceStatus.UNKNOWN
            if blocked and route.eligibility is EvidenceStatus.PASS
            else route.eligibility
        )
        routes.append(
            replace(route, evidence=tuple(merged.values()), eligibility=eligibility)
        )
        for item in component_evidence:
            if (
                item.severity is RuleSeverity.HARD
                and item.status is EvidenceStatus.UNKNOWN
                and item.contract_disposition is PlanDisposition.DECISION_REQUIRED
            ):
                contract_rejections.append(
                    CandidateRejection(
                        route.route_id,
                        route.component_id,
                        route.supplier_id,
                        EvidenceStatus.UNKNOWN,
                        "EVIDENCE_CONTRACT_BLOCKED",
                        item.summary,
                        (item.rule_id,),
                    )
                )
    return CandidateBuildResult(
        routes=tuple(routes),
        rejections=tuple(set((*candidates.rejections, *contract_rejections))),
        alerts=candidates.alerts,
    )


def _applicable_rule_ids(batch: EvaluationBatch) -> frozenset[str]:
    return frozenset(
        item.rule_id
        for item in batch.active
        if item.applicable is not False
        and item.selector_status is not EvidenceStatus.FAIL
    )


def _optimizer_problem(
    snapshot: ScenarioSnapshot,
    registry: PolicyRegistry,
    ledgers: LedgerBuildResult,
    candidates: CandidateBuildResult,
    evaluations: Mapping[str, EvaluationBatch],
    component_id: str,
    policy_parameters: ApplicablePolicyParameters,
) -> OptimizerProblem:
    component = next(item for item in snapshot.components if item.component_id == component_id)
    ledger = ledgers.ledger_for(component_id)
    applicable = _applicable_rule_ids(evaluations[component_id])
    resolver = EntityResolver(registry)
    minimum_secondary_fraction: Decimal | None = None
    minimum_secondary_rule_id: str | None = None
    named_primary_supplier_id: str | None = None
    named_primary_rule_id: str | None = None
    moq_rule_id: str | None = None
    sub_moq_approval_rule_id: str | None = None
    order_approvals = [
        OrderApprovalConstraint(
            rule_id=item.rule_id,
            maximum_without_approval=item.amount_exceeds,
            approving_authority=item.authority,
        )
        for item in policy_parameters.approval_thresholds
    ]

    secondary_parameters = tuple(
        item
        for item in policy_parameters.secondary_allocations
        if item.rule_id in applicable
    )
    if len(secondary_parameters) > 1:
        raise PlanningFailure(
            f"multiple minimum-secondary parameters apply to {component_id}"
        )
    if secondary_parameters:
        minimum_secondary_fraction = secondary_parameters[0].minimum_fraction
        minimum_secondary_rule_id = secondary_parameters[0].rule_id

    for rule in registry.active_rules(snapshot.configuration.current_date):
        kind = _rule_kind(rule)
        body = _rule_body(rule)
        if kind == "catalog_minimum_order_quantity":
            moq_rule_id = rule.rule_id
        elif kind == "sub_moq_written_approval":
            sub_moq_approval_rule_id = rule.rule_id
        elif kind == "named_primary_supplier" and rule.rule_id in applicable:
            reference = body.get("supplier")
            if isinstance(reference, Mapping):
                resolution = resolver.resolve_named_supplier(reference, snapshot.suppliers)
                named_primary_supplier_id = resolution.resolved_supplier_id
                if named_primary_supplier_id is not None:
                    named_primary_rule_id = rule.rule_id

    return OptimizerProblem(
        component_id=component_id,
        unit_of_measure=component.unit_of_measure,
        net_requirement=ledger.eventual_gap,
        recovery_demand=total_recovery_demand(ledger),
        authorized_recovery_surplus=total_recovery_demand(ledger),
        routes=candidates.routes_for(component_id),
        demand_buckets=ledgers.buckets_for(component_id),
        supply_ledger=ledger,
        suppliers=snapshot.suppliers,
        evidence_contract=evaluations[component_id].contract,
        minimum_secondary_fraction=minimum_secondary_fraction,
        minimum_secondary_rule_id=minimum_secondary_rule_id,
        named_primary_supplier_id=named_primary_supplier_id,
        named_primary_rule_id=named_primary_rule_id,
        order_approval_constraints=tuple(order_approvals),
        policy_parameters=policy_parameters,
        moq_rule_id=moq_rule_id,
        sub_moq_approval_rule_id=sub_moq_approval_rule_id,
    )


def _solver_results(outcome: object) -> tuple[SolverResult, ...]:
    values = (
        getattr(outcome, "calibration"),
        getattr(outcome, "baseline"),
        getattr(outcome, "executable"),
        *getattr(outcome, "counterfactuals"),
    )
    return tuple(item for item in values if isinstance(item, SolverResult))


def _below_b_no_alternative_proofs(
    snapshot: ScenarioSnapshot,
    registry: PolicyRegistry,
    contract: EvidenceContract,
    ledgers: LedgerBuildResult,
    candidates: CandidateBuildResult,
    evaluations: Mapping[str, EvaluationBatch],
    policy_parameters: ApplicablePolicyParameters,
) -> tuple[NoAlternativeProof, ...]:
    """Run the planner/validator predicate pair required by the below-B gate."""

    demanded_components = {
        ledger.component_id
        for ledger in ledgers.supply_ledgers
        if ledger.eventual_gap > ZERO or total_recovery_demand(ledger) > ZERO
    }
    pending = {
        route.component_id
        for route in candidates.routes
        if route.component_id in demanded_components
        if any(
            requirement.endswith(":no-alternative-certificate")
            for requirement in route.approval_requirements
        )
    }
    if not pending:
        return ()
    components = {item.component_id: item for item in snapshot.components}
    solver = IntegerScaledSolver()
    validator = IndependentPlanValidator(
        registry, policy_parameters=policy_parameters
    )
    proofs: list[NoAlternativeProof] = []
    for component_id in sorted(pending):
        problem = _optimizer_problem(
            snapshot,
            registry,
            ledgers,
            candidates,
            evaluations,
            component_id,
            policy_parameters,
        )
        # Named-primary is shaping, not evidence that a B-or-better
        # alternative is absent.  All hard gates and per-order allocation
        # rules remain in this clean predicate solve.
        predicate = solver.solve(
            replace(
                problem,
                solve_kind=SolveKind.QUANTITY_CALIBRATION,
                named_primary_supplier_id=None,
                named_primary_rule_id=None,
                minimum_compliant_total=None,
                coverage_target=None,
                cheapest_covering_cost=None,
                relaxed_rule_id=None,
            )
        )
        independent = validator.independently_check_b_or_better(
            snapshot,
            component_id,
            contract,
        )
        planner_complete = (
            predicate.is_certified_optimal
            and predicate.exact_post_validated
            and bool(predicate.objective_vector)
        )
        planner_no_alternative = (
            planner_complete and predicate.objective_vector[0] > ZERO
        )
        planner_alternative_exists = (
            planner_complete and predicate.objective_vector[0] == ZERO
        )
        independent_no_alternative = (
            independent.status is SolverStatus.INFEASIBLE
            and independent.certificate_complete
        )
        independent_alternative_exists = (
            independent.status is SolverStatus.OPTIMAL
            and independent.certificate_complete
        )
        if planner_no_alternative and independent_no_alternative:
            status = SolverStatus.INFEASIBLE
        elif planner_alternative_exists and independent_alternative_exists:
            status = SolverStatus.OPTIMAL
        else:
            continue
        proofs.append(
            NoAlternativeProof(
                component_fingerprint(components[component_id]),
                status,
                certificate_complete=True,
                independently_validated=True,
            )
        )
    return tuple(proofs)


def _component_rule_evidence(batch: EvaluationBatch) -> tuple[EvidenceResult, ...]:
    return tuple(
        evaluation.evidence
        for evaluation in batch.active
        if evaluation.applicable is not False
        and evaluation.evidence is not None
        and evaluation.evidence.scope is EvidenceScope.RULE
    )


def _contract_blocking_evidence(batch: EvaluationBatch) -> tuple[EvidenceResult, ...]:
    """Return hard rule evidence whose active contract requires a decision."""

    return tuple(
        item
        for item in _component_rule_evidence(batch)
        if item.severity is RuleSeverity.HARD
        and item.status is EvidenceStatus.UNKNOWN
        and item.contract_disposition is PlanDisposition.DECISION_REQUIRED
    )


def _plan_allocations(plans: Sequence[CandidatePlan]) -> dict[str, Decimal]:
    allocations: dict[str, Decimal] = {}
    for plan in plans:
        for line in plan.lines:
            allocations[line.supplier_id] = (
                allocations.get(line.supplier_id, ZERO) + line.quantity
            )
    return allocations


def _quarantine_affects_component(
    snapshot: ScenarioSnapshot,
    component_id: str,
) -> bool:
    return any(
        component_id in issue.affected_component_ids
        for issue in snapshot.route_input_issues
    )


def _decision_categories(
    snapshot: ScenarioSnapshot,
    registry: PolicyRegistry,
    contract: EvidenceContract,
    ledgers: LedgerBuildResult,
    candidates: CandidateBuildResult,
    batch: EvaluationBatch,
    component_id: str,
    outcome: object | None,
    *,
    applicable_alternatives: Sequence[CandidatePlan] | None = None,
) -> tuple[AlertCategory, ...]:
    categories = {
        item.category
        for item in ledgers.alerts
        if item.component_id in {None, component_id}
    }
    if registry.economic_autonomy.provisional:
        categories.add(AlertCategory.ASSUMPTION)
    categories.update(
        item.category
        for item in candidates.alerts
        if item.component_id in {None, component_id}
    )
    categories.update(
        item.category
        for item in batch.alerts
        if item.entity_id in {None, component_id}
    )
    quarantine_affected = _quarantine_affects_component(snapshot, component_id)
    if quarantine_affected:
        categories.add(AlertCategory.DATA_QUALITY)
    contract_blockers = _contract_blocking_evidence(batch)
    evidence_blocked = bool(contract_blockers) and any(
        route.is_evidence_blocked
        for route in candidates.routes_for(component_id)
    )
    if evidence_blocked:
        categories.update(
            {AlertCategory.DECISION_REQUIRED, AlertCategory.EVIDENCE_CONTRACT}
        )
        if not registry.economic_autonomy.provisional:
            categories.discard(AlertCategory.ASSUMPTION)
        categories.discard(AlertCategory.NO_ELIGIBLE_SUPPLIER)
    selected = getattr(outcome, "selected_plan", None) if outcome is not None else None
    lateness = post_plan_deadline_lateness(
        ledgers.ledger_for(component_id),
        ledgers.buckets_for(component_id),
        selected.lines if selected is not None else (),
    )
    if lateness:
        categories.add(AlertCategory.LATE_ARRIVAL)
    if outcome is None:
        return tuple(sorted(categories, key=lambda item: item.value))

    categories.update(item.category for item in getattr(outcome, "alerts"))
    selected = getattr(outcome, "selected_plan")
    alternatives = (
        tuple(getattr(outcome, "alternatives"))
        if applicable_alternatives is None
        else tuple(applicable_alternatives)
    )
    residual = getattr(outcome, "residual_gap")
    if residual > ZERO:
        categories.add(AlertCategory.UNMET_DEMAND)
    if (
        not evidence_blocked
        and not quarantine_affected
        and selected is None
        and not any(
            route.may_enter_executable_model and route.feasible_deadlines
            for route in candidates.routes_for(component_id)
        )
    ):
        categories.add(AlertCategory.NO_ELIGIBLE_SUPPLIER)
    if quarantine_affected and residual > ZERO:
        # The excluded source route prevents a complete eligibility or
        # infeasibility proof.  DATA_QUALITY is the terminal explanation.
        categories.discard(AlertCategory.NO_ELIGIBLE_SUPPLIER)
    if (
        selected is not None
        and selected.disposition is PlanDisposition.EXECUTE_WITH_ASSUMPTION
    ):
        categories.update({AlertCategory.ASSUMPTION, AlertCategory.EVIDENCE_CONTRACT})
    if any(
        item.disposition is PlanDisposition.RECOMMEND_APPROVAL
        for item in alternatives
    ):
        categories.add(AlertCategory.APPROVAL_REQUIRED)

    plans = tuple(item for item in (selected, *alternatives) if item is not None)
    if plans:
        component = next(
            item for item in snapshot.components if item.component_id == component_id
        )
        final_batch = PolicyEvaluator(registry, contract).evaluate(
            EvaluationContext(
                scenario_date=snapshot.configuration.current_date,
                suppliers=snapshot.suppliers,
                component=component,
                purchase_orders=snapshot.purchase_orders,
                facts={"allocations": _plan_allocations(plans)},
            )
        )
        categories.update(
            alert.category
            for alert in final_batch.alerts
            if alert.category is AlertCategory.CAPACITY_UNKNOWN
        )
    return tuple(sorted(categories, key=lambda item: item.value))


def _normalization_disclosures(
    ledgers: LedgerBuildResult,
    batch: EvaluationBatch,
    component_id: str,
) -> tuple[
    SourceEntityNormalizationDisclosure | UnitNormalizationDisclosure, ...
]:
    """Collect typed component disclosures without flattening their source facts."""

    disclosures = {
        item.normalization_disclosure
        for item in ledgers.alerts
        if item.component_id == component_id
        and item.normalization_disclosure is not None
    }
    disclosures.update(
        item.normalization_disclosure
        for item in batch.alerts
        if item.entity_id == component_id
        and item.normalization_disclosure is not None
    )
    return tuple(
        sorted(
            disclosures,
            key=lambda item: (type(item).__name__, repr(item)),
        )
    )


def _closed_decision(
    snapshot: ScenarioSnapshot,
    contract: EvidenceContract,
    ledgers: LedgerBuildResult,
    batch: EvaluationBatch,
    candidates: CandidateBuildResult,
    registry: PolicyRegistry,
    component_id: str,
    *,
    source_fingerprint: str | None = None,
) -> DecisionRecord:
    ledger = ledgers.ledger_for(component_id)
    categories = _decision_categories(
        snapshot,
        registry,
        contract,
        ledgers,
        candidates,
        batch,
        component_id,
        None,
    )
    decision = DecisionRecord(
        requirement_id=f"requirement:{component_id}",
        component_id=component_id,
        evidence_contract=contract,
        demand_buckets=ledgers.buckets_for(component_id),
        supply_ledger=ledger,
        total_requirement=ledger.total_demand,
        initial_eventual_gap=ZERO,
        covered_quantity=ledger.total_demand,
        residual_gap=ZERO,
        requirement_state=RequirementState(
            FulfillmentStatus.FULFILLED, ResolutionStatus.RESOLVED
        ),
        selected_plan=None,
        alternatives=(),
        evidence=_component_rule_evidence(batch),
        alert_categories=categories,
        rationale="Pending deterministic rendering.",
        deadline_lateness=post_plan_deadline_lateness(
            ledger,
            ledgers.buckets_for(component_id),
        ),
        economic_autonomy=registry.economic_autonomy,
        source_fingerprint=source_fingerprint,
        normalization_disclosures=_normalization_disclosures(
            ledgers, batch, component_id
        ),
    )
    return replace(decision, rationale=render_decision_rationale(decision))


def _planned_decision(
    snapshot: ScenarioSnapshot,
    registry: PolicyRegistry,
    contract: EvidenceContract,
    ledgers: LedgerBuildResult,
    candidates: CandidateBuildResult,
    batch: EvaluationBatch,
    outcome: object,
    component_id: str,
    *,
    source_fingerprint: str | None = None,
) -> DecisionRecord:
    ledger = ledgers.ledger_for(component_id)
    selected = getattr(outcome, "selected_plan")
    sub_moq_approval_rule_ids = frozenset(
        rule.rule_id
        for rule in registry.active_rules(snapshot.configuration.current_date)
        if _rule_kind(rule) == "sub_moq_written_approval"
    )
    alternatives = _nonredundant_alternatives(
        selected,
        getattr(outcome, "alternatives"),
        sub_moq_approval_rule_ids=sub_moq_approval_rule_ids,
    )
    planned_coverage = selected.eventual_covered_quantity if selected is not None else ZERO
    existing_coverage = min(ledger.total_demand, ledger.eventual_supply)
    residual = getattr(outcome, "residual_gap")
    requirement_state = getattr(outcome, "requirement_state")
    if residual > ZERO and _quarantine_affects_component(snapshot, component_id):
        requirement_state = RequirementState(
            requirement_state.fulfillment,
            ResolutionStatus.UNRESOLVED,
        )
    comparator_facts, material_rejections = _structured_rationale_facts(
        snapshot,
        registry,
        candidates,
        selected,
        component_id,
    )
    decision = DecisionRecord(
        requirement_id=f"requirement:{component_id}",
        component_id=component_id,
        evidence_contract=contract,
        demand_buckets=ledgers.buckets_for(component_id),
        supply_ledger=ledger,
        total_requirement=ledger.total_demand,
        initial_eventual_gap=ledger.eventual_gap,
        covered_quantity=existing_coverage + planned_coverage,
        residual_gap=residual,
        requirement_state=requirement_state,
        selected_plan=selected,
        alternatives=alternatives,
        evidence=_component_rule_evidence(batch),
        alert_categories=_decision_categories(
            snapshot,
            registry,
            contract,
            ledgers,
            candidates,
            batch,
            component_id,
            outcome,
            applicable_alternatives=alternatives,
        ),
        rationale="Pending deterministic rendering.",
        deadline_lateness=post_plan_deadline_lateness(
            ledger,
            ledgers.buckets_for(component_id),
            selected.lines if selected is not None else (),
        ),
        economic_autonomy=registry.economic_autonomy,
        source_fingerprint=(
            source_fingerprint
            if source_fingerprint is not None
            else component_source_fingerprint(
                snapshot,
                component_id,
                registry.content_hash,
                contract,
                policy_concepts_version=registry.concepts_hash,
                candidate_routes=candidates.routes,
                candidate_rejections=candidates.rejections,
                candidate_alerts=candidates.alerts,
            )
        ),
        comparator_facts=comparator_facts,
        material_rejections=material_rejections,
        normalization_disclosures=_normalization_disclosures(
            ledgers, batch, component_id
        ),
    )
    return replace(decision, rationale=render_decision_rationale(decision))


def _trace_for_stage(route: CandidateRoute, stage: int):
    return next((item for item in route.comparator_trace if item.stage == stage), None)


def _structured_rationale_facts(
    snapshot: ScenarioSnapshot,
    registry: PolicyRegistry,
    candidates: CandidateBuildResult,
    selected: CandidatePlan | None,
    component_id: str,
) -> tuple[tuple[DecisionComparatorFact, ...], tuple[MaterialRouteRejection, ...]]:
    """Carry a compact, deterministic explanation boundary into decisions."""

    if selected is None:
        return (), ()
    selected_route_ids = tuple(line.route_id for line in selected.lines)
    selected_routes = {
        route.route_id: route
        for route in candidates.routes_for(component_id)
        if route.route_id in selected_route_ids
    }
    if set(selected_routes) != set(selected_route_ids):
        raise PlanningFailure(
            f"selected plan for {component_id} references a missing candidate route"
        )
    selected_lines = {line.route_id: line for line in selected.lines}
    selected_total = sum((line.quantity for line in selected.lines), ZERO)
    minimum = selected.minimum_compliant_total
    if minimum is None:
        raise PlanningFailure(
            f"selected plan for {component_id} has no certified quantity calibration"
        )
    quantity_rules = tuple(
        sorted(
            {
                rule_id
                for route in selected_routes.values()
                for trace in route.comparator_trace
                if trace.stage in {1, 5}
                for rule_id in trace.source_rule_ids
            }
            | {
                exception_id.split(":condition_", 1)[0]
                for line in selected.lines
                for allocation in line.bucket_allocations
                for exception_id in allocation.exception_ids
            }
        )
    )
    if not quantity_rules:
        raise PlanningFailure(
            f"selected plan for {component_id} has no rule-backed quantity facts"
        )
    comparator_facts: list[DecisionComparatorFact] = [
        DecisionComparatorFact(
            stage=0,
            kind="quantity_calibration",
            comparator="certified_quantity_calibration",
            outcome=(
                f"selected total {selected_total} against certified minimum {minimum}; "
                f"forced surplus {selected.forced_surplus}; discretionary surplus "
                f"{selected.discretionary_surplus}"
            ),
            selected_route_ids=selected_route_ids,
            compared_route_ids=(),
            rule_ids=quantity_rules,
            decisive=True,
            quantity_delta=selected_total - minimum,
            cost_delta=(
                selected.total_cost - selected.cheapest_covering_cost
                if selected.cheapest_covering_cost is not None
                else None
            ),
            policy_window=registry.economic_autonomy.disclosure(),
        )
    ]

    rejection_by_route: dict[str, list[CandidateRejection]] = {}
    for rejection in candidates.rejections:
        if rejection.component_id == component_id:
            rejection_by_route.setdefault(rejection.route_id, []).append(rejection)
    unselected_by_supplier: dict[str, list[CandidateRoute]] = {}
    selected_supplier_ids = {line.supplier_id for line in selected.lines}
    for route in candidates.routes_for(component_id):
        if route.supplier_id not in selected_supplier_ids:
            unselected_by_supplier.setdefault(route.supplier_id, []).append(route)

    material: list[MaterialRouteRejection] = []
    suppliers = {item.supplier_id: item for item in snapshot.suppliers}
    resolver = EntityResolver(registry)
    rejection_priority = {
        "EVIDENCE_CONTRACT_BLOCKED": 0,
        "POLICY_GATE_FAILED": 1,
        "POLICY_GATE_UNRESOLVED": 2,
        "APPROVAL_OR_CERTIFICATE_REQUIRED": 3,
        "NO_FEASIBLE_DEADLINE": 4,
    }
    for supplier_id, supplier_routes in sorted(unselected_by_supplier.items()):
        rejected = min(
            supplier_routes,
            key=lambda route: (
                route.eligibility is not EvidenceStatus.PASS,
                bool(route.approval_requirements),
                route.unit_price,
                route.material_available_date,
                route.route_id,
            ),
        )
        selected_route = min(
            selected_routes.values(),
            key=lambda route: (
                abs(route.unit_price - rejected.unit_price),
                abs((route.material_available_date - rejected.material_available_date).days),
                route.route_id,
            ),
        )
        selected_line = selected_lines[selected_route.route_id]
        route_rejections = rejection_by_route.get(rejected.route_id, [])
        primary_rejection = min(
            route_rejections,
            key=lambda item: (
                rejection_priority.get(item.code, 99),
                item.code,
                item.rule_ids,
            ),
            default=None,
        )

        price_delta = rejected.unit_price - selected_route.unit_price
        delivery_delta = (
            rejected.material_available_date - selected_route.material_available_date
        ).days
        if not rejected.may_enter_executable_model:
            material_rule_ids = tuple(
                sorted(
                    {
                        rule_id
                        for item in route_rejections
                        for rule_id in item.rule_ids
                    }
                )
            )
            material.append(
                MaterialRouteRejection(
                    route_id=rejected.route_id,
                    supplier_id=supplier_id,
                    reason_code=(
                        primary_rejection.code
                        if primary_rejection is not None
                        else "POLICY_GATE_FAILED"
                    ),
                    eligibility=rejected.eligibility,
                    rule_ids=material_rule_ids,
                    unit_price=rejected.unit_price,
                    selected_unit_price=selected_route.unit_price,
                    price_delta=price_delta,
                    material_available_date=rejected.material_available_date,
                    selected_material_available_date=selected_route.material_available_date,
                    delivery_delta_days=delivery_delta,
                )
            )
            continue
        first_due = min(
            allocation.due_date for allocation in selected_line.bucket_allocations
        )
        selected_domestic = resolver.resolve_concept(
            "domestic_supplier", suppliers[selected_route.supplier_id]
        ).status
        rejected_domestic = resolver.resolve_concept(
            "domestic_supplier", suppliers[rejected.supplier_id]
        ).status
        stage_outcomes: list[tuple[int, str]] = []
        selected_on_time = first_due in selected_route.feasible_deadlines
        rejected_on_time = first_due in rejected.feasible_deadlines
        stage_outcomes.append(
            (
                1,
                "selected_on_time"
                if selected_on_time and not rejected_on_time
                else "rejected_on_time_advantage"
                if rejected_on_time and not selected_on_time
                else f"equal_on_time={str(selected_on_time).lower()}",
            )
        )
        selected_domestic_trace = _trace_for_stage(selected_route, 2)
        rejected_domestic_trace = _trace_for_stage(rejected, 2)
        domestic_states = {
            trace.outcome
            for trace in (selected_domestic_trace, rejected_domestic_trace)
            if trace is not None
        }
        if "skipped" in domestic_states:
            domestic_outcome = "skipped_condition_b"
        elif "moot" in domestic_states:
            domestic_outcome = "moot_condition_a_or_c"
        elif selected_domestic is rejected_domestic:
            domestic_outcome = "moot_same_domesticity"
        elif (
            selected_domestic is EvidenceStatus.PASS
            and rejected_domestic is EvidenceStatus.FAIL
        ):
            domestic_outcome = "selected_domestic_preference_applied"
        else:
            domestic_outcome = "rejected_domestic_preference_advantage"
        stage_outcomes.append((2, domestic_outcome))

        for stage, selected_label in (
            (3, "selected_strategic_retention"),
            (4, "selected_sustainability_preference"),
        ):
            selected_trace = _trace_for_stage(selected_route, stage)
            rejected_trace = _trace_for_stage(rejected, stage)
            due_token = first_due.isoformat()
            selected_penalty = bool(
                selected_trace is not None
                and selected_trace.outcome.startswith("penalty:")
                and due_token in selected_trace.outcome.split(":", 1)[1].split(",")
            )
            rejected_penalty = bool(
                rejected_trace is not None
                and rejected_trace.outcome.startswith("penalty:")
                and due_token in rejected_trace.outcome.split(":", 1)[1].split(",")
            )
            stage_outcomes.append(
                (
                    stage,
                    selected_label
                    if not selected_penalty and rejected_penalty
                    else "rejected_policy_preference_advantage"
                    if selected_penalty and not rejected_penalty
                    else "outside_window_or_equal",
                )
            )
        stage_outcomes.extend(
            (
                (
                    5,
                    "selected_lower_known_cost"
                    if selected_route.unit_price < rejected.unit_price
                    else "rejected_lower_known_cost"
                    if rejected.unit_price < selected_route.unit_price
                    else "equal_known_cost",
                ),
                (
                    6,
                    "selected_shorter_lead_time"
                    if selected_route.lead_time_days < rejected.lead_time_days
                    else "rejected_shorter_lead_time"
                    if rejected.lead_time_days < selected_route.lead_time_days
                    else "equal_lead_time",
                ),
                (
                    7,
                    "selected_lower_id_free_fingerprint"
                    if (
                        selected_route.supplier_fingerprint,
                        selected_route.route_fingerprint,
                    )
                    < (rejected.supplier_fingerprint, rejected.route_fingerprint)
                    else "rejected_lower_id_free_fingerprint",
                ),
            )
        )
        selected_outcomes = {
            "selected_on_time",
            "selected_domestic_preference_applied",
            "selected_strategic_retention",
            "selected_sustainability_preference",
            "selected_lower_known_cost",
            "selected_shorter_lead_time",
            "selected_lower_id_free_fingerprint",
        }
        rejected_outcomes = {
            "rejected_on_time_advantage",
            "rejected_domestic_preference_advantage",
            "rejected_policy_preference_advantage",
            "rejected_lower_known_cost",
            "rejected_shorter_lead_time",
            "rejected_lower_id_free_fingerprint",
        }
        decisive_stage = next(
            (
                stage
                for stage, outcome in stage_outcomes
                if outcome in selected_outcomes | rejected_outcomes
            ),
            None,
        )
        decisive_outcome = next(
            (
                outcome
                for stage, outcome in stage_outcomes
                if stage == decisive_stage
            ),
            None,
        )
        if decisive_stage is None or decisive_outcome not in selected_outcomes:
            allocation_rules = tuple(
                sorted(
                    set(quantity_rules)
                    | {
                        rule_id
                        for item in route_rejections
                        for rule_id in item.rule_ids
                    }
                )
            )
            material.append(
                MaterialRouteRejection(
                    route_id=rejected.route_id,
                    supplier_id=supplier_id,
                    reason_code="NOT_SELECTED_BY_CERTIFIED_ALLOCATION",
                    eligibility=rejected.eligibility,
                    rule_ids=allocation_rules,
                    unit_price=rejected.unit_price,
                    selected_unit_price=selected_route.unit_price,
                    price_delta=price_delta,
                    material_available_date=rejected.material_available_date,
                    selected_material_available_date=selected_route.material_available_date,
                    delivery_delta_days=delivery_delta,
                )
            )
            continue
        for stage, raw_outcome in stage_outcomes:
            selected_trace = _trace_for_stage(selected_route, stage)
            rejected_trace = _trace_for_stage(rejected, stage)
            rule_ids = tuple(
                sorted(
                    {
                        rule_id
                        for trace in (selected_trace, rejected_trace)
                        if trace is not None
                        for rule_id in trace.source_rule_ids
                    }
                )
            )
            if stage < 7 and not rule_ids:
                raise PlanningFailure(
                    f"route comparator stage {stage} for {component_id} has no source rule"
                )
            comparator_facts.append(
                DecisionComparatorFact(
                    stage=stage,
                    kind="route_selection",
                    comparator=(
                        selected_trace.comparator
                        if selected_trace is not None
                        else "id_free_fingerprint"
                    ),
                    outcome=(
                        raw_outcome
                        if stage <= decisive_stage
                        else f"not_evaluated_after_stage_{decisive_stage}"
                    ),
                    selected_route_ids=(selected_route.route_id,),
                    compared_route_ids=(rejected.route_id,),
                    rule_ids=rule_ids,
                    decisive=stage == decisive_stage,
                    cost_delta=price_delta if stage in {3, 4, 5} else None,
                    delivery_delta_days=(
                        delivery_delta if stage in {1, 4, 6} else None
                    ),
                    policy_window=(
                        selected_trace.explanation
                        if selected_trace is not None
                        else "ID-free deterministic tie key"
                    ),
                )
            )
        material_rule_ids = tuple(
            sorted(
                {
                    rule_id
                    for item in route_rejections
                    for rule_id in item.rule_ids
                }
                | {
                    rule_id
                    for fact in comparator_facts
                    if fact.compared_route_ids == (rejected.route_id,)
                    and fact.decisive
                    for rule_id in fact.rule_ids
                }
            )
        )
        material.append(
            MaterialRouteRejection(
                route_id=rejected.route_id,
                supplier_id=supplier_id,
                reason_code=(
                    primary_rejection.code
                    if primary_rejection is not None
                    else "NOT_SELECTED_BY_COMPARATOR"
                ),
                eligibility=rejected.eligibility,
                rule_ids=material_rule_ids,
                unit_price=rejected.unit_price,
                selected_unit_price=selected_route.unit_price,
                price_delta=price_delta,
                material_available_date=rejected.material_available_date,
                selected_material_available_date=selected_route.material_available_date,
                delivery_delta_days=delivery_delta,
            )
        )
    return tuple(comparator_facts), tuple(material)


def _plan_line_facts(plan: CandidatePlan) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            line.route_id,
            line.supplier_id,
            line.quantity,
            line.unit_price,
            line.order_date,
            line.expected_delivery_date,
            line.material_available_date,
            line.bucket_allocations,
        )
        for line in plan.lines
    )


def _nonredundant_alternatives(
    selected: CandidatePlan | None,
    alternatives: Sequence[CandidatePlan],
    *,
    sub_moq_approval_rule_ids: frozenset[str] = frozenset(),
) -> tuple[CandidatePlan, ...]:
    """Keep only counterfactuals applicable at the current decision frontier.

    A relaxed rule that produces the identical plan proves the relaxation was
    unnecessary.  It is neither an approval proposal nor a second semantic
    action and must not enter the recommendation set.

    Selecting an executable plan with forced surplus also closes any live
    sub-MOQ approval path for that requirement.  The optimizer still computes
    the counterfactual, but it is mutually exclusive with the commitment and
    therefore is not persisted as a current approval request.  If execution
    is withheld for another reason, the complete proposal remains applicable.
    """

    selected_facts = _plan_line_facts(selected) if selected is not None else None
    result: list[CandidatePlan] = []
    seen: set[tuple[tuple[object, ...], ...]] = set()
    for plan in alternatives:
        represented_sub_moq_approval = (
            plan.disposition is PlanDisposition.RECOMMEND_APPROVAL
            and bool(
                sub_moq_approval_rule_ids
                & set(plan.relaxed_rule_ids)
                & set(plan.unresolved_approval_ids)
            )
        )
        if (
            selected is not None
            and selected.forced_surplus > ZERO
            and represented_sub_moq_approval
        ):
            continue
        facts = _plan_line_facts(plan)
        if selected_facts is not None and facts == selected_facts:
            continue
        if facts in seen:
            continue
        seen.add(facts)
        result.append(plan)
    return tuple(sorted(result, key=lambda item: item.plan_id))


def _has_unproven_primary_solve(results: Sequence[SolverResult]) -> bool:
    return any(
        item.solve_kind.value != "counterfactual"
        and item.status
        in {
            SolverStatus.FEASIBLE_INCUMBENT,
            SolverStatus.TIMEOUT,
            SolverStatus.RESOURCE_LIMIT,
            SolverStatus.UNBOUNDED,
            SolverStatus.ERROR,
            SolverStatus.UNRESOLVED,
        }
        for item in results
    )


def _successful_audit_line(
    *,
    config: RuntimeConfig,
    attempt: int,
    input_hash: str,
    snapshot: ScenarioSnapshot,
    registry: PolicyRegistry,
    ledgers: LedgerBuildResult,
    candidates: CandidateBuildResult,
    solver_results: tuple[SolverResult, ...],
    validation: ValidationResult,
    commit: CommitResult,
    active_directives: tuple[str, ...],
    inactive_directives: tuple[str, ...],
    timings_us: Mapping[str, int],
    internal_failure_exclusions: tuple[InternalFailureExclusion, ...] = (),
    validation_pass_count: int = 1,
) -> str:
    active_rules = registry.active_rules(snapshot.configuration.current_date)
    active_rule_ids = {item.rule_id for item in active_rules}
    compiler = registry.pack.get("compiler", {})
    limits = SolverLimits()
    rejection_counts = Counter(item.code for item in candidates.rejections)
    fields: dict[str, object] = {
        "run_id": deterministic_run_id(
            input_hash=input_hash,
            snapshot_digest=snapshot.state_digest,
            contract=config.contract.value,
            attempt=attempt,
        ),
        "replan_count": attempt,
        "contract": config.contract.value,
        "model_mode": config.model_mode.value,
        "hashes": {
            "scenario_file": input_hash,
            "snapshot": f"sha256:{snapshot.state_digest}",
            "policy_pack": registry.content_hash,
            "concepts": registry.concepts_hash,
        },
        "rule_versions": {
            "pack_id": registry.pack_id,
            "schema_version": str(registry.pack.get("schema_version", "unknown")),
            "compiler_version": str(
                compiler.get("version", "unknown")
                if isinstance(compiler, Mapping)
                else "unknown"
            ),
            "rules": tuple(
                {
                    "rule_id": item.rule_id,
                    "effective_from": item.effective_from,
                    "effective_through": item.effective_through,
                }
                for item in registry.rules
            ),
        },
        "active_rule_ids": tuple(sorted(active_rule_ids)),
        "inactive_rule_ids": tuple(
            item.rule_id for item in registry.rules if item.rule_id not in active_rule_ids
        ),
        "active_directive_ids": active_directives,
        "inactive_directive_ids": inactive_directives,
        "active_evidence_document_ids": tuple(
            sorted({item.source_document for item in active_rules})
        ),
        "inactive_evidence_document_ids": tuple(
            sorted(
                {
                    item.source_document
                    for item in registry.rules
                    if item.rule_id not in active_rule_ids
                }
            )
        ),
        "component_ledgers": tuple(
            {
                "component_id": ledger.component_id,
                "total_demand": ledger.total_demand,
                "eventual_supply": ledger.eventual_supply,
                "eventual_gap": ledger.eventual_gap,
                "deadlines": tuple(
                    {
                        "due_date": item.due_date,
                        "cumulative_demand": item.cumulative_demand,
                        "on_time_supply": item.on_time_supply,
                        "on_time_gap": item.on_time_gap,
                        "recoverable_gap": item.recoverable_gap,
                    }
                    for item in ledger.deadline_positions
                ),
            }
            for ledger in ledgers.supply_ledgers
        ),
        "candidate_rejection_counts": dict(sorted(rejection_counts.items())),
        "solver_limits": {
            "time_limit_seconds": (
                str(limits.time_limit_seconds)
                if limits.time_limit_seconds is not None
                else None
            ),
            "node_limit": limits.node_limit,
        },
        "solver_outcomes": tuple(
            {
                "component_id": item.component_id,
                "solve_kind": item.solve_kind.value,
                "status": item.status.value,
                "exact_post_validated": item.exact_post_validated,
                "stages": tuple(
                    {
                        "stage_name": stage.stage_name,
                        "status": stage.status.value,
                        "certificate_complete": stage.certificate_complete,
                        "hit_resource_limit": stage.hit_resource_limit,
                        "mip_gap": stage.mip_gap,
                    }
                    for stage in item.stage_results
                ),
            }
            for item in solver_results
        ),
        "validation": {
            "is_valid": validation.is_valid,
            "completed": validation.completed,
            "exact_decimal_checks_completed": validation.exact_decimal_checks_completed,
            "solver_results_verified": validation.solver_results_verified,
            "issue_codes": tuple(item.code for item in validation.issues),
        },
        "commit": {
            "dry_run": config.dry_run,
            "committed_po_numbers": commit.committed_po_numbers,
            "inserted_alert_count": commit.inserted_alert_count,
            "deleted_alert_count": commit.deleted_alert_count,
            "no_op": commit.no_op,
        },
        "timings_us": dict(timings_us),
    }
    if internal_failure_exclusions:
        fields["partial_run"] = {
            "status": "COMPLETED_WITH_COMPONENT_EXCLUSIONS",
            "validation_pass_count": validation_pass_count,
            "excluded_component_ids": tuple(
                item.component_id for item in internal_failure_exclusions
            ),
            "excluded_requirement_ids": tuple(
                item.requirement_id for item in internal_failure_exclusions
            ),
            "validation_codes": tuple(
                sorted(
                    {
                        issue.code
                        for item in internal_failure_exclusions
                        for issue in item.issues
                    }
                )
            ),
            "owner": "PROCUREMENT_ENGINEERING",
        }
    return audit_json_line("run_completed", fields)

def _build_planning_inputs(
    snapshot: ScenarioSnapshot,
    registry: PolicyRegistry,
    contract: EvidenceContract,
    policy_parameters: ApplicablePolicyParameters,
) -> tuple[
    LedgerBuildResult,
    dict[str, EvaluationBatch],
    CandidateBuildResult,
]:
    ledgers = build_ledgers(snapshot)
    evaluations = _component_evaluations(snapshot, registry, contract, ledgers)
    candidates = _merge_component_rule_evidence(
        build_candidate_routes(
            snapshot,
            ledgers,
            registry=registry,
            contract=contract,
            policy_parameters=policy_parameters,
        ),
        evaluations,
    )
    ledgers = _route_aware_ledgers(snapshot, candidates)
    evaluations = _component_evaluations(snapshot, registry, contract, ledgers)
    candidates = _merge_component_rule_evidence(candidates, evaluations)
    no_alternative_proofs = _below_b_no_alternative_proofs(
        snapshot,
        registry,
        contract,
        ledgers,
        candidates,
        evaluations,
        policy_parameters,
    )
    if no_alternative_proofs:
        candidates = _merge_component_rule_evidence(
            build_candidate_routes(
                snapshot,
                ledgers,
                registry=registry,
                contract=contract,
                no_alternative_proofs=no_alternative_proofs,
                policy_parameters=policy_parameters,
            ),
            evaluations,
        )
        ledgers = _route_aware_ledgers(snapshot, candidates)
        evaluations = _component_evaluations(snapshot, registry, contract, ledgers)
        candidates = _merge_component_rule_evidence(candidates, evaluations)
    return ledgers, evaluations, candidates


def _snapshot_without_managed_outputs(
    snapshot: ScenarioSnapshot,
) -> ScenarioSnapshot:
    external_orders = tuple(
        order
        for order in snapshot.purchase_orders
        if parse_owned_purchase_order(order) is None
    )
    if len(external_orders) == len(snapshot.purchase_orders):
        return snapshot
    return build_snapshot(
        configuration=snapshot.configuration,
        products=snapshot.products,
        components=snapshot.components,
        suppliers=snapshot.suppliers,
        bom_lines=snapshot.bom_lines,
        catalog_lines=snapshot.catalog_lines,
        production_orders=snapshot.production_orders,
        inventory=snapshot.inventory,
        purchase_orders=external_orders,
        alerts=snapshot.alerts,
        route_input_issues=snapshot.route_input_issues,
    )


def _snapshot_for_current_reconstruction(
    snapshot: ScenarioSnapshot,
    ledgers: LedgerBuildResult,
    candidates: CandidateBuildResult,
    policy_pack_version: str,
    policy_concepts_version: str,
    contract: EvidenceContract,
) -> ScenarioSnapshot:
    demand_digests = {
        ledger.component_id: demand_fingerprint_from_facts(
            ledgers.buckets_for(ledger.component_id),
            ledger.on_hand,
            ledger.committed_inbound,
        )
        for ledger in ledgers.supply_ledgers
    }
    current_numbers = current_managed_order_numbers(
        snapshot,
        demand_digests,
        policy_pack_version,
        contract,
        candidates.routes,
        policy_concepts_version=policy_concepts_version,
        candidate_rejections=candidates.rejections,
        candidate_alerts=candidates.alerts,
    )
    if not current_numbers:
        return snapshot
    return build_snapshot(
        configuration=snapshot.configuration,
        products=snapshot.products,
        components=snapshot.components,
        suppliers=snapshot.suppliers,
        bom_lines=snapshot.bom_lines,
        catalog_lines=snapshot.catalog_lines,
        production_orders=snapshot.production_orders,
        inventory=snapshot.inventory,
        purchase_orders=tuple(
            order
            for order in snapshot.purchase_orders
            if order.po_number not in current_numbers
        ),
        alerts=snapshot.alerts,
        route_input_issues=snapshot.route_input_issues,
    )


def _run_once(
    config: RuntimeConfig,
    scenario_path: Path,
    *,
    attempt: int,
    _test_validation_issue_injector: TestValidationIssueInjector | None = None,
) -> RunArtifacts:
    """Plan, validate, and commit one optimistic-concurrency attempt."""

    timings_us: dict[str, int] = {}
    run_started = perf_counter_ns()
    input_hash = file_sha256(scenario_path)

    phase_started = perf_counter_ns()
    snapshot = SQLiteRepository().load_snapshot(scenario_path)
    timings_us["snapshot_load"] = (perf_counter_ns() - phase_started) // 1_000

    phase_started = perf_counter_ns()
    registry = load_policy_registry()
    policy_parameters = registry.parameters_for(snapshot.configuration.current_date)
    timings_us["policy_load"] = (perf_counter_ns() - phase_started) // 1_000

    phase_started = perf_counter_ns()
    ownership_snapshot = _snapshot_without_managed_outputs(snapshot)
    ownership_ledgers, ownership_evaluations, ownership_candidates = _build_planning_inputs(
        ownership_snapshot,
        registry,
        config.contract,
        policy_parameters,
    )
    if ownership_snapshot is snapshot:
        current_ledgers, current_evaluations, current_candidates = (
            ownership_ledgers,
            ownership_evaluations,
            ownership_candidates,
        )
    else:
        current_ledgers, current_evaluations, current_candidates = _build_planning_inputs(
            snapshot,
            registry,
            config.contract,
            policy_parameters,
        )
    planning_snapshot = _snapshot_for_current_reconstruction(
        snapshot,
        current_ledgers,
        ownership_candidates,
        registry.content_hash,
        registry.concepts_hash,
        config.contract,
    )
    if planning_snapshot is snapshot:
        ledgers, evaluations, candidates = (
            current_ledgers,
            current_evaluations,
            current_candidates,
        )
    else:
        ledgers, evaluations, candidates = _build_planning_inputs(
            planning_snapshot,
            registry,
            config.contract,
            policy_parameters,
        )
    timings_us["ledger_build"] = (perf_counter_ns() - phase_started) // 1_000
    timings_us["candidate_build"] = (perf_counter_ns() - phase_started) // 1_000

    component_ids = tuple(item.component_id for item in ledgers.supply_ledgers)
    if (
        config.explain_component_id is not None
        and config.explain_component_id not in component_ids
    ):
        raise CliUsageError(
            f"--explain component is not present in scenario demand: "
            f"{config.explain_component_id}"
        )

    decisions: list[DecisionRecord] = []
    solver_results: list[SolverResult] = []
    optimizer = ProcurementOptimizer()
    phase_started = perf_counter_ns()
    for component_id in component_ids:
        ledger = ledgers.ledger_for(component_id)
        batch = evaluations[component_id]
        if (
            ledger.eventual_gap == ZERO
            and total_recovery_demand(ledger) == ZERO
        ):
            decisions.append(
                _closed_decision(
                    planning_snapshot,
                    config.contract,
                    ledgers,
                    batch,
                    candidates,
                    registry,
                    component_id,
                )
            )
            continue
        problem = _optimizer_problem(
            planning_snapshot,
            registry,
            ledgers,
            candidates,
            evaluations,
            component_id,
            policy_parameters,
        )
        outcome = optimizer.optimize(problem)
        ownership_source_fingerprint = (
            component_source_fingerprint(
                ownership_snapshot,
                component_id,
                registry.content_hash,
                config.contract,
                policy_concepts_version=registry.concepts_hash,
                candidate_routes=ownership_candidates.routes,
                candidate_rejections=ownership_candidates.rejections,
                candidate_alerts=ownership_candidates.alerts,
            )
            if getattr(outcome, "selected_plan") is not None
            else None
        )
        results = _solver_results(outcome)
        solver_results.extend(results)
        decisions.append(
            _planned_decision(
                planning_snapshot,
                registry,
                config.contract,
                ledgers,
                candidates,
                batch,
                outcome,
                component_id,
                source_fingerprint=ownership_source_fingerprint,
            )
        )
    timings_us["optimization"] = (perf_counter_ns() - phase_started) // 1_000

    planned = tuple(sorted(decisions, key=lambda item: item.component_id))
    result_tuple = tuple(solver_results)
    phase_started = perf_counter_ns()
    validator = IndependentPlanValidator(
        registry,
        policy_parameters=policy_parameters,
        validation_pass=1,
        test_issue_injector=_test_validation_issue_injector,
    )
    validation = validator.validate(planning_snapshot, planned, result_tuple)
    warnings = tuple(
        item
        for item in validation.issues
        if item.severity is ValidationSeverity.WARNING
    )
    if config.strict and warnings:
        details = "; ".join(f"{item.code}: {item.message}" for item in warnings)
        raise PlanningFailure(f"strict validation rejected warnings: {details}")
    if _has_unproven_primary_solve(result_tuple):
        raise PlanningFailure("a primary optimization solve did not complete with a certificate")

    internal_failure_exclusions: tuple[InternalFailureExclusion, ...] = ()
    validation_pass_count = 1
    if not validation.is_valid:
        errors = tuple(
            item
            for item in validation.issues
            if item.severity is ValidationSeverity.ERROR
        )
        safely_classified = (
            validation.completed
            and validation.exact_decimal_checks_completed
            and validation.solver_results_verified
            and bool(errors)
            and all(is_containable_component_error(item) for item in errors)
        )
        if config.strict or not safely_classified:
            details = "; ".join(
                f"{item.code}: {item.message}" for item in validation.issues
            ) or "validation did not complete all required proof checks"
            raise PlanningFailure(f"independent validation failed: {details}")

        requirement_ids = {
            item.component_id: item.requirement_id for item in planned
        }
        internal_failure_exclusions = build_internal_failure_exclusions(
            errors,
            requirement_ids,
        )
        excluded_components = {
            item.component_id for item in internal_failure_exclusions
        }
        if not excluded_components or len(excluded_components) >= len(planned):
            raise PlanningFailure(
                "component isolation requires at least one independently valid survivor"
            )

        # Existing managed commitments are immutable.  A partial run may not
        # represent one as a current action for an excluded component, so it
        # fails globally instead of silently leaving a stale managed PO.
        for order in snapshot.purchase_orders:
            parsed = parse_owned_purchase_order(order)
            if parsed is not None and order.component_id in excluded_components:
                raise PlanningFailure(
                    "component isolation cannot proceed while an excluded component "
                    "has an existing managed purchase order"
                )

        planned = tuple(
            item
            for item in planned
            if item.component_id not in excluded_components
        )
        result_tuple = tuple(
            item
            for item in result_tuple
            if item.component_id not in excluded_components
        )
        survivor_validator = IndependentPlanValidator(
            registry,
            policy_parameters=policy_parameters,
            validation_pass=2,
            test_issue_injector=_test_validation_issue_injector,
        )
        validation = survivor_validator.validate(
            planning_snapshot,
            planned,
            result_tuple,
            exclusions=internal_failure_exclusions,
        )
        validation_pass_count = 2
        if not validation.is_valid:
            details = "; ".join(
                f"{item.code}: {item.message}" for item in validation.issues
            ) or "survivor validation did not complete all required proof checks"
            raise PlanningFailure(
                f"independent survivor revalidation failed: {details}"
            )
    timings_us["validation"] = (perf_counter_ns() - phase_started) // 1_000

    reconciled = reconcile_managed_decisions(
        snapshot, planned, registry.content_hash
    )
    active = tuple(
        item.rule_id
        for item in registry.active_rules(snapshot.configuration.current_date)
        if item.data.get("directive") is not None
    )
    inactive = tuple(
        item.rule_id
        for item in registry.rules
        if item.data.get("directive") is not None and item.rule_id not in active
    )
    outputs = build_decision_outputs(
        reconciled,
        registry.content_hash,
        policy_registry=registry,
        active_directives=active,
        inactive_directives=inactive,
        visible_alert_prefixes=config.alert_prefixes,
        route_input_issues=snapshot.route_input_issues,
        internal_failure_exclusions=internal_failure_exclusions,
    )
    phase_started = perf_counter_ns()
    commit = commit_decisions(
        scenario_path,
        snapshot,
        reconciled,
        validation,
        registry.content_hash,
        dry_run=config.dry_run,
        policy_registry=registry,
        active_directives=active,
        inactive_directives=inactive,
        visible_alert_prefixes=config.alert_prefixes,
        internal_failure_exclusions=internal_failure_exclusions,
    )
    timings_us["commit"] = (perf_counter_ns() - phase_started) // 1_000
    timings_us["total"] = (perf_counter_ns() - run_started) // 1_000

    audit_line = _successful_audit_line(
        config=config,
        attempt=attempt,
        input_hash=input_hash,
        snapshot=snapshot,
        registry=registry,
        ledgers=ledgers,
        candidates=candidates,
        solver_results=result_tuple,
        validation=validation,
        commit=commit,
        active_directives=active,
        inactive_directives=inactive,
        timings_us=timings_us,
        internal_failure_exclusions=internal_failure_exclusions,
        validation_pass_count=validation_pass_count,
    )
    return RunArtifacts(
        snapshot=snapshot,
        registry=registry,
        ledgers=ledgers,
        candidates=candidates,
        decisions=reconciled,
        solver_results=result_tuple,
        validation=validation,
        outputs=outputs,
        commit=commit,
        audit_json_lines=(audit_line,),
        internal_failure_exclusions=internal_failure_exclusions,
        validation_pass_count=validation_pass_count,
    )


def run(
    config: RuntimeConfig,
    *,
    _test_validation_issue_injector: TestValidationIssueInjector | None = None,
) -> RunArtifacts:
    """Execute one deterministic run, replanning once on a stale snapshot."""

    if config.model_mode is ModelMode.REQUIRED:
        raise OptionalModelUnavailable(
            "--llm=required is unavailable because no optional model adapter is configured "
            "for live planning"
        )

    scenario_path = resolve_scenario_path(config.scenario_path)
    for attempt in range(2):
        try:
            return _run_once(
                config,
                scenario_path,
                attempt=attempt,
                _test_validation_issue_injector=_test_validation_issue_injector,
            )
        except ConcurrentModificationError as error:
            if attempt == 1:
                raise ConcurrentModificationError(
                    "scenario changed again after one full replan; no duplicate decision "
                    "writes were made"
                ) from error
    raise AssertionError("unreachable concurrency retry state")


def _result_payload(config: RuntimeConfig, artifacts: RunArtifacts) -> dict[str, object]:
    explained = (
        tuple(
            item
            for item in artifacts.decisions
            if item.component_id == config.explain_component_id
        )
        if config.explain_component_id is not None
        else artifacts.decisions
    )
    partial = bool(artifacts.internal_failure_exclusions)
    payload: dict[str, object] = {
        "status": (
            "partial-dry-run"
            if partial and config.dry_run
            else "partially-committed"
            if partial
            else "dry-run"
            if config.dry_run
            else "committed"
        ),
        "contract": config.contract,
        "model_mode": config.model_mode,
        "policy_pack_id": artifacts.registry.pack_id,
        "policy_pack_hash": artifacts.registry.content_hash,
        "snapshot_digest": artifacts.snapshot.state_digest,
        "decision_count": len(artifacts.decisions),
        "purchase_order_count": len(artifacts.outputs.purchase_orders),
        "owned_alert_count": len(artifacts.outputs.alerts),
        "decisions": explained,
        "validation": artifacts.validation,
        "commit": artifacts.commit,
    }
    if partial:
        payload["partial_run"] = {
            "status": "COMPLETED_WITH_COMPONENT_EXCLUSIONS",
            "validation_pass_count": artifacts.validation_pass_count,
            "excluded_components": artifacts.internal_failure_exclusions,
            "owner": "PROCUREMENT_ENGINEERING",
        }
    return payload


def render_result(config: RuntimeConfig, artifacts: RunArtifacts) -> str:
    """Render deterministic machine or human output from completed artifacts."""

    if config.json_output:
        return canonical_dumps(_result_payload(config, artifacts))
    partial = bool(artifacts.internal_failure_exclusions)
    status = (
        "partial-dry-run"
        if partial and config.dry_run
        else "partially-committed"
        if partial
        else "dry-run"
        if config.dry_run
        else "committed"
    )
    lines = [
        "Apex procurement run validated: "
        f"contract={config.contract.value}; decisions={len(artifacts.decisions)}; "
        f"purchase_orders={len(artifacts.outputs.purchase_orders)}; "
        f"alerts={len(artifacts.outputs.alerts)}; "
        f"status={status}."
    ]
    if partial:
        lines.append(
            "Engineering-owned component exclusions: "
            + ", ".join(
                item.component_id
                for item in artifacts.internal_failure_exclusions
            )
            + f"; independent validation passes={artifacts.validation_pass_count}."
        )
    if config.explain_component_id is not None:
        decision = next(
            item
            for item in artifacts.decisions
            if item.component_id == config.explain_component_id
        )
        lines.append(decision.rationale)
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI boundary with the operations exit-code contract from section 14."""

    error_type = "UnknownError"
    try:
        config = parse_config(argv)
        artifacts = run(config)
        for line in artifacts.audit_json_lines:
            print(line, file=sys.stderr)
        print(render_result(config, artifacts))
        return ExitCode.SUCCESS
    except (CliUsageError, ScenarioPathError) as error:
        code = ExitCode.CLI_OR_PATH
        message = str(error)
        error_type = type(error).__name__
    except RepositoryLoadError as error:
        code = ExitCode.INVALID_SCENARIO
        message = str(error)
        error_type = type(error).__name__
    except (PolicyValidationError, OptionalModelUnavailable) as error:
        code = ExitCode.INVALID_POLICY
        message = str(error)
        error_type = type(error).__name__
    except PlanningFailure as error:
        code = ExitCode.SOLVER_OR_VALIDATOR
        message = str(error)
        error_type = type(error).__name__
    except ConcurrentModificationError as error:
        code = ExitCode.CONCURRENT_MODIFICATION
        message = str(error)
        error_type = type(error).__name__
    except (CommitFailure, DecisionError) as error:
        code = ExitCode.COMMIT_FAILURE
        message = str(error)
        error_type = type(error).__name__
    except (TypeError, ValueError, ArithmeticError) as error:
        code = ExitCode.SOLVER_OR_VALIDATOR
        message = str(error)
        error_type = type(error).__name__
    print(
        audit_json_line(
            "run_failed",
            {
                "exit_code": int(code),
                "error_type": error_type,
            },
        ),
        file=sys.stderr,
    )
    print(f"error: {sanitize_control_characters(message)}", file=sys.stderr)
    return code


__all__ = [
    "ExitCode",
    "RunArtifacts",
    "build_parser",
    "main",
    "parse_config",
    "render_result",
    "run",
]
