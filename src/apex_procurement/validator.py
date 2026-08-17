"""Independent, exact validation of procurement decisions.

This module intentionally does not import :mod:`apex_procurement.optimizer`,
candidate construction, the ledger builder, or the policy evaluator.  The
validator rebuilds its facts and its small integer model directly from the
immutable snapshot and the reviewed policy registry.  Keeping this boundary
boring and duplicated is deliberate: sharing a convenient helper with the
planner would also share its mistakes.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from enum import Enum
from fractions import Fraction
import hashlib
from itertools import combinations
import json
from math import gcd, lcm
import re
import unicodedata

from .config import EvidenceContract
from .domain import (
    AlertCategory,
    CandidatePlan,
    Component,
    DecisionComparatorFact,
    DeadlineLateness,
    DecisionRecord,
    EvidenceBasis,
    EvidenceResult,
    EvidenceScope,
    EvidenceStatus,
    FulfillmentStatus,
    MaterialRouteRejection,
    PlanDisposition,
    PlanLine,
    ResolutionStatus,
    RouteInputIssue,
    RouteQuarantineScope,
    RuleSeverity,
    ScenarioSnapshot,
    SolveKind,
    SolverResult,
    SolverStatus,
    Supplier,
    SupplierCatalogLine,
    ValidationIssue,
    ValidationResult,
    ValidationSeverity,
    ZERO,
)
from .policy.registry import PolicyRegistry, PolicyRule, load_policy_registry
from .policy.parameters import (
    ApplicablePolicyParameters,
    EconomicAutonomyParameters,
    SecondaryAllocationParameters,
)


_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_CERT_SPLIT_RE = re.compile(r"[,;|/]")
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_TERMINAL_ALERTS = frozenset(
    {
        AlertCategory.NO_ELIGIBLE_SUPPLIER,
        AlertCategory.DECISION_REQUIRED,
        AlertCategory.APPROVAL_REQUIRED,
        AlertCategory.SOLVER_UNPROVEN,
        AlertCategory.POLICY_CONFLICT,
        AlertCategory.DATA_QUALITY,
    }
)
_SYNTHETIC_RULE_PREFIXES = (
    "MASTER-DATA.",
    "DATA-QUALITY.",
    "VALIDATOR.",
)
_EXECUTABLE_OBJECTIVE_SUFFIX = (
    "stage_02_unit_late_days",
    "stage_03_discretionary_surplus",
    "stage_04_policy_review_exposure",
    "stage_05_named_primary_deviation",
    "stage_06_international_volume",
    "stage_07_strategic_shift",
    "stage_08_sustainability_band",
    "stage_09_known_landed_cost",
    "stage_09_moq_excess",
    "stage_10_total_lead_time",
    "stage_10_line_count",
)
_COMPLIANCE_DIAGNOSTIC_PREFIX = "Non-executable compliance-cost diagnostic;"


def _quarantined_supplier_ids(snapshot: ScenarioSnapshot) -> frozenset[str]:
    return frozenset(
        issue.supplier_id
        for issue in snapshot.route_input_issues
        if issue.blast_radius is RouteQuarantineScope.SUPPLIER_ALL_ROUTES
    )


def _quarantined_catalog_keys(
    snapshot: ScenarioSnapshot,
) -> frozenset[tuple[str, str]]:
    return frozenset(
        (issue.supplier_id, issue.component_id)
        for issue in snapshot.route_input_issues
        if issue.blast_radius is RouteQuarantineScope.CATALOG_OFFER
        and issue.component_id is not None
    )


def _evidence_contract_blockers(plan: CandidatePlan) -> tuple[EvidenceResult, ...]:
    return tuple(
        item
        for item in plan.evidence
        if item.severity is RuleSeverity.HARD
        and item.status is EvidenceStatus.UNKNOWN
        and item.contract_disposition is PlanDisposition.DECISION_REQUIRED
    )


def _is_evidence_contract_diagnostic(plan: CandidatePlan) -> bool:
    return (
        plan.disposition is PlanDisposition.DECISION_REQUIRED
        and bool(_evidence_contract_blockers(plan))
        and not plan.relaxed_rule_ids
    )


class NamedEntityOutcome(str, Enum):
    RESOLVED = "RESOLVED"
    STALE_SOURCE_ID = "STALE_SOURCE_ID"
    CONFLICT = "CONFLICT"
    MISSING_OR_AMBIGUOUS = "MISSING_OR_AMBIGUOUS"


@dataclass(frozen=True, slots=True)
class NamedEntityCheck:
    outcome: NamedEntityOutcome
    supplier_id: str | None
    source_id: str
    legal_name: str

    @property
    def resolved(self) -> bool:
        return self.outcome in {
            NamedEntityOutcome.RESOLVED,
            NamedEntityOutcome.STALE_SOURCE_ID,
        }


@dataclass(frozen=True, slots=True)
class IndependentSolve:
    status: SolverStatus
    objective_vector: tuple[Decimal, ...]
    allocation: tuple[tuple[str, Decimal], ...] = ()
    minimum_compliant_total: Decimal | None = None
    cheapest_covering_cost: Decimal | None = None
    certificate_complete: bool = True
    recovery_quantity: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class IndependentSolverLimits:
    """Resource controls for the validator's independently built MILPs."""

    time_limit_seconds: float | None = None
    force_status: SolverStatus | None = None

    def __post_init__(self) -> None:
        if self.time_limit_seconds is not None and (
            not isinstance(self.time_limit_seconds, (int, float))
            or isinstance(self.time_limit_seconds, bool)
            or self.time_limit_seconds <= 0
        ):
            raise ValueError("time_limit_seconds must be positive or None")
        if self.force_status is not None and not isinstance(
            self.force_status, SolverStatus
        ):
            raise TypeError("force_status must be SolverStatus or None")


@dataclass(frozen=True, slots=True)
class _IndependentMilpRow:
    coefficients: Mapping[int, Fraction]
    lower: Fraction | None = None
    upper: Fraction | None = None


@dataclass(slots=True)
class _IndependentMilpModel:
    names: list[str] = field(default_factory=list)
    lower: list[int] = field(default_factory=list)
    upper: list[int] = field(default_factory=list)
    rows: list[_IndependentMilpRow] = field(default_factory=list)

    def add_variable(self, name: str, lower: int, upper: int) -> int:
        if lower > upper:
            raise ValueError(f"invalid bounds for {name}")
        index = len(self.names)
        self.names.append(name)
        self.lower.append(lower)
        self.upper.append(upper)
        return index

    def add_row(
        self,
        coefficients: Mapping[int, Fraction | int],
        *,
        lower: Fraction | int | None = None,
        upper: Fraction | int | None = None,
    ) -> None:
        exact = {
            index: value if isinstance(value, Fraction) else Fraction(value)
            for index, value in coefficients.items()
            if value
        }
        lower_fraction = (
            None
            if lower is None
            else lower
            if isinstance(lower, Fraction)
            else Fraction(lower)
        )
        upper_fraction = (
            None
            if upper is None
            else upper
            if isinstance(upper, Fraction)
            else Fraction(upper)
        )
        self.rows.append(_IndependentMilpRow(exact, lower_fraction, upper_fraction))


@dataclass(frozen=True, slots=True)
class _IndependentMilpResult:
    status: SolverStatus
    values: tuple[int, ...] | None
    certificate_complete: bool


class _IndependentSolveInterrupted(Exception):
    def __init__(self, status: SolverStatus) -> None:
        super().__init__(status.value)
        self.status = status


@dataclass(frozen=True, slots=True)
class _SourceRequirement:
    component: Component
    bucket_quantities: tuple[tuple[date, Decimal], ...]
    total_demand: Decimal
    on_hand: Decimal
    inbound: tuple[tuple[str, Decimal, date], ...]
    eventual_supply: Decimal
    eventual_gap: Decimal

    def cumulative_demand(self, due: date) -> Decimal:
        return sum((quantity for item_due, quantity in self.bucket_quantities if item_due <= due), ZERO)

    def on_time_supply(self, due: date) -> Decimal:
        return self.on_hand + sum(
            (quantity for _number, quantity, delivery in self.inbound if delivery <= due), ZERO
        )

    def bucket_shortage(self, due: date) -> Decimal:
        bucket = dict(self.bucket_quantities)[due]
        gap = max(ZERO, self.cumulative_demand(due) - self.on_time_supply(due))
        return min(bucket, gap)


@dataclass(frozen=True, slots=True)
class _SupplySegment:
    bucket_index: int
    due_date: date
    quantity: Decimal
    material_available: date | None
    committed: bool
    uncovered: bool


@dataclass(frozen=True, slots=True)
class _Offer:
    component: Component
    supplier: Supplier
    catalog: SupplierCatalogLine
    expected_delivery: date
    material_available: date
    lead_days: int
    shipping_method: str
    international: bool
    review_keys: frozenset[str]
    allowed_buckets: tuple[date, ...]
    gate_conditions: tuple[tuple[date, str], ...]
    upper_bound: Decimal
    supplier_fingerprint: str
    route_fingerprint: str
    route_id: str

    def condition_for(self, due: date) -> str | None:
        return dict(self.gate_conditions).get(due)


@dataclass(slots=True)
class _IssueSink:
    values: list[ValidationIssue]

    def error(
        self,
        code: str,
        message: str,
        *,
        component_id: str | None = None,
        plan_id: str | None = None,
        rule_ids: Iterable[str] = (),
    ) -> None:
        self.values.append(
            ValidationIssue(
                code=code,
                severity=ValidationSeverity.ERROR,
                message=message,
                component_id=component_id,
                plan_id=plan_id,
                rule_ids=tuple(rule_ids),
            )
        )

    def warning(
        self,
        code: str,
        message: str,
        *,
        component_id: str | None = None,
        plan_id: str | None = None,
        rule_ids: Iterable[str] = (),
    ) -> None:
        self.values.append(
            ValidationIssue(
                code=code,
                severity=ValidationSeverity.WARNING,
                message=message,
                component_id=component_id,
                plan_id=plan_id,
                rule_ids=tuple(rule_ids),
            )
        )


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(_TOKEN_RE.findall(unicodedata.normalize("NFKC", value).casefold()))


def _normal_name(value: str) -> str:
    return " ".join(_tokens(value))


def _normal_text(value: str | None) -> str | None:
    return None if value is None else " ".join(_tokens(value))


def _phrase(haystack: tuple[str, ...], phrase: str) -> bool:
    needle = _tokens(phrase)
    return bool(needle) and any(
        haystack[index : index + len(needle)] == needle
        for index in range(len(haystack) - len(needle) + 1)
    )


def _source_term_overlap(tokens: tuple[str, ...], terms: Iterable[object]) -> bool:
    target = frozenset(tokens)
    for raw_term in terms:
        text_value = str(raw_term)
        significant = tuple(token for token in _tokens(text_value) if len(token) > 2)
        if significant and set(significant).issubset(target):
            return True
        acronyms = {
            token.casefold()
            for token in re.findall(r"\b[A-Z][A-Z0-9]{1,}\b", text_value)
        }
        if acronyms.intersection(target):
            return True
    return False


def _certification(value: str) -> str:
    return "".join(_tokens(value)).upper()


def _certifications(values: Iterable[str]) -> frozenset[str]:
    return frozenset(
        _certification(part)
        for value in values
        for part in _CERT_SPLIT_RE.split(value)
        if part.strip()
    )


def _canonical_hash(payload: Mapping[str, object]) -> str:
    """Hash semantic facts without importing candidate construction code."""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _supplier_fingerprint(supplier: Supplier) -> str:
    return _canonical_hash(
        {
            "legal_name": _normal_name(supplier.name),
            "country": _normal_text(supplier.country),
            "certifications": tuple(sorted(_certifications(supplier.certifications))),
            "relationship_tier": _normal_text(supplier.relationship_tier),
            "sustainability_rating": _normal_text(supplier.sustainability_rating),
        }
    )


def _component_fingerprint(component: Component) -> str:
    return _canonical_hash(
        {
            "name": _normal_text(component.name),
            "description": _normal_text(component.description),
            "category": _normal_text(component.category),
            "unit_of_measure": _normal_text(component.unit_of_measure),
            "is_hazardous": component.is_hazardous,
            "required_certifications": tuple(
                sorted(_certifications(component.required_certifications))
            ),
        }
    )


def _route_fingerprint(
    component: Component,
    catalog: SupplierCatalogLine,
    shipping_method: str,
    lead_days: int,
) -> str:
    return _canonical_hash(
        {
            "component": _component_fingerprint(component),
            "unit_price": str(catalog.unit_price),
            "minimum_order_quantity": str(catalog.minimum_order_quantity),
            "catalog_lead_time_days": catalog.lead_time_days,
            "effective_lead_time_days": lead_days,
            "shipping_method": _normal_text(shipping_method),
            "catalog_notes": _normal_text(catalog.notes),
        }
    )


def _independent_route_id(
    supplier_fingerprint: str,
    route_fingerprint: str,
    exception_codes: Iterable[str],
    feasible_deadlines: Iterable[date],
    exception_scope_deadlines: Iterable[date],
) -> str:
    """Reproduce the public route identity without importing candidate code."""

    digest = _canonical_hash(
        {
            "supplier_fingerprint": supplier_fingerprint,
            "route_fingerprint": route_fingerprint,
            "exceptions": sorted(exception_codes),
            "feasible_deadlines": sorted(
                item.isoformat() for item in feasible_deadlines
            ),
            "exception_scope_deadlines": sorted(
                item.isoformat() for item in exception_scope_deadlines
            ),
        }
    )
    return f"route-{digest}"


def _holds(supplier: Supplier, required: str) -> bool:
    wanted = _certification(required)
    held = _certifications(supplier.certifications)
    if wanted in held:
        return True
    return wanted in {"ULLISTING", "ULLISTED"} and bool(
        held & {"ULLISTING", "ULLISTED"}
    )


def _rating(value: str | None) -> Decimal | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", value).strip().upper()
    if not text or text[0] not in "ABCDEF":
        return None
    base = Decimal(6 - (ord(text[0]) - ord("A")))
    if len(text) == 1:
        return base
    if text[1:] == "+":
        return base + Decimal("0.25")
    if text[1:] == "-":
        return base - Decimal("0.25")
    return None


def _business_days(left: date, right: date) -> int:
    start, end = sorted((left, right))
    count = 0
    while start < end:
        start += timedelta(days=1)
        if start.weekday() < 5:
            count += 1
    return count


def _ceil_units(value: Decimal, increment: Decimal) -> int:
    return int((value / increment).to_integral_value(rounding=ROUND_CEILING))


def _floor_units(value: Decimal, increment: Decimal) -> int:
    return int((value / increment).to_integral_value(rounding=ROUND_FLOOR))


def _decimal(value: Fraction) -> Decimal:
    return Decimal(value.numerator) / Decimal(value.denominator)


class IndependentPlanValidator:
    """Rebuild source facts and independently certify proposed decisions."""

    def __init__(
        self,
        registry: PolicyRegistry | None = None,
        *,
        policy_parameters: ApplicablePolicyParameters | None = None,
        autonomy: EconomicAutonomyParameters | None = None,
        receiving_buffer_days: int = 0,
        accepted_shipment_pairs: Iterable[tuple[str, str]] = (),
        approved_rule_ids: Iterable[str] = (),
        capacity_confirmed_supplier_ids: Iterable[str] = (),
        numeric_capacity_by_supplier: Mapping[str, Decimal] | None = None,
        enumeration_node_limit: int = 2_000_000,
        tiny_case_unit_limit: int = 64,
        solver_limits: IndependentSolverLimits | None = None,
    ) -> None:
        self.registry = registry or load_policy_registry()
        if not isinstance(self.registry, PolicyRegistry):
            raise TypeError("registry must be PolicyRegistry")
        if policy_parameters is not None and not isinstance(
            policy_parameters, ApplicablePolicyParameters
        ):
            raise TypeError("policy_parameters must be ApplicablePolicyParameters or None")
        if autonomy is not None and not isinstance(
            autonomy, EconomicAutonomyParameters
        ):
            raise TypeError("autonomy must be EconomicAutonomyParameters or None")
        if not isinstance(receiving_buffer_days, int) or isinstance(receiving_buffer_days, bool) or receiving_buffer_days < 0:
            raise ValueError("receiving_buffer_days must be a nonnegative int")
        if not isinstance(enumeration_node_limit, int) or enumeration_node_limit <= 0:
            raise ValueError("enumeration_node_limit must be positive")
        if (
            not isinstance(tiny_case_unit_limit, int)
            or isinstance(tiny_case_unit_limit, bool)
            or tiny_case_unit_limit < 0
        ):
            raise ValueError("tiny_case_unit_limit must be a nonnegative int")
        if solver_limits is not None and not isinstance(
            solver_limits, IndependentSolverLimits
        ):
            raise TypeError("solver_limits must be IndependentSolverLimits or None")
        self.policy_parameters = policy_parameters
        self.autonomy = autonomy or (
            policy_parameters.economic_autonomy
            if policy_parameters is not None
            else self.registry.economic_autonomy
        )
        self.receiving_buffer_days = receiving_buffer_days
        self.accepted_shipment_pairs = frozenset(tuple(item) for item in accepted_shipment_pairs)
        self.approved_rule_ids = frozenset(approved_rule_ids)
        self.capacity_confirmed_supplier_ids = frozenset(capacity_confirmed_supplier_ids)
        self.numeric_capacity_by_supplier = dict(numeric_capacity_by_supplier or {})
        if any(value < ZERO or not value.is_finite() for value in self.numeric_capacity_by_supplier.values()):
            raise ValueError("numeric capacity values must be finite and nonnegative")
        self.enumeration_node_limit = enumeration_node_limit
        self.tiny_case_unit_limit = tiny_case_unit_limit
        self.solver_limits = solver_limits or IndependentSolverLimits()
        self._concepts = {
            str(item["concept_id"]): item for item in self.registry.concepts["concepts"]
        }
        aliases: dict[tuple[str, ...], str] = {}
        for country, raw_aliases in self.registry.concepts["country_aliases"].items():
            for alias in raw_aliases:
                aliases[_tokens(str(alias))] = str(country)
        self._country_aliases = aliases
        known_international: set[tuple[str, ...]] = set()
        international = self._concepts.get("international_supplier", {})
        for fixture in international.get("positive_fixtures", ()):
            fixture_tokens = _tokens(str(fixture))
            if fixture_tokens[:1] == ("country",) and len(fixture_tokens) > 1:
                known_international.add(fixture_tokens[1:])
        self._known_international = frozenset(known_international)

    def _parameters(self, current: date) -> ApplicablePolicyParameters:
        parameters = self.policy_parameters or self.registry.parameters_for(current)
        if parameters.scenario_date != current:
            raise ValueError("policy_parameters scenario date does not match the snapshot")
        if parameters.content_hash != self.registry.content_hash:
            raise ValueError("policy_parameters do not belong to the active registry")
        return parameters

    # ---- independent policy and source reconstruction -----------------

    def _rules(self, current: date, kind: str) -> tuple[PolicyRule, ...]:
        return tuple(
            rule
            for rule in self.registry.active_rules(current)
            if isinstance(rule.data.get("constraint"), Mapping)
            and rule.data["constraint"].get("kind") == kind
        )

    def _directive_rules(self, current: date, kind: str) -> tuple[PolicyRule, ...]:
        return tuple(
            rule
            for rule in self.registry.active_rules(current)
            if isinstance(rule.data.get("directive"), Mapping)
            and rule.data["directive"].get("kind") == kind
        )

    def _concept(self, concept_id: str, entity: Component | Supplier) -> EvidenceStatus:
        concept = self._concepts[concept_id]
        if concept["entity_kind"] == "supplier":
            if not isinstance(entity, Supplier):
                return EvidenceStatus.UNKNOWN
            if concept_id in {"domestic_supplier", "international_supplier"}:
                canonical = self._country_aliases.get(_tokens(entity.country or ""))
                if canonical is None:
                    if _tokens(entity.country or "") not in self._known_international:
                        return EvidenceStatus.UNKNOWN
                    domestic = False
                else:
                    domestic = canonical in self.registry.concepts["country_aliases"]
                if concept_id == "international_supplier":
                    domestic = not domestic
                return EvidenceStatus.PASS if domestic else EvidenceStatus.FAIL
            if concept_id == "strategic_supplier":
                return EvidenceStatus.PASS if _tokens(entity.relationship_tier or "") == ("strategic",) else EvidenceStatus.FAIL
            return EvidenceStatus.UNKNOWN
        if not isinstance(entity, Component):
            return EvidenceStatus.UNKNOWN
        if concept_id == "hazardous_material":
            return EvidenceStatus.PASS if entity.is_hazardous else EvidenceStatus.FAIL
        if concept_id == "critical_component":
            children = ("microcontroller_ic", "power_mosfet", "pcb_blank", "neodymium_magnet", "sensor_ic")
            statuses = tuple(self._concept(item, entity) for item in children)
            if EvidenceStatus.PASS in statuses:
                return EvidenceStatus.PASS
            identity = _tokens(" ".join(filter(None, (entity.name, entity.category))))
            inferred_source_overlap = any(
                _source_term_overlap(
                    identity,
                    self._concepts[child].get("source_terms", ()),
                )
                for child in children
            ) or _source_term_overlap(
                identity,
                self._concepts[concept_id].get("source_terms", ()),
            )
            return (
                EvidenceStatus.UNKNOWN
                if EvidenceStatus.UNKNOWN in statuses or inferred_source_overlap
                else EvidenceStatus.FAIL
            )
        identity = _tokens(" ".join(filter(None, (entity.name, entity.category))))
        if any(_phrase(identity, str(item)) for item in concept.get("negative_fixtures", ())):
            return EvidenceStatus.FAIL
        if concept_id == "safety_critical_part" and entity.required_certifications:
            return EvidenceStatus.PASS
        if concept_id == "electronic_component" and _tokens(entity.category or "") == ("electronic", "component"):
            return EvidenceStatus.PASS
        if any(_phrase(identity, str(item)) for item in concept.get("synonyms", ())):
            return EvidenceStatus.PASS
        if (
            str(concept.get("resolution")) == "enumerated_both_ways"
            and _source_term_overlap(identity, concept.get("source_terms", ()))
        ):
            return EvidenceStatus.UNKNOWN
        return EvidenceStatus.FAIL

    def resolve_source_named_entity(
        self, reference: Mapping[str, object], suppliers: Sequence[Supplier]
    ) -> NamedEntityCheck:
        """Apply the asymmetric four-case ladder without the planner resolver."""

        source_id = str(reference.get("source_id", ""))
        legal_name = str(reference.get("legal_name", ""))
        id_match = next((item for item in suppliers if item.supplier_id == source_id), None)
        name_matches = tuple(item for item in suppliers if _normal_name(item.name) == _normal_name(legal_name))
        if id_match is not None and len(name_matches) == 1 and name_matches[0] is id_match:
            return NamedEntityCheck(NamedEntityOutcome.RESOLVED, id_match.supplier_id, source_id, legal_name)
        if id_match is None and len(name_matches) == 1:
            return NamedEntityCheck(NamedEntityOutcome.STALE_SOURCE_ID, name_matches[0].supplier_id, source_id, legal_name)
        if id_match is not None and len(name_matches) == 1 and name_matches[0] is not id_match:
            return NamedEntityCheck(NamedEntityOutcome.CONFLICT, None, source_id, legal_name)
        return NamedEntityCheck(NamedEntityOutcome.MISSING_OR_AMBIGUOUS, None, source_id, legal_name)

    def _source_requirements(self, snapshot: ScenarioSnapshot, sink: _IssueSink) -> dict[str, _SourceRequirement]:
        bom_by_product: dict[str, list[tuple[str, Decimal]]] = defaultdict(list)
        for item in snapshot.bom_lines:
            bom_by_product[item.product_id].append((item.component_id, item.quantity_per))
        buckets: dict[str, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        for order in snapshot.production_orders:
            for component_id, quantity_per in bom_by_product.get(order.product_id, ()):
                buckets[component_id][order.materials_needed_by] += order.quantity * quantity_per
        components = {item.component_id: item for item in snapshot.components}
        inventory = {item.component_id: item.quantity_on_hand for item in snapshot.inventory}
        inbound: dict[str, list[tuple[str, Decimal, date]]] = defaultdict(list)
        current = snapshot.configuration.current_date
        for item in snapshot.purchase_orders:
            if item.expected_delivery_date is None:
                continue
            if item.expected_delivery_date < current:
                continue
            inbound[item.component_id].append((item.po_number, item.quantity, item.expected_delivery_date))
        result: dict[str, _SourceRequirement] = {}
        for component_id, raw_buckets in buckets.items():
            component = components[component_id]
            ordered = tuple(sorted(raw_buckets.items()))
            total = sum((quantity for _due, quantity in ordered), ZERO)
            on_hand = inventory.get(component_id, ZERO)
            committed = tuple(sorted(inbound.get(component_id, ()), key=lambda item: (item[2], item[0])))
            eventual = on_hand + sum((item[1] for item in committed), ZERO)
            result[component_id] = _SourceRequirement(
                component, ordered, total, on_hand, committed, eventual, max(ZERO, total - eventual)
            )
        return result

    def _supply_segments(
        self,
        requirement: _SourceRequirement,
    ) -> tuple[_SupplySegment, ...]:
        """Independently allocate existing supply once in deadline order."""

        supplies: list[list[object]] = []
        if requirement.on_hand > ZERO:
            supplies.append([requirement.on_hand, None, False])
        supplies.extend(
            [quantity, delivery, True]
            for _number, quantity, delivery in requirement.inbound
        )
        supply_index = 0
        result: list[_SupplySegment] = []
        for bucket_index, (due, quantity) in enumerate(
            requirement.bucket_quantities
        ):
            remaining = quantity
            while remaining > ZERO and supply_index < len(supplies):
                available, material_date, committed = supplies[supply_index]
                assert isinstance(available, Decimal)
                assigned = min(remaining, available)
                result.append(
                    _SupplySegment(
                        bucket_index,
                        due,
                        assigned,
                        material_date if isinstance(material_date, date) else None,
                        bool(committed),
                        False,
                    )
                )
                remaining -= assigned
                supplies[supply_index][0] = available - assigned
                if supplies[supply_index][0] == ZERO:
                    supply_index += 1
            if remaining > ZERO:
                result.append(
                    _SupplySegment(
                        bucket_index,
                        due,
                        remaining,
                        None,
                        False,
                        True,
                    )
                )
        return tuple(result)

    def _recovery_segments(
        self,
        requirement: _SourceRequirement,
        offers: Sequence[_Offer],
    ) -> tuple[_SupplySegment, ...]:
        return tuple(
            segment
            for segment in self._supply_segments(requirement)
            if segment.committed
            and segment.material_available is not None
            and segment.material_available > segment.due_date
            and any(
                segment.due_date in offer.allowed_buckets
                and offer.material_available < segment.material_available
                for offer in offers
            )
        )

    def _recovery_demand(
        self,
        requirement: _SourceRequirement,
        offers: Sequence[_Offer],
    ) -> Decimal:
        return sum(
            (item.quantity for item in self._recovery_segments(requirement, offers)),
            ZERO,
        )

    def _deadline_lateness(
        self,
        requirement: _SourceRequirement,
        plan: CandidatePlan | None,
    ) -> tuple[DeadlineLateness, ...]:
        supplies: list[list[object]] = []
        if requirement.on_hand > ZERO:
            supplies.append([requirement.on_hand, None])
        supplies.extend(
            [quantity, delivery]
            for _number, quantity, delivery in requirement.inbound
        )
        if plan is not None:
            supplies.extend(
                [line.quantity, line.material_available_date]
                for line in plan.lines
            )
        supplies.sort(
            key=lambda item: date.min if item[1] is None else item[1]
        )
        supply_index = 0
        result: list[DeadlineLateness] = []
        for due, quantity in requirement.bucket_quantities:
            remaining = quantity
            late = ZERO
            unit_late_days = ZERO
            while remaining > ZERO and supply_index < len(supplies):
                available, material_date = supplies[supply_index]
                assert isinstance(available, Decimal)
                assigned = min(remaining, available)
                if isinstance(material_date, date) and material_date > due:
                    late += assigned
                    unit_late_days += assigned * Decimal(
                        (material_date - due).days
                    )
                remaining -= assigned
                supplies[supply_index][0] = available - assigned
                if supplies[supply_index][0] == ZERO:
                    supply_index += 1
            if remaining > ZERO:
                late += remaining
            if late > ZERO:
                result.append(
                    DeadlineLateness(due, late, unit_late_days, remaining)
                )
        return tuple(result)

    def _required_certifications(self, component: Component, current: date) -> tuple[str, ...]:
        required = {
            part.strip()
            for value in component.required_certifications
            for part in _CERT_SPLIT_RE.split(value)
            if part.strip()
        }
        certification_rules = self._rules(current, "required_certification")
        iso_applies = (
            self._concept("electronic_component", component) is EvidenceStatus.PASS
            or self._concept("printed_circuit_board", component) is EvidenceStatus.PASS
            or self._concept("safety_critical_part", component) is EvidenceStatus.PASS
        )
        power_applies = self._concept("power_supply_component", component) is EvidenceStatus.PASS
        for rule in certification_rules:
            value = str(rule.data["constraint"]["certification"])
            canonical = _certification(value)
            if canonical == "ISO9001" and iso_applies:
                required.add(value)
            elif canonical != "ISO9001" and power_applies:
                required.add(value)
        return tuple(sorted(required))

    def _relationship_predates(self, supplier: Supplier, effective: date) -> bool:
        years = tuple(int(item) for item in _YEAR_RE.findall(supplier.notes or ""))
        return bool(years) and min(years) < effective.year

    def _selector_status(self, rule: PolicyRule, component: Component) -> EvidenceStatus:
        selector = rule.data.get("selector")
        if not isinstance(selector, Mapping) or selector.get("entity") != "component":
            return EvidenceStatus.PASS
        tags = tuple(str(item) for item in selector.get("semantic_tags", ()))
        if not tags:
            return EvidenceStatus.PASS
        statuses = tuple(self._concept(tag, component) for tag in tags)
        operator = str(selector.get("operator", "all"))
        if operator == "any":
            if EvidenceStatus.PASS in statuses:
                return EvidenceStatus.PASS
            return EvidenceStatus.UNKNOWN if EvidenceStatus.UNKNOWN in statuses else EvidenceStatus.FAIL
        if operator == "none":
            if EvidenceStatus.PASS in statuses:
                return EvidenceStatus.FAIL
            return EvidenceStatus.UNKNOWN if EvidenceStatus.UNKNOWN in statuses else EvidenceStatus.PASS
        if EvidenceStatus.FAIL in statuses:
            return EvidenceStatus.FAIL
        return EvidenceStatus.UNKNOWN if EvidenceStatus.UNKNOWN in statuses else EvidenceStatus.PASS

    def _rolling_review_keys(
        self,
        snapshot: ScenarioSnapshot,
        component: Component,
        contract: EvidenceContract,
    ) -> frozenset[str]:
        if contract is not EvidenceContract.BENCHMARK:
            return frozenset()
        applicable = {
            rule.rule_id: rule
            for rule in self.registry.active_rules(snapshot.configuration.current_date)
            if rule.evidence_basis == EvidenceBasis.ROLLING_WINDOW.value
            and self._selector_status(rule, component) is not EvidenceStatus.FAIL
        }
        # A narrower rule erases its broader predecessor only when membership
        # is proven.  UNKNOWN membership retains both robust branches.
        for rule in tuple(applicable.values()):
            if self._selector_status(rule, component) is not EvidenceStatus.PASS:
                continue
            precedence = rule.data.get("precedence")
            if not isinstance(precedence, Mapping):
                continue
            for relation in ("supersedes", "outranks"):
                for target in precedence.get(relation, ()):
                    applicable.pop(str(target), None)
        keys = {"ROLLING_HISTORY_UNKNOWN"} if applicable else set()
        if any(
            self._selector_status(rule, component) is EvidenceStatus.UNKNOWN
            for rule in applicable.values()
        ):
            keys.update(("INFERRED_CONCEPT_MEMBERSHIP", "ROBUST_BOTH_WAYS"))
        return frozenset(keys)

    def _review_keys(
        self,
        snapshot: ScenarioSnapshot,
        requirement: _SourceRequirement,
        supplier: Supplier,
        contract: EvidenceContract,
    ) -> frozenset[str]:
        """Reconstruct normalized stage-4 assumption keys from source facts."""

        keys = set(
            self._rolling_review_keys(snapshot, requirement.component, contract)
        )
        pcb_rules = self._rules(snapshot.configuration.current_date, "incumbent_supplier_only")
        if (
            contract is EvidenceContract.BENCHMARK
            and pcb_rules
            and self._concept("printed_circuit_board_component", requirement.component)
            is EvidenceStatus.PASS
            and (requirement.component.component_id, supplier.supplier_id)
            not in self.accepted_shipment_pairs
        ):
            prior = any(
                item.component_id == requirement.component.component_id
                and item.supplier_id == supplier.supplier_id
                for item in snapshot.purchase_orders
            )
            if prior or self._relationship_predates(supplier, pcb_rules[0].effective_from):
                keys.add("PCB_INCUMBENCY_INFERRED")
        rating = _rating(supplier.sustainability_rating)
        if rating is not None and rating < _rating("B"):
            named = self._named_primary(snapshot, requirement.component)
            if named is not None and named.resolved and named.supplier_id == supplier.supplier_id:
                keys.add("BELOW_B_REVIEW_DISCHARGED_BY_MEMO")
        return frozenset(keys)

    def _hard_eligible(
        self,
        snapshot: ScenarioSnapshot,
        requirement: _SourceRequirement,
        supplier: Supplier,
        catalog: SupplierCatalogLine,
        contract: EvidenceContract,
        *,
        allow_contract_blocked: bool = False,
    ) -> tuple[bool, bool]:
        if supplier.on_approved_list is not True:
            return False, False
        if any(not _holds(supplier, required) for required in self._required_certifications(requirement.component, snapshot.configuration.current_date)):
            return False, False
        assumption = False
        # Both critical and non-critical components have a rolling-volume rule
        # under the base policy.  The benchmark contract licenses the absent
        # history as a rule-level assumption; production does not.
        rolling_rules = tuple(
            rule
            for rule in self.registry.active_rules(snapshot.configuration.current_date)
            if rule.evidence_basis == EvidenceBasis.ROLLING_WINDOW.value
        )
        if rolling_rules:
            if contract is EvidenceContract.PRODUCTION:
                if not allow_contract_blocked:
                    return False, False
            else:
                assumption = True
        pcb_rules = self._rules(snapshot.configuration.current_date, "incumbent_supplier_only")
        if pcb_rules and self._concept("printed_circuit_board_component", requirement.component) is EvidenceStatus.PASS:
            rule = pcb_rules[0]
            pair = (requirement.component.component_id, supplier.supplier_id)
            if pair not in self.accepted_shipment_pairs:
                if contract is EvidenceContract.PRODUCTION:
                    if not allow_contract_blocked:
                        return False, False
                else:
                    prior = any(
                        item.component_id == requirement.component.component_id
                        and item.supplier_id == supplier.supplier_id
                        for item in snapshot.purchase_orders
                    )
                    if not prior and not self._relationship_predates(supplier, rule.effective_from):
                        return False, False
                    assumption = True
        rating = _rating(supplier.sustainability_rating)
        if rating is None:
            return False, assumption
        if rating < _rating("B"):
            named = self._named_primary(snapshot, requirement.component)
            if named is None or named.supplier_id != supplier.supplier_id:
                return False, assumption
            assumption = True
        return True, assumption

    def _named_primary(self, snapshot: ScenarioSnapshot, component: Component) -> NamedEntityCheck | None:
        rules = tuple(
            rule
            for rule in self._directive_rules(
                snapshot.configuration.current_date, "named_primary_supplier"
            )
            if self._selector_status(rule, component) is EvidenceStatus.PASS
        )
        if not rules:
            return None
        reference = rules[0].data["directive"]["supplier"]
        return self.resolve_source_named_entity(reference, snapshot.suppliers)

    def _release_subject(self, snapshot: ScenarioSnapshot, component: Component) -> NamedEntityCheck | None:
        rules = tuple(
            rule
            for rule in self._directive_rules(
                snapshot.configuration.current_date, "named_primary_supplier"
            )
            if self._selector_status(rule, component) is EvidenceStatus.PASS
        )
        if not rules:
            return None
        release = rules[0].data.get("release_condition")
        if not isinstance(release, Mapping) or not isinstance(release.get("subject"), Mapping):
            return None
        return self.resolve_source_named_entity(release["subject"], snapshot.suppliers)

    def _shaping_degradation_required(
        self, snapshot: ScenarioSnapshot, component: Component
    ) -> bool:
        """Independently reconstruct unresolved conditional shaping scope."""

        for rule in self._directive_rules(
            snapshot.configuration.current_date, "named_primary_supplier"
        ):
            if (
                RuleSeverity(rule.severity) is not RuleSeverity.SHAPING
                or self._selector_status(rule, component) is EvidenceStatus.FAIL
            ):
                continue
            references: list[Mapping[str, object]] = []
            directive = rule.data.get("directive")
            if isinstance(directive, Mapping) and isinstance(
                directive.get("supplier"), Mapping
            ):
                references.append(directive["supplier"])
            release = rule.data.get("release_condition")
            if isinstance(release, Mapping) and isinstance(
                release.get("subject"), Mapping
            ):
                references.append(release["subject"])
            if any(
                not self.resolve_source_named_entity(
                    reference, snapshot.suppliers
                ).resolved
                for reference in references
            ):
                return True
        return False

    def _minimum_secondary_parameter(
        self, snapshot: ScenarioSnapshot, component: Component
    ) -> SecondaryAllocationParameters | None:
        status = self._concept("neodymium_magnet", component)
        semantic_status = {
            "neodymium_magnet": (
                True
                if status is EvidenceStatus.PASS
                else False
                if status is EvidenceStatus.FAIL
                else None
            )
        }
        matches = self._parameters(
            snapshot.configuration.current_date
        ).matching_secondary_allocations(semantic_status)
        if len(matches) > 1:
            raise ValueError("multiple minimum-secondary parameters match one component")
        return matches[0] if matches else None

    def _minimum_secondary(self, snapshot: ScenarioSnapshot, component: Component) -> Decimal | None:
        parameter = self._minimum_secondary_parameter(snapshot, component)
        return parameter.minimum_fraction if parameter is not None else None

    def _minimum_secondary_rule_id(
        self, snapshot: ScenarioSnapshot, component: Component
    ) -> str | None:
        parameter = self._minimum_secondary_parameter(snapshot, component)
        return parameter.rule_id if parameter is not None else None

    def _increment(self, component: Component) -> Decimal:
        unit = " ".join(_tokens(component.unit_of_measure))
        return Decimal("0.01") if unit in {"kg", "meter"} else Decimal("1")

    def _enumeration_increment(
        self,
        requirement: _SourceRequirement,
        offers: Sequence[_Offer],
        secondary: Decimal | None,
    ) -> Decimal:
        """Return an exact source-derived lattice for independent line search.

        Catalog quantities remain constrained by the unit-of-measure increment,
        but iterating every hundredth of a kilogram when every source boundary
        is a multiple of five kilograms is needless and can exhaust the
        validator before it proves anything.  Linear objectives attain their
        optima at source or rational-policy boundaries, so the search lattice
        is the GCD of those boundaries divided by every active ratio
        denominator.  If that division is not integral we conservatively fall
        back to the catalog increment.
        """

        base = self._increment(requirement.component)
        quantities = [
            requirement.total_demand,
            requirement.on_hand,
            requirement.eventual_gap,
            *(quantity for _due, quantity in requirement.bucket_quantities),
            *(item.catalog.minimum_order_quantity for item in offers),
            *(item.upper_bound for item in offers),
        ]
        units: list[int] = []
        for quantity in quantities:
            scaled = quantity / base
            if scaled == scaled.to_integral_value() and scaled > ZERO:
                units.append(int(scaled))
        boundary_gcd = 0
        for value in units:
            boundary_gcd = gcd(boundary_gcd, value)
        if boundary_gcd <= 1:
            return base
        denominator = Fraction(self.autonomy.max_surplus_fraction).denominator
        if secondary is not None:
            secondary_denominator = Fraction(secondary).denominator
            denominator = (
                denominator
                * secondary_denominator
                // gcd(denominator, secondary_denominator)
            )
        if boundary_gcd % denominator:
            return base
        return base * Decimal(boundary_gcd // denominator)

    def _allocation_floor(
        self,
        catalogs: Sequence[SupplierCatalogLine],
        secondary: Decimal | None,
    ) -> Decimal:
        if secondary is None or not catalogs:
            return ZERO
        by_supplier: dict[str, Decimal] = {}
        for item in catalogs:
            by_supplier[item.supplier_id] = max(
                by_supplier.get(item.supplier_id, ZERO),
                item.minimum_order_quantity,
            )
        allocation_floor = sum(by_supplier.values(), ZERO)
        if by_supplier:
            primary_share = Decimal("1") - secondary
            allocation_floor = max(
                allocation_floor,
                (max(by_supplier.values()) / primary_share).to_integral_value(
                    rounding=ROUND_CEILING
                ),
            )
        return allocation_floor

    def _allocation_surplus_bound(
        self, requirement: _SourceRequirement, catalogs: Sequence[SupplierCatalogLine], secondary: Decimal | None
    ) -> Decimal:
        return max(
            ZERO,
            self._allocation_floor(catalogs, secondary) - requirement.total_demand,
        )

    def _forced_allocation_surplus(
        self,
        requirement: _SourceRequirement,
        catalogs: Sequence[SupplierCatalogLine],
        secondary: Decimal | None,
    ) -> Decimal:
        return max(
            ZERO,
            self._allocation_floor(catalogs, secondary) - requirement.eventual_gap,
        )

    def derive_upper_bounds(
        self,
        snapshot: ScenarioSnapshot,
        requirement: _SourceRequirement,
        catalogs: Sequence[SupplierCatalogLine],
    ) -> dict[str, Decimal]:
        """Independently derive the §8.2 Big-M values (equality is legal)."""

        secondary = self._minimum_secondary(snapshot, requirement.component)
        allocation = self._allocation_surplus_bound(requirement, catalogs, secondary)
        # The route-aware planner may buy a bridge against committed late
        # supply.  Use the independently reconstructed physical late segments
        # as the safe pre-offer bound; solve construction below applies the
        # stricter route-specific recovery authorization.
        physical_recovery_bound = sum(
            (
                item.quantity
                for item in self._supply_segments(requirement)
                if item.committed
                and item.material_available is not None
                and item.material_available > item.due_date
            ),
            ZERO,
        )
        base = requirement.total_demand + physical_recovery_bound + allocation
        return {
            item.supplier_id: max(base, item.minimum_order_quantity)
            for item in catalogs
        }

    def _domestic_gate(
        self,
        snapshot: ScenarioSnapshot,
        requirement: _SourceRequirement,
        due: date,
        eligible: Sequence[tuple[Supplier, SupplierCatalogLine]],
    ) -> str | None:
        domestic = tuple(
            item for item in eligible if self._concept("domestic_supplier", item[0]) is EvidenceStatus.PASS
        )
        international = tuple(
            item for item in eligible if self._concept("international_supplier", item[0]) is EvidenceStatus.PASS
        )
        if not international:
            return None
        if not domestic:
            return "c"
        if not any(
            snapshot.configuration.current_date + timedelta(days=item[1].lead_time_days) <= due
            for item in domestic
        ):
            return "a"
        best_domestic = min(item[1].unit_price for item in domestic)
        best_international = min(item[1].unit_price for item in international)
        critical = self._concept("critical_component", requirement.component)
        critical_status = (
            True
            if critical is EvidenceStatus.PASS
            else False
            if critical is EvidenceStatus.FAIL
            else None
        )
        threshold = self._parameters(
            snapshot.configuration.current_date
        ).domestic_premiums.for_critical_status(
            critical_status
        ).maximum_premium_fraction
        premium = Decimal("Infinity") if best_international == ZERO and best_domestic > ZERO else (
            ZERO if best_international == ZERO else (best_domestic - best_international) / best_international
        )
        return "b" if premium > threshold else None

    def _offers(
        self,
        snapshot: ScenarioSnapshot,
        requirement: _SourceRequirement,
        contract: EvidenceContract,
        *,
        include_unapproved: bool = False,
        include_evidence_blocked: bool = False,
    ) -> tuple[_Offer, ...]:
        suppliers = {item.supplier_id: item for item in snapshot.suppliers}
        catalogs = tuple(item for item in snapshot.catalog_lines if item.component_id == requirement.component.component_id)
        quarantined_suppliers = _quarantined_supplier_ids(snapshot)
        quarantined_catalogs = _quarantined_catalog_keys(snapshot)
        eligible: list[tuple[Supplier, SupplierCatalogLine, bool]] = []
        for catalog in catalogs:
            if (
                catalog.supplier_id in quarantined_suppliers
                or (catalog.supplier_id, catalog.component_id)
                in quarantined_catalogs
            ):
                continue
            supplier = suppliers.get(catalog.supplier_id)
            if supplier is None:
                # Snapshot quarantine validation reports an unaccounted
                # missing supplier.  It must never become a source offer.
                continue
            allowed, assumption = self._hard_eligible(
                snapshot,
                requirement,
                supplier,
                catalog,
                contract,
                allow_contract_blocked=include_evidence_blocked,
            )
            if allowed:
                eligible.append((supplier, catalog, assumption))
        gate = {
            due: self._domestic_gate(snapshot, requirement, due, tuple((supplier, catalog) for supplier, catalog, _assumption in eligible))
            for due, _quantity in requirement.bucket_quantities
        }
        upper = self.derive_upper_bounds(
            snapshot,
            requirement,
            tuple(catalog for _supplier, catalog, _assumption in eligible),
        )
        forced_allocation_surplus = self._forced_allocation_surplus(
            requirement,
            tuple(catalog for _supplier, catalog, _assumption in eligible),
            self._minimum_secondary(snapshot, requirement.component),
        )
        critical = self._concept("critical_component", requirement.component)
        critical_status = (
            True
            if critical is EvidenceStatus.PASS
            else False
            if critical is EvidenceStatus.FAIL
            else None
        )
        premium_rule_id = self._parameters(
            snapshot.configuration.current_date
        ).domestic_premiums.for_critical_status(critical_status).rule_id
        result: list[_Offer] = []
        for supplier, catalog, _assumption in eligible:
            international = self._concept("international_supplier", supplier) is EvidenceStatus.PASS
            expected = snapshot.configuration.current_date + timedelta(days=catalog.lead_time_days)
            pcb = self._concept("printed_circuit_board_component", requirement.component) is EvidenceStatus.PASS
            buffer = self.receiving_buffer_days if requirement.component.is_hazardous or pcb else 0
            material = expected + timedelta(days=buffer)
            review_keys = self._review_keys(
                snapshot, requirement, supplier, contract
            )
            supplier_hash = _supplier_fingerprint(supplier)
            route_hash = _route_fingerprint(
                requirement.component, catalog, "standard", catalog.lead_time_days
            )
            gate_conditions = tuple(
                (due, gate[due] or "shut")
                for due, _quantity in requirement.bucket_quantities
            )
            if international:
                standard_groups: dict[str, list[date]] = defaultdict(list)
                for due, _quantity in requirement.bucket_quantities:
                    condition = gate[due]
                    if condition is not None:
                        standard_groups[condition].append(due)
            else:
                # Ordinary domestic routes may be allocated late and priced
                # by unit-late-days, so their bucket scope is not restricted
                # to physically on-time dates.
                standard_groups = {
                    "domestic": [
                        due for due, _quantity in requirement.bucket_quantities
                    ]
                }
            for _condition, scoped_buckets in sorted(standard_groups.items()):
                allowed_buckets = tuple(scoped_buckets)
                feasible_deadlines = tuple(
                    due for due in allowed_buckets if material <= due
                )
                exception_codes = (
                    (f"{premium_rule_id}:condition_{_condition}",)
                    if international
                    else ()
                )
                exception_scope_deadlines = (
                    allowed_buckets if international else ()
                )
                bound = upper[catalog.supplier_id]
                if international:
                    allowance = sum(
                        (
                            requirement.bucket_shortage(due)
                            for due in allowed_buckets
                        ),
                        ZERO,
                    )
                    if _condition == "b":
                        allowance += forced_allocation_surplus
                    bound = min(bound, allowance)
                if bound < catalog.minimum_order_quantity:
                    continue
                result.append(
                    _Offer(
                        requirement.component,
                        supplier,
                        catalog,
                        expected,
                        material,
                        catalog.lead_time_days,
                        "standard",
                        international,
                        review_keys,
                        allowed_buckets,
                        gate_conditions,
                        bound,
                        supplier_hash,
                        route_hash,
                        _independent_route_id(
                            supplier_hash,
                            route_hash,
                            exception_codes,
                            feasible_deadlines,
                            exception_scope_deadlines,
                        ),
                    )
                )
            air_rules = self._rules(snapshot.configuration.current_date, "air_freight_authorization")
            if not international or not air_rules:
                continue
            air_rule = air_rules[0]
            reduction = int(air_rule.data["constraint"]["lead_time_reduction_days"])
            minimum_lead = int(air_rule.data["constraint"]["minimum_lead_time_days"])
            air_lead = max(minimum_lead, catalog.lead_time_days - reduction)
            if air_lead >= catalog.lead_time_days:
                continue
            approval_rules = (
                *self._rules(snapshot.configuration.current_date, "air_freight_individual_approval"),
                *self._rules(snapshot.configuration.current_date, "air_freight_period_spend_cap"),
            )
            approvals_satisfied = all(item.rule_id in self.approved_rule_ids for item in approval_rules)
            if not include_unapproved and not approvals_satisfied:
                continue
            air_expected = snapshot.configuration.current_date + timedelta(days=air_lead)
            air_material = air_expected + timedelta(days=buffer)
            air_route_hash = _route_fingerprint(
                requirement.component, catalog, "air freight", air_lead
            )
            air_groups: dict[str, list[date]] = defaultdict(list)
            for due, _quantity in requirement.bucket_quantities:
                condition = gate[due]
                if (
                    condition is not None
                    and material > due
                    and air_material <= due
                ):
                    air_groups[condition].append(due)
            for _condition, scoped_buckets in sorted(air_groups.items()):
                air_allowed = tuple(scoped_buckets)
                air_bound = min(
                    upper[catalog.supplier_id],
                    sum(
                        (
                            requirement.bucket_shortage(due)
                            for due in air_allowed
                        ),
                        ZERO,
                    ),
                )
                if air_bound < catalog.minimum_order_quantity:
                    continue
                result.append(
                    _Offer(
                        requirement.component,
                        supplier,
                        catalog,
                        air_expected,
                        air_material,
                        air_lead,
                        "air freight",
                        international,
                        review_keys,
                        air_allowed,
                        gate_conditions,
                        air_bound,
                        supplier_hash,
                        air_route_hash,
                        _independent_route_id(
                            supplier_hash,
                            air_route_hash,
                            (
                                f"{premium_rule_id}:condition_{_condition}",
                                air_rule.rule_id,
                            ),
                            air_allowed,
                            air_allowed,
                        ),
                    )
                )
        return tuple(
            sorted(
                result,
                key=lambda item: (
                    item.supplier_fingerprint,
                    item.route_fingerprint,
                ),
            )
        )

    # ---- exact plan arithmetic and objective reconstruction ------------

    def _match_offer(self, offers: Sequence[_Offer], line: PlanLine) -> _Offer | None:
        matches = tuple(
            item
            for item in offers
            if item.supplier.supplier_id == line.supplier_id
            and item.catalog.component_id == line.component_id
            and item.expected_delivery == line.expected_delivery_date
            and item.material_available == line.material_available_date
            and all(
                allocation.due_date in item.allowed_buckets
                for allocation in line.bucket_allocations
            )
        )
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _semantic_tie_key(
        offers: Sequence[_Offer], allocation: Mapping[int, Decimal]
    ) -> tuple[tuple[str, str, Decimal], ...]:
        return tuple(
            sorted(
                (
                    offers[index].supplier_fingerprint,
                    offers[index].route_fingerprint,
                    quantity,
                )
                for index, quantity in allocation.items()
                if quantity > ZERO
            )
        )

    def _named_deviation(
        self, snapshot: ScenarioSnapshot, component: Component, quantities: Mapping[str, Decimal]
    ) -> Decimal:
        named = self._named_primary(snapshot, component)
        if named is None or not named.resolved or named.supplier_id in self.capacity_confirmed_supplier_ids:
            return ZERO
        named_quantity = quantities.get(named.supplier_id or "", ZERO)
        return max((max(ZERO, value - named_quantity) for key, value in quantities.items() if key != named.supplier_id), default=ZERO)

    def _plan_recovery_capacity(
        self,
        requirement: _SourceRequirement,
        plan: CandidatePlan,
        offers: Sequence[_Offer],
    ) -> Decimal:
        """Bound classified recovery from the plan's actual route allocations."""

        matched = {
            line.route_id: self._match_offer(offers, line)
            for line in plan.lines
        }
        source_segments = self._supply_segments(requirement)
        total_capacity = ZERO
        recovered = ZERO
        for bucket_index, (due, _quantity) in enumerate(
            requirement.bucket_quantities
        ):
            route_capacity: list[list[object]] = []
            for line in plan.lines:
                offer = matched[line.route_id]
                if offer is None:
                    continue
                allocated = sum(
                    (
                        allocation.quantity
                        for allocation in line.bucket_allocations
                        if allocation.due_date == due
                    ),
                    ZERO,
                )
                if allocated > ZERO:
                    route_capacity.append(
                        [offer.material_available, offer.route_fingerprint, allocated]
                    )
            total_capacity += sum(
                (item[2] for item in route_capacity if isinstance(item[2], Decimal)),
                ZERO,
            )
            late_segments = sorted(
                (
                    segment
                    for segment in source_segments
                    if segment.bucket_index == bucket_index
                    and segment.committed
                    and segment.material_available is not None
                    and segment.material_available > due
                ),
                key=lambda item: item.material_available or date.max,
            )
            for segment in late_segments:
                remaining = segment.quantity
                assert segment.material_available is not None
                for route in sorted(
                    route_capacity,
                    key=lambda item: (item[0], item[1]),
                    reverse=True,
                ):
                    material_available, _fingerprint, available = route
                    assert isinstance(material_available, date)
                    assert isinstance(available, Decimal)
                    if material_available >= segment.material_available:
                        continue
                    assigned = min(remaining, available)
                    remaining -= assigned
                    route[2] = available - assigned
                    recovered += assigned
                    if remaining == ZERO:
                        break
        recovery_budget = max(
            ZERO,
            total_capacity - plan.eventual_covered_quantity,
        )
        return min(recovered, recovery_budget)

    def _objective(
        self,
        snapshot: ScenarioSnapshot,
        requirement: _SourceRequirement,
        plan: CandidatePlan,
        offers: Sequence[_Offer],
    ) -> tuple[Decimal, ...]:
        parameters = self._parameters(snapshot.configuration.current_date)
        strategic_savings = (
            parameters.strategic_continuity.maximum_alternative_savings_fraction
        )
        sustainability = parameters.sustainability
        matched = {line.route_id: self._match_offer(offers, line) for line in plan.lines}
        total_quantity = sum((line.quantity for line in plan.lines), ZERO)
        physical_gaps: list[Decimal] = []
        for due, _quantity in requirement.bucket_quantities:
            planned = sum(
                (
                    allocation.quantity
                    for line in plan.lines
                    if line.material_available_date <= due
                    for allocation in line.bucket_allocations
                    if allocation.due_date <= due
                ),
                ZERO,
            )
            physical_gaps.append(max(ZERO, requirement.cumulative_demand(due) - requirement.on_time_supply(due) - planned))
        unit_late = sum(
            (
                item.unit_late_days
                for item in self._deadline_lateness(requirement, plan)
            ),
            ZERO,
        )
        # Stage 4 counts the normalized union of assumption codes.  Multiple
        # policy branches can depend on the same unknown fact, so counting
        # evidence rows or per-route totals would double-charge it.
        review = Decimal(
            len(
                {
                    key
                    for offer in matched.values()
                    if offer is not None
                    for key in offer.review_keys
                }
            )
        )
        quantities: dict[str, Decimal] = defaultdict(Decimal)
        for line in plan.lines:
            quantities[line.supplier_id] += line.quantity
        named_deviation = self._named_deviation(snapshot, requirement.component, quantities)
        international_penalty = ZERO
        strategic_penalty = ZERO
        sustainability_penalty = ZERO
        suppliers = {item.supplier_id: item for item in snapshot.suppliers}
        for line in plan.lines:
            offer = matched[line.route_id]
            if offer is None:
                continue
            current_rating = _rating(offer.supplier.sustainability_rating)
            for allocation in line.bucket_allocations:
                due = allocation.due_date
                if offer.international and offer.condition_for(due) != "b":
                    international_penalty += allocation.quantity
                if self._concept("strategic_supplier", offer.supplier) is EvidenceStatus.FAIL:
                    strategic = tuple(
                        item for item in offers
                        if due in item.allowed_buckets
                        and self._concept("strategic_supplier", item.supplier) is EvidenceStatus.PASS
                    )
                    if strategic:
                        best = min(strategic, key=lambda item: item.catalog.unit_price)
                        savings = ZERO if best.catalog.unit_price == ZERO else (
                            best.catalog.unit_price - offer.catalog.unit_price
                        ) / best.catalog.unit_price
                        if savings <= strategic_savings:
                            strategic_penalty += allocation.quantity
                if current_rating is not None:
                    comparable = tuple(
                        item
                        for item in offers
                        if due in item.allowed_buckets
                        and _rating(item.supplier.sustainability_rating) is not None
                        and _rating(item.supplier.sustainability_rating) > current_rating
                        and (
                            item.catalog.unit_price == offer.catalog.unit_price
                            if min(item.catalog.unit_price, offer.catalog.unit_price) == ZERO
                            else abs(item.catalog.unit_price - offer.catalog.unit_price)
                            / min(item.catalog.unit_price, offer.catalog.unit_price)
                            <= sustainability.comparable_price_fraction
                        )
                        and _business_days(item.material_available, offer.material_available)
                        <= sustainability.comparable_delivery_days
                    )
                    if comparable:
                        best_rating = max(_rating(item.supplier.sustainability_rating) for item in comparable)
                        assert best_rating is not None
                        sustainability_penalty += (best_rating - current_rating) * allocation.quantity
        weighted_lead = sum(
            (line.quantity * Decimal((line.expected_delivery_date - line.order_date).days) for line in plan.lines), ZERO
        )
        return tuple(physical_gaps) + (
            unit_late,
            plan.discretionary_surplus,
            review,
            named_deviation,
            international_penalty,
            strategic_penalty,
            sustainability_penalty,
            plan.total_cost,
            max(
                ZERO,
                total_quantity
                - requirement.eventual_gap
                - plan.recovery_quantity,
            ),
            weighted_lead,
            Decimal(len(plan.lines)),
        )

    def _structured_route_stage_outcomes(
        self,
        snapshot: ScenarioSnapshot,
        requirement: _SourceRequirement,
        selected: _Offer,
        rejected: _Offer,
        due: date,
        offers: Sequence[_Offer],
    ) -> tuple[tuple[int, str], ...]:
        """Independently reproduce the strict route comparator chain."""

        selected_on_time = selected.material_available <= due
        rejected_on_time = rejected.material_available <= due
        outcomes: list[tuple[int, str]] = [
            (
                1,
                "selected_on_time"
                if selected_on_time and not rejected_on_time
                else "rejected_on_time_advantage"
                if rejected_on_time and not selected_on_time
                else f"equal_on_time={str(selected_on_time).lower()}",
            )
        ]
        conditions = {
            item
            for item in (selected.condition_for(due), rejected.condition_for(due))
            if item is not None
        }
        if "b" in conditions:
            domestic = "skipped_condition_b"
        elif conditions & {"a", "c"}:
            domestic = "moot_condition_a_or_c"
        elif selected.international == rejected.international:
            domestic = "moot_same_domesticity"
        elif not selected.international and rejected.international:
            domestic = "selected_domestic_preference_applied"
        else:
            domestic = "rejected_domestic_preference_advantage"
        outcomes.append((2, domestic))

        parameters = self._parameters(snapshot.configuration.current_date)

        def strategic_penalty(offer: _Offer) -> bool:
            if self._concept("strategic_supplier", offer.supplier) is not EvidenceStatus.FAIL:
                return False
            strategic = tuple(
                item
                for item in offers
                if due in item.allowed_buckets
                and self._concept("strategic_supplier", item.supplier)
                is EvidenceStatus.PASS
            )
            if not strategic:
                return False
            best = min(strategic, key=lambda item: item.catalog.unit_price)
            savings = (
                ZERO
                if best.catalog.unit_price == ZERO
                else (best.catalog.unit_price - offer.catalog.unit_price)
                / best.catalog.unit_price
            )
            return (
                savings
                <= parameters.strategic_continuity.maximum_alternative_savings_fraction
            )

        def sustainability_penalty(offer: _Offer) -> bool:
            rating = _rating(offer.supplier.sustainability_rating)
            if rating is None:
                return False
            for alternative in offers:
                other = _rating(alternative.supplier.sustainability_rating)
                if due not in alternative.allowed_buckets or other is None or other <= rating:
                    continue
                low = min(offer.catalog.unit_price, alternative.catalog.unit_price)
                price_comparable = (
                    offer.catalog.unit_price == alternative.catalog.unit_price
                    if low == ZERO
                    else abs(offer.catalog.unit_price - alternative.catalog.unit_price)
                    / low
                    <= parameters.sustainability.comparable_price_fraction
                )
                delivery_comparable = (
                    _business_days(
                        offer.material_available,
                        alternative.material_available,
                    )
                    <= parameters.sustainability.comparable_delivery_days
                )
                if price_comparable and delivery_comparable:
                    return True
            return False

        for stage, selected_label, penalty in (
            (3, "selected_strategic_retention", strategic_penalty),
            (4, "selected_sustainability_preference", sustainability_penalty),
        ):
            selected_penalty = penalty(selected)
            rejected_penalty = penalty(rejected)
            outcomes.append(
                (
                    stage,
                    selected_label
                    if not selected_penalty and rejected_penalty
                    else "rejected_policy_preference_advantage"
                    if selected_penalty and not rejected_penalty
                    else "outside_window_or_equal",
                )
            )
        outcomes.extend(
            (
                (
                    5,
                    "selected_lower_known_cost"
                    if selected.catalog.unit_price < rejected.catalog.unit_price
                    else "rejected_lower_known_cost"
                    if rejected.catalog.unit_price < selected.catalog.unit_price
                    else "equal_known_cost",
                ),
                (
                    6,
                    "selected_shorter_lead_time"
                    if selected.lead_days < rejected.lead_days
                    else "rejected_shorter_lead_time"
                    if rejected.lead_days < selected.lead_days
                    else "equal_lead_time",
                ),
                (
                    7,
                    "selected_lower_id_free_fingerprint"
                    if (selected.supplier_fingerprint, selected.route_fingerprint)
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
        decisive = next(
            (
                stage
                for stage, outcome in outcomes
                if outcome in selected_outcomes | rejected_outcomes
            ),
            None,
        )
        if decisive is None:
            return tuple(outcomes)
        return tuple(
            (
                stage,
                outcome
                if stage <= decisive
                else f"not_evaluated_after_stage_{decisive}",
            )
            for stage, outcome in outcomes
        )

    def _independent_material_blockers(
        self,
        snapshot: ScenarioSnapshot,
        requirement: _SourceRequirement,
        offer: _Offer,
        contract: EvidenceContract,
    ) -> tuple[EvidenceStatus, tuple[str, ...]]:
        """Reconstruct hard route status and citations from source facts."""

        current = snapshot.configuration.current_date
        supplier = offer.supplier
        component = requirement.component
        failed: set[str] = set()
        unknown: set[str] = set()

        if supplier.on_approved_list is not True:
            failed.update(
                rule.rule_id
                for rule in self._rules(current, "approved_supplier_required")
            )

        certification_rules = self._rules(current, "required_certification")
        electronic = _tokens(component.category or "") == (
            "electronic",
            "component",
        )
        iso_required = (
            electronic
            or self._concept("printed_circuit_board", component)
            is EvidenceStatus.PASS
            or self._concept("safety_critical_part", component)
            is EvidenceStatus.PASS
        )
        for rule in certification_rules:
            required = str(rule.data["constraint"]["certification"])
            canonical = _certification(required)
            applies = (
                canonical == "ISO9001" and iso_required
            ) or (
                canonical != "ISO9001"
                and self._concept("power_supply_component", component)
                is EvidenceStatus.PASS
            )
            if applies and not _holds(supplier, required):
                failed.add(rule.rule_id)
        for value in component.required_certifications:
            for required in _CERT_SPLIT_RE.split(value):
                required = required.strip()
                if required and not _holds(supplier, required):
                    failed.add(
                        "MASTER-DATA.required_certification."
                        + _certification(required)
                    )

        domestic = self._concept("domestic_supplier", supplier)
        if domestic is EvidenceStatus.UNKNOWN:
            unknown.add("DATA-QUALITY.supplier_domesticity")

        pcb_rules = self._rules(current, "incumbent_supplier_only")
        if (
            pcb_rules
            and self._concept("printed_circuit_board_component", component)
            is EvidenceStatus.PASS
            and (component.component_id, supplier.supplier_id)
            not in self.accepted_shipment_pairs
        ):
            if contract is EvidenceContract.PRODUCTION:
                unknown.add(pcb_rules[0].rule_id)
            else:
                prior = any(
                    item.component_id == component.component_id
                    and item.supplier_id == supplier.supplier_id
                    for item in snapshot.purchase_orders
                )
                relationship = self._relationship_predates(
                    supplier, pcb_rules[0].effective_from
                )
                if not prior and not relationship:
                    failed.add(pcb_rules[0].rule_id)

        if offer.international and all(
            condition == "shut" for _due, condition in offer.gate_conditions
        ):
            failed.update(
                rule.rule_id
                for rule in self._rules(
                    current, "international_sourcing_justification"
                )
            )

        rating = _rating(supplier.sustainability_rating)
        below_rules = self._rules(current, "below_rating_review")
        if below_rules:
            boundary = _rating(
                str(below_rules[0].data["constraint"]["below_rating"])
            )
            if rating is None or boundary is None:
                unknown.add(below_rules[0].rule_id)
            elif rating < boundary:
                suppliers = {
                    item.supplier_id: item for item in snapshot.suppliers
                }
                b_or_better = False
                for other in snapshot.catalog_lines:
                    candidate = suppliers.get(other.supplier_id)
                    other_rating = (
                        _rating(candidate.sustainability_rating)
                        if candidate is not None
                        else None
                    )
                    if (
                        other.component_id == component.component_id
                        and other.supplier_id != supplier.supplier_id
                        and candidate is not None
                        and other_rating is not None
                        and other_rating >= boundary
                        and self._hard_eligible(
                            snapshot,
                            requirement,
                            candidate,
                            other,
                            contract,
                        )[0]
                    ):
                        b_or_better = True
                        break
                if b_or_better:
                    failed.add(below_rules[0].rule_id)
                else:
                    unknown.add(below_rules[0].rule_id)

        if failed:
            return EvidenceStatus.FAIL, tuple(sorted(failed | unknown))
        if unknown:
            return EvidenceStatus.UNKNOWN, tuple(sorted(unknown))
        return EvidenceStatus.PASS, ()

    def _check_structured_rationale_facts(
        self,
        snapshot: ScenarioSnapshot,
        decision: DecisionRecord,
        requirement: _SourceRequirement,
        offers: Sequence[_Offer],
        sink: _IssueSink,
    ) -> None:
        """Verify explanation facts directly from source and certified plan facts."""

        selected_plan = decision.selected_plan
        if selected_plan is None:
            if decision.comparator_facts or decision.material_rejections:
                sink.error(
                    "RATIONALE_FACTS_UNSCOPED",
                    "A decision without an executable plan carries selection rationale facts.",
                    component_id=decision.component_id,
                )
            return
        quantity = tuple(
            item
            for item in decision.comparator_facts
            if item.kind == "quantity_calibration"
        )
        selected_total = sum((line.quantity for line in selected_plan.lines), ZERO)
        minimum = selected_plan.minimum_compliant_total
        expected_quantity_rules = tuple(
            sorted(
                {
                    rule.rule_id
                    for kind in ("on_time_arrival", "total_cost_of_ownership")
                    for rule in self._rules(snapshot.configuration.current_date, kind)
                }
                | {
                    exception.split(":condition_", 1)[0]
                    for line in selected_plan.lines
                    for allocation in line.bucket_allocations
                    for exception in allocation.exception_ids
                }
            )
        )
        expected_quantity_cost = (
            selected_plan.total_cost - selected_plan.cheapest_covering_cost
            if selected_plan.cheapest_covering_cost is not None
            else None
        )
        expected_quantity_outcome = (
            f"selected total {selected_total} against certified minimum {minimum}; "
            f"forced surplus {selected_plan.forced_surplus}; discretionary surplus "
            f"{selected_plan.discretionary_surplus}"
        )
        if (
            len(quantity) != 1
            or minimum is None
            or quantity[0].stage != 0
            or not quantity[0].decisive
            or quantity[0].comparator != "certified_quantity_calibration"
            or quantity[0].outcome != expected_quantity_outcome
            or quantity[0].selected_route_ids
            != tuple(line.route_id for line in selected_plan.lines)
            or quantity[0].compared_route_ids
            or quantity[0].rule_ids != expected_quantity_rules
            or quantity[0].quantity_delta != selected_total - minimum
            or quantity[0].cost_delta != expected_quantity_cost
            or quantity[0].delivery_delta_days is not None
            or quantity[0].policy_window
            != self._parameters(
                snapshot.configuration.current_date
            ).economic_autonomy.disclosure()
        ):
            sink.error(
                "RATIONALE_QUANTITY_CALIBRATION_MISMATCH",
                "Structured quantity calibration does not match the certified source/plan facts.",
                component_id=decision.component_id,
            )

        route_facts = tuple(
            item
            for item in decision.comparator_facts
            if item.kind == "route_selection"
        )
        if not route_facts and not decision.material_rejections:
            return

        selected_offers = {
            line.route_id: offer
            for line in selected_plan.lines
            if (offer := self._match_offer(offers, line)) is not None
        }
        if len(selected_offers) != len(selected_plan.lines):
            sink.error(
                "RATIONALE_SELECTED_ROUTE_MISMATCH",
                "A selected route cannot be reconstructed exactly from source facts.",
                component_id=decision.component_id,
            )
            return

        selected_suppliers = {line.supplier_id for line in selected_plan.lines}
        suppliers = {item.supplier_id: item for item in snapshot.suppliers}
        quarantined_suppliers = _quarantined_supplier_ids(snapshot)
        quarantined_catalogs = _quarantined_catalog_keys(snapshot)
        relevant_catalogs = tuple(
            catalog
            for catalog in snapshot.catalog_lines
            if catalog.component_id == decision.component_id
            and catalog.supplier_id in suppliers
            and catalog.supplier_id not in quarantined_suppliers
            and (catalog.supplier_id, catalog.component_id)
            not in quarantined_catalogs
        )
        expected_rejected_suppliers = {
            catalog.supplier_id
            for catalog in relevant_catalogs
            if catalog.supplier_id not in selected_suppliers
        }
        actual_rejected_suppliers = {
            item.supplier_id for item in decision.material_rejections
        }
        if actual_rejected_suppliers != expected_rejected_suppliers:
            sink.error(
                "RATIONALE_MATERIAL_REJECTION_MISSING",
                "Material rejection coverage must include every unselected catalog supplier, including hard-gated routes.",
                component_id=decision.component_id,
            )

        gate_conditions = offers[0].gate_conditions
        buffer_days = (
            self.receiving_buffer_days
            if requirement.component.is_hazardous
            or self._concept(
                "printed_circuit_board_component", requirement.component
            )
            is EvidenceStatus.PASS
            else 0
        )
        rationale_offers = list(offers)
        offer_status = {item.route_id: EvidenceStatus.PASS for item in offers}
        offer_rules: dict[str, tuple[str, ...]] = {
            item.route_id: () for item in offers
        }
        for catalog in relevant_catalogs:
            supplier = suppliers[catalog.supplier_id]
            if any(
                item.catalog == catalog
                and item.supplier.supplier_id == supplier.supplier_id
                and item.shipping_method == "standard"
                for item in rationale_offers
            ):
                continue
            supplier_hash = _supplier_fingerprint(supplier)
            route_hash = _route_fingerprint(
                requirement.component,
                catalog,
                "standard",
                catalog.lead_time_days,
            )
            expected = snapshot.configuration.current_date + timedelta(
                days=catalog.lead_time_days
            )
            material = expected + timedelta(days=buffer_days)
            international = (
                self._concept("international_supplier", supplier)
                is EvidenceStatus.PASS
            )
            all_deadlines = tuple(
                due for due, _quantity in requirement.bucket_quantities
            )
            route_specs: list[
                tuple[tuple[str, ...], tuple[date, ...], tuple[date, ...]]
            ] = []
            if international:
                by_condition: dict[str, list[date]] = defaultdict(list)
                for due, condition in gate_conditions:
                    if condition != "shut":
                        by_condition[condition].append(due)
                if by_condition:
                    critical = self._concept(
                        "critical_component", requirement.component
                    )
                    critical_status = (
                        True
                        if critical is EvidenceStatus.PASS
                        else False
                        if critical is EvidenceStatus.FAIL
                        else None
                    )
                    premium_rule_id = self._parameters(
                        snapshot.configuration.current_date
                    ).domestic_premiums.for_critical_status(
                        critical_status
                    ).rule_id
                    for condition, scoped in sorted(by_condition.items()):
                        scope = tuple(scoped)
                        route_specs.append(
                            (
                                (f"{premium_rule_id}:condition_{condition}",),
                                tuple(due for due in scope if material <= due),
                                scope,
                            )
                        )
                else:
                    route_specs.append(((), (), ()))
            else:
                route_specs.append(
                    ((), tuple(due for due in all_deadlines if material <= due), ())
                )
            for exceptions, feasible, exception_scope in route_specs:
                route_id = _independent_route_id(
                    supplier_hash,
                    route_hash,
                    exceptions,
                    feasible,
                    exception_scope,
                )
                offer = _Offer(
                    requirement.component,
                    supplier,
                    catalog,
                    expected,
                    material,
                    catalog.lead_time_days,
                    "standard",
                    international,
                    frozenset(),
                    exception_scope or all_deadlines,
                    gate_conditions,
                    ZERO,
                    supplier_hash,
                    route_hash,
                    route_id,
                )
                status, rules = self._independent_material_blockers(
                    snapshot,
                    requirement,
                    offer,
                    decision.evidence_contract,
                )
                rationale_offers.append(offer)
                offer_status[route_id] = status
                offer_rules[route_id] = rules

        current = snapshot.configuration.current_date
        parameters = self._parameters(current)
        expected_comparators = {
            1: "on_time_feasibility",
            2: "domestic_preference",
            3: "strategic_retention",
            4: "sustainability_band",
            5: "known_landed_cost",
            6: "shorter_lead_time",
            7: "id_free_fingerprint",
        }
        expected_rules = {
            1: tuple(rule.rule_id for rule in self._rules(current, "on_time_arrival")),
            2: tuple(
                rule.rule_id
                for rule in self._rules(current, "domestic_supplier_preference")
            ),
            3: tuple(
                rule.rule_id
                for rule in self._rules(current, "strategic_supplier_continuity")
            ),
            4: tuple(
                rule.rule_id
                for rule in self._rules(current, "sustainability_preference")
            ),
            5: tuple(
                rule.rule_id
                for rule in self._rules(current, "total_cost_of_ownership")
            ),
            6: tuple(rule.rule_id for rule in self._rules(current, "on_time_arrival")),
            7: (),
        }
        expected_windows = {
            1: "Material availability was compared with each scoped demand deadline.",
            2: (
                "The domestic comparator is skipped only for §3(b), moot for "
                "§3(a)/(c), and not reached when the gate is shut."
            ),
            3: (
                "A non-Strategic route is penalized only where its savings do not "
                f"strictly exceed {parameters.strategic_continuity.maximum_alternative_savings_fraction}."
            ),
            4: (
                "The rating preference applies only inside the inclusive "
                f"{parameters.sustainability.comparable_price_fraction}-price and "
                f"{parameters.sustainability.comparable_delivery_days}-business-day window."
            ),
            5: (
                "Known catalog price is used because no additional landed-cost fact "
                "is represented."
            ),
            6: "Shorter supplier lead time wins after the policy comparators.",
            7: "The final deterministic key excludes supplier and component database IDs.",
        }
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
        expected_route_facts: list[DecisionComparatorFact] = []
        expected_material: list[MaterialRouteRejection] = []
        by_supplier: dict[str, list[_Offer]] = defaultdict(list)
        for offer in rationale_offers:
            if offer.supplier.supplier_id in expected_rejected_suppliers:
                by_supplier[offer.supplier.supplier_id].append(offer)

        air_approval_rule_ids = {
            rule.rule_id
            for kind in (
                "air_freight_individual_approval",
                "air_freight_period_spend_cap",
            )
            for rule in self._rules(current, kind)
        }
        below_b_rule_ids = {
            rule.rule_id for rule in self._rules(current, "below_rating_review")
        }

        def approval_required(offer: _Offer) -> bool:
            if (
                offer.shipping_method == "air freight"
                and not air_approval_rule_ids.issubset(self.approved_rule_ids)
            ):
                return True
            return (
                offer_status.get(offer.route_id) is EvidenceStatus.UNKNOWN
                and bool(set(offer_rules.get(offer.route_id, ())) & below_b_rule_ids)
            )

        for supplier_id in sorted(expected_rejected_suppliers):
            rejected_offer = min(
                by_supplier[supplier_id],
                key=lambda item: (
                    offer_status.get(item.route_id, EvidenceStatus.UNKNOWN)
                    is not EvidenceStatus.PASS,
                    approval_required(item),
                    item.catalog.unit_price,
                    item.material_available,
                    item.route_id,
                ),
            )
            selected_offer = min(
                selected_offers.values(),
                key=lambda item: (
                    abs(item.catalog.unit_price - rejected_offer.catalog.unit_price),
                    abs((item.material_available - rejected_offer.material_available).days),
                    item.route_id,
                ),
            )
            selected_line = next(
                line
                for line in selected_plan.lines
                if line.route_id == selected_offer.route_id
            )
            due = min(
                allocation.due_date
                for allocation in selected_line.bucket_allocations
            )
            price_delta = (
                rejected_offer.catalog.unit_price
                - selected_offer.catalog.unit_price
            )
            delivery_delta = (
                rejected_offer.material_available
                - selected_offer.material_available
            ).days
            status = offer_status.get(
                rejected_offer.route_id, EvidenceStatus.UNKNOWN
            )
            blocker_rules = set(offer_rules.get(rejected_offer.route_id, ()))
            route_path: list[DecisionComparatorFact] = []
            first_difference: tuple[int, str] | None = None
            if status is EvidenceStatus.PASS:
                outcomes = self._structured_route_stage_outcomes(
                    snapshot,
                    requirement,
                    selected_offer,
                    rejected_offer,
                    due,
                    offers,
                )
                first_difference = next(
                    (
                        (stage, outcome)
                        for stage, outcome in outcomes
                        if outcome in selected_outcomes | rejected_outcomes
                    ),
                    None,
                )
                if first_difference is not None and first_difference[1] in selected_outcomes:
                    deciding_stage = first_difference[0]
                    for stage, outcome in outcomes:
                        fact = DecisionComparatorFact(
                            stage=stage,
                            kind="route_selection",
                            comparator=expected_comparators[stage],
                            outcome=outcome,
                            selected_route_ids=(selected_offer.route_id,),
                            compared_route_ids=(rejected_offer.route_id,),
                            rule_ids=tuple(sorted(expected_rules[stage])),
                            decisive=stage == deciding_stage,
                            cost_delta=(
                                price_delta if stage in {3, 4, 5} else None
                            ),
                            delivery_delta_days=(
                                delivery_delta if stage in {1, 4, 6} else None
                            ),
                            policy_window=expected_windows[stage],
                        )
                        route_path.append(fact)
                        expected_route_facts.append(fact)
                    for fact in route_path:
                        if fact.decisive:
                            blocker_rules.update(fact.rule_ids)

            if status is EvidenceStatus.FAIL:
                reason_code = "POLICY_GATE_FAILED"
            elif status is EvidenceStatus.UNKNOWN:
                reason_code = "POLICY_GATE_UNRESOLVED"
            elif not any(
                rejected_offer.material_available <= scoped_due
                for scoped_due in rejected_offer.allowed_buckets
            ):
                reason_code = "NO_FEASIBLE_DEADLINE"
            elif route_path:
                reason_code = "NOT_SELECTED_BY_COMPARATOR"
            else:
                reason_code = "NOT_SELECTED_BY_CERTIFIED_ALLOCATION"
                blocker_rules.update(expected_quantity_rules)

            expected_material.append(
                MaterialRouteRejection(
                    route_id=rejected_offer.route_id,
                    supplier_id=supplier_id,
                    reason_code=reason_code,
                    eligibility=status,
                    rule_ids=tuple(sorted(blocker_rules)),
                    unit_price=rejected_offer.catalog.unit_price,
                    selected_unit_price=selected_offer.catalog.unit_price,
                    price_delta=price_delta,
                    material_available_date=rejected_offer.material_available,
                    selected_material_available_date=selected_offer.material_available,
                    delivery_delta_days=delivery_delta,
                )
            )

        canonical_fact_key = lambda item: (
            item.stage,
            item.comparator,
            item.compared_route_ids,
        )
        actual_route_facts = tuple(
            sorted(
                (
                    item
                    for item in decision.comparator_facts
                    if item.kind == "route_selection"
                ),
                key=canonical_fact_key,
            )
        )
        expected_route_tuple = tuple(
            sorted(expected_route_facts, key=canonical_fact_key)
        )
        if actual_route_facts != expected_route_tuple:
            sink.error(
                "RATIONALE_COMPARATOR_MISMATCH",
                "Route comparator facts must exactly match independently reconstructed routes, stages, outcomes, citations, deltas, and windows.",
                component_id=decision.component_id,
            )
        if decision.material_rejections != tuple(expected_material):
            sink.error(
                "RATIONALE_MATERIAL_REJECTION_MISMATCH",
                "Material rejection route, reason, eligibility, citations, price, or date facts do not match independent reconstruction.",
                component_id=decision.component_id,
            )

    def _synthetic_objective(
        self,
        snapshot: ScenarioSnapshot,
        requirement: _SourceRequirement,
        offers: Sequence[_Offer],
        allocation: Mapping[int, Decimal],
        phased_allocation: Mapping[tuple[int, int], Decimal],
        q_min: Decimal,
    ) -> tuple[Decimal, ...]:
        parameters = self._parameters(snapshot.configuration.current_date)
        strategic_savings = (
            parameters.strategic_continuity.maximum_alternative_savings_fraction
        )
        sustainability = parameters.sustainability
        physical_gaps = tuple(
            max(
                ZERO,
                requirement.cumulative_demand(due)
                - requirement.on_time_supply(due)
                - sum(
                    (
                        quantity
                        for (offer_index, bucket_index), quantity
                        in phased_allocation.items()
                        if offers[offer_index].material_available <= due
                        and requirement.bucket_quantities[bucket_index][0] <= due
                    ),
                    ZERO,
                ),
            )
            for due, _bucket in requirement.bucket_quantities
        )
        # Synthetic phased search is retained as the tiny differential oracle.
        # It receives the same existing-commitment lateness offset as the
        # independently built MILP; recovery deltas are handled in that MILP.
        unit_late = sum(
            (
                segment.quantity
                * Decimal((segment.material_available - segment.due_date).days)
                for segment in self._supply_segments(requirement)
                if segment.committed
                and segment.material_available is not None
                and segment.material_available > segment.due_date
            ),
            ZERO,
        )
        international = ZERO
        strategic = ZERO
        sustainable = ZERO
        cost = ZERO
        lead = ZERO
        review_keys: set[str] = set()
        quantities: dict[str, Decimal] = defaultdict(Decimal)
        for index, quantity in allocation.items():
            if quantity == ZERO:
                continue
            offer = offers[index]
            quantities[offer.supplier.supplier_id] += quantity
            cost += quantity * offer.catalog.unit_price
            lead += quantity * Decimal(offer.lead_days)
            review_keys.update(offer.review_keys)
        for (index, bucket_index), quantity in phased_allocation.items():
            if quantity == ZERO:
                continue
            offer = offers[index]
            due = requirement.bucket_quantities[bucket_index][0]
            unit_late += quantity * Decimal(
                max(0, (offer.material_available - due).days)
            )
            if offer.international and offer.condition_for(due) != "b":
                international += quantity
            if self._concept("strategic_supplier", offer.supplier) is EvidenceStatus.FAIL:
                strategic_options = tuple(
                    item
                    for item in offers
                    if due in item.allowed_buckets
                    and self._concept("strategic_supplier", item.supplier)
                    is EvidenceStatus.PASS
                )
                if strategic_options:
                    best = min(
                        strategic_options,
                        key=lambda item: item.catalog.unit_price,
                    )
                    savings = (
                        ZERO
                        if best.catalog.unit_price == ZERO
                        else (
                            best.catalog.unit_price - offer.catalog.unit_price
                        )
                        / best.catalog.unit_price
                    )
                    if savings <= strategic_savings:
                        strategic += quantity
            current_rating = _rating(offer.supplier.sustainability_rating)
            if current_rating is not None:
                better = tuple(
                    item
                    for item in offers
                    if due in item.allowed_buckets
                    and _rating(item.supplier.sustainability_rating) is not None
                    and _rating(item.supplier.sustainability_rating)
                    > current_rating
                    and (
                        item.catalog.unit_price == offer.catalog.unit_price
                        if min(
                            item.catalog.unit_price, offer.catalog.unit_price
                        )
                        == ZERO
                        else abs(
                            item.catalog.unit_price
                            - offer.catalog.unit_price
                        )
                        / min(
                            item.catalog.unit_price, offer.catalog.unit_price
                        )
                        <= sustainability.comparable_price_fraction
                    )
                    and _business_days(
                        item.material_available, offer.material_available
                    )
                    <= sustainability.comparable_delivery_days
                )
                if better:
                    best_rating = max(
                        _rating(item.supplier.sustainability_rating)
                        for item in better
                    )
                    assert best_rating is not None
                    sustainable += (best_rating - current_rating) * quantity
        total = sum(allocation.values(), ZERO)
        named = self._named_deviation(snapshot, requirement.component, quantities)
        return physical_gaps + (
            unit_late,
            max(ZERO, total - q_min),
            Decimal(len(review_keys)),
            named,
            international,
            strategic,
            sustainable,
            cost,
            max(
                ZERO,
                total
                - requirement.eventual_gap
                - self._recovery_demand(requirement, offers),
            ),
            lead,
            Decimal(sum(1 for value in allocation.values() if value > ZERO)),
        )

    def _best_phased_objective(
        self,
        snapshot: ScenarioSnapshot,
        requirement: _SourceRequirement,
        offers: Sequence[_Offer],
        allocation: Mapping[int, Decimal],
        q_min: Decimal,
        nodes: list[int],
    ) -> tuple[Decimal, ...] | None:
        """Enumerate route-to-deadline allocation independently of line totals.

        The planner's ``x`` line quantities do not prove its ``z`` bucket
        assignment.  This pass distributes every selected route quantity over
        only its policy-allowed deadlines and compares the complete staged
        vector.  A constant-coefficient fast path closes the common all-on-time
        case without walking equivalent compositions.
        """

        parameters = self._parameters(snapshot.configuration.current_date)
        strategic_savings = (
            parameters.strategic_continuity.maximum_alternative_savings_fraction
        )
        sustainability = parameters.sustainability
        selected = tuple(
            index for index, quantity in allocation.items() if quantity > ZERO
        )
        due_to_index = {
            due: index
            for index, (due, _quantity) in enumerate(
                requirement.bucket_quantities
            )
        }
        allowed: dict[int, tuple[int, ...]] = {
            index: tuple(
                due_to_index[due]
                for due in offers[index].allowed_buckets
                if due in due_to_index
            )
            for index in selected
        }
        if any(not indexes for indexes in allowed.values()):
            return None

        values = [
            *allocation.values(),
            requirement.on_hand,
            *(quantity for _due, quantity in requirement.bucket_quantities),
            *(quantity for _number, quantity, _due in requirement.inbound),
        ]
        places = max(
            0,
            max(
                (-value.normalize().as_tuple().exponent for value in values),
                default=0,
            ),
        )
        atom = Decimal(1).scaleb(-places)

        # If every selected route is on time for every bucket it may serve and
        # carries no bucket-varying policy coefficient, assigning it to the
        # earliest allowed bucket simultaneously maximizes every cumulative
        # coverage stage.  Later stages are then invariant to the split.
        def bucket_metrics(index: int, bucket: int) -> tuple[Decimal, ...]:
            offer = offers[index]
            due = requirement.bucket_quantities[bucket][0]
            strategic_penalty = ZERO
            if self._concept("strategic_supplier", offer.supplier) is EvidenceStatus.FAIL:
                strategic_options = tuple(
                    item
                    for item in offers
                    if due in item.allowed_buckets
                    and self._concept("strategic_supplier", item.supplier)
                    is EvidenceStatus.PASS
                )
                if strategic_options:
                    best = min(
                        strategic_options,
                        key=lambda item: item.catalog.unit_price,
                    )
                    savings = (
                        ZERO
                        if best.catalog.unit_price == ZERO
                        else (
                            best.catalog.unit_price - offer.catalog.unit_price
                        )
                        / best.catalog.unit_price
                    )
                    if savings <= strategic_savings:
                        strategic_penalty = Decimal("1")
            sustainability_penalty = ZERO
            rating = _rating(offer.supplier.sustainability_rating)
            if rating is not None:
                comparable = tuple(
                    item
                    for item in offers
                    if due in item.allowed_buckets
                    and _rating(item.supplier.sustainability_rating) is not None
                    and _rating(item.supplier.sustainability_rating) > rating
                    and (
                        item.catalog.unit_price == offer.catalog.unit_price
                        if min(
                            item.catalog.unit_price, offer.catalog.unit_price
                        )
                        == ZERO
                        else abs(
                            item.catalog.unit_price
                            - offer.catalog.unit_price
                        )
                        / min(
                            item.catalog.unit_price, offer.catalog.unit_price
                        )
                        <= sustainability.comparable_price_fraction
                    )
                    and _business_days(
                        item.material_available, offer.material_available
                    )
                    <= sustainability.comparable_delivery_days
                )
                if comparable:
                    best_rating = max(
                        _rating(item.supplier.sustainability_rating)
                        for item in comparable
                    )
                    assert best_rating is not None
                    sustainability_penalty = best_rating - rating
            return (
                Decimal(max(0, (offer.material_available - due).days)),
                Decimal(
                    offer.international and offer.condition_for(due) != "b"
                ),
                strategic_penalty,
                sustainability_penalty,
            )

        if len(selected) == 1:
            index = selected[0]
            offer = offers[index]
            total = allocation[index]
            phased: dict[tuple[int, int], Decimal] = {}
            assigned = ZERO
            for bucket_index, (due, _quantity) in enumerate(
                requirement.bucket_quantities
            ):
                if offer.material_available > due:
                    continue
                eligible = tuple(
                    candidate
                    for candidate in allowed[index]
                    if candidate <= bucket_index
                )
                if not eligible:
                    continue
                required = min(
                    total,
                    max(
                        ZERO,
                        requirement.cumulative_demand(due)
                        - requirement.on_time_supply(due),
                    ),
                )
                increment_required = max(ZERO, required - assigned)
                if increment_required == ZERO:
                    continue
                target = min(
                    eligible,
                    key=lambda candidate: (bucket_metrics(index, candidate), candidate),
                )
                phased[(index, target)] = (
                    phased.get((index, target), ZERO) + increment_required
                )
                assigned += increment_required
            remainder = total - assigned
            if remainder > ZERO:
                target = min(
                    allowed[index],
                    key=lambda candidate: (bucket_metrics(index, candidate), candidate),
                )
                phased[(index, target)] = phased.get((index, target), ZERO) + remainder
            return self._synthetic_objective(
                snapshot,
                requirement,
                offers,
                allocation,
                phased,
                q_min,
            )

        if all(len(indexes) == 1 for indexes in allowed.values()):
            phased = {
                (index, allowed[index][0]): quantity
                for index, quantity in allocation.items()
                if quantity > ZERO
            }
            return self._synthetic_objective(
                snapshot,
                requirement,
                offers,
                allocation,
                phased,
                q_min,
            )

        constant = True
        for index in selected:
            metrics = {
                bucket_metrics(index, bucket)
                for bucket in allowed[index]
            }
            if len(metrics) != 1 or next(iter(metrics))[0] != ZERO:
                constant = False
                break
        if constant:
            phased = {
                (index, min(allowed[index])): quantity
                for index, quantity in allocation.items()
                if quantity > ZERO
            }
            return self._synthetic_objective(
                snapshot,
                requirement,
                offers,
                allocation,
                phased,
                q_min,
            )

        best: tuple[Decimal, ...] | None = None
        phased_units: dict[tuple[int, int], int] = {}

        def distributions(
            total: int,
            buckets: tuple[int, ...],
            position: int = 0,
        ) -> Iterable[tuple[int, ...]]:
            nodes[0] += 1
            if nodes[0] > self.enumeration_node_limit:
                return
            if position == len(buckets) - 1:
                yield (total,)
                return
            for quantity in range(total + 1):
                for remainder in distributions(
                    total - quantity, buckets, position + 1
                ):
                    yield (quantity, *remainder)

        def visit(position: int) -> None:
            nonlocal best
            if nodes[0] > self.enumeration_node_limit:
                return
            if position == len(selected):
                phased = {
                    key: Decimal(quantity) * atom
                    for key, quantity in phased_units.items()
                    if quantity
                }
                objective = self._synthetic_objective(
                    snapshot,
                    requirement,
                    offers,
                    allocation,
                    phased,
                    q_min,
                )
                if best is None or objective < best:
                    best = objective
                return
            index = selected[position]
            scaled = allocation[index] / atom
            if scaled != scaled.to_integral_value():
                nodes[0] = self.enumeration_node_limit + 1
                return
            buckets = allowed[index]
            for distribution in distributions(int(scaled), buckets):
                for bucket, quantity in zip(buckets, distribution, strict=True):
                    phased_units[(index, bucket)] = quantity
                visit(position + 1)
                for bucket in buckets:
                    phased_units.pop((index, bucket), None)
                if nodes[0] > self.enumeration_node_limit:
                    return

        visit(0)
        return best

    # ---- separate integer enumeration --------------------------------

    def _vectors_for_total(
        self,
        total_units: int,
        offers: Sequence[_Offer],
        increment: Decimal,
        secondary: Decimal | None,
        named_supplier: str | None,
        nodes: list[int],
    ) -> Iterable[dict[int, Decimal]]:
        count = len(offers)
        if count == 0:
            return
        upper = [
            _floor_units(item.upper_bound, increment)
            for item in offers
        ]
        lower = [_ceil_units(item.catalog.minimum_order_quantity, increment) for item in offers]
        for size in range(1, count + 1):
            if secondary is not None and size < 2:
                continue
            for subset in combinations(range(count), size):
                if named_supplier is not None and not any(offers[index].supplier.supplier_id == named_supplier for index in subset):
                    continue
                if sum(lower[index] for index in subset) > total_units or sum(upper[index] for index in subset) < total_units:
                    continue
                chosen: dict[int, int] = {}

                def walk(position: int, remaining: int) -> Iterable[dict[int, Decimal]]:
                    nodes[0] += 1
                    if nodes[0] > self.enumeration_node_limit:
                        return
                    if position == len(subset) - 1:
                        index = subset[position]
                        if lower[index] <= remaining <= upper[index]:
                            chosen[index] = remaining
                            by_supplier: dict[str, int] = defaultdict(int)
                            for key, value in chosen.items():
                                by_supplier[offers[key].supplier.supplier_id] += value
                            secondary_ok = secondary is None or all(
                                Decimal(value) <= (Decimal("1") - secondary) * Decimal(total_units)
                                for value in by_supplier.values()
                            )
                            named_ok = named_supplier is None or (
                                named_supplier in by_supplier
                                and by_supplier[named_supplier]
                                >= max((value for key, value in by_supplier.items() if key != named_supplier), default=0)
                            )
                            if secondary_ok and named_ok:
                                yield {key: Decimal(value) * increment for key, value in chosen.items()}
                            chosen.pop(index, None)
                        return
                    index = subset[position]
                    future = subset[position + 1 :]
                    minimum_future = sum(lower[item] for item in future)
                    maximum_future = sum(upper[item] for item in future)
                    start = max(lower[index], remaining - maximum_future)
                    stop = min(upper[index], remaining - minimum_future)
                    for value in range(start, stop + 1):
                        chosen[index] = value
                        yield from walk(position + 1, remaining - value)
                    chosen.pop(index, None)

                yield from walk(0, total_units)

    def _milp_optimize(
        self,
        model: _IndependentMilpModel,
        objective: Mapping[int, Fraction],
    ) -> _IndependentMilpResult:
        forced = self.solver_limits.force_status
        if forced is not None:
            return _IndependentMilpResult(forced, None, False)
        try:
            import numpy as np
            from scipy.optimize import Bounds, LinearConstraint, milp
            from scipy.sparse import coo_array
        except ImportError:
            return _IndependentMilpResult(SolverStatus.ERROR, None, False)

        row_indexes: list[int] = []
        column_indexes: list[int] = []
        data: list[float] = []
        lower_rows: list[float] = []
        upper_rows: list[float] = []
        for row_index, row in enumerate(model.rows):
            denominator = 1
            for coefficient in row.coefficients.values():
                denominator = lcm(denominator, coefficient.denominator)
            if row.lower is not None:
                denominator = lcm(denominator, row.lower.denominator)
            if row.upper is not None:
                denominator = lcm(denominator, row.upper.denominator)
            for column, coefficient in row.coefficients.items():
                row_indexes.append(row_index)
                column_indexes.append(column)
                data.append(float(coefficient * denominator))
            lower_rows.append(
                -np.inf
                if row.lower is None
                else float(row.lower * denominator)
            )
            upper_rows.append(
                np.inf
                if row.upper is None
                else float(row.upper * denominator)
            )
        matrix = coo_array(
            (data, (row_indexes, column_indexes)),
            shape=(len(model.rows), len(model.names)),
        ).tocsr()

        objective_denominator = 1
        for coefficient in objective.values():
            objective_denominator = lcm(
                objective_denominator, coefficient.denominator
            )
        integer_objective = np.zeros(len(model.names), dtype=float)
        for index, coefficient in objective.items():
            integer_objective[index] = float(
                coefficient * objective_denominator
            )
        options: dict[str, float] = {"mip_rel_gap": 0.0}
        if self.solver_limits.time_limit_seconds is not None:
            options["time_limit"] = float(
                self.solver_limits.time_limit_seconds
            )
        result = milp(
            c=integer_objective,
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
        values: tuple[int, ...] | None = None
        if result.x is not None:
            rounded = tuple(int(round(float(value))) for value in result.x)
            if all(
                abs(float(value) - rounded[index]) <= 1e-6
                for index, value in enumerate(result.x)
            ):
                values = rounded
        gap_value = getattr(result, "mip_gap", None)
        gap = None if gap_value is None else Decimal(str(gap_value))
        if result.status == 0 and (gap is None or gap == ZERO):
            status = SolverStatus.OPTIMAL
            complete = True
        elif result.status == 2:
            status = SolverStatus.INFEASIBLE
            complete = True
        elif result.status == 1:
            status = (
                SolverStatus.FEASIBLE_INCUMBENT
                if values is not None
                else SolverStatus.RESOURCE_LIMIT
            )
            complete = False
        elif values is not None:
            status = SolverStatus.FEASIBLE_INCUMBENT
            complete = False
        else:
            status = SolverStatus.ERROR
            complete = False
        if status is SolverStatus.OPTIMAL and values is None:
            return _IndependentMilpResult(SolverStatus.ERROR, None, False)
        if values is not None:
            for index, value in enumerate(values):
                if not model.lower[index] <= value <= model.upper[index]:
                    return _IndependentMilpResult(
                        SolverStatus.ERROR, None, False
                    )
            for row in model.rows:
                activity = sum(
                    coefficient * values[index]
                    for index, coefficient in row.coefficients.items()
                )
                if (
                    row.lower is not None
                    and activity < row.lower
                ) or (
                    row.upper is not None
                    and activity > row.upper
                ):
                    return _IndependentMilpResult(
                        SolverStatus.ERROR, None, False
                    )
        return _IndependentMilpResult(status, values, complete)

    def _milp_quantity_atom(
        self,
        requirement: _SourceRequirement,
        offers: Sequence[_Offer],
    ) -> Decimal:
        values = [
            self._increment(requirement.component),
            requirement.total_demand,
            requirement.on_hand,
            requirement.eventual_gap,
            *(quantity for _due, quantity in requirement.bucket_quantities),
            *(quantity for _number, quantity, _due in requirement.inbound),
            *(offer.catalog.minimum_order_quantity for offer in offers),
            *(offer.upper_bound for offer in offers),
        ]
        places = max(
            0,
            max(
                (-value.normalize().as_tuple().exponent for value in values),
                default=0,
            ),
        )
        return Decimal(1).scaleb(-places)

    def _milp_minimize_and_pin(
        self,
        model: _IndependentMilpModel,
        objective: Mapping[int, Fraction | int],
    ) -> tuple[_IndependentMilpResult, Fraction | None]:
        exact_objective = {
            index: value if isinstance(value, Fraction) else Fraction(value)
            for index, value in objective.items()
            if value
        }
        if not exact_objective:
            return (
                _IndependentMilpResult(SolverStatus.OPTIMAL, None, True),
                Fraction(),
            )
        result = self._milp_optimize(model, exact_objective)
        if (
            result.status is not SolverStatus.OPTIMAL
            or not result.certificate_complete
            or result.values is None
        ):
            return result, None
        value = sum(
            coefficient * result.values[index]
            for index, coefficient in exact_objective.items()
        )
        model.add_row(exact_objective, lower=value, upper=value)
        return result, value

    def _independently_solve_milp(
        self,
        snapshot: ScenarioSnapshot,
        requirement: _SourceRequirement,
        offers: Sequence[_Offer],
        solve_kind: SolveKind,
        *,
        named_supplier: str | None,
        coverage_target: Decimal | None = None,
        q_min: Decimal | None = None,
        cheapest_covering_cost: Decimal | None = None,
    ) -> IndependentSolve:
        parameters = self._parameters(snapshot.configuration.current_date)
        strategic_savings = (
            parameters.strategic_continuity.maximum_alternative_savings_fraction
        )
        sustainability = parameters.sustainability
        atom = self._milp_quantity_atom(requirement, offers)
        increment_atoms = _ceil_units(
            self._increment(requirement.component), atom
        )
        model = _IndependentMilpModel()
        source_segments = self._supply_segments(requirement)
        uncovered_by_bucket = tuple(
            sum(
                (
                    segment.quantity
                    for segment in source_segments
                    if segment.bucket_index == bucket_index
                    and segment.uncovered
                ),
                ZERO,
            )
            for bucket_index in range(len(requirement.bucket_quantities))
        )
        recovery_segments = self._recovery_segments(requirement, offers)
        recovery_demand = sum(
            (segment.quantity for segment in recovery_segments),
            ZERO,
        )
        upper_atoms = tuple(
            _floor_units(offer.upper_bound, atom) for offer in offers
        )
        x: list[int] = []
        y: list[int] = []
        z: list[tuple[int, ...]] = []
        baseline_allocations: list[tuple[int, ...]] = []
        recovery_allocations: list[tuple[int, ...]] = []
        for offer_index, (offer, upper) in enumerate(
            zip(offers, upper_atoms, strict=True)
        ):
            x_var = model.add_variable(f"x[{offer_index}]", 0, upper)
            step_var = model.add_variable(
                f"step[{offer_index}]", 0, upper // increment_atoms
            )
            y_var = model.add_variable(f"y[{offer_index}]", 0, 1)
            z_vars = tuple(
                model.add_variable(
                    f"z[{offer_index},{bucket_index}]",
                    0,
                    upper
                    if due in offer.allowed_buckets
                    else 0,
                )
                for bucket_index, (due, _quantity) in enumerate(
                    requirement.bucket_quantities
                )
            )
            baseline_vars = tuple(
                model.add_variable(
                    f"baseline[{offer_index},{bucket_index}]",
                    0,
                    min(
                        upper,
                        _ceil_units(uncovered_by_bucket[bucket_index], atom),
                    ),
                )
                for bucket_index in range(len(requirement.bucket_quantities))
            )
            recovery_vars = tuple(
                model.add_variable(
                    f"recovery[{offer_index},{segment_index}]",
                    0,
                    (
                        min(upper, _ceil_units(segment.quantity, atom))
                        if segment.due_date in offer.allowed_buckets
                        and segment.material_available is not None
                        and offer.material_available < segment.material_available
                        else 0
                    ),
                )
                for segment_index, segment in enumerate(recovery_segments)
            )
            x.append(x_var)
            y.append(y_var)
            z.append(z_vars)
            baseline_allocations.append(baseline_vars)
            recovery_allocations.append(recovery_vars)
            model.add_row(
                {x_var: 1, step_var: -increment_atoms},
                lower=0,
                upper=0,
            )
            model.add_row(
                {x_var: 1, **{variable: -1 for variable in z_vars}},
                lower=0,
                upper=0,
            )
            for bucket_index, z_variable in enumerate(z_vars):
                model.add_row(
                    {
                        baseline_vars[bucket_index]: 1,
                        **{
                            recovery_vars[segment_index]: 1
                            for segment_index, segment in enumerate(
                                recovery_segments
                            )
                            if segment.bucket_index == bucket_index
                        },
                        z_variable: -1,
                    },
                    upper=0,
                )
            minimum = _ceil_units(
                offer.catalog.minimum_order_quantity, atom
            )
            model.add_row({x_var: 1, y_var: -minimum}, lower=0)
            model.add_row({x_var: 1, y_var: -upper}, upper=0)

        for bucket_index, quantity in enumerate(uncovered_by_bucket):
            model.add_row(
                {
                    baseline_allocations[offer_index][bucket_index]: 1
                    for offer_index in range(len(offers))
                },
                upper=_ceil_units(quantity, atom),
            )
        for segment_index, segment in enumerate(recovery_segments):
            model.add_row(
                {
                    recovery_allocations[offer_index][segment_index]: 1
                    for offer_index in range(len(offers))
                },
                upper=_ceil_units(segment.quantity, atom),
            )
        model.add_row(
            {
                recovery_allocations[offer_index][segment_index]: 1
                for offer_index in range(len(offers))
                for segment_index in range(len(recovery_segments))
            },
            upper=_ceil_units(recovery_demand, atom),
        )

        by_supplier: dict[str, list[int]] = defaultdict(list)
        for index, offer in enumerate(offers):
            by_supplier[offer.supplier.supplier_id].append(index)
        secondary = self._minimum_secondary(snapshot, requirement.component)
        if secondary is not None:
            if len(by_supplier) >= 2:
                maximum_share = Fraction(Decimal("1") - secondary)
                for indexes in by_supplier.values():
                    coefficients = {
                        variable: -maximum_share for variable in x
                    }
                    for index in indexes:
                        coefficients[x[index]] = (
                            coefficients.get(x[index], Fraction()) + 1
                        )
                    model.add_row(coefficients, upper=0)
            else:
                for indexes in by_supplier.values():
                    model.add_row(
                        {x[index]: 1 for index in indexes},
                        lower=0,
                        upper=0,
                    )
        if named_supplier is not None:
            named_indexes = by_supplier.get(named_supplier, ())
            for supplier_id, indexes in by_supplier.items():
                if supplier_id == named_supplier:
                    continue
                coefficients = {x[index]: 1 for index in indexes}
                for index in named_indexes:
                    coefficients[x[index]] = (
                        coefficients.get(x[index], 0) - 1
                    )
                model.add_row(coefficients, upper=0)

        eventual_gap_atoms = _ceil_units(requirement.eventual_gap, atom)
        eventual_gap = model.add_variable(
            "eventual_gap", 0, eventual_gap_atoms
        )
        model.add_row(
            {
                eventual_gap: 1,
                **{
                    baseline_allocations[offer_index][bucket_index]: 1
                    for offer_index in range(len(offers))
                    for bucket_index in range(
                        len(requirement.bucket_quantities)
                    )
                },
            },
            lower=eventual_gap_atoms,
        )
        if solve_kind is SolveKind.BASELINE and coverage_target is not None:
            target_atoms = _ceil_units(coverage_target, atom)
            model.add_row(
                {
                    baseline_allocations[offer_index][bucket_index]: 1
                    for offer_index in range(len(offers))
                    for bucket_index in range(
                        len(requirement.bucket_quantities)
                    )
                },
                lower=target_atoms,
                upper=target_atoms,
            )
        unresolved: list[int] = []
        for bucket_index, (due, _quantity) in enumerate(
            requirement.bucket_quantities
        ):
            required = max(
                ZERO,
                requirement.cumulative_demand(due)
                - requirement.on_time_supply(due),
            )
            required_atoms = _ceil_units(required, atom)
            variable = model.add_variable(
                f"unresolved[{bucket_index}]", 0, required_atoms
            )
            unresolved.append(variable)
            coefficients: dict[int, Fraction | int] = {variable: 1}
            for offer_index, offer in enumerate(offers):
                if offer.material_available > due:
                    continue
                for allocated_index in range(bucket_index + 1):
                    coefficients[z[offer_index][allocated_index]] = 1
            model.add_row(coefficients, lower=required_atoms)

        review_routes: dict[str, list[int]] = defaultdict(list)
        for offer_index, offer in enumerate(offers):
            for key in offer.review_keys:
                review_routes[key].append(offer_index)
        review_variables: list[int] = []
        for review_index, (_key, indexes) in enumerate(
            sorted(review_routes.items())
        ):
            variable = model.add_variable(
                f"review[{review_index}]", 0, 1
            )
            review_variables.append(variable)
            for index in indexes:
                model.add_row({variable: 1, y[index]: -1}, lower=0)
            model.add_row(
                {variable: 1, **{y[index]: -1 for index in indexes}},
                upper=0,
            )

        planning_requirement = requirement.eventual_gap + recovery_demand
        total_upper = sum(upper_atoms)
        q_min_atoms = _ceil_units(q_min or ZERO, atom)
        forced_atoms = max(0, q_min_atoms - eventual_gap_atoms)
        recovery_coefficients = {
            recovery_allocations[offer_index][segment_index]: -1
            for offer_index in range(len(offers))
            for segment_index in range(len(recovery_segments))
        }
        moq_excess = model.add_variable(
            "moq_excess", 0, total_upper
        )
        model.add_row(
            {
                moq_excess: 1,
                **{variable: -1 for variable in x},
                **{
                    recovery_allocations[offer_index][segment_index]: 1
                    for offer_index in range(len(offers))
                    for segment_index in range(len(recovery_segments))
                },
            },
            lower=-eventual_gap_atoms,
        )

        discretionary = model.add_variable(
            "discretionary_surplus", 0, total_upper
        )
        discretionary_coefficients = {
            discretionary: 1,
            **{variable: -1 for variable in x},
        }
        if not recovery_segments:
            model.add_row(
                discretionary_coefficients,
                lower=-q_min_atoms,
            )
        elif forced_atoms == 0:
            model.add_row(
                {
                    **discretionary_coefficients,
                    **{
                        variable: -coefficient
                        for variable, coefficient in recovery_coefficients.items()
                    },
                },
                lower=-q_min_atoms,
            )
        else:
            recovery_credit = model.add_variable(
                "recovery_credit", 0, total_upper
            )
            recovery_credit_active = model.add_variable(
                "recovery_credit_active", 0, 1
            )
            model.add_row(
                {recovery_credit: 1, **recovery_coefficients},
                upper=0,
            )
            model.add_row(
                {
                    recovery_credit: 1,
                    recovery_credit_active: -total_upper,
                },
                upper=0,
            )
            model.add_row(
                {
                    recovery_credit: 1,
                    **recovery_coefficients,
                    recovery_credit_active: total_upper,
                },
                upper=total_upper - forced_atoms,
            )
            model.add_row(
                {
                    **discretionary_coefficients,
                    recovery_credit: 1,
                },
                lower=-q_min_atoms,
            )

        if solve_kind is SolveKind.EXECUTABLE:
            if q_min is None or cheapest_covering_cost is None:
                return IndependentSolve(
                    SolverStatus.ERROR, (), certificate_complete=False
                )
            model.add_row(
                {
                    baseline_allocations[offer_index][bucket_index]: 1
                    for offer_index in range(len(offers))
                    for bucket_index in range(
                        len(requirement.bucket_quantities)
                    )
                },
                lower=_ceil_units(min(q_min, requirement.eventual_gap), atom),
            )
            surplus_cap = (
                self.autonomy.max_surplus_fraction
                * planning_requirement
            )
            if self.autonomy.max_surplus_units is not None:
                surplus_cap = min(
                    surplus_cap, self.autonomy.max_surplus_units
                )
            model.add_row(
                {discretionary: 1},
                upper=Fraction(surplus_cap) / Fraction(atom),
            )
            cost_coefficients = {
                x[index]: Fraction(offer.catalog.unit_price * atom)
                for index, offer in enumerate(offers)
            }
            model.add_row(
                cost_coefficients,
                upper=Fraction(
                    cheapest_covering_cost
                    + self.autonomy.max_excess_cost_usd
                ),
            )

        last_values: tuple[int, ...] | None = None

        def minimize(
            coefficients: Mapping[int, Fraction | int],
        ) -> Fraction | None:
            nonlocal last_values
            result, value = self._milp_minimize_and_pin(
                model, coefficients
            )
            if result.values is not None:
                last_values = result.values
            if (
                result.status is not SolverStatus.OPTIMAL
                or not result.certificate_complete
            ):
                raise _IndependentSolveInterrupted(result.status)
            return value

        try:
            if solve_kind is SolveKind.QUANTITY_CALIBRATION:
                uncovered = minimize({eventual_gap: 1})
                total = minimize({variable: 1 for variable in x})
                assert uncovered is not None and total is not None
                if last_values is None:
                    final = self._milp_optimize(model, {})
                    if final.values is None:
                        return IndependentSolve(
                            final.status, (), certificate_complete=False
                        )
                    last_values = final.values
                total_quantity = _decimal(total * Fraction(atom))
                return IndependentSolve(
                    SolverStatus.OPTIMAL,
                    (
                        _decimal(uncovered * Fraction(atom)),
                        total_quantity,
                    ),
                    tuple(
                        sorted(
                            (
                                offers[index].supplier.supplier_id,
                                Decimal(last_values[x[index]]) * atom,
                            )
                            for index in range(len(offers))
                            if last_values[x[index]] > 0
                        )
                    ),
                    minimum_compliant_total=total_quantity,
                )
            if solve_kind is SolveKind.BASELINE:
                uncovered = minimize({eventual_gap: 1})
                cost = minimize(
                    {
                        x[index]: Fraction(
                            offer.catalog.unit_price * atom
                        )
                        for index, offer in enumerate(offers)
                    }
                )
                assert uncovered is not None and cost is not None
                if last_values is None:
                    final = self._milp_optimize(model, {})
                    if final.values is None:
                        return IndependentSolve(
                            final.status, (), certificate_complete=False
                        )
                    last_values = final.values
                exact_cost = _decimal(cost)
                return IndependentSolve(
                    SolverStatus.OPTIMAL,
                    (
                        _decimal(uncovered * Fraction(atom)),
                        exact_cost,
                    ),
                    tuple(
                        sorted(
                            (
                                offers[index].supplier.supplier_id,
                                Decimal(last_values[x[index]]) * atom,
                            )
                            for index in range(len(offers))
                            if last_values[x[index]] > 0
                        )
                    ),
                    cheapest_covering_cost=exact_cost,
                )

            assert q_min is not None and cheapest_covering_cost is not None
            objective: list[Decimal] = []
            for variable in unresolved:
                value = minimize({variable: 1})
                assert value is not None
                objective.append(_decimal(value * Fraction(atom)))
            late_coefficients: dict[int, Fraction | int] = {}
            committed_late = sum(
                (
                    segment.quantity
                    * Decimal(
                        (segment.material_available - segment.due_date).days
                    )
                    for segment in source_segments
                    if segment.committed
                    and segment.material_available is not None
                    and segment.material_available > segment.due_date
                ),
                ZERO,
            )
            late_offset = model.add_variable(
                "committed_lateness_offset",
                _ceil_units(committed_late, atom),
                _ceil_units(committed_late, atom),
            )
            late_coefficients[late_offset] = 1
            for offer_index, offer in enumerate(offers):
                for bucket_index, (due, _quantity) in enumerate(
                    requirement.bucket_quantities
                ):
                    late_coefficients[
                        baseline_allocations[offer_index][bucket_index]
                    ] = max(0, (offer.material_available - due).days)
                for segment_index, segment in enumerate(recovery_segments):
                    assert segment.material_available is not None
                    late_coefficients[
                        recovery_allocations[offer_index][segment_index]
                    ] = (
                        max(
                            0,
                            (offer.material_available - segment.due_date).days,
                        )
                        - (segment.material_available - segment.due_date).days
                    )
            late = minimize(late_coefficients)
            assert late is not None
            objective.append(_decimal(late * Fraction(atom)))
            discretionary_value = minimize({discretionary: 1})
            assert discretionary_value is not None
            objective.append(
                _decimal(discretionary_value * Fraction(atom))
            )
            review = minimize({variable: 1 for variable in review_variables})
            assert review is not None
            objective.append(_decimal(review))
            objective.append(ZERO)

            international = minimize(
                {
                    z[offer_index][bucket_index]: 1
                    for offer_index, offer in enumerate(offers)
                    for bucket_index, (due, _quantity) in enumerate(
                        requirement.bucket_quantities
                    )
                    if offer.international
                    and offer.condition_for(due) != "b"
                }
            )
            assert international is not None
            objective.append(
                _decimal(international * Fraction(atom))
            )

            strategic_coefficients: dict[int, Fraction | int] = {}
            sustainability_coefficients: dict[int, Fraction | int] = {}
            for offer_index, offer in enumerate(offers):
                current_rating = _rating(
                    offer.supplier.sustainability_rating
                )
                for bucket_index, (due, _quantity) in enumerate(
                    requirement.bucket_quantities
                ):
                    if due not in offer.allowed_buckets:
                        continue
                    if (
                        self._concept("strategic_supplier", offer.supplier)
                        is EvidenceStatus.FAIL
                    ):
                        strategic = tuple(
                            item
                            for item in offers
                            if due in item.allowed_buckets
                            and self._concept(
                                "strategic_supplier", item.supplier
                            )
                            is EvidenceStatus.PASS
                        )
                        if strategic:
                            best = min(
                                strategic,
                                key=lambda item: item.catalog.unit_price,
                            )
                            savings = (
                                ZERO
                                if best.catalog.unit_price == ZERO
                                else (
                                    best.catalog.unit_price
                                    - offer.catalog.unit_price
                                )
                                / best.catalog.unit_price
                            )
                            if savings <= strategic_savings:
                                strategic_coefficients[
                                    z[offer_index][bucket_index]
                                ] = 1
                    if current_rating is None:
                        continue
                    comparable = tuple(
                        item
                        for item in offers
                        if due in item.allowed_buckets
                        and _rating(item.supplier.sustainability_rating)
                        is not None
                        and _rating(item.supplier.sustainability_rating)
                        > current_rating
                        and (
                            item.catalog.unit_price
                            == offer.catalog.unit_price
                            if min(
                                item.catalog.unit_price,
                                offer.catalog.unit_price,
                            )
                            == ZERO
                            else abs(
                                item.catalog.unit_price
                                - offer.catalog.unit_price
                            )
                            / min(
                                item.catalog.unit_price,
                                offer.catalog.unit_price,
                            )
                            <= sustainability.comparable_price_fraction
                        )
                        and _business_days(
                            item.material_available,
                            offer.material_available,
                        )
                        <= sustainability.comparable_delivery_days
                    )
                    if comparable:
                        best_rating = max(
                            _rating(
                                item.supplier.sustainability_rating
                            )
                            for item in comparable
                        )
                        assert best_rating is not None
                        sustainability_coefficients[
                            z[offer_index][bucket_index]
                        ] = Fraction(best_rating - current_rating)
            strategic = minimize(strategic_coefficients)
            sustainable = minimize(sustainability_coefficients)
            assert strategic is not None and sustainable is not None
            objective.extend(
                (
                    _decimal(strategic * Fraction(atom)),
                    _decimal(sustainable * Fraction(atom)),
                )
            )
            cost = minimize(
                {
                    x[index]: Fraction(offer.catalog.unit_price * atom)
                    for index, offer in enumerate(offers)
                }
            )
            excess = minimize({moq_excess: 1})
            lead = minimize(
                {
                    x[index]: offer.lead_days
                    for index, offer in enumerate(offers)
                }
            )
            line_count = minimize({variable: 1 for variable in y})
            assert cost is not None
            assert excess is not None
            assert lead is not None
            assert line_count is not None
            objective.extend(
                (
                    _decimal(cost),
                    _decimal(excess * Fraction(atom)),
                    _decimal(lead * Fraction(atom)),
                    _decimal(line_count),
                )
            )

            if last_values is None:
                final = self._milp_optimize(model, {})
                if final.values is None:
                    return IndependentSolve(
                        final.status, (), certificate_complete=False
                    )
                last_values = final.values
            selected_count = sum(last_values[variable] for variable in y)
            first_unfixed = 0
            for _position in range(selected_count):
                membership = {
                    y[index]: -(1 << (len(offers) - index))
                    for index in range(first_unfixed, len(offers))
                }
                result = self._milp_optimize(
                    model,
                    {
                        index: Fraction(value)
                        for index, value in membership.items()
                    },
                )
                if (
                    result.status is not SolverStatus.OPTIMAL
                    or not result.certificate_complete
                    or result.values is None
                ):
                    raise _IndependentSolveInterrupted(result.status)
                chosen = next(
                    (
                        index
                        for index in range(first_unfixed, len(offers))
                        if result.values[y[index]] == 1
                    ),
                    None,
                )
                if chosen is None:
                    raise _IndependentSolveInterrupted(SolverStatus.ERROR)
                for index in range(first_unfixed, chosen):
                    model.add_row({y[index]: 1}, lower=0, upper=0)
                model.add_row({y[chosen]: 1}, lower=1, upper=1)
                quantity_result, _quantity = self._milp_minimize_and_pin(
                    model, {x[chosen]: 1}
                )
                if (
                    quantity_result.status is not SolverStatus.OPTIMAL
                    or not quantity_result.certificate_complete
                    or quantity_result.values is None
                ):
                    raise _IndependentSolveInterrupted(
                        quantity_result.status
                    )
                last_values = quantity_result.values
                first_unfixed = chosen + 1
            return IndependentSolve(
                SolverStatus.OPTIMAL,
                tuple(objective),
                tuple(
                    sorted(
                        (
                            offers[index].supplier.supplier_id,
                            Decimal(last_values[x[index]]) * atom,
                        )
                        for index in range(len(offers))
                        if last_values[x[index]] > 0
                    )
                ),
                minimum_compliant_total=q_min,
                cheapest_covering_cost=cheapest_covering_cost,
                recovery_quantity=Decimal(
                    sum(
                        last_values[
                            recovery_allocations[offer_index][segment_index]
                        ]
                        for offer_index in range(len(offers))
                        for segment_index in range(len(recovery_segments))
                    )
                )
                * atom,
            )
        except _IndependentSolveInterrupted as interrupted:
            return IndependentSolve(
                interrupted.status, (), certificate_complete=False
            )

    def independently_solve(
        self,
        snapshot: ScenarioSnapshot,
        decision: DecisionRecord,
        solve_kind: SolveKind,
    ) -> IndependentSolve:
        sink = _IssueSink([])
        requirement = self._source_requirements(snapshot, sink).get(
            decision.component_id
        )
        if requirement is None:
            return IndependentSolve(
                SolverStatus.ERROR, (), certificate_complete=False
            )
        offers = self._offers(
            snapshot, requirement, decision.evidence_contract
        )
        secondary = self._minimum_secondary(snapshot, requirement.component)
        increment = self._enumeration_increment(
            requirement, offers, secondary
        )
        catalogs = tuple(item.catalog for item in offers)
        derived = self.derive_upper_bounds(
            snapshot, requirement, catalogs
        )
        group_bound = (
            max(
                [requirement.eventual_gap]
                + list(derived.values())
                + [
                    sum(
                        (
                            item.minimum_order_quantity
                            for item in catalogs
                        ),
                        ZERO,
                    )
                ]
            )
            if offers
            else ZERO
        )
        maximum_units = _floor_units(group_bound, increment)
        source_segments = self._supply_segments(requirement)
        uncovered_bucket_indexes = {
            segment.bucket_index
            for segment in source_segments
            if segment.uncovered and segment.quantity > ZERO
        }
        use_enumeration = (
            self.solver_limits.force_status is None
            and maximum_units <= self.tiny_case_unit_limit
            and len(offers) <= 3
            and len(requirement.bucket_quantities) <= 3
            and not self._recovery_segments(requirement, offers)
            and all(
                offer.material_available
                <= requirement.bucket_quantities[bucket_index][0]
                for offer in offers
                for bucket_index in uncovered_bucket_indexes
            )
        )
        if use_enumeration:
            return self._independently_enumerate(
                snapshot, decision, solve_kind
            )

        named_check = self._named_primary(
            snapshot, requirement.component
        )
        named_supplier = None
        if (
            solve_kind
            in {SolveKind.QUANTITY_CALIBRATION, SolveKind.EXECUTABLE}
            and named_check is not None
            and named_check.resolved
            and named_check.supplier_id
            not in self.capacity_confirmed_supplier_ids
        ):
            named_supplier = named_check.supplier_id
        if solve_kind is SolveKind.BASELINE:
            q_result = self.independently_solve(
                snapshot, decision, SolveKind.QUANTITY_CALIBRATION
            )
            if (
                q_result.status
                not in {SolverStatus.OPTIMAL, SolverStatus.INFEASIBLE}
                or not q_result.certificate_complete
                or not q_result.objective_vector
            ):
                return IndependentSolve(
                    q_result.status, (), certificate_complete=False
                )
            return self._independently_solve_milp(
                snapshot,
                requirement,
                offers,
                solve_kind,
                named_supplier=named_supplier,
                coverage_target=(
                    requirement.eventual_gap - q_result.objective_vector[0]
                ),
            )
        if solve_kind is not SolveKind.EXECUTABLE:
            return self._independently_solve_milp(
                snapshot,
                requirement,
                offers,
                solve_kind,
                named_supplier=named_supplier,
            )
        q_result = self.independently_solve(
            snapshot, decision, SolveKind.QUANTITY_CALIBRATION
        )
        baseline = self.independently_solve(
            snapshot, decision, SolveKind.BASELINE
        )
        if (
            q_result.status is not SolverStatus.OPTIMAL
            or baseline.status is not SolverStatus.OPTIMAL
            or not q_result.certificate_complete
            or not baseline.certificate_complete
            or q_result.minimum_compliant_total is None
            or baseline.cheapest_covering_cost is None
        ):
            status = (
                q_result.status
                if q_result.status is not SolverStatus.OPTIMAL
                else baseline.status
            )
            return IndependentSolve(
                status, (), certificate_complete=False
            )
        return self._independently_solve_milp(
            snapshot,
            requirement,
            offers,
            solve_kind,
            named_supplier=named_supplier,
            q_min=q_result.minimum_compliant_total,
            cheapest_covering_cost=baseline.cheapest_covering_cost,
        )

    def _independently_enumerate(
        self,
        snapshot: ScenarioSnapshot,
        decision: DecisionRecord,
        solve_kind: SolveKind,
    ) -> IndependentSolve:
        sink = _IssueSink([])
        requirement = self._source_requirements(snapshot, sink).get(decision.component_id)
        if requirement is None:
            return IndependentSolve(SolverStatus.ERROR, (), certificate_complete=False)
        offers = self._offers(snapshot, requirement, decision.evidence_contract)
        base_increment = self._increment(requirement.component)
        secondary = self._minimum_secondary(snapshot, requirement.component)
        named_check = self._named_primary(snapshot, requirement.component)
        named_supplier = None
        if (
            solve_kind in {SolveKind.QUANTITY_CALIBRATION, SolveKind.EXECUTABLE}
            and named_check is not None
            and named_check.resolved
            and named_check.supplier_id not in self.capacity_confirmed_supplier_ids
        ):
            named_supplier = named_check.supplier_id
        catalogs = tuple(item.catalog for item in offers)
        increment = self._enumeration_increment(
            requirement, offers, secondary
        )
        derived = self.derive_upper_bounds(snapshot, requirement, catalogs)
        group_bound = max(
            [requirement.eventual_gap]
            + list(derived.values())
            + [sum((item.minimum_order_quantity for item in catalogs), ZERO)]
        ) if offers else ZERO
        maximum_units = _floor_units(group_bound, increment)
        target_units = _ceil_units(requirement.eventual_gap, increment)
        coverage_target = requirement.eventual_gap
        if solve_kind is SolveKind.BASELINE:
            q_result = self.independently_solve(
                snapshot, decision, SolveKind.QUANTITY_CALIBRATION
            )
            if (
                q_result.status
                not in {SolverStatus.OPTIMAL, SolverStatus.INFEASIBLE}
                or not q_result.certificate_complete
                or not q_result.objective_vector
            ):
                return IndependentSolve(
                    q_result.status, (), certificate_complete=False
                )
            coverage_target = (
                requirement.eventual_gap - q_result.objective_vector[0]
            )
            target_units = _ceil_units(coverage_target, increment)
        nodes = [0]
        best_q: tuple[Decimal, dict[int, Decimal]] | None = None
        best_baseline: tuple[tuple[Decimal, Decimal], dict[int, Decimal]] | None = None
        cheapest_unit_price = min(
            (item.catalog.unit_price for item in offers),
            default=ZERO,
        )
        # Q first establishes the maximum coverage and then the least total.
        total_range: Iterable[int]
        if solve_kind is SolveKind.QUANTITY_CALIBRATION:
            total_range = (
                *range(target_units, maximum_units + 1),
                *range(target_units - 1, -1, -1),
            )
        elif solve_kind is SolveKind.BASELINE:
            total_range = range(target_units, maximum_units + 1)
        else:
            total_range = ()
        for total_units in total_range:
            vectors = self._vectors_for_total(total_units, offers, increment, secondary, named_supplier, nodes)
            for vector in vectors:
                total = Decimal(total_units) * increment
                coverage = min(requirement.eventual_gap, total)
                if best_q is None or coverage > best_q[0] or (coverage == best_q[0] and total < sum(best_q[1].values(), ZERO)):
                    best_q = (coverage, vector)
                if (
                    solve_kind is SolveKind.BASELINE
                    and coverage < coverage_target
                ):
                    continue
                uncovered = (
                    requirement.eventual_gap - coverage_target
                    if solve_kind is SolveKind.BASELINE
                    else max(ZERO, requirement.eventual_gap - coverage)
                )
                cost = sum((offers[index].catalog.unit_price * quantity for index, quantity in vector.items()), ZERO)
                key = (uncovered, cost)
                if best_baseline is None or key < best_baseline[0]:
                    best_baseline = (key, vector)
            if nodes[0] > self.enumeration_node_limit:
                return IndependentSolve(SolverStatus.RESOURCE_LIMIT, (), certificate_complete=False)
            if solve_kind is SolveKind.QUANTITY_CALIBRATION and best_q is not None:
                total = sum(best_q[1].values(), ZERO)
                uncovered = requirement.eventual_gap - best_q[0]
                return IndependentSolve(
                    SolverStatus.OPTIMAL,
                    (uncovered, total),
                    tuple(sorted((offers[index].supplier.supplier_id, quantity) for index, quantity in best_q[1].items())),
                    minimum_compliant_total=total,
                )
            if (
                solve_kind is SolveKind.BASELINE
                and best_baseline is not None
                and best_baseline[0][0] == ZERO
                and cheapest_unit_price > ZERO
                and Decimal(total_units + 1) * increment * cheapest_unit_price
                > best_baseline[0][1]
            ):
                # Every unvisited allocation contains at least the next total
                # quantity and cannot cost less than this exact lower bound.
                # The strict comparison retains all equal-cost allocations;
                # solve 0 certifies only the (gap, cost) vector.
                break
        if solve_kind is SolveKind.QUANTITY_CALIBRATION:
            if best_q is None:
                return IndependentSolve(SolverStatus.INFEASIBLE, (requirement.eventual_gap, ZERO))
            uncovered = requirement.eventual_gap - best_q[0]
            total = sum(best_q[1].values(), ZERO)
            return IndependentSolve(SolverStatus.OPTIMAL, (uncovered, total), minimum_compliant_total=total)
        if solve_kind is SolveKind.BASELINE:
            if best_baseline is None:
                return IndependentSolve(SolverStatus.OPTIMAL, (requirement.eventual_gap, ZERO), cheapest_covering_cost=ZERO)
            return IndependentSolve(
                SolverStatus.OPTIMAL,
                best_baseline[0],
                tuple(sorted((offers[index].supplier.supplier_id, quantity) for index, quantity in best_baseline[1].items())),
                cheapest_covering_cost=best_baseline[0][1],
            )
        q_result = self.independently_solve(snapshot, decision, SolveKind.QUANTITY_CALIBRATION)
        baseline = self.independently_solve(snapshot, decision, SolveKind.BASELINE)
        if q_result.status is not SolverStatus.OPTIMAL or baseline.status is not SolverStatus.OPTIMAL or q_result.minimum_compliant_total is None or baseline.cheapest_covering_cost is None:
            return IndependentSolve(SolverStatus.UNRESOLVED, (), certificate_complete=False)
        q_min = q_result.minimum_compliant_total
        # Solve Q may land on a finer legal unit if a ratio boundary does not
        # divide the coarser lattice.  That case already forced
        # _enumeration_increment back to the base unit; retain the explicit
        # guard here so executable enumeration never rounds a calibration.
        if q_min / increment != (q_min / increment).to_integral_value():
            increment = base_increment
            maximum_units = _floor_units(group_bound, increment)
        lower_units = _ceil_units(q_min, increment)
        surplus_limit = self.autonomy.max_surplus_fraction * requirement.eventual_gap
        if self.autonomy.max_surplus_units is not None:
            surplus_limit = min(surplus_limit, self.autonomy.max_surplus_units)
        upper_units = min(maximum_units, _floor_units(q_min + surplus_limit, increment))
        best: tuple[
            tuple[Decimal, ...],
            tuple[tuple[str, str, Decimal], ...],
            dict[int, Decimal],
        ] | None = None
        nodes = [0]
        for total_units in range(lower_units, upper_units + 1):
            for vector in self._vectors_for_total(total_units, offers, increment, secondary, named_supplier, nodes):
                objective = self._best_phased_objective(
                    snapshot,
                    requirement,
                    offers,
                    vector,
                    q_min,
                    nodes,
                )
                if objective is None:
                    continue
                cost_index = len(requirement.bucket_quantities) + _EXECUTABLE_OBJECTIVE_SUFFIX.index(
                    "stage_09_known_landed_cost"
                )
                if objective[cost_index] - baseline.cheapest_covering_cost > self.autonomy.max_excess_cost_usd:
                    continue
                semantic_tie = self._semantic_tie_key(offers, vector)
                if best is None or (objective, semantic_tie) < best[:2]:
                    best = (objective, semantic_tie, vector)
            if nodes[0] > self.enumeration_node_limit:
                return IndependentSolve(SolverStatus.RESOURCE_LIMIT, (), certificate_complete=False)
        if best is None:
            return IndependentSolve(SolverStatus.INFEASIBLE, ())
        return IndependentSolve(
            SolverStatus.OPTIMAL,
            best[0],
            tuple(sorted((offers[index].supplier.supplier_id, quantity) for index, quantity in best[2].items())),
            minimum_compliant_total=q_min,
            cheapest_covering_cost=baseline.cheapest_covering_cost,
        )

    def independently_check_b_or_better(
        self,
        snapshot: ScenarioSnapshot,
        component_id: str,
        contract: EvidenceContract,
    ) -> IndependentSolve:
        """Certify whether full coverage exists without a below-B supplier.

        This is a separate predicate solve for the sustainability gate.  It
        intentionally omits named-primary shaping: a B-or-better plan is an
        alternative even when a memo would rank a different supplier first.
        Hard eligibility, MOQ, and prospective secondary allocation remain.
        """

        sink = _IssueSink([])
        requirement = self._source_requirements(snapshot, sink).get(component_id)
        if requirement is None:
            return IndependentSolve(
                SolverStatus.ERROR,
                (),
                certificate_complete=False,
            )
        boundary = _rating("B")
        assert boundary is not None
        offers = tuple(
            item
            for item in self._offers(snapshot, requirement, contract)
            if (rating := _rating(item.supplier.sustainability_rating)) is not None
            and rating >= boundary
        )
        secondary = self._minimum_secondary(snapshot, requirement.component)
        catalogs = tuple(item.catalog for item in offers)
        derived = self.derive_upper_bounds(snapshot, requirement, catalogs)
        group_bound = (
            max(
                [requirement.eventual_gap]
                + list(derived.values())
                + [
                    sum(
                        (item.minimum_order_quantity for item in catalogs),
                        ZERO,
                    )
                ]
            )
            if offers
            else ZERO
        )
        enumeration_increment = self._enumeration_increment(
            requirement, offers, secondary
        )
        maximum_units = _floor_units(group_bound, enumeration_increment)
        if (
            self.solver_limits.force_status is not None
            or maximum_units > self.tiny_case_unit_limit
            or len(offers) > 3
            or len(requirement.bucket_quantities) > 3
        ):
            result = self._independently_solve_milp(
                snapshot,
                requirement,
                offers,
                SolveKind.QUANTITY_CALIBRATION,
                named_supplier=None,
            )
            if (
                result.status is not SolverStatus.OPTIMAL
                or not result.certificate_complete
            ):
                return result
            if result.objective_vector[0] == ZERO:
                return result
            return IndependentSolve(
                SolverStatus.INFEASIBLE,
                (result.objective_vector[0],),
            )
        increment = self._increment(requirement.component)
        maximum_units = _floor_units(group_bound, increment)
        nodes = [0]
        for total_units in range(1, maximum_units + 1):
            for vector in self._vectors_for_total(
                total_units,
                offers,
                increment,
                secondary,
                None,
                nodes,
            ):
                total = Decimal(total_units) * increment
                if total >= requirement.eventual_gap:
                    return IndependentSolve(
                        SolverStatus.OPTIMAL,
                        (ZERO, total),
                        tuple(
                            sorted(
                                (
                                    offers[index].supplier.supplier_id,
                                    quantity,
                                )
                                for index, quantity in vector.items()
                            )
                        ),
                    )
            if nodes[0] > self.enumeration_node_limit:
                return IndependentSolve(
                    SolverStatus.RESOURCE_LIMIT,
                    (),
                    certificate_complete=False,
                )
        return IndependentSolve(
            SolverStatus.INFEASIBLE,
            (requirement.eventual_gap,),
        )

    # ---- invariant checks ---------------------------------------------

    def _check_decision_facts(
        self,
        snapshot: ScenarioSnapshot,
        decision: DecisionRecord,
        requirement: _SourceRequirement,
        sink: _IssueSink,
    ) -> None:
        component_id = decision.component_id
        expected_buckets = requirement.bucket_quantities
        actual_buckets = tuple((item.due_date, item.bucket_quantity) for item in decision.demand_buckets)
        if actual_buckets != expected_buckets:
            sink.error("SOURCE_DEMAND_MISMATCH", "Decision demand buckets do not match BOM and production rows.", component_id=component_id)
        if decision.total_requirement != requirement.total_demand:
            sink.error("TOTAL_REQUIREMENT_MISMATCH", "Total requirement was not recomputed from source demand.", component_id=component_id)
        if decision.supply_ledger.on_hand != requirement.on_hand:
            sink.error("ON_HAND_MISMATCH", "Supply ledger on-hand quantity differs from source inventory.", component_id=component_id)
        actual_inbound = tuple((item.po_number, item.quantity, item.expected_delivery_date) for item in decision.supply_ledger.committed_inbound)
        if actual_inbound != requirement.inbound:
            sink.error("INBOUND_DATE_INCLUSION_MISMATCH", "Committed inbound must use inclusive expected-delivery dates, never order dates.", component_id=component_id)
        if decision.initial_eventual_gap != requirement.eventual_gap:
            sink.error("INITIAL_GAP_MISMATCH", "Initial eventual gap differs from independently netted supply.", component_id=component_id)

    def _check_plan(
        self,
        snapshot: ScenarioSnapshot,
        decision: DecisionRecord,
        requirement: _SourceRequirement,
        plan: CandidatePlan,
        offers: Sequence[_Offer],
        sink: _IssueSink,
    ) -> None:
        component_id = decision.component_id
        plan_id = plan.plan_id
        suppliers = {item.supplier_id: item for item in snapshot.suppliers}
        catalogs = {(item.component_id, item.supplier_id): item for item in snapshot.catalog_lines}
        evidence_contract_diagnostic = _is_evidence_contract_diagnostic(plan)
        compliance_diagnostic = (
            not plan.disposition.writes_purchase_order
            and plan.summary.startswith(_COMPLIANCE_DIAGNOSTIC_PREFIX)
        )
        baseline_vector_diagnostic = (
            compliance_diagnostic and len(plan.objective_vector) == 2
        )
        sub_moq_rule_ids = {
            rule.rule_id
            for rule in self._rules(
                snapshot.configuration.current_date,
                "sub_moq_written_approval",
            )
        }
        represented_sub_moq_approval = (
            plan.disposition is PlanDisposition.RECOMMEND_APPROVAL
            and bool(
                sub_moq_rule_ids
                & set(plan.relaxed_rule_ids)
                & set(plan.unresolved_approval_ids)
            )
        )
        order_value_rules = self._rules(
            snapshot.configuration.current_date,
            "order_value_approval",
        )
        approval_parameters = self._parameters(
            snapshot.configuration.current_date
        ).approval_thresholds
        order_value_rule_ids = {item.rule_id for item in approval_parameters}
        if order_value_rule_ids != {item.rule_id for item in order_value_rules}:
            raise ValueError("typed approval parameters disagree with active approval rules")
        group_ids = {line.allocation_group_id for line in plan.lines}
        if len(group_ids) != 1:
            sink.error("ALLOCATION_GROUP_MISMATCH", "All lines for one component/run must share one allocation group.", component_id=component_id, plan_id=plan_id)
        increment = self._increment(requirement.component)
        derived_u = self.derive_upper_bounds(
            snapshot,
            requirement,
            tuple(item.catalog for item in offers),
        )
        offer_catalogs = tuple(
            {
                (item.catalog.component_id, item.catalog.supplier_id): item.catalog
                for item in offers
            }.values()
        )
        secondary_rule_id = self._minimum_secondary_rule_id(
            snapshot,
            requirement.component,
        )
        forced_exception_surplus = self._forced_allocation_surplus(
            requirement,
            offer_catalogs,
            self._minimum_secondary(snapshot, requirement.component),
        )
        exception_totals: dict[str, Decimal] = defaultdict(Decimal)
        exception_dates: dict[str, set[date]] = defaultdict(set)
        for line in plan.lines:
            catalog = catalogs.get((line.component_id, line.supplier_id))
            supplier = suppliers.get(line.supplier_id)
            if catalog is None or supplier is None:
                sink.error("CATALOG_ROUTE_MISMATCH", "Plan line has no matching component/supplier catalog row.", component_id=component_id, plan_id=plan_id)
                continue
            if line.unit_price != catalog.unit_price:
                sink.error("CATALOG_PRICE_MISMATCH", "Plan unit price differs from the source catalog Decimal.", component_id=component_id, plan_id=plan_id)
            if (
                line.quantity < catalog.minimum_order_quantity
                and not represented_sub_moq_approval
            ):
                sink.error("MOQ_VIOLATION", "Plan quantity is below the catalog MOQ without represented approval.", component_id=component_id, plan_id=plan_id)
            if line.quantity / increment != (line.quantity / increment).to_integral_value():
                sink.error("QUANTITY_PRECISION", "Plan quantity violates the independently derived unit increment.", component_id=component_id, plan_id=plan_id)
            if line.order_date != snapshot.configuration.current_date:
                sink.error("ORDER_DATE_MISMATCH", "Order date must equal scenario_config.current_date.", component_id=component_id, plan_id=plan_id)
            offer = self._match_offer(offers, line)
            expected = line.expected_delivery_date
            if offer is None:
                sink.error("DELIVERY_DATE_MISMATCH", "Expected delivery must match an independently reconstructed active standard or air shipping lead.", component_id=component_id, plan_id=plan_id)
            pcb = self._concept("printed_circuit_board_component", requirement.component) is EvidenceStatus.PASS
            buffer = self.receiving_buffer_days if requirement.component.is_hazardous or pcb else 0
            if line.material_available_date != expected + timedelta(days=buffer):
                sink.error("MATERIAL_DATE_MISMATCH", "Feasibility material date does not match delivery plus receiving buffer.", component_id=component_id, plan_id=plan_id)
            eligible, _assumption = self._hard_eligible(
                snapshot,
                requirement,
                supplier,
                catalog,
                decision.evidence_contract,
                allow_contract_blocked=evidence_contract_diagnostic,
            )
            if not eligible:
                sink.error(
                    "SUPPLIER_INELIGIBLE",
                    "Plan uses a supplier failing proven ASL, certification, rating, or scoped incumbency gates.",
                    component_id=component_id,
                    plan_id=plan_id,
                )
            if line.quantity > derived_u.get(line.supplier_id, ZERO):
                sink.error("UPPER_BOUND_DERIVATION", "Line exceeds the independently derived sound U bound.", component_id=component_id, plan_id=plan_id)
            if offer is not None and offer.shipping_method == "air freight" and plan.disposition.writes_purchase_order:
                air_approvals = (
                    *self._rules(snapshot.configuration.current_date, "air_freight_individual_approval"),
                    *self._rules(snapshot.configuration.current_date, "air_freight_period_spend_cap"),
                )
                missing = tuple(item.rule_id for item in air_approvals if item.rule_id not in self.approved_rule_ids)
                if missing:
                    sink.error("AIR_FREIGHT_UNAPPROVED", "Air freight cannot execute without every active route and period approval.", component_id=component_id, plan_id=plan_id, rule_ids=missing)
            for allocation in line.bucket_allocations:
                if allocation.due_date not in dict(requirement.bucket_quantities):
                    sink.error("UNKNOWN_ALLOCATION_BUCKET", "Line allocation references a non-demand deadline.", component_id=component_id, plan_id=plan_id)
                    continue
                if offer is None:
                    continue
                condition = offer.condition_for(allocation.due_date)
                if offer.international and condition in {None, "shut"}:
                    sink.error("EXCEPTION_SCOPE", "International quantity was allocated where no §3 predicate is true.", component_id=component_id, plan_id=plan_id)
                if offer.international and not any("condition_" in item for item in allocation.exception_ids):
                    sink.error("INTERNATIONAL_JUSTIFICATION_MISSING", "International allocation lacks its documented, bucket-scoped §3 justification.", component_id=component_id, plan_id=plan_id)
                if offer.shipping_method == "air freight" and not any("air_freight_authorization" in item for item in allocation.exception_ids):
                    sink.error("AIR_EXCEPTION_SCOPE", "Air-freight allocation lacks its active memo authorization and bucket predicate.", component_id=component_id, plan_id=plan_id)
                for exception_id in allocation.exception_ids:
                    exception_totals[exception_id] += allocation.quantity
                    exception_dates[exception_id].update(offer.allowed_buckets)
                    if "condition_" in exception_id and not exception_id.endswith(f"condition_{condition}"):
                        sink.error("EXCEPTION_PREDICATE_MISMATCH", "Allocation exception label disagrees with the recomputed bucket predicate.", component_id=component_id, plan_id=plan_id)
            expected_order_approvals = {
                parameter.rule_id
                for parameter in approval_parameters
                if line.line_total
                > parameter.amount_exceeds
                and parameter.rule_id not in self.approved_rule_ids
            }
            represented_order_approvals = (
                set(line.approval_rule_ids) & order_value_rule_ids
            )
            if plan.disposition.writes_purchase_order and expected_order_approvals:
                sink.error(
                    "UNAPPROVED_ORDER_VALUE",
                    "Executable line exceeds one or more approval thresholds without runtime approval evidence.",
                    component_id=component_id,
                    plan_id=plan_id,
                    rule_ids=tuple(sorted(expected_order_approvals)),
                )
            if (
                plan.disposition is PlanDisposition.RECOMMEND_APPROVAL
                and represented_order_approvals != expected_order_approvals
            ):
                sink.error(
                    "ORDER_VALUE_APPROVAL_CLASSIFICATION",
                    "Approval proposal does not carry exactly the active thresholds crossed by this line.",
                    component_id=component_id,
                    plan_id=plan_id,
                    rule_ids=tuple(
                        sorted(
                            represented_order_approvals
                            ^ expected_order_approvals
                        )
                    ),
                )
        for exception_id, quantity in exception_totals.items():
            allowance = sum((requirement.bucket_shortage(due) for due in exception_dates[exception_id]), ZERO)
            if (
                exception_id.endswith("condition_b")
                and secondary_rule_id not in plan.relaxed_rule_ids
            ):
                allowance += forced_exception_surplus
            if quantity > allowance:
                sink.error("EXCEPTION_AGGREGATE_CAP", "Exception quantity exceeds the aggregate net shortage of qualifying buckets.", component_id=component_id, plan_id=plan_id)
        total_quantity = sum((line.quantity for line in plan.lines), ZERO)
        independently_covered = min(requirement.eventual_gap, total_quantity)
        independent_recovery_demand = self._recovery_demand(
            requirement,
            offers,
        )
        if plan.net_requirement != requirement.eventual_gap or plan.eventual_covered_quantity != independently_covered or plan.residual_gap != requirement.eventual_gap - independently_covered:
            sink.error("PLAN_COVERAGE_MISMATCH", "Plan requirement, eventual coverage, or residual does not match exact source arithmetic.", component_id=component_id, plan_id=plan_id)
        if plan.recovery_demand != independent_recovery_demand:
            sink.error(
                "RECOVERY_DEMAND_MISMATCH",
                "Recovery demand was not independently reconstructed from strictly earlier eligible routes.",
                component_id=component_id,
                plan_id=plan_id,
            )
        recovery_capacity = self._plan_recovery_capacity(
            requirement,
            plan,
            offers,
        )
        if plan.recovery_quantity > recovery_capacity:
            sink.error(
                "RECOVERY_QUANTITY_MISMATCH",
                "Recovery quantity exceeds strictly improving quantity supported by the plan's actual route allocations.",
                component_id=component_id,
                plan_id=plan_id,
            )
        if plan.total_cost != sum((line.quantity * catalogs[(line.component_id, line.supplier_id)].unit_price for line in plan.lines if (line.component_id, line.supplier_id) in catalogs), ZERO):
            sink.error("PLAN_COST_MISMATCH", "Plan total cost does not equal source catalog price times quantity.", component_id=component_id, plan_id=plan_id)
        if plan.minimum_compliant_total is not None:
            forced = max(ZERO, plan.minimum_compliant_total - plan.net_requirement)
            recovery_headroom = max(
                ZERO,
                plan.recovery_quantity - forced,
            )
            discretionary = max(
                ZERO,
                total_quantity
                - plan.minimum_compliant_total
                - recovery_headroom,
            )
            if (
                plan.forced_surplus != forced
                or plan.discretionary_surplus != discretionary
            ):
                sink.error("SURPLUS_SPLIT_MISMATCH", "Forced and discretionary surplus were not independently split at solve-Q minimum.", component_id=component_id, plan_id=plan_id)
            cap = self.autonomy.max_surplus_fraction * (
                plan.net_requirement + independent_recovery_demand
            )
            if self.autonomy.max_surplus_units is not None:
                cap = min(cap, self.autonomy.max_surplus_units)
            if plan.discretionary_surplus > cap and plan.disposition.writes_purchase_order:
                sink.error("AUTONOMY_SURPLUS_EXCEEDED", "Executable plan exceeds the inclusive discretionary-surplus bound.", component_id=component_id, plan_id=plan_id)
        evidence_vector_diagnostic = (
            evidence_contract_diagnostic and len(plan.objective_vector) == 2
        )
        exact_objective = (
            (plan.residual_gap, total_quantity)
            if evidence_vector_diagnostic
            else
            (plan.residual_gap, plan.total_cost)
            if baseline_vector_diagnostic
            else self._objective(snapshot, requirement, plan, offers)
        )
        if (
            not baseline_vector_diagnostic
            and not evidence_vector_diagnostic
            and plan.unit_late_days
            != exact_objective[len(requirement.bucket_quantities)]
        ):
            sink.error("UNIT_LATE_DAYS_MISMATCH", "Unit-late-days was not exactly recomputed from material availability and allocations.", component_id=component_id, plan_id=plan_id)
        allocation_rule_ids = {
            rule.rule_id
            for rule in self.registry.active_rules(
                snapshot.configuration.current_date
            )
            if isinstance(rule.data.get("constraint"), Mapping)
            and rule.data["constraint"].get("kind")
            in {"minimum_secondary_fraction", "supplier_volume_cap"}
        }
        compliance_cost_diagnostic = (
            not plan.disposition.writes_purchase_order
            and bool(set(plan.relaxed_rule_ids) & allocation_rule_ids)
            and len(plan.objective_vector) == 2
        )
        if evidence_vector_diagnostic:
            if plan.objective_vector != exact_objective:
                sink.error(
                    "OBJECTIVE_VECTOR_MISMATCH",
                    "Evidence-contract diagnostic does not match its independently recomputed quantity-calibration vector.",
                    component_id=component_id,
                    plan_id=plan_id,
                )
        elif compliance_cost_diagnostic:
            diagnostic_objective = (
                max(ZERO, requirement.eventual_gap - total_quantity),
                plan.total_cost,
            )
            if plan.objective_vector != diagnostic_objective:
                sink.error("OBJECTIVE_VECTOR_MISMATCH", "Compliance-cost diagnostic does not match the independently recomputed baseline vector.", component_id=component_id, plan_id=plan_id)
        elif plan.objective_vector != exact_objective:
            sink.error("OBJECTIVE_VECTOR_MISMATCH", "Plan objective vector differs from the validator's exact Decimal vector.", component_id=component_id, plan_id=plan_id)
        secondary = self._minimum_secondary(snapshot, requirement.component)
        secondary_relaxed = (
            not plan.disposition.writes_purchase_order
            and secondary_rule_id is not None
            and secondary_rule_id in plan.relaxed_rule_ids
        )
        if secondary is not None and not secondary_relaxed:
            quantities: dict[str, Decimal] = defaultdict(Decimal)
            for line in plan.lines:
                quantities[line.supplier_id] += line.quantity
            eligible_count = len({offer.supplier.supplier_id for offer in offers})
            secondary_rule_ids = {
                rule.rule_id
                for rule in self._rules(
                    snapshot.configuration.current_date,
                    "minimum_secondary_fraction",
                )
            }
            represented_secondary_diagnostic = (
                not plan.disposition.writes_purchase_order
                and bool(set(plan.relaxed_rule_ids) & secondary_rule_ids)
            )
            if eligible_count >= 2:
                maximum = (Decimal("1") - secondary) * total_quantity
                if (
                    any(value > maximum for value in quantities.values())
                    and not represented_secondary_diagnostic
                ):
                    sink.error("SECONDARY_ALLOCATION_VIOLATION", "Per-order minimum-secondary allocation is violated.", component_id=component_id, plan_id=plan_id)
            elif (
                plan.disposition.writes_purchase_order
                or not represented_secondary_diagnostic
            ):
                sink.error("SECONDARY_ALLOCATION_UNSATISFIABLE", "An executable plan cannot silently drop a minimum-secondary rule with fewer than two eligible suppliers.", component_id=component_id, plan_id=plan_id)
        named_deviation = self._named_deviation(snapshot, requirement.component, {supplier: sum((line.quantity for line in plan.lines if line.supplier_id == supplier), ZERO) for supplier in {line.supplier_id for line in plan.lines}})
        if named_deviation > ZERO:
            if plan.disposition.writes_purchase_order:
                sink.error("NAMED_PRIMARY_DEVIATION", "Executable plan lies outside named-primary membership.", component_id=component_id, plan_id=plan_id)
            elif plan.disposition is not PlanDisposition.DECISION_REQUIRED:
                sink.error("NAMED_PRIMARY_DISPOSITION", "A named-primary-deviating alternative must be DECISION_REQUIRED.", component_id=component_id, plan_id=plan_id)
        boundary = _rating("B")
        assert boundary is not None
        uses_below_b = any(
            (rating := _rating(suppliers[line.supplier_id].sustainability_rating))
            is not None
            and rating < boundary
            for line in plan.lines
            if line.supplier_id in suppliers
        )
        if plan.disposition.writes_purchase_order and uses_below_b:
            no_alternative = self.independently_check_b_or_better(
                snapshot,
                component_id,
                decision.evidence_contract,
            )
            if (
                no_alternative.status is not SolverStatus.INFEASIBLE
                or not no_alternative.certificate_complete
            ):
                sink.error(
                    "BELOW_B_NO_ALTERNATIVE_UNPROVEN",
                    "An executable below-B route lacks an independently completed no-alternative proof.",
                    component_id=component_id,
                    plan_id=plan_id,
                )
        self._check_evidence(snapshot, decision, plan, sink)

    def _check_time_phased_ledger(
        self,
        decision: DecisionRecord,
        requirement: _SourceRequirement,
        offers: Sequence[_Offer],
        sink: _IssueSink,
    ) -> None:
        positions = {
            item.due_date: item
            for item in decision.supply_ledger.deadline_positions
        }
        segments = self._supply_segments(requirement)
        for bucket_index, (due, _quantity) in enumerate(
            requirement.bucket_quantities
        ):
            position = positions.get(due)
            if position is None:
                sink.error(
                    "DEADLINE_POSITION_MISSING",
                    "Supply ledger omits a source demand deadline.",
                    component_id=decision.component_id,
                )
                continue
            on_time = requirement.on_time_supply(due)
            gap = max(ZERO, requirement.cumulative_demand(due) - on_time)
            late_segments = tuple(
                item
                for item in segments
                if item.bucket_index == bucket_index
                and item.committed
                and item.material_available is not None
                and item.material_available > due
            )
            committed_late = sum(
                (item.quantity for item in late_segments), ZERO
            )
            committed_late_days = sum(
                (
                    item.quantity
                    * Decimal((item.material_available - due).days)
                    for item in late_segments
                    if item.material_available is not None
                ),
                ZERO,
            )
            recoverable = sum(
                (
                    item.quantity
                    for item in late_segments
                    if item.material_available is not None
                    and any(
                        due in offer.allowed_buckets
                        and offer.material_available < item.material_available
                        for offer in offers
                    )
                ),
                ZERO,
            )
            if (
                position.cumulative_demand
                != requirement.cumulative_demand(due)
                or position.on_time_supply != on_time
                or position.on_time_gap != gap
                or position.committed_late_quantity != committed_late
                or position.committed_unit_late_days != committed_late_days
                or position.recoverable_gap != recoverable
            ):
                sink.error(
                    "TIME_PHASED_LEDGER_MISMATCH",
                    "On-time gap, committed lateness, or strict-improvement recovery differs from independent reconstruction.",
                    component_id=decision.component_id,
                )

    def _check_evidence(self, snapshot: ScenarioSnapshot, decision: DecisionRecord, plan: CandidatePlan, sink: _IssueSink) -> None:
        active = {item.rule_id: item for item in self.registry.active_rules(snapshot.configuration.current_date)}
        evidence_contract_diagnostic = _is_evidence_contract_diagnostic(plan)
        for evidence in (*decision.evidence, *plan.evidence):
            if evidence.rule_id not in active and not evidence.rule_id.startswith(_SYNTHETIC_RULE_PREFIXES):
                sink.error("INACTIVE_RULE_CITATION", "Evidence cites a policy rule inactive on the scenario date.", component_id=decision.component_id, plan_id=plan.plan_id, rule_ids=(evidence.rule_id,))
            if plan.disposition.writes_purchase_order and evidence.severity is RuleSeverity.HARD and evidence.status is EvidenceStatus.FAIL:
                sink.error("PROVEN_POLICY_VIOLATION", "A proven hard policy failure cannot execute.", component_id=decision.component_id, plan_id=plan.plan_id, rule_ids=(evidence.rule_id,))
            if plan.disposition.writes_purchase_order and evidence.severity is RuleSeverity.HARD and evidence.status is EvidenceStatus.UNKNOWN:
                permitted = evidence.scope is EvidenceScope.RULE and evidence.contract_disposition is PlanDisposition.EXECUTE_WITH_ASSUMPTION and plan.disposition is PlanDisposition.EXECUTE_WITH_ASSUMPTION
                if not permitted:
                    sink.error("UNLICENSED_EVIDENCE", "Hard unknown evidence is not licensed for execution by the active contract.", component_id=decision.component_id, plan_id=plan.plan_id, rule_ids=(evidence.rule_id,))
            if (
                evidence_contract_diagnostic
                and evidence in plan.evidence
                and evidence.severity is RuleSeverity.HARD
                and evidence.status is EvidenceStatus.FAIL
            ):
                sink.error(
                    "EVIDENCE_DIAGNOSTIC_PROVEN_FAILURE",
                    "An evidence-contract diagnostic may preserve UNKNOWN evidence but cannot treat a proven hard failure as a potential route.",
                    component_id=decision.component_id,
                    plan_id=plan.plan_id,
                    rule_ids=(evidence.rule_id,),
                )
        rolling_applies = any(
            rule.evidence_basis == EvidenceBasis.ROLLING_WINDOW.value
            for rule in self.registry.active_rules(snapshot.configuration.current_date)
        )
        if rolling_applies and plan.disposition.writes_purchase_order:
            if decision.evidence_contract is EvidenceContract.PRODUCTION:
                sink.error("ROLLING_HISTORY_UNLICENSED", "Production contract cannot execute without licensed rolling history.", component_id=decision.component_id, plan_id=plan.plan_id)
            elif plan.disposition is not PlanDisposition.EXECUTE_WITH_ASSUMPTION:
                sink.error("EVIDENCE_CONTRACT_DISPOSITION", "Benchmark rolling-history unknown requires EXECUTE_WITH_ASSUMPTION.", component_id=decision.component_id, plan_id=plan.plan_id)

    def _check_requirement_evidence_disposition(
        self,
        snapshot: ScenarioSnapshot,
        decision: DecisionRecord,
        requirement: _SourceRequirement,
        sink: _IssueSink,
    ) -> None:
        applicable_rolling = {
            rule.rule_id: rule
            for rule in self.registry.active_rules(snapshot.configuration.current_date)
            if rule.evidence_basis == EvidenceBasis.ROLLING_WINDOW.value
            and self._selector_status(rule, requirement.component)
            is not EvidenceStatus.FAIL
        }
        for rule in tuple(applicable_rolling.values()):
            if (
                self._selector_status(rule, requirement.component)
                is not EvidenceStatus.PASS
            ):
                continue
            precedence = rule.data.get("precedence")
            if not isinstance(precedence, Mapping):
                continue
            for relation in ("supersedes", "outranks"):
                for target in precedence.get(relation, ()):
                    applicable_rolling.pop(str(target), None)
        expected_rolling_ids = set(applicable_rolling)
        actual_rolling = {
            item.rule_id: item
            for item in decision.evidence
            if item.basis is EvidenceBasis.ROLLING_WINDOW
        }
        if (
            decision.evidence_contract is EvidenceContract.PRODUCTION
            or actual_rolling
        ) and set(actual_rolling) != expected_rolling_ids:
            sink.error(
                "ROLLING_EVIDENCE_PROPAGATION",
                "Requirement evidence does not preserve every in-scope rolling-window rule.",
                component_id=decision.component_id,
                rule_ids=tuple(sorted(set(actual_rolling) ^ expected_rolling_ids)),
            )
        expected_disposition = (
            PlanDisposition.DECISION_REQUIRED
            if decision.evidence_contract is EvidenceContract.PRODUCTION
            else PlanDisposition.EXECUTE_WITH_ASSUMPTION
        )
        for evidence in actual_rolling.values():
            if (
                evidence.status is not EvidenceStatus.UNKNOWN
                or evidence.contract_disposition is not expected_disposition
            ):
                sink.error(
                    "EVIDENCE_CONTRACT_DISPOSITION",
                    "Rolling-window UNKNOWN evidence does not carry the active contract's required disposition.",
                    component_id=decision.component_id,
                    rule_ids=(evidence.rule_id,),
                )

        all_evidence = tuple(decision.evidence) + tuple(
            evidence
            for plan in decision.alternatives
            for evidence in plan.evidence
        )
        blockers = tuple(
            item
            for item in all_evidence
            if item.severity is RuleSeverity.HARD
            and item.status is EvidenceStatus.UNKNOWN
            and item.contract_disposition is PlanDisposition.DECISION_REQUIRED
        )
        potential_routes = self._offers(
            snapshot,
            requirement,
            decision.evidence_contract,
            include_unapproved=True,
            include_evidence_blocked=True,
        )
        evidence_blocked = bool(blockers and potential_routes)
        if evidence_blocked:
            if AlertCategory.DECISION_REQUIRED not in decision.alert_categories:
                sink.error(
                    "EVIDENCE_DECISION_ALERT_MISSING",
                    "Evidence-blocked requirements require a component-specific DECISION_REQUIRED alert.",
                    component_id=decision.component_id,
                )
            if AlertCategory.EVIDENCE_CONTRACT not in decision.alert_categories:
                sink.error(
                    "EVIDENCE_CONTRACT_ALERT_MISSING",
                    "Evidence-blocked requirements require an EVIDENCE_CONTRACT remediation alert.",
                    component_id=decision.component_id,
                )
            if (
                AlertCategory.ASSUMPTION in decision.alert_categories
                and not (
                    decision.economic_autonomy is not None
                    and decision.economic_autonomy.provisional
                )
            ):
                sink.error(
                    "PRODUCTION_EVIDENCE_AS_ASSUMPTION",
                    "Unavailable production facts cannot be rendered as assumptions relied upon.",
                    component_id=decision.component_id,
                )
            if AlertCategory.NO_ELIGIBLE_SUPPLIER in decision.alert_categories:
                sink.error(
                    "EVIDENCE_BLOCKED_AS_INELIGIBLE",
                    "Missing production evidence cannot be classified as NO_ELIGIBLE_SUPPLIER.",
                    component_id=decision.component_id,
                )
            if (
                decision.residual_gap > ZERO
                and decision.requirement_state.resolution
                is not ResolutionStatus.UNRESOLVED
            ):
                sink.error(
                    "EVIDENCE_BLOCKED_REQUIREMENT_STATE",
                    "A positive evidence-blocked residual must remain UNRESOLVED.",
                    component_id=decision.component_id,
                )
            if decision.residual_gap > ZERO and not any(
                _is_evidence_contract_diagnostic(plan)
                for plan in decision.alternatives
            ):
                sink.error(
                    "EVIDENCE_DIAGNOSTIC_MISSING",
                    "A positive evidence-blocked requirement lacks its non-executable solver diagnostic.",
                    component_id=decision.component_id,
                )

    def _check_capacity(self, snapshot: ScenarioSnapshot, decision: DecisionRecord, sink: _IssueSink) -> None:
        subject = self._release_subject(snapshot, next(item for item in snapshot.components if item.component_id == decision.component_id))
        plans = tuple(item for item in (decision.selected_plan, *decision.alternatives) if item is not None)
        positive = False
        if subject is not None and subject.resolved and subject.supplier_id not in self.numeric_capacity_by_supplier:
            positive = any(any(line.supplier_id == subject.supplier_id and line.quantity > ZERO for line in plan.lines) for plan in plans)
        has_alert = AlertCategory.CAPACITY_UNKNOWN in decision.alert_categories
        if positive and not has_alert:
            sink.error("CAPACITY_UNKNOWN_MISSING", "Positive allocation to the capacity-release subject lacks CAPACITY_UNKNOWN disclosure.", component_id=decision.component_id)
        if has_alert and not positive:
            sink.error("CAPACITY_UNKNOWN_UNSCOPED", "CAPACITY_UNKNOWN may apply only to a positive allocation to the release-condition subject without numeric capacity.", component_id=decision.component_id)
        if decision.selected_plan is None and positive and set(decision.alert_categories) <= {AlertCategory.CAPACITY_UNKNOWN, AlertCategory.RUN_ACCOUNTING}:
            sink.error("CAPACITY_UNKNOWN_DISPOSITIVE", "Capacity uncertainty is disclosure-only and cannot by itself withhold a plan.", component_id=decision.component_id)

    def _check_solver_results(
        self,
        snapshot: ScenarioSnapshot,
        decision: DecisionRecord,
        results: Sequence[SolverResult],
        requirement: _SourceRequirement,
        sink: _IssueSink,
    ) -> bool:
        by_kind: dict[SolveKind, list[SolverResult]] = defaultdict(list)
        for result in results:
            if result.component_id == decision.component_id:
                by_kind[result.solve_kind].append(result)
        selected = decision.selected_plan
        executable_results = by_kind.get(SolveKind.EXECUTABLE, ())
        certified_plan = selected
        if selected is None and len(executable_results) == 1:
            claimed_plan = executable_results[0].candidate_plan
            if (
                claimed_plan is not None
                and claimed_plan.disposition
                is PlanDisposition.RECOMMEND_APPROVAL
            ):
                matching = tuple(
                    item
                    for item in decision.alternatives
                    if item.plan_id == claimed_plan.plan_id
                    and item.disposition
                    is PlanDisposition.RECOMMEND_APPROVAL
                )
                if len(matching) == 1 and matching[0] == claimed_plan:
                    certified_plan = matching[0]
                else:
                    sink.error(
                        "APPROVAL_PROPOSAL_MISMATCH",
                        "The certified solve-1 plan is not preserved exactly once as a complete approval proposal.",
                        component_id=decision.component_id,
                    )
        verified = True
        if decision.requirement_state.resolution is ResolutionStatus.INFEASIBLE:
            unlicensed = any(
                item.status is EvidenceStatus.UNKNOWN
                and item.severity is RuleSeverity.HARD
                and not (
                    item.scope is EvidenceScope.RULE
                    and item.contract_disposition is PlanDisposition.EXECUTE_WITH_ASSUMPTION
                    and decision.evidence_contract is EvidenceContract.BENCHMARK
                )
                for item in decision.evidence
            )
            if unlicensed:
                sink.error("UNLICENSED_INFEASIBILITY", "Approval- or evidence-blocked requirements are UNRESOLVED, never INFEASIBLE.", component_id=decision.component_id)
                verified = False
            own_feasibility = self.independently_solve(snapshot, decision, SolveKind.QUANTITY_CALIBRATION)
            if (
                own_feasibility.status
                not in {SolverStatus.OPTIMAL, SolverStatus.INFEASIBLE}
                or not own_feasibility.certificate_complete
            ):
                sink.error(
                    "INDEPENDENT_SOLVE_UNPROVEN",
                    "Validator could not complete the independent infeasibility solve.",
                    component_id=decision.component_id,
                )
                return False
            independently_infeasible = (
                own_feasibility.status is SolverStatus.INFEASIBLE
                or (
                    own_feasibility.status is SolverStatus.OPTIMAL
                    and bool(own_feasibility.objective_vector)
                    and own_feasibility.objective_vector[0] > ZERO
                )
            )
            if not independently_infeasible:
                sink.error("INFEASIBILITY_NOT_REPRODUCED", "Validator did not reproduce a positive eventual remainder under completed hard-rule search.", component_id=decision.component_id)
                verified = False
        if certified_plan is not None:
            for kind in (SolveKind.QUANTITY_CALIBRATION, SolveKind.BASELINE, SolveKind.EXECUTABLE):
                matches = by_kind.get(kind, ())
                if len(matches) != 1:
                    sink.error("SOLVER_RESULT_CARDINALITY", f"Executable decision requires exactly one {kind.value} result.", component_id=decision.component_id)
                    verified = False
                    continue
                result = matches[0]
                if not result.is_certified_optimal or not result.exact_post_validated:
                    sink.error("SOLVER_UNPROVEN", "Feasible incumbent, timeout, gap, or incomplete stage cannot support an executable action or approval proposal.", component_id=decision.component_id)
                    verified = False
            if not verified:
                return False
            q_claim = by_kind[SolveKind.QUANTITY_CALIBRATION][0]
            baseline_claim = by_kind[SolveKind.BASELINE][0]
            executable_claim = by_kind[SolveKind.EXECUTABLE][0]
            q_own = self.independently_solve(snapshot, decision, SolveKind.QUANTITY_CALIBRATION)
            baseline_own = self.independently_solve(snapshot, decision, SolveKind.BASELINE)
            executable_own = self.independently_solve(snapshot, decision, SolveKind.EXECUTABLE)
            independently_proven: dict[SolveKind, bool] = {}
            for name, claim, own in (
                ("solve Q", q_claim, q_own),
                ("solve 0", baseline_claim, baseline_own),
                ("solve 1", executable_claim, executable_own),
            ):
                proven = (
                    own.status is SolverStatus.OPTIMAL
                    and own.certificate_complete
                )
                independently_proven[claim.solve_kind] = proven
                if not proven:
                    sink.error("INDEPENDENT_SOLVE_UNPROVEN", f"Validator could not complete independent {name} solve.", component_id=decision.component_id)
                    verified = False
                elif claim.objective_vector != own.objective_vector:
                    sink.error("SOLVER_OBJECTIVE_DISAGREEMENT", f"Planner and independent {name} objective vectors disagree.", component_id=decision.component_id)
                    verified = False
            if independently_proven.get(SolveKind.QUANTITY_CALIBRATION) and (
                q_own.minimum_compliant_total
                != certified_plan.minimum_compliant_total
                or q_claim.minimum_compliant_total
                != q_own.minimum_compliant_total
            ):
                sink.error("CALIBRATION_MISMATCH", "Persisted minimum_compliant_total differs from independent solve Q.", component_id=decision.component_id, plan_id=certified_plan.plan_id)
                verified = False
            if independently_proven.get(SolveKind.BASELINE) and (
                baseline_own.cheapest_covering_cost
                != certified_plan.cheapest_covering_cost
                or baseline_claim.cheapest_covering_cost
                != baseline_own.cheapest_covering_cost
            ):
                sink.error("BASELINE_COST_MISMATCH", "Persisted cheapest_covering_cost differs from independent solve 0's certified coverage target.", component_id=decision.component_id, plan_id=certified_plan.plan_id)
                verified = False
            if (
                independently_proven.get(SolveKind.EXECUTABLE)
                and executable_own.recovery_quantity
                != certified_plan.recovery_quantity
            ):
                sink.error(
                    "RECOVERY_QUANTITY_MISMATCH",
                    "Persisted recovery quantity differs from the independent executable solve's classified recovery allocation.",
                    component_id=decision.component_id,
                    plan_id=certified_plan.plan_id,
                )
                verified = False
            if (
                independently_proven.get(SolveKind.BASELINE)
                and certified_plan.cheapest_covering_cost is not None
                and certified_plan.total_cost - certified_plan.cheapest_covering_cost
                > self.autonomy.max_excess_cost_usd
            ):
                sink.error("AUTONOMY_COST_EXCEEDED", "Certified plan exceeds the excess-cost autonomy bound.", component_id=decision.component_id, plan_id=certified_plan.plan_id)
                verified = False
            exact = self._objective(snapshot, requirement, certified_plan, self._offers(snapshot, requirement, decision.evidence_contract))
            if executable_claim.candidate_plan is None or executable_claim.candidate_plan.plan_id != certified_plan.plan_id:
                sink.error("EXECUTABLE_PLAN_MISMATCH", "Solve-1 candidate differs from the selected action or complete approval proposal.", component_id=decision.component_id)
                verified = False
            if independently_proven.get(SolveKind.EXECUTABLE) and (
                executable_claim.objective_vector != exact
                or executable_own.objective_vector != exact
            ):
                sink.error("SUBOPTIMAL_INCUMBENT", "Certified plan is not the independently reproduced lexicographic optimum.", component_id=decision.component_id, plan_id=certified_plan.plan_id)
                verified = False
        else:
            if decision.requirement_state.resolution is ResolutionStatus.INFEASIBLE:
                certified = tuple(
                    item
                    for item in executable_results
                    if item.status is SolverStatus.INFEASIBLE
                    and all(
                        stage.status is SolverStatus.INFEASIBLE
                        and stage.certificate_complete
                        and not stage.hit_resource_limit
                        for stage in item.stage_results
                    )
                )
                if len(certified) != 1:
                    sink.error("INFEASIBILITY_CERTIFICATE_MISSING", "INFEASIBLE requires exactly one completed planner infeasibility certificate.", component_id=decision.component_id)
                    verified = False
            if any(item.status in {SolverStatus.TIMEOUT, SolverStatus.RESOURCE_LIMIT, SolverStatus.FEASIBLE_INCUMBENT, SolverStatus.UNRESOLVED} for item in executable_results):
                if decision.requirement_state.resolution is ResolutionStatus.INFEASIBLE:
                    sink.error("UNPROVEN_INFEASIBILITY", "Timeout or merely feasible evidence must be UNRESOLVED, never INFEASIBLE.", component_id=decision.component_id)
                    verified = False
        return verified

    def _check_route_quarantine(
        self,
        snapshot: ScenarioSnapshot,
        decisions: Sequence[DecisionRecord],
        sink: _IssueSink,
    ) -> None:
        """Independently reconstruct the route set removed by source quarantine."""

        issues = tuple(snapshot.route_input_issues)
        if any(not isinstance(item, RouteInputIssue) for item in issues):
            sink.error(
                "INVALID_ROUTE_ISSUE",
                "Snapshot route-input issues are not typed quarantine records.",
            )
            return
        supplier_ids = {item.supplier_id for item in snapshot.suppliers}
        component_ids = {item.component_id for item in snapshot.components}
        catalog_keys = {
            (item.supplier_id, item.component_id)
            for item in snapshot.catalog_lines
        }
        catalog_components: dict[str, set[str]] = defaultdict(set)
        for supplier_id, component_id in catalog_keys:
            catalog_components[supplier_id].add(component_id)

        quarantined_suppliers = _quarantined_supplier_ids(snapshot)
        quarantined_catalogs = _quarantined_catalog_keys(snapshot)
        for issue in issues:
            if any(
                component_id not in component_ids
                for component_id in issue.affected_component_ids
            ):
                sink.error(
                    "QUARANTINE_COMPONENT_UNKNOWN",
                    "A route-input issue names an affected component absent from master data.",
                )
            if issue.blast_radius is RouteQuarantineScope.SUPPLIER_ALL_ROUTES:
                if issue.supplier_id in supplier_ids:
                    sink.error(
                        "QUARANTINED_SUPPLIER_PRESENT",
                        "A supplier-wide malformed source row remained eligible as a typed supplier.",
                    )
                if not catalog_components.get(issue.supplier_id, set()).issubset(
                    set(issue.affected_component_ids)
                ):
                    sink.error(
                        "QUARANTINE_BLAST_RADIUS_MISMATCH",
                        "Supplier-wide quarantine does not cover every reconstructable catalog route.",
                    )
            else:
                key = (issue.supplier_id, issue.component_id)
                if key in catalog_keys:
                    sink.error(
                        "QUARANTINED_CATALOG_PRESENT",
                        "A malformed catalog offer remained available as a typed source offer.",
                        component_id=issue.component_id,
                    )

        for supplier_id, component_id in catalog_keys:
            if (
                supplier_id not in supplier_ids
                and supplier_id not in quarantined_suppliers
            ):
                sink.error(
                    "UNACCOUNTED_CATALOG_SUPPLIER",
                    "A catalog offer lacks a typed supplier and no supplier-wide issue explains it.",
                    component_id=component_id,
                )

        for decision in decisions:
            relevant = tuple(
                issue
                for issue in issues
                if decision.component_id in issue.affected_component_ids
            )
            if not relevant:
                continue
            if AlertCategory.DATA_QUALITY not in decision.alert_categories:
                sink.error(
                    "QUARANTINE_ALERT_MISSING",
                    "Every demanded component touched by route quarantine requires DATA_QUALITY.",
                    component_id=decision.component_id,
                )
            plans = tuple(
                plan
                for plan in (decision.selected_plan, *decision.alternatives)
                if plan is not None
            )
            for plan in plans:
                for line in plan.lines:
                    if line.supplier_id in quarantined_suppliers or (
                        line.supplier_id,
                        line.component_id,
                    ) in quarantined_catalogs:
                        sink.error(
                            "QUARANTINED_ROUTE_SELECTED",
                            "A managed plan uses a quarantined source route.",
                            component_id=decision.component_id,
                            plan_id=plan.plan_id,
                        )
            if decision.residual_gap > ZERO:
                if decision.requirement_state.resolution is not ResolutionStatus.UNRESOLVED:
                    sink.error(
                        "QUARANTINE_INFEASIBILITY_CLAIM",
                        "Route quarantine prevents a proven infeasibility "
                        "classification; the residual must remain UNRESOLVED.",
                        component_id=decision.component_id,
                    )
                if AlertCategory.NO_ELIGIBLE_SUPPLIER in decision.alert_categories:
                    sink.error(
                        "QUARANTINE_FALSE_NO_ELIGIBLE",
                        "Quarantined commercial evidence cannot support NO_ELIGIBLE_SUPPLIER.",
                        component_id=decision.component_id,
                    )

    def validate(
        self,
        snapshot: ScenarioSnapshot,
        decisions: Sequence[DecisionRecord],
        solver_results: Sequence[SolverResult],
        /,
    ) -> ValidationResult:
        if not isinstance(snapshot, ScenarioSnapshot):
            raise TypeError("snapshot must be ScenarioSnapshot")
        decision_tuple = tuple(decisions)
        result_tuple = tuple(solver_results)
        if any(not isinstance(item, DecisionRecord) for item in decision_tuple):
            raise TypeError("decisions contains an invalid item")
        if any(not isinstance(item, SolverResult) for item in result_tuple):
            raise TypeError("solver_results contains an invalid item")
        sink = _IssueSink([])
        source = self._source_requirements(snapshot, sink)
        by_component: dict[str, list[DecisionRecord]] = defaultdict(list)
        requirement_ids: set[str] = set()
        executable_action_keys: set[tuple[object, ...]] = set()
        solver_verified = True
        for decision in decision_tuple:
            by_component[decision.component_id].append(decision)
            if decision.requirement_id in requirement_ids:
                sink.error("DUPLICATE_REQUIREMENT", "Requirement ID is not unique.", component_id=decision.component_id)
            requirement_ids.add(decision.requirement_id)
            requirement = source.get(decision.component_id)
            if requirement is None:
                sink.error("UNKNOWN_REQUIREMENT_COMPONENT", "Decision has no independently reconstructed source requirement.", component_id=decision.component_id)
                continue
            if self.policy_parameters is not None:
                expected_autonomy = self._parameters(
                    snapshot.configuration.current_date
                ).economic_autonomy
                if decision.economic_autonomy != expected_autonomy:
                    sink.error(
                        "ECONOMIC_AUTONOMY_DISCLOSURE_MISMATCH",
                        "Decision output does not disclose the active typed economic-autonomy parameters.",
                        component_id=decision.component_id,
                    )
                if (
                    expected_autonomy.provisional
                    and AlertCategory.ASSUMPTION not in decision.alert_categories
                ):
                    sink.error(
                        "PROVISIONAL_AUTONOMY_ALERT_MISSING",
                        "Provisional economic-autonomy parameters require an ASSUMPTION alert.",
                        component_id=decision.component_id,
                    )
            self._check_decision_facts(snapshot, decision, requirement, sink)
            self._check_requirement_evidence_disposition(
                snapshot,
                decision,
                requirement,
                sink,
            )
            offers = self._offers(snapshot, requirement, decision.evidence_contract, include_unapproved=True)
            self._check_structured_rationale_facts(
                snapshot,
                decision,
                requirement,
                offers,
                sink,
            )
            self._check_time_phased_ledger(
                decision,
                requirement,
                offers,
                sink,
            )
            for plan in tuple(item for item in (decision.selected_plan, *decision.alternatives) if item is not None):
                plan_offers = (
                    self._offers(
                        snapshot,
                        requirement,
                        decision.evidence_contract,
                        include_unapproved=True,
                        include_evidence_blocked=True,
                    )
                    if _is_evidence_contract_diagnostic(plan)
                    else offers
                )
                self._check_plan(
                    snapshot,
                    decision,
                    requirement,
                    plan,
                    plan_offers,
                    sink,
                )
                plan_action_keys: set[tuple[object, ...]] = set()
                for line in plan.lines:
                    key = (decision.requirement_id, line.component_id, line.supplier_id, line.route_id, line.order_date)
                    if key in plan_action_keys or (
                        plan.disposition.writes_purchase_order
                        and key in executable_action_keys
                    ):
                        sink.error("DUPLICATE_ACTION", "Semantic action identity is duplicated.", component_id=decision.component_id, plan_id=plan.plan_id)
                    plan_action_keys.add(key)
                    if plan.disposition.writes_purchase_order:
                        executable_action_keys.add(key)
            approval_plans = tuple(
                plan
                for plan in decision.alternatives
                if plan.disposition is PlanDisposition.RECOMMEND_APPROVAL
            )
            if (
                approval_plans
                and AlertCategory.APPROVAL_REQUIRED
                not in decision.alert_categories
            ):
                sink.error(
                    "APPROVAL_ALERT_MISSING",
                    "Every complete RECOMMEND_APPROVAL proposal requires an APPROVAL_REQUIRED alert.",
                    component_id=decision.component_id,
                )
            self._check_capacity(snapshot, decision, sink)
            expected_lateness = self._deadline_lateness(
                requirement,
                decision.selected_plan,
            )
            if decision.deadline_lateness != expected_lateness:
                sink.error(
                    "DEADLINE_LATENESS_MISMATCH",
                    "Post-plan missed deadlines, quantities, or unit-late-days differ from independent FIFO reconstruction.",
                    component_id=decision.component_id,
                )
            if expected_lateness and AlertCategory.LATE_ARRIVAL not in decision.alert_categories:
                sink.error(
                    "LATE_ARRIVAL_ALERT_MISSING",
                    "Every positive post-plan deadline miss requires LATE_ARRIVAL.",
                    component_id=decision.component_id,
                )
            if not expected_lateness and AlertCategory.LATE_ARRIVAL in decision.alert_categories:
                sink.error(
                    "LATE_ARRIVAL_ALERT_UNSCOPED",
                    "LATE_ARRIVAL is present without an independently reconstructed deadline miss.",
                    component_id=decision.component_id,
                )
            existing_coverage = min(requirement.total_demand, requirement.eventual_supply)
            planned = decision.selected_plan.eventual_covered_quantity if decision.selected_plan is not None else ZERO
            expected_covered = min(requirement.total_demand, existing_coverage + planned)
            expected_residual = requirement.total_demand - expected_covered
            expected_fulfillment = FulfillmentStatus.FULFILLED if expected_residual == ZERO else (FulfillmentStatus.UNFULFILLED if expected_covered == ZERO else FulfillmentStatus.PARTIALLY_FULFILLED)
            if decision.covered_quantity != expected_covered or decision.residual_gap != expected_residual or decision.requirement_state.fulfillment is not expected_fulfillment:
                sink.error("REQUIREMENT_STATE_MISMATCH", "Requirement fulfillment/residual was not recomputed from physical coverage.", component_id=decision.component_id)
            if expected_residual == ZERO and decision.requirement_state.resolution is not ResolutionStatus.RESOLVED:
                sink.error("REQUIREMENT_RESOLUTION_MISMATCH", "Zero residual requires RESOLVED.", component_id=decision.component_id)
            if expected_residual > ZERO and decision.requirement_state.resolution is ResolutionStatus.RESOLVED:
                sink.error("REQUIREMENT_RESOLUTION_MISMATCH", "Positive residual cannot be RESOLVED.", component_id=decision.component_id)
            if expected_residual > ZERO and not (set(decision.alert_categories) & _TERMINAL_ALERTS):
                sink.error("SILENT_RESIDUAL_GAP", "Every positive post-plan residual needs a terminal component-specific alert, including after a partial PO.", component_id=decision.component_id)
            solver_verified = self._check_solver_results(snapshot, decision, result_tuple, requirement, sink) and solver_verified
            if self._shaping_degradation_required(
                snapshot, requirement.component
            ):
                if AlertCategory.POLICY_CONFLICT not in decision.alert_categories:
                    sink.error("SHAPING_DEGRADATION_MISSING", "Unresolvable shaping subject must drop only that directive and emit POLICY_CONFLICT.", component_id=decision.component_id)
            citations = tuple(
                item
                for rule in self.registry.active_rules(snapshot.configuration.current_date)
                for item in (rule.rule_id, rule.source_document)
                if item in decision.rationale
            )
            if not decision.rationale.strip() or not citations:
                sink.error("RATIONALE_CITATION_MISSING", "Decision rationale must cite at least one active policy source.", component_id=decision.component_id)
        for component_id, requirement in source.items():
            records = by_component.get(component_id, ())
            if requirement.eventual_gap > ZERO and len(records) != 1:
                sink.error("INITIAL_GAP_DECISION_CARDINALITY", "Every positive initial gap requires exactly one DecisionRecord.", component_id=component_id)
            elif requirement.eventual_gap > ZERO and records[0].selected_plan is None and not (set(records[0].alert_categories) & _TERMINAL_ALERTS):
                sink.error("SILENT_INITIAL_GAP", "An initial gap without executable PO requires a terminal component-specific alert.", component_id=component_id)
        for component_id, records in by_component.items():
            if len(records) > 1:
                sink.error("MULTIPLE_COMPONENT_DECISIONS", "A component requirement may not execute more than one selected decision.", component_id=component_id)
        self._check_route_quarantine(snapshot, decision_tuple, sink)
        # A forced solve-Q surplus is advisory.  Withholding solely because it
        # exceeds the discretionary ratio is a validator defect, not an outcome.
        for decision in decision_tuple:
            if decision.selected_plan is None:
                forced_options = tuple(
                    plan for plan in decision.alternatives
                    if plan.forced_surplus > ZERO and plan.discretionary_surplus == ZERO and not plan.unresolved_approval_ids
                )
                if forced_options and set(decision.alert_categories) <= {AlertCategory.DECISION_REQUIRED, AlertCategory.FORCED_SURPLUS, AlertCategory.RUN_ACCOUNTING}:
                    sink.error("FORCED_SURPLUS_MISCLASSIFIED", "Forced solve-Q surplus was treated as a business rejection; only discretionary surplus is ratio-gated.", component_id=decision.component_id)
        return ValidationResult(
            completed=True,
            exact_decimal_checks_completed=True,
            solver_results_verified=solver_verified,
            checked_invariants=(
                "action-uniqueness",
                "approval-line-classification-and-complete-proposals",
                "allocation-and-exception-scoping",
                "catalog-asl-certification-dates",
                "evidence-contract-and-entity-ladder",
                "exact-objective-and-certified-solves",
                "forced-discretionary-surplus-and-autonomy",
                "inbound-delivery-date-netting",
                "no-silent-gap-and-requirement-state",
                "policy-window-comparators-6-8",
                "rationale-citations-and-capacity-disclosure",
                "structured-quantity-comparators-and-material-rejections",
                "route-input-quarantine-and-data-quality",
                "time-phased-recovery-lateness-and-alerts",
                "u-derivation",
            ),
            issues=tuple(sink.values),
        )


PlanValidator = IndependentPlanValidator


def EconomicAutonomy(
    *,
    max_surplus_fraction: Decimal | None = None,
    max_surplus_units: Decimal | None = None,
    max_excess_cost_usd: Decimal | None = None,
) -> EconomicAutonomyParameters:
    """Compatibility constructor whose omitted values still come from the pack."""

    base = load_policy_registry().economic_autonomy
    return replace(
        base,
        max_surplus_fraction=(
            base.max_surplus_fraction
            if max_surplus_fraction is None
            else max_surplus_fraction
        ),
        max_surplus_units=(
            base.max_surplus_units if max_surplus_units is None else max_surplus_units
        ),
        max_excess_cost_usd=(
            base.max_excess_cost_usd
            if max_excess_cost_usd is None
            else max_excess_cost_usd
        ),
    )


def validate_plans(
    snapshot: ScenarioSnapshot,
    decisions: Sequence[DecisionRecord],
    solver_results: Sequence[SolverResult],
    *,
    registry: PolicyRegistry | None = None,
) -> ValidationResult:
    return IndependentPlanValidator(registry).validate(snapshot, decisions, solver_results)


__all__ = [
    "EconomicAutonomy",
    "IndependentPlanValidator",
    "IndependentSolve",
    "IndependentSolverLimits",
    "NamedEntityCheck",
    "NamedEntityOutcome",
    "PlanValidator",
    "validate_plans",
]
