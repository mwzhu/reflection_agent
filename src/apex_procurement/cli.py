"""Offline command line orchestration for the Apex procurement planner.

The module deliberately contains only application wiring.  Source loading,
policy decisions, allocation, independent validation, rendering, and writes
remain owned by their respective public subsystem boundaries.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import IntEnum
from pathlib import Path
import sys
from time import perf_counter_ns

from .audit import audit_json_line, deterministic_run_id, file_sha256

from .candidates import (
    CandidateBuildResult,
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
    reconcile_managed_decisions,
)
from .domain import (
    AlertCategory,
    CandidatePlan,
    CandidateRoute,
    CommitResult,
    DecisionRecord,
    EvidenceScope,
    EvidenceStatus,
    FulfillmentStatus,
    PlanDisposition,
    RequirementState,
    ResolutionStatus,
    RuleSeverity,
    ScenarioSnapshot,
    SolveKind,
    SolverResult,
    SolverStatus,
    ValidationResult,
    ValidationSeverity,
    ZERO,
)
from .explanations import render_decision_rationale
from .ledgers import LedgerBuildResult, build_ledgers
from .optimizer import (
    IntegerScaledSolver,
    OptimizerProblem,
    OrderApprovalConstraint,
    ProcurementOptimizer,
    SolverLimits,
)
from .policy.entity_resolution import EntityResolver
from .policy.evaluator import EvaluationBatch, EvaluationContext, PolicyEvaluator
from .policy.registry import PolicyRegistry, PolicyRule, load_policy_registry
from .policy.schema import PolicyValidationError
from .repository import (
    SQLiteRepository,
    ScenarioDataError,
    ScenarioLoadError,
    ScenarioPathError,
    ScenarioSchemaError,
    resolve_scenario_path,
)
from .serialization import canonical_dumps, sanitize_control_characters
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
        "--recompile-policy",
        action="store_true",
        help="request offline policy-pack recompilation before planning",
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
        recompile_policy=args.recompile_policy,
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
    return CandidateBuildResult(
        routes=tuple(routes),
        rejections=candidates.rejections,
        alerts=candidates.alerts,
    )


def _applicable_rule_ids(batch: EvaluationBatch) -> frozenset[str]:
    return frozenset(
        item.rule_id
        for item in batch.active
        if item.applicable is True and item.selector_status is EvidenceStatus.PASS
    )


def _optimizer_problem(
    snapshot: ScenarioSnapshot,
    registry: PolicyRegistry,
    ledgers: LedgerBuildResult,
    candidates: CandidateBuildResult,
    evaluations: Mapping[str, EvaluationBatch],
    component_id: str,
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
    order_approvals: list[OrderApprovalConstraint] = []

    for rule in registry.active_rules(snapshot.configuration.current_date):
        kind = _rule_kind(rule)
        body = _rule_body(rule)
        if kind == "order_value_approval":
            order_approvals.append(
                OrderApprovalConstraint(
                    rule_id=rule.rule_id,
                    maximum_without_approval=Decimal(str(body["amount_exceeds"])),
                    approving_authority=str(body["authority"]),
                )
            )
        elif kind == "catalog_minimum_order_quantity":
            moq_rule_id = rule.rule_id
        elif kind == "sub_moq_written_approval":
            sub_moq_approval_rule_id = rule.rule_id
        elif kind == "minimum_secondary_fraction" and rule.rule_id in applicable:
            minimum_secondary_fraction = Decimal(str(body["value"]))
            minimum_secondary_rule_id = rule.rule_id
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
        routes=candidates.routes_for(component_id),
        demand_buckets=ledgers.buckets_for(component_id),
        supply_ledger=ledger,
        suppliers=snapshot.suppliers,
        minimum_secondary_fraction=minimum_secondary_fraction,
        minimum_secondary_rule_id=minimum_secondary_rule_id,
        named_primary_supplier_id=named_primary_supplier_id,
        named_primary_rule_id=named_primary_rule_id,
        order_approval_constraints=tuple(order_approvals),
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
) -> tuple[NoAlternativeProof, ...]:
    """Run the planner/validator predicate pair required by the below-B gate."""

    demanded_components = {
        ledger.component_id
        for ledger in ledgers.supply_ledgers
        if ledger.eventual_gap > ZERO
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
    validator = IndependentPlanValidator(registry)
    proofs: list[NoAlternativeProof] = []
    for component_id in sorted(pending):
        problem = _optimizer_problem(
            snapshot,
            registry,
            ledgers,
            candidates,
            evaluations,
            component_id,
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


def _component_rule_evidence(batch: EvaluationBatch) -> tuple[object, ...]:
    return tuple(
        evaluation.evidence
        for evaluation in batch.active
        if evaluation.applicable is not False
        and evaluation.evidence is not None
        and evaluation.evidence.scope is EvidenceScope.RULE
    )


def _plan_allocations(plans: Sequence[CandidatePlan]) -> dict[str, Decimal]:
    allocations: dict[str, Decimal] = {}
    for plan in plans:
        for line in plan.lines:
            allocations[line.supplier_id] = (
                allocations.get(line.supplier_id, ZERO) + line.quantity
            )
    return allocations


def _decision_categories(
    snapshot: ScenarioSnapshot,
    registry: PolicyRegistry,
    contract: EvidenceContract,
    ledgers: LedgerBuildResult,
    candidates: CandidateBuildResult,
    batch: EvaluationBatch,
    component_id: str,
    outcome: object | None,
) -> tuple[AlertCategory, ...]:
    categories = {
        item.category
        for item in ledgers.alerts
        if item.component_id in {None, component_id}
    }
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
    if outcome is None:
        return tuple(sorted(categories, key=lambda item: item.value))

    categories.update(item.category for item in getattr(outcome, "alerts"))
    selected = getattr(outcome, "selected_plan")
    alternatives = getattr(outcome, "alternatives")
    residual = getattr(outcome, "residual_gap")
    if residual > ZERO:
        categories.add(AlertCategory.UNMET_DEMAND)
    if selected is None and not any(
        route.may_enter_executable_model and route.feasible_deadlines
        for route in candidates.routes_for(component_id)
    ):
        categories.add(AlertCategory.NO_ELIGIBLE_SUPPLIER)
    if (
        selected is not None
        and selected.disposition is PlanDisposition.EXECUTE_WITH_ASSUMPTION
    ):
        categories.update({AlertCategory.ASSUMPTION, AlertCategory.EVIDENCE_CONTRACT})

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


def _closed_decision(
    snapshot: ScenarioSnapshot,
    contract: EvidenceContract,
    ledgers: LedgerBuildResult,
    batch: EvaluationBatch,
    candidates: CandidateBuildResult,
    registry: PolicyRegistry,
    component_id: str,
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
) -> DecisionRecord:
    ledger = ledgers.ledger_for(component_id)
    selected = getattr(outcome, "selected_plan")
    alternatives = _nonredundant_alternatives(
        selected, getattr(outcome, "alternatives")
    )
    planned_coverage = selected.eventual_covered_quantity if selected is not None else ZERO
    existing_coverage = min(ledger.total_demand, ledger.eventual_supply)
    residual = getattr(outcome, "residual_gap")
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
        requirement_state=getattr(outcome, "requirement_state"),
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
        ),
        rationale="Pending deterministic rendering.",
    )
    return replace(decision, rationale=render_decision_rationale(decision))


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
) -> tuple[CandidatePlan, ...]:
    """Keep only counterfactuals that actually change the selected action.

    A relaxed rule that produces the identical plan proves the relaxation was
    unnecessary.  It is neither an approval proposal nor a second semantic
    action and must not enter the recommendation set.
    """

    selected_facts = _plan_line_facts(selected) if selected is not None else None
    result: list[CandidatePlan] = []
    seen: set[tuple[tuple[object, ...], ...]] = set()
    for plan in alternatives:
        facts = _plan_line_facts(plan)
        if selected_facts is not None and facts == selected_facts:
            continue
        if facts in seen:
            continue
        seen.add(facts)
        if (
            plan.disposition is PlanDisposition.RECOMMEND_APPROVAL
            and not plan.unresolved_approval_ids
        ):
            plan = replace(
                plan, unresolved_approval_ids=plan.relaxed_rule_ids
            )
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
    return audit_json_line("run_completed", fields)


def _run_once(
    config: RuntimeConfig,
    scenario_path: Path,
    *,
    attempt: int,
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
    timings_us["policy_load"] = (perf_counter_ns() - phase_started) // 1_000

    phase_started = perf_counter_ns()
    ledgers = build_ledgers(snapshot)
    timings_us["ledger_build"] = (perf_counter_ns() - phase_started) // 1_000
    phase_started = perf_counter_ns()
    evaluations = _component_evaluations(
        snapshot, registry, config.contract, ledgers
    )
    candidates = _merge_component_rule_evidence(
        build_candidate_routes(
            snapshot,
            ledgers,
            registry=registry,
            contract=config.contract,
        ),
        evaluations,
    )
    no_alternative_proofs = _below_b_no_alternative_proofs(
        snapshot,
        registry,
        config.contract,
        ledgers,
        candidates,
        evaluations,
    )
    if no_alternative_proofs:
        candidates = _merge_component_rule_evidence(
            build_candidate_routes(
                snapshot,
                ledgers,
                registry=registry,
                contract=config.contract,
                no_alternative_proofs=no_alternative_proofs,
            ),
            evaluations,
        )
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
        if ledger.eventual_gap == ZERO:
            decisions.append(
                _closed_decision(
                    snapshot,
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
            snapshot,
            registry,
            ledgers,
            candidates,
            evaluations,
            component_id,
        )
        outcome = optimizer.optimize(problem)
        results = _solver_results(outcome)
        solver_results.extend(results)
        decisions.append(
            _planned_decision(
                snapshot,
                registry,
                config.contract,
                ledgers,
                candidates,
                batch,
                outcome,
                component_id,
            )
        )
    timings_us["optimization"] = (perf_counter_ns() - phase_started) // 1_000

    planned = tuple(sorted(decisions, key=lambda item: item.component_id))
    result_tuple = tuple(solver_results)
    phase_started = perf_counter_ns()
    validator = IndependentPlanValidator(registry)
    validation = validator.validate(snapshot, planned, result_tuple)
    timings_us["validation"] = (perf_counter_ns() - phase_started) // 1_000
    if not validation.is_valid:
        details = "; ".join(
            f"{item.code}: {item.message}" for item in validation.issues
        )
        raise PlanningFailure(f"independent validation failed: {details}")
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
    )
    return RunArtifacts(
        snapshot,
        registry,
        ledgers,
        candidates,
        reconciled,
        result_tuple,
        validation,
        outputs,
        commit,
        (audit_line,),
    )


def run(config: RuntimeConfig) -> RunArtifacts:
    """Execute one deterministic run, replanning once on a stale snapshot."""

    if config.recompile_policy:
        raise OptionalModelUnavailable(
            "live policy recompilation is unavailable; use the reviewed compiled policy pack"
        )
    if config.model_mode is ModelMode.REQUIRED:
        raise OptionalModelUnavailable(
            "--llm=required is unavailable because no optional model adapter is installed"
        )

    scenario_path = resolve_scenario_path(config.scenario_path)
    for attempt in range(2):
        try:
            return _run_once(config, scenario_path, attempt=attempt)
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
    return {
        "status": "dry-run" if config.dry_run else "committed",
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


def render_result(config: RuntimeConfig, artifacts: RunArtifacts) -> str:
    """Render deterministic machine or human output from completed artifacts."""

    if config.json_output:
        return canonical_dumps(_result_payload(config, artifacts))
    lines = [
        "Apex procurement run validated: "
        f"contract={config.contract.value}; decisions={len(artifacts.decisions)}; "
        f"purchase_orders={len(artifacts.outputs.purchase_orders)}; "
        f"alerts={len(artifacts.outputs.alerts)}; "
        f"status={'dry-run' if config.dry_run else 'committed'}."
    ]
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
    except (ScenarioDataError, ScenarioSchemaError, ScenarioLoadError) as error:
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
