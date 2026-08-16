"""Three-valued evaluation of the reviewed policy pack.

This module evaluates facts; it does not choose suppliers or plan quantities.
Callers may provide a proposed quantity/allocation in ``EvaluationContext`` to
ask whether that already-formed proposal satisfies a rule.  Missing facts stay
``UNKNOWN`` and are mapped by the named evidence contract.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from ..config import EvidenceContract
from ..domain import (
    AlertCategory,
    Component,
    EvidenceBasis,
    EvidenceResult,
    EvidenceScope,
    EvidenceStatus,
    ExistingPurchaseOrder,
    PlanDisposition,
    RuleSeverity,
    Supplier,
    SupplierCatalogLine,
    ZERO,
)
from .entity_resolution import (
    ConceptResolution,
    EntityResolver,
    NamedEntityResolution,
    ResolutionAlert,
    canonical_certification,
)
from .registry import PolicyRegistry, PolicyRule


@dataclass(frozen=True, slots=True)
class CapacityConfirmation:
    """One runtime record capable of satisfying a compiled release predicate."""

    supplier_id: str
    predicate: str
    affirmative: bool
    evidence_source: str

    def __post_init__(self) -> None:
        if not self.supplier_id or not self.predicate or not self.evidence_source:
            raise ValueError("capacity confirmation text fields must be non-empty")
        if not isinstance(self.affirmative, bool):
            raise TypeError("affirmative must be bool")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(child) for key, child in value.items()})
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(_freeze(child) for child in value)
    return value


@dataclass(frozen=True, slots=True)
class EvaluationContext:
    """Runtime entities and already-known facts for one rule scope.

    ``facts`` is intentionally open-ended: T05 owns policy evaluation while
    later stages own route and plan representations.  Stable fact names keep
    that dependency one-way without changing the frozen shared contracts.
    """

    scenario_date: date
    suppliers: tuple[Supplier, ...]
    component: Component | None = None
    supplier: Supplier | None = None
    catalog_line: SupplierCatalogLine | None = None
    purchase_orders: tuple[ExistingPurchaseOrder, ...] = ()
    confirmations: tuple[CapacityConfirmation, ...] = ()
    facts: Mapping[str, object] = MappingProxyType({})

    def __post_init__(self) -> None:
        if not isinstance(self.scenario_date, date):
            raise TypeError("scenario_date must be date")
        if any(not isinstance(item, Supplier) for item in self.suppliers):
            raise TypeError("suppliers must contain Supplier values")
        if self.component is not None and not isinstance(self.component, Component):
            raise TypeError("component must be Component or None")
        if self.supplier is not None and not isinstance(self.supplier, Supplier):
            raise TypeError("supplier must be Supplier or None")
        if self.catalog_line is not None and not isinstance(
            self.catalog_line, SupplierCatalogLine
        ):
            raise TypeError("catalog_line must be SupplierCatalogLine or None")
        if any(not isinstance(item, ExistingPurchaseOrder) for item in self.purchase_orders):
            raise TypeError("purchase_orders must contain ExistingPurchaseOrder values")
        if any(not isinstance(item, CapacityConfirmation) for item in self.confirmations):
            raise TypeError("confirmations must contain CapacityConfirmation values")
        if not isinstance(self.facts, Mapping):
            raise TypeError("facts must be a mapping")
        object.__setattr__(self, "suppliers", tuple(self.suppliers))
        object.__setattr__(self, "purchase_orders", tuple(self.purchase_orders))
        object.__setattr__(self, "confirmations", tuple(self.confirmations))
        object.__setattr__(self, "facts", _freeze(self.facts))


@dataclass(frozen=True, slots=True)
class EvaluationAlert:
    category: AlertCategory
    code: str
    message: str
    rule_id: str
    entity_id: str | None = None


@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    rule_id: str
    source_document: str
    constraint_kind: str
    active: bool
    applicable: bool | None
    selector_status: EvidenceStatus
    evidence: EvidenceResult | None
    alerts: tuple[EvaluationAlert, ...] = ()
    superseded_by: str | None = None
    released: bool = False

    @property
    def blocks_scope(self) -> bool:
        if not self.active or self.applicable is False or self.evidence is None:
            return False
        if self.evidence.severity is not RuleSeverity.HARD:
            return False
        return self.evidence.status is EvidenceStatus.FAIL or (
            self.evidence.status is EvidenceStatus.UNKNOWN
            and self.evidence.contract_disposition
            in {PlanDisposition.DECISION_REQUIRED, PlanDisposition.RECOMMEND_APPROVAL}
        )


@dataclass(frozen=True, slots=True)
class EvaluationBatch:
    scenario_date: date
    contract: EvidenceContract
    evaluations: tuple[RuleEvaluation, ...]

    @property
    def active(self) -> tuple[RuleEvaluation, ...]:
        return tuple(item for item in self.evaluations if item.active and item.superseded_by is None)

    @property
    def blocking(self) -> tuple[RuleEvaluation, ...]:
        return tuple(item for item in self.active if item.blocks_scope)

    @property
    def alerts(self) -> tuple[EvaluationAlert, ...]:
        return tuple(alert for item in self.evaluations for alert in item.alerts)


@dataclass(frozen=True, slots=True)
class _PredicateResult:
    status: EvidenceStatus
    summary: str
    assumptions: tuple[str, ...] = ()
    alerts: tuple[EvaluationAlert, ...] = ()


class PolicyEvaluator:
    """Evaluate active rules using Kleene truth tables and declared evidence."""

    def __init__(
        self,
        registry: PolicyRegistry,
        contract: EvidenceContract = EvidenceContract.BENCHMARK,
        *,
        resolver: EntityResolver | None = None,
    ) -> None:
        if not isinstance(registry, PolicyRegistry):
            raise TypeError("registry must be PolicyRegistry")
        if not isinstance(contract, EvidenceContract):
            raise TypeError("contract must be EvidenceContract")
        self.registry = registry
        self.contract = contract
        self.resolver = resolver or EntityResolver(registry)

    def evaluate(self, context: EvaluationContext) -> EvaluationBatch:
        """Evaluate all rules, preserving inactive rows for an auditable trace."""

        evaluations = tuple(self.evaluate_rule(rule, context) for rule in self.registry.rules)
        return EvaluationBatch(
            context.scenario_date,
            self.contract,
            self._apply_precedence(evaluations),
        )

    def evaluate_rule(
        self, rule: PolicyRule | str, context: EvaluationContext
    ) -> RuleEvaluation:
        if isinstance(rule, str):
            rule = self.registry.rule(rule)
        if not isinstance(rule, PolicyRule):
            raise TypeError("rule must be PolicyRule or rule ID")
        if not isinstance(context, EvaluationContext):
            raise TypeError("context must be EvaluationContext")
        kind = self._rule_kind(rule)
        if not rule.is_active(context.scenario_date):
            return RuleEvaluation(
                rule.rule_id,
                rule.source_document,
                kind,
                False,
                False,
                EvidenceStatus.FAIL,
                None,
            )

        named, named_alerts = self._resolve_named_references(rule, context)
        unresolved_named = tuple(item for item in named.values() if item.status is EvidenceStatus.UNKNOWN)
        if unresolved_named:
            severity = RuleSeverity(rule.severity)
            if severity is RuleSeverity.SHAPING:
                alerts = tuple(
                    EvaluationAlert(
                        AlertCategory.POLICY_CONFLICT,
                        "SHAPING_REFERENCE_UNRESOLVED",
                        f"Dropped shaping directive {rule.rule_id!r}: its source-named supplier cannot be resolved safely",
                        rule.rule_id,
                    )
                    for _item in unresolved_named[:1]
                )
                evidence = self._evidence(
                    rule,
                    EvidenceStatus.UNKNOWN,
                    "Source-named reference unresolved; this shaping directive is inapplicable only within its own scope.",
                    assumptions=("SOURCE_NAMED_ENTITY_UNRESOLVED",),
                    disposition=None,
                )
                return RuleEvaluation(
                    rule.rule_id,
                    rule.source_document,
                    kind,
                    True,
                    False,
                    EvidenceStatus.UNKNOWN,
                    evidence,
                    named_alerts + alerts,
                )
            evidence = self._evidence(
                rule,
                EvidenceStatus.UNKNOWN,
                "A hard rule's source-named reference cannot be resolved safely.",
                assumptions=("SOURCE_NAMED_ENTITY_UNRESOLVED",),
                disposition=PlanDisposition.DECISION_REQUIRED,
            )
            return RuleEvaluation(
                rule.rule_id,
                rule.source_document,
                kind,
                True,
                True,
                EvidenceStatus.UNKNOWN,
                evidence,
                named_alerts,
            )

        selector_status, selector_alerts, selector_assumptions = self._evaluate_selector(rule, context)
        alerts = named_alerts + selector_alerts
        if selector_status is EvidenceStatus.FAIL:
            return RuleEvaluation(
                rule.rule_id,
                rule.source_document,
                kind,
                True,
                False,
                selector_status,
                None,
                alerts,
            )

        if kind == "named_primary_supplier" and self._is_released(rule, context, named):
            evidence = self._evidence(
                rule,
                EvidenceStatus.PASS,
                "An affirmative runtime capacity record releases the named-primary directive.",
            )
            return RuleEvaluation(
                rule.rule_id,
                rule.source_document,
                kind,
                True,
                False,
                selector_status,
                evidence,
                alerts,
                released=True,
            )

        predicate = self._evaluate_predicate(rule, context, named)
        alerts += predicate.alerts
        assumptions = tuple(sorted(set(selector_assumptions + predicate.assumptions)))
        status = predicate.status
        disposition: PlanDisposition | None = None

        if selector_status is EvidenceStatus.UNKNOWN:
            # Non-membership makes the rule inapplicable.  A proposal passes
            # both readings only when it also passes the membership reading.
            if predicate.status is EvidenceStatus.FAIL:
                status = EvidenceStatus.UNKNOWN
                disposition = PlanDisposition.DECISION_REQUIRED
                summary = (
                    "Unresolved selector membership changes executability: the proposal fails if the entity is in scope."
                )
            else:
                status = EvidenceStatus.UNKNOWN
                disposition = self._unknown_disposition(rule)
                summary = (
                    "Selector membership is unresolved; the proposal was evaluated both in and out of scope. "
                    + predicate.summary
                )
            assumptions = tuple(sorted(set(assumptions + ("ROBUST_BOTH_WAYS",))))
        else:
            summary = predicate.summary
            if status is EvidenceStatus.UNKNOWN:
                disposition = self._unknown_disposition(rule)

        evidence = self._evidence(
            rule,
            status,
            summary,
            assumptions=assumptions,
            disposition=disposition,
        )
        if kind == "named_primary_supplier":
            alerts += self._capacity_alerts(rule, context, named)
        return RuleEvaluation(
            rule.rule_id,
            rule.source_document,
            kind,
            True,
            True if selector_status is EvidenceStatus.PASS else None,
            selector_status,
            evidence,
            alerts,
        )

    def _rule_kind(self, rule: PolicyRule) -> str:
        constraint = rule.data.get("constraint")
        if isinstance(constraint, Mapping):
            return str(constraint.get("kind", "unknown_constraint"))
        directive = rule.data.get("directive")
        if isinstance(directive, Mapping):
            return str(directive.get("kind", "unknown_directive"))
        return "unknown_rule"

    def _resolve_named_references(
        self, rule: PolicyRule, context: EvaluationContext
    ) -> tuple[dict[str, NamedEntityResolution], tuple[EvaluationAlert, ...]]:
        references: dict[str, Mapping[str, object]] = {}
        directive = rule.data.get("directive")
        if isinstance(directive, Mapping) and isinstance(directive.get("supplier"), Mapping):
            references["directive.supplier"] = directive["supplier"]
        release = rule.data.get("release_condition")
        if isinstance(release, Mapping) and isinstance(release.get("subject"), Mapping):
            references["release_condition.subject"] = release["subject"]
        constraint = rule.data.get("constraint")
        if isinstance(constraint, Mapping):
            for key in ("supplier", "subject"):
                if isinstance(constraint.get(key), Mapping):
                    references[f"constraint.{key}"] = constraint[key]

        resolved: dict[str, NamedEntityResolution] = {}
        alerts: list[EvaluationAlert] = []
        for path, reference in references.items():
            result = self.resolver.resolve_named_supplier(reference, context.suppliers)
            resolved[path] = result
            alerts.extend(self._convert_resolution_alerts(rule, result.alerts, result.resolved_supplier_id))
        return resolved, tuple(alerts)

    @staticmethod
    def _convert_resolution_alerts(
        rule: PolicyRule,
        alerts: Iterable[ResolutionAlert],
        entity_id: str | None,
    ) -> tuple[EvaluationAlert, ...]:
        return tuple(
            EvaluationAlert(alert.category, alert.code, alert.message, rule.rule_id, entity_id)
            for alert in alerts
        )

    def _evaluate_selector(
        self, rule: PolicyRule, context: EvaluationContext
    ) -> tuple[EvidenceStatus, tuple[EvaluationAlert, ...], tuple[str, ...]]:
        selector = rule.data["selector"]
        statuses: list[EvidenceStatus] = []
        alerts: list[EvaluationAlert] = []
        assumptions: list[str] = []
        for concept_id in selector.get("semantic_tags", ()):
            concept = self.resolver.concept(str(concept_id))
            kind = concept["entity_kind"]
            entity: Component | Supplier | Mapping[str, object] | None
            if kind == "component":
                entity = context.component
            elif kind == "supplier":
                entity = context.supplier
            else:
                entity = context.facts
            if entity is None:
                statuses.append(EvidenceStatus.UNKNOWN)
                assumptions.append("SELECTOR_ENTITY_MISSING")
                continue
            resolution: ConceptResolution = self.resolver.resolve_concept(str(concept_id), entity)
            statuses.append(resolution.status)
            assumptions.extend(resolution.assumption_codes)
            alerts.extend(
                self._convert_resolution_alerts(rule, resolution.alerts, resolution.entity_id)
            )

        for condition in selector.get("route_conditions", ()):
            value = context.facts.get(str(condition))
            statuses.append(
                EvidenceStatus.PASS
                if value is True
                else EvidenceStatus.FAIL
                if value is False
                else EvidenceStatus.UNKNOWN
            )

        if not statuses:
            return EvidenceStatus.PASS, tuple(alerts), tuple(sorted(set(assumptions)))
        operator = str(selector.get("operator", "all"))
        if operator == "all":
            status = self.truth_all(statuses)
        elif operator == "any":
            status = self.truth_any(statuses)
        elif operator == "none":
            status = self.truth_not(self.truth_any(statuses))
        else:
            raise ValueError(f"unsupported selector operator {operator!r}")
        return status, tuple(alerts), tuple(sorted(set(assumptions)))

    @staticmethod
    def truth_not(value: EvidenceStatus) -> EvidenceStatus:
        if value is EvidenceStatus.PASS:
            return EvidenceStatus.FAIL
        if value is EvidenceStatus.FAIL:
            return EvidenceStatus.PASS
        return EvidenceStatus.UNKNOWN

    @staticmethod
    def truth_all(values: Iterable[EvidenceStatus]) -> EvidenceStatus:
        items = tuple(values)
        if any(value is EvidenceStatus.FAIL for value in items):
            return EvidenceStatus.FAIL
        if all(value is EvidenceStatus.PASS for value in items):
            return EvidenceStatus.PASS
        return EvidenceStatus.UNKNOWN

    @staticmethod
    def truth_any(values: Iterable[EvidenceStatus]) -> EvidenceStatus:
        items = tuple(values)
        if any(value is EvidenceStatus.PASS for value in items):
            return EvidenceStatus.PASS
        if all(value is EvidenceStatus.FAIL for value in items):
            return EvidenceStatus.FAIL
        return EvidenceStatus.UNKNOWN

    def _evaluate_predicate(
        self,
        rule: PolicyRule,
        context: EvaluationContext,
        named: Mapping[str, NamedEntityResolution],
    ) -> _PredicateResult:
        kind = self._rule_kind(rule)
        method = getattr(self, f"_predicate_{kind}", None)
        if method is None:
            return _PredicateResult(
                EvidenceStatus.UNKNOWN,
                f"No deterministic evaluator is registered for rule kind {kind!r}.",
                ("RULE_KIND_UNSUPPORTED",),
            )
        return method(rule, context, named)

    @staticmethod
    def _bool_result(value: object, pass_summary: str, fail_summary: str) -> _PredicateResult:
        if value is True:
            return _PredicateResult(EvidenceStatus.PASS, pass_summary)
        if value is False:
            return _PredicateResult(EvidenceStatus.FAIL, fail_summary)
        return _PredicateResult(EvidenceStatus.UNKNOWN, "Required predicate evidence is absent.")

    @staticmethod
    def _decimal(value: object) -> Decimal | None:
        if value is None or isinstance(value, bool) or isinstance(value, float):
            return None
        try:
            parsed = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        return parsed if parsed.is_finite() else None

    @staticmethod
    def _decimal_mapping(value: object) -> dict[str, Decimal] | None:
        if not isinstance(value, Mapping):
            return None
        result: dict[str, Decimal] = {}
        for key, raw in value.items():
            parsed = PolicyEvaluator._decimal(raw)
            if parsed is None or parsed < ZERO:
                return None
            result[str(key)] = parsed
        return result

    def _predicate_approved_supplier_required(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        if context.supplier is None:
            return _PredicateResult(EvidenceStatus.UNKNOWN, "Supplier is absent.")
        if context.supplier.on_approved_list is True:
            return _PredicateResult(EvidenceStatus.PASS, "Supplier is on the Approved Supplier List.")
        return _PredicateResult(
            EvidenceStatus.FAIL,
            "Supplier is not affirmatively present on the Approved Supplier List.",
            ("ASL_NOT_APPROVED",) if context.supplier.on_approved_list is None else (),
        )

    def _predicate_required_certification(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        if context.supplier is None:
            return _PredicateResult(EvidenceStatus.UNKNOWN, "Supplier is absent.")
        required = canonical_certification(str(rule.data["constraint"]["certification"]))
        held = {canonical_certification(item) for item in context.supplier.certifications}
        return _PredicateResult(
            EvidenceStatus.PASS if required in held else EvidenceStatus.FAIL,
            "Required certification is held." if required in held else "Required certification is not held.",
        )

    def _predicate_domestic_supplier_preference(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        if context.supplier is None:
            return _PredicateResult(EvidenceStatus.UNKNOWN, "Supplier is absent.")
        domestic_concept = self._concept_by_country_polarity(international=False)
        domestic = self.resolver.resolve_concept(domestic_concept, context.supplier)
        alerts = self._convert_resolution_alerts(rule, domestic.alerts, context.supplier.supplier_id)
        if domestic.status is EvidenceStatus.PASS:
            return _PredicateResult(EvidenceStatus.PASS, "The proposed supplier is domestic.", alerts=alerts)
        if domestic.status is EvidenceStatus.UNKNOWN:
            return _PredicateResult(EvidenceStatus.UNKNOWN, "Supplier domesticity is unresolved.", domestic.assumption_codes, alerts)
        constraint = rule.data["constraint"]
        if context.facts.get("no_domestic_timeline") is True and constraint.get("allow_when_no_domestic_timeline") is True:
            return _PredicateResult(EvidenceStatus.PASS, "International sourcing is permitted because no domestic route meets the timeline.", alerts=alerts)
        if context.facts.get("no_domestic_source") is True and constraint.get("allow_when_no_domestic_source") is True:
            return _PredicateResult(EvidenceStatus.PASS, "International sourcing is permitted because no domestic source exists.", alerts=alerts)
        premium = self._decimal(context.facts.get("domestic_premium_fraction"))
        threshold = self._decimal(constraint.get("maximum_premium_fraction"))
        if premium is not None and threshold is not None:
            return _PredicateResult(
                EvidenceStatus.PASS if premium > threshold else EvidenceStatus.FAIL,
                "Domestic premium strictly exceeds the policy threshold." if premium > threshold else "Domestic premium does not strictly exceed the policy threshold.",
                alerts=alerts,
            )
        return _PredicateResult(EvidenceStatus.UNKNOWN, "International-sourcing gate evidence is incomplete.", alerts=alerts)

    def _predicate_international_sourcing_justification(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        return self._bool_result(
            context.facts.get("international_justification_provided"),
            "International-sourcing justification is documented.",
            "International-sourcing justification is missing.",
        )

    def _predicate_supplier_volume_cap(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        history = self._decimal_mapping(context.facts.get("rolling_supplier_volumes"))
        proposed = self._decimal_mapping(context.facts.get("proposed_supplier_volumes"))
        if history is None:
            return _PredicateResult(
                EvidenceStatus.UNKNOWN,
                "Rolling-window history is absent; it is unknown and was not replaced with zero.",
                ("ROLLING_HISTORY_UNKNOWN",),
            )
        if proposed is None:
            return _PredicateResult(EvidenceStatus.UNKNOWN, "Proposed supplier volumes are absent.")
        combined = dict(history)
        for supplier_id, quantity in proposed.items():
            combined[supplier_id] = combined.get(supplier_id, ZERO) + quantity
        total = sum(combined.values(), ZERO)
        if total == ZERO:
            return _PredicateResult(EvidenceStatus.PASS, "Known rolling and proposed volume is zero.")
        maximum = self._decimal(rule.data["constraint"].get("maximum_fraction"))
        assert maximum is not None
        violators = tuple(sorted(key for key, value in combined.items() if value / total > maximum))
        return _PredicateResult(
            EvidenceStatus.FAIL if violators else EvidenceStatus.PASS,
            f"Supplier volume cap exceeded by {', '.join(violators)}." if violators else "Every supplier share is within the rolling-window cap.",
        )

    def _predicate_minimum_qualified_suppliers(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        count = context.facts.get("qualified_supplier_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return _PredicateResult(EvidenceStatus.UNKNOWN, "Qualified-supplier count is absent.")
        minimum = int(rule.data["constraint"]["minimum_count"])
        return _PredicateResult(
            EvidenceStatus.PASS if count >= minimum else EvidenceStatus.FAIL,
            "Qualified-supplier count meets the diagnostic minimum." if count >= minimum else "Qualified-supplier count is below the diagnostic minimum.",
        )

    def _predicate_sole_source_justification(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        count = context.facts.get("global_supplier_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return _PredicateResult(EvidenceStatus.UNKNOWN, "Global supplier count is absent.")
        if count != 1:
            return _PredicateResult(EvidenceStatus.PASS, "The proposal is not globally sole-source.")
        return self._bool_result(
            context.facts.get("sole_source_justification_provided"),
            "Sole Source Justification is documented.",
            "The globally sole-source proposal lacks its required justification.",
        )

    def _predicate_catalog_minimum_order_quantity(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        quantity = self._decimal(context.facts.get("quantity"))
        if quantity is None or context.catalog_line is None:
            return _PredicateResult(EvidenceStatus.UNKNOWN, "Order quantity or catalog MOQ is absent.")
        return _PredicateResult(
            EvidenceStatus.PASS if quantity >= context.catalog_line.minimum_order_quantity else EvidenceStatus.FAIL,
            "Order quantity meets the catalog MOQ." if quantity >= context.catalog_line.minimum_order_quantity else "Order quantity is below the catalog MOQ.",
        )

    def _predicate_sub_moq_written_approval(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        quantity = self._decimal(context.facts.get("quantity"))
        if quantity is None or context.catalog_line is None:
            return _PredicateResult(EvidenceStatus.UNKNOWN, "Order quantity or catalog MOQ is absent.")
        if quantity >= context.catalog_line.minimum_order_quantity:
            return _PredicateResult(EvidenceStatus.PASS, "Order is not below MOQ, so written supplier approval is not needed.")
        return self._bool_result(
            context.facts.get("sub_moq_written_approval"),
            "Written supplier approval for the sub-MOQ order exists.",
            "Written supplier approval for the sub-MOQ order is absent.",
        )

    def _predicate_hazardous_receiving_and_storage(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        return self._bool_result(
            context.facts.get("hazardous_handling_planned"),
            "Special receiving, storage, and FIFO handling are represented.",
            "Required hazardous-material handling is not represented.",
        )

    def _predicate_hazardous_procurement_review(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        return self._bool_result(
            context.facts.get("hazardous_review_approved"),
            "Hazardous-material procurement review is approved.",
            "Hazardous-material procurement review is not approved.",
        )

    def _predicate_critical_component_categories(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        if context.component is None:
            return _PredicateResult(EvidenceStatus.UNKNOWN, "Component is absent.")
        aggregate = self._aggregate_concept_for_rule(rule)
        if aggregate is None:
            return _PredicateResult(EvidenceStatus.UNKNOWN, "Critical aggregate concept is unavailable.")
        resolution = self.resolver.resolve_concept(aggregate, context.component)
        alerts = self._convert_resolution_alerts(rule, resolution.alerts, context.component.component_id)
        return _PredicateResult(
            resolution.status,
            "Component is in an enumerated critical category." if resolution.status is EvidenceStatus.PASS else "Critical-category membership is inferred and remains unresolved." if resolution.status is EvidenceStatus.UNKNOWN else "Component is outside the closed critical-category enumeration.",
            resolution.assumption_codes,
            alerts,
        )

    def _predicate_total_cost_of_ownership(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        return self._bool_result(
            context.facts.get("all_tco_costs_represented"),
            "All available total-cost-of-ownership fields are represented.",
            "Known shipping or handling costs were omitted.",
        )

    def _predicate_order_value_approval(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        total = self._decimal(context.facts.get("order_total"))
        threshold = self._decimal(rule.data["constraint"].get("amount_exceeds"))
        if total is None or threshold is None:
            return _PredicateResult(EvidenceStatus.UNKNOWN, "Order value is absent.")
        if total <= threshold:
            return _PredicateResult(EvidenceStatus.PASS, "Order value does not exceed this approval threshold.")
        authority = str(rule.data["constraint"]["authority"])
        approvals = tuple(str(item) for item in context.facts.get("approved_authorities", ()))
        if authority in approvals:
            return _PredicateResult(EvidenceStatus.PASS, f"Required approval from {authority} is present.")
        return _PredicateResult(EvidenceStatus.UNKNOWN, f"Required approval from {authority} is absent.", ("APPROVAL_REQUIRED",))

    def _predicate_emergency_approval_bypass(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        if context.facts.get("emergency_bypass_requested") is not True:
            return _PredicateResult(EvidenceStatus.PASS, "No emergency approval bypass is claimed.")
        return _PredicateResult(EvidenceStatus.UNKNOWN, "The emergency qualifying predicate is unsupported; the bypass cannot be established.", ("EMERGENCY_BYPASS_UNSUPPORTED",))

    def _predicate_sustainability_preference(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        return self._bool_result(
            context.facts.get("sustainability_preference_honored"),
            "Sustainability preference is honored among comparable routes.",
            "Sustainability preference is not honored among comparable routes.",
        )

    def _predicate_below_rating_review(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        if context.supplier is None or context.supplier.sustainability_rating is None:
            return _PredicateResult(EvidenceStatus.UNKNOWN, "Supplier sustainability rating is absent.", ("SUSTAINABILITY_RATING_UNKNOWN",))
        rating = self._rating(context.supplier.sustainability_rating)
        boundary = self._rating(str(rule.data["constraint"]["below_rating"]))
        if rating is None or boundary is None:
            return _PredicateResult(EvidenceStatus.UNKNOWN, "Supplier sustainability rating cannot be parsed.", ("SUSTAINABILITY_RATING_UNKNOWN",))
        if rating >= boundary:
            return _PredicateResult(EvidenceStatus.PASS, "Supplier is not below the review threshold.")
        if context.facts.get("no_better_rated_alternative") is not True:
            return _PredicateResult(EvidenceStatus.FAIL, "A below-threshold supplier is not proven to be the only available alternative.")
        return self._bool_result(
            context.facts.get("below_rating_review_approved"),
            "Additional review for the below-threshold supplier is represented.",
            "Additional review for the below-threshold supplier is absent.",
        )

    def _predicate_certification_preference(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        return self._bool_result(
            context.facts.get("certification_preference_honored"),
            "Certification preference is honored.",
            "Certification preference is not honored.",
        )

    def _predicate_strategic_supplier_continuity(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        if context.facts.get("strategic_volume_maintained") is True:
            return _PredicateResult(EvidenceStatus.PASS, "Strategic supplier volume is maintained.")
        savings = self._decimal(context.facts.get("alternative_savings_fraction"))
        threshold = self._decimal(rule.data["constraint"].get("maximum_alternative_savings_fraction"))
        if savings is None or threshold is None:
            return _PredicateResult(EvidenceStatus.UNKNOWN, "Strategic continuity comparison is incomplete.")
        return _PredicateResult(
            EvidenceStatus.PASS if savings > threshold else EvidenceStatus.FAIL,
            "Alternative savings exceed the continuity threshold." if savings > threshold else "Strategic volume was shifted without savings above the threshold.",
        )

    def _predicate_strategic_volume_shift_approval(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        if context.facts.get("significant_strategic_shift") is not True:
            return _PredicateResult(EvidenceStatus.PASS, "No significant strategic-supplier shift is proposed.")
        authority = str(rule.data["constraint"]["authority"])
        approvals = tuple(str(item) for item in context.facts.get("approved_authorities", ()))
        if authority in approvals:
            return _PredicateResult(EvidenceStatus.PASS, f"Required approval from {authority} is present.")
        return _PredicateResult(EvidenceStatus.UNKNOWN, f"Required approval from {authority} is absent.", ("APPROVAL_REQUIRED",))

    def _predicate_on_time_arrival(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        available = context.facts.get("material_available_date")
        required = context.facts.get("required_date")
        if not isinstance(available, date) or not isinstance(required, date):
            return _PredicateResult(EvidenceStatus.UNKNOWN, "Material-available or required date is absent.")
        return _PredicateResult(
            EvidenceStatus.PASS if available <= required else EvidenceStatus.FAIL,
            "Material is available on time." if available <= required else "Material is not available by the required date.",
        )

    def _predicate_quoted_lead_time_delivery_date(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        order_date = context.facts.get("order_date")
        expected = context.facts.get("expected_delivery_date")
        if not isinstance(order_date, date) or not isinstance(expected, date) or context.catalog_line is None:
            return _PredicateResult(EvidenceStatus.UNKNOWN, "Order date, expected delivery, or quoted lead time is absent.")
        calculated = order_date + timedelta(days=context.catalog_line.lead_time_days)
        return _PredicateResult(
            EvidenceStatus.PASS if expected == calculated else EvidenceStatus.FAIL,
            "Expected delivery equals order date plus quoted supplier lead time." if expected == calculated else "Expected delivery does not equal the quoted-lead-time calculation.",
        )

    def _predicate_minimum_secondary_fraction(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        allocations = self._decimal_mapping(context.facts.get("allocations"))
        eligible_count = context.facts.get("eligible_supplier_count")
        if allocations is None:
            return _PredicateResult(EvidenceStatus.UNKNOWN, "Prospective allocation group is absent.")
        if isinstance(eligible_count, int) and not isinstance(eligible_count, bool) and eligible_count < 2:
            return _PredicateResult(EvidenceStatus.FAIL, "The prospective secondary-allocation rule is structurally unsatisfiable with fewer than two eligible suppliers.")
        positive = tuple(value for value in allocations.values() if value > ZERO)
        total = sum(positive, ZERO)
        if total == ZERO:
            return _PredicateResult(EvidenceStatus.UNKNOWN, "Prospective allocation group has no positive volume.")
        secondary = total - max(positive)
        minimum = self._decimal(rule.data["constraint"].get("value"))
        assert minimum is not None
        return _PredicateResult(
            EvidenceStatus.PASS if secondary / total >= minimum else EvidenceStatus.FAIL,
            "Prospective secondary share meets the minimum." if secondary / total >= minimum else "Prospective secondary share is below the minimum.",
        )

    def _predicate_named_primary_supplier(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        primary = named.get("directive.supplier")
        allocations = self._decimal_mapping(context.facts.get("allocations"))
        if primary is None or primary.supplier is None or allocations is None:
            return _PredicateResult(EvidenceStatus.UNKNOWN, "Named-primary allocation evidence is incomplete.")
        positive = {key: value for key, value in allocations.items() if value > ZERO}
        if not positive:
            return _PredicateResult(EvidenceStatus.UNKNOWN, "Allocation group has no positive volume.")
        maximum = max(positive.values())
        named_quantity = positive.get(primary.supplier.supplier_id, ZERO)
        return _PredicateResult(
            EvidenceStatus.PASS if named_quantity == maximum else EvidenceStatus.FAIL,
            "The source-named supplier has the largest allocation share." if named_quantity == maximum else "The source-named supplier is not primary in the allocation group.",
        )

    def _predicate_air_freight_authorization(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        if context.facts.get("air_freight_used") is not True:
            return _PredicateResult(EvidenceStatus.PASS, "Air freight is not used.")
        required = (
            context.facts.get("confirmed_production") is True,
            context.facts.get("standard_lead_time_causes_production_delay") is True,
        )
        if all(required):
            return _PredicateResult(EvidenceStatus.PASS, "Air freight satisfies the compiled authorization predicates.")
        if any(value is False for value in required):
            return _PredicateResult(EvidenceStatus.FAIL, "Air freight does not satisfy every authorization predicate.")
        return _PredicateResult(EvidenceStatus.UNKNOWN, "Air-freight authorization evidence is incomplete.")

    def _predicate_air_freight_cost_documentation(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        return self._bool_result(
            context.facts.get("air_freight_cost_documented"),
            "Air-freight cost is documented.",
            "Air-freight cost documentation is missing.",
        )

    def _predicate_air_freight_period_spend_cap(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        spend = self._decimal(context.facts.get("air_freight_period_spend"))
        cap = self._decimal(rule.data["constraint"].get("maximum_amount"))
        if spend is None or cap is None:
            return _PredicateResult(EvidenceStatus.UNKNOWN, "Authorization-period air-freight spend is absent.")
        return _PredicateResult(
            EvidenceStatus.PASS if spend <= cap else EvidenceStatus.FAIL,
            "Authorization-period air-freight spend is within the cap." if spend <= cap else "Authorization-period air-freight spend exceeds the cap.",
        )

    def _predicate_air_freight_individual_approval(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        return self._bool_result(
            context.facts.get("air_freight_request_approved"),
            "The individual air-freight request is approved.",
            "The individual air-freight request is unapproved.",
        )

    def _predicate_shipment_certificate_of_conformance(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        return self._bool_result(
            context.facts.get("conformance_certificate_planned"),
            "Certificate of Conformance requirements are represented.",
            "Certificate of Conformance requirements are absent.",
        )

    def _predicate_incumbent_supplier_only(self, rule: PolicyRule, context: EvaluationContext, named: Mapping[str, NamedEntityResolution]) -> _PredicateResult:
        if context.supplier is None or context.component is None:
            return _PredicateResult(EvidenceStatus.UNKNOWN, "Supplier or component is absent.")
        contract_data = self.registry.pack["contracts"][self.contract.value]["rule_resolutions"].get(rule.rule_id)
        strategy = contract_data.get("resolution_strategy") if isinstance(contract_data, Mapping) else None
        accepted = tuple(str(item) for item in context.facts.get("accepted_shipment_supplier_ids", ()))
        if context.supplier.supplier_id in accepted:
            return _PredicateResult(EvidenceStatus.PASS, "An accepted-shipment record establishes incumbency.")
        if strategy == "affirmative_record_required":
            return _PredicateResult(EvidenceStatus.UNKNOWN, "No accepted-shipment record establishes incumbency.", ("PCB_INBOUND_HISTORY_UNKNOWN",))

        prior_order = any(
            order.component_id == context.component.component_id
            and order.supplier_id == context.supplier.supplier_id
            for order in context.purchase_orders
        )
        relationship_predates = context.facts.get("relationship_predates_rule") is True
        catalog_valid = context.catalog_line is not None and context.supplier.on_approved_list is True
        if prior_order or (relationship_predates and catalog_valid):
            return _PredicateResult(EvidenceStatus.UNKNOWN, "Documented benchmark inference supports incumbency, but receipt acceptance is not proven.", ("PCB_INCUMBENCY_INFERRED",))
        return _PredicateResult(EvidenceStatus.FAIL, "The supplier does not satisfy the benchmark incumbency inference.")

    @staticmethod
    def _rating(value: str) -> Decimal | None:
        text = value.strip().upper()
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

    def _concept_by_country_polarity(self, *, international: bool) -> str:
        matches = []
        for raw in self.registry.concepts["concepts"]:
            if "country" not in raw.get("structured_signals", ()):
                continue
            synonyms = {tuple(str(token) for token in self._tokenize(str(item))) for item in raw.get("synonyms", ())}
            is_international = ("international",) in synonyms or ("non", "domestic") in synonyms
            if is_international is international:
                matches.append(str(raw["concept_id"]))
        if len(matches) != 1:
            raise ValueError("policy pack must define one domestic and one international country concept")
        return matches[0]

    @staticmethod
    def _tokenize(value: str) -> tuple[str, ...]:
        from .entity_resolution import normalized_tokens

        return normalized_tokens(value)

    def _aggregate_concept_for_rule(self, rule: PolicyRule) -> str | None:
        categories = tuple(str(item) for item in rule.data["constraint"].get("categories", ()))
        for concept_id, members in self.resolver._aggregate_members.items():
            if tuple(members) == categories:
                return concept_id
        return None

    def _is_released(
        self,
        rule: PolicyRule,
        context: EvaluationContext,
        named: Mapping[str, NamedEntityResolution],
    ) -> bool:
        release = rule.data.get("release_condition")
        subject = named.get("release_condition.subject")
        if not isinstance(release, Mapping) or subject is None or subject.supplier is None:
            return False
        if release.get("resolution") != "affirmative_record_required":
            return False
        return any(
            record.affirmative
            and record.supplier_id == subject.supplier.supplier_id
            and record.predicate == release.get("predicate")
            and record.evidence_source == release.get("evidence_source")
            for record in context.confirmations
        )

    def _capacity_alerts(
        self,
        rule: PolicyRule,
        context: EvaluationContext,
        named: Mapping[str, NamedEntityResolution],
    ) -> tuple[EvaluationAlert, ...]:
        risk = rule.data.get("risk_disclosure")
        subject = named.get("release_condition.subject")
        allocations = self._decimal_mapping(context.facts.get("allocations"))
        if not isinstance(risk, Mapping) or subject is None or subject.supplier is None or allocations is None:
            return ()
        numeric = frozenset(str(item) for item in context.facts.get("numeric_capacity_supplier_ids", ()))
        quantity = allocations.get(subject.supplier.supplier_id, ZERO)
        if quantity <= ZERO or subject.supplier.supplier_id in numeric:
            return ()
        return (
            EvaluationAlert(
                AlertCategory.CAPACITY_UNKNOWN,
                str(risk.get("kind")),
                "Positive allocation touches the release-condition subject without numeric throughput evidence; this disclosure does not change disposition.",
                rule.rule_id,
                subject.supplier.supplier_id,
            ),
        )

    def _unknown_disposition(self, rule: PolicyRule) -> PlanDisposition:
        contract = self.registry.pack["contracts"][self.contract.value]
        rule_resolution = contract.get("rule_resolutions", {}).get(rule.rule_id)
        if isinstance(rule_resolution, Mapping) and "missing_disposition" in rule_resolution:
            return PlanDisposition(str(rule_resolution["missing_disposition"]))
        basis = rule.evidence_basis
        mapped = contract.get(basis)
        if isinstance(mapped, Mapping) and "disposition" in mapped:
            return PlanDisposition(str(mapped["disposition"]))
        if self.contract is EvidenceContract.BENCHMARK:
            return PlanDisposition.EXECUTE_WITH_ASSUMPTION
        return PlanDisposition.DECISION_REQUIRED

    def _evidence(
        self,
        rule: PolicyRule,
        status: EvidenceStatus,
        summary: str,
        *,
        assumptions: tuple[str, ...] = (),
        disposition: PlanDisposition | None = None,
    ) -> EvidenceResult:
        return EvidenceResult(
            rule_id=rule.rule_id,
            status=status,
            basis=EvidenceBasis(rule.evidence_basis),
            scope=EvidenceScope.RULE if rule.evidence_basis == "rolling_window" else EvidenceScope.CANDIDATE,
            severity=RuleSeverity(rule.severity),
            summary=summary,
            source_references=(rule.source_document,),
            assumption_codes=assumptions,
            contract_disposition=disposition,
        )

    def _apply_precedence(
        self, evaluations: tuple[RuleEvaluation, ...]
    ) -> tuple[RuleEvaluation, ...]:
        by_id = {item.rule_id: item for item in evaluations}
        replacements: dict[str, str] = {}
        conflicts: set[str] = set()
        for rule in self.registry.rules:
            current = by_id[rule.rule_id]
            # An UNKNOWN selector represents two branches.  It cannot erase a
            # broader rule globally; supersession applies only in its proven
            # in-scope branch.
            if not current.active or current.applicable is not True:
                continue
            precedence = rule.data.get("precedence")
            if not isinstance(precedence, Mapping):
                continue
            for relation in ("supersedes", "outranks"):
                for target in precedence.get(relation, ()):
                    target_eval = by_id.get(str(target))
                    if target_eval is not None and target_eval.active and target_eval.applicable is True:
                        replacements[target_eval.rule_id] = rule.rule_id

        # Narrower selectors and then later effective dates decide otherwise
        # competing instances of the same single-valued constraint kind.
        cumulative_kinds = {
            "required_certification",
            "order_value_approval",
            "documentation_requirement",
        }
        groups: dict[str, list[PolicyRule]] = {}
        for rule in self.registry.rules:
            item = by_id[rule.rule_id]
            if not item.active or item.applicable is not True or item.constraint_kind in cumulative_kinds:
                continue
            groups.setdefault(item.constraint_kind, []).append(rule)
        for rules in groups.values():
            if len(rules) < 2:
                continue
            candidates = [rule for rule in rules if rule.rule_id not in replacements]
            if len(candidates) < 2:
                continue
            specificities = {rule.rule_id: self._specificity(rule) for rule in candidates}
            highest = max(specificities.values())
            narrower = [rule for rule in candidates if specificities[rule.rule_id] == highest]
            for rule in candidates:
                if specificities[rule.rule_id] < highest:
                    replacements[rule.rule_id] = min(item.rule_id for item in narrower)
            if len(narrower) > 1:
                latest = max(rule.effective_from for rule in narrower)
                latest_rules = [rule for rule in narrower if rule.effective_from == latest]
                if len(latest_rules) == 1:
                    winner = latest_rules[0].rule_id
                    for rule in narrower:
                        if rule.rule_id != winner:
                            replacements[rule.rule_id] = winner
                else:
                    serialized = {
                        repr((rule.data.get("constraint"), rule.data.get("directive")))
                        for rule in latest_rules
                    }
                    if len(serialized) > 1:
                        conflicts.update(rule.rule_id for rule in latest_rules)

        result = []
        for item in evaluations:
            if item.rule_id in conflicts and item.evidence is not None:
                evidence = replace(
                    item.evidence,
                    status=EvidenceStatus.UNKNOWN,
                    summary="Active rules of equal specificity, authority, and effective date conflict.",
                    assumption_codes=tuple(
                        sorted(set(item.evidence.assumption_codes + ("POLICY_CONFLICT",)))
                    ),
                    contract_disposition=PlanDisposition.DECISION_REQUIRED,
                )
                alert = EvaluationAlert(
                    AlertCategory.POLICY_CONFLICT,
                    "UNRESOLVED_RULE_CONFLICT",
                    "Equal-precedence hard rules conflict; the affected scope is blocked.",
                    item.rule_id,
                )
                item = replace(item, evidence=evidence, alerts=item.alerts + (alert,))
            result.append(replace(item, superseded_by=replacements.get(item.rule_id)))
        return tuple(result)

    @staticmethod
    def _specificity(rule: PolicyRule) -> tuple[int, int]:
        selector = rule.data["selector"]
        return (
            len(tuple(selector.get("semantic_tags", ()))) + len(tuple(selector.get("route_conditions", ()))),
            0 if selector.get("match") == "all" else 1,
        )


__all__ = [
    "CapacityConfirmation",
    "EvaluationAlert",
    "EvaluationBatch",
    "EvaluationContext",
    "PolicyEvaluator",
    "RuleEvaluation",
]
