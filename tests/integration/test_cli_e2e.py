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

from apex_procurement.cli import PlanningFailure, main, run
from apex_procurement.config import RuntimeConfig
from apex_procurement.domain import (
    PlanDisposition,
    SolveKind,
    ValidationIssue,
    ValidationSeverity,
)


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


def output_rows(path: Path) -> tuple[tuple[object, ...], tuple[object, ...]]:
    with closing(sqlite3.connect(path)) as connection:
        orders = tuple(connection.execute("SELECT * FROM purchase_orders ORDER BY 1"))
        alerts = tuple(connection.execute("SELECT * FROM alerts ORDER BY 1"))
    return orders, alerts


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
                    accounting_count = connection.execute(
                        "SELECT COUNT(*) FROM alerts "
                        "WHERE description LIKE '%category=RUN_ACCOUNTING%'"
                    ).fetchone()[0]
                    order_counts = connection.execute(
                        "SELECT COUNT(*), COUNT(DISTINCT po_number) FROM purchase_orders"
                    ).fetchone()
                self.assertEqual(accounting_count, 1)
                self.assertEqual(order_counts[0], order_counts[1])

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
                "WHERE component_id = 'CMP-014' AND rationale LIKE '[APEX_AGENT:%'"
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
                        "AND rationale LIKE '[APEX_AGENT:%'"
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
                        "SELECT COUNT(*) FROM alerts "
                        "WHERE description LIKE '%category=LATE_ARRIVAL%'"
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
                "SELECT description FROM alerts "
                "WHERE description LIKE '%category=APPROVAL_REQUIRED%'"
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
                "SELECT description FROM alerts "
                "WHERE description LIKE '%category=APPROVAL_REQUIRED%'"
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
                    "AND rationale LIKE '[APEX_AGENT:%' "
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
        self.assertEqual(recompile.returncode, 4)
        self.assertIn("reviewed compiled policy pack", recompile.stderr)


if __name__ == "__main__":
    unittest.main()
