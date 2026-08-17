from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import shutil
import tempfile
import unittest

from apex_procurement.domain import (
    AlertCategory,
    BomLine,
    Component,
    ExistingPurchaseOrder,
    InventoryPosition,
    Product,
    ProductionOrder,
    ScenarioConfiguration,
    Supplier,
    SupplierCatalogLine,
    UnitNormalizationDisclosure,
    UnitRoundingRule,
)
from apex_procurement.ledgers import (
    RouteAvailability,
    aggregate_round_and_apply_moq,
    build_ledgers,
    existing_surplus,
    recovery_surplus,
    unit_late_days,
)
from apex_procurement.repository import SQLiteRepository
from apex_procurement.snapshot import build_snapshot


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_ROOT = PROJECT_ROOT / "data" / "scenarios"


def component(component_id: str = "part-alpha", unit: str = "each") -> Component:
    return Component(
        component_id=component_id,
        name=f"Component {component_id}",
        description=None,
        category=None,
        unit_of_measure=unit,
        is_hazardous=False,
    )


def product(product_id: str = "product-alpha") -> Product:
    return Product(
        product_id=product_id,
        name=f"Product {product_id}",
        description=None,
        category=None,
        unit_price=None,
    )


def supplier() -> Supplier:
    return Supplier(
        supplier_id="supplier-alpha",
        name="Supplier Alpha",
        country=None,
        is_domestic=None,
        certifications=(),
        sustainability_rating=None,
        relationship_tier=None,
        on_approved_list=True,
    )


def purchase_order(
    po_number: str,
    quantity: str,
    delivery_date: date | None,
    *,
    component_id: str = "part-alpha",
    order_date: date | None = date(2034, 8, 1),
    rationale: str | None = None,
) -> ExistingPurchaseOrder:
    return ExistingPurchaseOrder(
        po_number=po_number,
        component_id=component_id,
        supplier_id="supplier-alpha",
        quantity=Decimal(quantity),
        unit_price=Decimal("1.25"),
        order_date=order_date,
        expected_delivery_date=delivery_date,
        rationale=rationale,
    )


def synthetic_snapshot(
    *,
    current_date: date,
    components: tuple[Component, ...] | None = None,
    products: tuple[Product, ...] | None = None,
    bom_lines: tuple[BomLine, ...] | None = None,
    production_orders: tuple[ProductionOrder, ...] | None = None,
    inventory: tuple[InventoryPosition, ...] = (),
    purchase_orders: tuple[ExistingPurchaseOrder, ...] = (),
    with_catalog: bool = True,
):
    selected_components = components or (component(),)
    selected_products = products or (product(),)
    selected_bom = bom_lines or (
        BomLine("product-alpha", "part-alpha", Decimal("1")),
    )
    selected_orders = production_orders or (
        ProductionOrder(
            order_id="order-alpha",
            product_id="product-alpha",
            quantity=Decimal("10"),
            customer=None,
            materials_needed_by=current_date + timedelta(days=9),
        ),
    )
    catalog = (
        tuple(
            SupplierCatalogLine(
                supplier_id="supplier-alpha",
                component_id=item.component_id,
                unit_price=Decimal("1.25"),
                lead_time_days=4,
                minimum_order_quantity=Decimal("1"),
            )
            for item in selected_components
        )
        if with_catalog
        else ()
    )
    return build_snapshot(
        configuration=ScenarioConfiguration(current_date),
        products=selected_products,
        components=selected_components,
        suppliers=(supplier(),),
        bom_lines=selected_bom,
        catalog_lines=catalog,
        production_orders=selected_orders,
        inventory=inventory,
        purchase_orders=purchase_orders,
        alerts=(),
    )


class SuppliedFixtureLedgerTests(unittest.TestCase):
    def load_fixture(self, name: str):
        source = SCENARIO_ROOT / name
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        target = Path(temporary_directory.name) / name
        shutil.copy2(source, target)
        return SQLiteRepository().load_snapshot(target)

    def test_documented_eventual_shortage_counts(self) -> None:
        expected = {
            "scenario_01_baseline.sqlite": 13,
            "scenario_02_partial_procurement.sqlite": 11,
            "scenario_03_tight_timeline.sqlite": 17,
            "scenario_04_low_inventory.sqlite": 19,
            "scenario_05_competing_demand.sqlite": 17,
            "scenario_06_simple.sqlite": 2,
        }
        for name, count in expected.items():
            with self.subTest(scenario=name):
                result = build_ledgers(self.load_fixture(name))
                self.assertEqual(
                    sum(ledger.eventual_gap > 0 for ledger in result.supply_ledgers),
                    count,
                )

    def test_scenario_one_early_magnet_shortage_has_240_unit_late_day_floor(self) -> None:
        snapshot = self.load_fixture("scenario_01_baseline.sqlite")
        result = build_ledgers(snapshot)
        magnet = next(
            item
            for item in snapshot.components
            if "neodymium" in item.name.casefold()
        )
        ledger = result.ledger_for(magnet.component_id)
        early = ledger.deadline_positions[0]
        fastest_catalog_lead = min(
            line.lead_time_days
            for line in snapshot.catalog_lines
            if line.component_id == magnet.component_id
        )
        fastest_arrival = snapshot.configuration.current_date + timedelta(
            days=fastest_catalog_lead
        )

        self.assertEqual(early.cumulative_demand, Decimal("200"))
        self.assertEqual(early.on_time_supply, Decimal("120"))
        self.assertEqual(early.on_time_gap, Decimal("80"))
        self.assertEqual(fastest_arrival, date(2025, 9, 15))
        self.assertEqual(
            unit_late_days(early.on_time_gap, fastest_arrival, early.due_date),
            Decimal("240"),
        )

    def test_scenario_two_nets_all_four_august_orders_by_delivery_date(self) -> None:
        snapshot = self.load_fixture("scenario_02_partial_procurement.sqlite")
        result = build_ledgers(snapshot)
        magnet = next(
            item
            for item in snapshot.components
            if "neodymium" in item.name.casefold()
        )
        magnet_ledger = result.ledger_for(magnet.component_id)

        self.assertTrue(all(po.order_date.month == 8 for po in snapshot.purchase_orders))
        self.assertEqual(
            sum(len(item.committed_inbound) for item in result.supply_ledgers),
            4,
        )
        self.assertEqual(len(magnet_ledger.committed_inbound), 2)
        self.assertEqual(magnet_ledger.eventual_supply, Decimal("270"))
        self.assertEqual(magnet_ledger.eventual_gap, Decimal("58"))
        self.assertEqual(
            sum(item.eventual_gap > 0 for item in result.supply_ledgers),
            11,
        )

    def test_fractional_demand_is_exact_and_cumulative_buckets_do_not_double_count(self) -> None:
        snapshot = self.load_fixture("scenario_01_baseline.sqlite")
        result = build_ledgers(snapshot)
        coating = next(
            item for item in snapshot.components if "coating" in item.name.casefold()
        )
        buckets = result.buckets_for(coating.component_id)

        self.assertEqual(
            [item.bucket_quantity for item in buckets],
            [Decimal("6.25"), Decimal("5"), Decimal("4")],
        )
        self.assertEqual(
            [item.cumulative_quantity for item in buckets],
            [Decimal("6.25"), Decimal("11.25"), Decimal("15.25")],
        )
        self.assertEqual(
            sum((item.bucket_quantity for item in buckets), Decimal("0")),
            Decimal("15.25"),
        )
        self.assertEqual(
            result.ledger_for(coating.component_id).total_demand,
            Decimal("15.25"),
        )


class InboundAndRecoveryTests(unittest.TestCase):
    def test_delivery_boundary_is_inclusive_while_overdue_and_null_are_excluded(self) -> None:
        scenario_date = date(2035, 1, 1)
        snapshot = synthetic_snapshot(
            current_date=scenario_date,
            purchase_orders=(
                purchase_order("arrives-today", "2", scenario_date),
                purchase_order("already-overdue", "3", scenario_date - timedelta(days=1)),
                purchase_order("missing-date", "4", None),
            ),
        )
        result = build_ledgers(snapshot)
        ledger = result.ledger_for("part-alpha")

        self.assertEqual(
            tuple(item.po_number for item in ledger.committed_inbound),
            ("arrives-today",),
        )
        self.assertEqual(ledger.eventual_supply, Decimal("2"))
        self.assertEqual(ledger.deadline_positions[0].on_time_supply, Decimal("2"))
        alerts = {item.code: item for item in result.alerts}
        self.assertEqual(
            alerts["OVERDUE_INBOUND_PENDING_RECONCILIATION"].category,
            AlertCategory.DATA_QUALITY,
        )
        self.assertEqual(
            alerts["UNDATED_COMMITTED_INBOUND"].category,
            AlertCategory.DATA_QUALITY,
        )

    def test_late_inbound_closes_eventual_gap_but_only_strictly_earlier_route_recovers(self) -> None:
        scenario_date = date(2035, 1, 1)
        due_date = date(2035, 1, 10)
        committed_arrival = date(2035, 1, 20)
        snapshot = synthetic_snapshot(
            current_date=scenario_date,
            production_orders=(
                ProductionOrder(
                    "order-alpha",
                    "product-alpha",
                    Decimal("10"),
                    None,
                    due_date,
                ),
            ),
            purchase_orders=(purchase_order("committed-late", "10", committed_arrival),),
        )

        earlier = build_ledgers(
            snapshot,
            route_availabilities=(
                RouteAvailability("route-early", "part-alpha", date(2035, 1, 15)),
            ),
        ).ledger_for("part-alpha")
        equal = build_ledgers(
            snapshot,
            route_availabilities=(
                RouteAvailability("route-equal", "part-alpha", committed_arrival),
            ),
        ).ledger_for("part-alpha")
        later = build_ledgers(
            snapshot,
            route_availabilities=(
                RouteAvailability("route-later", "part-alpha", date(2035, 1, 25)),
            ),
        ).ledger_for("part-alpha")

        self.assertEqual(earlier.eventual_gap, Decimal("0"))
        self.assertEqual(earlier.deadline_positions[0].on_time_gap, Decimal("10"))
        self.assertEqual(earlier.deadline_positions[0].recoverable_gap, Decimal("10"))
        self.assertEqual(equal.deadline_positions[0].recoverable_gap, Decimal("0"))
        self.assertEqual(later.deadline_positions[0].recoverable_gap, Decimal("0"))
        self.assertEqual(recovery_surplus(earlier, Decimal("10")), Decimal("10"))

    def test_rerun_with_committed_recovery_order_does_not_recover_again(self) -> None:
        scenario_date = date(2035, 1, 1)
        snapshot = synthetic_snapshot(
            current_date=scenario_date,
            purchase_orders=(
                purchase_order("original-late", "10", date(2035, 1, 20)),
                purchase_order(
                    "agent-recovery",
                    "10",
                    date(2035, 1, 15),
                    rationale="Previously committed bridge order.",
                ),
            ),
        )
        ledger = build_ledgers(
            snapshot,
            route_availabilities=(
                RouteAvailability("same-best-route", "part-alpha", date(2035, 1, 15)),
            ),
        ).ledger_for("part-alpha")

        self.assertEqual(ledger.deadline_positions[0].on_time_gap, Decimal("10"))
        self.assertEqual(ledger.deadline_positions[0].recoverable_gap, Decimal("0"))
        self.assertEqual(existing_surplus(ledger), Decimal("10"))

    def test_recovery_is_quantity_phased_across_multiple_committed_arrivals(self) -> None:
        scenario_date = date(2035, 1, 1)
        snapshot = synthetic_snapshot(
            current_date=scenario_date,
            purchase_orders=(
                purchase_order("less-late", "4", date(2035, 1, 14)),
                purchase_order("more-late", "6", date(2035, 1, 20)),
            ),
        )
        ledger = build_ledgers(
            snapshot,
            route_availabilities=(
                RouteAvailability("middle-route", "part-alpha", date(2035, 1, 16)),
            ),
        ).ledger_for("part-alpha")

        self.assertEqual(ledger.deadline_positions[0].recoverable_gap, Decimal("6"))


class QuantityAndGeneralisationTests(unittest.TestCase):
    def test_unknown_unit_rounds_as_discrete_with_assumption_alert(self) -> None:
        scenario_date = date(2041, 6, 1)
        unknown_component = component(unit="box")
        snapshot = synthetic_snapshot(
            current_date=scenario_date,
            components=(unknown_component,),
            production_orders=(
                ProductionOrder(
                    "order-one",
                    "product-alpha",
                    Decimal("0.4"),
                    None,
                    date(2041, 6, 20),
                ),
                ProductionOrder(
                    "order-two",
                    "product-alpha",
                    Decimal("0.4"),
                    None,
                    date(2041, 6, 20),
                ),
            ),
        )
        result = build_ledgers(snapshot)
        quantity = aggregate_round_and_apply_moq(
            (Decimal("0.4"), Decimal("0.4")),
            unit_of_measure="box",
            minimum_order_quantity=Decimal("3"),
        )

        self.assertEqual(result.ledger_for("part-alpha").total_demand, Decimal("0.8"))
        self.assertEqual(quantity.aggregate_quantity, Decimal("0.8"))
        self.assertEqual(quantity.rounded_quantity, Decimal("1"))
        self.assertEqual(quantity.order_quantity, Decimal("3"))
        self.assertTrue(quantity.is_discrete)
        self.assertTrue(quantity.used_unknown_unit_assumption)
        self.assertIn(
            (AlertCategory.ASSUMPTION, "UNKNOWN_UNIT_TREATED_AS_DISCRETE"),
            {(item.category, item.code) for item in result.alerts},
        )
        alert = next(
            item
            for item in result.alerts
            if item.code == "UNKNOWN_UNIT_TREATED_AS_DISCRETE"
        )
        self.assertEqual(
            alert.normalization_disclosure,
            UnitNormalizationDisclosure(
                source_unit="box",
                increment=Decimal("1"),
                rounding_rule=(
                    UnitRoundingRule.DISCRETE_CEILING_AFTER_AGGREGATION
                ),
            ),
        )
        self.assertIn("before MOQ", alert.description)

    def test_aggregate_then_round_avoids_per_order_double_rounding(self) -> None:
        discrete = aggregate_round_and_apply_moq(
            (Decimal("0.4"), Decimal("0.4")),
            unit_of_measure="can",
            minimum_order_quantity=Decimal("1"),
        )
        continuous = aggregate_round_and_apply_moq(
            (Decimal("0.004"), Decimal("0.004")),
            unit_of_measure="kg",
            minimum_order_quantity=Decimal("0.001"),
        )

        self.assertEqual(discrete.rounded_quantity, Decimal("1"))
        self.assertEqual(discrete.order_quantity, Decimal("1"))
        self.assertFalse(discrete.used_unknown_unit_assumption)
        self.assertEqual(continuous.aggregate_quantity, Decimal("0.008"))
        self.assertEqual(continuous.rounded_quantity, Decimal("0.01"))
        self.assertEqual(continuous.order_quantity, Decimal("0.01"))

    def test_moq_is_applied_after_rounding(self) -> None:
        below_moq = aggregate_round_and_apply_moq(
            (Decimal("4.2"),),
            unit_of_measure="each",
            minimum_order_quantity=Decimal("5"),
        )
        above_moq = aggregate_round_and_apply_moq(
            (Decimal("5.2"),),
            unit_of_measure="each",
            minimum_order_quantity=Decimal("5"),
        )

        self.assertEqual(below_moq.rounded_quantity, Decimal("5"))
        self.assertEqual(below_moq.order_quantity, Decimal("5"))
        self.assertEqual(above_moq.rounded_quantity, Decimal("6"))
        self.assertEqual(above_moq.order_quantity, Decimal("6"))

    def test_empty_inventory_and_shared_bom_use_arbitrary_ids_and_future_dates(self) -> None:
        shared_component = component("shared-fastener", "each")
        products = (product("assembly-red"), product("assembly-blue"))
        deadline_one = date(2047, 2, 17)
        deadline_two = date(2047, 3, 29)
        snapshot = synthetic_snapshot(
            current_date=date(2047, 1, 3),
            components=(shared_component,),
            products=products,
            bom_lines=(
                BomLine("assembly-red", "shared-fastener", Decimal("1.5")),
                BomLine("assembly-blue", "shared-fastener", Decimal("0.25")),
            ),
            production_orders=(
                ProductionOrder(
                    "red-demand",
                    "assembly-red",
                    Decimal("2"),
                    None,
                    deadline_one,
                ),
                ProductionOrder(
                    "blue-demand",
                    "assembly-blue",
                    Decimal("4"),
                    None,
                    deadline_two,
                ),
            ),
            inventory=(),
        )
        result = build_ledgers(snapshot)
        ledger = result.ledger_for("shared-fastener")

        self.assertEqual(ledger.on_hand, Decimal("0"))
        self.assertEqual(ledger.total_demand, Decimal("4"))
        self.assertEqual(
            [item.cumulative_demand for item in ledger.deadline_positions],
            [Decimal("3.0"), Decimal("4.00")],
        )
        self.assertEqual(
            [item.on_time_gap for item in ledger.deadline_positions],
            [Decimal("3.0"), Decimal("4.00")],
        )
        self.assertIn(
            "MISSING_INVENTORY_POSITION",
            {item.code for item in result.alerts},
        )

    def test_on_hand_is_allocated_to_earliest_deadline_once(self) -> None:
        snapshot = synthetic_snapshot(
            current_date=date(2037, 1, 1),
            production_orders=(
                ProductionOrder(
                    "early-order",
                    "product-alpha",
                    Decimal("8"),
                    None,
                    date(2037, 1, 10),
                ),
                ProductionOrder(
                    "later-order",
                    "product-alpha",
                    Decimal("7"),
                    None,
                    date(2037, 2, 10),
                ),
            ),
            inventory=(InventoryPosition("part-alpha", Decimal("10")),),
        )
        ledger = build_ledgers(snapshot).ledger_for("part-alpha")

        self.assertEqual(
            [item.cumulative_demand for item in ledger.deadline_positions],
            [Decimal("8"), Decimal("15")],
        )
        self.assertEqual(
            [item.on_time_gap for item in ledger.deadline_positions],
            [Decimal("0"), Decimal("5")],
        )


if __name__ == "__main__":
    unittest.main()
