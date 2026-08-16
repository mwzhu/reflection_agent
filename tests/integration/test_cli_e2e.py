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
