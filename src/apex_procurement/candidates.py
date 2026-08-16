"""Deterministic supplier-route construction and the interim greedy solver.

Candidate construction is deliberately quantity-free.  It turns every catalog
offer into one or more auditable routes, splitting routes when a permission is
valid for only some demand buckets.  The optimizer can therefore bind quantity
to the exact deadlines that opened an international or air-freight exception.

The ``GreedySolver`` at the bottom of this module is only an integration
milestone.  Its result is always ``UNRESOLVED`` and its diagnostic plan is
always ``DECISION_REQUIRED``.  It cannot certify infeasibility, prove that no
alternative exists, or consume an exception route.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from functools import cmp_to_key
import hashlib
import json
import re
import unicodedata

from .config import EvidenceContract
from .domain import (
    AlertCategory,
    BucketAllocation,
    CandidatePlan,
    CandidateRoute,
    ComparatorTrace,
    Component,
    DemandBucket,
    EvidenceBasis,
    EvidenceResult,
    EvidenceScope,
    EvidenceStatus,
    PlanDisposition,
    PlanLine,
    RuleSeverity,
    ScenarioSnapshot,
    SolveKind,
    SolverResult,
    SolverStageResult,
    SolverStatus,
    Supplier,
    SupplierCatalogLine,
    ZERO,
)
from .ledgers import LedgerBuildResult, aggregate_round_and_apply_moq
from .policy.entity_resolution import (
    EntityResolver,
    canonical_certification,
    normalize_legal_name,
    normalized_tokens,
)
from .policy.registry import PolicyRegistry, PolicyRule, load_policy_registry


_STANDARD_SHIPPING = "standard"
_AIR_SHIPPING = "air freight"
_ELECTRONIC_CATEGORY = normalized_tokens("Electronic Component")
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_CERTIFICATION_SPLIT_RE = re.compile(r"[,;|/]")


def _require_date(value: date, name: str) -> None:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"{name} must be datetime.date")


def _normalise_text(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(normalized_tokens(value))


def _canonical_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_certifications(values: Iterable[str]) -> tuple[str, ...]:
    certifications = {
        canonical_certification(part)
        for value in values
        for part in _CERTIFICATION_SPLIT_RE.split(value)
        if part.strip()
    }
    return tuple(sorted(certifications))


def supplier_fingerprint(supplier: Supplier) -> str:
    """Hash semantic supplier attributes, excluding the database supplier ID."""

    if not isinstance(supplier, Supplier):
        raise TypeError("supplier must be Supplier")
    return _canonical_hash(
        {
            "legal_name": normalize_legal_name(supplier.name),
            "country": _normalise_text(supplier.country),
            "certifications": _canonical_certifications(supplier.certifications),
            "relationship_tier": _normalise_text(supplier.relationship_tier),
            "sustainability_rating": _normalise_text(
                supplier.sustainability_rating
            ),
        }
    )


def component_fingerprint(component: Component) -> str:
    """Hash component semantics without its surrogate component ID."""

    if not isinstance(component, Component):
        raise TypeError("component must be Component")
    return _canonical_hash(
        {
            "name": _normalise_text(component.name),
            "description": _normalise_text(component.description),
            "category": _normalise_text(component.category),
            "unit_of_measure": _normalise_text(component.unit_of_measure),
            "is_hazardous": component.is_hazardous,
            "required_certifications": _canonical_certifications(
                component.required_certifications
            ),
        }
    )


def route_fingerprint(
    component: Component,
    catalog_line: SupplierCatalogLine,
    shipping_method: str,
    lead_time_days: int,
) -> str:
    """Hash non-ID commercial and physical terms for one route."""

    if not isinstance(catalog_line, SupplierCatalogLine):
        raise TypeError("catalog_line must be SupplierCatalogLine")
    if not isinstance(shipping_method, str) or not shipping_method.strip():
        raise ValueError("shipping_method must be non-empty text")
    if not isinstance(lead_time_days, int) or isinstance(lead_time_days, bool):
        raise TypeError("lead_time_days must be int")
    return _canonical_hash(
        {
            "component": component_fingerprint(component),
            "unit_price": str(catalog_line.unit_price),
            "minimum_order_quantity": str(catalog_line.minimum_order_quantity),
            "catalog_lead_time_days": catalog_line.lead_time_days,
            "effective_lead_time_days": lead_time_days,
            "shipping_method": _normalise_text(shipping_method),
            "catalog_notes": _normalise_text(catalog_line.notes),
        }
    )


def _route_id(
    supplier_hash: str,
    route_hash: str,
    exception_codes: Iterable[str],
    feasible_deadlines: Iterable[date],
    exception_scope_deadlines: Iterable[date],
) -> str:
    digest = _canonical_hash(
        {
            "supplier_fingerprint": supplier_hash,
            "route_fingerprint": route_hash,
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


class DomesticGateCondition(str, Enum):
    """The mutually exclusive §3 reading used for one demand bucket."""

    SHUT = "shut"
    NO_DOMESTIC_TIMELINE = "a"
    PREMIUM = "b"
    NO_DOMESTIC_SOURCE = "c"


@dataclass(frozen=True, slots=True)
class DomesticGateDecision:
    condition: DomesticGateCondition
    threshold: Decimal
    premium_fraction: Decimal | None
    rule_ids: tuple[str, ...] = ()

    @property
    def permits_international(self) -> bool:
        return self.condition is not DomesticGateCondition.SHUT

    @property
    def domestic_preference_state(self) -> str:
        if self.condition is DomesticGateCondition.SHUT:
            return "not_reached"
        if self.condition is DomesticGateCondition.PREMIUM:
            return "skipped"
        return "moot"


def evaluate_domestic_gate(
    *,
    domestic_source_exists: bool,
    domestic_can_meet_deadline: bool,
    best_domestic_price: Decimal | None,
    best_international_price: Decimal | None,
    critical_status: EvidenceStatus | bool,
    noncritical_rule_id: str = "POLICY.section_3.domestic_preference",
    critical_rule_id: str = "POLICY.section_3.critical_premium_threshold",
) -> DomesticGateDecision:
    """Evaluate §3 with strict thresholds and a guarded international denominator.

    Unknown criticality uses the stricter 50% reading, the intersection that
    remains safe under both possible classifications.
    """

    for name, value in (
        ("best_domestic_price", best_domestic_price),
        ("best_international_price", best_international_price),
    ):
        if value is not None and (not isinstance(value, Decimal) or value < ZERO):
            raise TypeError(f"{name} must be a nonnegative Decimal or None")
    if isinstance(critical_status, bool):
        critical = critical_status
    elif isinstance(critical_status, EvidenceStatus):
        critical = critical_status is not EvidenceStatus.FAIL
    else:
        raise TypeError("critical_status must be bool or EvidenceStatus")
    threshold = Decimal("0.50") if critical else Decimal("0.35")
    rule_ids = (critical_rule_id,) if critical else (noncritical_rule_id,)

    # Without an international offer there is no valid ratio denominator and
    # no international route for any lettered permission to admit.
    if best_international_price is None:
        return DomesticGateDecision(
            DomesticGateCondition.SHUT, threshold, None, rule_ids
        )
    if not domestic_source_exists:
        return DomesticGateDecision(
            DomesticGateCondition.NO_DOMESTIC_SOURCE, threshold, None, rule_ids
        )
    if not domestic_can_meet_deadline:
        return DomesticGateDecision(
            DomesticGateCondition.NO_DOMESTIC_TIMELINE, threshold, None, rule_ids
        )
    if best_domestic_price is None:
        # Defensive consistency: a declared source without a priced offer is
        # not enough evidence to perform the premium calculation.
        return DomesticGateDecision(
            DomesticGateCondition.SHUT, threshold, None, rule_ids
        )
    if best_international_price == ZERO:
        premium = Decimal("Infinity") if best_domestic_price > ZERO else ZERO
    else:
        premium = (
            best_domestic_price - best_international_price
        ) / best_international_price
    condition = (
        DomesticGateCondition.PREMIUM
        if premium > threshold
        else DomesticGateCondition.SHUT
    )
    return DomesticGateDecision(condition, threshold, premium, rule_ids)


@dataclass(frozen=True, slots=True)
class NoAlternativeProof:
    """A checked counterfactual certificate supplied by the exact optimizer.

    The component is identified by its ID-free semantic fingerprint so a
    consistent database-ID permutation cannot alter the proof lookup.
    """

    component_fingerprint: str
    status: SolverStatus
    certificate_complete: bool
    independently_validated: bool

    def __post_init__(self) -> None:
        if not isinstance(self.component_fingerprint, str) or not self.component_fingerprint:
            raise ValueError("component_fingerprint must be non-empty text")
        if not isinstance(self.status, SolverStatus):
            raise TypeError("status must be SolverStatus")
        if not isinstance(self.certificate_complete, bool):
            raise TypeError("certificate_complete must be bool")
        if not isinstance(self.independently_validated, bool):
            raise TypeError("independently_validated must be bool")

    @property
    def proves_no_alternative(self) -> bool:
        return (
            self.status is SolverStatus.INFEASIBLE
            and self.certificate_complete
            and self.independently_validated
        )

    @property
    def proves_alternative_exists(self) -> bool:
        return (
            self.status is SolverStatus.OPTIMAL
            and self.certificate_complete
            and self.independently_validated
        )


@dataclass(frozen=True, slots=True)
class CandidateAlert:
    category: AlertCategory
    code: str
    message: str
    component_id: str | None = None
    supplier_id: str | None = None
    route_id: str | None = None
    rule_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateRejection:
    route_id: str
    component_id: str
    supplier_id: str
    status: EvidenceStatus
    code: str
    reason: str
    rule_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateBuildResult:
    routes: tuple[CandidateRoute, ...]
    rejections: tuple[CandidateRejection, ...] = ()
    alerts: tuple[CandidateAlert, ...] = ()

    def __post_init__(self) -> None:
        routes = tuple(self.routes)
        if any(not isinstance(item, CandidateRoute) for item in routes):
            raise TypeError("routes contains an invalid item")
        rejections = tuple(self.rejections)
        if any(not isinstance(item, CandidateRejection) for item in rejections):
            raise TypeError("rejections contains an invalid item")
        alerts = tuple(self.alerts)
        if any(not isinstance(item, CandidateAlert) for item in alerts):
            raise TypeError("alerts contains an invalid item")
        object.__setattr__(
            self,
            "routes",
            tuple(
                sorted(
                    routes,
                    key=lambda item: (
                        item.component_id,
                        item.supplier_fingerprint,
                        item.route_fingerprint,
                        item.exception_codes,
                        item.route_id,
                    ),
                )
            ),
        )
        object.__setattr__(
            self,
            "rejections",
            tuple(
                sorted(
                    rejections,
                    key=lambda item: (
                        item.component_id,
                        item.route_id,
                        item.code,
                        item.reason,
                    ),
                )
            ),
        )
        object.__setattr__(
            self,
            "alerts",
            tuple(
                sorted(
                    alerts,
                    key=lambda item: (
                        item.category.value,
                        item.code,
                        item.component_id or "",
                        item.route_id or "",
                        item.message,
                    ),
                )
            ),
        )

    @property
    def executable_routes(self) -> tuple[CandidateRoute, ...]:
        """Routes admissible to solve 1 before quantity constraints are added."""

        return tuple(
            route
            for route in self.routes
            if route.may_enter_executable_model and route.feasible_deadlines
        )

    @property
    def recommendation_routes(self) -> tuple[CandidateRoute, ...]:
        return tuple(
            route
            for route in self.routes
            if route.eligibility is not EvidenceStatus.FAIL
            and route not in self.executable_routes
        )

    @property
    def evidence_blocked_routes(self) -> tuple[CandidateRoute, ...]:
        """Routes blocked by missing contract evidence rather than hard failure."""

        return tuple(route for route in self.routes if route.is_evidence_blocked)

    def routes_for(self, component_id: str) -> tuple[CandidateRoute, ...]:
        return tuple(item for item in self.routes if item.component_id == component_id)


@dataclass(frozen=True, slots=True)
class _OfferFacts:
    component: Component
    supplier: Supplier
    catalog: SupplierCatalogLine
    supplier_hash: str
    domestic_status: EvidenceStatus
    base_evidence: tuple[EvidenceResult, ...]
    base_status: EvidenceStatus


def _rule_kind(rule: PolicyRule) -> str:
    body = rule.data.get("constraint") or rule.data.get("directive")
    return str(body.get("kind")) if isinstance(body, Mapping) else ""


def _rule_basis(rule: PolicyRule) -> EvidenceBasis:
    return EvidenceBasis(rule.evidence_basis)


def _rule_severity(rule: PolicyRule) -> RuleSeverity:
    return RuleSeverity(rule.severity)


def _evidence(
    rule: PolicyRule,
    status: EvidenceStatus,
    summary: str,
    *,
    scope: EvidenceScope = EvidenceScope.CANDIDATE,
    assumptions: Iterable[str] = (),
    disposition: PlanDisposition | None = None,
) -> EvidenceResult:
    return EvidenceResult(
        rule_id=rule.rule_id,
        status=status,
        basis=_rule_basis(rule),
        scope=scope,
        severity=_rule_severity(rule),
        summary=summary,
        source_references=(rule.source_document,),
        assumption_codes=tuple(assumptions),
        contract_disposition=disposition,
    )


def _synthetic_evidence(
    rule_id: str,
    status: EvidenceStatus,
    summary: str,
    *,
    severity: RuleSeverity = RuleSeverity.HARD,
    scope: EvidenceScope = EvidenceScope.CANDIDATE,
    assumptions: Iterable[str] = (),
    disposition: PlanDisposition | None = None,
) -> EvidenceResult:
    return EvidenceResult(
        rule_id=rule_id,
        status=status,
        basis=EvidenceBasis.ENTITY_ATTRIBUTE,
        scope=scope,
        severity=severity,
        summary=summary,
        source_references=("scenario master data",),
        assumption_codes=tuple(assumptions),
        contract_disposition=disposition,
    )


def _eligibility(evidence: Iterable[EvidenceResult]) -> EvidenceStatus:
    items = tuple(evidence)
    if any(
        item.severity is RuleSeverity.HARD and item.status is EvidenceStatus.FAIL
        for item in items
    ):
        return EvidenceStatus.FAIL
    for item in items:
        if item.severity is not RuleSeverity.HARD or item.status is not EvidenceStatus.UNKNOWN:
            continue
        assumed_rule_unknown = (
            item.scope is EvidenceScope.RULE
            and item.contract_disposition is PlanDisposition.EXECUTE_WITH_ASSUMPTION
        )
        if not assumed_rule_unknown:
            return EvidenceStatus.UNKNOWN
    return EvidenceStatus.PASS


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


def _holds_certification(supplier: Supplier, required: str) -> bool:
    wanted = canonical_certification(required)
    held = set(_canonical_certifications(supplier.certifications))
    if wanted in held:
        return True
    # The source and fixtures use both the noun and adjective form.
    ul_wanted = wanted in {"ULLISTING", "ULLISTED"}
    return ul_wanted and bool(held & {"ULLISTING", "ULLISTED"})


def _business_day_distance(left: date, right: date) -> int:
    if left == right:
        return 0
    start, end = sorted((left, right))
    cursor = start
    count = 0
    while cursor < end:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            count += 1
    return count


class CandidateBuilder:
    """Build auditable candidate routes from a snapshot and demand ledgers."""

    def __init__(
        self,
        registry: PolicyRegistry | None = None,
        contract: EvidenceContract = EvidenceContract.BENCHMARK,
        *,
        receiving_buffer_days: int = 0,
        accepted_shipment_pairs: Iterable[tuple[str, str]] = (),
        no_alternative_proofs: Iterable[NoAlternativeProof] = (),
        approved_air_route_fingerprints: Iterable[str] = (),
        approved_below_b_route_fingerprints: Iterable[str] = (),
        air_period_spend: Decimal | None = None,
    ) -> None:
        self.registry = registry or load_policy_registry()
        if not isinstance(self.registry, PolicyRegistry):
            raise TypeError("registry must be PolicyRegistry")
        if not isinstance(contract, EvidenceContract):
            raise TypeError("contract must be EvidenceContract")
        if (
            not isinstance(receiving_buffer_days, int)
            or isinstance(receiving_buffer_days, bool)
            or receiving_buffer_days < 0
        ):
            raise ValueError("receiving_buffer_days must be a nonnegative int")
        if air_period_spend is not None and (
            not isinstance(air_period_spend, Decimal) or air_period_spend < ZERO
        ):
            raise TypeError("air_period_spend must be a nonnegative Decimal or None")
        self.contract = contract
        self.receiving_buffer_days = receiving_buffer_days
        self.accepted_shipment_pairs = frozenset(tuple(item) for item in accepted_shipment_pairs)
        proofs = tuple(no_alternative_proofs)
        if any(not isinstance(item, NoAlternativeProof) for item in proofs):
            raise TypeError("no_alternative_proofs contains an invalid item")
        if len({item.component_fingerprint for item in proofs}) != len(proofs):
            raise ValueError("no_alternative_proofs contains duplicate component fingerprints")
        self.no_alternative_proofs = {
            item.component_fingerprint: item for item in proofs
        }
        self.approved_air_route_fingerprints = frozenset(
            approved_air_route_fingerprints
        )
        self.approved_below_b_route_fingerprints = frozenset(
            approved_below_b_route_fingerprints
        )
        self.air_period_spend = air_period_spend
        self.resolver = EntityResolver(self.registry)

    def _rules(self, kind: str, scenario_date: date) -> tuple[PolicyRule, ...]:
        return tuple(
            rule
            for rule in self.registry.active_rules(scenario_date)
            if _rule_kind(rule) == kind
        )

    def _one_rule(self, kind: str, scenario_date: date) -> PolicyRule:
        rules = self._rules(kind, scenario_date)
        if len(rules) != 1:
            raise ValueError(
                f"expected one active {kind!r} rule, found {len(rules)}"
            )
        return rules[0]

    def _certification_evidence(
        self,
        component: Component,
        supplier: Supplier,
        scenario_date: date,
    ) -> tuple[EvidenceResult, ...]:
        results: list[EvidenceResult] = []
        certification_rules = self._rules("required_certification", scenario_date)
        iso_rule = next(
            rule
            for rule in certification_rules
            if canonical_certification(str(rule.data["constraint"]["certification"]))
            == "ISO9001"
        )
        ul_rules = tuple(rule for rule in certification_rules if rule is not iso_rule)

        electronic = normalized_tokens(component.category or "") == _ELECTRONIC_CATEGORY
        pcb = self.resolver.resolve_concept("printed_circuit_board", component)
        safety = self.resolver.resolve_concept("safety_critical_part", component)
        iso_required = electronic or pcb.status is EvidenceStatus.PASS or safety.status is EvidenceStatus.PASS
        if iso_required:
            held = _holds_certification(supplier, "ISO-9001")
            reasons = []
            if electronic:
                reasons.append("components.category is Electronic Component")
            if pcb.status is EvidenceStatus.PASS:
                reasons.append("the component is a printed circuit board")
            if safety.status is EvidenceStatus.PASS:
                reasons.append("positive evidence marks the component safety-critical")
            results.append(
                _evidence(
                    iso_rule,
                    EvidenceStatus.PASS if held else EvidenceStatus.FAIL,
                    f"ISO-9001 is required because {'; '.join(reasons)}; "
                    + ("the supplier holds it." if held else "the supplier does not hold it."),
                )
            )

        declared_requirements = tuple(
            part.strip()
            for value in component.required_certifications
            for part in _CERTIFICATION_SPLIT_RE.split(value)
            if part.strip()
        )
        for required in declared_requirements:
            held = _holds_certification(supplier, required)
            results.append(
                _synthetic_evidence(
                    f"MASTER-DATA.required_certification.{canonical_certification(required)}",
                    EvidenceStatus.PASS if held else EvidenceStatus.FAIL,
                    f"The component declares {required!r} as required; "
                    + ("the supplier holds it." if held else "the supplier does not hold it."),
                )
            )

        power = self.resolver.resolve_concept("power_supply_component", component)
        if power.status is EvidenceStatus.PASS:
            for rule in ul_rules:
                required = str(rule.data["constraint"]["certification"])
                held = _holds_certification(supplier, required)
                results.append(
                    _evidence(
                        rule,
                        EvidenceStatus.PASS if held else EvidenceStatus.FAIL,
                        f"Power-supply components require {required}; "
                        + ("the supplier holds it." if held else "the supplier does not hold it."),
                    )
                )
        return tuple(results)

    def _relationship_predates(self, supplier: Supplier, effective_from: date) -> bool:
        if not supplier.notes:
            return False
        years = tuple(int(item) for item in _YEAR_RE.findall(supplier.notes))
        return bool(years) and min(years) < effective_from.year

    def _pcb_evidence(
        self,
        snapshot: ScenarioSnapshot,
        component: Component,
        supplier: Supplier,
        catalog: SupplierCatalogLine,
        prior_evidence: Sequence[EvidenceResult],
    ) -> tuple[EvidenceResult, ...]:
        scenario_date = snapshot.configuration.current_date
        pcb_status = self.resolver.resolve_concept(
            "printed_circuit_board_component", component
        ).status
        rules = self._rules("incumbent_supplier_only", scenario_date)
        if not rules or pcb_status is EvidenceStatus.FAIL:
            return ()
        rule = rules[0]
        if pcb_status is EvidenceStatus.UNKNOWN:
            return (
                _evidence(
                    rule,
                    EvidenceStatus.UNKNOWN,
                    "PCB-rule membership is unresolved and changes route executability.",
                    assumptions=("ROBUST_BOTH_WAYS",),
                    disposition=PlanDisposition.DECISION_REQUIRED,
                ),
            )
        if (component.component_id, supplier.supplier_id) in self.accepted_shipment_pairs:
            incumbency = _evidence(
                rule,
                EvidenceStatus.PASS,
                "An accepted-shipment record affirmatively establishes PCB incumbency.",
            )
        elif self.contract is EvidenceContract.PRODUCTION:
            incumbency = _evidence(
                rule,
                EvidenceStatus.UNKNOWN,
                "Production mode requires an accepted-shipment record; none is present.",
                assumptions=("PCB_INBOUND_HISTORY_UNKNOWN",),
                disposition=PlanDisposition.DECISION_REQUIRED,
            )
        else:
            prior_order = any(
                item.component_id == component.component_id
                and item.supplier_id == supplier.supplier_id
                for item in snapshot.purchase_orders
            )
            hard_prerequisites_pass = _eligibility(prior_evidence) is EvidenceStatus.PASS
            relationship = (
                self._relationship_predates(supplier, rule.effective_from)
                and catalog is not None
                and hard_prerequisites_pass
            )
            if prior_order or relationship:
                support = "a prior order" if prior_order else "a relationship predating the memo"
                incumbency = _evidence(
                    rule,
                    EvidenceStatus.UNKNOWN,
                    f"Benchmark inference uses {support}; receipt acceptance is not proven.",
                    scope=EvidenceScope.RULE,
                    assumptions=("PCB_INCUMBENCY_INFERRED",),
                    disposition=PlanDisposition.EXECUTE_WITH_ASSUMPTION,
                )
            else:
                incumbency = _evidence(
                    rule,
                    EvidenceStatus.FAIL,
                    "The supplier is new under the benchmark PCB incumbency inference.",
                )

        coc_rules = self._rules("shipment_certificate_of_conformance", scenario_date)
        coc = tuple(
            _evidence(
                coc_rule,
                EvidenceStatus.PASS,
                "The route represents the required Certificate of Conformance and cross-section results.",
            )
            for coc_rule in coc_rules
        )
        return (incumbency,) + coc

    def _base_offer_facts(
        self, snapshot: ScenarioSnapshot
    ) -> tuple[_OfferFacts, ...]:
        components = {item.component_id: item for item in snapshot.components}
        suppliers = {item.supplier_id: item for item in snapshot.suppliers}
        scenario_date = snapshot.configuration.current_date
        asl_rule = self._one_rule("approved_supplier_required", scenario_date)
        moq_rule = self._one_rule(
            "catalog_minimum_order_quantity", scenario_date
        )
        result: list[_OfferFacts] = []
        for catalog in snapshot.catalog_lines:
            component = components[catalog.component_id]
            supplier = suppliers[catalog.supplier_id]
            asl_pass = supplier.on_approved_list is True
            asl = _evidence(
                asl_rule,
                EvidenceStatus.PASS if asl_pass else EvidenceStatus.FAIL,
                "Supplier is affirmatively present on the ASL."
                if asl_pass
                else "Supplier is not affirmatively present on the ASL; NULL is not approval.",
                assumptions=("ASL_NOT_APPROVED",)
                if supplier.on_approved_list is None
                else (),
            )
            evidence: list[EvidenceResult] = [
                asl,
                _synthetic_evidence(
                    "MASTER-DATA.catalog_offer",
                    EvidenceStatus.PASS,
                    "A catalog offer exists for this component and supplier route.",
                ),
                _evidence(
                    moq_rule,
                    EvidenceStatus.PASS,
                    f"The catalog MOQ is represented exactly as {catalog.minimum_order_quantity}.",
                ),
            ]
            evidence.extend(
                self._certification_evidence(component, supplier, scenario_date)
            )
            evidence.extend(
                self._pcb_evidence(snapshot, component, supplier, catalog, evidence)
            )
            domestic = self.resolver.resolve_concept("domestic_supplier", supplier)
            if domestic.status is EvidenceStatus.UNKNOWN:
                evidence.append(
                    _synthetic_evidence(
                        "DATA-QUALITY.supplier_domesticity",
                        EvidenceStatus.UNKNOWN,
                        "Supplier country cannot be resolved against the reviewed domestic aliases.",
                        assumptions=("SUPPLIER_ATTRIBUTE_UNKNOWN",),
                        disposition=PlanDisposition.DECISION_REQUIRED,
                    )
                )
            result.append(
                _OfferFacts(
                    component=component,
                    supplier=supplier,
                    catalog=catalog,
                    supplier_hash=supplier_fingerprint(supplier),
                    domestic_status=domestic.status,
                    base_evidence=tuple(evidence),
                    base_status=_eligibility(evidence),
                )
            )
        return tuple(result)

    def _critical_status(self, component: Component) -> EvidenceStatus:
        return self.resolver.resolve_concept("critical_component", component).status

    def _domestic_rule_ids(
        self, component: Component, scenario_date: date
    ) -> tuple[str, str]:
        rules = self._rules("domestic_supplier_preference", scenario_date)
        noncritical = next(
            rule
            for rule in rules
            if Decimal(str(rule.data["constraint"]["maximum_premium_fraction"]))
            == Decimal("0.35")
        )
        critical = next(
            rule
            for rule in rules
            if Decimal(str(rule.data["constraint"]["maximum_premium_fraction"]))
            == Decimal("0.50")
        )
        return noncritical.rule_id, critical.rule_id

    def _gate_by_component(
        self,
        snapshot: ScenarioSnapshot,
        ledgers: LedgerBuildResult,
        facts: Sequence[_OfferFacts],
    ) -> dict[tuple[str, date], DomesticGateDecision]:
        scenario_date = snapshot.configuration.current_date
        by_component: dict[str, list[_OfferFacts]] = defaultdict(list)
        for item in facts:
            by_component[item.component.component_id].append(item)
        result: dict[tuple[str, date], DomesticGateDecision] = {}
        for component_id, offers in by_component.items():
            component = offers[0].component
            domestic = tuple(
                item
                for item in offers
                if item.domestic_status is EvidenceStatus.PASS
                and item.base_status is EvidenceStatus.PASS
            )
            international = tuple(
                item
                for item in offers
                if item.domestic_status is EvidenceStatus.FAIL
                and item.base_status is EvidenceStatus.PASS
            )
            best_domestic = min(
                (item.catalog.unit_price for item in domestic), default=None
            )
            best_international = min(
                (item.catalog.unit_price for item in international), default=None
            )
            noncritical_id, critical_id = self._domestic_rule_ids(
                component, scenario_date
            )
            critical_status = self._critical_status(component)
            for bucket in ledgers.buckets_for(component_id):
                domestic_can_meet = any(
                    self._standard_material_date(item, scenario_date) <= bucket.due_date
                    for item in domestic
                )
                result[(component_id, bucket.due_date)] = evaluate_domestic_gate(
                    domestic_source_exists=bool(domestic),
                    domestic_can_meet_deadline=domestic_can_meet,
                    best_domestic_price=best_domestic,
                    best_international_price=best_international,
                    critical_status=critical_status,
                    noncritical_rule_id=noncritical_id,
                    critical_rule_id=critical_id,
                )
        return result

    def _is_pcb(self, component: Component) -> bool:
        return (
            self.resolver.resolve_concept(
                "printed_circuit_board_component", component
            ).status
            is EvidenceStatus.PASS
        )

    def _receiving_buffer(self, component: Component) -> int:
        if component.is_hazardous or self._is_pcb(component):
            return self.receiving_buffer_days
        return 0

    def _standard_material_date(self, facts: _OfferFacts, order_date: date) -> date:
        return order_date + timedelta(
            days=facts.catalog.lead_time_days + self._receiving_buffer(facts.component)
        )

    def _international_evidence(
        self,
        decision: DomesticGateDecision,
        scenario_date: date,
    ) -> tuple[EvidenceResult, ...]:
        justification_rule = self._one_rule(
            "international_sourcing_justification", scenario_date
        )
        if decision.condition is DomesticGateCondition.SHUT:
            summary = "No §3 condition permits this international route."
            status = EvidenceStatus.FAIL
        elif decision.condition is DomesticGateCondition.NO_DOMESTIC_TIMELINE:
            summary = "§3(a) permits international sourcing for the cited deadlines."
            status = EvidenceStatus.PASS
        elif decision.condition is DomesticGateCondition.PREMIUM:
            summary = (
                "§3(b) permits international sourcing: the domestic premium "
                f"{decision.premium_fraction} strictly exceeds {decision.threshold}."
            )
            status = EvidenceStatus.PASS
        else:
            summary = "§3(c) permits international sourcing because no domestic source exists."
            status = EvidenceStatus.PASS
        gate_evidence: tuple[EvidenceResult, ...] = ()
        if decision.rule_ids:
            gate_rule = self.registry.rule(decision.rule_ids[0])
            gate_evidence = (
                _evidence(gate_rule, status, summary),
            )
        return gate_evidence + (
            _evidence(
                justification_rule,
                status,
                summary
                + (
                    " The route carries the resulting international-sourcing justification."
                    if status is EvidenceStatus.PASS
                    else " No valid international-sourcing justification can be documented."
                ),
            ),
        )

    def _memo_discharges_below_b_review(
        self,
        snapshot: ScenarioSnapshot,
        facts: _OfferFacts,
    ) -> bool:
        for rule in self._rules(
            "named_primary_supplier", snapshot.configuration.current_date
        ):
            selector = rule.data["selector"]
            tags = tuple(str(item) for item in selector.get("semantic_tags", ()))
            if not tags or any(
                self.resolver.resolve_concept(tag, facts.component).status
                is not EvidenceStatus.PASS
                for tag in tags
            ):
                continue
            directive = rule.data.get("directive")
            reference = directive.get("supplier") if isinstance(directive, Mapping) else None
            if not isinstance(reference, Mapping):
                continue
            resolved = self.resolver.resolve_named_supplier(reference, snapshot.suppliers)
            if (
                resolved.status is EvidenceStatus.PASS
                and resolved.supplier is not None
                and resolved.supplier.supplier_id == facts.supplier.supplier_id
            ):
                return True
        return False

    def _below_b_evidence(
        self,
        snapshot: ScenarioSnapshot,
        facts: _OfferFacts,
        route_hash: str,
    ) -> tuple[EvidenceResult, tuple[str, ...]]:
        rule = self._one_rule(
            "below_rating_review", snapshot.configuration.current_date
        )
        boundary = _rating(str(rule.data["constraint"]["below_rating"]))
        rating = _rating(facts.supplier.sustainability_rating)
        if rating is None or boundary is None:
            return (
                _evidence(
                    rule,
                    EvidenceStatus.UNKNOWN,
                    "The sustainability rating is absent or unparseable; both gate readings differ.",
                    assumptions=("SUSTAINABILITY_RATING_UNKNOWN", "ROBUST_BOTH_WAYS"),
                    disposition=PlanDisposition.DECISION_REQUIRED,
                ),
                (f"{rule.rule_id}:rating-evidence",),
            )
        if rating >= boundary:
            return (
                _evidence(
                    rule,
                    EvidenceStatus.PASS,
                    "Supplier is rated B or better; additional review is not required.",
                ),
                (),
            )

        proof = self.no_alternative_proofs.get(component_fingerprint(facts.component))
        if proof is not None and proof.proves_alternative_exists:
            return (
                _evidence(
                    rule,
                    EvidenceStatus.FAIL,
                    "A completed, independently checked counterfactual found a B-or-better alternative.",
                ),
                (),
            )
        if proof is None or not proof.proves_no_alternative:
            return (
                _evidence(
                    rule,
                    EvidenceStatus.UNKNOWN,
                    "No completed, independently checked counterfactual proves that B-or-better alternatives are unavailable.",
                    assumptions=("NO_ALTERNATIVE_PROOF_REQUIRED",),
                    disposition=PlanDisposition.DECISION_REQUIRED,
                ),
                (f"{rule.rule_id}:no-alternative-certificate",),
            )

        if self._memo_discharges_below_b_review(snapshot, facts):
            return (
                _evidence(
                    rule,
                    EvidenceStatus.UNKNOWN,
                    "A source-named VP directive is treated as the represented additional review.",
                    scope=EvidenceScope.RULE,
                    assumptions=("BELOW_B_REVIEW_DISCHARGED_BY_MEMO",),
                    disposition=PlanDisposition.EXECUTE_WITH_ASSUMPTION,
                ),
                (),
            )
        if route_hash in self.approved_below_b_route_fingerprints:
            return (
                _evidence(
                    rule,
                    EvidenceStatus.PASS,
                    "The no-alternative certificate and additional review are both represented.",
                ),
                (),
            )
        return (
            _evidence(
                rule,
                EvidenceStatus.UNKNOWN,
                "No-alternative is proven, but the required additional review is absent.",
                assumptions=("BELOW_B_REVIEW_REQUIRED",),
                disposition=PlanDisposition.RECOMMEND_APPROVAL,
            ),
            (f"{rule.rule_id}:additional-review",),
        )

    def _air_evidence(
        self,
        scenario_date: date,
        route_hash: str,
    ) -> tuple[tuple[EvidenceResult, ...], tuple[str, ...]]:
        results: list[EvidenceResult] = []
        approvals: list[str] = []
        authorization = self._one_rule("air_freight_authorization", scenario_date)
        results.append(
            _evidence(
                authorization,
                EvidenceStatus.PASS,
                "Air freight is in its effective window, serves confirmed production, and standard lead misses each scoped deadline.",
            )
        )
        for rule in self._rules("air_freight_cost_documentation", scenario_date):
            results.append(
                _evidence(
                    rule,
                    EvidenceStatus.PASS,
                    "The route carries the memo's air-cost documentation requirement.",
                )
            )
        for rule in self._rules("air_freight_period_spend_cap", scenario_date):
            cap = Decimal(str(rule.data["constraint"]["maximum_amount"]))
            if self.air_period_spend is not None and self.air_period_spend <= cap:
                results.append(
                    _evidence(
                        rule,
                        EvidenceStatus.PASS,
                        "Represented authorization-period spend is within the cap.",
                    )
                )
            else:
                results.append(
                    _evidence(
                        rule,
                        EvidenceStatus.UNKNOWN,
                        "Authorization-period air spend is absent or not proven within the cap.",
                        assumptions=("AIR_FREIGHT_PERIOD_SPEND_UNKNOWN",),
                        disposition=PlanDisposition.RECOMMEND_APPROVAL,
                    )
                )
                approvals.append(rule.rule_id)
        for rule in self._rules("air_freight_individual_approval", scenario_date):
            approved = route_hash in self.approved_air_route_fingerprints
            results.append(
                _evidence(
                    rule,
                    EvidenceStatus.PASS if approved else EvidenceStatus.UNKNOWN,
                    "Individual air-freight approval is represented."
                    if approved
                    else "Individual Procurement Manager approval is absent.",
                    assumptions=() if approved else ("APPROVAL_REQUIRED",),
                    disposition=None
                    if approved
                    else PlanDisposition.RECOMMEND_APPROVAL,
                )
            )
            if not approved:
                approvals.append(rule.rule_id)
        return tuple(results), tuple(approvals)

    def _make_route(
        self,
        snapshot: ScenarioSnapshot,
        facts: _OfferFacts,
        *,
        shipping_method: str,
        lead_time_days: int,
        deadlines: Iterable[date],
        evidence: Iterable[EvidenceResult],
        exception_codes: Iterable[str] = (),
        exception_scope_deadlines: Iterable[date] = (),
        approval_requirements: Iterable[str] = (),
    ) -> CandidateRoute:
        deadline_tuple = tuple(sorted(set(deadlines)))
        exception_tuple = tuple(sorted(set(exception_codes)))
        exception_deadline_tuple = tuple(
            sorted(set(exception_scope_deadlines))
        )
        route_hash = route_fingerprint(
            facts.component, facts.catalog, shipping_method, lead_time_days
        )
        all_evidence = list(evidence)
        order_date = snapshot.configuration.current_date
        expected = order_date + timedelta(days=lead_time_days)
        material = expected + timedelta(days=self._receiving_buffer(facts.component))
        for delivery_rule in self._rules(
            "quoted_lead_time_delivery_date", order_date
        ):
            all_evidence.append(
                _evidence(
                    delivery_rule,
                    EvidenceStatus.PASS,
                    "Expected delivery is the scenario order date plus the effective supplier-route lead; receiving time is excluded.",
                )
            )
        below_b, below_b_approvals = self._below_b_evidence(
            snapshot, facts, route_hash
        )
        all_evidence.append(below_b)
        approvals = tuple(
            sorted(set(tuple(approval_requirements) + below_b_approvals))
        )
        return CandidateRoute(
            route_id=_route_id(
                facts.supplier_hash,
                route_hash,
                exception_tuple,
                deadline_tuple,
                exception_deadline_tuple,
            ),
            component_id=facts.component.component_id,
            supplier_id=facts.supplier.supplier_id,
            supplier_fingerprint=facts.supplier_hash,
            route_fingerprint=route_hash,
            unit_price=facts.catalog.unit_price,
            minimum_order_quantity=facts.catalog.minimum_order_quantity,
            shipping_method=shipping_method,
            lead_time_days=lead_time_days,
            order_date=order_date,
            expected_delivery_date=expected,
            material_available_date=material,
            eligibility=_eligibility(all_evidence),
            feasible_deadlines=deadline_tuple,
            exception_scope_deadlines=exception_deadline_tuple,
            evidence=tuple(all_evidence),
            exception_codes=exception_tuple,
            approval_requirements=approvals,
        )

    def _build_standard_routes(
        self,
        snapshot: ScenarioSnapshot,
        ledgers: LedgerBuildResult,
        facts: _OfferFacts,
        gate: Mapping[tuple[str, date], DomesticGateDecision],
    ) -> tuple[CandidateRoute, ...]:
        order_date = snapshot.configuration.current_date
        material_date = self._standard_material_date(facts, order_date)
        buckets = ledgers.buckets_for(facts.component.component_id)
        physical_deadlines = tuple(
            item.due_date for item in buckets if material_date <= item.due_date
        )
        if facts.domestic_status is EvidenceStatus.PASS:
            return (
                self._make_route(
                    snapshot,
                    facts,
                    shipping_method=_STANDARD_SHIPPING,
                    lead_time_days=facts.catalog.lead_time_days,
                    deadlines=physical_deadlines,
                    evidence=facts.base_evidence,
                ),
            )
        if facts.domestic_status is EvidenceStatus.UNKNOWN:
            return (
                self._make_route(
                    snapshot,
                    facts,
                    shipping_method=_STANDARD_SHIPPING,
                    lead_time_days=facts.catalog.lead_time_days,
                    deadlines=(),
                    evidence=facts.base_evidence,
                ),
            )

        grouped: dict[DomesticGateCondition, list[date]] = defaultdict(list)
        for bucket in buckets:
            decision = gate[(facts.component.component_id, bucket.due_date)]
            if decision.permits_international:
                grouped[decision.condition].append(bucket.due_date)
        if not grouped:
            # Preserve one classified route for the catalog offer even though
            # the international eligibility gate is shut.
            decision = gate[(facts.component.component_id, buckets[0].due_date)] if buckets else DomesticGateDecision(DomesticGateCondition.SHUT, Decimal("0.50"), None)
            return (
                self._make_route(
                    snapshot,
                    facts,
                    shipping_method=_STANDARD_SHIPPING,
                    lead_time_days=facts.catalog.lead_time_days,
                    deadlines=(),
                    evidence=facts.base_evidence
                    + self._international_evidence(decision, order_date),
                ),
            )
        routes = []
        for condition, scoped_deadlines in sorted(
            grouped.items(), key=lambda item: item[0].value
        ):
            representative = next(
                gate[(facts.component.component_id, due)]
                for due in scoped_deadlines
                if gate[(facts.component.component_id, due)].condition is condition
            )
            allowed = tuple(
                due for due in scoped_deadlines if due in physical_deadlines
            )
            exception = f"{representative.rule_ids[0]}:condition_{condition.value}"
            routes.append(
                self._make_route(
                    snapshot,
                    facts,
                    shipping_method=_STANDARD_SHIPPING,
                    lead_time_days=facts.catalog.lead_time_days,
                    deadlines=allowed,
                    evidence=facts.base_evidence
                    + self._international_evidence(representative, order_date),
                    exception_codes=(exception,),
                    exception_scope_deadlines=scoped_deadlines,
                )
            )
        return tuple(routes)

    def _build_air_routes(
        self,
        snapshot: ScenarioSnapshot,
        ledgers: LedgerBuildResult,
        facts: _OfferFacts,
        gate: Mapping[tuple[str, date], DomesticGateDecision],
    ) -> tuple[CandidateRoute, ...]:
        scenario_date = snapshot.configuration.current_date
        air_rules = self._rules("air_freight_authorization", scenario_date)
        if (
            not air_rules
            or facts.domestic_status is not EvidenceStatus.FAIL
            or facts.base_status is EvidenceStatus.FAIL
        ):
            return ()
        air_rule = air_rules[0]
        reduction = int(air_rule.data["constraint"]["lead_time_reduction_days"])
        minimum = int(air_rule.data["constraint"]["minimum_lead_time_days"])
        air_lead = max(facts.catalog.lead_time_days - reduction, minimum)
        standard_material = self._standard_material_date(facts, scenario_date)
        air_material = scenario_date + timedelta(
            days=air_lead + self._receiving_buffer(facts.component)
        )
        grouped: dict[DomesticGateCondition, list[date]] = defaultdict(list)
        for bucket in ledgers.buckets_for(facts.component.component_id):
            decision = gate[(facts.component.component_id, bucket.due_date)]
            if (
                decision.permits_international
                and standard_material > bucket.due_date
                and air_material <= bucket.due_date
            ):
                grouped[decision.condition].append(bucket.due_date)
        routes = []
        route_hash = route_fingerprint(
            facts.component, facts.catalog, _AIR_SHIPPING, air_lead
        )
        air_evidence, approvals = self._air_evidence(scenario_date, route_hash)
        for condition, deadlines in sorted(
            grouped.items(), key=lambda item: item[0].value
        ):
            representative = gate[(facts.component.component_id, deadlines[0])]
            exceptions = (
                f"{representative.rule_ids[0]}:condition_{condition.value}",
                air_rule.rule_id,
            )
            routes.append(
                self._make_route(
                    snapshot,
                    facts,
                    shipping_method=_AIR_SHIPPING,
                    lead_time_days=air_lead,
                    deadlines=deadlines,
                    evidence=facts.base_evidence
                    + self._international_evidence(representative, scenario_date)
                    + air_evidence,
                    exception_codes=exceptions,
                    exception_scope_deadlines=deadlines,
                    approval_requirements=approvals,
                )
            )
        return tuple(routes)

    def _comparator_traces(
        self,
        snapshot: ScenarioSnapshot,
        routes: Sequence[CandidateRoute],
    ) -> tuple[CandidateRoute, ...]:
        suppliers = {item.supplier_id: item for item in snapshot.suppliers}
        scenario_date = snapshot.configuration.current_date
        strategic_rule = self._one_rule("strategic_supplier_continuity", scenario_date)
        sustainability_rule = self._one_rule("sustainability_preference", scenario_date)
        cost_rule = self._one_rule("total_cost_of_ownership", scenario_date)
        delivery_rule = self._one_rule("on_time_arrival", scenario_date)
        domestic_rule_ids = tuple(
            rule.rule_id
            for rule in self._rules("domestic_supplier_preference", scenario_date)
        )
        by_component: dict[str, list[CandidateRoute]] = defaultdict(list)
        for route in routes:
            by_component[route.component_id].append(route)

        traced: list[CandidateRoute] = []
        for route in routes:
            alternatives = tuple(
                item
                for item in by_component[route.component_id]
                if item.route_id != route.route_id
                and item.eligibility is EvidenceStatus.PASS
                and not item.approval_requirements
            )
            feasible_outcome = (
                "eligible_on_time"
                if route.feasible_deadlines
                else "no_feasible_deadline"
            )
            international_codes = tuple(
                item for item in route.exception_codes if ":condition_" in item
            )
            if not international_codes:
                component_international_codes = tuple(
                    code
                    for candidate in by_component[route.component_id]
                    for code in candidate.exception_codes
                    if ":condition_" in code
                )
                if any(
                    item.endswith("condition_b")
                    for item in component_international_codes
                ):
                    domestic_outcome = "skipped"
                elif any(
                    item.endswith(("condition_a", "condition_c"))
                    for item in component_international_codes
                ):
                    domestic_outcome = "moot"
                else:
                    domestic_outcome = "not_reached"
            elif any(item.endswith("condition_b") for item in international_codes):
                domestic_outcome = "skipped"
            elif any(
                item.endswith(("condition_a", "condition_c"))
                for item in international_codes
            ):
                domestic_outcome = "moot"
            else:
                domestic_outcome = "not_reached"

            supplier = suppliers[route.supplier_id]
            strategic = _normalise_text(supplier.relationship_tier) == "strategic"
            strategic_penalty_deadlines: list[str] = []
            if not strategic:
                for due in route.feasible_deadlines:
                    candidates = tuple(
                        item
                        for item in alternatives
                        if due in item.feasible_deadlines
                        and _normalise_text(
                            suppliers[item.supplier_id].relationship_tier
                        )
                        == "strategic"
                    )
                    if not candidates:
                        continue
                    best = min(candidates, key=lambda item: item.unit_price)
                    if best.unit_price == ZERO:
                        within = route.unit_price >= ZERO
                    else:
                        savings = (best.unit_price - route.unit_price) / best.unit_price
                        within = savings <= Decimal("0.15")
                    if within:
                        strategic_penalty_deadlines.append(due.isoformat())

            rating = _rating(supplier.sustainability_rating)
            sustainability_penalty_deadlines: list[str] = []
            if rating is not None:
                for due in route.feasible_deadlines:
                    for alternative in alternatives:
                        if due not in alternative.feasible_deadlines:
                            continue
                        other_rating = _rating(
                            suppliers[alternative.supplier_id].sustainability_rating
                        )
                        if other_rating is None or other_rating <= rating:
                            continue
                        low_price = min(route.unit_price, alternative.unit_price)
                        price_comparable = (
                            route.unit_price == alternative.unit_price
                            if low_price == ZERO
                            else abs(route.unit_price - alternative.unit_price) / low_price
                            <= Decimal("0.10")
                        )
                        delivery_comparable = (
                            _business_day_distance(
                                route.material_available_date,
                                alternative.material_available_date,
                            )
                            <= 5
                        )
                        if price_comparable and delivery_comparable:
                            sustainability_penalty_deadlines.append(due.isoformat())
                            break

            compared = tuple(item.route_id for item in alternatives)
            trace = (
                ComparatorTrace(
                    1,
                    "on_time_feasibility",
                    feasible_outcome,
                    "Material availability was compared with each scoped demand deadline.",
                    compared,
                    (delivery_rule.rule_id,),
                ),
                ComparatorTrace(
                    2,
                    "domestic_preference",
                    domestic_outcome,
                    "The domestic comparator is skipped only for §3(b), moot for §3(a)/(c), and not reached when the gate is shut.",
                    compared,
                    domestic_rule_ids,
                ),
                ComparatorTrace(
                    3,
                    "strategic_retention",
                    "penalty:" + ",".join(strategic_penalty_deadlines)
                    if strategic_penalty_deadlines
                    else "no_penalty",
                    "A non-Strategic route is penalized only where its savings do not strictly exceed 15%.",
                    compared,
                    (strategic_rule.rule_id,),
                ),
                ComparatorTrace(
                    4,
                    "sustainability_band",
                    "penalty:" + ",".join(sustainability_penalty_deadlines)
                    if sustainability_penalty_deadlines
                    else "no_penalty",
                    "The rating preference applies only inside the inclusive 10%-price and five-business-day window.",
                    compared,
                    (sustainability_rule.rule_id,),
                ),
                ComparatorTrace(
                    5,
                    "known_landed_cost",
                    str(route.unit_price),
                    "Known catalog price is used because no additional landed-cost fact is represented.",
                    compared,
                    (cost_rule.rule_id,),
                ),
                ComparatorTrace(
                    6,
                    "shorter_lead_time",
                    str(route.lead_time_days),
                    "Shorter supplier lead time wins after the policy comparators.",
                    compared,
                    (delivery_rule.rule_id,),
                ),
                ComparatorTrace(
                    7,
                    "id_free_fingerprint",
                    f"{route.supplier_fingerprint}:{route.route_fingerprint}",
                    "The final deterministic key excludes supplier and component database IDs.",
                    compared,
                ),
            )
            traced.append(replace(route, comparator_trace=trace))
        return tuple(traced)

    def _mark_ambiguous_fingerprints(
        self, routes: Sequence[CandidateRoute]
    ) -> tuple[tuple[CandidateRoute, ...], tuple[CandidateAlert, ...]]:
        suppliers_by_hash: dict[str, set[str]] = defaultdict(set)
        for route in routes:
            suppliers_by_hash[route.supplier_fingerprint].add(route.supplier_id)
        collisions = {
            key for key, values in suppliers_by_hash.items() if len(values) > 1
        }
        if not collisions:
            return tuple(routes), ()
        marked = []
        alerts = []
        for route in routes:
            if route.supplier_fingerprint not in collisions:
                marked.append(route)
                continue
            evidence = route.evidence + (
                _synthetic_evidence(
                    "DATA-QUALITY.ambiguous_supplier_fingerprint",
                    EvidenceStatus.UNKNOWN,
                    "Multiple supplier rows have the same ID-free semantic fingerprint; autonomous tie-breaking is unsafe.",
                    assumptions=("AMBIGUOUS_SUPPLIER_FINGERPRINT",),
                    disposition=PlanDisposition.DECISION_REQUIRED,
                ),
            )
            marked.append(
                replace(route, eligibility=EvidenceStatus.UNKNOWN, evidence=evidence)
            )
            alerts.append(
                CandidateAlert(
                    AlertCategory.DATA_QUALITY,
                    "AMBIGUOUS_SUPPLIER_FINGERPRINT",
                    "Semantically indistinguishable supplier rows prevent autonomous selection.",
                    component_id=route.component_id,
                    supplier_id=route.supplier_id,
                    route_id=route.route_id,
                    rule_ids=("DATA-QUALITY.ambiguous_supplier_fingerprint",),
                )
            )
        return tuple(marked), tuple(alerts)

    def _rejections(
        self, routes: Sequence[CandidateRoute]
    ) -> tuple[CandidateRejection, ...]:
        result: list[CandidateRejection] = []
        for route in routes:
            blocking = tuple(
                item
                for item in route.evidence
                if item.severity is RuleSeverity.HARD
                and item.status in {EvidenceStatus.FAIL, EvidenceStatus.UNKNOWN}
                and not (
                    item.status is EvidenceStatus.UNKNOWN
                    and item.scope is EvidenceScope.RULE
                    and item.contract_disposition
                    is PlanDisposition.EXECUTE_WITH_ASSUMPTION
                )
            )
            for item in blocking:
                result.append(
                    CandidateRejection(
                        route.route_id,
                        route.component_id,
                        route.supplier_id,
                        route.eligibility,
                        "POLICY_GATE_FAILED"
                        if item.status is EvidenceStatus.FAIL
                        else "POLICY_GATE_UNRESOLVED",
                        item.summary,
                        (item.rule_id,),
                    )
                )
            if route.approval_requirements:
                result.append(
                    CandidateRejection(
                        route.route_id,
                        route.component_id,
                        route.supplier_id,
                        route.eligibility,
                        "APPROVAL_OR_CERTIFICATE_REQUIRED",
                        "The route has unresolved approval or proof gates.",
                        route.approval_requirements,
                    )
                )
            if not route.feasible_deadlines:
                result.append(
                    CandidateRejection(
                        route.route_id,
                        route.component_id,
                        route.supplier_id,
                        route.eligibility,
                        "NO_FEASIBLE_DEADLINE",
                        "The route cannot make material available by any deadline in its permitted scope.",
                    )
                )
        return tuple(result)

    def build(
        self, snapshot: ScenarioSnapshot, ledgers: LedgerBuildResult
    ) -> CandidateBuildResult:
        if not isinstance(snapshot, ScenarioSnapshot):
            raise TypeError("snapshot must be ScenarioSnapshot")
        if not isinstance(ledgers, LedgerBuildResult):
            raise TypeError("ledgers must be LedgerBuildResult")
        facts = self._base_offer_facts(snapshot)
        gate = self._gate_by_component(snapshot, ledgers, facts)
        routes: list[CandidateRoute] = []
        for item in facts:
            routes.extend(self._build_standard_routes(snapshot, ledgers, item, gate))
            routes.extend(self._build_air_routes(snapshot, ledgers, item, gate))
        routes, alerts = self._mark_ambiguous_fingerprints(routes)
        routes = self._comparator_traces(snapshot, routes)
        return CandidateBuildResult(
            routes=routes,
            rejections=self._rejections(routes),
            alerts=alerts,
        )


def build_candidate_routes(
    snapshot: ScenarioSnapshot,
    ledgers: LedgerBuildResult,
    *,
    registry: PolicyRegistry | None = None,
    contract: EvidenceContract = EvidenceContract.BENCHMARK,
    receiving_buffer_days: int = 0,
    accepted_shipment_pairs: Iterable[tuple[str, str]] = (),
    no_alternative_proofs: Iterable[NoAlternativeProof] = (),
    approved_air_route_fingerprints: Iterable[str] = (),
    approved_below_b_route_fingerprints: Iterable[str] = (),
    air_period_spend: Decimal | None = None,
) -> CandidateBuildResult:
    """Convenience wrapper around :class:`CandidateBuilder`."""

    return CandidateBuilder(
        registry,
        contract,
        receiving_buffer_days=receiving_buffer_days,
        accepted_shipment_pairs=accepted_shipment_pairs,
        no_alternative_proofs=no_alternative_proofs,
        approved_air_route_fingerprints=approved_air_route_fingerprints,
        approved_below_b_route_fingerprints=approved_below_b_route_fingerprints,
        air_period_spend=air_period_spend,
    ).build(snapshot, ledgers)


build_candidates = build_candidate_routes


class AmbiguousFingerprintError(ValueError):
    """Raised rather than using a surrogate ID to resolve a semantic tie."""


def _compare_routes(
    left: CandidateRoute,
    right: CandidateRoute,
    suppliers: Mapping[str, Supplier],
    due_date: date | None,
) -> int:
    left_feasible = bool(left.feasible_deadlines) if due_date is None else due_date in left.feasible_deadlines
    right_feasible = bool(right.feasible_deadlines) if due_date is None else due_date in right.feasible_deadlines
    if left_feasible != right_feasible:
        return -1 if left_feasible else 1

    # Under §3(b) the premium test has already adjudicated price, so the
    # preference is skipped.  Under §3(a)/(c) it is moot because domestic
    # supply cannot serve the scoped bucket or does not exist.  A shut gate
    # never reaches this comparator because that route is ineligible.  Thus no
    # open-gate international route receives a bare domestic penalty here;
    # feasibility and the remaining comparators decide.

    left_supplier = suppliers[left.supplier_id]
    right_supplier = suppliers[right.supplier_id]
    left_strategic = _normalise_text(left_supplier.relationship_tier) == "strategic"
    right_strategic = _normalise_text(right_supplier.relationship_tier) == "strategic"
    if left_strategic != right_strategic:
        strategic_route = left if left_strategic else right
        alternative = right if left_strategic else left
        if strategic_route.unit_price == ZERO:
            retain = True
        else:
            savings = (
                strategic_route.unit_price - alternative.unit_price
            ) / strategic_route.unit_price
            retain = savings <= Decimal("0.15")
        if retain:
            return -1 if left_strategic else 1

    left_rating = _rating(left_supplier.sustainability_rating)
    right_rating = _rating(right_supplier.sustainability_rating)
    if (
        left_rating is not None
        and right_rating is not None
        and left_rating != right_rating
    ):
        minimum_price = min(left.unit_price, right.unit_price)
        comparable_price = (
            left.unit_price == right.unit_price
            if minimum_price == ZERO
            else abs(left.unit_price - right.unit_price) / minimum_price
            <= Decimal("0.10")
        )
        comparable_delivery = (
            _business_day_distance(
                left.material_available_date, right.material_available_date
            )
            <= 5
        )
        if comparable_price and comparable_delivery:
            return -1 if left_rating > right_rating else 1

    if left.unit_price != right.unit_price:
        return -1 if left.unit_price < right.unit_price else 1
    if left.lead_time_days != right.lead_time_days:
        return -1 if left.lead_time_days < right.lead_time_days else 1
    left_key = (left.supplier_fingerprint, left.route_fingerprint)
    right_key = (right.supplier_fingerprint, right.route_fingerprint)
    if left_key == right_key and left.supplier_id != right.supplier_id:
        raise AmbiguousFingerprintError(
            "distinct supplier rows have the same ID-free commercial tie key"
        )
    return -1 if left_key < right_key else 1 if left_key > right_key else 0


def rank_candidate_routes(
    routes: Iterable[CandidateRoute],
    suppliers: Iterable[Supplier],
    *,
    due_date: date | None = None,
) -> tuple[CandidateRoute, ...]:
    """Order routes with the §7 lexicographic comparator chain."""

    if due_date is not None:
        _require_date(due_date, "due_date")
    route_tuple = tuple(routes)
    supplier_map = {item.supplier_id: item for item in suppliers}
    if any(item.supplier_id not in supplier_map for item in route_tuple):
        raise ValueError("every route requires its supplier row")
    eligible = tuple(
        item
        for item in route_tuple
        if item.eligibility is EvidenceStatus.PASS
        and not item.approval_requirements
    )
    return tuple(
        sorted(
            eligible,
            key=cmp_to_key(
                lambda left, right: _compare_routes(
                    left, right, supplier_map, due_date
                )
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class GreedyProblem:
    """Small adapter input used until the certified optimizer replaces it."""

    component_id: str
    unit_of_measure: str
    net_requirement: Decimal
    routes: tuple[CandidateRoute, ...]
    demand_buckets: tuple[DemandBucket, ...]
    suppliers: tuple[Supplier, ...]

    def __post_init__(self) -> None:
        if not self.component_id or not self.unit_of_measure:
            raise ValueError("component_id and unit_of_measure must be non-empty")
        if not isinstance(self.net_requirement, Decimal) or self.net_requirement < ZERO:
            raise TypeError("net_requirement must be a nonnegative Decimal")
        object.__setattr__(self, "routes", tuple(self.routes))
        object.__setattr__(self, "demand_buckets", tuple(self.demand_buckets))
        object.__setattr__(self, "suppliers", tuple(self.suppliers))


class GreedySolver:
    """Comparator-ordered diagnostic allocator with no proof authority."""

    def solve(self, problem: GreedyProblem, /) -> SolverResult:
        if not isinstance(problem, GreedyProblem):
            raise TypeError("problem must be GreedyProblem")
        stage = SolverStageResult(
            stage_name="greedy_diagnostic",
            status=SolverStatus.UNRESOLVED,
            objective_value=None,
            mip_gap=None,
            certificate_complete=False,
            hit_resource_limit=False,
            message="Heuristic ordering supplies no optimality or infeasibility certificate.",
        )
        # Exceptions include international permissions, air freight, below-B
        # proof-dependent routes, and any future scoped relaxation.  The
        # interim solver must not consume or make claims about any of them.
        ordinary_routes = tuple(
            route
            for route in problem.routes
            if not route.exception_codes
            and route.eligibility is EvidenceStatus.PASS
            and not route.approval_requirements
            and route.feasible_deadlines
        )
        if problem.net_requirement == ZERO or not ordinary_routes:
            return SolverResult(
                component_id=problem.component_id,
                solve_kind=SolveKind.EXECUTABLE,
                status=SolverStatus.UNRESOLVED,
                stage_results=(stage,),
                candidate_plan=None,
                objective_vector=(),
                exact_post_validated=False,
                message="No ordinary diagnostic allocation was formed; proof-dependent conclusions are withheld.",
            )
        try:
            ranked = rank_candidate_routes(ordinary_routes, problem.suppliers)
        except AmbiguousFingerprintError as error:
            return SolverResult(
                component_id=problem.component_id,
                solve_kind=SolveKind.EXECUTABLE,
                status=SolverStatus.UNRESOLVED,
                stage_results=(stage,),
                candidate_plan=None,
                objective_vector=(),
                exact_post_validated=False,
                message=f"DATA_QUALITY: {error}",
            )
        route = ranked[0]
        quantity = aggregate_round_and_apply_moq(
            (problem.net_requirement,),
            unit_of_measure=problem.unit_of_measure,
            minimum_order_quantity=route.minimum_order_quantity,
        ).order_quantity
        usable_buckets = tuple(
            item
            for item in problem.demand_buckets
            if item.component_id == problem.component_id
            and item.due_date in route.feasible_deadlines
        )
        if not usable_buckets:
            return SolverResult(
                component_id=problem.component_id,
                solve_kind=SolveKind.EXECUTABLE,
                status=SolverStatus.UNRESOLVED,
                stage_results=(stage,),
                candidate_plan=None,
                objective_vector=(),
                exact_post_validated=False,
                message="No ordinary route serves a demand bucket; proof-dependent conclusions are withheld.",
            )
        allocation = BucketAllocation(
            due_date=usable_buckets[-1].due_date,
            quantity=quantity,
        )
        line = PlanLine(
            route_id=route.route_id,
            component_id=route.component_id,
            supplier_id=route.supplier_id,
            quantity=quantity,
            unit_price=route.unit_price,
            order_date=route.order_date,
            expected_delivery_date=route.expected_delivery_date,
            material_available_date=route.material_available_date,
            allocation_group_id=f"greedy-{component_fingerprint_from_route(route)}",
            bucket_allocations=(allocation,),
        )
        covered = min(problem.net_requirement, quantity)
        plan = CandidatePlan(
            plan_id=f"greedy-{route.route_id}",
            component_id=problem.component_id,
            disposition=PlanDisposition.DECISION_REQUIRED,
            lines=(line,),
            net_requirement=problem.net_requirement,
            eventual_covered_quantity=covered,
            residual_gap=problem.net_requirement - covered,
            total_cost=line.line_total,
            summary="Uncertified ordinary-route heuristic diagnostic; no policy conclusion is drawn.",
        )
        return SolverResult(
            component_id=problem.component_id,
            solve_kind=SolveKind.EXECUTABLE,
            status=SolverStatus.UNRESOLVED,
            stage_results=(stage,),
            candidate_plan=plan,
            objective_vector=(),
            exact_post_validated=False,
            message="A non-executable heuristic diagnostic was formed.",
        )


def component_fingerprint_from_route(route: CandidateRoute) -> str:
    """Stable short grouping token when only the frozen route is available."""

    return route.route_fingerprint[:20]


__all__ = [
    "AmbiguousFingerprintError",
    "CandidateAlert",
    "CandidateBuildResult",
    "CandidateBuilder",
    "CandidateRejection",
    "DomesticGateCondition",
    "DomesticGateDecision",
    "GreedyProblem",
    "GreedySolver",
    "NoAlternativeProof",
    "build_candidate_routes",
    "build_candidates",
    "component_fingerprint",
    "evaluate_domestic_gate",
    "rank_candidate_routes",
    "route_fingerprint",
    "supplier_fingerprint",
]
