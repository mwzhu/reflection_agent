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
from typing import Callable, Sequence

from .domain import (
    AlertCategory,
    CommitResult,
    DecisionRecord,
    ExistingPurchaseOrder,
    PlanLine,
    ScenarioSnapshot,
    ValidationResult,
)
from .explanations import (
    ExplanationError,
    RenderedAlert,
    parse_owned_alert,
    render_alerts,
    render_decision_rationale,
    render_line_rationale,
    validate_stored_alert,
)
from .policy.registry import PolicyRegistry
from .repository import SQLiteRepository, ScenarioLoadError
from .serialization import canonical_dumps, canonical_loads


PO_MARKER_VERSION = 1
_HEX_64 = r"[0-9a-f]{64}"
_TOKEN = r"[A-Za-z0-9_-]+"
_PO_MARKER = re.compile(
    rf"\A\[APEX_AGENT:v(?P<version>[0-9]+) action=(?P<action>{_HEX_64}) "
    rf"demand=(?P<demand>{_HEX_64}) route=(?P<route>{_TOKEN}) "
    rf"policy=(?P<policy>{_TOKEN}) line=(?P<line>[0-9]+) "
    rf"record=(?P<record>{_TOKEN})\]"
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
    policy_pack_version: str
    route_id: str
    line_index: int
    decision: DecisionRecord


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
    demand = sorted(
        (
            contribution.order_id,
            bucket.due_date.isoformat(),
            _canonical_decimal(contribution.quantity),
        )
        for bucket in decision.demand_buckets
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
        for item in decision.supply_ledger.committed_inbound
        if not item.agent_owned
    )
    payload = canonical_dumps(
        {
            "version": 1,
            "demand": demand,
            "on_hand": _canonical_decimal(decision.supply_ledger.on_hand),
            "counted_external_inbound": inbound,
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
    return replace(decision, rationale=render_decision_rationale(decision))


def _po_marker(
    *,
    key: str,
    demand_digest: str,
    route_id: str,
    policy_pack_version: str,
    line_index: int,
    decision_token: str,
) -> str:
    return (
        f"[APEX_AGENT:v{PO_MARKER_VERSION} action={key} demand={demand_digest} "
        f"route={_encoded(route_id)} policy={_encoded(policy_pack_version)} "
        f"line={line_index} record={decision_token}]"
    )


def _purchase_order_output(
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
    record_token = _encoded(canonical_dumps(decision))
    marker = _po_marker(
        key=key,
        demand_digest=demand_digest,
        route_id=line.route_id,
        policy_pack_version=policy_pack_version,
        line_index=line_index,
        decision_token=record_token,
    )
    rationale = f"{marker} {render_line_rationale(decision, line)}"
    return PurchaseOrderOutput(
        action_key=key,
        demand_fingerprint=demand_digest,
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


def build_decision_outputs(
    decisions: Sequence[DecisionRecord],
    policy_pack_version: str,
    *,
    policy_registry: PolicyRegistry | None = None,
    active_directives: Sequence[str] = (),
    inactive_directives: Sequence[str] = (),
    visible_alert_prefixes: bool = False,
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
        if decision.selected_plan is None:
            continue
        for index, line in enumerate(decision.selected_plan.lines):
            orders.append(_purchase_order_output(decision, line, index, policy_pack_version))
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
    match = _PO_MARKER.match(rationale)
    if match is None:
        if claims_ownership:
            raise OwnershipMarkerError(
                f"purchase order {order.po_number} has a malformed or missing ownership marker"
            )
        return None
    if not claims_ownership or int(match.group("version")) != PO_MARKER_VERSION:
        raise OwnershipMarkerError(
            f"purchase order {order.po_number} has an invalid ownership marker version or number"
        )
    route_id = _decoded(match.group("route"), "route")
    policy_version = _decoded(match.group("policy"), "policy")
    record_json = _decoded(match.group("record"), "record")
    try:
        decision = canonical_loads(record_json, DecisionRecord)
    except (TypeError, ValueError) as error:
        raise OwnershipMarkerError(
            f"purchase order {order.po_number} contains an invalid decision record"
        ) from error
    if _encoded(canonical_dumps(decision)) != match.group("record"):
        raise OwnershipMarkerError(
            f"purchase order {order.po_number} contains a non-canonical decision record"
        )
    if decision.rationale != render_decision_rationale(decision):
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
    )
    expected_rationale = f"{marker} {render_line_rationale(decision, line)}"
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
        policy_pack_version=policy_version,
        route_id=route_id,
        line_index=line_index,
        decision=decision,
    )


def reconstruct_managed_decisions(snapshot: ScenarioSnapshot) -> tuple[DecisionRecord, ...]:
    """Rebuild and cross-check prior managed records from validated PO rationales."""

    if not isinstance(snapshot, ScenarioSnapshot):
        raise TypeError("snapshot must be ScenarioSnapshot")
    groups: dict[str, tuple[DecisionRecord, set[int]]] = {}
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
        token = canonical_dumps(parsed.decision)
        if token not in groups:
            groups[token] = (parsed.decision, set())
        groups[token][1].add(parsed.line_index)
    records: list[DecisionRecord] = []
    for token in sorted(groups):
        record, indexes = groups[token]
        assert record.selected_plan is not None
        expected = set(range(len(record.selected_plan.lines)))
        if indexes != expected:
            raise OwnershipMarkerError(
                f"managed record {record.requirement_id} is missing or duplicates selected lines"
            )
        records.append(record)
    return tuple(sorted(records, key=lambda item: (item.requirement_id, item.component_id)))


def reconcile_managed_decisions(
    snapshot: ScenarioSnapshot,
    planned_decisions: Sequence[DecisionRecord],
    policy_pack_version: str,
) -> tuple[DecisionRecord, ...]:
    """Reattach unchanged closed requirements to their prior managed actions.

    Ledgers correctly count an owned PO as physical inbound.  Consequently an
    unchanged second planning pass can describe the requirement as closed with
    no newly selected plan.  The canonical fingerprint excludes that owned
    inbound, allowing this function to recognize the same source demand and
    return the reconstructable prior action instead.  A changed demand,
    inventory position, external inbound row, or policy version does not match
    and therefore retains the newly planned record.
    """

    if not isinstance(snapshot, ScenarioSnapshot):
        raise TypeError("snapshot must be ScenarioSnapshot")
    if not isinstance(policy_pack_version, str) or not policy_pack_version:
        raise ValueError("policy_pack_version must be non-empty text")
    reconstruct_managed_decisions(snapshot)
    prior_by_requirement: dict[str, list[tuple[DecisionRecord, str]]] = {}
    for order in snapshot.purchase_orders:
        parsed = parse_owned_purchase_order(order)
        if parsed is None:
            continue
        candidates = prior_by_requirement.setdefault(parsed.decision.requirement_id, [])
        pair = (parsed.decision, parsed.policy_pack_version)
        if pair not in candidates:
            candidates.append(pair)

    planned = tuple(_normalized_decision(item) for item in planned_decisions)
    if len({item.requirement_id for item in planned}) != len(planned):
        raise DecisionError("planned decisions contain duplicate requirement IDs")
    reconciled: list[DecisionRecord] = []
    planned_ids: set[str] = set()
    for current in sorted(planned, key=lambda item: (item.requirement_id, item.component_id)):
        planned_ids.add(current.requirement_id)
        matching = [
            prior
            for prior, version in prior_by_requirement.get(current.requirement_id, [])
            if version == policy_pack_version
            and prior.component_id == current.component_id
            and prior.evidence_contract is current.evidence_contract
            and demand_fingerprint(prior) == demand_fingerprint(current)
        ]
        exact = [item for item in matching if item == current]
        if len(exact) == 1:
            reconciled.append(exact[0])
        elif current.selected_plan is None and len(matching) == 1:
            reconciled.append(matching[0])
        elif len(exact) > 1 or (current.selected_plan is None and len(matching) > 1):
            raise ActionCollisionError(
                f"multiple prior actions ambiguously match requirement {current.requirement_id}"
            )
        else:
            reconciled.append(current)

    for requirement_id in sorted(set(prior_by_requirement) - planned_ids):
        matching = [
            prior
            for prior, version in prior_by_requirement[requirement_id]
            if version == policy_pack_version
        ]
        if len(matching) == 1:
            reconciled.append(matching[0])
        elif len(matching) > 1:
            raise ActionCollisionError(
                f"multiple prior actions exist for omitted requirement {requirement_id}"
            )
    return tuple(sorted(reconciled, key=lambda item: (item.requirement_id, item.component_id)))


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
        )
        try:
            resolved = self._scenario_path.resolve(strict=True)
        except (OSError, FileNotFoundError) as error:
            raise CommitFailure(f"scenario path is not a writable file: {self._scenario_path}") from error
        if not resolved.is_file():
            raise CommitFailure(f"scenario path is not a writable file: {self._scenario_path}")

        try:
            with closing(
                sqlite3.connect(
                    resolved,
                    isolation_level=None,
                    timeout=self._timeout_seconds,
                )
            ) as connection:
                connection.row_factory = sqlite3.Row
                connection.enable_load_extension(False)
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA trusted_schema = OFF")
                return self._commit_locked(connection, snapshot, outputs, dry_run=dry_run)
        except DecisionError:
            raise
        except (sqlite3.Error, UnicodeError, ScenarioLoadError, ExplanationError) as error:
            raise CommitFailure("atomic decision commit failed and was rolled back") from error

    def _commit_locked(
        self,
        connection: sqlite3.Connection,
        snapshot: ScenarioSnapshot,
        outputs: DecisionOutputs,
        *,
        dry_run: bool,
    ) -> CommitResult:
        inserted_numbers: list[str] = []
        inserted_alerts = 0
        deleted_alerts = 0
        began = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            began = True
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
                    if _stored_fields(same_number) != target.business_fields:
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
            )
            self._step(CommitStep.POSTCONDITIONS_CHECKED)
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
            if stored is None or _stored_fields(stored) != target.business_fields:
                raise CommitPostconditionError(
                    f"purchase order {number} failed exact business-field post-validation"
                )
            parsed = parse_owned_purchase_order(stored)
            if parsed is None or parsed.action_key != target.action_key:
                raise CommitPostconditionError(
                    f"purchase order {number} failed ownership post-validation"
                )
        reconstruct_managed_decisions(after)

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
    "demand_fingerprint",
    "parse_owned_purchase_order",
    "po_number_for_action",
    "reconstruct_managed_decisions",
    "reconcile_managed_decisions",
]
