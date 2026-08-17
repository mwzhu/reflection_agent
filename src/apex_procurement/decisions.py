"""Action identity, managed-record reconstruction, and atomic SQLite commits."""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from pathlib import Path
import re
import sqlite3
from typing import Callable, Mapping, Sequence

from .config import EvidenceContract
from .domain import (
    AlertCategory,
    CandidateRoute,
    CommitResult,
    DecisionRecord,
    DemandBucket,
    ExistingPurchaseOrder,
    InboundSupply,
    PlanLine,
    RouteInputIssue,
    ScenarioSnapshot,
    ValidationResult,
)
from .explanations import (
    ExplanationError,
    RenderedAlert,
    parse_owned_alert,
    render_alerts,
    render_decision_rationale,
    render_legacy_v1_decision_rationale,
    render_legacy_v1_line_rationale,
    render_line_rationale,
    validate_stored_alert,
)
from .policy.registry import PolicyRegistry
from .repository import (
    SQLiteRepository,
    ScenarioLoadError,
    ScenarioPathError,
    resolve_scenario_path,
)
from .serialization import canonical_dumps, canonical_loads


PO_MARKER_VERSION = 4
_SUPPORTED_PO_MARKER_VERSIONS = frozenset({1, 2, 3, PO_MARKER_VERSION})
_HEX_64 = r"[0-9a-f]{64}"
_TOKEN = r"[A-Za-z0-9_-]+"
_LEGACY_PO_MARKER = re.compile(
    rf"\A\[APEX_AGENT:v(?P<version>[0-9]+) action=(?P<action>{_HEX_64}) "
    rf"demand=(?P<demand>{_HEX_64}) route=(?P<route>{_TOKEN}) "
    rf"policy=(?P<policy>{_TOKEN}) line=(?P<line>[0-9]+) "
    rf"record=(?P<record>{_TOKEN})\]"
)
_COMPACT_PO_MARKER = re.compile(
    rf"\A\[APEX_AGENT:v(?P<version>[0-9]+) action=(?P<action>{_HEX_64}) "
    rf"demand=(?P<demand>{_HEX_64}) source=(?P<source>{_HEX_64}) "
    rf"contract=(?P<contract>{_TOKEN}) requirement=(?P<requirement>{_TOKEN}) "
    rf"route=(?P<route>{_TOKEN}) policy=(?P<policy>{_TOKEN}) "
    rf"line=(?P<line>[0-9]+)/(?P<line_count>[1-9][0-9]*) "
    rf"group=(?P<group>{_HEX_64}) fields=(?P<fields>{_HEX_64})\]"
)


class DecisionError(RuntimeError):
    """Base class for deterministic decision-output failures."""


class OwnershipMarkerError(DecisionError):
    """A row that purports to be managed has an invalid ownership marker."""


class ActionCollisionError(DecisionError):
    """A short or full action key maps to different business fields."""


class ConcurrentModificationError(DecisionError):
    """The scenario changed between snapshot loading and write-lock acquisition."""


class CommitPostconditionError(DecisionError):
    """The database did not satisfy the verified target state after writes."""


class CommitFailure(DecisionError):
    """SQLite could not complete the atomic decision transaction."""


class CommitStep(str, Enum):
    DIGEST_RECHECKED = "digest_rechecked"
    PURCHASE_ORDERS_INSERTED = "purchase_orders_inserted"
    ALERTS_RECONCILED = "alerts_reconciled"
    POSTCONDITIONS_CHECKED = "postconditions_checked"


@dataclass(frozen=True, slots=True)
class PurchaseOrderOutput:
    action_key: str
    demand_fingerprint: str
    source_fingerprint: str | None
    evidence_contract: EvidenceContract
    policy_pack_version: str
    route_id: str
    line_index: int
    po_number: str
    component_id: str
    supplier_id: str
    quantity: Decimal
    unit_price: Decimal
    order_date: date
    expected_delivery_date: date
    rationale: str
    decision: DecisionRecord

    def __post_init__(self) -> None:
        for digest, name in (
            (self.action_key, "action_key"),
            (self.demand_fingerprint, "demand_fingerprint"),
        ):
            if not re.fullmatch(_HEX_64, digest):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        if self.source_fingerprint is not None and not re.fullmatch(
            _HEX_64, self.source_fingerprint
        ):
            raise ValueError("source_fingerprint must be a lowercase SHA-256 digest or None")
        if not isinstance(self.evidence_contract, EvidenceContract):
            raise TypeError("evidence_contract must be EvidenceContract")
        if self.po_number != po_number_for_action(self.action_key):
            raise ValueError("po_number does not match the action key")
        if not isinstance(self.line_index, int) or isinstance(self.line_index, bool):
            raise TypeError("line_index must be int")
        if self.line_index < 0:
            raise ValueError("line_index must be nonnegative")

    @property
    def business_fields(self) -> tuple[object, ...]:
        return (
            self.po_number,
            self.component_id,
            self.supplier_id,
            self.quantity,
            self.unit_price,
            self.order_date,
            self.expected_delivery_date,
            self.rationale,
        )


@dataclass(frozen=True, slots=True)
class ParsedOwnedPurchaseOrder:
    action_key: str
    demand_fingerprint: str
    source_fingerprint: str | None
    evidence_contract: EvidenceContract | None
    policy_pack_version: str
    route_id: str
    line_index: int
    line_count: int
    requirement_id: str
    group_digest: str
    field_digest: str
    decision: DecisionRecord | None
    marker_version: int


@dataclass(frozen=True, slots=True)
class DecisionOutputs:
    decisions: tuple[DecisionRecord, ...]
    purchase_orders: tuple[PurchaseOrderOutput, ...]
    alerts: tuple[RenderedAlert, ...]


def _canonical_decimal(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise TypeError("action identity requires finite Decimal values")
    sign, digits_tuple, exponent = value.as_tuple()
    digits = list(digits_tuple)
    if not digits or all(digit == 0 for digit in digits):
        return "0"
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    return f"{'-' if sign else '+'}{''.join(str(item) for item in digits)}e{exponent}"


def _encoded(text: str) -> str:
    if not isinstance(text, str) or not text or any(ord(item) < 32 for item in text):
        raise ValueError("marker values must be non-empty control-free text")
    return urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _decoded(token: str, name: str) -> str:
    try:
        padding = "=" * (-len(token) % 4)
        raw = urlsafe_b64decode((token + padding).encode("ascii"))
        value = raw.decode("utf-8")
    except (UnicodeError, ValueError) as error:
        raise OwnershipMarkerError(f"invalid {name} encoding in purchase-order marker") from error
    if _encoded(value) != token:
        raise OwnershipMarkerError(f"non-canonical {name} encoding in purchase-order marker")
    return value


def demand_fingerprint(decision: DecisionRecord) -> str:
    """Hash canonical demand and netting facts, excluding managed output rows."""

    if not isinstance(decision, DecisionRecord):
        raise TypeError("decision must be DecisionRecord")
    return demand_fingerprint_from_facts(
        decision.demand_buckets,
        decision.supply_ledger.on_hand,
        decision.supply_ledger.committed_inbound,
    )


def demand_fingerprint_from_facts(
    demand_buckets: Sequence[DemandBucket],
    on_hand: Decimal,
    committed_inbound: Sequence[InboundSupply],
) -> str:
    """Hash current demand/netting facts without requiring a DecisionRecord."""

    demand = sorted(
        (
            contribution.order_id,
            bucket.due_date.isoformat(),
            _canonical_decimal(contribution.quantity),
        )
        for bucket in demand_buckets
        for contribution in bucket.contributions
    )
    inbound = sorted(
        (
            item.po_number,
            item.component_id,
            item.supplier_id,
            _canonical_decimal(item.quantity),
            item.expected_delivery_date.isoformat(),
            item.order_date.isoformat() if item.order_date is not None else None,
            _canonical_decimal(item.unit_price) if item.unit_price is not None else None,
        )
        for item in committed_inbound
        if not item.agent_owned
    )
    payload = canonical_dumps(
        {
            "version": 1,
            "demand": demand,
            "on_hand": _canonical_decimal(on_hand),
            "counted_external_inbound": inbound,
        }
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def component_source_fingerprint(
    snapshot: ScenarioSnapshot,
    component_id: str,
    policy_pack_version: str,
    evidence_contract: EvidenceContract,
    *,
    policy_concepts_version: str = "",
    candidate_routes: Sequence[CandidateRoute] = (),
    candidate_rejections: Sequence[object] = (),
    candidate_alerts: Sequence[object] = (),
) -> str:
    """Bind ownership to every source fact that can change a component choice.

    Managed output rows are deliberately excluded.  Existing external orders
    for relevant suppliers remain included because rolling history, capacity,
    and physical inbound can change route eligibility or ranking.
    """

    if not isinstance(snapshot, ScenarioSnapshot):
        raise TypeError("snapshot must be ScenarioSnapshot")
    if not isinstance(component_id, str) or not component_id:
        raise ValueError("component_id must be non-empty text")
    if not isinstance(policy_pack_version, str) or not policy_pack_version:
        raise ValueError("policy_pack_version must be non-empty text")
    if not isinstance(evidence_contract, EvidenceContract):
        raise TypeError("evidence_contract must be EvidenceContract")
    if not isinstance(policy_concepts_version, str):
        raise TypeError("policy_concepts_version must be str")
    route_facts = tuple(
        item for item in candidate_routes if item.component_id == component_id
    )
    rejection_facts = tuple(
        item
        for item in candidate_rejections
        if getattr(item, "component_id", None) == component_id
    )
    alert_facts = tuple(
        item
        for item in candidate_alerts
        if getattr(item, "component_id", None) in {None, component_id}
    )
    components = tuple(
        item for item in snapshot.components if item.component_id == component_id
    )
    if len(components) != 1:
        raise DecisionError(
            f"component source fingerprint requires exactly one {component_id!r} row"
        )
    bom_lines = tuple(
        item for item in snapshot.bom_lines if item.component_id == component_id
    )
    product_ids = {item.product_id for item in bom_lines}
    catalog_lines = tuple(
        item for item in snapshot.catalog_lines if item.component_id == component_id
    )
    issue_suppliers = {
        issue.supplier_id
        for issue in snapshot.route_input_issues
        if component_id in issue.affected_component_ids
    }
    supplier_ids = {
        item.supplier_id for item in catalog_lines
    } | issue_suppliers
    external_orders = tuple(
        order
        for order in snapshot.purchase_orders
        if parse_owned_purchase_order(order) is None
        and (
            order.component_id == component_id
            or order.supplier_id in supplier_ids
        )
    )
    payload = canonical_dumps(
        {
            "version": 1,
            "policy_pack_version": policy_pack_version,
            "policy_concepts_version": policy_concepts_version,
            "evidence_contract": evidence_contract,
            "configuration": snapshot.configuration,
            "component": components[0],
            "products": tuple(
                item for item in snapshot.products if item.product_id in product_ids
            ),
            "bom_lines": bom_lines,
            "production_orders": tuple(
                item
                for item in snapshot.production_orders
                if item.product_id in product_ids
            ),
            "inventory": tuple(
                item for item in snapshot.inventory if item.component_id == component_id
            ),
            "suppliers": tuple(
                item for item in snapshot.suppliers if item.supplier_id in supplier_ids
            ),
            "catalog_lines": catalog_lines,
            "external_purchase_orders": external_orders,
            "route_input_issues": tuple(
                issue
                for issue in snapshot.route_input_issues
                if component_id in issue.affected_component_ids
            ),
            "candidate_routes": route_facts,
            "candidate_rejections": rejection_facts,
            "candidate_alerts": alert_facts,
        }
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def action_key(
    demand_digest: str,
    component_id: str,
    supplier_id: str,
    route_id: str,
    planning_date: date,
    policy_pack_version: str,
) -> str:
    """Hash the complete action-identity tuple from section 10.3."""

    if not re.fullmatch(_HEX_64, demand_digest):
        raise ValueError("demand_digest must be a lowercase SHA-256 digest")
    if not isinstance(planning_date, date):
        raise TypeError("planning_date must be date")
    for value, name in (
        (component_id, "component_id"),
        (supplier_id, "supplier_id"),
        (route_id, "route_id"),
        (policy_pack_version, "policy_pack_version"),
    ):
        if not isinstance(value, str) or not value or any(ord(item) < 32 for item in value):
            raise ValueError(f"{name} must be non-empty control-free text")
    payload = canonical_dumps(
        {
            "version": 1,
            "demand_fingerprint": demand_digest,
            "component": component_id,
            "supplier": supplier_id,
            "route": route_id,
            "planning_date": planning_date,
            "policy_pack_version": policy_pack_version,
        }
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def po_number_for_action(key: str) -> str:
    if not re.fullmatch(_HEX_64, key):
        raise ValueError("action key must be a lowercase SHA-256 digest")
    return f"APX-{key[:8]}"


def _normalized_decision(decision: DecisionRecord) -> DecisionRecord:
    if _is_legacy_v1_decision(decision):
        return decision
    return replace(decision, rationale=render_decision_rationale(decision))


def _legacy_v1_record(decision: DecisionRecord) -> str:
    """Serialize exactly the DecisionRecord schema embedded by marker v1."""

    primitive = canonical_loads(canonical_dumps(decision), dict)
    primitive.pop("comparator_facts", None)
    primitive.pop("material_rejections", None)
    primitive.pop("source_fingerprint", None)
    primitive.pop("deadline_lateness", None)
    ledger = primitive.get("supply_ledger")
    if isinstance(ledger, dict):
        positions = ledger.get("deadline_positions")
        if isinstance(positions, list):
            for position in positions:
                if isinstance(position, dict):
                    position.pop("committed_late_quantity", None)
                    position.pop("committed_unit_late_days", None)
    plans = [primitive.get("selected_plan")]
    alternatives = primitive.get("alternatives")
    if isinstance(alternatives, list):
        plans.extend(alternatives)
    for plan in plans:
        if isinstance(plan, dict):
            plan.pop("recovery_demand", None)
            plan.pop("recovery_quantity", None)
            lines = plan.get("lines")
            if isinstance(lines, list):
                for line in lines:
                    if isinstance(line, dict):
                        line.pop("approval_rule_ids", None)
    return canonical_dumps(primitive)


def _legacy_v2_record(decision: DecisionRecord) -> str:
    """Serialize the pre-R03 record schema without line approval fields."""

    primitive = canonical_loads(canonical_dumps(decision), dict)
    primitive.pop("comparator_facts", None)
    primitive.pop("material_rejections", None)
    primitive.pop("source_fingerprint", None)
    plans = [primitive.get("selected_plan")]
    alternatives = primitive.get("alternatives")
    if isinstance(alternatives, list):
        plans.extend(alternatives)
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        lines = plan.get("lines")
        if not isinstance(lines, list):
            continue
        for line in lines:
            if isinstance(line, dict):
                line.pop("approval_rule_ids", None)
    return canonical_dumps(primitive)


def _legacy_v3_record(decision: DecisionRecord) -> str:
    """Serialize the pre-R07 record schema embedded by marker v3."""

    primitive = canonical_loads(canonical_dumps(decision), dict)
    primitive.pop("comparator_facts", None)
    primitive.pop("material_rejections", None)
    primitive.pop("source_fingerprint", None)
    return canonical_dumps(primitive)


def _load_legacy_decision(record_json: str) -> DecisionRecord:
    """Upgrade pre-R03 aggregate approval IDs to the line-level contract."""

    primitive = canonical_loads(record_json, dict)
    plans = [primitive.get("selected_plan")]
    alternatives = primitive.get("alternatives")
    if isinstance(alternatives, list):
        plans.extend(alternatives)
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        approval_ids = plan.get("unresolved_approval_ids", [])
        if not isinstance(approval_ids, list):
            approval_ids = []
        lines = plan.get("lines")
        if not isinstance(lines, list):
            continue
        for line in lines:
            if isinstance(line, dict):
                line.setdefault("approval_rule_ids", approval_ids)
    return canonical_loads(
        canonical_dumps(primitive),
        DecisionRecord,
        allow_missing_defaults=True,
    )


def _is_legacy_v1_decision(decision: DecisionRecord) -> bool:
    plans = tuple(
        item
        for item in (decision.selected_plan, *decision.alternatives)
        if item is not None
    )
    return (
        not decision.deadline_lateness
        and all(
            position.committed_late_quantity == 0
            and position.committed_unit_late_days == 0
            for position in decision.supply_ledger.deadline_positions
        )
        and all(
            plan.recovery_demand == 0 and plan.recovery_quantity == 0
            for plan in plans
        )
        and decision.rationale == render_legacy_v1_decision_rationale(decision)
    )


def _po_marker(
    *,
    key: str,
    demand_digest: str,
    route_id: str,
    policy_pack_version: str,
    line_index: int,
    decision_token: str,
    version: int = PO_MARKER_VERSION,
) -> str:
    return (
        f"[APEX_AGENT:v{version} action={key} demand={demand_digest} "
        f"route={_encoded(route_id)} policy={_encoded(policy_pack_version)} "
        f"line={line_index} record={decision_token}]"
    )


def _compact_field_digest(
    *,
    action_digest: str,
    demand_digest: str,
    source_digest: str,
    evidence_contract: EvidenceContract,
    requirement_id: str,
    route_id: str,
    policy_pack_version: str,
    line_index: int,
    line_count: int,
    po_number: str,
    component_id: str,
    supplier_id: str,
    quantity: Decimal,
    unit_price: Decimal,
    order_date: date,
    expected_delivery_date: date,
    rationale_body: str,
) -> str:
    payload = canonical_dumps(
        {
            "version": 1,
            "action": action_digest,
            "demand": demand_digest,
            "source": source_digest,
            "evidence_contract": evidence_contract,
            "requirement": requirement_id,
            "route": route_id,
            "policy": policy_pack_version,
            "line": line_index,
            "line_count": line_count,
            "po_number": po_number,
            "component": component_id,
            "supplier": supplier_id,
            "quantity": _canonical_decimal(quantity),
            "unit_price": _canonical_decimal(unit_price),
            "order_date": order_date,
            "expected_delivery_date": expected_delivery_date,
            "rationale_body": rationale_body,
        }
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _compact_group_digest(
    *,
    requirement_id: str,
    demand_digest: str,
    source_digest: str,
    evidence_contract: EvidenceContract,
    policy_pack_version: str,
    line_count: int,
    lines: Sequence[tuple[int, str, str, str]],
) -> str:
    payload = canonical_dumps(
        {
            "version": 1,
            "requirement": requirement_id,
            "demand": demand_digest,
            "source": source_digest,
            "evidence_contract": evidence_contract,
            "policy": policy_pack_version,
            "line_count": line_count,
            "lines": tuple(sorted(lines)),
        }
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _compact_po_marker(
    *,
    key: str,
    demand_digest: str,
    source_digest: str,
    evidence_contract: EvidenceContract,
    requirement_id: str,
    route_id: str,
    policy_pack_version: str,
    line_index: int,
    line_count: int,
    group_digest: str,
    field_digest: str,
) -> str:
    return (
        f"[APEX_AGENT:v{PO_MARKER_VERSION} action={key} demand={demand_digest} "
        f"source={source_digest} contract={_encoded(evidence_contract.value)} "
        f"requirement={_encoded(requirement_id)} route={_encoded(route_id)} "
        f"policy={_encoded(policy_pack_version)} line={line_index}/{line_count} "
        f"group={group_digest} fields={field_digest}]"
    )


def _legacy_purchase_order_output(
    decision: DecisionRecord,
    line: PlanLine,
    line_index: int,
    policy_pack_version: str,
) -> PurchaseOrderOutput:
    demand_digest = demand_fingerprint(decision)
    key = action_key(
        demand_digest,
        line.component_id,
        line.supplier_id,
        line.route_id,
        line.order_date,
        policy_pack_version,
    )
    legacy_v1 = _is_legacy_v1_decision(decision)
    marker_version = 1 if legacy_v1 else PO_MARKER_VERSION
    record_token = _encoded(
        _legacy_v1_record(decision) if legacy_v1 else canonical_dumps(decision)
    )
    marker = _po_marker(
        key=key,
        demand_digest=demand_digest,
        route_id=line.route_id,
        policy_pack_version=policy_pack_version,
        line_index=line_index,
        decision_token=record_token,
        version=marker_version,
    )
    line_rationale = (
        render_legacy_v1_line_rationale(decision, line)
        if legacy_v1
        else render_line_rationale(decision, line)
    )
    rationale = f"{marker} {line_rationale}"
    return PurchaseOrderOutput(
        action_key=key,
        demand_fingerprint=demand_digest,
        source_fingerprint=None,
        evidence_contract=decision.evidence_contract,
        policy_pack_version=policy_pack_version,
        route_id=line.route_id,
        line_index=line_index,
        po_number=po_number_for_action(key),
        component_id=line.component_id,
        supplier_id=line.supplier_id,
        quantity=line.quantity,
        unit_price=line.unit_price,
        order_date=line.order_date,
        expected_delivery_date=line.expected_delivery_date,
        rationale=rationale,
        decision=decision,
    )


def _purchase_order_outputs(
    decision: DecisionRecord,
    policy_pack_version: str,
) -> tuple[PurchaseOrderOutput, ...]:
    if decision.selected_plan is None:
        return ()
    if _is_legacy_v1_decision(decision):
        return tuple(
            _legacy_purchase_order_output(
                decision,
                line,
                line_index,
                policy_pack_version,
            )
            for line_index, line in enumerate(decision.selected_plan.lines)
        )

    demand_digest = demand_fingerprint(decision)
    source_digest = decision.source_fingerprint
    if source_digest is None:
        raise DecisionError(
            f"selected decision {decision.requirement_id} lacks a component source fingerprint"
        )
    lines = decision.selected_plan.lines
    line_count = len(lines)
    prepared: list[tuple[PlanLine, int, str, str, str, str]] = []
    for line_index, line in enumerate(lines):
        key = action_key(
            demand_digest,
            line.component_id,
            line.supplier_id,
            line.route_id,
            line.order_date,
            policy_pack_version,
        )
        number = po_number_for_action(key)
        rationale_body = render_line_rationale(decision, line)
        field_digest = _compact_field_digest(
            action_digest=key,
            demand_digest=demand_digest,
            source_digest=source_digest,
            evidence_contract=decision.evidence_contract,
            requirement_id=decision.requirement_id,
            route_id=line.route_id,
            policy_pack_version=policy_pack_version,
            line_index=line_index,
            line_count=line_count,
            po_number=number,
            component_id=line.component_id,
            supplier_id=line.supplier_id,
            quantity=line.quantity,
            unit_price=line.unit_price,
            order_date=line.order_date,
            expected_delivery_date=line.expected_delivery_date,
            rationale_body=rationale_body,
        )
        prepared.append(
            (line, line_index, key, number, rationale_body, field_digest)
        )
    group_digest = _compact_group_digest(
        requirement_id=decision.requirement_id,
        demand_digest=demand_digest,
        source_digest=source_digest,
        evidence_contract=decision.evidence_contract,
        policy_pack_version=policy_pack_version,
        line_count=line_count,
        lines=tuple(
            (line_index, key, line.route_id, field_digest)
            for line, line_index, key, _number, _body, field_digest in prepared
        ),
    )
    return tuple(
        PurchaseOrderOutput(
            action_key=key,
            demand_fingerprint=demand_digest,
            source_fingerprint=source_digest,
            evidence_contract=decision.evidence_contract,
            policy_pack_version=policy_pack_version,
            route_id=line.route_id,
            line_index=line_index,
            po_number=number,
            component_id=line.component_id,
            supplier_id=line.supplier_id,
            quantity=line.quantity,
            unit_price=line.unit_price,
            order_date=line.order_date,
            expected_delivery_date=line.expected_delivery_date,
            rationale=(
                _compact_po_marker(
                    key=key,
                    demand_digest=demand_digest,
                    source_digest=source_digest,
                    evidence_contract=decision.evidence_contract,
                    requirement_id=decision.requirement_id,
                    route_id=line.route_id,
                    policy_pack_version=policy_pack_version,
                    line_index=line_index,
                    line_count=line_count,
                    group_digest=group_digest,
                    field_digest=field_digest,
                )
                + " "
                + rationale_body
            ),
            decision=decision,
        )
        for line, line_index, key, number, rationale_body, field_digest in prepared
    )


def build_decision_outputs(
    decisions: Sequence[DecisionRecord],
    policy_pack_version: str,
    *,
    policy_registry: PolicyRegistry | None = None,
    active_directives: Sequence[str] = (),
    inactive_directives: Sequence[str] = (),
    visible_alert_prefixes: bool = False,
    route_input_issues: Sequence[RouteInputIssue] = (),
) -> DecisionOutputs:
    """Normalize records and construct the exact PO and owned-alert target rows."""

    if not isinstance(policy_pack_version, str) or not policy_pack_version:
        raise ValueError("policy_pack_version must be non-empty text")
    records = tuple(_normalized_decision(item) for item in decisions)
    records = tuple(sorted(records, key=lambda item: (item.requirement_id, item.component_id)))
    if len({item.requirement_id for item in records}) != len(records):
        raise DecisionError("managed decisions contain duplicate requirement IDs")
    if len({item.evidence_contract for item in records}) > 1:
        raise DecisionError("all managed decisions in one run must use one evidence contract")
    orders: list[PurchaseOrderOutput] = []
    for decision in records:
        orders.extend(_purchase_order_outputs(decision, policy_pack_version))
    by_key: dict[str, PurchaseOrderOutput] = {}
    by_number: dict[str, PurchaseOrderOutput] = {}
    for order in orders:
        if order.action_key in by_key:
            raise ActionCollisionError(f"duplicate target action key: {order.action_key}")
        existing = by_number.get(order.po_number)
        if existing is not None and existing.action_key != order.action_key:
            raise ActionCollisionError(
                f"short action-key collision for purchase order {order.po_number}"
            )
        by_key[order.action_key] = order
        by_number[order.po_number] = order
    alerts = render_alerts(
        records,
        policy_registry=policy_registry,
        active_directives=active_directives,
        inactive_directives=inactive_directives,
        visible_prefixes=visible_alert_prefixes,
        route_input_issues=route_input_issues,
    )
    return DecisionOutputs(
        decisions=records,
        purchase_orders=tuple(sorted(orders, key=lambda item: item.po_number)),
        alerts=alerts,
    )


def parse_owned_purchase_order(order: ExistingPurchaseOrder) -> ParsedOwnedPurchaseOrder | None:
    """Validate and decode an owned PO; reject any malformed ownership claim."""

    if not isinstance(order, ExistingPurchaseOrder):
        raise TypeError("order must be ExistingPurchaseOrder")
    rationale = order.rationale or ""
    claims_ownership = order.po_number.startswith("APX-") or "[APEX_AGENT:" in rationale
    compact = _COMPACT_PO_MARKER.match(rationale)
    legacy = _LEGACY_PO_MARKER.match(rationale)
    match = compact or legacy
    if match is None:
        if claims_ownership:
            raise OwnershipMarkerError(
                f"purchase order {order.po_number} has a malformed or missing ownership marker"
            )
        return None
    marker_version = int(match.group("version"))
    if (
        not claims_ownership
        or marker_version not in _SUPPORTED_PO_MARKER_VERSIONS
    ):
        raise OwnershipMarkerError(
            f"purchase order {order.po_number} has an invalid ownership marker version or number"
        )
    if compact is not None:
        if marker_version != PO_MARKER_VERSION:
            raise OwnershipMarkerError(
                f"purchase order {order.po_number} has an invalid compact marker version"
            )
        route_id = _decoded(match.group("route"), "route")
        policy_version = _decoded(match.group("policy"), "policy")
        try:
            evidence_contract = EvidenceContract(
                _decoded(match.group("contract"), "contract")
            )
        except ValueError as error:
            raise OwnershipMarkerError(
                f"purchase order {order.po_number} has an invalid evidence contract"
            ) from error
        requirement_id = _decoded(match.group("requirement"), "requirement")
        line_index = int(match.group("line"))
        line_count = int(match.group("line_count"))
        if line_index >= line_count:
            raise OwnershipMarkerError(
                f"purchase order {order.po_number} line index is outside its compact group"
            )
        if (
            order.unit_price is None
            or order.order_date is None
            or order.expected_delivery_date is None
        ):
            raise OwnershipMarkerError(
                f"purchase order {order.po_number} has incomplete owned business fields"
            )
        demand_digest = match.group("demand")
        source_digest = match.group("source")
        key = action_key(
            demand_digest,
            order.component_id,
            order.supplier_id,
            route_id,
            order.order_date,
            policy_version,
        )
        if match.group("action") != key or order.po_number != po_number_for_action(key):
            raise OwnershipMarkerError(
                f"purchase order {order.po_number} action key or short number is invalid"
            )
        marker_end = match.end()
        if rationale[marker_end : marker_end + 1] != " " or not rationale[marker_end + 1 :]:
            raise OwnershipMarkerError(
                f"purchase order {order.po_number} has a missing compact rationale body"
            )
        rationale_body = rationale[marker_end + 1 :]
        field_digest = _compact_field_digest(
            action_digest=key,
            demand_digest=demand_digest,
            source_digest=source_digest,
            evidence_contract=evidence_contract,
            requirement_id=requirement_id,
            route_id=route_id,
            policy_pack_version=policy_version,
            line_index=line_index,
            line_count=line_count,
            po_number=order.po_number,
            component_id=order.component_id,
            supplier_id=order.supplier_id,
            quantity=order.quantity,
            unit_price=order.unit_price,
            order_date=order.order_date,
            expected_delivery_date=order.expected_delivery_date,
            rationale_body=rationale_body,
        )
        if match.group("fields") != field_digest:
            raise OwnershipMarkerError(
                f"purchase order {order.po_number} compact marker does not validate against stored business fields"
            )
        return ParsedOwnedPurchaseOrder(
            action_key=key,
            demand_fingerprint=demand_digest,
            source_fingerprint=source_digest,
            evidence_contract=evidence_contract,
            policy_pack_version=policy_version,
            route_id=route_id,
            line_index=line_index,
            line_count=line_count,
            requirement_id=requirement_id,
            group_digest=match.group("group"),
            field_digest=field_digest,
            decision=None,
            marker_version=marker_version,
        )

    if marker_version not in {1, 2, 3}:
        raise OwnershipMarkerError(
            f"purchase order {order.po_number} has an invalid legacy marker version"
        )
    route_id = _decoded(match.group("route"), "route")
    policy_version = _decoded(match.group("policy"), "policy")
    record_json = _decoded(match.group("record"), "record")
    legacy_schema_version: int | None = None
    try:
        decision = canonical_loads(record_json, DecisionRecord)
    except (TypeError, ValueError) as strict_error:
        try:
            decision = _load_legacy_decision(record_json)
            legacy_schema_version = marker_version
        except (TypeError, ValueError) as compatibility_error:
            raise OwnershipMarkerError(
                f"purchase order {order.po_number} contains an invalid decision record"
            ) from compatibility_error
    expected_record = (
        _legacy_v1_record(decision)
        if legacy_schema_version == 1
        else _legacy_v2_record(decision)
        if legacy_schema_version == 2
        else _legacy_v3_record(decision)
        if legacy_schema_version == 3
        else canonical_dumps(decision)
    )
    if _encoded(expected_record) != match.group("record"):
        raise OwnershipMarkerError(
            f"purchase order {order.po_number} contains a non-canonical decision record"
        )
    decision_rationale = (
        render_legacy_v1_decision_rationale(decision)
        if legacy_schema_version == 1
        else render_decision_rationale(decision)
    )
    if decision.rationale != decision_rationale:
        raise OwnershipMarkerError(
            f"purchase order {order.po_number} contains non-canonical decision rationale"
        )
    if decision.selected_plan is None:
        raise OwnershipMarkerError(
            f"purchase order {order.po_number} decision record has no selected plan"
        )
    line_index = int(match.group("line"))
    if line_index >= len(decision.selected_plan.lines):
        raise OwnershipMarkerError(
            f"purchase order {order.po_number} line index is outside its selected plan"
        )
    line = decision.selected_plan.lines[line_index]
    if line.route_id != route_id:
        raise OwnershipMarkerError(
            f"purchase order {order.po_number} route marker disagrees with its decision record"
        )
    demand_digest = demand_fingerprint(decision)
    if match.group("demand") != demand_digest:
        raise OwnershipMarkerError(
            f"purchase order {order.po_number} demand fingerprint is invalid"
        )
    key = action_key(
        demand_digest,
        line.component_id,
        line.supplier_id,
        line.route_id,
        line.order_date,
        policy_version,
    )
    if match.group("action") != key or order.po_number != po_number_for_action(key):
        raise OwnershipMarkerError(
            f"purchase order {order.po_number} action key or short number is invalid"
        )
    marker = _po_marker(
        key=key,
        demand_digest=demand_digest,
        route_id=route_id,
        policy_pack_version=policy_version,
        line_index=line_index,
        decision_token=match.group("record"),
        version=marker_version,
    )
    line_rationale = (
        render_legacy_v1_line_rationale(decision, line)
        if legacy_schema_version == 1
        else render_line_rationale(decision, line)
    )
    expected_rationale = f"{marker} {line_rationale}"
    expected_fields = (
        line.component_id,
        line.supplier_id,
        line.quantity,
        line.unit_price,
        line.order_date,
        line.expected_delivery_date,
        expected_rationale,
    )
    actual_fields = (
        order.component_id,
        order.supplier_id,
        order.quantity,
        order.unit_price,
        order.order_date,
        order.expected_delivery_date,
        order.rationale,
    )
    if actual_fields != expected_fields:
        raise OwnershipMarkerError(
            f"purchase order {order.po_number} marker does not validate against stored business fields"
        )
    return ParsedOwnedPurchaseOrder(
        action_key=key,
        demand_fingerprint=demand_digest,
        source_fingerprint=None,
        evidence_contract=None,
        policy_pack_version=policy_version,
        route_id=route_id,
        line_index=line_index,
        line_count=len(decision.selected_plan.lines),
        requirement_id=decision.requirement_id,
        group_digest=sha256(record_json.encode("utf-8")).hexdigest(),
        field_digest=sha256(expected_rationale.encode("utf-8")).hexdigest(),
        decision=decision,
        marker_version=marker_version,
    )


@dataclass(frozen=True, slots=True)
class _ManagedPurchaseOrderGroup:
    requirement_id: str
    demand_fingerprint: str
    source_fingerprint: str | None
    evidence_contract: EvidenceContract | None
    policy_pack_version: str
    group_digest: str
    line_count: int
    rows: tuple[tuple[ExistingPurchaseOrder, ParsedOwnedPurchaseOrder], ...]
    legacy_decision: DecisionRecord | None = None


def _managed_purchase_order_groups(
    snapshot: ScenarioSnapshot,
) -> tuple[_ManagedPurchaseOrderGroup, ...]:
    groups: dict[str, list[tuple[ExistingPurchaseOrder, ParsedOwnedPurchaseOrder]]] = {}
    action_keys: dict[str, ExistingPurchaseOrder] = {}
    for order in snapshot.purchase_orders:
        parsed = parse_owned_purchase_order(order)
        if parsed is None:
            continue
        prior = action_keys.get(parsed.action_key)
        if prior is not None:
            raise ActionCollisionError(
                f"full action key {parsed.action_key} is reused by {prior.po_number} and {order.po_number}"
            )
        action_keys[parsed.action_key] = order
        groups.setdefault(parsed.group_digest, []).append((order, parsed))

    result: list[_ManagedPurchaseOrderGroup] = []
    for group_digest, raw_rows in sorted(groups.items()):
        rows = tuple(sorted(raw_rows, key=lambda item: item[1].line_index))
        first = rows[0][1]
        facts = {
            (
                parsed.requirement_id,
                parsed.demand_fingerprint,
                parsed.source_fingerprint,
                parsed.evidence_contract,
                parsed.policy_pack_version,
                parsed.line_count,
                parsed.marker_version,
            )
            for _order, parsed in rows
        }
        if len(facts) != 1:
            raise OwnershipMarkerError(
                f"managed group {group_digest} has inconsistent compact metadata"
            )
        indexes = tuple(parsed.line_index for _order, parsed in rows)
        if indexes != tuple(range(first.line_count)):
            raise OwnershipMarkerError(
                f"managed record {first.requirement_id} has an incomplete or duplicate line group"
            )
        legacy_decisions = {
            canonical_dumps(parsed.decision): parsed.decision
            for _order, parsed in rows
            if parsed.decision is not None
        }
        if first.marker_version == PO_MARKER_VERSION:
            if first.source_fingerprint is None or first.evidence_contract is None:
                raise OwnershipMarkerError(
                    f"managed record {first.requirement_id} lacks compact source metadata"
                )
            expected_group = _compact_group_digest(
                requirement_id=first.requirement_id,
                demand_digest=first.demand_fingerprint,
                source_digest=first.source_fingerprint,
                evidence_contract=first.evidence_contract,
                policy_pack_version=first.policy_pack_version,
                line_count=first.line_count,
                lines=tuple(
                    (
                        parsed.line_index,
                        parsed.action_key,
                        parsed.route_id,
                        parsed.field_digest,
                    )
                    for _order, parsed in rows
                ),
            )
            if expected_group != group_digest:
                raise OwnershipMarkerError(
                    f"managed record {first.requirement_id} has a forged compact group digest"
                )
            legacy_decision = None
        else:
            if len(legacy_decisions) != 1:
                raise OwnershipMarkerError(
                    f"managed record {first.requirement_id} has inconsistent legacy decisions"
                )
            legacy_decision = next(iter(legacy_decisions.values()))
        result.append(
            _ManagedPurchaseOrderGroup(
                requirement_id=first.requirement_id,
                demand_fingerprint=first.demand_fingerprint,
                source_fingerprint=first.source_fingerprint,
                evidence_contract=first.evidence_contract,
                policy_pack_version=first.policy_pack_version,
                group_digest=group_digest,
                line_count=first.line_count,
                rows=rows,
                legacy_decision=legacy_decision,
            )
        )
    return tuple(result)


def current_managed_order_numbers(
    snapshot: ScenarioSnapshot,
    demand_fingerprints: Mapping[str, str],
    policy_pack_version: str,
    evidence_contract: EvidenceContract,
    routes: Sequence[CandidateRoute],
    *,
    policy_concepts_version: str = "",
    candidate_rejections: Sequence[object] = (),
    candidate_alerts: Sequence[object] = (),
) -> frozenset[str]:
    """Identify complete current actions eligible for fresh reconstruction.

    Only exact current demand, policy, route, price, and date matches qualify.
    Everything else remains in the planning snapshot as an independently
    counted prior commitment.
    """

    if not isinstance(snapshot, ScenarioSnapshot):
        raise TypeError("snapshot must be ScenarioSnapshot")
    if not isinstance(policy_pack_version, str) or not policy_pack_version:
        raise ValueError("policy_pack_version must be non-empty text")
    if not isinstance(evidence_contract, EvidenceContract):
        raise TypeError("evidence_contract must be EvidenceContract")
    route_by_id = {route.route_id: route for route in routes}
    if len(route_by_id) != len(tuple(routes)):
        raise DecisionError("candidate routes contain duplicate route IDs")
    result: set[str] = set()
    for group in _managed_purchase_order_groups(snapshot):
        # Legacy markers have no all-candidate source digest.  Their embedded
        # selected route is insufficient proof that removing them is safe.
        if group.legacy_decision is not None:
            continue
        first_order = group.rows[0][0]
        if (
            group.policy_pack_version != policy_pack_version
            or group.evidence_contract is not evidence_contract
            or demand_fingerprints.get(first_order.component_id)
            != group.demand_fingerprint
            or group.source_fingerprint
            != component_source_fingerprint(
                snapshot,
                first_order.component_id,
                policy_pack_version,
                evidence_contract,
                policy_concepts_version=policy_concepts_version,
                candidate_routes=routes,
                candidate_rejections=candidate_rejections,
                candidate_alerts=candidate_alerts,
            )
        ):
            continue
        current = True
        for order, parsed in group.rows:
            route = route_by_id.get(parsed.route_id)
            current = current and (
                route is not None
                and route.component_id == order.component_id
                and route.supplier_id == order.supplier_id
                and route.may_enter_executable_model
                and order.unit_price == route.unit_price
                and order.order_date == route.order_date
                and order.expected_delivery_date == route.expected_delivery_date
                and order.quantity >= route.minimum_order_quantity
            )
        if current:
            result.update(order.po_number for order, _parsed in group.rows)
    return frozenset(result)


def reconstruct_managed_decisions(
    snapshot: ScenarioSnapshot,
    planned_decisions: Sequence[DecisionRecord] | None = None,
    policy_pack_version: str | None = None,
) -> tuple[DecisionRecord, ...]:
    """Reconstruct current managed actions from fresh, validated plan records.

    Legacy v1-v3 payloads remain strictly parseable, but are returned directly
    only for compatibility callers that have no compact rows.  Compact v4 rows
    intentionally contain no decision payload and therefore require the fresh
    independently validated records that are authoritative for the current run.
    """

    if not isinstance(snapshot, ScenarioSnapshot):
        raise TypeError("snapshot must be ScenarioSnapshot")
    groups = _managed_purchase_order_groups(snapshot)
    if planned_decisions is None:
        if any(group.legacy_decision is None for group in groups):
            raise DecisionError(
                "compact managed rows require fresh validated decisions for reconstruction"
            )
        return tuple(
            sorted(
                (group.legacy_decision for group in groups if group.legacy_decision is not None),
                key=lambda item: (item.requirement_id, item.component_id),
            )
        )
    if not isinstance(policy_pack_version, str) or not policy_pack_version:
        raise ValueError("policy_pack_version is required with planned_decisions")
    planned = tuple(_normalized_decision(item) for item in planned_decisions)
    outputs = build_decision_outputs(planned, policy_pack_version)
    targets_by_requirement: dict[str, list[PurchaseOrderOutput]] = {}
    for target in outputs.purchase_orders:
        targets_by_requirement.setdefault(target.decision.requirement_id, []).append(target)
    reconstructed: list[DecisionRecord] = []
    for group in groups:
        targets = targets_by_requirement.get(group.requirement_id, [])
        stored_actions = {parsed.action_key for _order, parsed in group.rows}
        target_actions = {target.action_key for target in targets}
        if stored_actions != target_actions:
            continue
        if group.policy_pack_version != policy_pack_version:
            continue
        if group.demand_fingerprint != targets[0].demand_fingerprint:
            continue
        if group.legacy_decision is None and (
            group.source_fingerprint != targets[0].source_fingerprint
            or group.evidence_contract is not targets[0].evidence_contract
        ):
            continue
        stored_by_action = {
            parsed.action_key: order for order, parsed in group.rows
        }
        for target in targets:
            stored = stored_by_action[target.action_key]
            same_business = _stored_fields(stored)[:-1] == target.business_fields[:-1]
            exact = _stored_fields(stored) == target.business_fields
            if not same_business or (
                group.legacy_decision is None and not exact
            ):
                raise ActionCollisionError(
                    f"current action {target.action_key} disagrees with stored business fields"
                )
        reconstructed.append(targets[0].decision)
    return tuple(
        sorted(
            {item.requirement_id: item for item in reconstructed}.values(),
            key=lambda item: (item.requirement_id, item.component_id),
        )
    )


def reconcile_managed_decisions(
    snapshot: ScenarioSnapshot,
    planned_decisions: Sequence[DecisionRecord],
    policy_pack_version: str,
) -> tuple[DecisionRecord, ...]:
    """Reconcile fresh planned decisions without reviving an embedded record."""

    if not isinstance(snapshot, ScenarioSnapshot):
        raise TypeError("snapshot must be ScenarioSnapshot")
    if not isinstance(policy_pack_version, str) or not policy_pack_version:
        raise ValueError("policy_pack_version must be non-empty text")
    planned = tuple(_normalized_decision(item) for item in planned_decisions)
    if len({item.requirement_id for item in planned}) != len(planned):
        raise DecisionError("planned decisions contain duplicate requirement IDs")
    _managed_purchase_order_groups(snapshot)
    # Matching current actions are reconstructed here as an ownership check;
    # unmatched historical commitments remain physical inbound and are never
    # copied into current decisions.
    reconstruct_managed_decisions(snapshot, planned, policy_pack_version)
    return tuple(sorted(planned, key=lambda item: (item.requirement_id, item.component_id)))


def _stored_fields(order: ExistingPurchaseOrder) -> tuple[object, ...]:
    return (
        order.po_number,
        order.component_id,
        order.supplier_id,
        order.quantity,
        order.unit_price,
        order.order_date,
        order.expected_delivery_date,
        order.rationale,
    )


def _source_state(snapshot: ScenarioSnapshot) -> tuple[object, ...]:
    return (
        snapshot.configuration,
        snapshot.products,
        snapshot.components,
        snapshot.suppliers,
        snapshot.bom_lines,
        snapshot.catalog_lines,
        snapshot.production_orders,
        snapshot.inventory,
    )


def _file_identity(path: Path) -> tuple[int, int]:
    status = path.stat(follow_symlinks=False)
    return status.st_dev, status.st_ino


class AtomicDecisionWriter:
    """Write validated decision rows with digest recheck and full rollback."""

    def __init__(
        self,
        scenario_path: Path,
        policy_pack_version: str,
        *,
        policy_registry: PolicyRegistry | None = None,
        active_directives: Sequence[str] = (),
        inactive_directives: Sequence[str] = (),
        visible_alert_prefixes: bool = False,
        step_hook: Callable[[CommitStep], None] | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        if not isinstance(scenario_path, Path):
            raise TypeError("scenario_path must be pathlib.Path")
        if not isinstance(policy_pack_version, str) or not policy_pack_version:
            raise ValueError("policy_pack_version must be non-empty text")
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be nonnegative")
        self._scenario_path = scenario_path
        self._policy_pack_version = policy_pack_version
        self._policy_registry = policy_registry
        self._active_directives = tuple(active_directives)
        self._inactive_directives = tuple(inactive_directives)
        self._visible_alert_prefixes = visible_alert_prefixes
        self._step_hook = step_hook
        self._timeout_seconds = timeout_seconds

    def _step(self, step: CommitStep) -> None:
        if self._step_hook is not None:
            self._step_hook(step)

    def commit(
        self,
        snapshot: ScenarioSnapshot,
        decisions: Sequence[DecisionRecord],
        validation: ValidationResult,
        /,
        *,
        dry_run: bool = False,
    ) -> CommitResult:
        if not isinstance(snapshot, ScenarioSnapshot):
            raise TypeError("snapshot must be ScenarioSnapshot")
        if not isinstance(validation, ValidationResult):
            raise TypeError("validation must be ValidationResult")
        if not validation.is_valid:
            raise DecisionError("invalid, incomplete, or unproven decisions cannot be committed")
        outputs = build_decision_outputs(
            decisions,
            self._policy_pack_version,
            policy_registry=self._policy_registry,
            active_directives=self._active_directives,
            inactive_directives=self._inactive_directives,
            visible_alert_prefixes=self._visible_alert_prefixes,
            route_input_issues=snapshot.route_input_issues,
        )
        try:
            resolved = resolve_scenario_path(self._scenario_path)
            expected_identity = _file_identity(resolved)
        except (ScenarioPathError, OSError) as error:
            raise CommitFailure(
                f"scenario path is not a writable file: {self._scenario_path}"
            ) from error

        try:
            with closing(
                sqlite3.connect(
                    f"{resolved.as_uri()}?mode=rw",
                    uri=True,
                    isolation_level=None,
                    timeout=self._timeout_seconds,
                )
            ) as connection:
                connection.row_factory = sqlite3.Row
                connection.enable_load_extension(False)
                if hasattr(connection, "setlimit"):
                    connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
                    connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 100_000)
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA trusted_schema = OFF")
                return self._commit_locked(
                    connection,
                    snapshot,
                    outputs,
                    dry_run=dry_run,
                    resolved_path=resolved,
                    expected_identity=expected_identity,
                )
        except DecisionError:
            raise
        except (
            OSError,
            sqlite3.Error,
            UnicodeError,
            ScenarioLoadError,
            ExplanationError,
        ) as error:
            raise CommitFailure("atomic decision commit failed and was rolled back") from error

    def _commit_locked(
        self,
        connection: sqlite3.Connection,
        snapshot: ScenarioSnapshot,
        outputs: DecisionOutputs,
        *,
        dry_run: bool,
        resolved_path: Path,
        expected_identity: tuple[int, int],
    ) -> CommitResult:
        inserted_numbers: list[str] = []
        inserted_alerts = 0
        deleted_alerts = 0
        began = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            began = True
            if _file_identity(resolved_path) != expected_identity:
                raise ConcurrentModificationError(
                    "scenario file identity changed after planning; no decision rows were written"
                )
            columns = SQLiteRepository._validate_schema(connection)
            current = SQLiteRepository()._load(connection, columns)
            if current.state_digest != snapshot.state_digest or current != snapshot:
                raise ConcurrentModificationError(
                    "scenario changed after planning; no decision rows were written"
                )
            self._step(CommitStep.DIGEST_RECHECKED)

            existing_by_number = {item.po_number: item for item in current.purchase_orders}
            existing_by_action: dict[str, ExistingPurchaseOrder] = {}
            for existing in current.purchase_orders:
                parsed = parse_owned_purchase_order(existing)
                if parsed is not None:
                    collision = existing_by_action.get(parsed.action_key)
                    if collision is not None:
                        raise ActionCollisionError(
                            f"full action key collision between {collision.po_number} and "
                            f"{existing.po_number}"
                        )
                    existing_by_action[parsed.action_key] = existing

            for target in outputs.purchase_orders:
                same_number = existing_by_number.get(target.po_number)
                same_action = existing_by_action.get(target.action_key)
                if same_action is not None and same_action.po_number != target.po_number:
                    raise ActionCollisionError(
                        f"action key {target.action_key} already belongs to {same_action.po_number}"
                    )
                if same_number is not None:
                    parsed = parse_owned_purchase_order(same_number)
                    if parsed is None or parsed.action_key != target.action_key:
                        raise ActionCollisionError(
                            f"purchase-order number collision at {target.po_number}"
                        )
                    stored_fields = _stored_fields(same_number)
                    older_marker_equivalent = (
                        parsed.marker_version < PO_MARKER_VERSION
                        and stored_fields[:-1] == target.business_fields[:-1]
                    )
                    if (
                        stored_fields != target.business_fields
                        and not older_marker_equivalent
                    ):
                        raise ActionCollisionError(
                            f"action key {target.action_key} matches different business fields"
                        )
                    continue
                connection.execute(
                    "INSERT INTO purchase_orders "
                    "(po_number, component_id, supplier_id, quantity, unit_price, order_date, "
                    "expected_delivery_date, rationale) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        target.po_number,
                        target.component_id,
                        target.supplier_id,
                        str(target.quantity),
                        str(target.unit_price),
                        target.order_date.isoformat(),
                        target.expected_delivery_date.isoformat(),
                        target.rationale,
                    ),
                )
                inserted_numbers.append(target.po_number)
            self._step(CommitStep.PURCHASE_ORDERS_INSERTED)

            target_descriptions = {item.description for item in outputs.alerts}
            scopes = {item.scope for item in outputs.alerts}
            stored_owned: dict[str, int] = {}
            external_alerts = {
                item.alert_id: item.description for item in current.alerts
            }
            for alert in current.alerts:
                parsed = parse_owned_alert(alert.description)
                if parsed is None:
                    if "[APEX_ALERT:" in alert.description:
                        raise OwnershipMarkerError(
                            f"alert {alert.alert_id} has a malformed ownership marker"
                        )
                    continue
                validated = validate_stored_alert(alert.description, scopes)
                if validated is None:
                    raise OwnershipMarkerError(
                        f"alert {alert.alert_id} has a forged or stale-invalid ownership marker"
                    )
                external_alerts.pop(alert.alert_id)
                if alert.description in stored_owned:
                    raise OwnershipMarkerError("duplicate owned alert descriptions are not valid")
                stored_owned[alert.description] = alert.alert_id

            obsolete = sorted(set(stored_owned) - target_descriptions)
            missing = sorted(target_descriptions - set(stored_owned))
            for description in obsolete:
                cursor = connection.execute(
                    "DELETE FROM alerts WHERE alert_id = ? AND description = ?",
                    (stored_owned[description], description),
                )
                if cursor.rowcount != 1:
                    raise CommitPostconditionError("owned alert disappeared during reconciliation")
                deleted_alerts += 1
            for description in missing:
                connection.execute(
                    "INSERT INTO alerts (description) VALUES (?)",
                    (description,),
                )
                inserted_alerts += 1
            self._step(CommitStep.ALERTS_RECONCILED)

            after_columns = SQLiteRepository._validate_schema(connection)
            after = SQLiteRepository()._load(connection, after_columns)
            self._check_postconditions(
                current,
                after,
                outputs,
                external_alerts,
                stored_owned,
                self._policy_pack_version,
            )
            self._step(CommitStep.POSTCONDITIONS_CHECKED)
            if _file_identity(resolved_path) != expected_identity:
                raise ConcurrentModificationError(
                    "scenario file identity changed during commit; all decision writes were rolled back"
                )
            if dry_run:
                connection.rollback()
                return CommitResult((), 0, 0, True)
            connection.commit()
            began = False
            return CommitResult(
                committed_po_numbers=tuple(inserted_numbers),
                inserted_alert_count=inserted_alerts,
                deleted_alert_count=deleted_alerts,
                no_op=not inserted_numbers and inserted_alerts == 0 and deleted_alerts == 0,
            )
        except BaseException:
            if began and connection.in_transaction:
                connection.rollback()
            raise

    @staticmethod
    def _check_postconditions(
        before: ScenarioSnapshot,
        after: ScenarioSnapshot,
        outputs: DecisionOutputs,
        external_alerts: dict[int, str],
        prior_owned: dict[str, int],
        policy_pack_version: str,
    ) -> None:
        if _source_state(after) != _source_state(before):
            raise CommitPostconditionError("a source or master table changed during commit")
        before_orders = {item.po_number: item for item in before.purchase_orders}
        after_orders = {item.po_number: item for item in after.purchase_orders}
        for number, order in before_orders.items():
            if after_orders.get(number) != order:
                raise CommitPostconditionError(
                    f"pre-existing purchase order {number} was modified or removed"
                )
        targets = {item.po_number: item for item in outputs.purchase_orders}
        if set(after_orders) != set(before_orders) | set(targets):
            raise CommitPostconditionError("purchase-order target set does not match committed rows")
        for number, target in targets.items():
            stored = after_orders.get(number)
            if stored is None:
                raise CommitPostconditionError(
                    f"purchase order {number} failed exact business-field post-validation"
                )
            parsed = parse_owned_purchase_order(stored)
            if parsed is None or parsed.action_key != target.action_key:
                raise CommitPostconditionError(
                    f"purchase order {number} failed ownership post-validation"
                )
            stored_fields = _stored_fields(stored)
            older_marker_equivalent = (
                parsed.marker_version < PO_MARKER_VERSION
                and stored_fields[:-1] == target.business_fields[:-1]
            )
            if (
                stored_fields != target.business_fields
                and not older_marker_equivalent
            ):
                raise CommitPostconditionError(
                    f"purchase order {number} failed exact business-field post-validation"
                )
        reconstruct_managed_decisions(
            after,
            outputs.decisions,
            policy_pack_version,
        )

        after_alerts = {item.alert_id: item.description for item in after.alerts}
        for alert_id, description in external_alerts.items():
            if after_alerts.get(alert_id) != description:
                raise CommitPostconditionError(f"external alert {alert_id} was modified or removed")
        target_descriptions = {item.description for item in outputs.alerts}
        owned_after: dict[str, int] = {}
        target_scopes = {item.scope for item in outputs.alerts}
        for alert in after.alerts:
            parsed = parse_owned_alert(alert.description)
            if parsed is None:
                if "[APEX_ALERT:" in alert.description:
                    raise CommitPostconditionError(
                        f"alert {alert.alert_id} has a malformed ownership marker after commit"
                    )
                continue
            if validate_stored_alert(alert.description, target_scopes) is None:
                raise CommitPostconditionError(
                    f"alert {alert.alert_id} failed ownership post-validation"
                )
            if alert.description in owned_after:
                raise CommitPostconditionError("duplicate owned alerts exist after reconciliation")
            owned_after[alert.description] = alert.alert_id
        if set(owned_after) != target_descriptions:
            raise CommitPostconditionError("owned alert target set does not match committed rows")
        for description, old_id in prior_owned.items():
            if description in target_descriptions and owned_after[description] != old_id:
                raise CommitPostconditionError("unchanged owned alert did not preserve its ID")
        accounting = [
            item
            for item in outputs.alerts
            if item.category is AlertCategory.RUN_ACCOUNTING
        ]
        if len(accounting) != 1 or accounting[0].description not in owned_after:
            raise CommitPostconditionError("exactly one current RUN_ACCOUNTING alert is required")


def commit_decisions(
    scenario_path: Path,
    snapshot: ScenarioSnapshot,
    decisions: Sequence[DecisionRecord],
    validation: ValidationResult,
    policy_pack_version: str,
    *,
    dry_run: bool = False,
    policy_registry: PolicyRegistry | None = None,
    active_directives: Sequence[str] = (),
    inactive_directives: Sequence[str] = (),
    visible_alert_prefixes: bool = False,
    step_hook: Callable[[CommitStep], None] | None = None,
) -> CommitResult:
    """Functional entry point for the atomic decision writer."""

    writer = AtomicDecisionWriter(
        scenario_path,
        policy_pack_version,
        policy_registry=policy_registry,
        active_directives=active_directives,
        inactive_directives=inactive_directives,
        visible_alert_prefixes=visible_alert_prefixes,
        step_hook=step_hook,
    )
    return writer.commit(snapshot, decisions, validation, dry_run=dry_run)


__all__ = [
    "ActionCollisionError",
    "AtomicDecisionWriter",
    "CommitFailure",
    "CommitPostconditionError",
    "CommitStep",
    "ConcurrentModificationError",
    "DecisionError",
    "DecisionOutputs",
    "OwnershipMarkerError",
    "PO_MARKER_VERSION",
    "ParsedOwnedPurchaseOrder",
    "PurchaseOrderOutput",
    "action_key",
    "build_decision_outputs",
    "commit_decisions",
    "component_source_fingerprint",
    "current_managed_order_numbers",
    "demand_fingerprint",
    "demand_fingerprint_from_facts",
    "parse_owned_purchase_order",
    "po_number_for_action",
    "reconstruct_managed_decisions",
    "reconcile_managed_decisions",
]
