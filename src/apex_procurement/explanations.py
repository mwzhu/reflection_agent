"""Deterministic, fact-only purchase-order and alert explanations.

The SQLite output schema has no structured explanation columns.  This module
therefore renders prose exclusively from the frozen domain records and the
reviewed policy pack.  It deliberately ignores the free-form ``summary`` and
``rationale`` fields supplied by upstream stages.
"""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from hashlib import sha256
import re
from typing import Iterable, Mapping, Sequence

from .config import EvidenceContract
from .domain import (
    AlertCategory,
    CandidatePlan,
    DecisionRecord,
    EvidenceResult,
    EvidenceStatus,
    InternalFailureExclusion,
    PlanDisposition,
    PlanLine,
    ResolutionStatus,
    RouteInputIssue,
    RouteQuarantineScope,
    RuleSeverity,
    SourceEntityNormalizationDisclosure,
    UnitNormalizationDisclosure,
    ZERO,
)
from .policy.registry import PolicyRegistry, load_policy_registry
from .policy.rendering import (
    ContractDisposition,
    TerminalRenderingPath,
    terminal_rendering_path,
)
from .serialization import canonical_dumps, sanitize_control_characters


ALERT_MARKER_VERSION = 2
AUDIT_ONLY_ALERT_CATEGORIES = frozenset(
    {AlertCategory.ASSUMPTION, AlertCategory.RUN_ACCOUNTING}
)
ERROR_ALERT_CATEGORIES = frozenset(
    {
        AlertCategory.UNMET_DEMAND,
        AlertCategory.LATE_ARRIVAL,
        AlertCategory.NO_ELIGIBLE_SUPPLIER,
        AlertCategory.POLICY_CONFLICT,
        AlertCategory.DECISION_REQUIRED,
        AlertCategory.PRE_EXISTING_VIOLATION,
        AlertCategory.SOLVER_UNPROVEN,
        AlertCategory.INTERNAL_FAILURE,
    }
)
_SUPPORTED_ALERT_MARKER_VERSIONS = frozenset({1, ALERT_MARKER_VERSION})
_HEX_64 = r"[0-9a-f]{64}"
_ALERT_MARKER = re.compile(
    rf" \[APEX_ALERT:v(?P<version>[0-9]+) key=(?P<key>{_HEX_64}) "
    r"category=(?P<category>[A-Z_]+) scope=(?P<scope>[A-Za-z0-9_-]+)\]\Z"
)


class ExplanationError(ValueError):
    """Structured facts are insufficient or inconsistent for safe prose."""


@dataclass(frozen=True, slots=True)
class RenderedAlert:
    """One concise target alert plus its separate detailed audit prose."""

    key: str
    category: AlertCategory
    scope: str
    description: str
    audit_description: str

    def __post_init__(self) -> None:
        if not re.fullmatch(_HEX_64, self.key):
            raise ValueError("alert key must be a lowercase SHA-256 digest")
        if not isinstance(self.category, AlertCategory):
            raise TypeError("category must be AlertCategory")
        _require_safe_text(self.scope, "scope")
        _require_safe_text(self.description, "description")
        _require_safe_text(self.audit_description, "audit_description")
        parsed = parse_owned_alert(self.description)
        if (
            parsed is None
            or parsed.key != self.key
            or parsed.category is not self.category
            or parsed.scope != self.scope
        ):
            raise ValueError("description does not contain its validated ownership marker")


@dataclass(frozen=True, slots=True)
class ParsedAlertMarker:
    key: str
    category: AlertCategory
    scope: str
    body: str


@dataclass(frozen=True, slots=True)
class ApprovalRule:
    """The structured approval fields needed for a complete proposal."""

    rule_id: str
    authority: str
    threshold: str
    required_action: str

    def __post_init__(self) -> None:
        _require_safe_text(self.rule_id, "rule_id")
        _require_safe_text(self.authority, "authority")
        _require_safe_text(self.threshold, "threshold")
        _require_safe_text(self.required_action, "required_action")


_GENERIC_ACTIONS: Mapping[AlertCategory, tuple[str, str]] = {
    AlertCategory.LATE_ARRIVAL: (
        "selected or committed supply does not cover every requirement by its due date",
        "review the affected production date and decide whether to expedite or reschedule",
    ),
    AlertCategory.NO_ELIGIBLE_SUPPLIER: (
        "no supplier route is proven eligible for the open requirement",
        "qualify a supplier or correct the cited eligibility evidence before rerunning",
    ),
    AlertCategory.POLICY_CONFLICT: (
        "policy evidence does not establish one safe autonomous outcome",
        "resolve the cited policy conflict and rerun against a fresh snapshot",
    ),
    AlertCategory.DOCUMENTATION_REQUIRED: (
        "the selected or proposed route requires supporting documentation",
        "obtain and retain the documentation identified by the cited rule IDs",
    ),
    AlertCategory.SOLE_SOURCE: (
        "the requirement has a sole-source sourcing condition",
        "review the sourcing risk and complete the required sole-source justification",
    ),
    AlertCategory.PRE_EXISTING_VIOLATION: (
        "a pre-existing commitment requires review and was not modified",
        "review the existing purchase order and take any commercial action manually",
    ),
    AlertCategory.DATA_QUALITY: (
        "one or more source facts are missing, ambiguous, or inconsistent",
        "correct the source data and rerun before relying on the affected conclusion",
    ),
    AlertCategory.EVIDENCE_CONTRACT: (
        "the result depends on the active evidence contract",
        "supply the missing authoritative evidence or review the stated contract assumption",
    ),
    AlertCategory.COST_OPPORTUNITY: (
        "a lower-cost alternative exists but was not selected under the active rules",
        "review the cited comparison before requesting a policy-compliant change",
    ),
    AlertCategory.CAPACITY_UNKNOWN: (
        "numeric supplier capacity is not represented for a positively allocated route",
        "confirm capacity with the supplier before operational release",
    ),
    AlertCategory.RECOVERY_SURPLUS: (
        "the selected recovery action creates duplicate or discretionary surplus",
        "review the quantified recovery tradeoff and downstream inventory plan",
    ),
    AlertCategory.FORCED_SURPLUS: (
        "the executable plan buys more than the net requirement because of hard quantity rules",
        "review storage and consumption plans for the quantified forced surplus",
    ),
}

# This rendering assertion is intentionally independent of the validator's
# SILENT_INITIAL_GAP and SILENT_RESIDUAL_GAP category backstops.
_TERMINAL_RENDERING_CATEGORIES = frozenset(
    {
        AlertCategory.NO_ELIGIBLE_SUPPLIER,
        AlertCategory.POLICY_CONFLICT,
        AlertCategory.DOCUMENTATION_REQUIRED,
        AlertCategory.SOLE_SOURCE,
        AlertCategory.PRE_EXISTING_VIOLATION,
        AlertCategory.DATA_QUALITY,
    }
)


def _require_safe_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{name} must not contain control characters")


def _number(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise TypeError("rendered numeric facts must be finite Decimal values")
    return format(value, "f")


def _money(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise TypeError("rendered monetary facts must be finite Decimal values")
    return f"${value:,.2f}"


def _economic_autonomy_disclosure(decision: DecisionRecord) -> str:
    parameters = decision.economic_autonomy
    if parameters is None:
        return ""
    return f" Active policy parameter disclosure: {parameters.disclosure()}."


def _encode_scope(scope: str) -> str:
    return urlsafe_b64encode(scope.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_scope(token: str) -> str | None:
    try:
        raw = urlsafe_b64decode((token + "=" * (-len(token) % 4)).encode("ascii"))
        scope = raw.decode("utf-8")
    except (UnicodeError, ValueError):
        return None
    if not scope or any(ord(character) < 32 for character in scope):
        return None
    return scope if _encode_scope(scope) == token else None


def _list(values: Iterable[str], *, empty: str = "none") -> str:
    items = tuple(sorted(set(values)))
    return ", ".join(items) if items else empty


def _evidence_rule_ids(decision: DecisionRecord, plan: CandidatePlan | None = None) -> tuple[str, ...]:
    evidence: list[EvidenceResult] = list(decision.evidence)
    if plan is not None:
        evidence.extend(plan.evidence)
    return tuple(sorted({item.rule_id for item in evidence}))


def _contract_blocking_evidence(
    decision: DecisionRecord,
    plan: CandidatePlan | None = None,
) -> tuple[EvidenceResult, ...]:
    evidence = list(decision.evidence)
    if plan is not None:
        evidence.extend(plan.evidence)
    unique = {
        (
            item.rule_id,
            item.status,
            item.contract_disposition,
            item.summary,
        ): item
        for item in evidence
        if item.severity is RuleSeverity.HARD
        and item.status is EvidenceStatus.UNKNOWN
        and item.contract_disposition is PlanDisposition.DECISION_REQUIRED
    }
    return tuple(
        sorted(unique.values(), key=lambda item: (item.rule_id, item.summary))
    )


def _comparator_facts(decision: DecisionRecord) -> str:
    quantity = tuple(
        item for item in decision.comparator_facts if item.kind == "quantity_calibration"
    )
    route = tuple(
        item for item in decision.comparator_facts if item.kind == "route_selection"
    )
    rendered = [
        (
            f"quantity calibration stage 0: {item.outcome}; selected routes "
            f"[{_list(item.selected_route_ids)}]; rule IDs [{_list(item.rule_ids)}]; "
            f"quantity delta {_number(item.quantity_delta) if item.quantity_delta is not None else 'n/a'}; "
            f"cost delta {_number(item.cost_delta) if item.cost_delta is not None else 'n/a'}; "
            f"window [{item.policy_window or 'none'}]"
        )
        for item in quantity
    ]
    pairs = sorted(
        {
            (item.selected_route_ids, item.compared_route_ids)
            for item in route
        }
    )
    for selected_ids, compared_ids in pairs:
        path = tuple(
            sorted(
                (
                    item
                    for item in route
                    if item.selected_route_ids == selected_ids
                    and item.compared_route_ids == compared_ids
                ),
                key=lambda item: item.stage,
            )
        )
        decisive = tuple(item for item in path if item.decisive)
        if len(decisive) != 1:
            raise ExplanationError(
                "each structured route comparison requires exactly one deciding stage"
            )
        winner = decisive[0]
        rendered.append(
            f"route selection selected [{_list(selected_ids)}] over "
            f"[{_list(compared_ids)}]; comparison path ["
            + ", ".join(
                f"{item.stage}:{item.comparator}={item.outcome}" for item in path
            )
            + f"]; deciding stage {winner.stage} {winner.comparator}; rule IDs "
            f"[{_list(winner.rule_ids)}]; cost delta "
            f"{_number(winner.cost_delta) if winner.cost_delta is not None else 'n/a'}; "
            f"delivery delta days "
            f"{winner.delivery_delta_days if winner.delivery_delta_days is not None else 'n/a'}; "
            f"window [{winner.policy_window or 'none'}]"
        )
    return "; ".join(rendered) or "none"


def _material_rejections(decision: DecisionRecord) -> str:
    return "; ".join(
        (
            f"supplier {item.supplier_id} route {item.route_id}: reason "
            f"{item.reason_code}; eligibility {item.eligibility.value}; rule IDs "
            f"[{_list(item.rule_ids)}]; unit price {_number(item.unit_price)} versus "
            f"selected {_number(item.selected_unit_price)} (delta "
            f"{_number(item.price_delta)}); material available "
            f"{item.material_available_date.isoformat()} versus selected "
            f"{item.selected_material_available_date.isoformat()} (delta "
            f"{item.delivery_delta_days} days)"
        )
        for item in decision.material_rejections
    ) or "none"


def render_decision_rationale(decision: DecisionRecord) -> str:
    """Render a canonical requirement rationale without upstream free text."""

    if not isinstance(decision, DecisionRecord):
        raise TypeError("decision must be DecisionRecord")
    demand = "; ".join(
        f"{item.order_id} due {bucket.due_date.isoformat()} quantity {_number(item.quantity)}"
        for bucket in decision.demand_buckets
        for item in bucket.contributions
    )
    inbound = "; ".join(
        f"{item.po_number} quantity {_number(item.quantity)} due "
        f"{item.expected_delivery_date.isoformat()}"
        for item in decision.supply_ledger.committed_inbound
    ) or "none"
    selection = (
        "; ".join(
            f"route {line.route_id} supplier {line.supplier_id} quantity {_number(line.quantity)}"
            for line in decision.selected_plan.lines
        )
        if decision.selected_plan is not None
        else "none"
    )
    rules = _evidence_rule_ids(decision, decision.selected_plan)
    blocking = _contract_blocking_evidence(decision, decision.selected_plan)
    contract_dispositions = (
        " Contract dispositions ["
        + _list(
            f"{item.rule_id}={item.contract_disposition.value}"
            for item in blocking
            if item.contract_disposition is not None
        )
        + "]."
        if blocking
        else ""
    )
    lateness = "; ".join(
        f"{item.due_date.isoformat()} late quantity {_number(item.late_quantity)} "
        f"unit-late-days {_number(item.unit_late_days)} unresolved "
        f"{_number(item.unresolved_quantity)}"
        for item in decision.deadline_lateness
    ) or "none"
    return sanitize_control_characters(
        f"Requirement {decision.requirement_id} for component {decision.component_id}: "
        f"demand [{demand}]; on hand {_number(decision.supply_ledger.on_hand)}; "
        f"counted inbound [{inbound}]; initial eventual gap "
        f"{_number(decision.initial_eventual_gap)}; selected [{selection}]; covered "
        f"{_number(decision.covered_quantity)}; residual {_number(decision.residual_gap)}; "
        f"fulfillment {decision.requirement_state.fulfillment.value}; resolution "
        f"{decision.requirement_state.resolution.value}; evidence contract "
        f"{decision.evidence_contract.value}; rule IDs [{_list(rules)}]."
        f"{contract_dispositions} Post-plan deadline misses [{lateness}]."
        f" Structured deciding comparators [{_comparator_facts(decision)}]."
        f" Material rejected routes [{_material_rejections(decision)}]."
        f"{_economic_autonomy_disclosure(decision)}"
    )


def render_line_rationale(decision: DecisionRecord, line: PlanLine) -> str:
    """Render one selected line, including fulfillment and resolution explicitly."""

    if decision.selected_plan is None or line not in decision.selected_plan.lines:
        raise ExplanationError("line must belong to the decision's selected plan")
    allocations = "; ".join(
        f"{allocation.due_date.isoformat()} quantity {_number(allocation.quantity)}"
        f" exceptions [{_list(allocation.exception_ids)}]"
        for allocation in line.bucket_allocations
    )
    plan = decision.selected_plan
    assumptions = set(plan.assumption_codes)
    for evidence in (*decision.evidence, *plan.evidence):
        assumptions.update(evidence.assumption_codes)
    evidence = ", ".join(
        f"{item.rule_id}={item.status.value}"
        for item in sorted(
            {*decision.evidence, *plan.evidence},
            key=lambda item: (item.rule_id, item.status.value),
        )
    ) or "none"
    objective = ", ".join(_number(item) for item in plan.objective_vector) or "none"
    lead_days = (line.expected_delivery_date - line.order_date).days
    return sanitize_control_characters(
        f"Requirement {decision.requirement_id}; component {decision.component_id}; "
        f"supplier {line.supplier_id}; route {line.route_id}; ordered quantity "
        f"{_number(line.quantity)} at unit price {_number(line.unit_price)} for line total "
        f"{_number(line.line_total)}; order date {line.order_date.isoformat()}; expected "
        f"delivery {line.expected_delivery_date.isoformat()} from approved lead time "
        f"{lead_days} days; material available "
        f"{line.material_available_date.isoformat()}; allocations [{allocations}]. "
        f"Demand {_number(decision.total_requirement)}, on hand "
        f"{_number(decision.supply_ledger.on_hand)}, counted inbound "
        f"{_number(sum((item.quantity for item in decision.supply_ledger.committed_inbound), ZERO))}, "
        f"forced surplus {_number(plan.forced_surplus)}, recovery demand "
        f"{_number(plan.recovery_demand)}, recovery quantity "
        f"{_number(plan.recovery_quantity)}, discretionary surplus "
        f"{_number(plan.discretionary_surplus)}, unit-late-days "
        f"{_number(plan.unit_late_days)}, residual eventual quantity "
        f"{_number(decision.residual_gap)}. Fulfillment "
        f"{decision.requirement_state.fulfillment.value}; resolution "
        f"{decision.requirement_state.resolution.value}; disposition {plan.disposition.value}; "
        f"objective [{objective}]; evidence [{evidence}]; deciding comparators "
        f"[{_comparator_facts(decision)}]; material rejected routes "
        f"[{_material_rejections(decision)}]; disclosures "
        f"[{_list(item.value for item in decision.alert_categories)}]; "
        f"assumptions [{_list(assumptions)}]."
        f"{_economic_autonomy_disclosure(decision)}"
    )


_ASSUMPTION_EXPLANATIONS = {
    "ROLLING_HISTORY_UNKNOWN": (
        "missing 12-month supplier-allocation history is permitted by the benchmark contract"
    ),
    "INFERRED_CONCEPT_MEMBERSHIP": (
        "component policy classification is inferred from master data"
    ),
    "ROBUST_BOTH_WAYS": (
        "the plan passes both possible policy classifications"
    ),
    "MODEL_RESIDUAL_CLASSIFICATION": (
        "a high-confidence optional model classification resolved otherwise "
        "unknown policy-concept membership; its evidence hashes are retained "
        "in the decision audit"
    ),
    "PCB_INCUMBENCY_INFERRED": (
        "prior orders establish PCB supplier incumbency because accepted-receipt "
        "history is missing"
    ),
    "PCB_INBOUND_HISTORY_UNKNOWN": (
        "accepted-receipt history needed to prove PCB supplier incumbency is unavailable"
    ),
    "BELOW_B_REVIEW_DISCHARGED_BY_MEMO": (
        "a VP memo satisfies additional review for a below-B supplier in the plan"
    ),
    "SUSTAINABILITY_RATING_UNKNOWN": "the supplier's sustainability rating is unavailable",
    "RELATIONSHIP_TIER_UNKNOWN": "the supplier's relationship tier is unavailable",
    "SUPPLIER_ATTRIBUTE_UNKNOWN": "a required supplier attribute is unavailable",
    "SUPPLIER_COUNTRY_UNKNOWN": "the supplier's country is unavailable",
    "APPROVED_LIST_STATE_UNKNOWN": "the supplier's approved-list state is unavailable",
    "DEMAND_CLASSIFICATION_UNKNOWN": "the demand classification is unavailable",
    "SOURCE_NAMED_ENTITY_UNRESOLVED": (
        "a supplier named by the governing source document could not be resolved"
    ),
    "SELECTOR_ENTITY_MISSING": (
        "an entity needed to determine policy scope is unavailable"
    ),
    "AIR_FREIGHT_PERIOD_SPEND_UNKNOWN": (
        "air-freight spend for the applicable authorization period is unavailable"
    ),
    "NO_ALTERNATIVE_PROOF_REQUIRED": (
        "the absence of a better-qualified alternative has not been independently proven"
    ),
    "BELOW_B_REVIEW_REQUIRED": (
        "additional review is still required for the supplier's below-B rating"
    ),
    "UNKNOWN_UNIT_TREATED_AS_DISCRETE": (
        "the unrecognized unit of measure was conservatively treated as discrete"
    ),
}


_SELECTION_REASONS = {
    "on_time_feasibility": "best meets the required material dates",
    "domestic_preference": "follows the domestic-sourcing preference",
    "strategic_retention": (
        "preserves strategic-supplier continuity within the policy savings threshold"
    ),
    "sustainability_band": (
        "follows the sustainability preference among comparable offers"
    ),
    "known_landed_cost": "has the lowest known cost after policy checks",
    "shorter_lead_time": (
        "has the shorter lead time after higher-priority policy ties"
    ),
    "id_free_fingerprint": (
        "uses the deterministic tie-break among otherwise equivalent routes"
    ),
}


def _human_join(values: Sequence[str]) -> str:
    items = tuple(value for value in values if value)
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def _purchase_order_demand_reason(
    decision: DecisionRecord,
    line: PlanLine,
) -> str:
    plan = decision.selected_plan
    if plan is None:
        raise ExplanationError("purchase-order demand reasoning requires a selected plan")
    due_dates = tuple(sorted({item.due_date for item in line.bucket_allocations}))
    order_ids = tuple(
        sorted(
            {
                contribution.order_id
                for bucket in decision.demand_buckets
                if bucket.due_date in due_dates
                for contribution in bucket.contributions
            }
        )
    )
    if len(plan.lines) > 1:
        effect = (
            "Contributes to closing"
            if decision.residual_gap == ZERO
            else "Contributes to reducing"
        )
    else:
        effect = "Closes" if decision.residual_gap == ZERO else "Reduces"
    target = (
        f" for {_human_join(order_ids)}"
        if order_ids
        else " for the assigned production demand"
    )
    if len(due_dates) == 1:
        timing = f" due {due_dates[0].isoformat()}"
    elif due_dates:
        timing = (
            f", with demand due from {due_dates[0].isoformat()} through "
            f"{due_dates[-1].isoformat()}"
        )
    else:
        timing = ""
    return (
        f"{effect} the {_number(plan.net_requirement)}-unit projected shortage"
        f"{target}{timing}."
    )


def _purchase_order_selection_reason(
    decision: DecisionRecord,
    line: PlanLine,
) -> str:
    plan = decision.selected_plan
    if plan is None:
        raise ExplanationError("purchase-order selection reasoning requires a selected plan")
    decisive = tuple(
        item
        for item in decision.comparator_facts
        if item.kind == "route_selection"
        and item.decisive
        and line.route_id in item.selected_route_ids
    )
    reasons: list[str] = []
    for item in decisive:
        reason = _SELECTION_REASONS.get(
            item.comparator,
            "won the decisive reviewed policy comparison",
        )
        if reason not in reasons:
            reasons.append(reason)
    late_allocations = tuple(
        item
        for item in line.bucket_allocations
        if line.material_available_date > item.due_date
    )
    if late_allocations:
        recovery = (
            "Recovery supply: no executable route met every assigned material deadline."
        )
    else:
        recovery = ""
    if reasons:
        selection = f"Supplier choice {_human_join(reasons)}."
    elif len(plan.lines) > 1:
        selection = (
            "Part of the lowest-cost supplier mix satisfying timing and allocation rules."
        )
    else:
        selection = (
            "Only executable route after supplier eligibility, policy, and timing checks."
        )
    return f"{selection} {recovery}".strip()


def _purchase_order_quantity_reason(plan: CandidatePlan) -> str:
    reasons: list[str] = []
    if plan.forced_surplus > ZERO:
        reasons.append(
            f"Quantity rule: supplier minimums/order increments raise the plan from "
            f"{_number(plan.net_requirement)} to "
            f"{_number(plan.minimum_compliant_total or ZERO)} units "
            f"({_number(plan.forced_surplus)} surplus)."
        )
    if plan.discretionary_surplus > ZERO:
        reasons.append(
            f"Allocation rule: the plan adds {_number(plan.discretionary_surplus)} units "
            "above the certified minimum, within the reviewed autonomy limit."
        )
    if plan.recovery_quantity > ZERO:
        reasons.append(
            f"The plan includes {_number(plan.recovery_quantity)} units of recovery supply "
            "to offset late committed inbound."
        )
    return " ".join(reasons)


def _purchase_order_sourcing_exception(line: PlanLine) -> str:
    exception_ids = {
        item
        for allocation in line.bucket_allocations
        for item in allocation.exception_ids
    }
    reasons: list[str] = []
    if any(item.endswith(":condition_a") for item in exception_ids):
        reasons.append("no domestic route meets the required timeline")
    if any(item.endswith(":condition_b") for item in exception_ids):
        reasons.append("the domestic price premium exceeds the applicable policy threshold")
    if any(item.endswith(":condition_c") for item in exception_ids):
        reasons.append("the component is unavailable from domestic sources")
    if reasons:
        return f"International sourcing: {_human_join(reasons)}."
    if exception_ids:
        return "A reviewed sourcing exception applies to this line's assigned demand."
    return ""


def _purchase_order_assumption_reason(
    decision: DecisionRecord,
    plan: CandidatePlan,
) -> str:
    assumptions = set(plan.assumption_codes)
    for evidence in (*decision.evidence, *plan.evidence):
        assumptions.update(evidence.assumption_codes)
    explanations: list[str] = []
    if {
        "INFERRED_CONCEPT_MEMBERSHIP",
        "ROBUST_BOTH_WAYS",
    } <= assumptions:
        explanations.append(
            "component policy classification is inferred from master data; the plan "
            "passes either classification"
        )
        assumptions.difference_update(
            {"INFERRED_CONCEPT_MEMBERSHIP", "ROBUST_BOTH_WAYS"}
        )
    for code in sorted(assumptions):
        explanation = _ASSUMPTION_EXPLANATIONS.get(
            code,
            "a documented planning assumption was required; its source evidence is in "
            "the decision audit",
        )
        if explanation not in explanations:
            explanations.append(explanation)
    if not explanations:
        return ""
    label = "Assumption" if len(explanations) == 1 else "Assumptions"
    return f"{label}: {'; '.join(explanations)}."


def render_purchase_order_rationale(decision: DecisionRecord, line: PlanLine) -> str:
    """Explain why a purchase order exists without repeating its business columns.

    Quantity, component, supplier, price, order date, and delivery date already
    have dedicated columns.  The rationale therefore carries only decision
    context: the demand trigger, the material selection reason, non-obvious
    quantity calibration, and plain-language assumptions.  The exhaustive
    evidence and comparator trace remain in the decision-audit table.
    """

    if decision.selected_plan is None or line not in decision.selected_plan.lines:
        raise ExplanationError("line must belong to the decision's selected plan")
    plan = decision.selected_plan
    parts = [
        _purchase_order_demand_reason(decision, line),
        _purchase_order_selection_reason(decision, line),
    ]
    quantity_reason = _purchase_order_quantity_reason(plan)
    if quantity_reason:
        parts.append(quantity_reason)
    sourcing_exception = _purchase_order_sourcing_exception(line)
    if sourcing_exception:
        parts.append(sourcing_exception)
    assumption_reason = _purchase_order_assumption_reason(decision, plan)
    if assumption_reason:
        parts.append(assumption_reason)
    return sanitize_control_characters(" ".join(parts))


def render_legacy_v1_decision_rationale(decision: DecisionRecord) -> str:
    """Reproduce the canonical rationale embedded in pre-R02 v1 PO markers."""

    if not isinstance(decision, DecisionRecord):
        raise TypeError("decision must be DecisionRecord")
    demand = "; ".join(
        f"{item.order_id} due {bucket.due_date.isoformat()} quantity {_number(item.quantity)}"
        for bucket in decision.demand_buckets
        for item in bucket.contributions
    )
    inbound = "; ".join(
        f"{item.po_number} quantity {_number(item.quantity)} due "
        f"{item.expected_delivery_date.isoformat()}"
        for item in decision.supply_ledger.committed_inbound
    ) or "none"
    selection = (
        "; ".join(
            f"route {line.route_id} supplier {line.supplier_id} quantity {_number(line.quantity)}"
            for line in decision.selected_plan.lines
        )
        if decision.selected_plan is not None
        else "none"
    )
    rules = _evidence_rule_ids(decision, decision.selected_plan)
    return sanitize_control_characters(
        f"Requirement {decision.requirement_id} for component {decision.component_id}: "
        f"demand [{demand}]; on hand {_number(decision.supply_ledger.on_hand)}; "
        f"counted inbound [{inbound}]; initial eventual gap "
        f"{_number(decision.initial_eventual_gap)}; selected [{selection}]; covered "
        f"{_number(decision.covered_quantity)}; residual {_number(decision.residual_gap)}; "
        f"fulfillment {decision.requirement_state.fulfillment.value}; resolution "
        f"{decision.requirement_state.resolution.value}; evidence contract "
        f"{decision.evidence_contract.value}; rule IDs [{_list(rules)}]."
    )


def render_legacy_v1_line_rationale(
    decision: DecisionRecord,
    line: PlanLine,
) -> str:
    """Reproduce the canonical line rationale embedded in pre-R02 v1 markers."""

    if decision.selected_plan is None or line not in decision.selected_plan.lines:
        raise ExplanationError("line must belong to the decision's selected plan")
    allocations = "; ".join(
        f"{allocation.due_date.isoformat()} quantity {_number(allocation.quantity)}"
        f" exceptions [{_list(allocation.exception_ids)}]"
        for allocation in line.bucket_allocations
    )
    plan = decision.selected_plan
    assumptions = set(plan.assumption_codes)
    for evidence in (*decision.evidence, *plan.evidence):
        assumptions.update(evidence.assumption_codes)
    evidence = ", ".join(
        f"{item.rule_id}={item.status.value}"
        for item in sorted(
            {*decision.evidence, *plan.evidence},
            key=lambda item: (item.rule_id, item.status.value),
        )
    ) or "none"
    alternatives = "; ".join(
        f"{candidate.plan_id} disposition {candidate.disposition.value}, routes "
        f"[{_list(item.route_id for item in candidate.lines)}], relaxed rules "
        f"[{_list(candidate.relaxed_rule_ids)}]"
        for candidate in decision.alternatives
    ) or "none"
    objective = ", ".join(_number(item) for item in plan.objective_vector) or "none"
    lead_days = (line.expected_delivery_date - line.order_date).days
    return sanitize_control_characters(
        f"Requirement {decision.requirement_id}; component {decision.component_id}; "
        f"supplier {line.supplier_id}; route {line.route_id}; ordered quantity "
        f"{_number(line.quantity)} at unit price {_number(line.unit_price)} for line total "
        f"{_number(line.line_total)}; order date {line.order_date.isoformat()}; expected "
        f"delivery {line.expected_delivery_date.isoformat()} from approved lead time "
        f"{lead_days} days; material available "
        f"{line.material_available_date.isoformat()}; allocations [{allocations}]. "
        f"Demand {_number(decision.total_requirement)}, on hand "
        f"{_number(decision.supply_ledger.on_hand)}, counted inbound "
        f"{_number(sum((item.quantity for item in decision.supply_ledger.committed_inbound), ZERO))}, "
        f"forced surplus {_number(plan.forced_surplus)}, recovery/discretionary surplus "
        f"{_number(plan.discretionary_surplus)}, residual eventual quantity "
        f"{_number(decision.residual_gap)}. Fulfillment "
        f"{decision.requirement_state.fulfillment.value}; resolution "
        f"{decision.requirement_state.resolution.value}; disposition {plan.disposition.value}; "
        f"objective [{objective}]; evidence [{evidence}]; alternatives and material rejections "
        f"[{alternatives}]; disclosures [{_list(item.value for item in decision.alert_categories)}]; "
        f"assumptions [{_list(assumptions)}]."
    )


def approval_rule(registry: PolicyRegistry, rule_id: str, supplier_id: str) -> ApprovalRule:
    """Extract a named authority and threshold from one reviewed policy rule."""

    try:
        rule = registry.rule(rule_id)
    except KeyError as error:
        raise ExplanationError(f"approval rule is absent from the policy pack: {rule_id}") from error
    constraint = rule.data.get("constraint")
    if not isinstance(constraint, Mapping):
        raise ExplanationError(f"approval rule has no structured constraint: {rule_id}")
    kind = constraint.get("kind")
    try:
        path = terminal_rendering_path(
            str(kind), ContractDisposition.RECOMMEND_APPROVAL
        )
    except (KeyError, ValueError) as error:
        raise ExplanationError(
            f"internal approval-rendering contract defect for {rule_id}: {kind!r}"
        ) from error
    if path is not TerminalRenderingPath.COMPLETE_APPROVAL_PROPOSAL:
        raise ExplanationError(
            f"no reviewed approval renderer for {rule_id}: {kind!r}"
        )
    if kind == "order_value_approval":
        authority = constraint.get("authority")
        amount = constraint.get("amount_exceeds")
        threshold = f"line total exceeds {_number(Decimal(str(amount)))}"
        required_action = "approve the complete proposal"
    elif kind == "sub_moq_written_approval":
        authority = f"supplier {supplier_id}"
        threshold = "quantity is below the supplier catalog minimum order quantity"
        required_action = "provide written approval for the complete sub-MOQ proposal"
    elif kind == "hazardous_procurement_review":
        authority = "hazardous-material procurement reviewer"
        threshold = "the component requires hazardous-material procurement review"
        required_action = "complete the hazardous-material procurement review"
    elif kind == "emergency_approval_bypass":
        authority = "procurement engineering and the authorized emergency approver"
        threshold = "an emergency approval-bypass claim requires a supported qualifying predicate"
        required_action = (
            "supply a supported qualifying record and complete the required approval workflow"
        )
    elif kind == "below_rating_review":
        authority = "procurement reviewer"
        threshold = "a below-B supplier requires additional review after no alternative is proven"
        required_action = "complete the additional supplier review"
    elif kind == "strategic_volume_shift_approval":
        authority = constraint.get("authority")
        threshold = "a significant strategic-supplier volume shift is proposed"
        required_action = "approve the complete strategic-volume-shift proposal"
    elif kind == "air_freight_period_spend_cap":
        authority = "procurement operations"
        maximum = constraint.get("maximum_amount")
        threshold = (
            "authorization-period air-freight spend must be proven at or below "
            f"{_number(Decimal(str(maximum)))} including the proposal"
        )
        required_action = (
            "supply authoritative period-spend evidence; approval cannot waive the cap"
        )
    elif kind == "air_freight_individual_approval":
        authority = constraint.get("authority")
        threshold = "an individual air-freight request is proposed"
        required_action = "approve the complete individual air-freight request"
    else:
        raise ExplanationError(f"unsupported approval constraint for {rule_id}: {kind!r}")
    if not isinstance(authority, str):
        raise ExplanationError(f"approval rule has no named authority: {rule_id}")
    return ApprovalRule(
        rule_id=rule_id,
        authority=authority,
        threshold=threshold,
        required_action=required_action,
    )


def _alert_key(
    category: AlertCategory,
    scope: str,
    body: str,
    *,
    version: int = ALERT_MARKER_VERSION,
) -> str:
    payload = canonical_dumps(
        {"version": version, "category": category.value, "scope": scope, "body": body}
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _concise_alert_body(category: AlertCategory, scope: str, body: str) -> str:
    """Reduce verbose audit prose to one operational issue and one action."""

    if category is AlertCategory.RUN_ACCOUNTING:
        match = re.search(
            r"Managed (?P<requirements>[0-9]+) component requirements.*?"
            r"Agent purchase orders: (?P<orders>[0-9]+); ordered cost: "
            r"(?P<cost>[^;]+);",
            body,
        )
        if match is not None:
            return (
                f"Run completed for {match.group('requirements')} component requirements: "
                f"{match.group('orders')} purchase orders totaling {match.group('cost')}."
            )

    if scope.startswith("run:assumption:"):
        code = scope.rsplit(":", 1)[-1]
        return (
            f"Run used policy assumption {code}. "
            "Approve or replace it in the reviewed policy pack, then rerun if it changes."
        )

    assumption = re.match(
        r"Component (?P<component>[^,]+), requirement [^ ]+ relied on assumption "
        r"(?P<code>[A-Z0-9_]+) under the (?P<contract>[a-z]+) evidence contract\.",
        body,
    )
    if assumption is not None:
        return (
            f"Component {assumption.group('component')} used assumption "
            f"{assumption.group('code')} under the {assumption.group('contract')} contract. "
            "Verify the missing evidence and rerun if it changes."
        )

    visible_body = re.sub(
        r", requirement requirement:[A-Za-z0-9_.:-]+",
        "",
        body,
    )
    first, separator, _remaining = visible_body.partition(". ")
    issue = first + ("." if separator else "")
    action = ""
    if "Human action:" in visible_body:
        action_tail = visible_body.rsplit("Human action:", 1)[1].strip()
        action_text, action_separator, _after_action = action_tail.partition(". ")
        action = f" Action: {action_text}{'.' if action_separator else ''}"
    return sanitize_control_characters(issue + action)


def _alert_lead(category: AlertCategory, body: str) -> str:
    if category in ERROR_ALERT_CATEGORIES:
        return "Error"
    if category is AlertCategory.EVIDENCE_CONTRACT:
        return "Error" if "Run-global production" in body else "Recommendation"
    if category is AlertCategory.DATA_QUALITY and "failed the route-input contract" in body:
        return "Error"
    return "Recommendation"


def make_owned_alert(
    category: AlertCategory,
    scope: str,
    body: str,
    *,
    visible_prefix: bool = False,
) -> RenderedAlert:
    """Create concise alert prose while retaining the detailed text for audit."""

    if not isinstance(category, AlertCategory):
        raise TypeError("category must be AlertCategory")
    scope = sanitize_control_characters(scope)
    body = sanitize_control_characters(body)
    _require_safe_text(scope, "scope")
    _require_safe_text(body, "body")
    concise = _concise_alert_body(category, scope, body)
    category_tag = f"[{category.value}] " if visible_prefix else ""
    visible = f"{_alert_lead(category, body)}: {category_tag}{concise}"
    key = _alert_key(category, scope, visible)
    description = (
        f"{visible} [APEX_ALERT:v{ALERT_MARKER_VERSION} key={key} "
        f"category={category.value} scope={_encode_scope(scope)}]"
    )
    return RenderedAlert(
        key=key,
        category=category,
        scope=scope,
        description=description,
        audit_description=body,
    )


def parse_owned_alert(description: str) -> ParsedAlertMarker | None:
    """Return a marker only when syntax, category, and digest all validate."""

    if not isinstance(description, str):
        return None
    match = _ALERT_MARKER.search(description)
    if match is None:
        return None
    marker_version = int(match.group("version"))
    if marker_version not in _SUPPORTED_ALERT_MARKER_VERSIONS:
        return None
    try:
        category = AlertCategory(match.group("category"))
    except ValueError:
        return None
    scope = _decode_scope(match.group("scope"))
    if scope is None:
        return None
    body = description[: match.start()]
    if match.group("key") != _alert_key(category, scope, body, version=marker_version):
        return None
    return ParsedAlertMarker(
        key=match.group("key"), category=category, scope=scope, body=body
    )


def validate_owned_alert(alert: RenderedAlert) -> bool:
    parsed = parse_owned_alert(alert.description)
    return (
        parsed is not None
        and parsed.scope == alert.scope
        and parsed.key == alert.key
    )


def validate_stored_alert(description: str, scopes: Iterable[str]) -> ParsedAlertMarker | None:
    """Validate a stored marker against candidate deterministic scopes.

    Stored alerts have no scope column.  The caller supplies current and prior
    requirement scopes; an obsolete marker remains owned because its digest is
    also accepted under the reserved ``legacy`` scope derived from its key.
    Target reconciliation primarily matches complete descriptions.
    """

    parsed = parse_owned_alert(description)
    if parsed is None:
        return None
    # ``scopes`` remains in the signature for compatibility with commit callers
    # and to make the reconciliation boundary explicit.  The encoded scope is
    # self-validating, so obsolete owned alerts can be safely recognized even
    # after their requirement disappears from the current target set.
    tuple(scopes)
    return parsed


def _generic_alert(decision: DecisionRecord, category: AlertCategory) -> str:
    issue, action = _GENERIC_ACTIONS[category]
    rules = _evidence_rule_ids(decision, decision.selected_plan)
    quantified = ""
    if decision.selected_plan is not None and category is AlertCategory.CAPACITY_UNKNOWN:
        quantities: dict[str, Decimal] = {}
        for line in decision.selected_plan.lines:
            quantities[line.supplier_id] = (
                quantities.get(line.supplier_id, ZERO) + line.quantity
            )
        planned = ", ".join(
            f"{_number(quantity)} units from {supplier_id}"
            for supplier_id, quantity in sorted(quantities.items())
        )
        return (
            f"Supplier capacity is not recorded for component {decision.component_id}, "
            f"although the selected plan orders {planned}. The scenario contains no numeric "
            f"capacity evidence for release validation. Applicable rule IDs "
            f"[{_list(rules)}]. Human action: confirm the planned quantities with the "
            "suppliers before releasing the purchase orders."
        )
    if decision.selected_plan is not None and category is AlertCategory.COST_OPPORTUNITY:
        lower_cost_options = tuple(
            plan
            for plan in decision.alternatives
            if plan.total_cost < decision.selected_plan.total_cost
            and plan.relaxed_rule_ids
        )
        if lower_cost_options:
            option = min(
                lower_cost_options,
                key=lambda plan: (plan.total_cost, plan.plan_id),
            )
            savings = decision.selected_plan.total_cost - option.total_cost
            return (
                f"Component {decision.component_id} has a lower-cost sourcing option "
                f"estimated at {_money(option.total_cost)}, saving {_money(savings)} versus "
                f"the {_money(decision.selected_plan.total_cost)} selected plan, but the "
                "option relaxes current supplier-allocation rules. The compliant selected "
                f"plan remains in effect under rule IDs [{_list(rules)}]. Human action: "
                "review whether to request a policy exception; keep the current orders "
                "unless a fresh run authorizes a change."
            )
    if decision.selected_plan is not None and category is AlertCategory.FORCED_SURPLUS:
        plan = decision.selected_plan
        lines = "; ".join(
            f"supplier {line.supplier_id}, quantity {_number(line.quantity)}, "
            f"unit price {_number(line.unit_price)}, line total {_number(line.line_total)}"
            for line in plan.lines
        )
        return (
            f"Component {decision.component_id} requires {_number(plan.net_requirement)} units, "
            f"but supplier or whole-unit quantity rules require ordering "
            f"{_number(plan.ordered_quantity)}, creating {_number(plan.forced_surplus)} surplus "
            f"units with an estimated value of {_money(plan.estimated_forced_surplus_value)}. "
            f"Executed action: [{lines}], total cost {_number(plan.total_cost)}, forced "
            f"surplus {_number(plan.forced_surplus)} against net requirement "
            f"{_number(plan.net_requirement)}. The agent preserved existing commitments and "
            f"committed only this minimum compliant outcome under rule IDs [{_list(rules)}]; "
            "no mutually exclusive sub-MOQ approval request remains live. A sub-MOQ "
            "alternative could be considered only after a future cancellation contract "
            "authorizes changing the commitment and a fresh run revalidates demand, inbound, "
            f"policy, quantity, supplier, price, and dates. Human action: the order was placed "
            f"successfully; {action}."
        )
    elif decision.selected_plan is not None and category is AlertCategory.RECOVERY_SURPLUS:
        quantified = (
            " Strictly improving recovery quantity is "
            f"{_number(decision.selected_plan.recovery_quantity)} against "
            f"{_number(decision.selected_plan.recovery_demand)} recoverable units."
        )
    elif category is AlertCategory.LATE_ARRIVAL and decision.selected_plan is not None:
        selected_dates = tuple(
            item.expected_delivery_date for item in decision.selected_plan.lines
        )
        quantified = (
            f" Latest selected delivery is {max(selected_dates).isoformat()} and earliest "
            f"requirement date is {decision.demand_buckets[0].due_date.isoformat()}."
        )
    return (
        f"Component {decision.component_id}: {issue}. "
        f"The agent preserved existing commitments and used only the validated disposition; "
        f"residual quantity is {_number(decision.residual_gap)}. Applicable rule IDs "
        f"[{_list(rules)}].{quantified} Human action: {action}."
    )


def _approval_alert(
    decision: DecisionRecord,
    plan: CandidatePlan,
    approvals_by_route: Mapping[str, tuple[ApprovalRule, ...]],
) -> str:
    proposal_lines: list[str] = []
    required_actions: set[tuple[str, str]] = set()
    represented_rule_ids: set[str] = set()
    for line in plan.lines:
        approvals = approvals_by_route.get(line.route_id, ())
        required_actions.update(
            (item.authority, item.required_action) for item in approvals
        )
        represented_rule_ids.update(item.rule_id for item in approvals)
        thresholds = "; ".join(
            f"{item.threshold} under {item.rule_id}"
            for item in approvals
        ) or "none"
        line_authorities = _list(item.authority for item in approvals)
        timing = "; ".join(
            (
                f"{_number(allocation.quantity)} allocated to {allocation.due_date.isoformat()} "
                + (
                    f"is available {max(0, (allocation.due_date - line.material_available_date).days)} days before/on the deadline"
                    if line.material_available_date <= allocation.due_date
                    else f"is {max(0, (line.material_available_date - allocation.due_date).days)} days late"
                )
            )
            for allocation in line.bucket_allocations
        )
        proposal_lines.append(
            f"supplier {line.supplier_id}, full quantity {_number(line.quantity)}, unit price "
            f"{_number(line.unit_price)}, line total {_number(line.line_total)}, order date "
            f"{line.order_date.isoformat()}, expected delivery "
            f"{line.expected_delivery_date.isoformat()}, material available "
            f"{line.material_available_date.isoformat()}, timing impact [{timing}], thresholds "
            f"crossed [{thresholds}], required authorities [{line_authorities}]"
        )
    action_text = "; ".join(
        f"{authority} must {action}"
        for authority, action in sorted(required_actions)
    )
    return (
        f"Complete approval proposal for component {decision.component_id}, requirement "
        f"{decision.requirement_id}: [{'; '.join(proposal_lines)}]. The certified plan would "
        f"cover {_number(plan.eventual_covered_quantity)} units and leave "
        f"{_number(plan.residual_gap)} uncovered at the proposal snapshot. The agent withheld "
        f"every line atomically, including any sub-threshold companion line, so repeated runs "
        f"cannot split the requirement around an approval threshold. Applicable rule IDs "
        f"[{_list(represented_rule_ids)}]. Human action: {action_text}, then the planner must recompute "
        f"dates, quantities, eligibility, and price from a fresh snapshot."
    )


def _plan_option_summary(plan: CandidatePlan) -> str:
    grouped: dict[tuple[str, Decimal, date], Decimal] = {}
    for line in plan.lines:
        key = (line.supplier_id, line.unit_price, line.expected_delivery_date)
        grouped[key] = grouped.get(key, ZERO) + line.quantity
    lines = "; ".join(
        f"{_number(quantity)} units from {supplier_id} at {_money(unit_price)} per unit, "
        f"delivering {delivery.isoformat()}"
        for (supplier_id, unit_price, delivery), quantity in sorted(grouped.items())
    )
    return f"{lines}; estimated total {_money(plan.total_cost)}"


def _decision_alert(
    decision: DecisionRecord,
    plans: Sequence[CandidatePlan],
) -> str:
    if not plans:
        raise ExplanationError("a decision-required alert needs at least one option")
    option = min(plans, key=lambda plan: (plan.total_cost, plan.plan_id))
    proposal = _plan_option_summary(option)
    blocking = tuple(
        sorted(
            {
                item
                for plan in plans
                for item in _contract_blocking_evidence(decision, plan)
            },
            key=lambda item: item.rule_id,
        )
    )
    plan_ids = _list(plan.plan_id for plan in plans)
    if blocking and decision.evidence_contract is EvidenceContract.PRODUCTION:
        missing = "; ".join(
            f"{item.rule_id}: {item.summary} "
            f"(contract disposition {item.contract_disposition.value})"
            for item in blocking
            if item.contract_disposition is not None
        )
        return (
            f"An order for component {decision.component_id} was withheld because required "
            f"policy evidence is unavailable; the lowest-cost available option is {proposal}. "
            f"Internal option IDs [{plan_ids}]. Missing evidence [{missing}]. The agent "
            f"placed no line and did not classify any supplier as ineligible from the absent "
            f"facts. Residual quantity is {_number(decision.residual_gap)}. Applicable rule IDs "
            f"[{_list(item.rule_id for item in blocking)}]. Human action: "
            "supply the cited rolling-window or other authoritative contract evidence and "
            "rerun from a fresh snapshot."
        )
    rule_ids = tuple(
        sorted(
            {
                rule_id
                for plan in plans
                for rule_id in (
                    *plan.relaxed_rule_ids,
                    *_evidence_rule_ids(decision, plan),
                )
            }
        )
    )
    return (
        f"An order for component {decision.component_id} was withheld because no available "
        f"option is authorized under current policy; the lowest-cost option is {proposal}. "
        f"Internal option IDs [{plan_ids}]. The agent did not place an alternative and left residual quantity "
        f"{_number(decision.residual_gap)}. Applicable rule IDs [{_list(rule_ids)}]. "
        "Human action: review the proposed option, resolve the cited policy constraint, "
        "and rerun from a fresh snapshot."
    )


def _evidence_contract_alert(decision: DecisionRecord) -> str:
    blocking = _contract_blocking_evidence(decision)
    missing = "; ".join(
        f"{item.rule_id}: {item.summary} "
        f"(contract disposition {item.contract_disposition.value})"
        for item in blocking
        if item.contract_disposition is not None
    ) or "the cited authoritative evidence is unavailable"
    return (
        f"Component {decision.component_id}, requirement {decision.requirement_id} is governed "
        f"by the {decision.evidence_contract.value} evidence contract; {missing}. The agent "
        "preserved the hard UNKNOWN result, withheld every affected action, and did not infer "
        "zero history or supplier ineligibility. Human action: provide the missing authoritative "
        "evidence and rerun from a fresh snapshot."
    )


def _source_entity_normalization_alert(
    decision: DecisionRecord,
    disclosure: SourceEntityNormalizationDisclosure,
) -> str:
    return (
        f"Component {decision.component_id}, requirement {decision.requirement_id}: "
        f"policy source {disclosure.source_document}, rule {disclosure.rule_id}, "
        f"reference {disclosure.reference_path} retains stale source ID "
        f"{disclosure.source_id} and legal name {disclosure.legal_name!r}. The agent "
        f"resolved current supplier ID {disclosure.resolved_supplier_id} using one "
        "unique exact normalized legal-name match; it did not trust the stale ID or "
        "perform a fuzzy match. Human action: correct the policy source ID during the "
        "next reviewed policy-pack update."
    )


def _unit_normalization_alert(
    decision: DecisionRecord,
    disclosure: UnitNormalizationDisclosure,
) -> str:
    return (
        f"Component {decision.component_id}, requirement {decision.requirement_id}: "
        f"source {disclosure.source_table}.{disclosure.source_field} supplied unit "
        f"{disclosure.source_unit!r}, which has no configured unit semantics. The "
        "deterministic fallback treats the unit as discrete: aggregate exact demand "
        f"once, then round up by ceiling to increment {_number(disclosure.increment)} "
        "before applying MOQ. No pack size or conversion factor was guessed. Human "
        "action: configure the source unit if different quantity semantics are required."
    )


def _residual_alert(decision: DecisionRecord) -> str:
    return (
        f"Component {decision.component_id}, requirement {decision.requirement_id} has residual "
        f"eventual quantity {_number(decision.residual_gap)} with fulfillment "
        f"{decision.requirement_state.fulfillment.value} and resolution "
        f"{decision.requirement_state.resolution.value}. The agent placed only validated, "
        f"authorized lines and preserved existing commitments. Human action: resolve the cited "
        f"evidence, approval, policy, or certified infeasibility outcome before rerunning."
    )


def _solver_alert(decision: DecisionRecord) -> str:
    rule_ids = _evidence_rule_ids(decision, decision.selected_plan)
    return (
        f"Component {decision.component_id}, requirement {decision.requirement_id} has no proven "
        f"executable conclusion because the solver result was not certified complete. The agent "
        f"placed no line supported by that result and did not claim infeasibility. Applicable "
        f"rule IDs [{_list(rule_ids)}]. Engineering action: rerun with a completed optimality "
        f"certificate and exact post-validation."
    )


def _late_alert(decision: DecisionRecord, due_date: date) -> str:
    lateness = next(
        item for item in decision.deadline_lateness if item.due_date == due_date
    )
    arrivals = tuple(
        line.material_available_date
        for line in (
            decision.selected_plan.lines
            if decision.selected_plan is not None
            else ()
        )
        if any(
            allocation.due_date == due_date and allocation.quantity > ZERO
            for allocation in line.bucket_allocations
        )
    )
    arrival = (
        f"; planned supply for that deadline arrives as late as {max(arrivals).isoformat()}"
        if arrivals
        else ""
    )
    eventual = (
        "All affected quantity has an eventual receipt."
        if lateness.unresolved_quantity == ZERO
        else f"{_number(lateness.unresolved_quantity)} units still have no eventual receipt."
    )
    return (
        f"Component {decision.component_id} will be {_number(lateness.late_quantity)} units "
        f"short on its {lateness.due_date.isoformat()} material deadline{arrival}. "
        f"The delay represents {_number(lateness.unit_late_days)} unit-days. {eventual} "
        "The agent preserved existing commitments and placed only the strictly improving, "
        "authorized recovery plan shown in the managed decision. Human action: review the "
        "affected production date and decide whether to expedite further or reschedule."
    )


def _route_input_data_quality_alert(
    issue: RouteInputIssue,
    applicable_rule_ids: Iterable[str],
) -> str:
    logical_key = f"supplier_id={issue.supplier_id}"
    if issue.component_id is not None:
        logical_key += f", component_id={issue.component_id}"
    blast_radius = (
        "only this supplier/component catalog offer"
        if issue.blast_radius is RouteQuarantineScope.CATALOG_OFFER
        else "all catalog routes for this supplier"
    )
    affected = ", ".join(issue.affected_component_ids) or "none"
    return (
        f"Source table {issue.source_table}, logical key ({logical_key}), field "
        f"{issue.field} failed the route-input contract because {issue.safe_reason} "
        f"(reason {issue.reason_code}). Blast radius: {blast_radius}; affected "
        f"component IDs: [{affected}]. The agent {issue.action}. Applicable rule IDs "
        f"[{_list(applicable_rule_ids, empty='none identifiable before source remediation')}]. "
        f"Human remediation: "
        f"{issue.remediation}."
    )


def _internal_failure_alert(exclusion: InternalFailureExclusion) -> str:
    codes = _list(item.code for item in exclusion.issues)
    return (
        f"Internal validation failure for component {exclusion.component_id}, "
        f"requirement {exclusion.requirement_id}: reviewed component-local codes "
        f"[{codes}]. The agent removed every executable action and solver result "
        "for this component before a fresh independent validation of all survivors; "
        "no purchase order or procurement approval/decision request was produced for "
        f"the excluded component. Owner: {exclusion.owner}. Engineering action: "
        "diagnose and correct the internal validation defect, then rerun from a fresh "
        "snapshot."
    )


def render_alerts(
    decisions: Sequence[DecisionRecord],
    *,
    policy_registry: PolicyRegistry | None = None,
    active_directives: Sequence[str] = (),
    inactive_directives: Sequence[str] = (),
    visible_prefixes: bool = False,
    route_input_issues: Sequence[RouteInputIssue] = (),
    internal_failure_exclusions: Sequence[InternalFailureExclusion] = (),
) -> tuple[RenderedAlert, ...]:
    """Render actionable problems and recommendations for the alerts table."""

    # Directive accounting remains part of the run audit/CLI contract, not the
    # operational alerts table. Keep accepting these arguments for API stability.
    records = tuple(sorted(decisions, key=lambda item: (item.requirement_id, item.component_id)))
    if any(not isinstance(item, DecisionRecord) for item in records):
        raise TypeError("decisions must contain DecisionRecord values")
    requirement_ids = tuple(item.requirement_id for item in records)
    if len(requirement_ids) != len(set(requirement_ids)):
        raise ExplanationError("managed decisions contain duplicate requirement IDs")
    registry = policy_registry
    rendered: list[RenderedAlert] = []
    source_issues = tuple(route_input_issues)
    if any(not isinstance(item, RouteInputIssue) for item in source_issues):
        raise TypeError("route_input_issues must contain RouteInputIssue values")
    source_issues = tuple(sorted(source_issues, key=lambda item: item.issue_id))
    exclusions = tuple(internal_failure_exclusions)
    if any(not isinstance(item, InternalFailureExclusion) for item in exclusions):
        raise TypeError(
            "internal_failure_exclusions must contain InternalFailureExclusion values"
        )
    exclusions = tuple(sorted(exclusions, key=lambda item: item.component_id))
    if len({item.component_id for item in exclusions}) != len(exclusions):
        raise ExplanationError("internal-failure exclusions contain duplicate components")
    if {item.component_id for item in exclusions} & {
        item.component_id for item in records
    }:
        raise ExplanationError(
            "an internal-failure component cannot also have a managed decision"
        )
    quarantine_components = frozenset(
        component_id
        for issue in source_issues
        for component_id in issue.affected_component_ids
    )
    evidence_contract_decisions: list[DecisionRecord] = []
    terminal_requirements: set[str] = set()

    def add(
        category: AlertCategory,
        scope: str,
        body: str,
        *,
        decision: DecisionRecord | None = None,
        terminal: bool = False,
    ) -> None:
        # ``decision`` remains an explicit call-site signal that this is a
        # requirement alert.  Run-global policy/contract boilerplate is
        # rendered once below rather than appended to every such alert.
        if terminal:
            if decision is None:
                raise AssertionError(
                    "a terminal requirement alert must identify its decision"
                )
            terminal_requirements.add(decision.requirement_id)
        rendered.append(
            make_owned_alert(category, scope, body, visible_prefix=visible_prefixes)
        )

    for exclusion in exclusions:
        add(
            AlertCategory.INTERNAL_FAILURE,
            f"requirement:{exclusion.requirement_id}:internal-failure",
            _internal_failure_alert(exclusion),
        )

    for issue in source_issues:
        affected_decisions = tuple(
            decision
            for decision in records
            if decision.component_id in issue.affected_component_ids
        )
        applicable_rule_ids = {
            rule_id
            for decision in affected_decisions
            for rule_id in _evidence_rule_ids(decision, decision.selected_plan)
        }
        add(
            AlertCategory.DATA_QUALITY,
            f"source-route-input:{issue.issue_id}",
            _route_input_data_quality_alert(issue, applicable_rule_ids),
        )
        terminal_requirements.update(
            decision.requirement_id for decision in affected_decisions
        )

    for decision in records:
        scope = f"requirement:{decision.requirement_id}"
        if decision.residual_gap > ZERO:
            add(
                AlertCategory.UNMET_DEMAND,
                f"{scope}:residual",
                _residual_alert(decision),
                decision=decision,
            )

        for index, disclosure in enumerate(
            decision.normalization_disclosures, start=1
        ):
            if isinstance(disclosure, SourceEntityNormalizationDisclosure):
                add(
                    AlertCategory.DATA_QUALITY,
                    f"{scope}:normalization:source-entity:{index}",
                    _source_entity_normalization_alert(decision, disclosure),
                    decision=decision,
                )
            elif isinstance(disclosure, UnitNormalizationDisclosure):
                add(
                    AlertCategory.DATA_QUALITY,
                    f"{scope}:normalization:unit:{index}",
                    _unit_normalization_alert(decision, disclosure),
                    decision=decision,
                )
            else:  # pragma: no cover - DecisionRecord enforces the union.
                raise ExplanationError("unsupported normalization disclosure")

        approval_count = 0
        decision_count = 0
        needs_human_resolution = (
            decision.requirement_state.resolution is ResolutionStatus.UNRESOLVED
        )
        decision_options: list[CandidatePlan] = []
        for plan in decision.alternatives:
            if plan.disposition is PlanDisposition.RECOMMEND_APPROVAL:
                if not needs_human_resolution:
                    continue
                if not plan.unresolved_approval_ids:
                    raise ExplanationError(
                        f"approval recommendation {plan.plan_id} has no approval rule ID"
                    )
                if registry is None:
                    registry = load_policy_registry()
                approvals_by_route: dict[str, tuple[ApprovalRule, ...]] = {}
                for line in plan.lines:
                    approvals_by_route[line.route_id] = tuple(
                        approval_rule(registry, rule_id, line.supplier_id)
                        for rule_id in line.approval_rule_ids
                    )
                represented = {
                    item.rule_id
                    for approvals in approvals_by_route.values()
                    for item in approvals
                }
                if represented != set(plan.unresolved_approval_ids):
                    raise ExplanationError(
                        f"approval recommendation {plan.plan_id} has inconsistent line approvals"
                    )
                add(
                    AlertCategory.APPROVAL_REQUIRED,
                    f"{scope}:approval:{plan.plan_id}:{'-'.join(sorted(represented))}",
                    _approval_alert(decision, plan, approvals_by_route),
                    decision=decision,
                    terminal=True,
                )
                approval_count += 1
            elif plan.disposition is PlanDisposition.DECISION_REQUIRED:
                decision_options.append(plan)
        if needs_human_resolution and decision_options:
            add(
                AlertCategory.DECISION_REQUIRED,
                f"{scope}:decision",
                _decision_alert(decision, decision_options),
                decision=decision,
                terminal=True,
            )
            decision_count = 1

        special = {
            AlertCategory.UNMET_DEMAND,
            AlertCategory.LATE_ARRIVAL,
            AlertCategory.ASSUMPTION,
            AlertCategory.APPROVAL_REQUIRED,
            AlertCategory.DECISION_REQUIRED,
            AlertCategory.SOLVER_UNPROVEN,
            AlertCategory.RUN_ACCOUNTING,
        }
        if decision.component_id in quarantine_components:
            # The source-scoped alert above carries the table/key/field and
            # remediation facts; do not dilute it with a generic duplicate.
            special.add(AlertCategory.DATA_QUALITY)
        if decision.deadline_lateness:
            if AlertCategory.LATE_ARRIVAL not in decision.alert_categories:
                raise ExplanationError(
                    f"requirement {decision.requirement_id} has deadline misses without LATE_ARRIVAL"
                )
            for lateness in decision.deadline_lateness:
                add(
                    AlertCategory.LATE_ARRIVAL,
                    f"{scope}:late:{lateness.due_date.isoformat()}",
                    _late_alert(decision, lateness.due_date),
                    decision=decision,
                )
        elif AlertCategory.LATE_ARRIVAL in decision.alert_categories:
            raise ExplanationError(
                f"requirement {decision.requirement_id} requests LATE_ARRIVAL without a deadline miss"
            )
        if (
            needs_human_resolution
            and AlertCategory.APPROVAL_REQUIRED in decision.alert_categories
            and approval_count == 0
        ):
            raise ExplanationError(
                f"requirement {decision.requirement_id} requests an approval alert without a proposal"
            )
        if (
            needs_human_resolution
            and AlertCategory.DECISION_REQUIRED in decision.alert_categories
            and decision_count == 0
        ):
            blocking = _contract_blocking_evidence(decision)
            remediation = (
                "supply authoritative evidence for rule IDs "
                f"[{_list(item.rule_id for item in blocking)}]"
                if blocking
                else "resolve rule IDs "
                f"[{_list(_evidence_rule_ids(decision))}]"
            )
            add(
                AlertCategory.DECISION_REQUIRED,
                f"{scope}:decision:terminal",
                (
                    f"Decision required for component {decision.component_id}, requirement "
                    f"{decision.requirement_id}: no autonomous alternative is authorized and "
                    f"residual quantity is {_number(decision.residual_gap)}. The agent placed no "
                    f"unsupported order. Applicable rule IDs "
                    f"[{_list(_evidence_rule_ids(decision))}]. Human action: {remediation} "
                    f"and rerun from a fresh snapshot."
                ),
                decision=decision,
                terminal=True,
            )
        if (
            AlertCategory.EVIDENCE_CONTRACT in decision.alert_categories
        ):
            special.add(AlertCategory.EVIDENCE_CONTRACT)
            if decision.selected_plan is not None or decision.residual_gap > ZERO:
                evidence_contract_decisions.append(decision)
        if AlertCategory.SOLVER_UNPROVEN in decision.alert_categories:
            add(
                AlertCategory.SOLVER_UNPROVEN,
                f"{scope}:solver",
                _solver_alert(decision),
                decision=decision,
                terminal=True,
            )
        for category in sorted(set(decision.alert_categories) - special, key=lambda item: item.value):
            if category not in _GENERIC_ACTIONS:
                raise ExplanationError(f"no deterministic alert template for {category.value}")
            add(
                category,
                f"{scope}:category:{category.value}",
                _generic_alert(decision, category),
                decision=decision,
                terminal=category in _TERMINAL_RENDERING_CATEGORIES,
            )

    unexplained = tuple(
        f"{decision.component_id}/{decision.requirement_id}"
        for decision in records
        if decision.residual_gap > ZERO
        and decision.requirement_id not in terminal_requirements
    )
    if unexplained:
        raise ExplanationError(
            "withheld or residual requirements lack a deterministic terminal "
            f"explanation: [{_list(unexplained)}]"
        )

    if evidence_contract_decisions:
        contract = records[0].evidence_contract.value if records else "none"
        affected_components = tuple(
            sorted({item.component_id for item in evidence_contract_decisions})
        )
        po_count = sum(
            len(item.selected_plan.lines)
            for item in evidence_contract_decisions
            if item.selected_plan is not None
        )
        evidence_listing: list[str] = []
        for decision in sorted(
            evidence_contract_decisions,
            key=lambda item: (item.component_id, item.requirement_id),
        ):
            rule_ids = {
                item.rule_id
                for item in decision.evidence
                if item.status is EvidenceStatus.UNKNOWN
            }
            rule_ids.update(
                item.rule_id for item in _contract_blocking_evidence(decision)
            )
            evidence_listing.append(
                f"{decision.component_id}/{decision.requirement_id} rule IDs "
                f"[{_list(rule_ids)}]"
            )
        listing = "; ".join(evidence_listing)
        component_count = len(affected_components)
        if contract == EvidenceContract.BENCHMARK.value:
            headline = (
                "Twelve-month supplier allocation history is unavailable; "
                f"{po_count} purchase order{'s' if po_count != 1 else ''} for "
                f"{component_count} component{'s' if component_count != 1 else ''} "
                "were created using the benchmark fallback."
            )
            action = (
                "load the missing supplier history and rerun before treating these "
                "orders as production-ready"
            )
        else:
            headline = (
                "Twelve-month supplier allocation history is unavailable, so procurement "
                f"remains blocked for {component_count} "
                f"component{'s' if component_count != 1 else ''}."
            )
            action = "load the missing supplier history and rerun the blocked requirements"
        add(
            AlertCategory.EVIDENCE_CONTRACT,
            "run:evidence-contract",
            f"{headline} Run-global {contract} evidence-contract trace: [{listing}]. The agent "
            "preserved every hard UNKNOWN disposition, withheld affected actions where the "
            "contract requires DECISION_REQUIRED, and did not infer zero history or supplier "
            "ineligibility. Component-specific DECISION_REQUIRED alerts state each terminal "
            f"impact. Human action: {action}.",
        )

    descriptions = tuple(item.description for item in rendered)
    if len(descriptions) != len(set(descriptions)):
        raise ExplanationError("rendered alerts contain duplicate descriptions")
    if any(
        item.category in AUDIT_ONLY_ALERT_CATEGORIES
        for item in rendered
    ):
        raise AssertionError("audit-only categories must not be rendered as operational alerts")
    if any(not validate_owned_alert(item) for item in rendered):
        raise AssertionError("rendered alert ownership marker failed validation")
    return tuple(sorted(rendered, key=lambda item: (item.category.value, item.key)))


__all__ = [
    "ALERT_MARKER_VERSION",
    "AUDIT_ONLY_ALERT_CATEGORIES",
    "ApprovalRule",
    "ExplanationError",
    "ParsedAlertMarker",
    "RenderedAlert",
    "approval_rule",
    "make_owned_alert",
    "parse_owned_alert",
    "render_alerts",
    "render_decision_rationale",
    "render_line_rationale",
    "render_purchase_order_rationale",
    "validate_owned_alert",
    "validate_stored_alert",
]
