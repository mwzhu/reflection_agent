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
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from enum import Enum
import hashlib
from itertools import combinations
import json
import re
import unicodedata

from .config import EvidenceContract
from .domain import (
    AlertCategory,
    CandidatePlan,
    Component,
    DecisionRecord,
    EvidenceBasis,
    EvidenceScope,
    EvidenceStatus,
    FulfillmentStatus,
    PlanDisposition,
    PlanLine,
    ResolutionStatus,
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


@dataclass(frozen=True, slots=True)
class EconomicAutonomy:
    """The provisional §8.3 bounds, injectable when Apex supplies its own."""

    max_surplus_fraction: Decimal = Decimal("0.10")
    max_surplus_units: Decimal | None = None
    max_excess_cost_usd: Decimal = Decimal("2500")

    def __post_init__(self) -> None:
        for name in ("max_surplus_fraction", "max_excess_cost_usd"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < ZERO:
                raise ValueError(f"{name} must be a finite nonnegative Decimal")
        if self.max_surplus_units is not None and (
            not isinstance(self.max_surplus_units, Decimal)
            or not self.max_surplus_units.is_finite()
            or self.max_surplus_units < ZERO
        ):
            raise ValueError("max_surplus_units must be a finite nonnegative Decimal or None")


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


class IndependentPlanValidator:
    """Rebuild source facts and independently certify proposed decisions."""

    def __init__(
        self,
        registry: PolicyRegistry | None = None,
        *,
        autonomy: EconomicAutonomy = EconomicAutonomy(),
        receiving_buffer_days: int = 0,
        accepted_shipment_pairs: Iterable[tuple[str, str]] = (),
        approved_rule_ids: Iterable[str] = (),
        capacity_confirmed_supplier_ids: Iterable[str] = (),
        numeric_capacity_by_supplier: Mapping[str, Decimal] | None = None,
        enumeration_node_limit: int = 2_000_000,
    ) -> None:
        self.registry = registry or load_policy_registry()
        if not isinstance(self.registry, PolicyRegistry):
            raise TypeError("registry must be PolicyRegistry")
        if not isinstance(autonomy, EconomicAutonomy):
            raise TypeError("autonomy must be EconomicAutonomy")
        if not isinstance(receiving_buffer_days, int) or isinstance(receiving_buffer_days, bool) or receiving_buffer_days < 0:
            raise ValueError("receiving_buffer_days must be a nonnegative int")
        if not isinstance(enumeration_node_limit, int) or enumeration_node_limit <= 0:
            raise ValueError("enumeration_node_limit must be positive")
        self.autonomy = autonomy
        self.receiving_buffer_days = receiving_buffer_days
        self.accepted_shipment_pairs = frozenset(tuple(item) for item in accepted_shipment_pairs)
        self.approved_rule_ids = frozenset(approved_rule_ids)
        self.capacity_confirmed_supplier_ids = frozenset(capacity_confirmed_supplier_ids)
        self.numeric_capacity_by_supplier = dict(numeric_capacity_by_supplier or {})
        if any(value < ZERO or not value.is_finite() for value in self.numeric_capacity_by_supplier.values()):
            raise ValueError("numeric capacity values must be finite and nonnegative")
        self.enumeration_node_limit = enumeration_node_limit
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
            return EvidenceStatus.UNKNOWN if EvidenceStatus.UNKNOWN in statuses else EvidenceStatus.FAIL
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
                return False, False
            assumption = True
        pcb_rules = self._rules(snapshot.configuration.current_date, "incumbent_supplier_only")
        if pcb_rules and self._concept("printed_circuit_board_component", requirement.component) is EvidenceStatus.PASS:
            rule = pcb_rules[0]
            pair = (requirement.component.component_id, supplier.supplier_id)
            if pair not in self.accepted_shipment_pairs:
                if contract is EvidenceContract.PRODUCTION:
                    return False, False
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
        rules = self._directive_rules(snapshot.configuration.current_date, "named_primary_supplier")
        if not rules or self._concept("neodymium_magnet", component) is not EvidenceStatus.PASS:
            return None
        reference = rules[0].data["directive"]["supplier"]
        return self.resolve_source_named_entity(reference, snapshot.suppliers)

    def _release_subject(self, snapshot: ScenarioSnapshot, component: Component) -> NamedEntityCheck | None:
        rules = self._directive_rules(snapshot.configuration.current_date, "named_primary_supplier")
        if not rules or self._concept("neodymium_magnet", component) is not EvidenceStatus.PASS:
            return None
        release = rules[0].data.get("release_condition")
        if not isinstance(release, Mapping) or not isinstance(release.get("subject"), Mapping):
            return None
        return self.resolve_source_named_entity(release["subject"], snapshot.suppliers)

    def _minimum_secondary(self, snapshot: ScenarioSnapshot, component: Component) -> Decimal | None:
        for rule in self._rules(snapshot.configuration.current_date, "minimum_secondary_fraction"):
            if self._concept("neodymium_magnet", component) is EvidenceStatus.PASS:
                return Decimal(str(rule.data["constraint"]["value"]))
        return None

    def _increment(self, component: Component) -> Decimal:
        unit = " ".join(_tokens(component.unit_of_measure))
        return Decimal("0.01") if unit in {"kg", "meter"} else Decimal("1")

    def _allocation_surplus_bound(
        self, requirement: _SourceRequirement, catalogs: Sequence[SupplierCatalogLine], secondary: Decimal | None
    ) -> Decimal:
        if secondary is None or not catalogs:
            return ZERO
        largest = max(item.minimum_order_quantity for item in catalogs)
        fraction_bound = largest / secondary if secondary > ZERO else largest
        all_moq = sum((item.minimum_order_quantity for item in catalogs), ZERO)
        return max(ZERO, fraction_bound - requirement.eventual_gap, all_moq - requirement.eventual_gap)

    def derive_upper_bounds(
        self,
        snapshot: ScenarioSnapshot,
        requirement: _SourceRequirement,
        catalogs: Sequence[SupplierCatalogLine],
    ) -> dict[str, Decimal]:
        """Independently derive the §8.2 Big-M values (equality is legal)."""

        recovery = max(
            (
                max(ZERO, requirement.cumulative_demand(due) - requirement.on_time_supply(due))
                for due, _quantity in requirement.bucket_quantities
            ),
            default=ZERO,
        )
        secondary = self._minimum_secondary(snapshot, requirement.component)
        allocation = self._allocation_surplus_bound(requirement, catalogs, secondary)
        base = requirement.total_demand + recovery + allocation
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
        threshold = Decimal("0.50") if critical is not EvidenceStatus.FAIL else Decimal("0.35")
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
    ) -> tuple[_Offer, ...]:
        suppliers = {item.supplier_id: item for item in snapshot.suppliers}
        catalogs = tuple(item for item in snapshot.catalog_lines if item.component_id == requirement.component.component_id)
        eligible: list[tuple[Supplier, SupplierCatalogLine, bool]] = []
        for catalog in catalogs:
            supplier = suppliers[catalog.supplier_id]
            allowed, assumption = self._hard_eligible(snapshot, requirement, supplier, catalog, contract)
            if allowed:
                eligible.append((supplier, catalog, assumption))
        gate = {
            due: self._domestic_gate(snapshot, requirement, due, tuple((supplier, catalog) for supplier, catalog, _assumption in eligible))
            for due, _quantity in requirement.bucket_quantities
        }
        upper = self.derive_upper_bounds(snapshot, requirement, catalogs)
        result: list[_Offer] = []
        for supplier, catalog, _assumption in eligible:
            international = self._concept("international_supplier", supplier) is EvidenceStatus.PASS
            allowed_buckets = tuple(
                due for due, _quantity in requirement.bucket_quantities if not international or gate[due] is not None
            )
            if not allowed_buckets:
                continue
            expected = snapshot.configuration.current_date + timedelta(days=catalog.lead_time_days)
            pcb = self._concept("printed_circuit_board_component", requirement.component) is EvidenceStatus.PASS
            buffer = self.receiving_buffer_days if requirement.component.is_hazardous or pcb else 0
            bound = upper[catalog.supplier_id]
            if international:
                allowance = sum((requirement.bucket_shortage(due) for due in allowed_buckets), ZERO)
                bound = min(bound, allowance)
            if bound < catalog.minimum_order_quantity:
                continue
            review_keys = self._review_keys(
                snapshot, requirement, supplier, contract
            )
            supplier_hash = _supplier_fingerprint(supplier)
            route_hash = _route_fingerprint(
                requirement.component, catalog, "standard", catalog.lead_time_days
            )
            result.append(
                _Offer(
                    requirement.component,
                    supplier,
                    catalog,
                    expected,
                    expected + timedelta(days=buffer),
                    catalog.lead_time_days,
                    "standard",
                    international,
                    review_keys,
                    allowed_buckets,
                    tuple((due, gate[due] or "shut") for due, _quantity in requirement.bucket_quantities),
                    bound,
                    supplier_hash,
                    route_hash,
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
            air_allowed = tuple(
                due
                for due in allowed_buckets
                if expected + timedelta(days=buffer) > due
                and snapshot.configuration.current_date + timedelta(days=air_lead + buffer) <= due
            )
            if not air_allowed:
                continue
            approval_rules = (
                *self._rules(snapshot.configuration.current_date, "air_freight_individual_approval"),
                *self._rules(snapshot.configuration.current_date, "air_freight_period_spend_cap"),
            )
            approvals_satisfied = all(item.rule_id in self.approved_rule_ids for item in approval_rules)
            if not include_unapproved and not approvals_satisfied:
                continue
            air_expected = snapshot.configuration.current_date + timedelta(days=air_lead)
            air_bound = min(
                upper[catalog.supplier_id],
                sum((requirement.bucket_shortage(due) for due in air_allowed), ZERO),
            )
            if air_bound < catalog.minimum_order_quantity:
                continue
            air_route_hash = _route_fingerprint(
                requirement.component, catalog, "air freight", air_lead
            )
            result.append(
                _Offer(
                    requirement.component,
                    supplier,
                    catalog,
                    air_expected,
                    air_expected + timedelta(days=buffer),
                    air_lead,
                    "air freight",
                    international,
                    review_keys,
                    air_allowed,
                    tuple((due, gate[due] or "shut") for due, _quantity in requirement.bucket_quantities),
                    air_bound,
                    supplier_hash,
                    air_route_hash,
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

    def _objective(
        self,
        snapshot: ScenarioSnapshot,
        requirement: _SourceRequirement,
        plan: CandidatePlan,
        offers: Sequence[_Offer],
    ) -> tuple[Decimal, ...]:
        matched = {line.route_id: self._match_offer(offers, line) for line in plan.lines}
        total_quantity = sum((line.quantity for line in plan.lines), ZERO)
        physical_gaps: list[Decimal] = []
        for due, _quantity in requirement.bucket_quantities:
            planned = sum(
                (line.quantity for line in plan.lines if line.material_available_date <= due), ZERO
            )
            physical_gaps.append(max(ZERO, requirement.cumulative_demand(due) - requirement.on_time_supply(due) - planned))
        unit_late = sum(
            (
                allocation.quantity * Decimal(max(0, (line.material_available_date - allocation.due_date).days))
                for line in plan.lines
                for allocation in line.bucket_allocations
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
                        if savings <= Decimal("0.15"):
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
                            <= Decimal("0.10")
                        )
                        and _business_days(item.material_available, offer.material_available) <= 5
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
            max(ZERO, total_quantity - requirement.eventual_gap),
            weighted_lead,
            Decimal(len(plan.lines)),
        )

    def _synthetic_objective(
        self,
        snapshot: ScenarioSnapshot,
        requirement: _SourceRequirement,
        offers: Sequence[_Offer],
        allocation: Mapping[int, Decimal],
        q_min: Decimal,
    ) -> tuple[Decimal, ...]:
        physical_gaps = tuple(
            max(
                ZERO,
                requirement.cumulative_demand(due)
                - requirement.on_time_supply(due)
                - sum((quantity for index, quantity in allocation.items() if offers[index].material_available <= due), ZERO),
            )
            for due, _bucket in requirement.bucket_quantities
        )
        unit_late = ZERO
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
            due = min(
                offer.allowed_buckets,
                key=lambda item: (
                    max(0, (offer.material_available - item).days),
                    0 if offer.condition_for(item) == "b" else 1,
                    item,
                ),
            )
            unit_late += quantity * Decimal(max(0, (offer.material_available - due).days))
            if offer.international and offer.condition_for(due) != "b":
                international += quantity
            if self._concept("strategic_supplier", offer.supplier) is EvidenceStatus.FAIL:
                strategic_options = tuple(
                    item for item in offers
                    if due in item.allowed_buckets and self._concept("strategic_supplier", item.supplier) is EvidenceStatus.PASS
                )
                if strategic_options:
                    best = min(strategic_options, key=lambda item: item.catalog.unit_price)
                    savings = ZERO if best.catalog.unit_price == ZERO else (best.catalog.unit_price - offer.catalog.unit_price) / best.catalog.unit_price
                    if savings <= Decimal("0.15"):
                        strategic += quantity
            current_rating = _rating(offer.supplier.sustainability_rating)
            if current_rating is not None:
                better = tuple(
                    item for item in offers
                    if due in item.allowed_buckets
                    and _rating(item.supplier.sustainability_rating) is not None
                    and _rating(item.supplier.sustainability_rating) > current_rating
                    and (item.catalog.unit_price == offer.catalog.unit_price if min(item.catalog.unit_price, offer.catalog.unit_price) == ZERO else abs(item.catalog.unit_price - offer.catalog.unit_price) / min(item.catalog.unit_price, offer.catalog.unit_price) <= Decimal("0.10"))
                    and _business_days(item.material_available, offer.material_available) <= 5
                )
                if better:
                    best_rating = max(_rating(item.supplier.sustainability_rating) for item in better)
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
            max(ZERO, total - requirement.eventual_gap),
            lead,
            Decimal(sum(1 for value in allocation.values() if value > ZERO)),
        )

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

    def independently_solve(
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
        increment = self._increment(requirement.component)
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
        derived = self.derive_upper_bounds(snapshot, requirement, catalogs)
        group_bound = max(
            [requirement.eventual_gap]
            + list(derived.values())
            + [sum((item.minimum_order_quantity for item in catalogs), ZERO)]
        ) if offers else ZERO
        maximum_units = _floor_units(group_bound, increment)
        target_units = _ceil_units(requirement.eventual_gap, increment)
        nodes = [0]
        best_q: tuple[Decimal, dict[int, Decimal]] | None = None
        best_baseline: tuple[tuple[Decimal, Decimal], dict[int, Decimal]] | None = None
        # Q first establishes the maximum coverage and then the least total.
        for total_units in range(0 if target_units == 0 else 1, maximum_units + 1):
            vectors = self._vectors_for_total(total_units, offers, increment, secondary, named_supplier, nodes)
            for vector in vectors:
                total = Decimal(total_units) * increment
                coverage = min(requirement.eventual_gap, total)
                if best_q is None or coverage > best_q[0] or (coverage == best_q[0] and total < sum(best_q[1].values(), ZERO)):
                    best_q = (coverage, vector)
                uncovered = max(ZERO, requirement.eventual_gap - coverage)
                cost = sum((offers[index].catalog.unit_price * quantity for index, quantity in vector.items()), ZERO)
                key = (uncovered, cost)
                if best_baseline is None or key < best_baseline[0]:
                    best_baseline = (key, vector)
            if nodes[0] > self.enumeration_node_limit:
                return IndependentSolve(SolverStatus.RESOURCE_LIMIT, (), certificate_complete=False)
            if solve_kind is SolveKind.QUANTITY_CALIBRATION and best_q is not None and best_q[0] == requirement.eventual_gap:
                total = sum(best_q[1].values(), ZERO)
                return IndependentSolve(
                    SolverStatus.OPTIMAL,
                    (ZERO, total),
                    tuple(sorted((offers[index].supplier.supplier_id, quantity) for index, quantity in best_q[1].items())),
                    minimum_compliant_total=total,
                )
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
                objective = self._synthetic_objective(snapshot, requirement, offers, vector, q_min)
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
        group_ids = {line.allocation_group_id for line in plan.lines}
        if len(group_ids) != 1:
            sink.error("ALLOCATION_GROUP_MISMATCH", "All lines for one component/run must share one allocation group.", component_id=component_id, plan_id=plan_id)
        increment = self._increment(requirement.component)
        derived_u = self.derive_upper_bounds(snapshot, requirement, tuple(item for item in snapshot.catalog_lines if item.component_id == component_id))
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
            if line.quantity < catalog.minimum_order_quantity:
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
            eligible, _assumption = self._hard_eligible(snapshot, requirement, supplier, catalog, decision.evidence_contract)
            if not eligible:
                sink.error("SUPPLIER_INELIGIBLE", "Plan uses a supplier failing ASL, certification, rating, or scoped incumbency gates.", component_id=component_id, plan_id=plan_id)
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
                    exception_dates[exception_id].add(allocation.due_date)
                    if "condition_" in exception_id and not exception_id.endswith(f"condition_{condition}"):
                        sink.error("EXCEPTION_PREDICATE_MISMATCH", "Allocation exception label disagrees with the recomputed bucket predicate.", component_id=component_id, plan_id=plan_id)
            if plan.disposition.writes_purchase_order:
                for rule in self._rules(snapshot.configuration.current_date, "order_value_approval"):
                    threshold = Decimal(str(rule.data["constraint"]["amount_exceeds"]))
                    if line.line_total > threshold and rule.rule_id not in self.approved_rule_ids:
                        sink.error("UNAPPROVED_ORDER_VALUE", "Executable line exceeds an approval threshold without runtime approval evidence.", component_id=component_id, plan_id=plan_id, rule_ids=(rule.rule_id,))
        for exception_id, quantity in exception_totals.items():
            allowance = sum((requirement.bucket_shortage(due) for due in exception_dates[exception_id]), ZERO)
            if quantity > allowance:
                sink.error("EXCEPTION_AGGREGATE_CAP", "Exception quantity exceeds the aggregate net shortage of qualifying buckets.", component_id=component_id, plan_id=plan_id)
        total_quantity = sum((line.quantity for line in plan.lines), ZERO)
        independently_covered = min(requirement.eventual_gap, total_quantity)
        if plan.net_requirement != requirement.eventual_gap or plan.eventual_covered_quantity != independently_covered or plan.residual_gap != requirement.eventual_gap - independently_covered:
            sink.error("PLAN_COVERAGE_MISMATCH", "Plan requirement, eventual coverage, or residual does not match exact source arithmetic.", component_id=component_id, plan_id=plan_id)
        if plan.total_cost != sum((line.quantity * catalogs[(line.component_id, line.supplier_id)].unit_price for line in plan.lines if (line.component_id, line.supplier_id) in catalogs), ZERO):
            sink.error("PLAN_COST_MISMATCH", "Plan total cost does not equal source catalog price times quantity.", component_id=component_id, plan_id=plan_id)
        if plan.minimum_compliant_total is not None:
            forced = max(ZERO, plan.minimum_compliant_total - plan.net_requirement)
            discretionary = max(ZERO, total_quantity - plan.minimum_compliant_total)
            if plan.forced_surplus != forced or plan.discretionary_surplus != discretionary:
                sink.error("SURPLUS_SPLIT_MISMATCH", "Forced and discretionary surplus were not independently split at solve-Q minimum.", component_id=component_id, plan_id=plan_id)
            cap = self.autonomy.max_surplus_fraction * plan.net_requirement
            if self.autonomy.max_surplus_units is not None:
                cap = min(cap, self.autonomy.max_surplus_units)
            if plan.discretionary_surplus > cap and plan.disposition.writes_purchase_order:
                sink.error("AUTONOMY_SURPLUS_EXCEEDED", "Executable plan exceeds the inclusive discretionary-surplus bound.", component_id=component_id, plan_id=plan_id)
        exact_objective = self._objective(snapshot, requirement, plan, offers)
        if plan.unit_late_days != exact_objective[len(requirement.bucket_quantities)]:
            sink.error("UNIT_LATE_DAYS_MISMATCH", "Unit-late-days was not exactly recomputed from material availability and allocations.", component_id=component_id, plan_id=plan_id)
        if plan.objective_vector != exact_objective:
            sink.error("OBJECTIVE_VECTOR_MISMATCH", "Plan objective vector differs from the validator's exact Decimal vector.", component_id=component_id, plan_id=plan_id)
        secondary = self._minimum_secondary(snapshot, requirement.component)
        if secondary is not None:
            quantities: dict[str, Decimal] = defaultdict(Decimal)
            for line in plan.lines:
                quantities[line.supplier_id] += line.quantity
            eligible_count = len({offer.supplier.supplier_id for offer in offers})
            if eligible_count >= 2:
                maximum = (Decimal("1") - secondary) * total_quantity
                if any(value > maximum for value in quantities.values()):
                    sink.error("SECONDARY_ALLOCATION_VIOLATION", "Per-order minimum-secondary allocation is violated.", component_id=component_id, plan_id=plan_id)
            elif plan.disposition.writes_purchase_order:
                sink.error("SECONDARY_ALLOCATION_UNSATISFIABLE", "An executable plan cannot silently drop a minimum-secondary rule with fewer than two eligible suppliers.", component_id=component_id, plan_id=plan_id)
        named_deviation = self._named_deviation(snapshot, requirement.component, {supplier: sum((line.quantity for line in plan.lines if line.supplier_id == supplier), ZERO) for supplier in {line.supplier_id for line in plan.lines}})
        if named_deviation > ZERO:
            if plan.disposition.writes_purchase_order:
                sink.error("NAMED_PRIMARY_DEVIATION", "Executable plan lies outside named-primary membership.", component_id=component_id, plan_id=plan_id)
            elif plan.disposition is not PlanDisposition.DECISION_REQUIRED:
                sink.error("NAMED_PRIMARY_DISPOSITION", "A named-primary-deviating alternative must be DECISION_REQUIRED.", component_id=component_id, plan_id=plan_id)
        self._check_evidence(snapshot, decision, plan, sink)

    def _check_evidence(self, snapshot: ScenarioSnapshot, decision: DecisionRecord, plan: CandidatePlan, sink: _IssueSink) -> None:
        active = {item.rule_id: item for item in self.registry.active_rules(snapshot.configuration.current_date)}
        for evidence in (*decision.evidence, *plan.evidence):
            if evidence.rule_id not in active and not evidence.rule_id.startswith(_SYNTHETIC_RULE_PREFIXES):
                sink.error("INACTIVE_RULE_CITATION", "Evidence cites a policy rule inactive on the scenario date.", component_id=decision.component_id, plan_id=plan.plan_id, rule_ids=(evidence.rule_id,))
            if plan.disposition.writes_purchase_order and evidence.severity is RuleSeverity.HARD and evidence.status is EvidenceStatus.FAIL:
                sink.error("PROVEN_POLICY_VIOLATION", "A proven hard policy failure cannot execute.", component_id=decision.component_id, plan_id=plan.plan_id, rule_ids=(evidence.rule_id,))
            if plan.disposition.writes_purchase_order and evidence.severity is RuleSeverity.HARD and evidence.status is EvidenceStatus.UNKNOWN:
                permitted = evidence.scope is EvidenceScope.RULE and evidence.contract_disposition is PlanDisposition.EXECUTE_WITH_ASSUMPTION and plan.disposition is PlanDisposition.EXECUTE_WITH_ASSUMPTION
                if not permitted:
                    sink.error("UNLICENSED_EVIDENCE", "Hard unknown evidence is not licensed for execution by the active contract.", component_id=decision.component_id, plan_id=plan.plan_id, rule_ids=(evidence.rule_id,))
        rolling_applies = any(
            rule.evidence_basis == EvidenceBasis.ROLLING_WINDOW.value
            for rule in self.registry.active_rules(snapshot.configuration.current_date)
        )
        if rolling_applies and plan.disposition.writes_purchase_order:
            if decision.evidence_contract is EvidenceContract.PRODUCTION:
                sink.error("ROLLING_HISTORY_UNLICENSED", "Production contract cannot execute without licensed rolling history.", component_id=decision.component_id, plan_id=plan.plan_id)
            elif plan.disposition is not PlanDisposition.EXECUTE_WITH_ASSUMPTION:
                sink.error("EVIDENCE_CONTRACT_DISPOSITION", "Benchmark rolling-history unknown requires EXECUTE_WITH_ASSUMPTION.", component_id=decision.component_id, plan_id=plan.plan_id)

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
        if selected is not None:
            for kind in (SolveKind.QUANTITY_CALIBRATION, SolveKind.BASELINE, SolveKind.EXECUTABLE):
                matches = by_kind.get(kind, ())
                if len(matches) != 1:
                    sink.error("SOLVER_RESULT_CARDINALITY", f"Executable decision requires exactly one {kind.value} result.", component_id=decision.component_id)
                    verified = False
                    continue
                result = matches[0]
                if not result.is_certified_optimal or not result.exact_post_validated:
                    sink.error("SOLVER_UNPROVEN", "Feasible incumbent, timeout, gap, or incomplete stage cannot support a PO.", component_id=decision.component_id)
                    verified = False
            if not verified:
                return False
            q_claim = by_kind[SolveKind.QUANTITY_CALIBRATION][0]
            baseline_claim = by_kind[SolveKind.BASELINE][0]
            executable_claim = by_kind[SolveKind.EXECUTABLE][0]
            q_own = self.independently_solve(snapshot, decision, SolveKind.QUANTITY_CALIBRATION)
            baseline_own = self.independently_solve(snapshot, decision, SolveKind.BASELINE)
            executable_own = self.independently_solve(snapshot, decision, SolveKind.EXECUTABLE)
            for name, claim, own in (
                ("solve Q", q_claim, q_own),
                ("solve 0", baseline_claim, baseline_own),
                ("solve 1", executable_claim, executable_own),
            ):
                if own.status is not SolverStatus.OPTIMAL or not own.certificate_complete:
                    sink.error("INDEPENDENT_SOLVE_UNPROVEN", f"Validator could not complete independent {name} enumeration.", component_id=decision.component_id)
                    verified = False
                elif claim.objective_vector != own.objective_vector:
                    sink.error("SOLVER_OBJECTIVE_DISAGREEMENT", f"Planner and independent {name} objective vectors disagree.", component_id=decision.component_id)
                    verified = False
            if q_own.minimum_compliant_total != selected.minimum_compliant_total or q_claim.minimum_compliant_total != q_own.minimum_compliant_total:
                sink.error("CALIBRATION_MISMATCH", "Persisted minimum_compliant_total differs from independent solve Q.", component_id=decision.component_id, plan_id=selected.plan_id)
                verified = False
            if baseline_own.cheapest_covering_cost != selected.cheapest_covering_cost or baseline_claim.cheapest_covering_cost != baseline_own.cheapest_covering_cost:
                sink.error("BASELINE_COST_MISMATCH", "Persisted cheapest_covering_cost differs from independent solve 0.", component_id=decision.component_id, plan_id=selected.plan_id)
                verified = False
            if selected.cheapest_covering_cost is not None and selected.total_cost - selected.cheapest_covering_cost > self.autonomy.max_excess_cost_usd:
                sink.error("AUTONOMY_COST_EXCEEDED", "Executable plan exceeds the excess-cost autonomy bound.", component_id=decision.component_id, plan_id=selected.plan_id)
                verified = False
            exact = self._objective(snapshot, requirement, selected, self._offers(snapshot, requirement, decision.evidence_contract))
            if executable_claim.candidate_plan is None or executable_claim.candidate_plan.plan_id != selected.plan_id:
                sink.error("EXECUTABLE_PLAN_MISMATCH", "Solve-1 selected candidate differs from the decision selected plan.", component_id=decision.component_id)
                verified = False
            if executable_claim.objective_vector != exact or executable_own.objective_vector != exact:
                sink.error("SUBOPTIMAL_INCUMBENT", "Selected feasible plan is not the independently reproduced lexicographic optimum.", component_id=decision.component_id, plan_id=selected.plan_id)
                verified = False
        else:
            executable_results = by_kind.get(SolveKind.EXECUTABLE, ())
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
        action_keys: set[tuple[object, ...]] = set()
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
            self._check_decision_facts(snapshot, decision, requirement, sink)
            offers = self._offers(snapshot, requirement, decision.evidence_contract, include_unapproved=True)
            for plan in tuple(item for item in (decision.selected_plan, *decision.alternatives) if item is not None):
                self._check_plan(snapshot, decision, requirement, plan, offers, sink)
                for line in plan.lines:
                    key = (decision.requirement_id, line.component_id, line.supplier_id, line.route_id, line.order_date)
                    if key in action_keys:
                        sink.error("DUPLICATE_ACTION", "Semantic action identity is duplicated.", component_id=decision.component_id, plan_id=plan.plan_id)
                    action_keys.add(key)
            self._check_capacity(snapshot, decision, sink)
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
            named = self._named_primary(snapshot, requirement.component)
            if named is not None and not named.resolved:
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
            if requirement.eventual_gap <= ZERO:
                continue
            records = by_component.get(component_id, ())
            if len(records) != 1:
                sink.error("INITIAL_GAP_DECISION_CARDINALITY", "Every positive initial gap requires exactly one DecisionRecord.", component_id=component_id)
            elif records[0].selected_plan is None and not (set(records[0].alert_categories) & _TERMINAL_ALERTS):
                sink.error("SILENT_INITIAL_GAP", "An initial gap without executable PO requires a terminal component-specific alert.", component_id=component_id)
        for component_id, records in by_component.items():
            if len(records) > 1:
                sink.error("MULTIPLE_COMPONENT_DECISIONS", "A component requirement may not execute more than one selected decision.", component_id=component_id)
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
                "allocation-and-exception-scoping",
                "catalog-asl-certification-dates",
                "evidence-contract-and-entity-ladder",
                "exact-objective-and-certified-solves",
                "forced-discretionary-surplus-and-autonomy",
                "inbound-delivery-date-netting",
                "no-silent-gap-and-requirement-state",
                "policy-window-comparators-6-8",
                "rationale-citations-and-capacity-disclosure",
                "u-derivation",
            ),
            issues=tuple(sink.values),
        )


PlanValidator = IndependentPlanValidator


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
    "NamedEntityCheck",
    "NamedEntityOutcome",
    "PlanValidator",
    "validate_plans",
]
