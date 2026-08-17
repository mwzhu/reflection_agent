"""Frozen domain contracts shared by all procurement planning stages.

The classes in this module are representations, not business-rule
implementations.  They make quantities, money, dates, evidence state, solver
proof state, and write disposition explicit so later stages cannot communicate
through ambiguous dictionaries or floating-point values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import re
from typing import Iterable

from .config import EvidenceContract
from .policy.parameters import EconomicAutonomyParameters


ZERO = Decimal("0")


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    if not value.strip():
        raise ValueError(f"{name} must be non-empty")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{name} must not contain control characters")


def _require_optional_text(value: str | None, name: str) -> None:
    if value is not None:
        if not isinstance(value, str):
            raise TypeError(f"{name} must be str or None")
        if any(ord(character) < 32 for character in value):
            raise ValueError(f"{name} must not contain control characters")


def _require_decimal(
    value: Decimal,
    name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    if positive and value <= ZERO:
        raise ValueError(f"{name} must be positive")
    if nonnegative and value < ZERO:
        raise ValueError(f"{name} must be nonnegative")


def _require_date(value: date, name: str) -> None:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"{name} must be datetime.date")


def _tuple(value: Iterable[object], name: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)):
        raise TypeError(f"{name} must be an iterable of values, not text")
    try:
        return tuple(value)
    except TypeError as error:
        raise TypeError(f"{name} must be iterable") from error


def _text_tuple(value: Iterable[str], name: str, *, sort: bool = True) -> tuple[str, ...]:
    items = _tuple(value, name)
    for item in items:
        _require_text(item, f"{name} item")
    result = tuple(items)
    return tuple(sorted(result)) if sort else result


def _require_unique(values: Iterable[str], name: str) -> None:
    items = tuple(values)
    if len(items) != len(set(items)):
        raise ValueError(f"{name} contains duplicate logical keys")


class EvidenceStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class EvidenceScope(str, Enum):
    RULE = "RULE"
    CANDIDATE = "CANDIDATE"


class EvidenceBasis(str, Enum):
    PROSPECTIVE_ORDER = "prospective_order"
    ENTITY_ATTRIBUTE = "entity_attribute"
    ROLLING_WINDOW = "rolling_window"
    EXTERNAL_SYSTEM = "external_system"


class RuleSeverity(str, Enum):
    HARD = "hard"
    SHAPING = "shaping"
    ADVISORY = "advisory"


class FulfillmentStatus(str, Enum):
    FULFILLED = "FULFILLED"
    PARTIALLY_FULFILLED = "PARTIALLY_FULFILLED"
    UNFULFILLED = "UNFULFILLED"


class ResolutionStatus(str, Enum):
    RESOLVED = "RESOLVED"
    INFEASIBLE = "INFEASIBLE"
    UNRESOLVED = "UNRESOLVED"


class PlanDisposition(str, Enum):
    EXECUTE = "EXECUTE"
    EXECUTE_WITH_ASSUMPTION = "EXECUTE_WITH_ASSUMPTION"
    RECOMMEND_APPROVAL = "RECOMMEND_APPROVAL"
    DECISION_REQUIRED = "DECISION_REQUIRED"

    @property
    def writes_purchase_order(self) -> bool:
        return self in {
            PlanDisposition.EXECUTE,
            PlanDisposition.EXECUTE_WITH_ASSUMPTION,
        }


class SolveKind(str, Enum):
    QUANTITY_CALIBRATION = "quantity_calibration"
    BASELINE = "baseline"
    EXECUTABLE = "executable"
    COUNTERFACTUAL = "counterfactual"


class SolverStatus(str, Enum):
    OPTIMAL = "OPTIMAL"
    INFEASIBLE = "INFEASIBLE"
    FEASIBLE_INCUMBENT = "FEASIBLE_INCUMBENT"
    TIMEOUT = "TIMEOUT"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    UNBOUNDED = "UNBOUNDED"
    ERROR = "ERROR"
    UNRESOLVED = "UNRESOLVED"


class ValidationSeverity(str, Enum):
    WARNING = "WARNING"
    ERROR = "ERROR"


class ValidationFailureScope(str, Enum):
    """Reviewed blast radius for an independent-validation issue.

    ``component_id`` remains diagnostic metadata only.  Containment requires
    both an explicitly component-local scope and a reviewed allowlisted code.
    """

    GLOBAL = "GLOBAL"
    COMPONENT_LOCAL = "COMPONENT_LOCAL"
    UNKNOWN = "UNKNOWN"


class AlertCategory(str, Enum):
    UNMET_DEMAND = "UNMET_DEMAND"
    LATE_ARRIVAL = "LATE_ARRIVAL"
    NO_ELIGIBLE_SUPPLIER = "NO_ELIGIBLE_SUPPLIER"
    POLICY_CONFLICT = "POLICY_CONFLICT"
    DECISION_REQUIRED = "DECISION_REQUIRED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    DOCUMENTATION_REQUIRED = "DOCUMENTATION_REQUIRED"
    SOLE_SOURCE = "SOLE_SOURCE"
    PRE_EXISTING_VIOLATION = "PRE_EXISTING_VIOLATION"
    DATA_QUALITY = "DATA_QUALITY"
    ASSUMPTION = "ASSUMPTION"
    EVIDENCE_CONTRACT = "EVIDENCE_CONTRACT"
    COST_OPPORTUNITY = "COST_OPPORTUNITY"
    CAPACITY_UNKNOWN = "CAPACITY_UNKNOWN"
    RECOVERY_SURPLUS = "RECOVERY_SURPLUS"
    FORCED_SURPLUS = "FORCED_SURPLUS"
    SOLVER_UNPROVEN = "SOLVER_UNPROVEN"
    INTERNAL_FAILURE = "INTERNAL_FAILURE"
    RUN_ACCOUNTING = "RUN_ACCOUNTING"


@dataclass(frozen=True, slots=True)
class SourceEntityNormalizationDisclosure:
    """Auditable resolution of one stale policy source ID by legal name."""

    source_id: str
    legal_name: str
    resolved_supplier_id: str
    rule_id: str
    source_document: str
    reference_path: str

    def __post_init__(self) -> None:
        for name in (
            "source_id",
            "legal_name",
            "resolved_supplier_id",
            "rule_id",
            "source_document",
            "reference_path",
        ):
            _require_text(getattr(self, name), name)
        if self.source_id == self.resolved_supplier_id:
            raise ValueError(
                "a stale source-ID disclosure requires different source and resolved IDs"
            )


class UnitRoundingRule(str, Enum):
    """Reviewed fallback rule for a source unit without configured semantics."""

    DISCRETE_CEILING_AFTER_AGGREGATION = (
        "DISCRETE_CEILING_AFTER_AGGREGATION"
    )


@dataclass(frozen=True, slots=True)
class UnitNormalizationDisclosure:
    """Auditable discrete-rounding default for an unrecognised source unit."""

    source_unit: str
    increment: Decimal
    rounding_rule: UnitRoundingRule
    source_table: str = "components"
    source_field: str = "unit_of_measure"

    def __post_init__(self) -> None:
        _require_text(self.source_unit, "source_unit")
        _require_decimal(self.increment, "increment", positive=True)
        if not isinstance(self.rounding_rule, UnitRoundingRule):
            raise TypeError("rounding_rule must be UnitRoundingRule")
        _require_text(self.source_table, "source_table")
        _require_text(self.source_field, "source_field")


class RouteQuarantineScope(str, Enum):
    """The largest commercial route set invalidated by one source issue."""

    CATALOG_OFFER = "catalog_offer"
    SUPPLIER_ALL_ROUTES = "supplier_all_routes"


VALID_REQUIREMENT_STATES = frozenset(
    {
        (FulfillmentStatus.FULFILLED, ResolutionStatus.RESOLVED),
        (FulfillmentStatus.PARTIALLY_FULFILLED, ResolutionStatus.INFEASIBLE),
        (FulfillmentStatus.PARTIALLY_FULFILLED, ResolutionStatus.UNRESOLVED),
        (FulfillmentStatus.UNFULFILLED, ResolutionStatus.INFEASIBLE),
        (FulfillmentStatus.UNFULFILLED, ResolutionStatus.UNRESOLVED),
    }
)


@dataclass(frozen=True, slots=True)
class RequirementState:
    fulfillment: FulfillmentStatus
    resolution: ResolutionStatus

    def __post_init__(self) -> None:
        if not isinstance(self.fulfillment, FulfillmentStatus):
            raise TypeError("fulfillment must be FulfillmentStatus")
        if not isinstance(self.resolution, ResolutionStatus):
            raise TypeError("resolution must be ResolutionStatus")
        if (self.fulfillment, self.resolution) not in VALID_REQUIREMENT_STATES:
            raise ValueError(
                f"invalid requirement state: {self.fulfillment.value}/{self.resolution.value}"
            )


@dataclass(frozen=True, slots=True)
class ScenarioConfiguration:
    current_date: date
    description: str | None = None

    def __post_init__(self) -> None:
        _require_date(self.current_date, "current_date")
        _require_optional_text(self.description, "description")


@dataclass(frozen=True, slots=True)
class Product:
    product_id: str
    name: str
    description: str | None
    category: str | None
    unit_price: Decimal | None

    def __post_init__(self) -> None:
        _require_text(self.product_id, "product_id")
        _require_text(self.name, "name")
        _require_optional_text(self.description, "description")
        _require_optional_text(self.category, "category")
        if self.unit_price is not None:
            _require_decimal(self.unit_price, "unit_price", nonnegative=True)


@dataclass(frozen=True, slots=True)
class Component:
    component_id: str
    name: str
    description: str | None
    category: str | None
    unit_of_measure: str
    is_hazardous: bool
    required_certifications: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.component_id, "component_id")
        _require_text(self.name, "name")
        _require_optional_text(self.description, "description")
        _require_optional_text(self.category, "category")
        _require_text(self.unit_of_measure, "unit_of_measure")
        if not isinstance(self.is_hazardous, bool):
            raise TypeError("is_hazardous must be bool")
        certifications = _text_tuple(self.required_certifications, "required_certifications")
        _require_unique(certifications, "required_certifications")
        object.__setattr__(self, "required_certifications", certifications)


@dataclass(frozen=True, slots=True)
class Supplier:
    supplier_id: str
    name: str
    country: str | None
    is_domestic: bool | None
    certifications: tuple[str, ...]
    sustainability_rating: str | None
    relationship_tier: str | None
    on_approved_list: bool | None
    notes: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.supplier_id, "supplier_id")
        _require_text(self.name, "name")
        _require_optional_text(self.country, "country")
        if self.is_domestic is not None and not isinstance(self.is_domestic, bool):
            raise TypeError("is_domestic must be bool or None")
        certifications = _text_tuple(self.certifications, "certifications")
        _require_unique(certifications, "certifications")
        object.__setattr__(self, "certifications", certifications)
        _require_optional_text(self.sustainability_rating, "sustainability_rating")
        _require_optional_text(self.relationship_tier, "relationship_tier")
        if self.on_approved_list is not None and not isinstance(self.on_approved_list, bool):
            raise TypeError("on_approved_list must be bool or None")
        _require_optional_text(self.notes, "notes")


@dataclass(frozen=True, slots=True)
class BomLine:
    product_id: str
    component_id: str
    quantity_per: Decimal

    def __post_init__(self) -> None:
        _require_text(self.product_id, "product_id")
        _require_text(self.component_id, "component_id")
        _require_decimal(self.quantity_per, "quantity_per", positive=True)


@dataclass(frozen=True, slots=True)
class SupplierCatalogLine:
    supplier_id: str
    component_id: str
    unit_price: Decimal
    lead_time_days: int
    minimum_order_quantity: Decimal
    notes: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.supplier_id, "supplier_id")
        _require_text(self.component_id, "component_id")
        _require_decimal(self.unit_price, "unit_price", nonnegative=True)
        if not isinstance(self.lead_time_days, int) or isinstance(self.lead_time_days, bool):
            raise TypeError("lead_time_days must be int")
        if self.lead_time_days < 0:
            raise ValueError("lead_time_days must be nonnegative")
        _require_decimal(
            self.minimum_order_quantity,
            "minimum_order_quantity",
            positive=True,
        )
        _require_optional_text(self.notes, "notes")


@dataclass(frozen=True, slots=True)
class ProductionOrder:
    order_id: str
    product_id: str
    quantity: Decimal
    customer: str | None
    materials_needed_by: date

    def __post_init__(self) -> None:
        _require_text(self.order_id, "order_id")
        _require_text(self.product_id, "product_id")
        _require_decimal(self.quantity, "quantity", positive=True)
        _require_optional_text(self.customer, "customer")
        _require_date(self.materials_needed_by, "materials_needed_by")


@dataclass(frozen=True, slots=True)
class InventoryPosition:
    component_id: str
    quantity_on_hand: Decimal
    warehouse_location: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.component_id, "component_id")
        _require_decimal(self.quantity_on_hand, "quantity_on_hand", nonnegative=True)
        _require_optional_text(self.warehouse_location, "warehouse_location")


@dataclass(frozen=True, slots=True)
class ExistingPurchaseOrder:
    po_number: str
    component_id: str
    supplier_id: str
    quantity: Decimal
    unit_price: Decimal | None
    order_date: date | None
    expected_delivery_date: date | None
    rationale: str | None

    def __post_init__(self) -> None:
        _require_text(self.po_number, "po_number")
        _require_text(self.component_id, "component_id")
        _require_text(self.supplier_id, "supplier_id")
        _require_decimal(self.quantity, "quantity", positive=True)
        if self.unit_price is not None:
            _require_decimal(self.unit_price, "unit_price", nonnegative=True)
        if self.order_date is not None:
            _require_date(self.order_date, "order_date")
        if self.expected_delivery_date is not None:
            _require_date(self.expected_delivery_date, "expected_delivery_date")
        _require_optional_text(self.rationale, "rationale")


@dataclass(frozen=True, slots=True)
class ExistingAlert:
    alert_id: int
    description: str

    def __post_init__(self) -> None:
        if not isinstance(self.alert_id, int) or isinstance(self.alert_id, bool):
            raise TypeError("alert_id must be int")
        if self.alert_id <= 0:
            raise ValueError("alert_id must be positive")
        _require_text(self.description, "description")


@dataclass(frozen=True, slots=True)
class RouteInputIssue:
    """Typed provenance for a malformed supplier or catalog source value.

    Unsafe raw values never enter explanations.  Their SQLite storage class
    and digest remain in the immutable snapshot so the quarantine is tied to
    the exact source state without reflecting hostile text into output rows.
    """

    source_table: str
    supplier_id: str
    component_id: str | None
    affected_component_ids: tuple[str, ...]
    field: str
    reason_code: str
    safe_reason: str
    blast_radius: RouteQuarantineScope
    action: str
    remediation: str
    raw_value_type: str
    raw_value_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "source_table",
            "supplier_id",
            "field",
            "reason_code",
            "safe_reason",
            "action",
            "remediation",
            "raw_value_type",
        ):
            _require_text(getattr(self, name), name)
        _require_optional_text(self.component_id, "component_id")
        components = _text_tuple(
            self.affected_component_ids,
            "affected_component_ids",
        )
        _require_unique(components, "affected_component_ids")
        object.__setattr__(self, "affected_component_ids", components)
        if not isinstance(self.blast_radius, RouteQuarantineScope):
            raise TypeError("blast_radius must be RouteQuarantineScope")
        if self.source_table == "supplier_catalog":
            if (
                self.blast_radius is not RouteQuarantineScope.CATALOG_OFFER
                or self.component_id is None
                or components != (self.component_id,)
            ):
                raise ValueError(
                    "catalog issues must quarantine exactly their supplier/component offer"
                )
        elif self.source_table == "suppliers":
            if (
                self.blast_radius is not RouteQuarantineScope.SUPPLIER_ALL_ROUTES
                or self.component_id is not None
            ):
                raise ValueError(
                    "supplier issues must quarantine every route for that supplier"
                )
        else:
            raise ValueError("route input issues may originate only from supplier route tables")
        if len(self.raw_value_sha256) != 64 or any(
            character not in "0123456789abcdef"
            for character in self.raw_value_sha256
        ):
            raise ValueError("raw_value_sha256 must be a lowercase SHA-256 digest")

    @property
    def issue_id(self) -> str:
        """Stable current-state identity without exposing the unsafe raw value."""

        parts = (
            self.source_table,
            self.supplier_id,
            self.component_id or "",
            ",".join(self.affected_component_ids),
            self.field,
            self.reason_code,
            self.blast_radius.value,
            self.raw_value_type,
            self.raw_value_sha256,
        )
        payload = b"".join(
            len(part.encode("utf-8")).to_bytes(8, "big") + part.encode("utf-8")
            for part in parts
        )
        return sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class ScenarioSnapshot:
    configuration: ScenarioConfiguration
    products: tuple[Product, ...]
    components: tuple[Component, ...]
    suppliers: tuple[Supplier, ...]
    bom_lines: tuple[BomLine, ...]
    catalog_lines: tuple[SupplierCatalogLine, ...]
    production_orders: tuple[ProductionOrder, ...]
    inventory: tuple[InventoryPosition, ...]
    purchase_orders: tuple[ExistingPurchaseOrder, ...]
    alerts: tuple[ExistingAlert, ...]
    state_digest: str
    route_input_issues: tuple[RouteInputIssue, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.configuration, ScenarioConfiguration):
            raise TypeError("configuration must be ScenarioConfiguration")
        collections = {
            "products": (self.products, Product, lambda item: item.product_id),
            "components": (self.components, Component, lambda item: item.component_id),
            "suppliers": (self.suppliers, Supplier, lambda item: item.supplier_id),
            "bom_lines": (
                self.bom_lines,
                BomLine,
                lambda item: (item.product_id, item.component_id),
            ),
            "catalog_lines": (
                self.catalog_lines,
                SupplierCatalogLine,
                lambda item: (item.component_id, item.supplier_id),
            ),
            "production_orders": (
                self.production_orders,
                ProductionOrder,
                lambda item: item.order_id,
            ),
            "inventory": (
                self.inventory,
                InventoryPosition,
                lambda item: item.component_id,
            ),
            "purchase_orders": (
                self.purchase_orders,
                ExistingPurchaseOrder,
                lambda item: item.po_number,
            ),
            "alerts": (self.alerts, ExistingAlert, lambda item: item.alert_id),
            "route_input_issues": (
                self.route_input_issues,
                RouteInputIssue,
                lambda item: item.issue_id,
            ),
        }
        for name, (raw_items, item_type, key) in collections.items():
            items = _tuple(raw_items, name)
            if any(not isinstance(item, item_type) for item in items):
                raise TypeError(f"{name} contains an invalid item")
            keys = tuple(key(item) for item in items)
            if len(keys) != len(set(keys)):
                raise ValueError(f"{name} contains duplicate logical keys")
            object.__setattr__(self, name, tuple(sorted(items, key=key)))
        _require_text(self.state_digest, "state_digest")


@dataclass(frozen=True, slots=True)
class DemandContribution:
    order_id: str
    product_id: str
    quantity: Decimal

    def __post_init__(self) -> None:
        _require_text(self.order_id, "order_id")
        _require_text(self.product_id, "product_id")
        _require_decimal(self.quantity, "quantity", positive=True)


@dataclass(frozen=True, slots=True)
class DemandBucket:
    component_id: str
    due_date: date
    bucket_quantity: Decimal
    cumulative_quantity: Decimal
    contributions: tuple[DemandContribution, ...]

    def __post_init__(self) -> None:
        _require_text(self.component_id, "component_id")
        _require_date(self.due_date, "due_date")
        _require_decimal(self.bucket_quantity, "bucket_quantity", positive=True)
        _require_decimal(self.cumulative_quantity, "cumulative_quantity", positive=True)
        if self.cumulative_quantity < self.bucket_quantity:
            raise ValueError("cumulative_quantity cannot be below bucket_quantity")
        contributions = _tuple(self.contributions, "contributions")
        if not contributions or any(
            not isinstance(item, DemandContribution) for item in contributions
        ):
            raise ValueError("contributions must contain DemandContribution values")
        if sum((item.quantity for item in contributions), ZERO) != self.bucket_quantity:
            raise ValueError("contribution quantities must equal bucket_quantity")
        object.__setattr__(
            self,
            "contributions",
            tuple(sorted(contributions, key=lambda item: (item.order_id, item.product_id))),
        )


@dataclass(frozen=True, slots=True)
class InboundSupply:
    po_number: str
    component_id: str
    supplier_id: str
    quantity: Decimal
    expected_delivery_date: date
    order_date: date | None = None
    unit_price: Decimal | None = None
    agent_owned: bool = False
    action_key: str | None = None
    demand_fingerprint: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.po_number, "po_number")
        _require_text(self.component_id, "component_id")
        _require_text(self.supplier_id, "supplier_id")
        _require_decimal(self.quantity, "quantity", positive=True)
        _require_date(self.expected_delivery_date, "expected_delivery_date")
        if self.order_date is not None:
            _require_date(self.order_date, "order_date")
        if self.unit_price is not None:
            _require_decimal(self.unit_price, "unit_price", nonnegative=True)
        if not isinstance(self.agent_owned, bool):
            raise TypeError("agent_owned must be bool")
        _require_optional_text(self.action_key, "action_key")
        _require_optional_text(self.demand_fingerprint, "demand_fingerprint")
        if self.agent_owned and (self.action_key is None or self.demand_fingerprint is None):
            raise ValueError("owned inbound supply requires action and demand fingerprints")


@dataclass(frozen=True, slots=True)
class DeadlineSupplyPosition:
    due_date: date
    cumulative_demand: Decimal
    on_time_supply: Decimal
    on_time_gap: Decimal
    recoverable_gap: Decimal
    committed_late_quantity: Decimal = ZERO
    committed_unit_late_days: Decimal = ZERO

    def __post_init__(self) -> None:
        _require_date(self.due_date, "due_date")
        for name in (
            "cumulative_demand",
            "on_time_supply",
            "on_time_gap",
            "recoverable_gap",
            "committed_late_quantity",
            "committed_unit_late_days",
        ):
            _require_decimal(getattr(self, name), name, nonnegative=True)
        expected_gap = max(ZERO, self.cumulative_demand - self.on_time_supply)
        if self.on_time_gap != expected_gap:
            raise ValueError("on_time_gap must equal max(0, demand - on_time supply)")
        if self.recoverable_gap > self.on_time_gap:
            raise ValueError("recoverable_gap cannot exceed on_time_gap")
        if self.committed_late_quantity > self.on_time_gap:
            raise ValueError("committed late quantity cannot exceed on_time_gap")


@dataclass(frozen=True, slots=True)
class DeadlineLateness:
    """One post-plan deadline miss retained in the managed decision state."""

    due_date: date
    late_quantity: Decimal
    unit_late_days: Decimal
    unresolved_quantity: Decimal = ZERO

    def __post_init__(self) -> None:
        _require_date(self.due_date, "due_date")
        for name in ("late_quantity", "unit_late_days", "unresolved_quantity"):
            _require_decimal(getattr(self, name), name, nonnegative=True)
        if self.late_quantity <= ZERO:
            raise ValueError("deadline lateness requires a positive late quantity")
        if self.unresolved_quantity > self.late_quantity:
            raise ValueError("unresolved quantity cannot exceed late quantity")


@dataclass(frozen=True, slots=True)
class SupplyLedger:
    component_id: str
    total_demand: Decimal
    on_hand: Decimal
    committed_inbound: tuple[InboundSupply, ...]
    eventual_supply: Decimal
    eventual_gap: Decimal
    deadline_positions: tuple[DeadlineSupplyPosition, ...]

    def __post_init__(self) -> None:
        _require_text(self.component_id, "component_id")
        for name in ("total_demand", "on_hand", "eventual_supply", "eventual_gap"):
            _require_decimal(getattr(self, name), name, nonnegative=True)
        inbound = _tuple(self.committed_inbound, "committed_inbound")
        if any(not isinstance(item, InboundSupply) for item in inbound):
            raise TypeError("committed_inbound contains an invalid item")
        if any(item.component_id != self.component_id for item in inbound):
            raise ValueError("all inbound supply must match the ledger component")
        inbound = tuple(
            sorted(inbound, key=lambda item: (item.expected_delivery_date, item.po_number))
        )
        object.__setattr__(self, "committed_inbound", inbound)
        expected_eventual = self.on_hand + sum((item.quantity for item in inbound), ZERO)
        if self.eventual_supply != expected_eventual:
            raise ValueError("eventual_supply must equal on_hand plus committed inbound")
        if self.eventual_gap != max(ZERO, self.total_demand - self.eventual_supply):
            raise ValueError("eventual_gap is inconsistent with demand and eventual supply")
        positions = _tuple(self.deadline_positions, "deadline_positions")
        if any(not isinstance(item, DeadlineSupplyPosition) for item in positions):
            raise TypeError("deadline_positions contains an invalid item")
        if tuple(item.due_date for item in positions) != tuple(
            sorted(item.due_date for item in positions)
        ):
            raise ValueError("deadline_positions must be ordered by due_date")
        object.__setattr__(self, "deadline_positions", positions)


@dataclass(frozen=True, slots=True)
class EvidenceResult:
    rule_id: str
    status: EvidenceStatus
    basis: EvidenceBasis
    scope: EvidenceScope
    severity: RuleSeverity
    summary: str
    source_references: tuple[str, ...] = ()
    assumption_codes: tuple[str, ...] = ()
    contract_disposition: PlanDisposition | None = None

    def __post_init__(self) -> None:
        _require_text(self.rule_id, "rule_id")
        if not isinstance(self.status, EvidenceStatus):
            raise TypeError("status must be EvidenceStatus")
        if not isinstance(self.basis, EvidenceBasis):
            raise TypeError("basis must be EvidenceBasis")
        if not isinstance(self.scope, EvidenceScope):
            raise TypeError("scope must be EvidenceScope")
        if not isinstance(self.severity, RuleSeverity):
            raise TypeError("severity must be RuleSeverity")
        _require_text(self.summary, "summary")
        references = _text_tuple(self.source_references, "source_references")
        assumptions = _text_tuple(self.assumption_codes, "assumption_codes")
        _require_unique(references, "source_references")
        _require_unique(assumptions, "assumption_codes")
        object.__setattr__(self, "source_references", references)
        object.__setattr__(self, "assumption_codes", assumptions)
        if self.contract_disposition is not None and not isinstance(
            self.contract_disposition, PlanDisposition
        ):
            raise TypeError("contract_disposition must be PlanDisposition or None")
        if self.status is not EvidenceStatus.UNKNOWN and self.contract_disposition is not None:
            raise ValueError("only UNKNOWN evidence can carry a contract disposition")
        if self.contract_disposition is PlanDisposition.EXECUTE:
            raise ValueError("unknown evidence may never become unconditional EXECUTE")


@dataclass(frozen=True, slots=True)
class ComparatorTrace:
    stage: int
    comparator: str
    outcome: str
    explanation: str
    compared_route_ids: tuple[str, ...] = ()
    source_rule_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.stage, int) or isinstance(self.stage, bool):
            raise TypeError("stage must be int")
        if self.stage <= 0:
            raise ValueError("stage must be positive")
        _require_text(self.comparator, "comparator")
        _require_text(self.outcome, "outcome")
        _require_text(self.explanation, "explanation")
        object.__setattr__(
            self,
            "compared_route_ids",
            _text_tuple(self.compared_route_ids, "compared_route_ids"),
        )
        object.__setattr__(
            self,
            "source_rule_ids",
            _text_tuple(self.source_rule_ids, "source_rule_ids"),
        )


@dataclass(frozen=True, slots=True)
class DecisionComparatorFact:
    """Structured fact showing why a selected plan beat an option."""

    stage: int
    kind: str
    comparator: str
    outcome: str
    selected_route_ids: tuple[str, ...]
    compared_route_ids: tuple[str, ...]
    rule_ids: tuple[str, ...]
    decisive: bool = False
    quantity_delta: Decimal | None = None
    cost_delta: Decimal | None = None
    delivery_delta_days: int | None = None
    policy_window: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stage, int) or isinstance(self.stage, bool):
            raise TypeError("stage must be int")
        if self.stage < 0:
            raise ValueError("stage must be nonnegative")
        _require_text(self.kind, "kind")
        if self.kind == "quantity_calibration" and self.stage != 0:
            raise ValueError("quantity calibration must use reserved stage 0")
        if self.kind == "route_selection" and not 1 <= self.stage <= 7:
            raise ValueError("route selection must use comparator stages 1 through 7")
        if not isinstance(self.decisive, bool):
            raise TypeError("decisive must be bool")
        _require_text(self.comparator, "comparator")
        _require_text(self.outcome, "outcome")
        for name in ("selected_route_ids", "compared_route_ids", "rule_ids"):
            values = _text_tuple(getattr(self, name), name)
            _require_unique(values, name)
            object.__setattr__(self, name, values)
        if not self.selected_route_ids:
            raise ValueError("selected_route_ids must not be empty")
        if not self.rule_ids and not (
            self.kind == "route_selection" and self.stage == 7
        ):
            raise ValueError(
                "rule_ids may be empty only for the deterministic stage-7 tie-break"
            )
        for name in ("quantity_delta", "cost_delta"):
            value = getattr(self, name)
            if value is not None:
                _require_decimal(value, name)
        if self.delivery_delta_days is not None and (
            not isinstance(self.delivery_delta_days, int)
            or isinstance(self.delivery_delta_days, bool)
        ):
            raise TypeError("delivery_delta_days must be int or None")
        _require_optional_text(self.policy_window, "policy_window")


@dataclass(frozen=True, slots=True)
class MaterialRouteRejection:
    """One material, de-noised route that was considered but not selected."""

    route_id: str
    supplier_id: str
    reason_code: str
    eligibility: EvidenceStatus
    rule_ids: tuple[str, ...]
    unit_price: Decimal
    selected_unit_price: Decimal
    price_delta: Decimal
    material_available_date: date
    selected_material_available_date: date
    delivery_delta_days: int

    def __post_init__(self) -> None:
        for name in ("route_id", "supplier_id", "reason_code"):
            _require_text(getattr(self, name), name)
        if not isinstance(self.eligibility, EvidenceStatus):
            raise TypeError("eligibility must be EvidenceStatus")
        rules = _text_tuple(self.rule_ids, "rule_ids")
        _require_unique(rules, "rule_ids")
        object.__setattr__(self, "rule_ids", rules)
        _require_decimal(self.unit_price, "unit_price", nonnegative=True)
        _require_decimal(self.selected_unit_price, "selected_unit_price", nonnegative=True)
        _require_decimal(self.price_delta, "price_delta")
        if self.price_delta != self.unit_price - self.selected_unit_price:
            raise ValueError("price_delta must equal route price minus selected price")
        _require_date(self.material_available_date, "material_available_date")
        _require_date(
            self.selected_material_available_date,
            "selected_material_available_date",
        )
        if not isinstance(self.delivery_delta_days, int) or isinstance(
            self.delivery_delta_days, bool
        ):
            raise TypeError("delivery_delta_days must be int")
        if self.delivery_delta_days != (
            self.material_available_date - self.selected_material_available_date
        ).days:
            raise ValueError(
                "delivery_delta_days must equal rejected minus selected availability"
            )


@dataclass(frozen=True, slots=True)
class CandidateRoute:
    """One immutable supplier route with separate physical and policy dates.

    ``feasible_deadlines`` records on-time physical arrival, while
    ``exception_scope_deadlines`` records buckets whose predicates permit an
    exception allocation even when material arrives late.
    """

    route_id: str
    component_id: str
    supplier_id: str
    supplier_fingerprint: str
    route_fingerprint: str
    unit_price: Decimal
    minimum_order_quantity: Decimal
    shipping_method: str
    lead_time_days: int
    order_date: date
    expected_delivery_date: date
    material_available_date: date
    eligibility: EvidenceStatus
    feasible_deadlines: tuple[date, ...]
    exception_scope_deadlines: tuple[date, ...]
    evidence: tuple[EvidenceResult, ...]
    exception_codes: tuple[str, ...] = ()
    approval_requirements: tuple[str, ...] = ()
    comparator_trace: tuple[ComparatorTrace, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "route_id",
            "component_id",
            "supplier_id",
            "supplier_fingerprint",
            "route_fingerprint",
            "shipping_method",
        ):
            _require_text(getattr(self, name), name)
        _require_decimal(self.unit_price, "unit_price", nonnegative=True)
        _require_decimal(
            self.minimum_order_quantity,
            "minimum_order_quantity",
            positive=True,
        )
        if not isinstance(self.lead_time_days, int) or isinstance(self.lead_time_days, bool):
            raise TypeError("lead_time_days must be int")
        if self.lead_time_days < 0:
            raise ValueError("lead_time_days must be nonnegative")
        for name in ("order_date", "expected_delivery_date", "material_available_date"):
            _require_date(getattr(self, name), name)
        if self.expected_delivery_date != self.order_date + timedelta(days=self.lead_time_days):
            raise ValueError("expected_delivery_date must use supplier shipping lead only")
        if self.material_available_date < self.expected_delivery_date:
            raise ValueError("material_available_date cannot precede supplier delivery")
        if not isinstance(self.eligibility, EvidenceStatus):
            raise TypeError("eligibility must be EvidenceStatus")
        deadlines = _tuple(self.feasible_deadlines, "feasible_deadlines")
        for deadline in deadlines:
            _require_date(deadline, "feasible deadline")
        object.__setattr__(self, "feasible_deadlines", tuple(sorted(set(deadlines))))
        exception_deadlines = _tuple(
            self.exception_scope_deadlines,
            "exception_scope_deadlines",
        )
        for deadline in exception_deadlines:
            _require_date(deadline, "exception scope deadline")
        object.__setattr__(
            self,
            "exception_scope_deadlines",
            tuple(sorted(set(exception_deadlines))),
        )
        evidence = _tuple(self.evidence, "evidence")
        if any(not isinstance(item, EvidenceResult) for item in evidence):
            raise TypeError("evidence contains an invalid item")
        object.__setattr__(self, "evidence", tuple(sorted(evidence, key=lambda item: item.rule_id)))
        object.__setattr__(
            self,
            "exception_codes",
            _text_tuple(self.exception_codes, "exception_codes"),
        )
        object.__setattr__(
            self,
            "approval_requirements",
            _text_tuple(self.approval_requirements, "approval_requirements"),
        )
        trace = _tuple(self.comparator_trace, "comparator_trace")
        if any(not isinstance(item, ComparatorTrace) for item in trace):
            raise TypeError("comparator_trace contains an invalid item")
        object.__setattr__(
            self,
            "comparator_trace",
            tuple(sorted(trace, key=lambda item: (item.stage, item.comparator))),
        )

    @property
    def may_enter_executable_model(self) -> bool:
        return self.eligibility is EvidenceStatus.PASS and not self.approval_requirements

    @property
    def blocking_hard_evidence(self) -> tuple[EvidenceResult, ...]:
        """Hard evidence that prevents this route from entering solve 1.

        A benchmark rule-level unknown licensed as ``EXECUTE_WITH_ASSUMPTION``
        is deliberately not a blocker.  Keeping the remaining evidence intact
        lets callers distinguish an unavailable fact from a proven supplier
        failure instead of flattening both into ``eligibility != PASS``.
        """

        return tuple(
            item
            for item in self.evidence
            if item.severity is RuleSeverity.HARD
            and (
                item.status is EvidenceStatus.FAIL
                or (
                    item.status is EvidenceStatus.UNKNOWN
                    and not (
                        item.scope is EvidenceScope.RULE
                        and item.contract_disposition
                        is PlanDisposition.EXECUTE_WITH_ASSUMPTION
                    )
                )
            )
        )

    @property
    def contract_disposition(self) -> PlanDisposition | None:
        """The non-executable disposition required by hard unknown evidence."""

        dispositions = {
            item.contract_disposition
            for item in self.blocking_hard_evidence
            if item.status is EvidenceStatus.UNKNOWN
            and item.contract_disposition is not None
        }
        if PlanDisposition.DECISION_REQUIRED in dispositions:
            return PlanDisposition.DECISION_REQUIRED
        if PlanDisposition.RECOMMEND_APPROVAL in dispositions:
            return PlanDisposition.RECOMMEND_APPROVAL
        return None

    @property
    def is_evidence_blocked(self) -> bool:
        """Whether missing contract evidence, rather than failure, blocks use."""

        blockers = self.blocking_hard_evidence
        return (
            bool(blockers)
            and not any(item.status is EvidenceStatus.FAIL for item in blockers)
            and self.contract_disposition is PlanDisposition.DECISION_REQUIRED
        )


@dataclass(frozen=True, slots=True)
class BucketAllocation:
    due_date: date
    quantity: Decimal
    exception_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_date(self.due_date, "due_date")
        _require_decimal(self.quantity, "quantity", positive=True)
        object.__setattr__(
            self,
            "exception_ids",
            _text_tuple(self.exception_ids, "exception_ids"),
        )


@dataclass(frozen=True, slots=True)
class PlanLine:
    route_id: str
    component_id: str
    supplier_id: str
    quantity: Decimal
    unit_price: Decimal
    order_date: date
    expected_delivery_date: date
    material_available_date: date
    allocation_group_id: str
    bucket_allocations: tuple[BucketAllocation, ...]
    approval_rule_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("route_id", "component_id", "supplier_id", "allocation_group_id"):
            _require_text(getattr(self, name), name)
        _require_decimal(self.quantity, "quantity", positive=True)
        _require_decimal(self.unit_price, "unit_price", nonnegative=True)
        for name in ("order_date", "expected_delivery_date", "material_available_date"):
            _require_date(getattr(self, name), name)
        if self.material_available_date < self.expected_delivery_date:
            raise ValueError("material_available_date cannot precede expected_delivery_date")
        allocations = _tuple(self.bucket_allocations, "bucket_allocations")
        if not allocations or any(not isinstance(item, BucketAllocation) for item in allocations):
            raise ValueError("bucket_allocations must contain BucketAllocation values")
        allocations = tuple(sorted(allocations, key=lambda item: item.due_date))
        if sum((item.quantity for item in allocations), ZERO) != self.quantity:
            raise ValueError("bucket allocations must sum exactly to line quantity")
        object.__setattr__(self, "bucket_allocations", allocations)
        object.__setattr__(
            self,
            "approval_rule_ids",
            _text_tuple(self.approval_rule_ids, "approval_rule_ids"),
        )

    @property
    def line_total(self) -> Decimal:
        return self.quantity * self.unit_price


@dataclass(frozen=True, slots=True)
class CandidatePlan:
    plan_id: str
    component_id: str
    disposition: PlanDisposition
    lines: tuple[PlanLine, ...]
    net_requirement: Decimal
    eventual_covered_quantity: Decimal
    residual_gap: Decimal
    total_cost: Decimal
    minimum_compliant_total: Decimal | None = None
    cheapest_covering_cost: Decimal | None = None
    forced_surplus: Decimal = ZERO
    discretionary_surplus: Decimal = ZERO
    recovery_demand: Decimal = ZERO
    recovery_quantity: Decimal = ZERO
    unit_late_days: Decimal = ZERO
    objective_vector: tuple[Decimal, ...] = ()
    relaxed_rule_ids: tuple[str, ...] = ()
    evidence: tuple[EvidenceResult, ...] = ()
    unresolved_approval_ids: tuple[str, ...] = ()
    assumption_codes: tuple[str, ...] = ()
    summary: str = "Candidate plan"

    def __post_init__(self) -> None:
        _require_text(self.plan_id, "plan_id")
        _require_text(self.component_id, "component_id")
        if not isinstance(self.disposition, PlanDisposition):
            raise TypeError("disposition must be PlanDisposition")
        lines = _tuple(self.lines, "lines")
        if not lines or any(not isinstance(item, PlanLine) for item in lines):
            raise ValueError("lines must contain PlanLine values")
        if any(item.component_id != self.component_id for item in lines):
            raise ValueError("all plan lines must match the plan component")
        _require_unique((item.route_id for item in lines), "plan route IDs")
        object.__setattr__(
            self,
            "lines",
            tuple(sorted(lines, key=lambda item: item.route_id)),
        )
        for name in (
            "net_requirement",
            "eventual_covered_quantity",
            "residual_gap",
            "total_cost",
            "forced_surplus",
            "discretionary_surplus",
            "recovery_demand",
            "recovery_quantity",
            "unit_late_days",
        ):
            _require_decimal(getattr(self, name), name, nonnegative=True)
        if self.minimum_compliant_total is not None:
            _require_decimal(
                self.minimum_compliant_total,
                "minimum_compliant_total",
                nonnegative=True,
            )
        if self.cheapest_covering_cost is not None:
            _require_decimal(
                self.cheapest_covering_cost,
                "cheapest_covering_cost",
                nonnegative=True,
            )
        if self.eventual_covered_quantity > self.net_requirement:
            raise ValueError("eventual_covered_quantity cannot exceed net_requirement")
        if self.residual_gap != self.net_requirement - self.eventual_covered_quantity:
            raise ValueError("residual_gap must equal net requirement minus covered quantity")
        if self.recovery_quantity > self.recovery_demand:
            raise ValueError("recovery quantity cannot exceed recovery demand")
        if self.total_cost != sum((item.line_total for item in lines), ZERO):
            raise ValueError("total_cost must exactly equal the sum of line totals")
        objective = _tuple(self.objective_vector, "objective_vector")
        for item in objective:
            _require_decimal(item, "objective value")
        object.__setattr__(self, "objective_vector", objective)
        for name in (
            "relaxed_rule_ids",
            "unresolved_approval_ids",
            "assumption_codes",
        ):
            object.__setattr__(self, name, _text_tuple(getattr(self, name), name))
        evidence = _tuple(self.evidence, "evidence")
        if any(not isinstance(item, EvidenceResult) for item in evidence):
            raise TypeError("evidence contains an invalid item")
        object.__setattr__(self, "evidence", tuple(sorted(evidence, key=lambda item: item.rule_id)))
        _require_text(self.summary, "summary")

        line_approval_ids = {
            rule_id
            for line in lines
            for rule_id in line.approval_rule_ids
        }
        if set(self.unresolved_approval_ids) != line_approval_ids:
            raise ValueError(
                "unresolved_approval_ids must equal the union of line approval rules"
            )
        if (
            self.disposition is PlanDisposition.RECOMMEND_APPROVAL
            and not line_approval_ids
        ):
            raise ValueError("RECOMMEND_APPROVAL requires a line-level approval rule")

        if self.disposition.writes_purchase_order:
            if line_approval_ids:
                raise ValueError("an executable plan cannot have unresolved approvals")
            if self.minimum_compliant_total is None or self.cheapest_covering_cost is None:
                raise ValueError("an executable plan requires certified calibration and baseline values")
        if self.minimum_compliant_total is not None:
            total_quantity = sum((item.quantity for item in lines), ZERO)
            if total_quantity < self.minimum_compliant_total:
                raise ValueError("certified quantity cannot be below the calibrated minimum")
            expected_forced = max(ZERO, self.minimum_compliant_total - self.net_requirement)
            if self.recovery_quantity > max(
                ZERO,
                total_quantity - self.net_requirement,
            ):
                raise ValueError("recovery quantity cannot exceed selected duplicate supply")
            recovery_headroom = max(
                ZERO,
                self.recovery_quantity - expected_forced,
            )
            expected_discretionary = max(
                ZERO,
                total_quantity - self.minimum_compliant_total - recovery_headroom,
            )
            if self.forced_surplus != expected_forced:
                raise ValueError("forced_surplus is inconsistent with calibration")
            if self.discretionary_surplus != expected_discretionary:
                raise ValueError("discretionary_surplus is inconsistent with calibration")
        if self.disposition.writes_purchase_order:
            for result in evidence:
                proven_hard_failure = (
                    result.severity is RuleSeverity.HARD
                    and result.status is EvidenceStatus.FAIL
                )
                candidate_unknown = (
                    result.severity is RuleSeverity.HARD
                    and result.scope is EvidenceScope.CANDIDATE
                    and result.status is EvidenceStatus.UNKNOWN
                )
                if proven_hard_failure or candidate_unknown:
                    raise ValueError("a hard failure or candidate-level unknown cannot execute")
                rule_unknown = (
                    result.severity is RuleSeverity.HARD
                    and result.scope is EvidenceScope.RULE
                    and result.status is EvidenceStatus.UNKNOWN
                )
                if rule_unknown and (
                    result.contract_disposition
                    is not PlanDisposition.EXECUTE_WITH_ASSUMPTION
                    or self.disposition is not PlanDisposition.EXECUTE_WITH_ASSUMPTION
                ):
                    raise ValueError(
                        "hard rule-level unknowns execute only under an assumption contract"
                    )
        if (
            self.disposition is PlanDisposition.EXECUTE_WITH_ASSUMPTION
            and not self.assumption_codes
        ):
            raise ValueError("EXECUTE_WITH_ASSUMPTION requires a named assumption")


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    requirement_id: str
    component_id: str
    evidence_contract: EvidenceContract
    demand_buckets: tuple[DemandBucket, ...]
    supply_ledger: SupplyLedger
    total_requirement: Decimal
    initial_eventual_gap: Decimal
    covered_quantity: Decimal
    residual_gap: Decimal
    requirement_state: RequirementState
    selected_plan: CandidatePlan | None
    alternatives: tuple[CandidatePlan, ...]
    evidence: tuple[EvidenceResult, ...]
    alert_categories: tuple[AlertCategory, ...]
    rationale: str
    deadline_lateness: tuple[DeadlineLateness, ...] = ()
    economic_autonomy: EconomicAutonomyParameters | None = None
    source_fingerprint: str | None = None
    comparator_facts: tuple[DecisionComparatorFact, ...] = ()
    material_rejections: tuple[MaterialRouteRejection, ...] = ()
    normalization_disclosures: tuple[
        SourceEntityNormalizationDisclosure | UnitNormalizationDisclosure, ...
    ] = ()

    def __post_init__(self) -> None:
        _require_text(self.requirement_id, "requirement_id")
        _require_text(self.component_id, "component_id")
        if not isinstance(self.evidence_contract, EvidenceContract):
            raise TypeError("evidence_contract must be EvidenceContract")
        buckets = _tuple(self.demand_buckets, "demand_buckets")
        if not buckets or any(not isinstance(item, DemandBucket) for item in buckets):
            raise ValueError("demand_buckets must contain DemandBucket values")
        buckets = tuple(sorted(buckets, key=lambda item: item.due_date))
        if any(item.component_id != self.component_id for item in buckets):
            raise ValueError("all demand buckets must match the decision component")
        previous = ZERO
        for bucket in buckets:
            if bucket.cumulative_quantity != previous + bucket.bucket_quantity:
                raise ValueError("demand bucket cumulative quantities are inconsistent")
            previous = bucket.cumulative_quantity
        object.__setattr__(self, "demand_buckets", buckets)
        if not isinstance(self.supply_ledger, SupplyLedger):
            raise TypeError("supply_ledger must be SupplyLedger")
        if self.supply_ledger.component_id != self.component_id:
            raise ValueError("supply ledger must match the decision component")
        for name in (
            "total_requirement",
            "initial_eventual_gap",
            "covered_quantity",
            "residual_gap",
        ):
            _require_decimal(getattr(self, name), name, nonnegative=True)
        if self.total_requirement != buckets[-1].cumulative_quantity:
            raise ValueError("total_requirement must equal final cumulative demand")
        if self.total_requirement != self.supply_ledger.total_demand:
            raise ValueError("total_requirement must equal supply-ledger demand")
        if self.initial_eventual_gap != self.supply_ledger.eventual_gap:
            raise ValueError("initial_eventual_gap must equal the supply-ledger gap")
        if self.covered_quantity + self.residual_gap != self.total_requirement:
            raise ValueError("covered quantity plus residual gap must equal the requirement")
        if not isinstance(self.requirement_state, RequirementState):
            raise TypeError("requirement_state must be RequirementState")
        expected_fulfillment = (
            FulfillmentStatus.FULFILLED
            if self.residual_gap == ZERO
            else (
                FulfillmentStatus.UNFULFILLED
                if self.covered_quantity == ZERO
                else FulfillmentStatus.PARTIALLY_FULFILLED
            )
        )
        if self.requirement_state.fulfillment is not expected_fulfillment:
            raise ValueError("requirement fulfillment disagrees with covered and residual quantities")
        if self.selected_plan is not None:
            if not isinstance(self.selected_plan, CandidatePlan):
                raise TypeError("selected_plan must be CandidatePlan or None")
            if not self.selected_plan.disposition.writes_purchase_order:
                raise ValueError("selected_plan must have an executable disposition")
            if self.selected_plan.component_id != self.component_id:
                raise ValueError("selected_plan must match the decision component")
            if self.selected_plan.eventual_covered_quantity != (
                self.initial_eventual_gap - self.residual_gap
            ):
                raise ValueError("selected plan coverage is inconsistent with the residual gap")
        elif self.residual_gap != self.initial_eventual_gap:
            raise ValueError("without a selected plan, the initial eventual gap must remain")
        alternatives = _tuple(self.alternatives, "alternatives")
        if any(not isinstance(item, CandidatePlan) for item in alternatives):
            raise TypeError("alternatives contains an invalid item")
        if any(item.disposition.writes_purchase_order for item in alternatives):
            raise ValueError("alternatives must remain in the recommendation set")
        if any(item.component_id != self.component_id for item in alternatives):
            raise ValueError("all alternatives must match the decision component")
        object.__setattr__(self, "alternatives", tuple(sorted(alternatives, key=lambda item: item.plan_id)))
        evidence = _tuple(self.evidence, "evidence")
        if any(not isinstance(item, EvidenceResult) for item in evidence):
            raise TypeError("evidence contains an invalid item")
        object.__setattr__(self, "evidence", tuple(sorted(evidence, key=lambda item: item.rule_id)))
        categories = _tuple(self.alert_categories, "alert_categories")
        if any(not isinstance(item, AlertCategory) for item in categories):
            raise TypeError("alert_categories contains an invalid item")
        object.__setattr__(self, "alert_categories", tuple(sorted(set(categories), key=lambda item: item.value)))
        _require_text(self.rationale, "rationale")
        lateness = _tuple(self.deadline_lateness, "deadline_lateness")
        if any(not isinstance(item, DeadlineLateness) for item in lateness):
            raise TypeError("deadline_lateness contains an invalid item")
        lateness = tuple(sorted(lateness, key=lambda item: item.due_date))
        _require_unique(
            (item.due_date.isoformat() for item in lateness),
            "deadline_lateness dates",
        )
        if any(item.due_date not in {bucket.due_date for bucket in buckets} for item in lateness):
            raise ValueError("deadline lateness must reference a demand bucket")
        object.__setattr__(self, "deadline_lateness", lateness)
        if self.economic_autonomy is not None and not isinstance(
            self.economic_autonomy, EconomicAutonomyParameters
        ):
            raise TypeError(
                "economic_autonomy must be EconomicAutonomyParameters or None"
            )
        if self.source_fingerprint is not None and not re.fullmatch(
            r"[0-9a-f]{64}", self.source_fingerprint
        ):
            raise ValueError("source_fingerprint must be a lowercase SHA-256 digest or None")
        comparators = _tuple(self.comparator_facts, "comparator_facts")
        if any(not isinstance(item, DecisionComparatorFact) for item in comparators):
            raise TypeError("comparator_facts contains an invalid item")
        quantity_facts = tuple(
            item for item in comparators if item.kind == "quantity_calibration"
        )
        if any(not item.decisive for item in quantity_facts):
            raise ValueError("quantity calibration facts must be decisive")
        route_facts = tuple(
            item for item in comparators if item.kind == "route_selection"
        )
        route_pairs = {
            (item.selected_route_ids, item.compared_route_ids) for item in route_facts
        }
        for pair in route_pairs:
            path = tuple(
                sorted(
                    (
                        item
                        for item in route_facts
                        if (item.selected_route_ids, item.compared_route_ids) == pair
                    ),
                    key=lambda item: item.stage,
                )
            )
            if tuple(item.stage for item in path) != tuple(range(1, 8)):
                raise ValueError("route comparison facts must reproduce stages 1 through 7")
            decisive = tuple(item for item in path if item.decisive)
            if len(decisive) != 1:
                raise ValueError("route comparison facts require exactly one deciding stage")
            deciding_stage = decisive[0].stage
            if any(
                item.stage > deciding_stage
                and item.outcome != f"not_evaluated_after_stage_{deciding_stage}"
                for item in path
            ):
                raise ValueError("route comparator stages after the first difference must not be evaluated")
        object.__setattr__(
            self,
            "comparator_facts",
            tuple(
                sorted(
                    comparators,
                    key=lambda item: (
                        item.stage,
                        item.comparator,
                        item.compared_route_ids,
                    ),
                )
            ),
        )
        rejections = _tuple(self.material_rejections, "material_rejections")
        if any(not isinstance(item, MaterialRouteRejection) for item in rejections):
            raise TypeError("material_rejections contains an invalid item")
        _require_unique(
            (item.supplier_id for item in rejections),
            "material rejection suppliers",
        )
        object.__setattr__(
            self,
            "material_rejections",
            tuple(sorted(rejections, key=lambda item: (item.supplier_id, item.route_id))),
        )
        disclosures = _tuple(
            self.normalization_disclosures, "normalization_disclosures"
        )
        if any(
            not isinstance(
                item,
                (
                    SourceEntityNormalizationDisclosure,
                    UnitNormalizationDisclosure,
                ),
            )
            for item in disclosures
        ):
            raise TypeError(
                "normalization_disclosures contains an invalid item"
            )
        if len(disclosures) != len(set(disclosures)):
            raise ValueError("normalization_disclosures contains duplicates")
        object.__setattr__(
            self,
            "normalization_disclosures",
            tuple(
                sorted(
                    disclosures,
                    key=lambda item: (type(item).__name__, repr(item)),
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class SolverStageResult:
    stage_name: str
    status: SolverStatus
    objective_value: Decimal | None
    mip_gap: Decimal | None
    certificate_complete: bool
    hit_resource_limit: bool
    message: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.stage_name, "stage_name")
        if not isinstance(self.status, SolverStatus):
            raise TypeError("status must be SolverStatus")
        if self.objective_value is not None:
            _require_decimal(self.objective_value, "objective_value")
        if self.mip_gap is not None:
            _require_decimal(self.mip_gap, "mip_gap", nonnegative=True)
        if not isinstance(self.certificate_complete, bool):
            raise TypeError("certificate_complete must be bool")
        if not isinstance(self.hit_resource_limit, bool):
            raise TypeError("hit_resource_limit must be bool")
        _require_optional_text(self.message, "message")
        if self.status is SolverStatus.OPTIMAL:
            if not self.certificate_complete or self.hit_resource_limit or self.mip_gap != ZERO:
                raise ValueError("OPTIMAL requires a completed zero-gap certificate and no limit")
        if self.status is SolverStatus.INFEASIBLE:
            if not self.certificate_complete or self.hit_resource_limit:
                raise ValueError("INFEASIBLE requires a completed certificate and no limit")


@dataclass(frozen=True, slots=True)
class SolverResult:
    component_id: str
    solve_kind: SolveKind
    status: SolverStatus
    stage_results: tuple[SolverStageResult, ...]
    candidate_plan: CandidatePlan | None
    objective_vector: tuple[Decimal, ...]
    minimum_compliant_total: Decimal | None = None
    cheapest_covering_cost: Decimal | None = None
    exact_post_validated: bool = False
    message: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.component_id, "component_id")
        if not isinstance(self.solve_kind, SolveKind):
            raise TypeError("solve_kind must be SolveKind")
        if not isinstance(self.status, SolverStatus):
            raise TypeError("status must be SolverStatus")
        stages = _tuple(self.stage_results, "stage_results")
        if not stages or any(not isinstance(item, SolverStageResult) for item in stages):
            raise ValueError("stage_results must contain SolverStageResult values")
        object.__setattr__(self, "stage_results", stages)
        objective = _tuple(self.objective_vector, "objective_vector")
        for item in objective:
            _require_decimal(item, "objective value")
        object.__setattr__(self, "objective_vector", objective)
        for name in ("minimum_compliant_total", "cheapest_covering_cost"):
            value = getattr(self, name)
            if value is not None:
                _require_decimal(value, name, nonnegative=True)
        if not isinstance(self.exact_post_validated, bool):
            raise TypeError("exact_post_validated must be bool")
        _require_optional_text(self.message, "message")
        if self.candidate_plan is not None:
            if not isinstance(self.candidate_plan, CandidatePlan):
                raise TypeError("candidate_plan must be CandidatePlan or None")
            if self.candidate_plan.component_id != self.component_id:
                raise ValueError("candidate_plan must match the solver component")
            if (
                self.status is not SolverStatus.OPTIMAL
                and self.candidate_plan.disposition.writes_purchase_order
            ):
                raise ValueError("an unproven incumbent must be labelled non-executable")
        if self.status is SolverStatus.OPTIMAL and any(
            item.status is not SolverStatus.OPTIMAL for item in stages
        ):
            raise ValueError("an OPTIMAL solve requires every recorded stage to be optimal")
        if self.status is SolverStatus.INFEASIBLE:
            if self.candidate_plan is not None:
                raise ValueError("an infeasible result cannot carry a candidate plan")
            if not any(item.status is SolverStatus.INFEASIBLE for item in stages):
                raise ValueError("an infeasible result requires an infeasibility certificate")

    @property
    def is_certified_optimal(self) -> bool:
        return self.status is SolverStatus.OPTIMAL and all(
            item.status is SolverStatus.OPTIMAL
            and item.certificate_complete
            and not item.hit_resource_limit
            and item.mip_gap == ZERO
            for item in self.stage_results
        )

    @property
    def has_executable_certificate(self) -> bool:
        """Whether the solver-side proof gate passed.

        Independent ``PlanValidator`` success is still required before commit.
        """

        return (
            self.is_certified_optimal
            and self.exact_post_validated
            and self.candidate_plan is not None
            and self.candidate_plan.disposition.writes_purchase_order
        )


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    severity: ValidationSeverity
    message: str
    component_id: str | None = None
    plan_id: str | None = None
    rule_ids: tuple[str, ...] = ()
    failure_scope: ValidationFailureScope = ValidationFailureScope.UNKNOWN

    def __post_init__(self) -> None:
        _require_text(self.code, "code")
        if not isinstance(self.severity, ValidationSeverity):
            raise TypeError("severity must be ValidationSeverity")
        _require_text(self.message, "message")
        _require_optional_text(self.component_id, "component_id")
        _require_optional_text(self.plan_id, "plan_id")
        object.__setattr__(self, "rule_ids", _text_tuple(self.rule_ids, "rule_ids"))
        if not isinstance(self.failure_scope, ValidationFailureScope):
            raise TypeError("failure_scope must be ValidationFailureScope")


@dataclass(frozen=True, slots=True)
class InternalFailureExclusion:
    """Engineering-owned record for one component removed after validation.

    This is deliberately separate from ``DecisionRecord`` and carries no plan
    disposition, supplier proposal, or approval semantics.
    """

    component_id: str
    requirement_id: str
    issues: tuple[ValidationIssue, ...]
    owner: str = "PROCUREMENT_ENGINEERING"

    def __post_init__(self) -> None:
        _require_text(self.component_id, "component_id")
        _require_text(self.requirement_id, "requirement_id")
        _require_text(self.owner, "owner")
        if self.owner != "PROCUREMENT_ENGINEERING":
            raise ValueError("internal failure exclusions must be engineering-owned")
        issues = _tuple(self.issues, "issues")
        if not issues or any(not isinstance(item, ValidationIssue) for item in issues):
            raise ValueError("issues must contain ValidationIssue values")
        if any(item.severity is not ValidationSeverity.ERROR for item in issues):
            raise ValueError("internal failure exclusions may contain only errors")
        if any(item.component_id != self.component_id for item in issues):
            raise ValueError("every excluded issue must match the affected component")
        if any(
            item.failure_scope is not ValidationFailureScope.COMPONENT_LOCAL
            for item in issues
        ):
            raise ValueError("every excluded issue must be explicitly component-local")
        object.__setattr__(
            self,
            "issues",
            tuple(
                sorted(
                    issues,
                    key=lambda item: (item.code, item.plan_id or "", item.message),
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class ValidationResult:
    completed: bool
    exact_decimal_checks_completed: bool
    solver_results_verified: bool
    checked_invariants: tuple[str, ...]
    issues: tuple[ValidationIssue, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "completed",
            "exact_decimal_checks_completed",
            "solver_results_verified",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be bool")
        invariants = _text_tuple(self.checked_invariants, "checked_invariants")
        _require_unique(invariants, "checked_invariants")
        object.__setattr__(self, "checked_invariants", invariants)
        issues = _tuple(self.issues, "issues")
        if any(not isinstance(item, ValidationIssue) for item in issues):
            raise TypeError("issues contains an invalid item")
        object.__setattr__(
            self,
            "issues",
            tuple(sorted(issues, key=lambda item: (item.severity.value, item.code, item.message))),
        )

    @property
    def is_valid(self) -> bool:
        return (
            self.completed
            and self.exact_decimal_checks_completed
            and self.solver_results_verified
            and not any(item.severity is ValidationSeverity.ERROR for item in self.issues)
        )


@dataclass(frozen=True, slots=True)
class CommitResult:
    committed_po_numbers: tuple[str, ...]
    inserted_alert_count: int
    deleted_alert_count: int
    no_op: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "committed_po_numbers",
            _text_tuple(self.committed_po_numbers, "committed_po_numbers"),
        )
        for name in ("inserted_alert_count", "deleted_alert_count"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name} must be int")
            if value < 0:
                raise ValueError(f"{name} must be nonnegative")
        if not isinstance(self.no_op, bool):
            raise TypeError("no_op must be bool")


__all__ = [
    "AlertCategory",
    "BomLine",
    "BucketAllocation",
    "CandidatePlan",
    "CandidateRoute",
    "CommitResult",
    "ComparatorTrace",
    "Component",
    "DeadlineLateness",
    "DeadlineSupplyPosition",
    "DecisionRecord",
    "DecisionComparatorFact",
    "DemandBucket",
    "DemandContribution",
    "EvidenceBasis",
    "EvidenceResult",
    "EvidenceScope",
    "EvidenceStatus",
    "ExistingAlert",
    "ExistingPurchaseOrder",
    "FulfillmentStatus",
    "InboundSupply",
    "InternalFailureExclusion",
    "InventoryPosition",
    "MaterialRouteRejection",
    "PlanDisposition",
    "PlanLine",
    "Product",
    "ProductionOrder",
    "RequirementState",
    "ResolutionStatus",
    "RouteInputIssue",
    "RouteQuarantineScope",
    "RuleSeverity",
    "ScenarioConfiguration",
    "ScenarioSnapshot",
    "SolveKind",
    "SolverResult",
    "SolverStageResult",
    "SolverStatus",
    "Supplier",
    "SupplierCatalogLine",
    "SupplyLedger",
    "SourceEntityNormalizationDisclosure",
    "UnitNormalizationDisclosure",
    "UnitRoundingRule",
    "VALID_REQUIREMENT_STATES",
    "ValidationIssue",
    "ValidationFailureScope",
    "ValidationResult",
    "ValidationSeverity",
    "ZERO",
]
