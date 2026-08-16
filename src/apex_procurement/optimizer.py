"""Certified integer-scaled procurement optimization.

The optimizer deliberately owns its problem description.  Shared domain
objects are frozen contracts; this module translates them into a small integer
linear model and translates a certified solution back into those contracts.

SciPy/HiGHS is the preferred backend.  It is imported lazily so the default
offline program still imports on machines where SciPy is absent.  A bounded,
exact stdlib branch-and-bound backend is provided for small instances.  The
fallback returns ``UNRESOLVED`` when its node budget is exhausted; it never
turns an incomplete search into an infeasibility or optimality claim.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal, ROUND_CEILING
from enum import Enum
from fractions import Fraction
import hashlib
import json
from math import gcd
from typing import Protocol

from .domain import (
    AlertCategory,
    BucketAllocation,
    CandidatePlan,
    CandidateRoute,
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
    Supplier,
    SupplyLedger,
    DemandBucket,
    ZERO,
)
from .ledgers import DEFAULT_UNIT_OF_MEASURE_CONTRACT


def _require_decimal(value: Decimal, name: str, *, nonnegative: bool = True) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    if nonnegative and value < ZERO:
        raise ValueError(f"{name} must be nonnegative")


def _fraction(value: Decimal | int | Fraction) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Fraction(value)
    if isinstance(value, Decimal):
        return Fraction(value)
    raise TypeError(f"cannot convert {type(value).__name__} to Fraction")


def _decimal(value: Fraction) -> Decimal:
    return Decimal(value.numerator) / Decimal(value.denominator)


def _lcm(left: int, right: int) -> int:
    return abs(left * right) // gcd(left, right) if left and right else 0


def _canonical_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class SecondaryShortageKind(str, Enum):
    """Why a required second eligible supplier is unavailable."""

    STRUCTURAL = "structural"
    RELAXABLE = "relaxable"


@dataclass(frozen=True, slots=True)
class SupplierVolume:
    supplier_id: str
    quantity: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.supplier_id, str) or not self.supplier_id.strip():
            raise ValueError("supplier_id must be non-empty text")
        _require_decimal(self.quantity, "quantity")


@dataclass(frozen=True, slots=True)
class ConcentrationConstraint:
    """One provable rolling-volume cap.

    Candidate/policy evaluation decides whether the evidence basis is
    satisfiable.  Therefore merely constructing this value authorizes emission
    of the corresponding model row.
    """

    rule_id: str
    maximum_fraction: Decimal
    existing_total_volume: Decimal
    existing_supplier_volumes: tuple[SupplierVolume, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.rule_id, str) or not self.rule_id.strip():
            raise ValueError("rule_id must be non-empty text")
        _require_decimal(self.maximum_fraction, "maximum_fraction")
        if not ZERO < self.maximum_fraction <= Decimal("1"):
            raise ValueError("maximum_fraction must be in (0, 1]")
        _require_decimal(self.existing_total_volume, "existing_total_volume")
        volumes = tuple(self.existing_supplier_volumes)
        if any(not isinstance(item, SupplierVolume) for item in volumes):
            raise TypeError("existing_supplier_volumes contains an invalid value")
        if len({item.supplier_id for item in volumes}) != len(volumes):
            raise ValueError("existing_supplier_volumes contains duplicate suppliers")
        if sum((item.quantity for item in volumes), ZERO) > self.existing_total_volume:
            raise ValueError("supplier history cannot exceed total history")
        object.__setattr__(
            self,
            "existing_supplier_volumes",
            tuple(sorted(volumes, key=lambda item: item.supplier_id)),
        )


@dataclass(frozen=True, slots=True)
class ExceptionAllowance:
    """An independently determined aggregate exception scope."""

    exception_id: str
    qualifying_deadlines: tuple[date, ...]
    maximum_quantity: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.exception_id, str) or not self.exception_id.strip():
            raise ValueError("exception_id must be non-empty text")
        deadlines = tuple(sorted(set(self.qualifying_deadlines)))
        if not deadlines or any(not isinstance(item, date) for item in deadlines):
            raise ValueError("qualifying_deadlines must contain dates")
        if self.maximum_quantity is not None:
            _require_decimal(self.maximum_quantity, "maximum_quantity")
        object.__setattr__(self, "qualifying_deadlines", deadlines)


@dataclass(frozen=True, slots=True)
class EconomicAutonomy:
    max_surplus_fraction: Decimal = Decimal("0.10")
    max_surplus_units: Decimal | None = None
    max_excess_cost_usd: Decimal = Decimal("2500")
    forced_surplus_review_usd: Decimal = Decimal("2500")
    provisional: bool = True

    def __post_init__(self) -> None:
        _require_decimal(self.max_surplus_fraction, "max_surplus_fraction")
        if self.max_surplus_units is not None:
            _require_decimal(self.max_surplus_units, "max_surplus_units")
        _require_decimal(self.max_excess_cost_usd, "max_excess_cost_usd")
        _require_decimal(
            self.forced_surplus_review_usd,
            "forced_surplus_review_usd",
        )
        if not isinstance(self.provisional, bool):
            raise TypeError("provisional must be bool")


@dataclass(frozen=True, slots=True)
class OrderApprovalConstraint:
    """A quantity-dependent order-value approval gate."""

    rule_id: str
    maximum_without_approval: Decimal
    approving_authority: str

    def __post_init__(self) -> None:
        if not isinstance(self.rule_id, str) or not self.rule_id.strip():
            raise ValueError("rule_id must be non-empty text")
        _require_decimal(
            self.maximum_without_approval,
            "maximum_without_approval",
        )
        if not isinstance(self.approving_authority, str) or not self.approving_authority.strip():
            raise ValueError("approving_authority must be non-empty text")


@dataclass(frozen=True, slots=True)
class OptimizerProblem:
    """One per-component solve over immutable source facts."""

    component_id: str
    unit_of_measure: str
    net_requirement: Decimal
    routes: tuple[CandidateRoute, ...]
    demand_buckets: tuple[DemandBucket, ...]
    supply_ledger: SupplyLedger
    suppliers: tuple[Supplier, ...]
    solve_kind: SolveKind = SolveKind.QUANTITY_CALIBRATION
    minimum_secondary_fraction: Decimal | None = None
    minimum_secondary_rule_id: str | None = None
    named_primary_supplier_id: str | None = None
    named_primary_rule_id: str | None = None
    concentration_constraints: tuple[ConcentrationConstraint, ...] = ()
    order_approval_constraints: tuple[OrderApprovalConstraint, ...] = ()
    approved_order_rule_ids: tuple[str, ...] = ()
    exception_allowances: tuple[ExceptionAllowance, ...] = ()
    autonomy: EconomicAutonomy = field(default_factory=EconomicAutonomy)
    authorized_recovery_surplus: Decimal = ZERO
    max_allocation_driven_surplus: Decimal | None = None
    minimum_compliant_total: Decimal | None = None
    coverage_target: Decimal | None = None
    cheapest_covering_cost: Decimal | None = None
    relaxed_rule_id: str | None = None
    relaxation_rule_ids: tuple[str, ...] = ()
    moq_rule_id: str | None = None
    sub_moq_approval_rule_id: str | None = None
    secondary_shortage_kind: SecondaryShortageKind | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.component_id, str) or not self.component_id.strip():
            raise ValueError("component_id must be non-empty text")
        if not isinstance(self.unit_of_measure, str) or not self.unit_of_measure.strip():
            raise ValueError("unit_of_measure must be non-empty text")
        _require_decimal(self.net_requirement, "net_requirement")
        if not isinstance(self.supply_ledger, SupplyLedger):
            raise TypeError("supply_ledger must be SupplyLedger")
        if self.supply_ledger.component_id != self.component_id:
            raise ValueError("supply_ledger must match component_id")
        if self.net_requirement != self.supply_ledger.eventual_gap:
            raise ValueError("net_requirement must equal supply_ledger.eventual_gap")
        routes = tuple(self.routes)
        buckets = tuple(sorted(self.demand_buckets, key=lambda item: item.due_date))
        suppliers = tuple(self.suppliers)
        if any(not isinstance(item, CandidateRoute) for item in routes):
            raise TypeError("routes contains an invalid item")
        if any(item.component_id != self.component_id for item in routes):
            raise ValueError("all routes must match component_id")
        if len({item.route_id for item in routes}) != len(routes):
            raise ValueError("routes contains duplicate route IDs")
        if not buckets or any(not isinstance(item, DemandBucket) for item in buckets):
            raise ValueError("demand_buckets must contain DemandBucket values")
        if any(item.component_id != self.component_id for item in buckets):
            raise ValueError("all demand buckets must match component_id")
        if any(not isinstance(item, Supplier) for item in suppliers):
            raise TypeError("suppliers contains an invalid item")
        supplier_ids = {item.supplier_id for item in suppliers}
        if any(item.supplier_id not in supplier_ids for item in routes):
            raise ValueError("every route requires its supplier row")
        if not isinstance(self.solve_kind, SolveKind):
            raise TypeError("solve_kind must be SolveKind")
        for name in (
            "authorized_recovery_surplus",
            "max_allocation_driven_surplus",
            "minimum_compliant_total",
            "coverage_target",
            "cheapest_covering_cost",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_decimal(value, name)
        if self.minimum_secondary_fraction is not None:
            _require_decimal(
                self.minimum_secondary_fraction,
                "minimum_secondary_fraction",
            )
            if not ZERO < self.minimum_secondary_fraction < Decimal("1"):
                raise ValueError("minimum_secondary_fraction must be in (0, 1)")
            if not self.minimum_secondary_rule_id:
                raise ValueError("minimum_secondary_rule_id is required with the fraction")
        if self.named_primary_supplier_id is not None:
            if self.named_primary_supplier_id not in supplier_ids:
                raise ValueError("named primary supplier is absent from suppliers")
            if not self.named_primary_rule_id:
                raise ValueError("named_primary_rule_id is required with a named supplier")
        if self.solve_kind is SolveKind.COUNTERFACTUAL and not self.relaxed_rule_id:
            raise ValueError("counterfactual solves require exactly one relaxed_rule_id")
        if self.solve_kind is SolveKind.EXECUTABLE and self.relaxed_rule_id:
            raise ValueError("the executable solve may not relax a rule")
        # Q and 0 may carry the same single rule only as private calibration
        # companions for a solve-2 run.  They remain non-executable and never
        # enter the selected-plan gate.
        if self.solve_kind in {SolveKind.EXECUTABLE, SolveKind.COUNTERFACTUAL}:
            if self.minimum_compliant_total is None or self.coverage_target is None:
                raise ValueError("decision solves require solve-Q calibration values")
            if self.cheapest_covering_cost is None:
                raise ValueError("decision solves require solve-0 baseline cost")
        concentrations = tuple(self.concentration_constraints)
        order_approvals = tuple(self.order_approval_constraints)
        allowances = tuple(self.exception_allowances)
        if any(not isinstance(item, ConcentrationConstraint) for item in concentrations):
            raise TypeError("concentration_constraints contains an invalid item")
        if any(not isinstance(item, OrderApprovalConstraint) for item in order_approvals):
            raise TypeError("order_approval_constraints contains an invalid item")
        if len({item.rule_id for item in order_approvals}) != len(order_approvals):
            raise ValueError("order_approval_constraints contains duplicate rule IDs")
        if any(not isinstance(item, ExceptionAllowance) for item in allowances):
            raise TypeError("exception_allowances contains an invalid item")
        if len({item.exception_id for item in allowances}) != len(allowances):
            raise ValueError("exception_allowances contains duplicate exception IDs")
        if not isinstance(self.autonomy, EconomicAutonomy):
            raise TypeError("autonomy must be EconomicAutonomy")
        if self.secondary_shortage_kind is not None and not isinstance(
            self.secondary_shortage_kind, SecondaryShortageKind
        ):
            raise TypeError("secondary_shortage_kind must be SecondaryShortageKind")
        object.__setattr__(
            self,
            "routes",
            tuple(
                sorted(
                    routes,
                    key=lambda item: (
                        item.supplier_fingerprint,
                        item.route_fingerprint,
                        item.route_id,
                    ),
                )
            ),
        )
        object.__setattr__(self, "demand_buckets", buckets)
        object.__setattr__(self, "suppliers", tuple(sorted(suppliers, key=lambda item: item.supplier_id)))
        object.__setattr__(
            self,
            "concentration_constraints",
            tuple(sorted(concentrations, key=lambda item: item.rule_id)),
        )
        object.__setattr__(
            self,
            "order_approval_constraints",
            tuple(sorted(order_approvals, key=lambda item: item.maximum_without_approval)),
        )
        object.__setattr__(
            self,
            "approved_order_rule_ids",
            tuple(sorted(set(self.approved_order_rule_ids))),
        )
        object.__setattr__(self, "exception_allowances", allowances)
        object.__setattr__(self, "relaxation_rule_ids", tuple(sorted(set(self.relaxation_rule_ids))))


@dataclass(frozen=True, slots=True)
class OptimizerAlert:
    category: AlertCategory
    code: str
    message: str
    component_id: str
    candidate_plan: CandidatePlan | None = None
    rule_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OptimizationOutcome:
    component_id: str
    calibration: SolverResult | None
    baseline: SolverResult | None
    executable: SolverResult | None
    counterfactuals: tuple[SolverResult, ...]
    selected_plan: CandidatePlan | None
    alternatives: tuple[CandidatePlan, ...]
    requirement_state: RequirementState
    residual_gap: Decimal
    alerts: tuple[OptimizerAlert, ...]
    derived_upper_bounds: tuple[tuple[str, Decimal], ...]
    emitted_constraint_rule_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SolverLimits:
    time_limit_seconds: float | None = None
    node_limit: int = 250_000
    force_status: SolverStatus | None = None
    force_mip_gap: Decimal | None = None

    def __post_init__(self) -> None:
        if self.time_limit_seconds is not None and self.time_limit_seconds <= 0:
            raise ValueError("time_limit_seconds must be positive")
        if not isinstance(self.node_limit, int) or isinstance(self.node_limit, bool) or self.node_limit <= 0:
            raise ValueError("node_limit must be a positive int")
        if self.force_status is not None and not isinstance(self.force_status, SolverStatus):
            raise TypeError("force_status must be SolverStatus")
        if self.force_mip_gap is not None:
            _require_decimal(self.force_mip_gap, "force_mip_gap")


@dataclass(slots=True)
class _Row:
    coefficients: dict[int, Fraction]
    lower: Fraction | None
    upper: Fraction | None
    name: str


@dataclass(slots=True)
class _IntegerModel:
    names: list[str] = field(default_factory=list)
    lower: list[int] = field(default_factory=list)
    upper: list[int] = field(default_factory=list)
    rows: list[_Row] = field(default_factory=list)

    def add_var(self, name: str, lower: int, upper: int) -> int:
        if lower > upper:
            raise ValueError(f"invalid bounds for {name}")
        index = len(self.names)
        self.names.append(name)
        self.lower.append(lower)
        self.upper.append(upper)
        return index

    def add_row(
        self,
        coefficients: Mapping[int, Decimal | int | Fraction],
        *,
        lower: Decimal | int | Fraction | None = None,
        upper: Decimal | int | Fraction | None = None,
        name: str,
    ) -> None:
        row = {
            index: _fraction(value)
            for index, value in coefficients.items()
            if _fraction(value) != 0
        }
        self.rows.append(
            _Row(
                row,
                None if lower is None else _fraction(lower),
                None if upper is None else _fraction(upper),
                name,
            )
        )


@dataclass(frozen=True, slots=True)
class _Objective:
    name: str
    coefficients: tuple[Fraction, ...]
    divisor: Fraction = Fraction(1)
    emitted: bool = True
    semantic_tie_break: bool = False

    def integer_coefficients(self) -> tuple[tuple[int, ...], int]:
        denominator = 1
        for value in self.coefficients:
            denominator = _lcm(denominator, value.denominator)
        values = tuple(int(item * denominator) for item in self.coefficients)
        return values, denominator


@dataclass(frozen=True, slots=True)
class _ModelContext:
    model: _IntegerModel
    problem: OptimizerProblem
    routes: tuple[CandidateRoute, ...]
    buckets: tuple[DemandBucket, ...]
    quantity_atom: Decimal
    x: tuple[int, ...]
    z: tuple[tuple[int, ...], ...]
    y: tuple[int, ...]
    unresolved: tuple[int, ...]
    eventual_gap: int
    discretionary: int
    review_exposure: tuple[int, ...]
    named_deviation: int
    moq_excess: int
    upper_atoms: tuple[int, ...]
    exception_caps: tuple[tuple[str, int], ...]
    emitted_rule_ids: tuple[str, ...]

    def quantity(self, atoms: int) -> Decimal:
        return Decimal(atoms) * self.quantity_atom


@dataclass(frozen=True, slots=True)
class _BackendResult:
    status: SolverStatus
    values: tuple[int, ...] | None
    objective_integer: int | None
    mip_gap: Decimal | None
    certificate_complete: bool
    hit_resource_limit: bool
    message: str | None = None


class _IntegerBackend(Protocol):
    def optimize(
        self,
        model: _IntegerModel,
        objective: Sequence[int],
        limits: SolverLimits,
    ) -> _BackendResult: ...


def _quantity_atom(problem: OptimizerProblem) -> Decimal:
    increment, _, _ = DEFAULT_UNIT_OF_MEASURE_CONTRACT.rule_for(
        problem.unit_of_measure
    )
    values: list[Decimal] = [
        increment,
        problem.net_requirement,
        problem.supply_ledger.total_demand,
        problem.supply_ledger.on_hand,
        problem.authorized_recovery_surplus,
    ]
    values.extend(item.minimum_order_quantity for item in problem.routes)
    values.extend(item.bucket_quantity for item in problem.demand_buckets)
    values.extend(item.quantity for item in problem.supply_ledger.committed_inbound)
    values.extend(
        item
        for item in (
            problem.max_allocation_driven_surplus,
            problem.minimum_compliant_total,
            problem.coverage_target,
            problem.autonomy.max_surplus_units,
        )
        if item is not None
    )
    places = max(0, max((-item.as_tuple().exponent for item in values), default=0))
    return Decimal(1).scaleb(-places)


def _atoms(value: Decimal, atom: Decimal) -> int:
    scaled = value / atom
    integral = scaled.to_integral_value()
    if scaled != integral:
        raise ValueError(f"quantity {value} is not exactly representable at scale {atom}")
    return int(integral)


def _ceil_atoms(value: Decimal, atom: Decimal, multiple: int) -> int:
    raw = int((value / atom).to_integral_value(rounding=ROUND_CEILING))
    return ((raw + multiple - 1) // multiple) * multiple


def _candidate_routes(problem: OptimizerProblem) -> tuple[CandidateRoute, ...]:
    relaxed = problem.relaxed_rule_id
    result: list[CandidateRoute] = []
    for route in problem.routes:
        if route.eligibility is EvidenceStatus.FAIL:
            continue
        if relaxed is None:
            if route.eligibility is EvidenceStatus.PASS and not route.approval_requirements:
                result.append(route)
            continue
        remaining_approvals = set(route.approval_requirements) - {relaxed}
        blocking_unknowns = {
            item.rule_id
            for item in route.evidence
            if item.severity is RuleSeverity.HARD
            and item.scope is EvidenceScope.CANDIDATE
            and item.status is EvidenceStatus.UNKNOWN
        } - {relaxed}
        if not remaining_approvals and not blocking_unknowns:
            result.append(route)
    return tuple(result)


def _supplier_moq_surplus(problem: OptimizerProblem, routes: Sequence[CandidateRoute]) -> Decimal:
    by_supplier: dict[str, Decimal] = {}
    for route in routes:
        by_supplier[route.supplier_id] = max(
            by_supplier.get(route.supplier_id, ZERO),
            route.minimum_order_quantity,
        )
    allocation_floor = sum(by_supplier.values(), ZERO)
    if problem.minimum_secondary_fraction is not None and by_supplier:
        largest_moq = max(by_supplier.values())
        share = Decimal("1") - problem.minimum_secondary_fraction
        allocation_floor = max(
            allocation_floor,
            (largest_moq / share).to_integral_value(rounding=ROUND_CEILING),
        )
    history_dilution = ZERO
    for constraint in problem.concentration_constraints:
        history = {
            item.supplier_id: item.quantity
            for item in constraint.existing_supplier_volumes
        }
        for supplier_id in by_supplier:
            excess = max(
                ZERO,
                history.get(supplier_id, ZERO)
                - constraint.maximum_fraction * constraint.existing_total_volume,
            )
            if excess:
                history_dilution = max(
                    history_dilution,
                    excess / constraint.maximum_fraction,
                )
    allocation_floor += history_dilution
    return max(ZERO, allocation_floor - problem.supply_ledger.total_demand)


def derive_upper_bounds(problem: OptimizerProblem) -> tuple[tuple[str, Decimal], ...]:
    """Derive the §8 valid Big-M bound for every route.

    The allocation-driven term defaults to the surplus needed to activate the
    largest-MOQ route for every eligible supplier.  It is conservative but
    source-derived, and is therefore safe for the per-component optimum.
    """

    routes = _candidate_routes(problem)
    allocation_surplus = (
        problem.max_allocation_driven_surplus
        if problem.max_allocation_driven_surplus is not None
        else _supplier_moq_surplus(problem, routes)
    )
    base = (
        problem.supply_ledger.total_demand
        + problem.authorized_recovery_surplus
        + allocation_surplus
    )
    return tuple(
        (route.route_id, max(base, route.minimum_order_quantity))
        for route in routes
    )


def _existing_exception_shortage(
    problem: OptimizerProblem,
    qualifying_deadlines: frozenset[date],
) -> Decimal:
    previous_gap = ZERO
    result = ZERO
    positions = {item.due_date: item for item in problem.supply_ledger.deadline_positions}
    for bucket in problem.demand_buckets:
        position = positions[bucket.due_date]
        current_gap = position.on_time_gap
        incremental_gap = max(ZERO, current_gap - previous_gap)
        if bucket.due_date in qualifying_deadlines:
            result += incremental_gap
        previous_gap = current_gap
    return result


def _rating(value: str | None) -> Fraction | None:
    if value is None:
        return None
    text = value.strip().upper()
    if not text or text[0] not in "ABCDEF":
        return None
    result = Fraction(6 - (ord(text[0]) - ord("A")))
    if text[1:] == "+":
        return result + Fraction(1, 4)
    if text[1:] == "-":
        return result - Fraction(1, 4)
    return result if len(text) == 1 else None


def _business_days(left: date, right: date) -> int:
    start, end = sorted((left, right))
    return sum(1 for ordinal in range(start.toordinal() + 1, end.toordinal() + 1) if date.fromordinal(ordinal).weekday() < 5)


def _review_keys(route: CandidateRoute) -> frozenset[str]:
    """Return normalized review exposures carried by a selected route.

    Assumption codes are the public, de-duplicated representation of benchmark
    evidence and approval exposure.  Counting evidence or approval-rule rows
    instead double-charges one normalized assumption when several policy
    branches depend on the same unknown fact.
    """

    return frozenset(
        tuple(
            code
            for item in route.evidence
            for code in item.assumption_codes
        )
    )


def _build_model(
    problem: OptimizerProblem,
    *,
    upper_bound_multiplier: int = 1,
) -> _ModelContext:
    model = _IntegerModel()
    routes = _candidate_routes(problem)
    buckets = problem.demand_buckets
    atom = _quantity_atom(problem)
    increment, _, _ = DEFAULT_UNIT_OF_MEASURE_CONTRACT.rule_for(problem.unit_of_measure)
    increment_atoms = _atoms(increment, atom)
    upper_decimals = dict(derive_upper_bounds(problem))
    upper_atoms = tuple(
        _atoms(upper_decimals[item.route_id], atom) * upper_bound_multiplier
        for item in routes
    )
    total_upper = sum(upper_atoms)
    demand_atoms = _atoms(problem.supply_ledger.total_demand, atom)

    x: list[int] = []
    step: list[int] = []
    y: list[int] = []
    z: list[tuple[int, ...]] = []
    for route_index, (route, route_upper) in enumerate(zip(routes, upper_atoms, strict=True)):
        x_var = model.add_var(f"x[{route_index}]", 0, route_upper)
        step_var = model.add_var(f"step[{route_index}]", 0, route_upper // increment_atoms)
        y_var = model.add_var(f"y[{route_index}]", 0, 1)
        z_vars = tuple(
            model.add_var(f"z[{route_index},{bucket_index}]", 0, route_upper)
            for bucket_index in range(len(buckets))
        )
        x.append(x_var)
        step.append(step_var)
        y.append(y_var)
        z.append(z_vars)
        model.add_row(
            {x_var: 1, step_var: -increment_atoms},
            lower=0,
            upper=0,
            name=f"quantity_increment[{route_index}]",
        )
        model.add_row(
            {x_var: 1, **{item: -1 for item in z_vars}},
            lower=0,
            upper=0,
            name=f"route_bucket_link[{route_index}]",
        )
        moq_relaxed = problem.solve_kind is SolveKind.COUNTERFACTUAL and problem.relaxed_rule_id in {
            problem.moq_rule_id,
            problem.sub_moq_approval_rule_id,
        }
        minimum = increment_atoms if moq_relaxed else _ceil_atoms(
            route.minimum_order_quantity,
            atom,
            increment_atoms,
        )
        model.add_row(
            {x_var: 1, y_var: -minimum},
            lower=0,
            name=f"moq_lower[{route_index}]",
        )
        model.add_row(
            {x_var: 1, y_var: -route_upper},
            upper=0,
            name=f"moq_upper[{route_index}]",
        )
        for bucket_index, bucket in enumerate(buckets):
            # Ordinary routes may be late and are priced by unit-late-days.
            # Exception routes may allocate only to the exact buckets whose
            # predicate opened the exception; CandidateBuilder stores those
            # dates separately from physical on-time feasibility.
            if (
                route.exception_codes
                and bucket.due_date not in route.exception_scope_deadlines
            ):
                model.add_row(
                    {z_vars[bucket_index]: 1},
                    lower=0,
                    upper=0,
                    name=f"exception_scope[{route_index},{bucket_index}]",
                )

    review_routes: dict[str, list[int]] = defaultdict(list)
    for route_index, route in enumerate(routes):
        for key in _review_keys(route):
            review_routes[key].append(route_index)
    review_exposure: list[int] = []
    for review_index, (key, route_indexes) in enumerate(sorted(review_routes.items())):
        review_var = model.add_var(f"review_exposure[{review_index}]", 0, 1)
        review_exposure.append(review_var)
        for route_index in route_indexes:
            model.add_row(
                {review_var: 1, y[route_index]: -1},
                lower=0,
                name=f"review_exposure_lower[{review_index},{route_index}]",
            )
        model.add_row(
            {review_var: 1, **{y[index]: -1 for index in route_indexes}},
            upper=0,
            name=f"review_exposure_upper[{review_index}]",
        )

    unresolved = tuple(
        model.add_var(f"unresolved[{index}]", 0, demand_atoms)
        for index in range(len(buckets))
    )
    eventual_gap = model.add_var(
        "eventual_gap",
        0,
        _atoms(problem.net_requirement, atom),
    )
    discretionary = model.add_var("discretionary_surplus", 0, total_upper)
    named_deviation = model.add_var("named_primary_deviation", 0, total_upper)
    moq_excess = model.add_var("moq_excess", 0, total_upper)

    positions = {item.due_date: item for item in problem.supply_ledger.deadline_positions}
    for bucket_index, bucket in enumerate(buckets):
        coefficients: dict[int, int] = {unresolved[bucket_index]: 1}
        for route_index, route in enumerate(routes):
            if route.material_available_date > bucket.due_date:
                continue
            for allocated_index in range(bucket_index + 1):
                coefficients[z[route_index][allocated_index]] = 1
        rhs = _atoms(
            max(
                ZERO,
                bucket.cumulative_quantity - positions[bucket.due_date].on_time_supply,
            ),
            atom,
        )
        model.add_row(
            coefficients,
            lower=rhs,
            name=f"cumulative_coverage[{bucket_index}]",
        )

    net_atoms = _atoms(problem.net_requirement, atom)
    model.add_row(
        {eventual_gap: 1, **{item: 1 for item in x}},
        lower=net_atoms,
        name="eventual_coverage",
    )
    if problem.coverage_target is not None and problem.solve_kind in {
        SolveKind.EXECUTABLE,
        SolveKind.COUNTERFACTUAL,
    }:
        target_atoms = _atoms(problem.coverage_target, atom)
        model.add_row(
            {item: 1 for item in x},
            lower=target_atoms,
            name="solve_q_coverage_target",
        )

    emitted: set[str] = set()
    supplier_route_indexes: dict[str, list[int]] = defaultdict(list)
    for index, route in enumerate(routes):
        supplier_route_indexes[route.supplier_id].append(index)

    secondary_active = (
        problem.minimum_secondary_fraction is not None
        and problem.relaxed_rule_id != problem.minimum_secondary_rule_id
    )
    if secondary_active:
        if len(supplier_route_indexes) >= 2:
            fraction = _fraction(problem.minimum_secondary_fraction)
            for supplier_id, indexes in supplier_route_indexes.items():
                coefficients = {item: -(1 - fraction) for item in x}
                for index in indexes:
                    coefficients[x[index]] = coefficients.get(x[index], Fraction()) + 1
                model.add_row(
                    coefficients,
                    upper=0,
                    name=f"secondary_allocation[{supplier_id}]",
                )
        else:
            # A positive one-supplier allocation violates the per-order rule
            # on its face.  Keep the zero-order point feasible so solve Q can
            # certify the exact uncovered quantity instead of manufacturing
            # infeasibility or silently dropping the rule.
            for supplier_id, indexes in supplier_route_indexes.items():
                model.add_row(
                    {x[index]: 1 for index in indexes},
                    lower=0,
                    upper=0,
                    name=f"secondary_allocation_unavailable[{supplier_id}]",
                )
        emitted.add(problem.minimum_secondary_rule_id or "minimum_secondary_fraction")

    for constraint in problem.concentration_constraints:
        if problem.relaxed_rule_id == constraint.rule_id:
            continue
        cap = _fraction(constraint.maximum_fraction)
        history = {item.supplier_id: item.quantity for item in constraint.existing_supplier_volumes}
        for supplier_id, indexes in supplier_route_indexes.items():
            coefficients = {item: -cap for item in x}
            for index in indexes:
                coefficients[x[index]] = coefficients.get(x[index], Fraction()) + 1
            rhs_quantity = (
                constraint.maximum_fraction * constraint.existing_total_volume
                - history.get(supplier_id, ZERO)
            )
            model.add_row(
                coefficients,
                upper=_fraction(rhs_quantity / atom),
                name=f"concentration[{constraint.rule_id},{supplier_id}]",
            )
        emitted.add(constraint.rule_id)

    route_cost_coefficients = {
        x[index]: _fraction(route.unit_price * atom)
        for index, route in enumerate(routes)
    }
    approved_order_rules = set(problem.approved_order_rule_ids)
    for approval in problem.order_approval_constraints:
        if (
            approval.rule_id in approved_order_rules
            or problem.relaxed_rule_id == approval.rule_id
        ):
            continue
        model.add_row(
            route_cost_coefficients,
            upper=_fraction(approval.maximum_without_approval),
            name=f"order_value_approval[{approval.rule_id}]",
        )
        emitted.add(approval.rule_id)

    allowance_overrides = {item.exception_id: item for item in problem.exception_allowances}
    exception_routes: dict[str, list[int]] = defaultdict(list)
    for route_index, route in enumerate(routes):
        for exception in route.exception_codes:
            exception_routes[exception].append(route_index)
    exception_caps: list[tuple[str, int]] = []
    for exception, indexes in sorted(exception_routes.items()):
        override = allowance_overrides.get(exception)
        route_deadlines = frozenset(
            due
            for index in indexes
            for due in routes[index].exception_scope_deadlines
        )
        qualifying = frozenset(
            override.qualifying_deadlines
            if override is not None
            else route_deadlines
        )
        if not qualifying <= route_deadlines:
            raise ValueError(
                f"exception allowance {exception!r} contains a deadline not opened by a route predicate"
            )
        derived_maximum = _existing_exception_shortage(problem, qualifying)
        if (
            override is not None
            and override.maximum_quantity is not None
            and override.maximum_quantity > derived_maximum
        ):
            raise ValueError(
                f"exception allowance {exception!r} exceeds the net unresolved shortage"
            )
        maximum = (
            override.maximum_quantity
            if override is not None and override.maximum_quantity is not None
            else derived_maximum
        )
        maximum_atoms = _atoms(maximum, atom)
        coefficients = {
            z[index][bucket_index]: 1
            for index in indexes
            for bucket_index, bucket in enumerate(buckets)
            if bucket.due_date in qualifying
        }
        model.add_row(
            coefficients,
            upper=maximum_atoms,
            name=f"exception_aggregate[{exception}]",
        )
        exception_caps.append((exception, maximum_atoms))

    q_min_atoms = _atoms(problem.minimum_compliant_total or ZERO, atom)
    model.add_row(
        {discretionary: 1, **{item: -1 for item in x}},
        lower=-q_min_atoms,
        name="discretionary_surplus_definition",
    )
    model.add_row(
        {moq_excess: 1, **{item: -1 for item in x}},
        lower=-net_atoms,
        name="moq_excess_definition",
    )

    named_active = (
        problem.named_primary_supplier_id is not None
        and problem.relaxed_rule_id != problem.named_primary_rule_id
        and problem.solve_kind is not SolveKind.BASELINE
    )
    if problem.named_primary_supplier_id is not None:
        named_indexes = supplier_route_indexes.get(problem.named_primary_supplier_id, [])
        for supplier_id, indexes in supplier_route_indexes.items():
            if supplier_id == problem.named_primary_supplier_id:
                continue
            coefficients: dict[int, int] = {named_deviation: 1}
            for index in indexes:
                coefficients[x[index]] = coefficients.get(x[index], 0) - 1
            for index in named_indexes:
                coefficients[x[index]] = coefficients.get(x[index], 0) + 1
            model.add_row(
                coefficients,
                lower=0,
                name=f"named_primary_deviation[{supplier_id}]",
            )
        if named_active:
            model.add_row(
                {named_deviation: 1},
                lower=0,
                upper=0,
                name="named_primary_membership",
            )
            emitted.add(problem.named_primary_rule_id or "named_primary_supplier")

    if problem.solve_kind in {SolveKind.EXECUTABLE, SolveKind.COUNTERFACTUAL}:
        surplus_cap = problem.autonomy.max_surplus_fraction * problem.net_requirement
        if problem.autonomy.max_surplus_units is not None:
            surplus_cap = min(surplus_cap, problem.autonomy.max_surplus_units)
        model.add_row(
            {discretionary: 1},
            upper=_fraction(surplus_cap / atom),
            name="autonomy_discretionary_surplus",
        )
        assert problem.cheapest_covering_cost is not None
        model.add_row(
            route_cost_coefficients,
            upper=_fraction(
                problem.cheapest_covering_cost
                + problem.autonomy.max_excess_cost_usd
            ),
            name="autonomy_excess_cost",
        )

    return _ModelContext(
        model=model,
        problem=problem,
        routes=routes,
        buckets=buckets,
        quantity_atom=atom,
        x=tuple(x),
        z=tuple(z),
        y=tuple(y),
        unresolved=unresolved,
        eventual_gap=eventual_gap,
        discretionary=discretionary,
        review_exposure=tuple(review_exposure),
        named_deviation=named_deviation,
        moq_excess=moq_excess,
        upper_atoms=upper_atoms,
        exception_caps=tuple(exception_caps),
        emitted_rule_ids=tuple(sorted(emitted)),
    )


def _coefficients(context: _ModelContext, values: Mapping[int, Fraction | int]) -> tuple[Fraction, ...]:
    result = [Fraction() for _ in context.model.names]
    for index, value in values.items():
        result[index] = _fraction(value)
    return tuple(result)


def _sustainability_coefficients(context: _ModelContext) -> dict[int, Fraction]:
    suppliers = {item.supplier_id: item for item in context.problem.suppliers}
    result: dict[int, Fraction] = {}
    for route_index, route in enumerate(context.routes):
        rating = _rating(suppliers[route.supplier_id].sustainability_rating)
        if rating is None:
            continue
        for bucket_index, bucket in enumerate(context.buckets):
            best = rating
            for alternative in context.routes:
                if alternative.route_id == route.route_id:
                    continue
                other = _rating(suppliers[alternative.supplier_id].sustainability_rating)
                if other is None or other <= best:
                    continue
                low = min(route.unit_price, alternative.unit_price)
                comparable_price = (
                    route.unit_price == alternative.unit_price
                    if low == ZERO
                    else abs(route.unit_price - alternative.unit_price) / low <= Decimal("0.10")
                )
                comparable_date = _business_days(
                    route.material_available_date,
                    alternative.material_available_date,
                ) <= 5
                alternative_can_serve = (
                    not alternative.exception_codes
                    or bucket.due_date in alternative.exception_scope_deadlines
                )
                if comparable_price and comparable_date and alternative_can_serve:
                    best = other
            if best > rating:
                result[context.z[route_index][bucket_index]] = best - rating
    return result


def _international_coefficients(context: _ModelContext) -> dict[int, Fraction]:
    """Return the §3(a)/(c) route/bucket volume coefficients.

    Candidate construction emits a separate exception-scoped route for each
    domestic-gate condition.  Using ``z`` still matters: it keeps the policy
    coefficient at the documented route/bucket level and cannot charge volume
    assigned to a bucket outside that exception scope.
    """

    return {
        context.z[route_index][bucket_index]: Fraction(1)
        for route_index, route in enumerate(context.routes)
        if any(code.endswith(("condition_a", "condition_c")) for code in route.exception_codes)
        for bucket_index, bucket in enumerate(context.buckets)
        if bucket.due_date in route.exception_scope_deadlines
    }


def _strategic_coefficients(context: _ModelContext) -> dict[int, Fraction]:
    """Recompute the inclusive 15% Strategic-retention window exactly."""

    suppliers = {item.supplier_id: item for item in context.problem.suppliers}

    def strategic(route: CandidateRoute) -> bool:
        tier = suppliers[route.supplier_id].relationship_tier
        return isinstance(tier, str) and " ".join(tier.split()).casefold() == "strategic"

    def can_serve(route: CandidateRoute, bucket: DemandBucket) -> bool:
        return (
            not route.exception_codes
            or bucket.due_date in route.exception_scope_deadlines
        )

    result: dict[int, Fraction] = {}
    for route_index, route in enumerate(context.routes):
        if strategic(route):
            continue
        for bucket_index, bucket in enumerate(context.buckets):
            if not can_serve(route, bucket):
                continue
            alternatives = tuple(
                item
                for item in context.routes
                if strategic(item) and can_serve(item, bucket)
            )
            if not alternatives:
                continue
            best = min(
                alternatives,
                key=lambda item: (
                    item.unit_price,
                    item.supplier_fingerprint,
                    item.route_fingerprint,
                ),
            )
            savings = (
                ZERO
                if best.unit_price == ZERO
                else (best.unit_price - route.unit_price) / best.unit_price
            )
            if savings <= Decimal("0.15"):
                result[context.z[route_index][bucket_index]] = Fraction(1)
    return result


def _objectives(
    context: _ModelContext,
    *,
    staged: bool = False,
) -> tuple[tuple[_Objective, ...], ...]:
    """Return stages; a stage may contain ordered sub-objectives."""

    size = len(context.model.names)
    zero = tuple(Fraction() for _ in range(size))
    if not staged and context.problem.solve_kind is SolveKind.QUANTITY_CALIBRATION:
        return (
            (_Objective("quantity_calibration_coverage", _coefficients(context, {context.eventual_gap: 1}), _fraction(context.quantity_atom)),),
            (_Objective("quantity_calibration_total", _coefficients(context, {item: 1 for item in context.x}), _fraction(context.quantity_atom)),),
        )
    if not staged and context.problem.solve_kind is SolveKind.BASELINE:
        cost = {
            context.x[index]: _fraction(route.unit_price * context.quantity_atom)
            for index, route in enumerate(context.routes)
        }
        return (
            (_Objective("baseline_eventual_gap", _coefficients(context, {context.eventual_gap: 1}), _fraction(context.quantity_atom)),),
            (_Objective("baseline_known_cost", _coefficients(context, cost)),),
        )

    stage1 = tuple(
        _Objective(
            f"stage_01_unresolved_{bucket.due_date.isoformat()}",
            _coefficients(context, {context.unresolved[index]: 1}),
            _fraction(context.quantity_atom),
        )
        for index, bucket in enumerate(context.buckets)
    )
    late = {
        context.z[route_index][bucket_index]: max(
            0,
            (route.material_available_date - bucket.due_date).days,
        )
        for route_index, route in enumerate(context.routes)
        for bucket_index, bucket in enumerate(context.buckets)
    }
    cost = {
        context.x[index]: _fraction(route.unit_price * context.quantity_atom)
        for index, route in enumerate(context.routes)
    }
    total_lead = {
        context.x[index]: route.lead_time_days
        for index, route in enumerate(context.routes)
    }
    return (
        stage1 or (_Objective("stage_01_unresolved", zero),),
        (_Objective("stage_02_unit_late_days", _coefficients(context, late), _fraction(context.quantity_atom)),),
        (_Objective("stage_03_discretionary_surplus", _coefficients(context, {context.discretionary: 1}), _fraction(context.quantity_atom)),),
        (_Objective("stage_04_policy_review_exposure", _coefficients(context, {item: 1 for item in context.review_exposure})),),
        (_Objective("stage_05_named_primary_deviation", _coefficients(context, {context.named_deviation: 1}), _fraction(context.quantity_atom)),),
        (_Objective("stage_06_international_volume", _coefficients(context, _international_coefficients(context)), _fraction(context.quantity_atom)),),
        (_Objective("stage_07_strategic_shift", _coefficients(context, _strategic_coefficients(context)), _fraction(context.quantity_atom)),),
        (_Objective("stage_08_sustainability_band", _coefficients(context, _sustainability_coefficients(context)), _fraction(context.quantity_atom)),),
        (
            _Objective("stage_09_known_landed_cost", _coefficients(context, cost)),
            _Objective("stage_09_moq_excess", _coefficients(context, {context.moq_excess: 1}), _fraction(context.quantity_atom)),
        ),
        (
            _Objective("stage_10_total_lead_time", _coefficients(context, total_lead), _fraction(context.quantity_atom)),
            _Objective("stage_10_line_count", _coefficients(context, {item: 1 for item in context.y})),
            # The final key is a sorted tuple, not a rank-times-quantity
            # surrogate.  It is solved specially by IntegerScaledSolver and
            # intentionally is not flattened into CandidatePlan's Decimal
            # metric vector.
            _Objective(
                "stage_10_id_free_tie",
                zero,
                emitted=False,
                semantic_tie_break=True,
            ),
        ),
    )


def _fixed_staged_objective_vector(
    context: _ModelContext,
    values: Sequence[int],
) -> tuple[Decimal, ...]:
    """Evaluate the literal staged objectives for a solve-0 fixed plan."""

    exact = list(values)
    total = sum(values[index] for index in context.x)
    net = _atoms(context.problem.net_requirement, context.quantity_atom)
    exact[context.discretionary] = 0
    exact[context.moq_excess] = max(0, total - net)

    positions = {
        item.due_date: item for item in context.problem.supply_ledger.deadline_positions
    }
    for bucket_index, bucket in enumerate(context.buckets):
        required = _atoms(
            max(
                ZERO,
                bucket.cumulative_quantity
                - positions[bucket.due_date].on_time_supply,
            ),
            context.quantity_atom,
        )
        available = sum(
            values[context.z[route_index][allocated_index]]
            for route_index, route in enumerate(context.routes)
            if route.material_available_date <= bucket.due_date
            for allocated_index in range(bucket_index + 1)
        )
        exact[context.unresolved[bucket_index]] = max(0, required - available)

    review_routes: dict[str, list[int]] = defaultdict(list)
    for route_index, route in enumerate(context.routes):
        for key in _review_keys(route):
            review_routes[key].append(route_index)
    for variable, (_key, route_indexes) in zip(
        context.review_exposure,
        sorted(review_routes.items()),
        strict=True,
    ):
        exact[variable] = int(any(values[context.y[index]] for index in route_indexes))

    named_indexes = tuple(
        index
        for index, route in enumerate(context.routes)
        if route.supplier_id == context.problem.named_primary_supplier_id
    )
    named_quantity = sum(values[context.x[index]] for index in named_indexes)
    exact[context.named_deviation] = max(
        (
            max(
                0,
                sum(
                    values[context.x[index]]
                    for index, route in enumerate(context.routes)
                    if route.supplier_id == supplier_id
                )
                - named_quantity,
            )
            for supplier_id in {
                route.supplier_id
                for route in context.routes
                if route.supplier_id != context.problem.named_primary_supplier_id
            }
        ),
        default=0,
    ) if context.problem.named_primary_supplier_id is not None else 0

    result: list[Decimal] = []
    for stage in _objectives(context, staged=True):
        for objective in stage:
            if not objective.emitted:
                continue
            coefficients, scale = objective.integer_coefficients()
            result.append(
                _decimal(
                    Fraction(_objective_value(coefficients, exact), scale)
                    * objective.divisor
                )
            )
    return tuple(result)


def _objective_value(coefficients: Sequence[int], values: Sequence[int]) -> int:
    return sum(coefficient * value for coefficient, value in zip(coefficients, values, strict=True))


def _pin_objective(model: _IntegerModel, coefficients: Sequence[int], value: int, name: str) -> None:
    model.add_row(
        {index: coefficient for index, coefficient in enumerate(coefficients) if coefficient},
        lower=value,
        upper=value,
        name=f"pin[{name}]",
    )


def _semantic_tie_value(context: _ModelContext, values: Sequence[int]) -> int:
    """Encode the selected ID-free tuple into an order-preserving integer.

    The encoding is only a compact certificate value.  Selection itself is
    performed tuple element by tuple element, so it does not rely on hashes or
    database identifiers.  Line count is pinned before this key is reached;
    consequently all compared keys have the same number of tuple elements.
    """

    pairs = tuple(
        sorted(
            {
                (route.supplier_fingerprint, route.route_fingerprint)
                for route in context.routes
            }
        )
    )
    ranks = {pair: rank for rank, pair in enumerate(pairs, start=1)}
    base = max((*context.upper_atoms, len(pairs)), default=0) + 1
    encoded = 0
    selected = sorted(
        (
            route.supplier_fingerprint,
            route.route_fingerprint,
            values[context.x[route_index]],
        )
        for route_index, route in enumerate(context.routes)
        if values[context.x[route_index]] > 0
    )
    for supplier_hash, route_hash, quantity in selected:
        encoded = encoded * base + ranks[
            (supplier_hash, route_hash)
        ]
        encoded = encoded * base + quantity
    return encoded


def _validate_integer_solution(model: _IntegerModel, values: Sequence[int]) -> tuple[bool, str | None]:
    if len(values) != len(model.names):
        return False, "solution length does not match model"
    for index, value in enumerate(values):
        if not isinstance(value, int) or isinstance(value, bool):
            return False, f"{model.names[index]} is not integral"
        if not model.lower[index] <= value <= model.upper[index]:
            return False, f"{model.names[index]} violates its bound"
    for row in model.rows:
        activity = sum(
            coefficient * values[index]
            for index, coefficient in row.coefficients.items()
        )
        if row.lower is not None and activity < row.lower:
            return False, f"constraint {row.name} is below its exact lower bound"
        if row.upper is not None and activity > row.upper:
            return False, f"constraint {row.name} is above its exact upper bound"
    return True, None


class ScipyMilpBackend:
    """Lazy SciPy/HiGHS integer-program backend."""

    def optimize(
        self,
        model: _IntegerModel,
        objective: Sequence[int],
        limits: SolverLimits,
    ) -> _BackendResult:
        if limits.force_status is not None:
            status = limits.force_status
            return _BackendResult(
                status=status,
                values=None,
                objective_integer=None,
                mip_gap=limits.force_mip_gap,
                certificate_complete=False,
                hit_resource_limit=status in {
                    SolverStatus.TIMEOUT,
                    SolverStatus.RESOURCE_LIMIT,
                    SolverStatus.FEASIBLE_INCUMBENT,
                },
                message="forced solver status",
            )
        try:
            import numpy as np
            from scipy.optimize import Bounds, LinearConstraint, milp
            from scipy.sparse import coo_array
        except ImportError as error:
            return _BackendResult(
                SolverStatus.ERROR,
                None,
                None,
                None,
                False,
                False,
                f"SciPy/HiGHS unavailable: {error}",
            )

        row_indexes: list[int] = []
        column_indexes: list[int] = []
        data: list[float] = []
        lower_rows: list[float] = []
        upper_rows: list[float] = []
        for row_index, row in enumerate(model.rows):
            denominator = 1
            for coefficient in row.coefficients.values():
                denominator = _lcm(denominator, coefficient.denominator)
            if row.lower is not None:
                denominator = _lcm(denominator, row.lower.denominator)
            if row.upper is not None:
                denominator = _lcm(denominator, row.upper.denominator)
            for column, coefficient in row.coefficients.items():
                row_indexes.append(row_index)
                column_indexes.append(column)
                data.append(float(coefficient * denominator))
            lower_rows.append(
                -np.inf if row.lower is None else float(row.lower * denominator)
            )
            upper_rows.append(
                np.inf if row.upper is None else float(row.upper * denominator)
            )
        matrix = coo_array(
            (data, (row_indexes, column_indexes)),
            shape=(len(model.rows), len(model.names)),
        ).tocsr()
        options: dict[str, float] = {"mip_rel_gap": 0.0}
        if limits.time_limit_seconds is not None:
            options["time_limit"] = limits.time_limit_seconds
        result = milp(
            c=np.asarray(tuple(objective), dtype=float),
            integrality=np.ones(len(model.names), dtype=int),
            bounds=Bounds(
                np.asarray(model.lower, dtype=float),
                np.asarray(model.upper, dtype=float),
            ),
            constraints=LinearConstraint(
                matrix,
                np.asarray(lower_rows, dtype=float),
                np.asarray(upper_rows, dtype=float),
            ),
            options=options,
        )
        values = None
        if result.x is not None:
            rounded = tuple(int(round(float(item))) for item in result.x)
            if all(abs(float(item) - rounded[index]) <= 1e-6 for index, item in enumerate(result.x)):
                values = rounded
        gap_float = getattr(result, "mip_gap", None)
        gap = None if gap_float is None else Decimal(str(gap_float))
        if limits.force_mip_gap is not None:
            gap = limits.force_mip_gap
        if result.status == 0 and gap == ZERO:
            status = SolverStatus.OPTIMAL
            complete = True
            limited = False
        elif result.status == 2:
            status = SolverStatus.INFEASIBLE
            complete = True
            limited = False
        elif result.status == 3:
            status = SolverStatus.UNBOUNDED
            complete = True
            limited = False
        elif result.status == 1:
            status = SolverStatus.FEASIBLE_INCUMBENT if values is not None else SolverStatus.TIMEOUT
            complete = False
            limited = True
        elif values is not None:
            status = SolverStatus.FEASIBLE_INCUMBENT
            complete = False
            limited = False
        else:
            status = SolverStatus.ERROR
            complete = False
            limited = False
        if gap is not None and gap != ZERO and status is SolverStatus.OPTIMAL:
            status = SolverStatus.FEASIBLE_INCUMBENT
            complete = False
        objective_integer = (
            _objective_value(objective, values) if values is not None else None
        )
        return _BackendResult(
            status,
            values,
            objective_integer,
            gap,
            complete,
            limited,
            str(result.message) if result.message else None,
        )


class StdlibBranchAndBoundBackend:
    """Exact bounded integer search for small models.

    Interval feasibility and objective bounds close branches exactly over
    :class:`fractions.Fraction`.  Reaching the node budget returns an
    unresolved incumbent, never an optimum certificate.
    """

    def optimize(
        self,
        model: _IntegerModel,
        objective: Sequence[int],
        limits: SolverLimits,
    ) -> _BackendResult:
        if limits.force_status is not None:
            return _BackendResult(
                limits.force_status,
                None,
                None,
                limits.force_mip_gap,
                False,
                True,
                "forced solver status",
            )
        order = tuple(
            sorted(
                range(len(model.names)),
                key=lambda index: (
                    model.upper[index] - model.lower[index],
                    0 if model.names[index].startswith("y[") else 1,
                    model.names[index],
                ),
            )
        )
        values: list[int | None] = [None] * len(model.names)
        best_values: tuple[int, ...] | None = None
        best_objective: int | None = None
        nodes = 0
        exhausted = False

        def row_possible(row: _Row) -> bool:
            minimum = Fraction()
            maximum = Fraction()
            for index, coefficient in row.coefficients.items():
                value = values[index]
                if value is not None:
                    minimum += coefficient * value
                    maximum += coefficient * value
                elif coefficient >= 0:
                    minimum += coefficient * model.lower[index]
                    maximum += coefficient * model.upper[index]
                else:
                    minimum += coefficient * model.upper[index]
                    maximum += coefficient * model.lower[index]
            return not (
                (row.lower is not None and maximum < row.lower)
                or (row.upper is not None and minimum > row.upper)
            )

        def objective_lower_bound() -> int:
            result = 0
            for index, coefficient in enumerate(objective):
                value = values[index]
                if value is not None:
                    result += coefficient * value
                elif coefficient >= 0:
                    result += coefficient * model.lower[index]
                else:
                    result += coefficient * model.upper[index]
            return result

        def visit(depth: int) -> None:
            nonlocal nodes, exhausted, best_values, best_objective
            if exhausted:
                return
            nodes += 1
            if nodes > limits.node_limit:
                exhausted = True
                return
            if any(not row_possible(row) for row in model.rows):
                return
            lower_bound = objective_lower_bound()
            if best_objective is not None and lower_bound >= best_objective:
                return
            if depth == len(order):
                candidate = tuple(int(item) for item in values if item is not None)
                valid, _ = _validate_integer_solution(model, candidate)
                if not valid:
                    return
                objective_value = _objective_value(objective, candidate)
                if best_objective is None or objective_value < best_objective:
                    best_objective = objective_value
                    best_values = candidate
                return
            index = order[depth]
            domain = range(model.lower[index], model.upper[index] + 1)
            if objective[index] < 0:
                domain = range(model.upper[index], model.lower[index] - 1, -1)
            for value in domain:
                values[index] = value
                visit(depth + 1)
                if exhausted:
                    break
            values[index] = None

        visit(0)
        if exhausted:
            return _BackendResult(
                SolverStatus.FEASIBLE_INCUMBENT if best_values is not None else SolverStatus.RESOURCE_LIMIT,
                best_values,
                best_objective,
                None,
                False,
                True,
                f"stdlib branch-and-bound exhausted its {limits.node_limit}-node budget",
            )
        if best_values is None:
            return _BackendResult(
                SolverStatus.INFEASIBLE,
                None,
                None,
                None,
                True,
                False,
                "stdlib branch-and-bound closed every branch as infeasible",
            )
        return _BackendResult(
            SolverStatus.OPTIMAL,
            best_values,
            best_objective,
            ZERO,
            True,
            False,
            f"stdlib branch-and-bound closed {nodes} nodes",
        )


class IntegerScaledSolver:
    """A :class:`~apex_procurement.protocols.Solver` implementation."""

    def __init__(
        self,
        backend: _IntegerBackend | None = None,
        *,
        fallback: _IntegerBackend | None = None,
        limits: SolverLimits | None = None,
        upper_bound_multiplier: int = 1,
    ) -> None:
        if (
            not isinstance(upper_bound_multiplier, int)
            or isinstance(upper_bound_multiplier, bool)
            or upper_bound_multiplier < 1
        ):
            raise ValueError("upper_bound_multiplier must be a positive int")
        self.backend = backend or ScipyMilpBackend()
        self.fallback = fallback or StdlibBranchAndBoundBackend()
        self.limits = limits or SolverLimits()
        self.upper_bound_multiplier = upper_bound_multiplier
        self.last_context: _ModelContext | None = None

    def _run_backend(
        self,
        model: _IntegerModel,
        objective: Sequence[int],
    ) -> _BackendResult:
        result = self.backend.optimize(model, objective, self.limits)
        if (
            result.status is SolverStatus.ERROR
            and result.message
            and result.message.startswith("SciPy/HiGHS unavailable")
        ):
            result = self.fallback.optimize(model, objective, self.limits)
        if result.values is not None:
            valid, validation_message = _validate_integer_solution(model, result.values)
            if not valid:
                result = replace(
                    result,
                    status=SolverStatus.ERROR,
                    values=None,
                    objective_integer=None,
                    certificate_complete=False,
                    message=f"exact incumbent validation failed: {validation_message}",
                )
        return result

    def _solve_semantic_tie(
        self,
        context: _ModelContext,
        values: tuple[int, ...],
    ) -> _BackendResult:
        """Certify the sorted semantic line tuple without an ID-based proxy."""

        model = context.model
        line_count = sum(values[index] for index in context.y)
        current = values
        first_unfixed = 0
        route_count = len(context.routes)
        for position in range(line_count):
            # With line count pinned, binary place values find the earliest
            # semantic route that can occupy this tuple position.  Only that
            # membership is pinned before quantity is minimized, preserving
            # the documented (fingerprint, fingerprint, quantity) ordering.
            membership = [0] * len(model.names)
            for route_index in range(first_unfixed, route_count):
                membership[context.y[route_index]] = -(1 << (route_count - route_index))
            result = self._run_backend(model, membership)
            if not (
                result.status is SolverStatus.OPTIMAL
                and result.certificate_complete
                and not result.hit_resource_limit
                and result.mip_gap == ZERO
                and result.values is not None
            ):
                return result
            chosen = next(
                (
                    route_index
                    for route_index in range(first_unfixed, route_count)
                    if result.values[context.y[route_index]] == 1
                ),
                None,
            )
            if chosen is None:
                return _BackendResult(
                    SolverStatus.ERROR,
                    None,
                    None,
                    None,
                    False,
                    False,
                    f"semantic tie position {position} had no selected route",
                )
            for route_index in range(first_unfixed, chosen):
                _pin_objective(
                    model,
                    tuple(
                        1 if index == context.y[route_index] else 0
                        for index in range(len(model.names))
                    ),
                    0,
                    f"stage_10_tie_absent_{position}_{route_index}",
                )
            _pin_objective(
                model,
                tuple(
                    1 if index == context.y[chosen] else 0
                    for index in range(len(model.names))
                ),
                1,
                f"stage_10_tie_route_{position}",
            )
            quantity_objective = [0] * len(model.names)
            quantity_objective[context.x[chosen]] = 1
            result = self._run_backend(model, quantity_objective)
            if not (
                result.status is SolverStatus.OPTIMAL
                and result.certificate_complete
                and not result.hit_resource_limit
                and result.mip_gap == ZERO
                and result.values is not None
            ):
                return result
            quantity = result.values[context.x[chosen]]
            _pin_objective(
                model,
                tuple(quantity_objective),
                quantity,
                f"stage_10_tie_quantity_{position}",
            )
            current = result.values
            first_unfixed = chosen + 1
        return _BackendResult(
            SolverStatus.OPTIMAL,
            current,
            _semantic_tie_value(context, current),
            ZERO,
            True,
            False,
            "ID-free semantic tuple tie-break certified",
        )

    def solve(self, problem: OptimizerProblem, /) -> SolverResult:
        if not isinstance(problem, OptimizerProblem):
            raise TypeError("problem must be OptimizerProblem")
        context = _build_model(
            problem,
            upper_bound_multiplier=self.upper_bound_multiplier,
        )
        self.last_context = context
        objectives = _objectives(context)
        model = context.model
        stage_results: list[SolverStageResult] = []
        objective_vector: list[Decimal] = []
        final_values: tuple[int, ...] | None = None

        for stage_index, subobjectives in enumerate(objectives, start=1):
            stage_values: list[Decimal] = []
            emitted_stage_values: list[Decimal] = []
            for objective in subobjectives:
                integer_coefficients, coefficient_scale = objective.integer_coefficients()
                if objective.semantic_tie_break:
                    assert final_values is not None
                    backend_result = self._solve_semantic_tie(context, final_values)
                else:
                    backend_result = self._run_backend(model, integer_coefficients)
                if (
                    backend_result.status is not SolverStatus.OPTIMAL
                    or not backend_result.certificate_complete
                    or backend_result.hit_resource_limit
                    or backend_result.mip_gap != ZERO
                ):
                    status = SolverStatus.INFEASIBLE if backend_result.status is SolverStatus.INFEASIBLE else SolverStatus.UNRESOLVED
                    stage_results.append(
                        SolverStageResult(
                            stage_name=objective.name,
                            status=backend_result.status,
                            objective_value=None,
                            mip_gap=backend_result.mip_gap,
                            certificate_complete=backend_result.certificate_complete,
                            hit_resource_limit=backend_result.hit_resource_limit,
                            message=backend_result.message,
                        )
                    )
                    diagnostic = None
                    if backend_result.values is not None:
                        diagnostic = _build_plan(
                            context,
                            backend_result.values,
                            (),
                            PlanDisposition.DECISION_REQUIRED,
                            exact=False,
                        )
                    return SolverResult(
                        component_id=problem.component_id,
                        solve_kind=problem.solve_kind,
                        status=status,
                        stage_results=tuple(stage_results),
                        candidate_plan=diagnostic,
                        objective_vector=tuple(objective_vector),
                        exact_post_validated=False,
                        message=backend_result.message,
                    )
                assert backend_result.values is not None
                final_values = backend_result.values
                exact_integer = (
                    backend_result.objective_integer
                    if objective.semantic_tie_break
                    else _objective_value(integer_coefficients, final_values)
                )
                assert exact_integer is not None
                value = (
                    Fraction(exact_integer, coefficient_scale)
                    * objective.divisor
                )
                stage_values.append(_decimal(value))
                if objective.emitted:
                    emitted_stage_values.append(_decimal(value))
                if not objective.semantic_tie_break:
                    _pin_objective(model, integer_coefficients, exact_integer, objective.name)
            stage_value = sum(stage_values, ZERO)
            stage_name = (
                f"stage_{stage_index:02d}"
                if problem.solve_kind in {SolveKind.EXECUTABLE, SolveKind.COUNTERFACTUAL}
                else subobjectives[-1].name
            )
            stage_results.append(
                SolverStageResult(
                    stage_name=stage_name,
                    status=SolverStatus.OPTIMAL,
                    objective_value=stage_value,
                    mip_gap=ZERO,
                    certificate_complete=True,
                    hit_resource_limit=False,
                )
            )
            objective_vector.extend(emitted_stage_values)

        assert final_values is not None
        valid, message = _validate_integer_solution(model, final_values)
        if not valid:
            return SolverResult(
                component_id=problem.component_id,
                solve_kind=problem.solve_kind,
                status=SolverStatus.UNRESOLVED,
                stage_results=tuple(stage_results),
                candidate_plan=None,
                objective_vector=tuple(objective_vector),
                exact_post_validated=False,
                message=f"exact final validation failed: {message}",
            )

        exact_vector: list[Decimal] = []
        for stage in objectives:
            for objective in stage:
                if not objective.emitted:
                    continue
                coefficients, scale = objective.integer_coefficients()
                exact_vector.append(
                    _decimal(
                        Fraction(_objective_value(coefficients, final_values), scale)
                        * objective.divisor
                    )
                )
        if tuple(exact_vector) != tuple(objective_vector):
            return SolverResult(
                component_id=problem.component_id,
                solve_kind=problem.solve_kind,
                status=SolverStatus.UNRESOLVED,
                stage_results=tuple(stage_results),
                candidate_plan=None,
                objective_vector=tuple(objective_vector),
                exact_post_validated=False,
                message="exact final objective-vector revalidation failed",
            )
        if problem.solve_kind in {SolveKind.EXECUTABLE, SolveKind.COUNTERFACTUAL}:
            total_atoms = sum(final_values[index] for index in context.x)
            q_min_atoms = _atoms(problem.minimum_compliant_total or ZERO, context.quantity_atom)
            net_atoms = _atoms(problem.net_requirement, context.quantity_atom)
            expected_discretionary = max(0, total_atoms - q_min_atoms)
            expected_moq_excess = max(0, total_atoms - net_atoms)
            selected_review_keys = {
                key
                for route_index, route in enumerate(context.routes)
                if final_values[context.y[route_index]] == 1
                for key in _review_keys(route)
            }
            if (
                final_values[context.discretionary] != expected_discretionary
                or final_values[context.moq_excess] != expected_moq_excess
                or sum(final_values[index] for index in context.review_exposure)
                != len(selected_review_keys)
            ):
                return SolverResult(
                    component_id=problem.component_id,
                    solve_kind=problem.solve_kind,
                    status=SolverStatus.UNRESOLVED,
                    stage_results=tuple(stage_results),
                    candidate_plan=None,
                    objective_vector=tuple(objective_vector),
                    exact_post_validated=False,
                    message="exact semantic objective revalidation failed",
                )

        disposition = _disposition(problem, context, final_values)
        plan_objective_vector = tuple(objective_vector)
        if problem.solve_kind is SolveKind.BASELINE:
            plan_objective_vector = _fixed_staged_objective_vector(
                context,
                final_values,
            )
        plan = _build_plan(
            context,
            final_values,
            plan_objective_vector,
            disposition,
            exact=True,
        )
        total = sum((line.quantity for line in plan.lines), ZERO) if plan else ZERO
        cost = plan.total_cost if plan else ZERO
        return SolverResult(
            component_id=problem.component_id,
            solve_kind=problem.solve_kind,
            status=SolverStatus.OPTIMAL,
            stage_results=tuple(stage_results),
            candidate_plan=plan,
            objective_vector=tuple(objective_vector),
            minimum_compliant_total=total if problem.solve_kind is SolveKind.QUANTITY_CALIBRATION else problem.minimum_compliant_total,
            cheapest_covering_cost=cost if problem.solve_kind is SolveKind.BASELINE else problem.cheapest_covering_cost,
            exact_post_validated=True,
            message="solver certificate and exact Decimal post-validation completed",
        )


def _disposition(
    problem: OptimizerProblem,
    context: _ModelContext,
    values: Sequence[int],
) -> PlanDisposition:
    if problem.solve_kind is SolveKind.COUNTERFACTUAL:
        approval_rules = {
            item
            for route_index, route in enumerate(context.routes)
            if values[context.x[route_index]] > 0
            for item in route.approval_requirements
        }
        order_approval_rules = {
            item.rule_id for item in problem.order_approval_constraints
        } - set(problem.approved_order_rule_ids)
        if (
            problem.relaxed_rule_id in approval_rules
            or problem.relaxed_rule_id in order_approval_rules
            or problem.relaxed_rule_id == problem.sub_moq_approval_rule_id
        ):
            return PlanDisposition.RECOMMEND_APPROVAL
        return PlanDisposition.DECISION_REQUIRED
    if problem.solve_kind is not SolveKind.EXECUTABLE:
        return PlanDisposition.DECISION_REQUIRED
    evidence = {
        item
        for route_index, route in enumerate(context.routes)
        if values[context.x[route_index]] > 0
        for item in route.evidence
    }
    if any(
        item.status is EvidenceStatus.UNKNOWN
        and item.contract_disposition is PlanDisposition.EXECUTE_WITH_ASSUMPTION
        for item in evidence
    ):
        return PlanDisposition.EXECUTE_WITH_ASSUMPTION
    return PlanDisposition.EXECUTE


def _build_plan(
    context: _ModelContext,
    values: Sequence[int],
    objective_vector: tuple[Decimal, ...],
    disposition: PlanDisposition,
    *,
    exact: bool,
) -> CandidatePlan | None:
    lines: list[PlanLine] = []
    selected_routes: list[CandidateRoute] = []
    for route_index, route in enumerate(context.routes):
        quantity_atoms = values[context.x[route_index]]
        if quantity_atoms <= 0:
            continue
        allocations = tuple(
            BucketAllocation(
                due_date=bucket.due_date,
                quantity=context.quantity(values[context.z[route_index][bucket_index]]),
                exception_ids=route.exception_codes,
            )
            for bucket_index, bucket in enumerate(context.buckets)
            if values[context.z[route_index][bucket_index]] > 0
        )
        quantity = context.quantity(quantity_atoms)
        group_hash = _canonical_hash(
            {
                "component": tuple(
                    sorted(item.route_fingerprint for item in context.routes)
                ),
                "order_date": route.order_date.isoformat(),
            }
        )
        lines.append(
            PlanLine(
                route_id=route.route_id,
                component_id=route.component_id,
                supplier_id=route.supplier_id,
                quantity=quantity,
                unit_price=route.unit_price,
                order_date=route.order_date,
                expected_delivery_date=route.expected_delivery_date,
                material_available_date=route.material_available_date,
                allocation_group_id=f"allocation-{group_hash}",
                bucket_allocations=allocations,
            )
        )
        selected_routes.append(route)
    if not lines:
        return None
    total_quantity = sum((item.quantity for item in lines), ZERO)
    covered = min(context.problem.net_requirement, total_quantity)
    minimum = context.problem.minimum_compliant_total
    if context.problem.solve_kind is SolveKind.QUANTITY_CALIBRATION:
        minimum = total_quantity
    forced = max(ZERO, (minimum or ZERO) - context.problem.net_requirement)
    discretionary = max(ZERO, total_quantity - (minimum or total_quantity))
    evidence_by_key: dict[tuple[object, ...], EvidenceResult] = {}
    for route in selected_routes:
        for item in route.evidence:
            key = (
                item.rule_id,
                item.status,
                item.scope,
                item.severity,
                item.summary,
            )
            evidence_by_key[key] = item
    assumptions = tuple(
        sorted(
            {
                code
                for item in evidence_by_key.values()
                for code in item.assumption_codes
            }
        )
    )
    relaxed = (context.problem.relaxed_rule_id,) if context.problem.relaxed_rule_id else ()
    plan_key = tuple(
        sorted(
            (
                route.supplier_fingerprint,
                route.route_fingerprint,
                str(lines[index].quantity),
            )
            for index, route in enumerate(selected_routes)
        )
    )
    plan_id = "plan-" + _canonical_hash(
        {
            "semantic_lines": plan_key,
            "solve_kind": context.problem.solve_kind.value,
            "relaxed_rule": context.problem.relaxed_rule_id,
        }
    )
    late_days = sum(
        allocation.quantity
        * Decimal(max(0, (line.material_available_date - allocation.due_date).days))
        for line in lines
        for allocation in line.bucket_allocations
    )
    return CandidatePlan(
        plan_id=plan_id,
        component_id=context.problem.component_id,
        disposition=disposition,
        lines=tuple(lines),
        net_requirement=context.problem.net_requirement,
        eventual_covered_quantity=covered,
        residual_gap=context.problem.net_requirement - covered,
        total_cost=sum((item.line_total for item in lines), ZERO),
        minimum_compliant_total=(
            minimum if disposition.writes_purchase_order else None
        ),
        cheapest_covering_cost=(
            context.problem.cheapest_covering_cost
            if disposition.writes_purchase_order
            else None
        ),
        forced_surplus=forced,
        discretionary_surplus=discretionary,
        unit_late_days=late_days,
        objective_vector=objective_vector,
        relaxed_rule_ids=relaxed,
        evidence=tuple(evidence_by_key.values()),
        unresolved_approval_ids=(
            tuple(sorted({item for route in selected_routes for item in route.approval_requirements}))
            if not disposition.writes_purchase_order
            else ()
        ),
        assumption_codes=assumptions,
        summary=(
            "Certified executable integer-scaled plan."
            if exact and disposition.writes_purchase_order
            else "Non-executable calibrated, baseline, counterfactual, or incumbent diagnostic."
        ),
    )


def _coverage_state(
    problem: OptimizerProblem,
    selected: CandidatePlan | None,
    *,
    unresolved: bool,
    approval_alternatives: bool,
) -> tuple[RequirementState, Decimal]:
    planned = selected.eventual_covered_quantity if selected is not None else ZERO
    residual = problem.net_requirement - planned
    existing_covered = min(
        problem.supply_ledger.total_demand,
        problem.supply_ledger.eventual_supply,
    )
    total_covered = existing_covered + planned
    fulfillment = (
        FulfillmentStatus.FULFILLED
        if residual == ZERO
        else FulfillmentStatus.UNFULFILLED
        if total_covered == ZERO
        else FulfillmentStatus.PARTIALLY_FULFILLED
    )
    if residual == ZERO:
        resolution = ResolutionStatus.RESOLVED
    elif unresolved or approval_alternatives:
        resolution = ResolutionStatus.UNRESOLVED
    else:
        resolution = ResolutionStatus.INFEASIBLE
    return RequirementState(fulfillment, resolution), residual


class ProcurementOptimizer:
    """Run solve Q, solve 0, solve 1, and named solve-2 counterfactuals."""

    def __init__(self, solver: IntegerScaledSolver | None = None) -> None:
        self.solver = solver or IntegerScaledSolver()

    def optimize(self, problem: OptimizerProblem) -> OptimizationOutcome:
        if not isinstance(problem, OptimizerProblem):
            raise TypeError("problem must be OptimizerProblem")
        if problem.solve_kind is not SolveKind.EXECUTABLE:
            problem = replace(
                problem,
                solve_kind=SolveKind.EXECUTABLE,
                minimum_compliant_total=problem.minimum_compliant_total or ZERO,
                coverage_target=problem.coverage_target or ZERO,
                cheapest_covering_cost=problem.cheapest_covering_cost or ZERO,
                relaxed_rule_id=None,
            )
        alerts: list[OptimizerAlert] = []
        alternatives: list[CandidatePlan] = []
        bounds = derive_upper_bounds(replace(problem, solve_kind=SolveKind.QUANTITY_CALIBRATION, minimum_compliant_total=None, coverage_target=None, cheapest_covering_cost=None))

        executable_suppliers = {
            route.supplier_id
            for route in problem.routes
            if route.eligibility is EvidenceStatus.PASS and not route.approval_requirements
        }
        secondary_missing = (
            problem.minimum_secondary_fraction is not None
            and problem.net_requirement > ZERO
            and len(executable_suppliers) < 2
        )
        if secondary_missing:
            kind = problem.secondary_shortage_kind
            if kind is None:
                potential = {
                    route.supplier_id
                    for route in problem.routes
                    if route.eligibility is not EvidenceStatus.FAIL
                }
                kind = SecondaryShortageKind.RELAXABLE if len(potential) >= 2 else SecondaryShortageKind.STRUCTURAL
            # This is the explicitly enumerated compliance-cost diagnostic,
            # not an executable solve and not a claim that one-rule approval
            # can waive a missing supply-base member.  Baseline semantics cost
            # the best hard-eligible one-supplier coverage plan while omitting
            # the inapplicable prospective split row.
            diagnostic_problem = replace(
                problem,
                solve_kind=SolveKind.BASELINE,
                minimum_secondary_fraction=None,
                minimum_secondary_rule_id=None,
                minimum_compliant_total=None,
                coverage_target=None,
                cheapest_covering_cost=None,
                relaxed_rule_id=None,
            )
            diagnostic_result = self.solver.solve(diagnostic_problem)
            diagnostic_plan = diagnostic_result.candidate_plan
            if diagnostic_plan is not None:
                ignored_rules = tuple(
                    sorted(
                        {
                            rule_id
                            for rule_id in (
                                problem.minimum_secondary_rule_id,
                                problem.named_primary_rule_id,
                                *(item.rule_id for item in problem.concentration_constraints),
                            )
                            if rule_id is not None
                        }
                    )
                )
                diagnostic_plan = replace(
                    diagnostic_plan,
                    relaxed_rule_ids=ignored_rules,
                    summary=(
                        "Non-executable compliance-cost diagnostic; allocation "
                        "rules are intentionally excluded and no waiver is claimed."
                    ),
                )
                alternatives.append(diagnostic_plan)
            secondary_counterfactuals: list[SolverResult] = []
            if kind is SecondaryShortageKind.RELAXABLE:
                relaxation_candidates = tuple(
                    sorted(
                        {
                            rule_id
                            for route in problem.routes
                            if route.supplier_id not in executable_suppliers
                            and route.eligibility is not EvidenceStatus.FAIL
                            for rule_id in route.approval_requirements
                        }
                    )
                )
                for rule_id in relaxation_candidates:
                    relaxed_q = self.solver.solve(
                        replace(
                            problem,
                            solve_kind=SolveKind.QUANTITY_CALIBRATION,
                            minimum_compliant_total=None,
                            coverage_target=None,
                            cheapest_covering_cost=None,
                            relaxed_rule_id=rule_id,
                        )
                    )
                    relaxed_baseline = self.solver.solve(
                        replace(
                            problem,
                            solve_kind=SolveKind.BASELINE,
                            minimum_compliant_total=None,
                            coverage_target=None,
                            cheapest_covering_cost=None,
                            relaxed_rule_id=rule_id,
                        )
                    )
                    if not (
                        relaxed_q.is_certified_optimal
                        and relaxed_q.exact_post_validated
                        and relaxed_baseline.is_certified_optimal
                        and relaxed_baseline.exact_post_validated
                    ):
                        continue
                    relaxed_plan = relaxed_q.candidate_plan
                    counterfactual = self.solver.solve(
                        replace(
                            problem,
                            solve_kind=SolveKind.COUNTERFACTUAL,
                            minimum_compliant_total=relaxed_q.minimum_compliant_total or ZERO,
                            coverage_target=(
                                relaxed_plan.eventual_covered_quantity
                                if relaxed_plan is not None
                                else ZERO
                            ),
                            cheapest_covering_cost=relaxed_baseline.cheapest_covering_cost or ZERO,
                            relaxed_rule_id=rule_id,
                        )
                    )
                    secondary_counterfactuals.append(counterfactual)
                    if counterfactual.candidate_plan is not None:
                        alternatives.append(counterfactual.candidate_plan)
            alerts.append(
                OptimizerAlert(
                    AlertCategory.SOLE_SOURCE if kind is SecondaryShortageKind.STRUCTURAL else AlertCategory.DECISION_REQUIRED,
                    "SECONDARY_ALLOCATION_UNSATISFIABLE",
                    f"The prospective secondary-allocation rule is {kind.value}ly unsatisfiable with fewer than two eligible suppliers.",
                    problem.component_id,
                    diagnostic_plan,
                    (problem.minimum_secondary_rule_id,) if problem.minimum_secondary_rule_id else (),
                )
            )
            state, residual = _coverage_state(problem, None, unresolved=True, approval_alternatives=True)
            return OptimizationOutcome(
                problem.component_id,
                None,
                None,
                None,
                tuple(secondary_counterfactuals),
                None,
                tuple(alternatives),
                state,
                residual,
                tuple(alerts),
                bounds,
                (),
            )

        calibration_problem = replace(
            problem,
            solve_kind=SolveKind.QUANTITY_CALIBRATION,
            minimum_compliant_total=None,
            coverage_target=None,
            cheapest_covering_cost=None,
            relaxed_rule_id=None,
        )
        calibration = self.solver.solve(calibration_problem)
        if not calibration.is_certified_optimal or not calibration.exact_post_validated:
            alerts.append(
                OptimizerAlert(
                    AlertCategory.SOLVER_UNPROVEN,
                    "SOLVE_Q_UNPROVEN",
                    "Quantity calibration lacked a completed zero-gap certificate; no order is executable.",
                    problem.component_id,
                    calibration.candidate_plan,
                )
            )
            if calibration.candidate_plan:
                alternatives.append(calibration.candidate_plan)
            state, residual = _coverage_state(problem, None, unresolved=True, approval_alternatives=False)
            return OptimizationOutcome(problem.component_id, calibration, None, None, (), None, tuple(alternatives), state, residual, tuple(alerts), bounds, ())
        q_min = calibration.minimum_compliant_total or ZERO
        coverage_target = calibration.candidate_plan.eventual_covered_quantity if calibration.candidate_plan else ZERO

        baseline_problem = replace(
            problem,
            solve_kind=SolveKind.BASELINE,
            minimum_compliant_total=None,
            coverage_target=None,
            cheapest_covering_cost=None,
            relaxed_rule_id=None,
        )
        baseline = self.solver.solve(baseline_problem)
        if not baseline.is_certified_optimal or not baseline.exact_post_validated:
            alerts.append(
                OptimizerAlert(
                    AlertCategory.SOLVER_UNPROVEN,
                    "SOLVE_0_UNPROVEN",
                    "The cheapest-covering baseline lacked a completed zero-gap certificate; no order is executable.",
                    problem.component_id,
                    baseline.candidate_plan,
                )
            )
            if baseline.candidate_plan:
                alternatives.append(baseline.candidate_plan)
            state, residual = _coverage_state(problem, None, unresolved=True, approval_alternatives=False)
            return OptimizationOutcome(problem.component_id, calibration, baseline, None, (), None, tuple(alternatives), state, residual, tuple(alerts), bounds, ())
        cheapest = baseline.cheapest_covering_cost or ZERO

        # Fixed, explicitly multi-rule compliance-cost diagnostic.  It is
        # never part of the executable or one-rule recommendation solve set.
        if problem.minimum_secondary_fraction is not None or problem.concentration_constraints:
            bare_problem = replace(
                baseline_problem,
                minimum_secondary_fraction=None,
                minimum_secondary_rule_id=None,
                concentration_constraints=(),
            )
            bare_result = self.solver.solve(bare_problem)
            bare_plan = bare_result.candidate_plan
            calibrated_plan = calibration.candidate_plan
            if (
                bare_result.is_certified_optimal
                and bare_result.exact_post_validated
                and bare_plan is not None
                and calibrated_plan is not None
                and (
                    bare_plan.total_cost != calibrated_plan.total_cost
                    or tuple((item.supplier_id, item.quantity) for item in bare_plan.lines)
                    != tuple((item.supplier_id, item.quantity) for item in calibrated_plan.lines)
                )
            ):
                ignored_rules = tuple(
                    sorted(
                        {
                            rule_id
                            for rule_id in (
                                problem.minimum_secondary_rule_id,
                                problem.named_primary_rule_id,
                                *(item.rule_id for item in problem.concentration_constraints),
                            )
                            if rule_id is not None
                        }
                    )
                )
                bare_plan = replace(
                    bare_plan,
                    relaxed_rule_ids=ignored_rules,
                    summary=(
                        "Non-executable compliance-cost diagnostic; allocation "
                        "rules are intentionally excluded and no waiver is claimed."
                    ),
                )
                alternatives.append(bare_plan)
                alerts.append(
                    OptimizerAlert(
                        AlertCategory.COST_OPPORTUNITY,
                        "COMPLIANCE_COST_DIAGNOSTIC",
                        "A non-executable bare-coverage diagnostic quantifies the cost of allocation compliance; it does not authorize a multi-rule waiver.",
                        problem.component_id,
                        bare_plan,
                    )
                )

        executable_problem = replace(
            problem,
            solve_kind=SolveKind.EXECUTABLE,
            minimum_compliant_total=q_min,
            coverage_target=coverage_target,
            cheapest_covering_cost=cheapest,
            relaxed_rule_id=None,
        )
        executable = self.solver.solve(executable_problem)
        executable_emitted = (
            self.solver.last_context.emitted_rule_ids
            if self.solver.last_context is not None
            else ()
        )
        selected = executable.candidate_plan if executable.has_executable_certificate else None
        if selected is None and not (
            executable.is_certified_optimal and executable.exact_post_validated
        ) and executable.status is not SolverStatus.INFEASIBLE:
            alerts.append(
                OptimizerAlert(
                    AlertCategory.SOLVER_UNPROVEN,
                    "SOLVE_1_UNPROVEN",
                    "The executable solve lacked a completed certificate or exact validation.",
                    problem.component_id,
                    executable.candidate_plan,
                )
            )
            if executable.candidate_plan:
                alternatives.append(executable.candidate_plan)
        elif executable.status is SolverStatus.INFEASIBLE:
            alerts.append(
                OptimizerAlert(
                    AlertCategory.DECISION_REQUIRED,
                    "AUTONOMY_OR_MEMBERSHIP_CONFLICT",
                    "No plan exists inside the inclusive executable-set bounds.",
                    problem.component_id,
                )
            )

        counterfactuals: list[SolverResult] = []
        requested_relaxations = set(problem.relaxation_rule_ids)
        requested_relaxations.update(
            item
            for route in problem.routes
            for item in route.approval_requirements
        )
        requested_relaxations.update(
            item.rule_id
            for item in problem.order_approval_constraints
            if item.rule_id not in problem.approved_order_rule_ids
        )
        if problem.named_primary_rule_id:
            requested_relaxations.add(problem.named_primary_rule_id)
        if problem.sub_moq_approval_rule_id and any(
            route.minimum_order_quantity > problem.net_requirement
            for route in problem.routes
            if route.eligibility is EvidenceStatus.PASS
        ):
            requested_relaxations.add(problem.sub_moq_approval_rule_id)
        for rule_id in sorted(requested_relaxations):
            counterfactual_problem = replace(
                problem,
                solve_kind=SolveKind.COUNTERFACTUAL,
                minimum_compliant_total=q_min,
                coverage_target=coverage_target,
                cheapest_covering_cost=cheapest,
                relaxed_rule_id=rule_id,
            )
            result = self.solver.solve(counterfactual_problem)
            counterfactuals.append(result)
            if result.candidate_plan is not None:
                alternatives.append(result.candidate_plan)
            if not result.is_certified_optimal:
                alerts.append(
                    OptimizerAlert(
                        AlertCategory.SOLVER_UNPROVEN,
                        "SOLVE_2_UNPROVEN",
                        f"Counterfactual {rule_id!r} is diagnostic-only because its solve is unproven.",
                        problem.component_id,
                        result.candidate_plan,
                        (rule_id,),
                    )
                )

        if selected and selected.forced_surplus > ZERO:
            alerts.append(
                OptimizerAlert(
                    AlertCategory.FORCED_SURPLUS,
                    "FORCED_SURPLUS",
                    f"Executable policy and MOQ conditions force {selected.forced_surplus} surplus units at total cost {selected.total_cost}.",
                    problem.component_id,
                    selected,
                )
            )
        unresolved = not executable.has_executable_certificate and executable.status is not SolverStatus.INFEASIBLE
        approval_alternatives = any(
            item.disposition in {PlanDisposition.RECOMMEND_APPROVAL, PlanDisposition.DECISION_REQUIRED}
            for item in alternatives
        )
        state, residual = _coverage_state(
            problem,
            selected,
            unresolved=unresolved,
            approval_alternatives=approval_alternatives,
        )
        return OptimizationOutcome(
            problem.component_id,
            calibration,
            baseline,
            executable,
            tuple(counterfactuals),
            selected,
            tuple(sorted(alternatives, key=lambda item: item.plan_id)),
            state,
            residual,
            tuple(alerts),
            bounds,
            executable_emitted,
        )


def solve_component(
    problem: OptimizerProblem,
    *,
    solver: IntegerScaledSolver | None = None,
) -> OptimizationOutcome:
    return ProcurementOptimizer(solver).optimize(problem)


# Clear public aliases for callers/tests that prefer the implementation name.
HighsSolver = IntegerScaledSolver
MilpSolver = IntegerScaledSolver


class StdlibSolver(IntegerScaledSolver):
    """The bounded stdlib backend exposed through the frozen Solver protocol."""

    def __init__(
        self,
        *,
        limits: SolverLimits | None = None,
        upper_bound_multiplier: int = 1,
    ) -> None:
        backend = StdlibBranchAndBoundBackend()
        super().__init__(
            backend=backend,
            fallback=backend,
            limits=limits,
            upper_bound_multiplier=upper_bound_multiplier,
        )


__all__ = [
    "ConcentrationConstraint",
    "EconomicAutonomy",
    "ExceptionAllowance",
    "HighsSolver",
    "IntegerScaledSolver",
    "MilpSolver",
    "OptimizationOutcome",
    "OptimizerAlert",
    "OptimizerProblem",
    "OrderApprovalConstraint",
    "ProcurementOptimizer",
    "ScipyMilpBackend",
    "SecondaryShortageKind",
    "SolverLimits",
    "StdlibBranchAndBoundBackend",
    "StdlibSolver",
    "SupplierVolume",
    "derive_upper_bounds",
    "solve_component",
]
