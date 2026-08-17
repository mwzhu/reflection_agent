from __future__ import annotations

from contextlib import closing, redirect_stdout
from dataclasses import replace
from decimal import Decimal
from io import StringIO
from importlib import metadata
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import apex_procurement.decisions as decisions_module
from apex_procurement.cli import PlanningFailure, main, run
from apex_procurement.config import EvidenceContract, RuntimeConfig
from apex_procurement.domain import (
    AlertCategory,
    EvidenceStatus,
    PlanDisposition,
    SolveKind,
    ValidationIssue,
    ValidationSeverity,
)
from apex_procurement.explanations import (
    ParsedAlertMarker,
    parse_owned_alert,
    render_decision_rationale,
    render_line_rationale,
)
from apex_procurement.validator import IndependentPlanValidator


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "data" / "scenarios" / "scenario_06_simple.sqlite"
SCENARIO_02 = (
    PROJECT_ROOT / "data" / "scenarios" / "scenario_02_partial_procurement.sqlite"
)
SCENARIO_04 = (
    PROJECT_ROOT / "data" / "scenarios" / "scenario_04_low_inventory.sqlite"
)
ASSIGNED_SCENARIOS = (
    PROJECT_ROOT / "data" / "scenarios" / "scenario_01_baseline.sqlite",
    PROJECT_ROOT / "data" / "scenarios" / "scenario_03_tight_timeline.sqlite",
)
IDEMPOTENCY_SCENARIOS = (
    PROJECT_ROOT / "data" / "scenarios" / "scenario_01_baseline.sqlite",
    PROJECT_ROOT / "data" / "scenarios" / "scenario_06_simple.sqlite",
)
ALL_SCENARIOS = tuple(
    sorted((PROJECT_ROOT / "data" / "scenarios").glob("scenario_*.sqlite"))
)
EXPECTED_OPERATIONAL_ALERT_CATEGORIES = {
    "scenario_01_baseline.sqlite": (
        "CAPACITY_UNKNOWN",
        "EVIDENCE_CONTRACT",
        "LATE_ARRIVAL",
    ),
    "scenario_02_partial_procurement.sqlite": (
        "CAPACITY_UNKNOWN",
        "COST_OPPORTUNITY",
        "EVIDENCE_CONTRACT",
    ),
    "scenario_03_tight_timeline.sqlite": (
        "CAPACITY_UNKNOWN",
        "EVIDENCE_CONTRACT",
        "LATE_ARRIVAL",
        "LATE_ARRIVAL",
        "LATE_ARRIVAL",
        "LATE_ARRIVAL",
    ),
    "scenario_04_low_inventory.sqlite": (
        "CAPACITY_UNKNOWN",
        "COST_OPPORTUNITY",
        "EVIDENCE_CONTRACT",
        "LATE_ARRIVAL",
    ),
    "scenario_05_competing_demand.sqlite": (
        "CAPACITY_UNKNOWN",
        "COST_OPPORTUNITY",
        "EVIDENCE_CONTRACT",
        "LATE_ARRIVAL",
    ),
    "scenario_06_simple.sqlite": ("EVIDENCE_CONTRACT",),
}


def output_rows(path: Path) -> tuple[tuple[object, ...], tuple[object, ...]]:
    with closing(sqlite3.connect(path)) as connection:
        orders = tuple(connection.execute("SELECT * FROM purchase_orders ORDER BY 1"))
        alerts = tuple(connection.execute("SELECT * FROM alerts ORDER BY 1"))
    return orders, alerts


def owned_alert_audits(path: Path) -> tuple[ParsedAlertMarker, ...]:
    with closing(sqlite3.connect(path)) as connection:
        rows = tuple(
            connection.execute(
                "SELECT alert_key, category, scope, audit_description "
                "FROM apex_alert_metadata ORDER BY alert_id"
            )
        )
    return tuple(
        ParsedAlertMarker(key, AlertCategory(category), scope, audit_description)
        for key, category, scope, audit_description in rows
    )


class AssembledCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "scenario.sqlite"
        shutil.copy2(SOURCE, self.path)
        # Exercise the entire pipeline without entering the separately tested
        # optimizer/validator objective boundary: every component is already
        # physically covered in this application fixture.
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE inventory SET quantity_on_hand = ?", (1_000_000,))
            connection.commit()

    def command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "agent.py"), *arguments],
            cwd="/",
            check=False,
            capture_output=True,
            text=True,
        )

    def recovery_scenario(self, name: str, *, lead_days: int = 14) -> Path:
        scenario = Path(self.temporary_directory.name) / name
        shutil.copy2(SOURCE, scenario)
        with closing(sqlite3.connect(scenario)) as connection:
            connection.execute(
                "UPDATE inventory SET quantity_on_hand = 0 "
                "WHERE component_id = 'CMP-014'"
            )
            connection.execute(
                "UPDATE supplier_catalog SET lead_time_days = ? "
                "WHERE component_id = 'CMP-014' AND supplier_id = 'SUP-112'",
                (lead_days,),
            )
            connection.execute(
                "INSERT INTO purchase_orders "
                "(po_number, component_id, supplier_id, quantity, unit_price, "
                "order_date, expected_delivery_date, rationale) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "EXT-LATE-COMMITMENT",
                    "CMP-014",
                    "SUP-112",
                    40,
                    32,
                    "2025-09-01",
                    "2025-10-25",
                    "External committed receipt; the agent must not modify it.",
                ),
            )
            connection.commit()
        return scenario

    def isolated_housing_scenario(
        self,
        name: str,
        *,
        requirement: int,
    ) -> Path:
        """Build a temporary exact CMP-008 requirement at the catalog $85."""

        scenario = Path(self.temporary_directory.name) / name
        shutil.copy2(SOURCE, scenario)
        with closing(sqlite3.connect(scenario)) as connection:
            connection.execute(
                "UPDATE production_schedule SET product_id = ?, quantity = ?",
                ("FG-1002", requirement),
            )
            connection.execute(
                "DELETE FROM bom WHERE product_id = ? AND component_id <> ?",
                ("FG-1002", "CMP-008"),
            )
            connection.execute(
                "UPDATE bom SET quantity_per = 1 "
                "WHERE product_id = ? AND component_id = ?",
                ("FG-1002", "CMP-008"),
            )
            connection.execute(
                "UPDATE inventory SET quantity_on_hand = 0 "
                "WHERE component_id = ?",
                ("CMP-008",),
            )
            connection.commit()
        return scenario

    def source_scenario(self, name: str) -> Path:
        scenario = Path(self.temporary_directory.name) / name
        shutil.copy2(SOURCE, scenario)
        return scenario

    def make_catalog_fields_nullable(self, scenario: Path) -> None:
        with closing(sqlite3.connect(scenario)) as connection, connection:
            connection.execute(
                "ALTER TABLE supplier_catalog RENAME TO old_supplier_catalog"
            )
            connection.execute(
                "CREATE TABLE supplier_catalog ("
                "supplier_id TEXT, component_id TEXT, unit_price, lead_time_days, "
                "minimum_order_qty, notes TEXT)"
            )
            connection.execute(
                "INSERT INTO supplier_catalog "
                "SELECT supplier_id, component_id, unit_price, lead_time_days, "
                "minimum_order_qty, notes FROM old_supplier_catalog"
            )
            connection.execute("DROP TABLE old_supplier_catalog")

    def source_data_quality_alerts(self, scenario: Path) -> tuple[str, ...]:
        return tuple(
            item.body
            for item in owned_alert_audits(scenario)
            if item.category is AlertCategory.DATA_QUALITY
            and "Source table " in item.body
        )

    def test_dry_run_is_offline_deterministic_and_writes_nothing(self) -> None:
        before = output_rows(self.path)
        arguments = ["--scenario", str(self.path), "--dry-run", "--json"]
        stdout = StringIO()
        with patch.object(socket, "socket", side_effect=AssertionError("network attempted")):
            with redirect_stdout(stdout):
                first_code = main(arguments)
        first = stdout.getvalue()
        stdout = StringIO()
        with patch.object(socket, "socket", side_effect=AssertionError("network attempted")):
            with redirect_stdout(stdout):
                second_code = main(arguments)
        second = stdout.getvalue()

        self.assertEqual(first_code, 0)
        self.assertEqual(second_code, 0)
        self.assertEqual(first, second)
        self.assertEqual(output_rows(self.path), before)

    def test_default_commit_is_idempotent(self) -> None:
        first = self.command("--scenario", str(self.path), "--json")
        rows_after_first = output_rows(self.path)
        second = self.command("--scenario", str(self.path), "--json")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(output_rows(self.path), rows_after_first)
        self.assertIn('"no_op":true', second.stdout)

    def test_first_rerun_after_managed_orders_is_an_exact_no_op(self) -> None:
        for source in IDEMPOTENCY_SCENARIOS:
            with self.subTest(scenario=source.name):
                scenario = Path(self.temporary_directory.name) / f"rerun-{source.name}"
                shutil.copy2(source, scenario)

                first = self.command(
                    "--scenario",
                    str(scenario),
                    "--contract=benchmark",
                    "--llm=off",
                    "--json",
                )
                rows_after_first = output_rows(scenario)
                second = self.command(
                    "--scenario",
                    str(scenario),
                    "--contract=benchmark",
                    "--llm=off",
                    "--json",
                )

                self.assertEqual(first.returncode, 0, first.stderr)
                self.assertGreater(len(rows_after_first[0]), 0)
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual(output_rows(scenario), rows_after_first)
                self.assertIn('"inserted_alert_count":0', second.stdout)
                self.assertIn('"deleted_alert_count":0', second.stdout)
                self.assertIn('"no_op":true', second.stdout)

                with closing(sqlite3.connect(scenario)) as connection:
                    audit_only_count = connection.execute(
                        "SELECT COUNT(*) FROM apex_alert_metadata "
                        "WHERE category IN ('ASSUMPTION', 'RUN_ACCOUNTING')"
                    ).fetchone()[0]
                    order_counts = connection.execute(
                        "SELECT COUNT(*), COUNT(DISTINCT po_number) FROM purchase_orders"
                    ).fetchone()
                    operational_categories = tuple(
                        row[0]
                        for row in connection.execute(
                            "SELECT category FROM apex_alert_metadata ORDER BY alert_id"
                        )
                    )
                    visible_descriptions = tuple(
                        row[0]
                        for row in connection.execute(
                            "SELECT description FROM alerts ORDER BY alert_id"
                        )
                    )
                self.assertEqual(audit_only_count, 0)
                self.assertEqual(order_counts[0], order_counts[1])
                self.assertFalse(
                    any(
                        description.startswith("Run completed")
                        or " used assumption " in description
                        for description in visible_descriptions
                    )
                )
                if source == SOURCE:
                    self.assertEqual(
                        operational_categories,
                        (AlertCategory.EVIDENCE_CONTRACT.value,),
                    )

    def test_compact_rationales_are_at_most_45_percent_of_v3_on_all_scenarios(self) -> None:
        observed: list[tuple[str, int, int]] = []
        stage_two_outcomes: set[str] = set()
        for source in ALL_SCENARIOS:
            artifacts = run(RuntimeConfig(source, dry_run=True))
            for decision in artifacts.decisions:
                quantity = tuple(
                    item
                    for item in decision.comparator_facts
                    if item.kind == "quantity_calibration"
                )
                if decision.selected_plan is not None:
                    self.assertEqual(len(quantity), 1)
                    self.assertEqual(quantity[0].stage, 0)
                    self.assertTrue(quantity[0].decisive)
                route_facts = tuple(
                    item
                    for item in decision.comparator_facts
                    if item.kind == "route_selection"
                )
                pairs = {
                    (item.selected_route_ids, item.compared_route_ids)
                    for item in route_facts
                }
                for pair in pairs:
                    path = tuple(
                        sorted(
                            (
                                item
                                for item in route_facts
                                if (
                                    item.selected_route_ids,
                                    item.compared_route_ids,
                                )
                                == pair
                            ),
                            key=lambda item: item.stage,
                        )
                    )
                    self.assertEqual(
                        tuple(item.stage for item in path),
                        tuple(range(1, 8)),
                    )
                    self.assertEqual(sum(item.decisive for item in path), 1)
                    stage_two_outcomes.add(path[1].outcome)
            audit_only = tuple(
                item
                for item in artifacts.outputs.alerts
                if item.category
                in {AlertCategory.ASSUMPTION, AlertCategory.RUN_ACCOUNTING}
            )
            evidence_contract = tuple(
                item
                for item in artifacts.outputs.alerts
                if item.category is AlertCategory.EVIDENCE_CONTRACT
            )
            self.assertFalse(audit_only)
            self.assertLessEqual(len(evidence_contract), 1)
            self.assertEqual(
                tuple(sorted(item.category.value for item in artifacts.outputs.alerts)),
                EXPECTED_OPERATIONAL_ALERT_CATEGORIES[source.name],
            )
            self.assertTrue(
                all(
                    item.description.startswith(("Recommendation:", "Error:"))
                    for item in artifacts.outputs.alerts
                )
            )
            self.assertFalse(
                any("requirement requirement:" in item.description for item in artifacts.outputs.alerts)
            )
            if source == SCENARIO_04:
                self.assertFalse(
                    any(
                        item.category is AlertCategory.DECISION_REQUIRED
                        for item in artifacts.outputs.alerts
                    )
                )
                opportunity = next(
                    item
                    for item in artifacts.outputs.alerts
                    if item.category is AlertCategory.COST_OPPORTUNITY
                )
                self.assertIn("$1,017.25", opportunity.description)
                self.assertIn("saving $396.55", opportunity.description)
                self.assertNotIn("plan-", opportunity.description)

            if source.name == "scenario_01_baseline.sqlite":
                cmp010 = next(
                    item
                    for item in artifacts.outputs.purchase_orders
                    if item.component_id == "CMP-010"
                )
                cmp010_rationale = cmp010.rationale.split("] ", 1)[1]
                self.assertIn(
                    "Closes the 12.5-unit projected shortage for PO-5001 and PO-5004",
                    cmp010_rationale,
                )
                self.assertIn(
                    "preserves strategic-supplier continuity",
                    cmp010_rationale,
                )
                self.assertIn(
                    "supplier minimums/order increments raise the plan from 12.5 to 13.0 units",
                    cmp010_rationale,
                )
                self.assertIn("missing 12-month supplier-allocation history", cmp010_rationale)

            for target in artifacts.outputs.purchase_orders:
                decision = replace(
                    target.decision,
                    source_fingerprint=None,
                    comparator_facts=(),
                    material_rejections=(),
                )
                decision = replace(
                    decision,
                    rationale=render_decision_rationale(decision),
                )
                assert decision.selected_plan is not None
                line = next(
                    item
                    for item in decision.selected_plan.lines
                    if item.route_id == target.route_id
                )
                record_token = decisions_module._encoded(
                    decisions_module._legacy_v3_record(decision)
                )
                old_marker = decisions_module._po_marker(
                    key=target.action_key,
                    demand_digest=target.demand_fingerprint,
                    route_id=target.route_id,
                    policy_pack_version=target.policy_pack_version,
                    line_index=target.line_index,
                    decision_token=record_token,
                    version=3,
                )
                old_rationale = f"{old_marker} {render_line_rationale(decision, line)}"
                human_rationale = target.rationale.split("] ", 1)[1]
                observed.append(
                    (source.name, len(human_rationale), len(old_rationale))
                )
                self.assertLessEqual(
                    len(human_rationale) * 100,
                    len(old_rationale) * 45,
                )
                self.assertNotIn(" record=", target.rationale)
                self.assertNotIn("deciding comparators", human_rationale)
                self.assertIn("projected shortage", human_rationale)
                self.assertNotIn("Initial shortage:", human_rationale)
                self.assertNotIn("plan residual:", human_rationale)
                self.assertNotIn(" per unit ", human_rationale)
                self.assertNotIn("ROLLING_HISTORY_UNKNOWN", human_rationale)
                self.assertNotIn(line.component_id, human_rationale)
                self.assertNotIn(line.supplier_id, human_rationale)
                if len(decision.selected_plan.lines) > 1:
                    self.assertTrue(human_rationale.startswith("Contributes to "))
                else:
                    self.assertTrue(
                        human_rationale.startswith(("Closes ", "Reduces "))
                    )
        self.assertTrue(observed)
        self.assertIn("skipped_condition_b", stage_two_outcomes)
        self.assertIn("moot_same_domesticity", stage_two_outcomes)

    def test_independent_validator_rejects_rationale_tampering_and_omission(self) -> None:
        artifacts = run(RuntimeConfig(SOURCE, dry_run=True))
        decision = next(
            item
            for item in artifacts.decisions
            if any(
                fact.kind == "route_selection" for fact in item.comparator_facts
            )
        )
        validator = IndependentPlanValidator(
            artifacts.registry,
            policy_parameters=artifacts.registry.parameters_for(
                artifacts.snapshot.configuration.current_date
            ),
        )
        deciding = next(
            item
            for item in decision.comparator_facts
            if item.kind == "route_selection" and item.decisive
        )
        tampered_facts = tuple(
            replace(item, outcome="tampered_outcome")
            if item is deciding
            else item
            for item in decision.comparator_facts
        )
        tampered = replace(decision, comparator_facts=tampered_facts)
        tampered_decisions = tuple(
            tampered if item.requirement_id == decision.requirement_id else item
            for item in artifacts.decisions
        )
        tampered_validation = validator.validate(
            artifacts.snapshot,
            tampered_decisions,
            artifacts.solver_results,
        )
        self.assertIn(
            "RATIONALE_COMPARATOR_MISMATCH",
            {item.code for item in tampered_validation.issues},
        )

        route_path = tuple(
            item
            for item in decision.comparator_facts
            if item.kind == "route_selection"
        )
        forged_route = "route-" + "f" * 64
        route_tampered = replace(
            decision,
            comparator_facts=tuple(
                replace(item, compared_route_ids=(forged_route,))
                if item.kind == "route_selection"
                else item
                for item in decision.comparator_facts
            ),
        )
        rule_tampered = replace(
            decision,
            comparator_facts=tuple(
                replace(
                    item,
                    rule_ids=("POL-PROC-001.section_2.approved_supplier",),
                )
                if item is deciding
                else item
                for item in decision.comparator_facts
            ),
        )
        for changed in (route_tampered, rule_tampered):
            with self.subTest(comparator_tamper=changed.comparator_facts):
                validation = validator.validate(
                    artifacts.snapshot,
                    tuple(
                        changed
                        if item.requirement_id == decision.requirement_id
                        else item
                        for item in artifacts.decisions
                    ),
                    artifacts.solver_results,
                )
                self.assertIn(
                    "RATIONALE_COMPARATOR_MISMATCH",
                    {item.code for item in validation.issues},
                )

        rejection = decision.material_rejections[0]
        rejection_mutations = (
            replace(rejection, route_id=forged_route),
            replace(rejection, reason_code="POLICY_GATE_FAILED"),
            replace(
                rejection,
                rule_ids=("POL-PROC-001.section_2.approved_supplier",),
            ),
            replace(rejection, eligibility=EvidenceStatus.FAIL),
        )
        for changed_rejection in rejection_mutations:
            with self.subTest(rejection_tamper=changed_rejection):
                changed = replace(
                    decision, material_rejections=(changed_rejection,)
                )
                validation = validator.validate(
                    artifacts.snapshot,
                    tuple(
                        changed
                        if item.requirement_id == decision.requirement_id
                        else item
                        for item in artifacts.decisions
                    ),
                    artifacts.solver_results,
                )
                self.assertIn(
                    "RATIONALE_MATERIAL_REJECTION_MISMATCH",
                    {item.code for item in validation.issues},
                )

        stage_seven = next(item for item in route_path if item.stage == 7)
        stage_seven_decision = replace(
            decision,
            comparator_facts=tuple(
                replace(item, decisive=False)
                if item is deciding
                else replace(
                    item,
                    decisive=True,
                    outcome="selected_lower_id_free_fingerprint",
                    rule_ids=(),
                )
                if item is stage_seven
                else item
                for item in decision.comparator_facts
            ),
        )
        rendered = render_decision_rationale(stage_seven_decision)
        self.assertIn("deciding stage 7 id_free_fingerprint", rendered)
        self.assertIn("rule IDs [none]", rendered)

        omitted = replace(decision, material_rejections=())
        omitted_decisions = tuple(
            omitted if item.requirement_id == decision.requirement_id else item
            for item in artifacts.decisions
        )
        omitted_validation = validator.validate(
            artifacts.snapshot,
            omitted_decisions,
            artifacts.solver_results,
        )
        self.assertIn(
            "RATIONALE_MATERIAL_REJECTION_MISSING",
            {item.code for item in omitted_validation.issues},
        )

        hard_artifacts = run(RuntimeConfig(ASSIGNED_SCENARIOS[0], dry_run=True))
        hard_decision = next(
            item
            for item in hard_artifacts.decisions
            if len(
                tuple(
                    rejection
                    for rejection in item.material_rejections
                    if rejection.eligibility is EvidenceStatus.FAIL
                )
            )
            >= 2
        )
        omitted_hard = replace(
            hard_decision,
            material_rejections=hard_decision.material_rejections[1:],
        )
        hard_validator = IndependentPlanValidator(
            hard_artifacts.registry,
            policy_parameters=hard_artifacts.registry.parameters_for(
                hard_artifacts.snapshot.configuration.current_date
            ),
        )
        hard_validation = hard_validator.validate(
            hard_artifacts.snapshot,
            tuple(
                omitted_hard
                if item.requirement_id == hard_decision.requirement_id
                else item
                for item in hard_artifacts.decisions
            ),
            hard_artifacts.solver_results,
        )
        self.assertIn(
            "RATIONALE_MATERIAL_REJECTION_MISSING",
            {item.code for item in hard_validation.issues},
        )

    def test_exact_comparator_reconstruction_handles_multiple_rejected_suppliers(self) -> None:
        scenario = Path(self.temporary_directory.name) / "multi-rejected.sqlite"
        shutil.copy2(ASSIGNED_SCENARIOS[0], scenario)
        with closing(sqlite3.connect(scenario)) as connection:
            connection.execute(
                "UPDATE suppliers SET country = 'Canada', is_domestic = 1 "
                "WHERE supplier_id = 'SUP-104'"
            )
            connection.commit()
        artifacts = run(RuntimeConfig(scenario, dry_run=True))
        decision = next(
            item
            for item in artifacts.decisions
            if len(
                {
                    fact.compared_route_ids
                    for fact in item.comparator_facts
                    if fact.kind == "route_selection"
                }
            )
            >= 2
        )
        pairs = {
            fact.compared_route_ids
            for fact in decision.comparator_facts
            if fact.kind == "route_selection"
        }
        self.assertGreaterEqual(len(pairs), 2)
        for pair in pairs:
            path = tuple(
                fact
                for fact in decision.comparator_facts
                if fact.compared_route_ids == pair
            )
            self.assertEqual({fact.stage for fact in path}, set(range(1, 8)))

    def test_production_contract_preserves_evidence_blocked_dispositions_on_all_scenarios(self) -> None:
        for source in ALL_SCENARIOS:
            with self.subTest(scenario=source.name):
                scenario = (
                    Path(self.temporary_directory.name)
                    / f"production-{source.name}"
                )
                shutil.copy2(source, scenario)
                initial_orders = output_rows(scenario)[0]
                arguments = (
                    "--scenario",
                    str(scenario),
                    "--contract=production",
                    "--llm=off",
                    "--json",
                )

                first = self.command(*arguments)
                self.assertEqual(first.returncode, 0, first.stderr)
                payload = json.loads(first.stdout)
                scoped_components: set[str] = set()
                blocked_components: set[str] = set()
                covered_components: set[str] = set()
                for decision in payload["decisions"]:
                    rolling = tuple(
                        item
                        for item in decision["evidence"]
                        if item["basis"] == "rolling_window"
                        and item["status"] == "UNKNOWN"
                        and item["contract_disposition"]
                        == PlanDisposition.DECISION_REQUIRED.value
                    )
                    if not rolling:
                        continue
                    scoped_components.add(decision["component_id"])
                    self.assertIn(
                        "DECISION_REQUIRED", decision["alert_categories"]
                    )
                    self.assertIn(
                        "EVIDENCE_CONTRACT", decision["alert_categories"]
                    )
                    # The only production-safe ASSUMPTION here is the pack's
                    # explicitly provisional economic-autonomy configuration;
                    # unavailable rolling evidence remains DECISION_REQUIRED.
                    self.assertIn("ASSUMPTION", decision["alert_categories"])
                    self.assertTrue(
                        decision["economic_autonomy"]["provisional"]
                    )
                    self.assertNotIn(
                        "NO_ELIGIBLE_SUPPLIER", decision["alert_categories"]
                    )
                    self.assertIsNone(decision["selected_plan"])
                    if Decimal(decision["residual_gap"]) > 0:
                        blocked_components.add(decision["component_id"])
                        self.assertEqual(
                            decision["requirement_state"]["resolution"],
                            "UNRESOLVED",
                        )
                        self.assertTrue(
                            any(
                                item["disposition"] == "DECISION_REQUIRED"
                                and any(
                                    evidence["contract_disposition"]
                                    == "DECISION_REQUIRED"
                                    for evidence in item["evidence"]
                                )
                                for item in decision["alternatives"]
                            )
                        )
                    else:
                        covered_components.add(decision["component_id"])
                self.assertTrue(scoped_components)

                rows_after_first = output_rows(scenario)
                self.assertEqual(rows_after_first[0], initial_orders)
                alert_audits = owned_alert_audits(scenario)
                with closing(sqlite3.connect(scenario)) as connection:
                    sequence_after_first = connection.execute(
                        "SELECT seq FROM sqlite_sequence WHERE name = 'alerts'"
                    ).fetchone()
                self.assertFalse(
                    any(
                        item.category
                        in {AlertCategory.ASSUMPTION, AlertCategory.RUN_ACCOUNTING}
                        for item in alert_audits
                    )
                )
                self.assertEqual(
                    sum(
                        item.category is AlertCategory.EVIDENCE_CONTRACT
                        for item in alert_audits
                    ),
                    1,
                )
                self.assertFalse(
                    any(
                        item.category is AlertCategory.NO_ELIGIBLE_SUPPLIER
                        for item in alert_audits
                    )
                )
                for component_id in blocked_components:
                    self.assertTrue(
                        any(
                            item.category is AlertCategory.DECISION_REQUIRED
                            and f"component {component_id}" in item.body
                            for item in alert_audits
                        )
                    )
                    global_evidence = next(
                        item
                        for item in alert_audits
                        if item.category is AlertCategory.EVIDENCE_CONTRACT
                    )
                    self.assertIn(component_id, global_evidence.body)
                for component_id in covered_components:
                    self.assertFalse(
                        any(
                            item.category is AlertCategory.DECISION_REQUIRED
                            and f"component {component_id}" in item.body
                            for item in alert_audits
                        )
                    )
                second = self.command(*arguments)
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual(output_rows(scenario), rows_after_first)
                self.assertIn('"no_op":true', second.stdout)
                with closing(sqlite3.connect(scenario)) as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT seq FROM sqlite_sequence WHERE name = 'alerts'"
                        ).fetchone(),
                        sequence_after_first,
                    )

    def test_late_committed_inbound_uses_strictly_earlier_recovery_and_reruns_no_op(self) -> None:
        scenario = self.recovery_scenario("strictly-earlier-recovery.sqlite")
        arguments = (
            "--scenario",
            str(scenario),
            "--contract=benchmark",
            "--llm=off",
            "--json",
        )

        first = self.command(*arguments)
        self.assertEqual(first.returncode, 0, first.stderr)
        payload = json.loads(first.stdout)
        decision = next(
            item for item in payload["decisions"] if item["component_id"] == "CMP-014"
        )
        selected = decision["selected_plan"]
        self.assertEqual(decision["initial_eventual_gap"], "0")
        self.assertIsNotNone(selected)
        self.assertEqual(selected["recovery_demand"], "40.0")
        self.assertEqual(selected["recovery_quantity"], "40.0")
        self.assertEqual(selected["discretionary_surplus"], "0")
        self.assertEqual(
            [(item["supplier_id"], item["quantity"], item["material_available_date"]) for item in selected["lines"]],
            [("SUP-112", "40.0", "2025-09-15")],
        )
        self.assertIn("RECOVERY_SURPLUS", decision["alert_categories"])
        self.assertEqual(decision["deadline_lateness"], [])

        rows_after_first = output_rows(scenario)
        with closing(sqlite3.connect(scenario)) as connection:
            sequence_after_first = connection.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = 'alerts'"
            ).fetchone()
            managed_recovery = connection.execute(
                "SELECT COUNT(*) FROM purchase_orders "
                "WHERE component_id = 'CMP-014' AND po_number IN "
                "(SELECT po_number FROM apex_po_metadata)"
            ).fetchone()[0]
        self.assertEqual(managed_recovery, 1)

        second = self.command(*arguments)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(output_rows(scenario), rows_after_first)
        with closing(sqlite3.connect(scenario)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT seq FROM sqlite_sequence WHERE name = 'alerts'"
                ).fetchone(),
                sequence_after_first,
            )
        second_payload = json.loads(second.stdout)
        self.assertEqual(
            second_payload["commit"],
            {
                "committed_po_numbers": [],
                "deleted_alert_count": 0,
                "inserted_alert_count": 0,
                "no_op": True,
            },
        )

    def test_malformed_catalog_price_uses_valid_route_and_reruns_no_op(self) -> None:
        scenario = self.source_scenario("catalog-price-quarantine.sqlite")
        hostile = "n/a\nHOSTILE-CONTROL-\x01"
        with closing(sqlite3.connect(scenario)) as connection, connection:
            connection.execute(
                "UPDATE supplier_catalog SET unit_price = ? "
                "WHERE supplier_id = ? AND component_id = ?",
                (hostile, "SUP-102", "CMP-016"),
            )
        arguments = (
            "--scenario",
            str(scenario),
            "--contract=benchmark",
            "--llm=off",
            "--json",
        )

        first = self.command(*arguments)
        self.assertEqual(first.returncode, 0, first.stderr)
        with closing(sqlite3.connect(scenario)) as connection:
            managed_routes = tuple(
                connection.execute(
                    "SELECT supplier_id, unit_price FROM purchase_orders "
                    "WHERE component_id = 'CMP-016' "
                    "AND po_number IN (SELECT po_number FROM apex_po_metadata) "
                    "ORDER BY supplier_id"
                )
            )
        self.assertEqual(managed_routes, (("SUP-109", 22.0),))
        alerts = self.source_data_quality_alerts(scenario)
        self.assertEqual(len(alerts), 1)
        self.assertIn("Source table supplier_catalog", alerts[0])
        self.assertIn("supplier_id=SUP-102, component_id=CMP-016", alerts[0])
        self.assertIn("field unit_price", alerts[0])
        self.assertIn(
            "Blast radius: only this supplier/component catalog offer",
            alerts[0],
        )
        self.assertIn(
            "without substituting price, lead time, quantity, or dates",
            alerts[0],
        )
        self.assertIn("Human remediation:", alerts[0])
        self.assertNotIn("HOSTILE", alerts[0])
        self.assertNotIn("\x01", alerts[0])
        self.assertNotIn("HOSTILE", first.stdout + first.stderr)

        rows_after_first = output_rows(scenario)
        second = self.command(*arguments)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(output_rows(scenario), rows_after_first)
        self.assertEqual(
            json.loads(second.stdout)["commit"],
            {
                "committed_po_numbers": [],
                "deleted_alert_count": 0,
                "inserted_alert_count": 0,
                "no_op": True,
            },
        )

    def test_null_price_and_bad_catalog_lead_time_quarantine_only_offer(self) -> None:
        cases = (
            ("unit_price", None, "price-null"),
            ("lead_time_days", None, "lead-null"),
            ("lead_time_days", "soon\nHOSTILE-LEAD-\x02", "lead-text"),
        )
        for field, malformed, label in cases:
            with self.subTest(kind=label):
                scenario = self.source_scenario(f"catalog-lead-{label}.sqlite")
                if malformed is None:
                    self.make_catalog_fields_nullable(scenario)
                with closing(sqlite3.connect(scenario)) as connection, connection:
                    connection.execute(
                        f'UPDATE supplier_catalog SET "{field}" = ? '
                        "WHERE supplier_id = ? AND component_id = ?",
                        (malformed, "SUP-102", "CMP-016"),
                    )
                completed = self.command(
                    "--scenario",
                    str(scenario),
                    "--contract=benchmark",
                    "--llm=off",
                    "--json",
                )

                self.assertEqual(completed.returncode, 0, completed.stderr)
                with closing(sqlite3.connect(scenario)) as connection:
                    managed_suppliers = tuple(
                        row[0]
                        for row in connection.execute(
                            "SELECT supplier_id FROM purchase_orders "
                            "WHERE component_id = 'CMP-016' "
                            "AND po_number IN (SELECT po_number FROM apex_po_metadata) "
                            "ORDER BY supplier_id"
                        )
                    )
                self.assertEqual(managed_suppliers, ("SUP-109",))
                alerts = self.source_data_quality_alerts(scenario)
                self.assertEqual(len(alerts), 1)
                self.assertIn(f"field {field}", alerts[0])
                self.assertIn(
                    "without substituting price, lead time, quantity, or dates",
                    alerts[0],
                )
                self.assertNotIn("HOSTILE-LEAD", alerts[0])
                self.assertNotIn("HOSTILE-LEAD", completed.stdout + completed.stderr)

                rows_after_first = output_rows(scenario)
                second = self.command(
                    "--scenario",
                    str(scenario),
                    "--contract=benchmark",
                    "--llm=off",
                    "--json",
                )
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual(output_rows(scenario), rows_after_first)
                self.assertTrue(json.loads(second.stdout)["commit"]["no_op"])

    def test_malformed_supplier_attribute_quarantines_only_that_supplier(self) -> None:
        scenario = self.source_scenario("supplier-wide-quarantine.sqlite")
        hostile = "not-a-boolean\nHOSTILE-SUPPLIER-\x03"
        with closing(sqlite3.connect(scenario)) as connection, connection:
            connection.execute(
                "UPDATE suppliers SET is_domestic = ? WHERE supplier_id = ?",
                (hostile, "SUP-102"),
            )
        completed = self.command(
            "--scenario",
            str(scenario),
            "--contract=benchmark",
            "--llm=off",
            "--json",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        selected_lines = tuple(
            line
            for decision in payload["decisions"]
            if decision["selected_plan"] is not None
            for line in decision["selected_plan"]["lines"]
        )
        self.assertTrue(selected_lines)
        self.assertFalse(
            any(line["supplier_id"] == "SUP-102" for line in selected_lines)
        )
        self.assertTrue(
            any(
                line["supplier_id"] == "SUP-109"
                and line["component_id"] == "CMP-016"
                for line in selected_lines
            )
        )
        self.assertTrue(
            any(line["component_id"] != "CMP-016" for line in selected_lines)
        )
        alerts = self.source_data_quality_alerts(scenario)
        self.assertEqual(len(alerts), 1)
        self.assertIn("Source table suppliers", alerts[0])
        self.assertIn("logical key (supplier_id=SUP-102)", alerts[0])
        self.assertIn("field is_domestic", alerts[0])
        self.assertIn("Blast radius: all catalog routes for this supplier", alerts[0])
        self.assertNotIn("HOSTILE-SUPPLIER", alerts[0])
        self.assertNotIn("HOSTILE-SUPPLIER", completed.stdout + completed.stderr)

    def test_quarantine_only_gap_is_data_quality_unresolved_not_no_eligible(self) -> None:
        scenario = self.source_scenario("quarantine-terminal.sqlite")
        with closing(sqlite3.connect(scenario)) as connection, connection:
            connection.execute(
                "UPDATE supplier_catalog SET unit_price = 'n/a' "
                "WHERE component_id = 'CMP-016'"
            )
        completed = self.command(
            "--scenario",
            str(scenario),
            "--contract=benchmark",
            "--llm=off",
            "--json",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        decision = next(
            item for item in payload["decisions"] if item["component_id"] == "CMP-016"
        )
        self.assertIsNone(decision["selected_plan"])
        self.assertGreater(Decimal(decision["residual_gap"]), 0)
        self.assertEqual(decision["requirement_state"]["resolution"], "UNRESOLVED")
        self.assertIn("DATA_QUALITY", decision["alert_categories"])
        self.assertIn("UNMET_DEMAND", decision["alert_categories"])
        self.assertNotIn("NO_ELIGIBLE_SUPPLIER", decision["alert_categories"])
        with closing(sqlite3.connect(scenario)) as connection:
            component_orders = connection.execute(
                "SELECT COUNT(*) FROM purchase_orders "
                "WHERE component_id = 'CMP-016' AND po_number IN "
                "(SELECT po_number FROM apex_po_metadata)"
            ).fetchone()[0]
        alert_audits = owned_alert_audits(scenario)
        self.assertEqual(component_orders, 0)
        self.assertTrue(
            any(item.category is AlertCategory.DATA_QUALITY for item in alert_audits)
        )
        self.assertTrue(
            any(item.category is AlertCategory.UNMET_DEMAND for item in alert_audits)
        )
        self.assertFalse(
            any(
                item.category is AlertCategory.NO_ELIGIBLE_SUPPLIER
                for item in alert_audits
            )
        )

    def test_structural_input_corruption_exits_three_without_any_write(self) -> None:
        cases = (
            (
                "bom",
                "UPDATE bom SET quantity_per = 'n/a' "
                "WHERE rowid = (SELECT min(rowid) FROM bom)",
            ),
            (
                "schedule",
                "UPDATE production_schedule SET quantity = 'n/a' "
                "WHERE rowid = (SELECT min(rowid) FROM production_schedule)",
            ),
            (
                "inventory",
                "UPDATE inventory SET quantity_on_hand = 'n/a' "
                "WHERE rowid = (SELECT min(rowid) FROM inventory)",
            ),
            (
                "configuration",
                "UPDATE scenario_config SET current_date = 'not-a-date'",
            ),
        )
        for label, mutation in cases:
            with self.subTest(table=label):
                scenario = self.source_scenario(f"structural-{label}.sqlite")
                with closing(sqlite3.connect(scenario)) as connection, connection:
                    connection.execute(mutation)
                rows_before = output_rows(scenario)
                bytes_before = scenario.read_bytes()

                completed = self.command(
                    "--scenario",
                    str(scenario),
                    "--contract=benchmark",
                    "--llm=off",
                    "--json",
                )

                self.assertEqual(completed.returncode, 3, completed.stderr)
                self.assertEqual(output_rows(scenario), rows_before)
                self.assertEqual(scenario.read_bytes(), bytes_before)

    def test_equal_or_later_route_does_not_create_recovery_order(self) -> None:
        for lead_days in (54, 60):
            with self.subTest(lead_days=lead_days):
                scenario = self.recovery_scenario(
                    f"non-improving-{lead_days}.sqlite",
                    lead_days=lead_days,
                )
                with closing(sqlite3.connect(scenario)) as connection:
                    connection.execute(
                        "DELETE FROM supplier_catalog "
                        "WHERE component_id = 'CMP-014' "
                        "AND supplier_id <> 'SUP-112'"
                    )
                    connection.execute(
                        "UPDATE supplier_catalog SET lead_time_days = ? "
                        "WHERE component_id = 'CMP-014'",
                        (lead_days,),
                    )
                    connection.commit()
                completed = self.command(
                    "--scenario",
                    str(scenario),
                    "--contract=benchmark",
                    "--llm=off",
                    "--json",
                )

                self.assertEqual(completed.returncode, 0, completed.stderr)
                payload = json.loads(completed.stdout)
                decision = next(
                    item
                    for item in payload["decisions"]
                    if item["component_id"] == "CMP-014"
                )
                self.assertEqual(decision["initial_eventual_gap"], "0")
                self.assertIsNone(decision["selected_plan"])
                self.assertNotIn("RECOVERY_SURPLUS", decision["alert_categories"])
                self.assertIn("LATE_ARRIVAL", decision["alert_categories"])
                with closing(sqlite3.connect(scenario)) as connection:
                    managed_recovery = connection.execute(
                        "SELECT COUNT(*) FROM purchase_orders "
                        "WHERE component_id = 'CMP-014' "
                        "AND po_number IN (SELECT po_number FROM apex_po_metadata)"
                    ).fetchone()[0]
                self.assertEqual(managed_recovery, 0)

    def test_scenarios_one_and_three_disclose_every_missed_deadline(self) -> None:
        expected = {
            "scenario_01_baseline.sqlite": {
                "CMP-003": (("2025-09-12", "80.0", "240.0"),),
            },
            "scenario_03_tight_timeline.sqlite": {
                "CMP-003": (("2025-09-12", "80.0", "240.0"),),
                "CMP-005": (("2025-09-10", "60.0", "60.0"),),
                "CMP-017": (("2025-09-10", "20.0", "20.0"),),
                "CMP-018": (("2025-09-10", "30.0", "30.0"),),
            },
        }
        for source in ASSIGNED_SCENARIOS:
            with self.subTest(scenario=source.name):
                scenario = Path(self.temporary_directory.name) / f"late-{source.name}"
                shutil.copy2(source, scenario)
                completed = self.command(
                    "--scenario",
                    str(scenario),
                    "--contract=benchmark",
                    "--llm=off",
                    "--json",
                )

                self.assertEqual(completed.returncode, 0, completed.stderr)
                payload = json.loads(completed.stdout)
                actual = {
                    item["component_id"]: tuple(
                        (
                            late["due_date"],
                            late["late_quantity"],
                            late["unit_late_days"],
                        )
                        for late in item["deadline_lateness"]
                    )
                    for item in payload["decisions"]
                    if item["deadline_lateness"]
                }
                self.assertEqual(actual, expected[source.name])
                for item in payload["decisions"]:
                    if item["deadline_lateness"]:
                        self.assertIn("LATE_ARRIVAL", item["alert_categories"])
                magnet = next(
                    item
                    for item in payload["decisions"]
                    if item["component_id"] == "CMP-003"
                )
                self.assertGreaterEqual(
                    Decimal(magnet["selected_plan"]["unit_late_days"]),
                    Decimal("240"),
                )
                with closing(sqlite3.connect(scenario)) as connection:
                    late_alerts = connection.execute(
                        "SELECT COUNT(*) FROM apex_alert_metadata "
                        "WHERE category = 'LATE_ARRIVAL'"
                    ).fetchone()[0]
                self.assertEqual(
                    late_alerts,
                    sum(len(value) for value in expected[source.name].values()),
                )

    def test_changed_inventory_creates_a_new_action_without_replacing_commitments(self) -> None:
        scenario = Path(self.temporary_directory.name) / "changed-inventory.sqlite"
        shutil.copy2(SOURCE, scenario)
        arguments = (
            "--scenario",
            str(scenario),
            "--contract=benchmark",
            "--llm=off",
            "--json",
        )

        first = self.command(*arguments)
        orders_after_first = set(output_rows(scenario)[0])
        self.assertEqual(first.returncode, 0, first.stderr)
        managed_component = sorted(orders_after_first)[0][1]
        with closing(sqlite3.connect(scenario)) as connection:
            connection.execute(
                "UPDATE inventory SET quantity_on_hand = quantity_on_hand - 1 "
                "WHERE component_id = ?",
                (managed_component,),
            )
            connection.commit()

        changed = self.command(*arguments)
        orders_after_change = set(output_rows(scenario)[0])

        self.assertEqual(changed.returncode, 0, changed.stderr)
        self.assertIn('"no_op":false', changed.stdout)
        self.assertTrue(orders_after_first < orders_after_change)

    def test_changed_alternative_route_keeps_old_po_physical_without_duplicate_demand(self) -> None:
        scenario = Path(self.temporary_directory.name) / "changed-alternative.sqlite"
        shutil.copy2(SOURCE, scenario)
        arguments = (
            "--scenario",
            str(scenario),
            "--contract=benchmark",
            "--llm=off",
            "--json",
        )

        first = self.command(*arguments)
        self.assertEqual(first.returncode, 0, first.stderr)
        orders_after_first = output_rows(scenario)[0]
        cmp016_before = tuple(
            row for row in orders_after_first if row[1] == "CMP-016"
        )
        self.assertEqual(len(cmp016_before), 1)
        self.assertTrue(
            cmp016_before[0][7].startswith(
                "Closes the 15.0-unit projected shortage"
            )
        )
        self.assertNotIn("[APEX_AGENT:", cmp016_before[0][7])
        with closing(sqlite3.connect(scenario)) as connection:
            marker = connection.execute(
                "SELECT marker FROM apex_po_metadata WHERE po_number = ?",
                (cmp016_before[0][0],),
            ).fetchone()[0]
        self.assertIn(" source=", marker)
        self.assertNotIn(" record=", cmp016_before[0][7])

        with closing(sqlite3.connect(scenario)) as connection, connection:
            connection.execute(
                "UPDATE supplier_catalog SET unit_price = ? "
                "WHERE component_id = ? AND supplier_id = ?",
                ("10.0", "CMP-016", "SUP-109"),
            )

        changed = self.command(*arguments)
        self.assertEqual(changed.returncode, 0, changed.stderr)
        changed_payload = json.loads(changed.stdout)
        cmp016_decision = next(
            item
            for item in changed_payload["decisions"]
            if item["component_id"] == "CMP-016"
        )
        cmp016_after = tuple(
            row for row in output_rows(scenario)[0] if row[1] == "CMP-016"
        )

        self.assertEqual(cmp016_after, cmp016_before)
        self.assertIsNone(cmp016_decision["selected_plan"])
        self.assertEqual(cmp016_decision["residual_gap"], "0")
        self.assertFalse(changed_payload["commit"]["no_op"])

    def test_default_contract_reconciles_optimizer_and_validator_objectives(self) -> None:
        scenario = Path(self.temporary_directory.name) / "scenario_objective.sqlite"
        shutil.copy2(SOURCE, scenario)

        completed = self.command("--scenario", str(scenario), "--llm=off")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("contract=benchmark", completed.stdout)
        with closing(sqlite3.connect(scenario)) as connection:
            rows = tuple(
                connection.execute(
                    "SELECT component_id, supplier_id, quantity, unit_price, "
                    "order_date, expected_delivery_date "
                    "FROM purchase_orders ORDER BY component_id, supplier_id"
                )
            )
        self.assertEqual(
            rows,
            (
                ("CMP-014", "SUP-112", 20.0, 32.0, "2025-09-01", "2025-09-15"),
                ("CMP-016", "SUP-102", 15.0, 18.0, "2025-09-01", "2025-09-15"),
            ),
        )

    def test_principal_cli_shortage_crosses_every_commit_boundary_layer(self) -> None:
        """The principal CLI evidence must exercise procurement, not covered demand."""

        scenario = self.source_scenario("principal-shortage.sqlite")
        before_orders, _before_alerts = output_rows(scenario)

        completed = self.command(
            "--scenario",
            str(scenario),
            "--contract=benchmark",
            "--llm=off",
            "--json",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        audit = json.loads(completed.stderr)
        shortage_decisions = tuple(
            item
            for item in payload["decisions"]
            if Decimal(item["initial_eventual_gap"]) > 0
        )
        executable = tuple(
            item for item in shortage_decisions if item["selected_plan"] is not None
        )
        self.assertTrue(shortage_decisions)
        self.assertTrue(executable)

        # Optimizer and independent validator both completed their proof work.
        executable_components = {item["component_id"] for item in executable}
        certified_components = {
            item["component_id"]
            for item in audit["solver_outcomes"]
            if item["solve_kind"] == "executable"
            and item["status"] == "OPTIMAL"
            and item["exact_post_validated"]
        }
        self.assertTrue(executable_components <= certified_components)
        self.assertTrue(audit["validation"]["is_valid"])
        self.assertTrue(audit["validation"]["solver_results_verified"])

        # Decision rendering and the atomic commit are visible in the actual DB.
        after_orders, after_alerts = output_rows(scenario)
        self.assertGreater(len(after_orders), len(before_orders))
        self.assertEqual(
            set(payload["commit"]["committed_po_numbers"]),
            {row[0] for row in after_orders} - {row[0] for row in before_orders},
        )
        self.assertTrue(
            all(
                row[7].startswith(("Closes ", "Reduces ", "Contributes to "))
                and "projected shortage" in row[7]
                and (
                    "Supplier choice" in row[7]
                    or "supplier mix" in row[7]
                    or "Only executable route" in row[7]
                )
                and "Initial shortage:" not in row[7]
                and " per unit " not in row[7]
                and "[APEX_AGENT:" not in row[7]
                for row in after_orders
                if row[0] in payload["commit"]["committed_po_numbers"]
            )
        )
        parsed_alerts = owned_alert_audits(scenario)
        self.assertTrue(parsed_alerts)
        self.assertFalse(
            any(
                item.category
                in {AlertCategory.ASSUMPTION, AlertCategory.RUN_ACCOUNTING}
                for item in parsed_alerts
            )
        )

    def test_installed_runtime_layout_executes_scenario_six(self) -> None:
        install_root = Path(self.temporary_directory.name) / "site-packages"
        shutil.copytree(
            PROJECT_ROOT / "src" / "apex_procurement",
            install_root / "apex_procurement",
        )
        for distribution_name in ("scipy", "numpy"):
            distribution = metadata.distribution(distribution_name)
            roots = {
                Path(item).parts[0]
                for item in distribution.files or ()
                if Path(item).parts
            }
            for root in roots:
                source = Path(distribution.locate_file(root))
                target = install_root / root
                if source.exists() and not target.exists():
                    target.symlink_to(
                        source, target_is_directory=source.is_dir()
                    )
        scenario = Path(self.temporary_directory.name) / "installed.sqlite"
        shutil.copy2(SOURCE, scenario)
        script = (
            "from pathlib import Path;"
            "import sys;"
            f"sys.path.insert(0, {str(install_root)!r});"
            "import apex_procurement;"
            f"assert Path(apex_procurement.__file__).is_relative_to(Path({str(install_root)!r}));"
            "from apex_procurement.cli import main;"
            "raise SystemExit(main(['--scenario',sys.argv[1],'--llm=off','--dry-run']))"
        )

        completed = subprocess.run(
            [sys.executable, "-I", "-c", script, str(scenario)],
            cwd="/",
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("status=dry-run", completed.stdout)

    def test_large_discrete_fg_demand_is_independently_certified(self) -> None:
        for quantity in (250, 300, 400):
            with self.subTest(quantity=quantity):
                scenario = (
                    Path(self.temporary_directory.name)
                    / f"large-{quantity}.sqlite"
                )
                shutil.copy2(SOURCE, scenario)
                with closing(sqlite3.connect(scenario)) as connection:
                    connection.execute(
                        "UPDATE production_schedule "
                        "SET product_id = ?, quantity = ?",
                        ("FG-1002", quantity),
                    )
                    connection.commit()

                completed = self.command(
                    "--scenario",
                    str(scenario),
                    "--contract=benchmark",
                    "--llm=off",
                    "--dry-run",
                )

                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertNotIn(
                    "INDEPENDENT_SOLVE_UNPROVEN", completed.stderr
                )
                self.assertNotIn("CALIBRATION_MISMATCH", completed.stderr)

    def test_700_unit_order_is_withheld_as_one_complete_stable_proposal(self) -> None:
        scenario = self.isolated_housing_scenario(
            "approval-700.sqlite",
            requirement=700,
        )
        arguments = (
            "--scenario",
            str(scenario),
            "--contract=benchmark",
            "--llm=off",
            "--json",
        )

        first = self.command(*arguments)
        self.assertEqual(first.returncode, 0, first.stderr)
        first_payload = json.loads(first.stdout)
        decision = next(
            item
            for item in first_payload["decisions"]
            if item["component_id"] == "CMP-008"
        )
        self.assertIsNone(decision["selected_plan"])
        proposal = next(
            item
            for item in decision["alternatives"]
            if item["disposition"] == "RECOMMEND_APPROVAL"
        )
        self.assertEqual(proposal["minimum_compliant_total"], "700.0")
        self.assertEqual(proposal["cheapest_covering_cost"], "59500.00")
        self.assertEqual(proposal["total_cost"], "59500.00")
        self.assertEqual(
            [
                (
                    item["supplier_id"],
                    item["quantity"],
                    item["unit_price"],
                    item["approval_rule_ids"],
                )
                for item in proposal["lines"]
            ],
            [
                (
                    "SUP-102",
                    "700.0",
                    "85.0",
                    ["POL-PROC-001.section_7.manager_approval"],
                )
            ],
        )
        rows_after_first = output_rows(scenario)
        self.assertEqual(rows_after_first[0], ())
        with closing(sqlite3.connect(scenario)) as connection:
            approval = connection.execute(
                "SELECT audit_description FROM apex_alert_metadata "
                "WHERE category = 'APPROVAL_REQUIRED'"
            ).fetchone()[0]
            sequence_after_first = connection.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = 'alerts'"
            ).fetchone()
        for expected in (
            "supplier SUP-102",
            "full quantity 700.0",
            "unit price 85.0",
            "line total 59500.00",
            "order date 2025-09-01",
            "expected delivery 2025-09-22",
            "material available 2025-09-22",
            "timing impact",
            "line total exceeds 50000",
            "Procurement Manager",
        ):
            self.assertIn(expected, approval)

        second = self.command(*arguments)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn("collision", second.stderr.lower())
        self.assertEqual(output_rows(scenario), rows_after_first)
        self.assertTrue(json.loads(second.stdout)["commit"]["no_op"])
        with closing(sqlite3.connect(scenario)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM purchase_orders "
                    "WHERE component_id = 'CMP-008'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT seq FROM sqlite_sequence WHERE name = 'alerts'"
                ).fetchone(),
                sequence_after_first,
            )

    def test_scenario_six_500_unit_case_never_optimizes_to_49984(self) -> None:
        scenario = Path(self.temporary_directory.name) / "scenario-06-500.sqlite"
        shutil.copy2(SOURCE, scenario)
        with closing(sqlite3.connect(scenario)) as connection:
            connection.execute("UPDATE production_schedule SET quantity = 500")
            connection.commit()

        completed = self.command(
            "--scenario",
            str(scenario),
            "--contract=benchmark",
            "--llm=off",
            "--json",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        decision = next(
            item
            for item in payload["decisions"]
            if item["component_id"] == "CMP-014"
        )
        self.assertIsNone(decision["selected_plan"])
        proposal = next(
            item
            for item in decision["alternatives"]
            if item["disposition"] == "RECOMMEND_APPROVAL"
        )
        self.assertEqual(proposal["total_cost"], "63360.00")
        self.assertEqual(
            [
                (item["supplier_id"], item["quantity"], item["unit_price"])
                for item in proposal["lines"]
            ],
            [("SUP-112", "1980.0", "32.0")],
        )
        with closing(sqlite3.connect(scenario)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM purchase_orders "
                    "WHERE component_id = 'CMP-014'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM purchase_orders "
                    "WHERE abs(quantity * unit_price - 49984) < 0.001"
                ).fetchone()[0],
                0,
            )

    def test_genuinely_sub_threshold_complete_line_executes(self) -> None:
        scenario = self.isolated_housing_scenario(
            "sub-threshold-500.sqlite",
            requirement=500,
        )

        completed = self.command(
            "--scenario",
            str(scenario),
            "--contract=benchmark",
            "--llm=off",
            "--json",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        decision = next(
            item
            for item in payload["decisions"]
            if item["component_id"] == "CMP-008"
        )
        selected = decision["selected_plan"]
        self.assertIsNotNone(selected)
        self.assertEqual(selected["total_cost"], "42500.00")
        self.assertEqual(selected["lines"][0]["quantity"], "500.0")
        self.assertEqual(selected["lines"][0]["approval_rule_ids"], [])
        with closing(sqlite3.connect(scenario)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT quantity, unit_price FROM purchase_orders "
                    "WHERE component_id = 'CMP-008'"
                ).fetchall(),
                [(500.0, 85.0)],
            )

    def test_line_above_150000_names_both_nested_authorities(self) -> None:
        scenario = self.isolated_housing_scenario(
            "nested-approval.sqlite",
            requirement=1800,
        )

        completed = self.command(
            "--scenario",
            str(scenario),
            "--contract=benchmark",
            "--llm=off",
            "--json",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        decision = next(
            item
            for item in payload["decisions"]
            if item["component_id"] == "CMP-008"
        )
        proposal = next(
            item
            for item in decision["alternatives"]
            if item["disposition"] == "RECOMMEND_APPROVAL"
        )
        self.assertEqual(proposal["total_cost"], "153000.00")
        self.assertEqual(
            proposal["lines"][0]["approval_rule_ids"],
            [
                "POL-PROC-001.section_7.manager_approval",
                "POL-PROC-001.section_7.vp_approval",
            ],
        )
        with closing(sqlite3.connect(scenario)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM purchase_orders "
                    "WHERE component_id = 'CMP-008'"
                ).fetchone()[0],
                0,
            )
            approval = connection.execute(
                "SELECT audit_description FROM apex_alert_metadata "
                "WHERE category = 'APPROVAL_REQUIRED'"
            ).fetchone()[0]
        for expected in (
            "line total exceeds 50000",
            "line total exceeds 150000",
            "Procurement Manager",
            "VP of Operations",
        ):
            self.assertIn(expected, approval)

    def test_scenario_two_executes_forced_allocation_surplus_only(self) -> None:
        scenario = Path(self.temporary_directory.name) / SCENARIO_02.name
        shutil.copy2(SCENARIO_02, scenario)

        artifacts = run(RuntimeConfig(scenario))
        magnet = next(
            item for item in artifacts.decisions if item.component_id == "CMP-003"
        )
        assert magnet.selected_plan is not None
        selected = magnet.selected_plan
        diagnostic = next(
            item
            for item in magnet.alternatives
            if item.summary.startswith("Non-executable compliance-cost diagnostic")
        )

        self.assertEqual(
            {(item.supplier_id, item.quantity) for item in selected.lines},
            {("SUP-107", Decimal("100")), ("SUP-108", Decimal("50"))},
        )
        self.assertEqual(selected.minimum_compliant_total, Decimal("150"))
        self.assertEqual(selected.forced_surplus, Decimal("92"))
        self.assertEqual(diagnostic.disposition, PlanDisposition.DECISION_REQUIRED)
        self.assertEqual(
            sum((item.quantity for item in diagnostic.lines), Decimal()),
            Decimal("58"),
        )
        with closing(sqlite3.connect(scenario)) as connection:
            written = tuple(
                connection.execute(
                    "SELECT supplier_id, quantity FROM purchase_orders "
                    "WHERE component_id = 'CMP-003' "
                    "AND po_number IN (SELECT po_number FROM apex_po_metadata) "
                    "ORDER BY supplier_id"
                )
            )
        self.assertEqual(written, (("SUP-107", 100.0), ("SUP-108", 50.0)))
        self.assertTrue(artifacts.validation.is_valid)

    def test_assigned_integration_scenarios_validate_without_invalid_orders(self) -> None:
        for source in ASSIGNED_SCENARIOS:
            with self.subTest(scenario=source.name):
                scenario = Path(self.temporary_directory.name) / source.name
                shutil.copy2(source, scenario)

                first = self.command("--scenario", str(scenario), "--llm=off")
                self.assertEqual(first.returncode, 0, first.stderr)
                with closing(sqlite3.connect(scenario)) as connection:
                    below_moq = connection.execute(
                        "SELECT COUNT(*) FROM purchase_orders AS p "
                        "JOIN supplier_catalog AS c "
                        "ON c.component_id = p.component_id "
                        "AND c.supplier_id = p.supplier_id "
                        "WHERE p.quantity < c.minimum_order_qty"
                    ).fetchone()[0]
                    magnet_rows = tuple(
                        connection.execute(
                            "SELECT p.supplier_id, p.quantity "
                            "FROM purchase_orders AS p "
                            "JOIN components AS c ON c.component_id = p.component_id "
                            "WHERE lower(c.name) LIKE '%neodymium%' "
                            "ORDER BY p.supplier_id"
                        )
                    )
                self.assertEqual(below_moq, 0)
                self.assertGreaterEqual(len({row[0] for row in magnet_rows}), 2)
                magnet_total = sum(row[1] for row in magnet_rows)
                self.assertTrue(
                    all(row[1] <= 0.8 * magnet_total for row in magnet_rows)
                )

    def test_scenario_four_baselines_match_independent_late_supply_oracle(self) -> None:
        scenario = Path(self.temporary_directory.name) / SCENARIO_04.name
        shutil.copy2(SCENARIO_04, scenario)

        artifacts = run(RuntimeConfig(scenario))
        baselines = {
            item.component_id: item.objective_vector
            for item in artifacts.solver_results
            if item.solve_kind is SolveKind.BASELINE
        }

        self.assertEqual(
            baselines["CMP-013"],
            (Decimal("0"), Decimal("337.500")),
        )
        self.assertEqual(
            baselines["CMP-015"],
            (Decimal("0"), Decimal("240.500")),
        )
        self.assertTrue(artifacts.validation.is_valid)
        self.assertGreater(len(artifacts.commit.committed_po_numbers), 0)

    def test_strict_warning_fails_before_any_write(self) -> None:
        before = output_rows(self.path)
        from apex_procurement.validator import IndependentPlanValidator

        original = IndependentPlanValidator.validate

        def warning_result(validator: object, *args: object, **kwargs: object):
            result = original(validator, *args, **kwargs)
            warning = ValidationIssue(
                "TEST_WARNING",
                ValidationSeverity.WARNING,
                "deterministic test warning",
            )
            return replace(result, issues=(*result.issues, warning))

        with patch.object(IndependentPlanValidator, "validate", warning_result):
            with self.assertRaisesRegex(PlanningFailure, "strict validation"):
                run(RuntimeConfig(self.path, dry_run=False, strict=True))
        self.assertEqual(output_rows(self.path), before)

    def test_path_and_invalid_data_exit_codes_are_precise(self) -> None:
        missing = self.command(
            "--scenario", str(Path(self.temporary_directory.name) / "missing.sqlite")
        )
        self.assertEqual(missing.returncode, 2)
        self.assertIn("scenario path is not a readable file", missing.stderr)

        malformed = Path(self.temporary_directory.name) / "malformed.sqlite"
        malformed.write_bytes(b"not a SQLite database")
        invalid = self.command("--scenario", str(malformed))
        self.assertEqual(invalid.returncode, 3)
        self.assertIn("scenario database could not be read safely", invalid.stderr)

    def test_explain_scope_and_unavailable_optional_modes_are_explicit(self) -> None:
        component_id = "component-from-snapshot"
        with closing(sqlite3.connect(self.path)) as connection:
            component_id = connection.execute(
                "SELECT b.component_id FROM production_schedule AS p "
                "JOIN bom AS b ON b.product_id = p.product_id "
                "ORDER BY b.component_id LIMIT 1"
            ).fetchone()[0]
        explained = self.command(
            "--scenario", str(self.path), "--dry-run", "--explain", component_id
        )
        self.assertEqual(explained.returncode, 0, explained.stderr)
        self.assertIn(f"component {component_id}", explained.stdout)

        unknown = self.command(
            "--scenario", str(self.path), "--dry-run", "--explain", "unknown-component"
        )
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("not present in scenario demand", unknown.stderr)

        required = self.command(
            "--scenario", str(self.path), "--llm", "required"
        )
        self.assertEqual(required.returncode, 4)
        self.assertIn("no optional model adapter", required.stderr)

        recompile = self.command(
            "--scenario", str(self.path), "--recompile-policy"
        )
        self.assertEqual(recompile.returncode, 2)
        self.assertIn("unrecognized arguments: --recompile-policy", recompile.stderr)


if __name__ == "__main__":
    unittest.main()
