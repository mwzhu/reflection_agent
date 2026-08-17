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


def render_purchase_order_rationale(decision: DecisionRecord, line: PlanLine) -> str:
    """Render the concise, human-facing explanation stored on a purchase order.

    The complete decision, evidence, comparator trace, and rejected alternatives
    are persisted separately in the decision-audit table.  This field is kept
    deliberately short because it is an operational purchase-order attribute,
    not an audit transport.
    """

    if decision.selected_plan is None or line not in decision.selected_plan.lines:
        raise ExplanationError("line must belong to the decision's selected plan")
    plan = decision.selected_plan
    assumptions = set(plan.assumption_codes)
    for evidence in (*decision.evidence, *plan.evidence):
        assumptions.update(evidence.assumption_codes)
    assumption_text = (
        f" Assumptions: {_list(assumptions)}."
        if assumptions
        else ""
    )
    return sanitize_control_characters(
        f"Order {_number(line.quantity)} units of {line.component_id} from "
        f"{line.supplier_id} at {_number(line.unit_price)} per unit "
        f"({_number(line.line_total)} total), expected {line.expected_delivery_date.isoformat()}. "
        f"Initial shortage: {_number(decision.initial_eventual_gap)}; plan residual: "
        f"{_number(decision.residual_gap)}.{assumption_text}"
    )


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

    if category is AlertCategory.EVIDENCE_CONTRACT and scope == "run:evidence-contract":
        components = sorted(set(re.findall(r"CMP-[0-9]+", body)))
        affected = ", ".join(components) if components else "one or more components"
        return (
            f"Required policy evidence is incomplete for {affected}. "
            "Verify the cited source records and rerun before releasing or revising "
            "the affected procurement actions."
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

    first, separator, _remaining = body.partition(". ")
    issue = first + ("." if separator else "")
    action = ""
    if "Human action:" in body:
        action_tail = body.rsplit("Human action:", 1)[1].strip()
        action_text, action_separator, _after_action = action_tail.partition(". ")
        action = f" Action: {action_text}{'.' if action_separator else ''}"
    return sanitize_control_characters(issue + action)


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
    visible = f"[{category.value}] {concise}" if visible_prefix else concise
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
    if decision.selected_plan is not None and category is AlertCategory.FORCED_SURPLUS:
        plan = decision.selected_plan
        lines = "; ".join(
            f"supplier {line.supplier_id}, quantity {_number(line.quantity)}, "
            f"unit price {_number(line.unit_price)}, line total {_number(line.line_total)}"
            for line in plan.lines
        )
        return (
            f"Component {decision.component_id}, requirement {decision.requirement_id}: {issue}. "
            f"Executed action: [{lines}], total cost {_number(plan.total_cost)}, forced "
            f"surplus {_number(plan.forced_surplus)} against net requirement "
            f"{_number(plan.net_requirement)}. The agent preserved existing commitments and "
            f"committed only this minimum compliant outcome under rule IDs [{_list(rules)}]; "
            "no mutually exclusive sub-MOQ approval request remains live. A sub-MOQ "
            "alternative could be considered only after a future cancellation contract "
            "authorizes changing the commitment and a fresh run revalidates demand, inbound, "
            f"policy, quantity, supplier, price, and dates. Human action: {action}."
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
        f"Component {decision.component_id}, requirement {decision.requirement_id}: {issue}. "
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


def _decision_alert(decision: DecisionRecord, plan: CandidatePlan) -> str:
    proposals = "; ".join(
        f"supplier {line.supplier_id}, quantity {_number(line.quantity)}, unit price "
        f"{_number(line.unit_price)}, line total {_number(line.line_total)}, delivery "
        f"{line.expected_delivery_date.isoformat()}"
        for line in plan.lines
    )
    blocking = _contract_blocking_evidence(decision, plan)
    if blocking and decision.evidence_contract is EvidenceContract.PRODUCTION:
        missing = "; ".join(
            f"{item.rule_id}: {item.summary} "
            f"(contract disposition {item.contract_disposition.value})"
            for item in blocking
            if item.contract_disposition is not None
        )
        return (
            f"Decision required for component {decision.component_id}, requirement "
            f"{decision.requirement_id}: authoritative production evidence is unavailable "
            f"[{missing}]. A non-executable diagnostic proposes [{proposals}], but the agent "
            f"placed no line and did not classify any supplier as ineligible from the absent "
            f"facts. Residual quantity is {_number(decision.residual_gap)}. Applicable rule IDs "
            f"[{_list(item.rule_id for item in blocking)}]. Human action: "
            "supply the cited rolling-window or other authoritative contract evidence and "
            "rerun from a fresh snapshot."
        )
    rule_ids = (*plan.relaxed_rule_ids, *_evidence_rule_ids(decision, plan))
    return (
        f"Decision required for component {decision.component_id}, requirement "
        f"{decision.requirement_id}: alternative {plan.plan_id} proposes [{proposals}]. "
        f"The agent did not place the alternative and left residual quantity "
        f"{_number(decision.residual_gap)}. Applicable rule IDs [{_list(rule_ids)}]. "
        f"Human action: select an outcome and rerun from a "
        f"fresh snapshot."
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
    return (
        f"Component {decision.component_id}, requirement {decision.requirement_id} misses "
        f"the {lateness.due_date.isoformat()} material deadline by "
        f"{_number(lateness.late_quantity)} units, representing "
        f"{_number(lateness.unit_late_days)} unit-late-days; "
        f"{_number(lateness.unresolved_quantity)} units have no eventual assigned receipt. "
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
    evidence_contract_requirements: list[tuple[str, str, tuple[str, ...]]] = []
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
        for plan in decision.alternatives:
            if plan.disposition is PlanDisposition.RECOMMEND_APPROVAL:
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
                add(
                    AlertCategory.DECISION_REQUIRED,
                    f"{scope}:decision:{plan.plan_id}",
                    _decision_alert(decision, plan),
                    decision=decision,
                    terminal=True,
                )
                decision_count += 1

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
        if AlertCategory.APPROVAL_REQUIRED in decision.alert_categories and approval_count == 0:
            raise ExplanationError(
                f"requirement {decision.requirement_id} requests an approval alert without a proposal"
            )
        if AlertCategory.DECISION_REQUIRED in decision.alert_categories and decision_count == 0:
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
            blocking = _contract_blocking_evidence(decision)
            contract_rules = tuple(
                sorted(
                    {
                        item.rule_id
                        for item in (*decision.evidence,)
                        if item.status is EvidenceStatus.UNKNOWN
                    }
                    | {item.rule_id for item in blocking}
                )
            )
            evidence_contract_requirements.append(
                (decision.component_id, decision.requirement_id, contract_rules)
            )
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

    if evidence_contract_requirements:
        contract = records[0].evidence_contract.value if records else "none"
        listing = "; ".join(
            f"{component_id}/{requirement_id} rule IDs [{_list(rule_ids)}]"
            for component_id, requirement_id, rule_ids in sorted(
                evidence_contract_requirements
            )
        )
        add(
            AlertCategory.EVIDENCE_CONTRACT,
            "run:evidence-contract",
            f"Run-global {contract} evidence-contract trace: [{listing}]. The agent "
            "preserved every hard UNKNOWN disposition, withheld affected actions where the "
            "contract requires DECISION_REQUIRED, and did not infer zero history or supplier "
            "ineligibility. Component-specific DECISION_REQUIRED alerts state each terminal "
            "impact. Human action: provide the cited authoritative evidence and rerun from a "
            "fresh snapshot.",
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
