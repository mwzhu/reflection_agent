from __future__ import annotations

from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from apex_procurement.config import EvidenceContract
from apex_procurement.decisions import (
    AtomicDecisionWriter,
    CommitStep,
    DecisionError,
    build_decision_outputs,
    demand_fingerprint,
    parse_owned_purchase_order,
    reconcile_managed_decisions,
    reconstruct_managed_decisions,
)
from apex_procurement.domain import (
    AlertCategory,
    BucketAllocation,
    CandidatePlan,
    DeadlineSupplyPosition,
    DecisionRecord,
    DemandBucket,
    DemandContribution,
    FulfillmentStatus,
    InboundSupply,
    PlanDisposition,
    PlanLine,
    RequirementState,
    ResolutionStatus,
    SupplyLedger,
    ValidationResult,
)
from apex_procurement.explanations import make_owned_alert, render_alerts
from apex_procurement.repository import SQLiteRepository


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_SCENARIO = PROJECT_ROOT / "data" / "scenarios" / "scenario_06_simple.sqlite"
PLANNING_DATE = date(2025, 9, 1)
DUE_DATE = date(2025, 10, 15)


def valid_validation() -> ValidationResult:
    return ValidationResult(
        completed=True,
        exact_decimal_checks_completed=True,
        solver_results_verified=True,
        checked_invariants=("exact-commercial-fields", "solver-certificate"),
    )


def make_line(
    *,
    quantity: Decimal = Decimal("20"),
    unit_price: Decimal = Decimal("32"),
    supplier_id: str = "SUP-112",
    route_id: str = "standard-route",
) -> PlanLine:
    return PlanLine(
        route_id=route_id,
        component_id="CMP-014",
        supplier_id=supplier_id,
        quantity=quantity,
        unit_price=unit_price,
        order_date=PLANNING_DATE,
        expected_delivery_date=date(2025, 9, 15),
        material_available_date=date(2025, 9, 15),
        allocation_group_id="allocation-one",
        bucket_allocations=(BucketAllocation(DUE_DATE, quantity),),
    )


def make_plan(
    *,
    disposition: PlanDisposition = PlanDisposition.EXECUTE,
    quantity: Decimal = Decimal("20"),
    covered: Decimal = Decimal("20"),
    residual: Decimal = Decimal("0"),
    assumption_codes: tuple[str, ...] = (),
    unresolved_approval_ids: tuple[str, ...] = (),
    plan_id: str = "selected-plan",
) -> CandidatePlan:
    line = make_line(quantity=quantity)
    executable = disposition.writes_purchase_order
    return CandidatePlan(
        plan_id=plan_id,
        component_id="CMP-014",
        disposition=disposition,
        lines=(line,),
        net_requirement=Decimal("20"),
        eventual_covered_quantity=covered,
        residual_gap=residual,
        total_cost=line.line_total,
        minimum_compliant_total=Decimal("20") if executable else None,
        cheapest_covering_cost=Decimal("640") if executable else None,
        forced_surplus=Decimal("0"),
        discretionary_surplus=Decimal("0"),
        assumption_codes=assumption_codes,
        unresolved_approval_ids=unresolved_approval_ids,
    )


def make_decision(
    *,
    selected_plan: CandidatePlan | None | object = ..., 
    alternatives: tuple[CandidatePlan, ...] = (),
    residual: Decimal = Decimal("0"),
    resolution: ResolutionStatus = ResolutionStatus.RESOLVED,
    alerts: tuple[AlertCategory, ...] = (),
    on_hand: Decimal = Decimal("20"),
    inbound: tuple[InboundSupply, ...] = (),
    demand_quantity: Decimal = Decimal("40"),
) -> DecisionRecord:
    bucket = DemandBucket(
        component_id="CMP-014",
        due_date=DUE_DATE,
        bucket_quantity=demand_quantity,
        cumulative_quantity=demand_quantity,
        contributions=(
            DemandContribution("PO-5001", "FG-1004", demand_quantity),
        ),
    )
    eventual_supply = on_hand + sum((item.quantity for item in inbound), Decimal("0"))
    initial_gap = max(Decimal("0"), demand_quantity - eventual_supply)
    if selected_plan is ...:
        selected = make_plan()
    else:
        selected = selected_plan
    covered_quantity = demand_quantity - residual
    fulfillment = (
        FulfillmentStatus.FULFILLED
        if residual == 0
        else (
            FulfillmentStatus.UNFULFILLED
            if covered_quantity == 0
            else FulfillmentStatus.PARTIALLY_FULFILLED
        )
    )
    return DecisionRecord(
        requirement_id="requirement-cmp-014",
        component_id="CMP-014",
        evidence_contract=EvidenceContract.BENCHMARK,
        demand_buckets=(bucket,),
        supply_ledger=SupplyLedger(
            component_id="CMP-014",
            total_demand=demand_quantity,
            on_hand=on_hand,
            committed_inbound=inbound,
            eventual_supply=eventual_supply,
            eventual_gap=initial_gap,
            deadline_positions=(
                DeadlineSupplyPosition(
                    due_date=DUE_DATE,
                    cumulative_demand=demand_quantity,
                    on_time_supply=min(demand_quantity, eventual_supply),
                    on_time_gap=max(Decimal("0"), demand_quantity - eventual_supply),
                    recoverable_gap=Decimal("0"),
                ),
            ),
        ),
        total_requirement=demand_quantity,
        initial_eventual_gap=initial_gap,
        covered_quantity=covered_quantity,
        residual_gap=residual,
        requirement_state=RequirementState(fulfillment, resolution),
        selected_plan=selected,  # type: ignore[arg-type]
        alternatives=alternatives,
        evidence=(),
        alert_categories=alerts,
        rationale="upstream prose is deliberately ignored",
    )


class TemporaryScenarioTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "scenario.sqlite"
        shutil.copy2(SOURCE_SCENARIO, self.path)

    def table_rows(self, table: str) -> tuple[tuple[object, ...], ...]:
        if table not in {"purchase_orders", "alerts"}:
            raise AssertionError("test helper permits only output tables")
        with closing(sqlite3.connect(self.path)) as connection:
            return tuple(connection.execute(f"SELECT * FROM {table} ORDER BY 1"))


class IdentityAndRenderingTests(unittest.TestCase):
    def test_fingerprint_ignores_owned_inbound_but_changes_with_demand_or_inventory(self) -> None:
        original = make_decision()
        owned = InboundSupply(
            po_number="APX-12345678",
            component_id="CMP-014",
            supplier_id="SUP-112",
            quantity=Decimal("20"),
            expected_delivery_date=date(2025, 9, 15),
            order_date=PLANNING_DATE,
            unit_price=Decimal("32"),
            agent_owned=True,
            action_key="a" * 64,
            demand_fingerprint="b" * 64,
        )
        with_owned = make_decision(selected_plan=None, inbound=(owned,))
        changed_inventory = make_decision(
            selected_plan=None,
            residual=Decimal("1"),
            resolution=ResolutionStatus.UNRESOLVED,
            on_hand=Decimal("39"),
        )
        changed_demand = make_decision(
            selected_plan=None,
            residual=Decimal("1"),
            resolution=ResolutionStatus.UNRESOLVED,
            on_hand=Decimal("40"),
            demand_quantity=Decimal("41"),
        )

        self.assertEqual(demand_fingerprint(original), demand_fingerprint(with_owned))
        self.assertNotEqual(demand_fingerprint(original), demand_fingerprint(changed_inventory))
        self.assertNotEqual(demand_fingerprint(original), demand_fingerprint(changed_demand))

    def test_owned_po_round_trips_full_record_and_selected_line_state(self) -> None:
        output = build_decision_outputs((make_decision(),), "policy-version-one")
        target = output.purchase_orders[0]
        from apex_procurement.domain import ExistingPurchaseOrder

        stored = ExistingPurchaseOrder(
            po_number=target.po_number,
            component_id=target.component_id,
            supplier_id=target.supplier_id,
            quantity=target.quantity,
            unit_price=target.unit_price,
            order_date=target.order_date,
            expected_delivery_date=target.expected_delivery_date,
            rationale=target.rationale,
        )
        parsed = parse_owned_purchase_order(stored)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.action_key, target.action_key)
        self.assertEqual(parsed.decision, output.decisions[0])
        self.assertRegex(target.po_number, r"\AAPX-[0-9a-f]{8}\Z")
        self.assertIn("Fulfillment FULFILLED; resolution RESOLVED", target.rationale)

    def test_residual_assumption_decision_and_solver_alerts_are_all_owned(self) -> None:
        selected = make_plan(
            disposition=PlanDisposition.EXECUTE_WITH_ASSUMPTION,
            covered=Decimal("10"),
            residual=Decimal("10"),
            assumption_codes=("rolling-history-absent",),
        )
        alternative = make_plan(
            disposition=PlanDisposition.DECISION_REQUIRED,
            plan_id="decision-alternative",
        )
        decision = make_decision(
            selected_plan=selected,
            alternatives=(alternative,),
            residual=Decimal("10"),
            resolution=ResolutionStatus.UNRESOLVED,
            alerts=(AlertCategory.SOLVER_UNPROVEN, AlertCategory.DECISION_REQUIRED),
        )
        alerts = render_alerts((decision,))
        categories = {item.category for item in alerts}

        self.assertTrue(
            {
                AlertCategory.UNMET_DEMAND,
                AlertCategory.ASSUMPTION,
                AlertCategory.DECISION_REQUIRED,
                AlertCategory.SOLVER_UNPROVEN,
                AlertCategory.RUN_ACCOUNTING,
            }.issubset(categories)
        )
        self.assertEqual(
            sum(item.category is AlertCategory.RUN_ACCOUNTING for item in alerts),
            1,
        )
        self.assertTrue(all("[APEX_ALERT:v1" in item.description for item in alerts))

    def test_approval_alert_is_a_complete_proposal_from_policy_facts(self) -> None:
        proposal = make_plan(
            disposition=PlanDisposition.RECOMMEND_APPROVAL,
            unresolved_approval_ids=("POL-PROC-001.section_7.manager_approval",),
            plan_id="approval-proposal",
        )
        decision = make_decision(
            selected_plan=None,
            alternatives=(proposal,),
            residual=Decimal("20"),
            resolution=ResolutionStatus.UNRESOLVED,
            alerts=(AlertCategory.APPROVAL_REQUIRED,),
        )
        alert = next(
            item
            for item in render_alerts((decision,))
            if item.category is AlertCategory.APPROVAL_REQUIRED
        )
        for expected in (
            "supplier SUP-112",
            "quantity 20",
            "unit price 32",
            "line total 640",
            "expected delivery 2025-09-15",
            "line total exceeds 50000",
            "Procurement Manager must approve",
        ):
            self.assertIn(expected, alert.description)


class AtomicCommitTests(TemporaryScenarioTestCase):
    def test_unchanged_second_run_reconstructs_records_and_writes_nothing(self) -> None:
        original = make_decision()
        snapshot = SQLiteRepository().load_snapshot(self.path)
        first = AtomicDecisionWriter(self.path, "policy-version-one").commit(
            snapshot, (original,), valid_validation()
        )
        first_rows = (self.table_rows("purchase_orders"), self.table_rows("alerts"))
        after_first = SQLiteRepository().load_snapshot(self.path)
        reconstructed = reconstruct_managed_decisions(after_first)
        alert_ids = {item.description: item.alert_id for item in after_first.alerts}
        stored_order = after_first.purchase_orders[0]
        parsed = parse_owned_purchase_order(stored_order)
        assert parsed is not None
        owned_inbound = InboundSupply(
            po_number=stored_order.po_number,
            component_id=stored_order.component_id,
            supplier_id=stored_order.supplier_id,
            quantity=stored_order.quantity,
            expected_delivery_date=stored_order.expected_delivery_date,  # type: ignore[arg-type]
            order_date=stored_order.order_date,
            unit_price=stored_order.unit_price,
            agent_owned=True,
            action_key=parsed.action_key,
            demand_fingerprint=parsed.demand_fingerprint,
        )
        closed_by_owned_inbound = make_decision(
            selected_plan=None,
            inbound=(owned_inbound,),
        )
        reconciled = reconcile_managed_decisions(
            after_first,
            (closed_by_owned_inbound,),
            "policy-version-one",
        )

        second = AtomicDecisionWriter(self.path, "policy-version-one").commit(
            after_first, reconciled, valid_validation()
        )
        after_second = SQLiteRepository().load_snapshot(self.path)

        self.assertEqual(first.committed_po_numbers, (first_rows[0][0][0],))
        self.assertEqual(reconstructed, build_decision_outputs((original,), "policy-version-one").decisions)
        self.assertEqual(reconciled, reconstructed)
        self.assertTrue(second.no_op)
        self.assertEqual(second.committed_po_numbers, ())
        self.assertEqual(second.inserted_alert_count, 0)
        self.assertEqual(second.deleted_alert_count, 0)
        self.assertEqual(first_rows, (self.table_rows("purchase_orders"), self.table_rows("alerts")))
        self.assertEqual(
            alert_ids,
            {item.description: item.alert_id for item in after_second.alerts},
        )

    def test_alert_reconciliation_inserts_missing_deletes_obsolete_and_preserves_external(self) -> None:
        decision = make_decision(alerts=(AlertCategory.DATA_QUALITY,))
        writer = AtomicDecisionWriter(self.path, "policy-version-one")
        writer.commit(SQLiteRepository().load_snapshot(self.path), (decision,), valid_validation())
        original = SQLiteRepository().load_snapshot(self.path)
        owned = list(original.alerts)
        preserved = owned[0]
        missing = owned[-1]
        obsolete = make_owned_alert(
            AlertCategory.DATA_QUALITY,
            "obsolete:scope",
            "Obsolete managed alert used by the reconciliation test.",
        )
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DELETE FROM alerts WHERE alert_id = ?", (missing.alert_id,))
            connection.execute(
                "INSERT INTO alerts (description) VALUES (?)", (obsolete.description,)
            )
            connection.execute(
                "INSERT INTO alerts (description) VALUES (?)", ("External operator alert",)
            )
        before = SQLiteRepository().load_snapshot(self.path)
        external = next(item for item in before.alerts if item.description == "External operator alert")

        result = writer.commit(before, (decision,), valid_validation())
        after = SQLiteRepository().load_snapshot(self.path)
        descriptions = {item.description: item.alert_id for item in after.alerts}

        self.assertEqual(result.inserted_alert_count, 1)
        self.assertEqual(result.deleted_alert_count, 1)
        self.assertEqual(descriptions[preserved.description], preserved.alert_id)
        self.assertIn(missing.description, descriptions)
        self.assertNotIn(obsolete.description, descriptions)
        self.assertEqual(descriptions[external.description], external.alert_id)

    def test_short_key_collision_with_external_po_is_a_hard_failure(self) -> None:
        target = build_decision_outputs((make_decision(),), "policy-version-one").purchase_orders[0]
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "INSERT INTO purchase_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    target.po_number,
                    target.component_id,
                    target.supplier_id,
                    "10",
                    "32",
                    PLANNING_DATE.isoformat(),
                    date(2025, 9, 15).isoformat(),
                    "external row using a colliding number",
                ),
            )
        snapshot = SQLiteRepository().load_snapshot(self.path)

        with self.assertRaises(DecisionError):
            AtomicDecisionWriter(self.path, "policy-version-one").commit(
                snapshot, (make_decision(),), valid_validation()
            )
        self.assertEqual(len(self.table_rows("purchase_orders")), 1)
        self.assertEqual(len(self.table_rows("alerts")), 0)

    def test_valid_action_marker_with_changed_business_fields_is_a_hard_failure(self) -> None:
        decision = make_decision()
        writer = AtomicDecisionWriter(self.path, "policy-version-one")
        writer.commit(SQLiteRepository().load_snapshot(self.path), (decision,), valid_validation())
        before_alerts = self.table_rows("alerts")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE purchase_orders SET quantity = quantity + 1")
        corrupted = SQLiteRepository().load_snapshot(self.path)

        with self.assertRaises(DecisionError):
            writer.commit(corrupted, (decision,), valid_validation())

        self.assertEqual(self.table_rows("purchase_orders")[0][3], 21.0)
        self.assertEqual(self.table_rows("alerts"), before_alerts)

    def test_malformed_owned_alert_rolls_back_a_preceding_po_insert(self) -> None:
        malformed = (
            "Forged marker [APEX_ALERT:v1 key=" + "0" * 64
            + " category=DATA_QUALITY scope=Zm9yZ2Vk]"
        )
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("INSERT INTO alerts (description) VALUES (?)", (malformed,))
        snapshot = SQLiteRepository().load_snapshot(self.path)

        with self.assertRaises(DecisionError):
            AtomicDecisionWriter(self.path, "policy-version-one").commit(
                snapshot, (make_decision(),), valid_validation()
            )

        self.assertEqual(self.table_rows("purchase_orders"), ())
        self.assertEqual(self.table_rows("alerts"), ((1, malformed),))

    def test_failure_after_each_transaction_step_rolls_back_all_outputs(self) -> None:
        for step in CommitStep:
            with self.subTest(step=step.value):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "scenario.sqlite"
                    shutil.copy2(SOURCE_SCENARIO, path)

                    def fail(observed: CommitStep, expected: CommitStep = step) -> None:
                        if observed is expected:
                            raise RuntimeError(f"forced failure after {expected.value}")

                    writer = AtomicDecisionWriter(
                        path,
                        "policy-version-one",
                        step_hook=fail,
                    )
                    with self.assertRaisesRegex(RuntimeError, "forced failure"):
                        writer.commit(
                            SQLiteRepository().load_snapshot(path),
                            (make_decision(),),
                            valid_validation(),
                        )
                    with closing(sqlite3.connect(path)) as connection:
                        self.assertEqual(
                            connection.execute("SELECT count(*) FROM purchase_orders").fetchone()[0],
                            0,
                        )
                        self.assertEqual(
                            connection.execute("SELECT count(*) FROM alerts").fetchone()[0],
                            0,
                        )


if __name__ == "__main__":
    unittest.main()
