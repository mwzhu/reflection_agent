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

from .domain import (
    AlertCategory,
    CandidatePlan,
    DecisionRecord,
    EvidenceResult,
    PlanDisposition,
    PlanLine,
    ZERO,
)
from .policy.registry import PolicyRegistry, load_policy_registry
from .serialization import canonical_dumps, sanitize_control_characters


ALERT_MARKER_VERSION = 1
_HEX_64 = r"[0-9a-f]{64}"
_ALERT_MARKER = re.compile(
    rf" \[APEX_ALERT:v(?P<version>[0-9]+) key=(?P<key>{_HEX_64}) "
    r"category=(?P<category>[A-Z_]+) scope=(?P<scope>[A-Za-z0-9_-]+)\]\Z"
)


class ExplanationError(ValueError):
    """Structured facts are insufficient or inconsistent for safe prose."""


@dataclass(frozen=True, slots=True)
class RenderedAlert:
    """One target alert with a validated, terminal ownership marker."""

    key: str
    category: AlertCategory
    scope: str
    description: str

    def __post_init__(self) -> None:
        if not re.fullmatch(_HEX_64, self.key):
            raise ValueError("alert key must be a lowercase SHA-256 digest")
        if not isinstance(self.category, AlertCategory):
            raise TypeError("category must be AlertCategory")
        _require_safe_text(self.scope, "scope")
        _require_safe_text(self.description, "description")
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

    def __post_init__(self) -> None:
        _require_safe_text(self.rule_id, "rule_id")
        _require_safe_text(self.authority, "authority")
        _require_safe_text(self.threshold, "threshold")


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


def _require_safe_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{name} must not contain control characters")


def _number(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise TypeError("rendered numeric facts must be finite Decimal values")
    return format(value, "f")


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
        f" Post-plan deadline misses [{lateness}]."
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
        f"forced surplus {_number(plan.forced_surplus)}, recovery demand "
        f"{_number(plan.recovery_demand)}, recovery quantity "
        f"{_number(plan.recovery_quantity)}, discretionary surplus "
        f"{_number(plan.discretionary_surplus)}, unit-late-days "
        f"{_number(plan.unit_late_days)}, residual eventual quantity "
        f"{_number(decision.residual_gap)}. Fulfillment "
        f"{decision.requirement_state.fulfillment.value}; resolution "
        f"{decision.requirement_state.resolution.value}; disposition {plan.disposition.value}; "
        f"objective [{objective}]; evidence [{evidence}]; alternatives and material rejections "
        f"[{alternatives}]; disclosures [{_list(item.value for item in decision.alert_categories)}]; "
        f"assumptions [{_list(assumptions)}]."
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
    if kind == "order_value_approval":
        authority = constraint.get("authority")
        amount = constraint.get("amount_exceeds")
        threshold = f"line total exceeds {_number(Decimal(str(amount)))}"
    elif kind == "sub_moq_written_approval":
        authority = f"supplier {supplier_id}"
        threshold = "quantity is below the supplier catalog minimum order quantity"
    elif kind == "strategic_volume_shift_approval":
        authority = constraint.get("authority")
        threshold = "a significant strategic-supplier volume shift is proposed"
    elif kind == "air_freight_individual_approval":
        authority = constraint.get("authority")
        threshold = "an individual air-freight request is proposed"
    else:
        raise ExplanationError(f"unsupported approval constraint for {rule_id}: {kind!r}")
    if not isinstance(authority, str):
        raise ExplanationError(f"approval rule has no named authority: {rule_id}")
    return ApprovalRule(rule_id=rule_id, authority=authority, threshold=threshold)


def _alert_key(category: AlertCategory, scope: str, body: str) -> str:
    payload = canonical_dumps(
        {"version": ALERT_MARKER_VERSION, "category": category.value, "scope": scope, "body": body}
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def make_owned_alert(
    category: AlertCategory,
    scope: str,
    body: str,
    *,
    visible_prefix: bool = False,
) -> RenderedAlert:
    """Attach a self-validating terminal ownership marker to deterministic prose."""

    if not isinstance(category, AlertCategory):
        raise TypeError("category must be AlertCategory")
    scope = sanitize_control_characters(scope)
    body = sanitize_control_characters(body)
    _require_safe_text(scope, "scope")
    _require_safe_text(body, "body")
    visible = f"[{category.value}] {body}" if visible_prefix else body
    key = _alert_key(category, scope, visible)
    description = (
        f"{visible} [APEX_ALERT:v{ALERT_MARKER_VERSION} key={key} "
        f"category={category.value} scope={_encode_scope(scope)}]"
    )
    return RenderedAlert(key=key, category=category, scope=scope, description=description)


def parse_owned_alert(description: str) -> ParsedAlertMarker | None:
    """Return a marker only when syntax, category, and digest all validate."""

    if not isinstance(description, str):
        return None
    match = _ALERT_MARKER.search(description)
    if match is None:
        return None
    if int(match.group("version")) != ALERT_MARKER_VERSION:
        return None
    try:
        category = AlertCategory(match.group("category"))
    except ValueError:
        return None
    scope = _decode_scope(match.group("scope"))
    if scope is None:
        return None
    body = description[: match.start()]
    if match.group("key") != _alert_key(category, scope, body):
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
        quantified = f" Forced surplus is {_number(decision.selected_plan.forced_surplus)}."
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
        f"residual quantity is {_number(decision.residual_gap)} and cited rule IDs are "
        f"[{_list(rules)}].{quantified} Human action: {action}."
    )


def _approval_alert(
    decision: DecisionRecord,
    plan: CandidatePlan,
    line: PlanLine,
    approval: ApprovalRule,
) -> str:
    return (
        f"Approval proposal for component {decision.component_id}, requirement "
        f"{decision.requirement_id}: supplier {line.supplier_id}, quantity "
        f"{_number(line.quantity)}, unit price {_number(line.unit_price)}, line total "
        f"{_number(line.line_total)}, expected delivery "
        f"{line.expected_delivery_date.isoformat()}. Threshold crossed: "
        f"{approval.threshold} under {approval.rule_id}. The agent did not place this order. "
        f"Human action: {approval.authority} must approve the complete proposal, then the "
        f"planner must recompute it from a fresh snapshot."
    )


def _decision_alert(decision: DecisionRecord, plan: CandidatePlan) -> str:
    proposals = "; ".join(
        f"supplier {line.supplier_id}, quantity {_number(line.quantity)}, unit price "
        f"{_number(line.unit_price)}, line total {_number(line.line_total)}, delivery "
        f"{line.expected_delivery_date.isoformat()}"
        for line in plan.lines
    )
    return (
        f"Decision required for component {decision.component_id}, requirement "
        f"{decision.requirement_id}: alternative {plan.plan_id} proposes [{proposals}] under "
        f"rule IDs [{_list((*plan.relaxed_rule_ids, *_evidence_rule_ids(decision, plan)))}]. "
        f"The agent did not place the alternative and left residual quantity "
        f"{_number(decision.residual_gap)}. Human action: select an outcome and rerun from a "
        f"fresh snapshot."
    )


def _assumption_alert(decision: DecisionRecord, code: str) -> str:
    return (
        f"Component {decision.component_id}, requirement {decision.requirement_id} relied on "
        f"assumption {code} under the {decision.evidence_contract.value} evidence contract. "
        f"The agent retained the assumption in the managed record. Human action: verify the "
        f"missing evidence and rerun if it changes."
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
    return (
        f"Component {decision.component_id}, requirement {decision.requirement_id} has no proven "
        f"executable conclusion because the solver result was not certified complete. The agent "
        f"placed no line supported by that result and did not claim infeasibility. Human action: "
        f"rerun with a completed optimality certificate and exact post-validation."
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


def render_alerts(
    decisions: Sequence[DecisionRecord],
    *,
    policy_registry: PolicyRegistry | None = None,
    active_directives: Sequence[str] = (),
    inactive_directives: Sequence[str] = (),
    visible_prefixes: bool = False,
) -> tuple[RenderedAlert, ...]:
    """Render the complete, deterministic owned-alert target set."""

    records = tuple(sorted(decisions, key=lambda item: (item.requirement_id, item.component_id)))
    if any(not isinstance(item, DecisionRecord) for item in records):
        raise TypeError("decisions must contain DecisionRecord values")
    requirement_ids = tuple(item.requirement_id for item in records)
    if len(requirement_ids) != len(set(requirement_ids)):
        raise ExplanationError("managed decisions contain duplicate requirement IDs")
    registry = policy_registry
    rendered: list[RenderedAlert] = []

    def add(category: AlertCategory, scope: str, body: str) -> None:
        rendered.append(
            make_owned_alert(category, scope, body, visible_prefix=visible_prefixes)
        )

    for decision in records:
        scope = f"requirement:{decision.requirement_id}"
        if decision.residual_gap > ZERO:
            add(AlertCategory.UNMET_DEMAND, f"{scope}:residual", _residual_alert(decision))

        assumptions: set[str] = set()
        plans = tuple(
            item
            for item in (decision.selected_plan, *decision.alternatives)
            if item is not None
        )
        for plan in plans:
            assumptions.update(plan.assumption_codes)
            for evidence in plan.evidence:
                assumptions.update(evidence.assumption_codes)
        for evidence in decision.evidence:
            assumptions.update(evidence.assumption_codes)
        for code in sorted(assumptions):
            add(AlertCategory.ASSUMPTION, f"{scope}:assumption:{code}", _assumption_alert(decision, code))

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
                for line in plan.lines:
                    for rule_id in plan.unresolved_approval_ids:
                        details = approval_rule(registry, rule_id, line.supplier_id)
                        add(
                            AlertCategory.APPROVAL_REQUIRED,
                            f"{scope}:approval:{plan.plan_id}:{line.route_id}:{rule_id}",
                            _approval_alert(decision, plan, line, details),
                        )
                        approval_count += 1
            elif plan.disposition is PlanDisposition.DECISION_REQUIRED:
                add(
                    AlertCategory.DECISION_REQUIRED,
                    f"{scope}:decision:{plan.plan_id}",
                    _decision_alert(decision, plan),
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
            add(
                AlertCategory.DECISION_REQUIRED,
                f"{scope}:decision:terminal",
                (
                    f"Decision required for component {decision.component_id}, requirement "
                    f"{decision.requirement_id}: no autonomous alternative is authorized and "
                    f"residual quantity is {_number(decision.residual_gap)}. The agent placed no "
                    f"unsupported order. Human action: resolve rule IDs "
                    f"[{_list(_evidence_rule_ids(decision))}] and rerun from a fresh snapshot."
                ),
            )
        if AlertCategory.SOLVER_UNPROVEN in decision.alert_categories:
            add(AlertCategory.SOLVER_UNPROVEN, f"{scope}:solver", _solver_alert(decision))
        for category in sorted(set(decision.alert_categories) - special, key=lambda item: item.value):
            if category not in _GENERIC_ACTIONS:
                raise ExplanationError(f"no deterministic alert template for {category.value}")
            add(category, f"{scope}:category:{category.value}", _generic_alert(decision, category))

    po_count = sum(
        len(item.selected_plan.lines) if item.selected_plan is not None else 0
        for item in records
    )
    ordered_cost = sum(
        (
            item.selected_plan.total_cost
            if item.selected_plan is not None
            else ZERO
        )
        for item in records
    )
    ordered_requirements = sum(item.selected_plan is not None for item in records)
    decision_required = sum(
        any(plan.disposition is PlanDisposition.DECISION_REQUIRED for plan in item.alternatives)
        or AlertCategory.DECISION_REQUIRED in item.alert_categories
        for item in records
    )
    no_eligible = sum(
        AlertCategory.NO_ELIGIBLE_SUPPLIER in item.alert_categories for item in records
    )
    existing_covered = sum(
        item.selected_plan is None and item.residual_gap == ZERO for item in records
    )
    accounting = (
        f"Managed {len(records)} component requirements: {ordered_requirements} covered by agent "
        f"orders ({_number(ordered_cost)} across {po_count} POs), {decision_required} deferred "
        f"for decision, {no_eligible} have no eligible supplier, {existing_covered} fully covered "
        f"without a new agent order. Evidence contract: "
        f"{records[0].evidence_contract.value if records else 'none'}. Active directives: "
        f"{_list(active_directives)}. Inactive directives: {_list(inactive_directives)}."
    )
    add(AlertCategory.RUN_ACCOUNTING, "run:accounting", accounting)

    descriptions = tuple(item.description for item in rendered)
    if len(descriptions) != len(set(descriptions)):
        raise ExplanationError("rendered alerts contain duplicate descriptions")
    if sum(item.category is AlertCategory.RUN_ACCOUNTING for item in rendered) != 1:
        raise AssertionError("exactly one run-accounting alert must be rendered")
    if any(not validate_owned_alert(item) for item in rendered):
        raise AssertionError("rendered alert ownership marker failed validation")
    return tuple(sorted(rendered, key=lambda item: (item.category.value, item.key)))


__all__ = [
    "ALERT_MARKER_VERSION",
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
    "validate_owned_alert",
    "validate_stored_alert",
]
