from __future__ import annotations

from contextlib import closing, redirect_stdout
from dataclasses import replace
from io import StringIO
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
from apex_procurement.domain import ValidationIssue, ValidationSeverity


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "data" / "scenarios" / "scenario_06_simple.sqlite"
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
