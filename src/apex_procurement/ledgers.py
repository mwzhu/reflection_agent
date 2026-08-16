"""Deterministic, time-phased demand and committed-supply ledgers.

This module deliberately stops before supplier eligibility and selection.  A
caller may provide already-computed physical route availability dates when it
wants recoverable lateness classified; the ledger never decides which of
those routes is policy eligible.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_CEILING
import unicodedata

from .domain import (
    AlertCategory,
    BomLine,
    DeadlineSupplyPosition,
    DemandBucket,
    DemandContribution,
    InboundSupply,
    ScenarioSnapshot,
    SupplyLedger,
    ZERO,
)


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    if not value.strip():
        raise ValueError(f"{name} must be non-empty")


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


def _normalise_unit(value: str) -> str:
    _require_text(value, "unit_of_measure")
    return unicodedata.normalize("NFKC", value).strip().casefold()


@dataclass(frozen=True, slots=True)
class UnitOfMeasureContract:
    """Reviewed rounding rules, independent of any supplier or MOQ choice."""

    discrete_units: tuple[str, ...]
    continuous_increments: tuple[tuple[str, Decimal], ...]

    def __post_init__(self) -> None:
        discrete = tuple(sorted({_normalise_unit(item) for item in self.discrete_units}))
        raw_continuous: dict[str, Decimal] = {}
        for unit, increment in self.continuous_increments:
            normalised = _normalise_unit(unit)
            _require_decimal(increment, "continuous increment", positive=True)
            if normalised in raw_continuous:
                raise ValueError(f"duplicate continuous unit {normalised!r}")
            raw_continuous[normalised] = increment
        overlap = set(discrete) & set(raw_continuous)
        if overlap:
            raise ValueError(f"units cannot be both discrete and continuous: {sorted(overlap)!r}")
        object.__setattr__(self, "discrete_units", discrete)
        object.__setattr__(
            self,
            "continuous_increments",
            tuple(sorted(raw_continuous.items())),
        )

    def rule_for(self, unit_of_measure: str) -> tuple[Decimal, bool, bool]:
        """Return ``(increment, discrete, recognised)`` for one unit."""

        normalised = _normalise_unit(unit_of_measure)
        continuous = dict(self.continuous_increments)
        if normalised in continuous:
            return continuous[normalised], False, True
        if normalised in self.discrete_units:
            return Decimal("1"), True, True
        # The conservative fallback is explicit: indivisible until configured.
        return Decimal("1"), True, False


DEFAULT_UNIT_OF_MEASURE_CONTRACT = UnitOfMeasureContract(
    discrete_units=("each", "tube", "can"),
    continuous_increments=(("kg", Decimal("0.01")), ("meter", Decimal("0.01"))),
)


@dataclass(frozen=True, slots=True)
class LedgerAlert:
    """A deterministic warning discovered while constructing physical ledgers."""

    category: AlertCategory
    code: str
    description: str
    component_id: str | None = None
    po_number: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.category, AlertCategory):
            raise TypeError("category must be AlertCategory")
        _require_text(self.code, "code")
        _require_text(self.description, "description")
        if self.component_id is not None:
            _require_text(self.component_id, "component_id")
        if self.po_number is not None:
            _require_text(self.po_number, "po_number")


@dataclass(frozen=True, slots=True)
class RouteAvailability:
    """Policy-neutral material availability for a possible new route."""

    route_id: str
    component_id: str
    material_available_date: date

    def __post_init__(self) -> None:
        _require_text(self.route_id, "route_id")
        _require_text(self.component_id, "component_id")
        _require_date(self.material_available_date, "material_available_date")


@dataclass(frozen=True, slots=True)
class QuantityDecision:
    """The auditable aggregate -> round -> MOQ quantity calculation."""

    aggregate_quantity: Decimal
    rounded_quantity: Decimal
    minimum_order_quantity: Decimal
    order_quantity: Decimal
    unit_of_measure: str
    is_discrete: bool
    used_unknown_unit_assumption: bool

    def __post_init__(self) -> None:
        for name in (
            "aggregate_quantity",
            "rounded_quantity",
            "minimum_order_quantity",
            "order_quantity",
        ):
            _require_decimal(getattr(self, name), name, nonnegative=True)
        _require_text(self.unit_of_measure, "unit_of_measure")
        if not isinstance(self.is_discrete, bool):
            raise TypeError("is_discrete must be bool")
        if not isinstance(self.used_unknown_unit_assumption, bool):
            raise TypeError("used_unknown_unit_assumption must be bool")
        if self.rounded_quantity < self.aggregate_quantity:
            raise ValueError("rounded_quantity cannot be below aggregate_quantity")
        if self.order_quantity < self.rounded_quantity:
            raise ValueError("order_quantity cannot be below rounded_quantity")
        if self.order_quantity < self.minimum_order_quantity:
            raise ValueError("order_quantity cannot be below minimum_order_quantity")


@dataclass(frozen=True, slots=True)
class LedgerBuildResult:
    """Immutable ledgers plus non-fatal construction disclosures."""

    demand_buckets: tuple[DemandBucket, ...]
    supply_ledgers: tuple[SupplyLedger, ...]
    alerts: tuple[LedgerAlert, ...]

    def __post_init__(self) -> None:
        buckets = tuple(self.demand_buckets)
        ledgers = tuple(self.supply_ledgers)
        alerts = tuple(self.alerts)
        if any(not isinstance(item, DemandBucket) for item in buckets):
            raise TypeError("demand_buckets contains an invalid item")
        if any(not isinstance(item, SupplyLedger) for item in ledgers):
            raise TypeError("supply_ledgers contains an invalid item")
        if any(not isinstance(item, LedgerAlert) for item in alerts):
            raise TypeError("alerts contains an invalid item")
        buckets = tuple(sorted(buckets, key=lambda item: (item.component_id, item.due_date)))
        ledgers = tuple(sorted(ledgers, key=lambda item: item.component_id))
        if len({item.component_id for item in ledgers}) != len(ledgers):
            raise ValueError("supply_ledgers contains duplicate components")
        if len({(item.component_id, item.due_date) for item in buckets}) != len(buckets):
            raise ValueError("demand_buckets contains duplicate component deadlines")
        ledger_components = {item.component_id for item in ledgers}
        if any(item.component_id not in ledger_components for item in buckets):
            raise ValueError("every demand bucket requires a matching supply ledger")
        alerts = tuple(
            sorted(
                alerts,
                key=lambda item: (
                    item.category.value,
                    item.code,
                    item.component_id or "",
                    item.po_number or "",
                    item.description,
                ),
            )
        )
        object.__setattr__(self, "demand_buckets", buckets)
        object.__setattr__(self, "supply_ledgers", ledgers)
        object.__setattr__(self, "alerts", alerts)

    def buckets_for(self, component_id: str) -> tuple[DemandBucket, ...]:
        _require_text(component_id, "component_id")
        return tuple(
            item for item in self.demand_buckets if item.component_id == component_id
        )

    def ledger_for(self, component_id: str) -> SupplyLedger:
        _require_text(component_id, "component_id")
        for ledger in self.supply_ledgers:
            if ledger.component_id == component_id:
                return ledger
        raise KeyError(component_id)


def _ceil_to_increment(quantity: Decimal, increment: Decimal) -> Decimal:
    if quantity == ZERO:
        return ZERO
    units = (quantity / increment).to_integral_value(rounding=ROUND_CEILING)
    return units * increment


def aggregate_round_and_apply_moq(
    quantities: Iterable[Decimal],
    *,
    unit_of_measure: str,
    minimum_order_quantity: Decimal,
    contract: UnitOfMeasureContract = DEFAULT_UNIT_OF_MEASURE_CONTRACT,
) -> QuantityDecision:
    """Aggregate exact quantities, round once, then enforce the route MOQ.

    The MOQ is rounded to the same physical increment so a fractional MOQ can
    never force a fractional discrete order.
    """

    if not isinstance(contract, UnitOfMeasureContract):
        raise TypeError("contract must be UnitOfMeasureContract")
    _require_decimal(minimum_order_quantity, "minimum_order_quantity", positive=True)
    values = tuple(quantities)
    for index, value in enumerate(values):
        _require_decimal(value, f"quantities[{index}]", nonnegative=True)
    aggregate = sum(values, ZERO)
    increment, is_discrete, recognised = contract.rule_for(unit_of_measure)
    rounded = _ceil_to_increment(aggregate, increment)
    rounded_moq = _ceil_to_increment(minimum_order_quantity, increment)
    return QuantityDecision(
        aggregate_quantity=aggregate,
        rounded_quantity=rounded,
        minimum_order_quantity=minimum_order_quantity,
        order_quantity=max(rounded, rounded_moq),
        unit_of_measure=unit_of_measure,
        is_discrete=is_discrete,
        used_unknown_unit_assumption=not recognised,
    )


def recovery_surplus(ledger: SupplyLedger, proposed_order_quantity: Decimal) -> Decimal:
    """Return quantity bought beyond the ledger's eventual baseline gap."""

    if not isinstance(ledger, SupplyLedger):
        raise TypeError("ledger must be SupplyLedger")
    _require_decimal(
        proposed_order_quantity,
        "proposed_order_quantity",
        nonnegative=True,
    )
    return max(ZERO, proposed_order_quantity - ledger.eventual_gap)


def existing_surplus(ledger: SupplyLedger) -> Decimal:
    """Return committed physical supply beyond total confirmed demand."""

    if not isinstance(ledger, SupplyLedger):
        raise TypeError("ledger must be SupplyLedger")
    return max(ZERO, ledger.eventual_supply - ledger.total_demand)


def unit_late_days(quantity: Decimal, available_date: date, due_date: date) -> Decimal:
    """Calculate exact unit-late-days for one quantity/date pairing."""

    _require_decimal(quantity, "quantity", nonnegative=True)
    _require_date(available_date, "available_date")
    _require_date(due_date, "due_date")
    return quantity * Decimal(max(0, (available_date - due_date).days))


def _alert(
    category: AlertCategory,
    code: str,
    description: str,
    *,
    component_id: str | None = None,
    po_number: str | None = None,
) -> LedgerAlert:
    return LedgerAlert(
        category=category,
        code=code,
        description=description,
        component_id=component_id,
        po_number=po_number,
    )


def _build_demand(
    snapshot: ScenarioSnapshot,
) -> tuple[dict[str, tuple[DemandBucket, ...]], list[LedgerAlert]]:
    bom_by_product: dict[str, list[BomLine]] = defaultdict(list)
    for line in snapshot.bom_lines:
        bom_by_product[line.product_id].append(line)

    raw: dict[tuple[str, date], list[DemandContribution]] = defaultdict(list)
    alerts: list[LedgerAlert] = []
    if not snapshot.production_orders:
        alerts.append(
            _alert(
                AlertCategory.DATA_QUALITY,
                "EMPTY_PRODUCTION_SCHEDULE",
                "The production schedule is empty; no component demand was generated.",
            )
        )
    for production_order in snapshot.production_orders:
        lines = bom_by_product.get(production_order.product_id, [])
        if not lines:
            alerts.append(
                _alert(
                    AlertCategory.DATA_QUALITY,
                    "PRODUCT_WITHOUT_BOM",
                    "A scheduled product has no BOM lines; its order generated no demand.",
                )
            )
            continue
        if production_order.materials_needed_by < snapshot.configuration.current_date:
            alerts.append(
                _alert(
                    AlertCategory.DATA_QUALITY,
                    "PAST_MATERIAL_DEADLINE",
                    "A production order's materials-needed-by date precedes the scenario date.",
                )
            )
        for line in lines:
            quantity = production_order.quantity * line.quantity_per
            raw[(line.component_id, production_order.materials_needed_by)].append(
                DemandContribution(
                    order_id=production_order.order_id,
                    product_id=production_order.product_id,
                    quantity=quantity,
                )
            )

    by_component: dict[str, list[DemandBucket]] = defaultdict(list)
    cumulative: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for (component_id, due_date), contributions in sorted(raw.items()):
        bucket_quantity = sum((item.quantity for item in contributions), ZERO)
        cumulative[component_id] += bucket_quantity
        by_component[component_id].append(
            DemandBucket(
                component_id=component_id,
                due_date=due_date,
                bucket_quantity=bucket_quantity,
                cumulative_quantity=cumulative[component_id],
                contributions=tuple(contributions),
            )
        )
    return (
        {component_id: tuple(items) for component_id, items in by_component.items()},
        alerts,
    )


def _recoverable_gap(
    *,
    due_date: date,
    on_time_gap: Decimal,
    inbound: tuple[InboundSupply, ...],
    earliest_new_route: date | None,
) -> Decimal:
    if on_time_gap == ZERO or earliest_new_route is None:
        return ZERO

    remaining_gap = on_time_gap
    recoverable = ZERO
    # Earliest committed receipts serve the earliest unmet demand first.  A
    # route can bridge only committed quantity it strictly improves upon;
    # uncovered eventual demand remains baseline purchasing, not recovery.
    for supply in inbound:
        if supply.expected_delivery_date <= due_date:
            continue
        covered = min(remaining_gap, supply.quantity)
        if earliest_new_route < supply.expected_delivery_date:
            recoverable += covered
        remaining_gap -= covered
        if remaining_gap == ZERO:
            break
    return recoverable


def build_ledgers(
    snapshot: ScenarioSnapshot,
    *,
    route_availabilities: Iterable[RouteAvailability] = (),
    unit_contract: UnitOfMeasureContract = DEFAULT_UNIT_OF_MEASURE_CONTRACT,
) -> LedgerBuildResult:
    """Explode a typed snapshot into cumulative demand and supply ledgers.

    Existing inbound is included solely by stored expected delivery date.  A
    route availability input is optional because policy evaluation happens in
    a later stage; when absent, recoverability is conservatively zero.
    """

    if not isinstance(snapshot, ScenarioSnapshot):
        raise TypeError("snapshot must be ScenarioSnapshot")
    if not isinstance(unit_contract, UnitOfMeasureContract):
        raise TypeError("unit_contract must be UnitOfMeasureContract")

    demand_by_component, alerts = _build_demand(snapshot)
    component_by_id = {item.component_id: item for item in snapshot.components}
    inventory_by_component = {
        item.component_id: item.quantity_on_hand for item in snapshot.inventory
    }
    catalog_components = {item.component_id for item in snapshot.catalog_lines}

    route_dates: dict[str, list[date]] = defaultdict(list)
    for availability in tuple(route_availabilities):
        if not isinstance(availability, RouteAvailability):
            raise TypeError("route_availabilities contains an invalid item")
        if availability.component_id not in component_by_id:
            raise ValueError(
                f"route availability references unknown component {availability.component_id!r}"
            )
        route_dates[availability.component_id].append(
            availability.material_available_date
        )

    inbound_by_component: dict[str, list[InboundSupply]] = defaultdict(list)
    current_date = snapshot.configuration.current_date
    for purchase_order in snapshot.purchase_orders:
        delivery_date = purchase_order.expected_delivery_date
        if delivery_date is None:
            alerts.append(
                _alert(
                    AlertCategory.DATA_QUALITY,
                    "UNDATED_COMMITTED_INBOUND",
                    "Committed inbound has no expected delivery date and was excluded.",
                    component_id=purchase_order.component_id,
                    po_number=purchase_order.po_number,
                )
            )
            continue
        if delivery_date < current_date:
            alerts.append(
                _alert(
                    AlertCategory.DATA_QUALITY,
                    "OVERDUE_INBOUND_PENDING_RECONCILIATION",
                    "Committed inbound predates the scenario date and was excluded pending reconciliation.",
                    component_id=purchase_order.component_id,
                    po_number=purchase_order.po_number,
                )
            )
            continue
        inbound_by_component[purchase_order.component_id].append(
            InboundSupply(
                po_number=purchase_order.po_number,
                component_id=purchase_order.component_id,
                supplier_id=purchase_order.supplier_id,
                quantity=purchase_order.quantity,
                expected_delivery_date=delivery_date,
                order_date=purchase_order.order_date,
                unit_price=purchase_order.unit_price,
            )
        )

    ledgers: list[SupplyLedger] = []
    all_buckets: list[DemandBucket] = []
    for component_id in sorted(demand_by_component):
        buckets = demand_by_component[component_id]
        all_buckets.extend(buckets)
        component = component_by_id[component_id]
        _, _, recognised_unit = unit_contract.rule_for(component.unit_of_measure)
        if not recognised_unit:
            alerts.append(
                _alert(
                    AlertCategory.ASSUMPTION,
                    "UNKNOWN_UNIT_TREATED_AS_DISCRETE",
                    "The unit of measure is unrecognised and will be rounded as discrete.",
                    component_id=component_id,
                )
            )
        if component_id not in inventory_by_component:
            alerts.append(
                _alert(
                    AlertCategory.DATA_QUALITY,
                    "MISSING_INVENTORY_POSITION",
                    "No inventory row exists for this demanded component; on-hand was treated as zero.",
                    component_id=component_id,
                )
            )
        if component_id not in catalog_components:
            alerts.append(
                _alert(
                    AlertCategory.DATA_QUALITY,
                    "NO_CATALOG_ROUTE",
                    "No supplier catalog row exists for this demanded component.",
                    component_id=component_id,
                )
            )
        on_hand = inventory_by_component.get(component_id, ZERO)
        inbound = tuple(
            sorted(
                inbound_by_component.get(component_id, []),
                key=lambda item: (item.expected_delivery_date, item.po_number),
            )
        )
        total_demand = buckets[-1].cumulative_quantity
        eventual_supply = on_hand + sum((item.quantity for item in inbound), ZERO)
        earliest_new_route = min(route_dates[component_id], default=None)
        positions: list[DeadlineSupplyPosition] = []
        for bucket in buckets:
            on_time_supply = on_hand + sum(
                (
                    item.quantity
                    for item in inbound
                    if item.expected_delivery_date <= bucket.due_date
                ),
                ZERO,
            )
            on_time_gap = max(ZERO, bucket.cumulative_quantity - on_time_supply)
            positions.append(
                DeadlineSupplyPosition(
                    due_date=bucket.due_date,
                    cumulative_demand=bucket.cumulative_quantity,
                    on_time_supply=on_time_supply,
                    on_time_gap=on_time_gap,
                    recoverable_gap=_recoverable_gap(
                        due_date=bucket.due_date,
                        on_time_gap=on_time_gap,
                        inbound=inbound,
                        earliest_new_route=earliest_new_route,
                    ),
                )
            )
        ledgers.append(
            SupplyLedger(
                component_id=component_id,
                total_demand=total_demand,
                on_hand=on_hand,
                committed_inbound=inbound,
                eventual_supply=eventual_supply,
                eventual_gap=max(ZERO, total_demand - eventual_supply),
                deadline_positions=tuple(positions),
            )
        )

    return LedgerBuildResult(
        demand_buckets=tuple(all_buckets),
        supply_ledgers=tuple(ledgers),
        alerts=tuple(alerts),
    )


__all__ = [
    "DEFAULT_UNIT_OF_MEASURE_CONTRACT",
    "LedgerAlert",
    "LedgerBuildResult",
    "QuantityDecision",
    "RouteAvailability",
    "UnitOfMeasureContract",
    "aggregate_round_and_apply_moq",
    "build_ledgers",
    "existing_surplus",
    "recovery_surplus",
    "unit_late_days",
]
