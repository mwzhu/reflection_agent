from __future__ import annotations

from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import apex_procurement.cli as cli_module
from apex_procurement.cli import ExitCode, main, run
from apex_procurement.config import RuntimeConfig
from apex_procurement.decisions import (
    AtomicDecisionWriter,
    CommitStep,
)
from apex_procurement.domain import SolverStatus, ValidationResult
from apex_procurement.optimizer import (
    IntegerScaledSolver,
    ProcurementOptimizer,
    SolverLimits,
)
from apex_procurement.repository import MAX_TEXT_BYTES, SQLiteRepository


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "data" / "scenarios" / "scenario_06_simple.sqlite"


def output_rows(path: Path) -> tuple[tuple[object, ...], tuple[object, ...]]:
    with closing(sqlite3.connect(path)) as connection:
        orders = tuple(connection.execute("SELECT * FROM purchase_orders ORDER BY 1"))
        alerts = tuple(connection.execute("SELECT * FROM alerts ORDER BY 1"))
    return orders, alerts


class OperationalHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "scenario.sqlite"
        shutil.copy2(SOURCE, self.path)

    def cover_inventory(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE inventory SET quantity_on_hand = ?", (1_000_000,))

    def invoke_main(self, *extra: str) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--scenario", str(self.path), *extra])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_one_concurrent_change_replans_once_and_commits_without_duplicates(self) -> None:
        self.cover_inventory()
        original_commit = cli_module.commit_decisions
        calls = 0

        def race_once(*args: object, **kwargs: object):
            nonlocal calls
            calls += 1
            if calls == 1:
                with closing(sqlite3.connect(self.path)) as connection, connection:
                    connection.execute(
                        "UPDATE inventory SET quantity_on_hand = quantity_on_hand + ?",
                        (1,),
                    )
            return original_commit(*args, **kwargs)

        with patch.object(cli_module, "commit_decisions", side_effect=race_once):
            artifacts = run(RuntimeConfig(self.path))

        self.assertEqual(calls, 2)
        self.assertIn('"replan_count":1', artifacts.audit_json_lines[0])
        with closing(sqlite3.connect(self.path)) as connection:
            counts = connection.execute(
                "SELECT COUNT(*), COUNT(DISTINCT po_number) FROM purchase_orders"
            ).fetchone()
        self.assertEqual(counts[0], counts[1])

    def test_second_concurrent_change_exits_six_without_output_writes(self) -> None:
        self.cover_inventory()
        before = output_rows(self.path)
        original_commit = cli_module.commit_decisions
        calls = 0

        def race_twice(*args: object, **kwargs: object):
            nonlocal calls
            calls += 1
            with closing(sqlite3.connect(self.path)) as connection, connection:
                connection.execute(
                    "UPDATE inventory SET quantity_on_hand = quantity_on_hand + ?",
                    (1,),
                )
            return original_commit(*args, **kwargs)

        with patch.object(cli_module, "commit_decisions", side_effect=race_twice):
            code, _, stderr = self.invoke_main()

        self.assertEqual(calls, 2)
        self.assertEqual(code, ExitCode.CONCURRENT_MODIFICATION)
        self.assertIn('"exit_code":6', stderr)
        self.assertEqual(output_rows(self.path), before)

    def test_audit_log_contains_required_facts_but_not_source_text(self) -> None:
        self.cover_inventory()
        secret = "do-not-log-this-database-secret"
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "UPDATE scenario_config SET scenario_description = ?", (secret,)
            )

        code, _, stderr = self.invoke_main("--dry-run")

        self.assertEqual(code, ExitCode.SUCCESS)
        lines = stderr.splitlines()
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload["event"], "run_completed")
        self.assertEqual(payload["contract"], "benchmark")
        self.assertIn("scenario_file", payload["hashes"])
        self.assertIn("policy_pack", payload["hashes"])
        self.assertIn("rules", payload["rule_versions"])
        self.assertIn("solver_outcomes", payload)
        self.assertTrue(payload["validation"]["is_valid"])
        self.assertIn("total", payload["timings_us"])
        self.assertNotIn(secret, stderr)
        self.assertNotIn("source_quote", stderr)

    def test_sql_text_path_confusion_and_symlink_do_not_change_query_scope(self) -> None:
        hostile_path = Path(self.temporary_directory.name) / (
            "scenario.sqlite?mode=rwc;DROP_TABLE_alerts--"
        )
        shutil.copy2(SOURCE, hostile_path)
        snapshot = SQLiteRepository().load_snapshot(hostile_path)
        self.assertTrue(snapshot.state_digest)
        with closing(sqlite3.connect(hostile_path)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type = ?", ("table",)
                )
            }
        self.assertIn("alerts", tables)

        link = Path(self.temporary_directory.name) / "linked.sqlite"
        link.symlink_to(hostile_path)
        code, _, stderr = self.invoke_main_for_path(link)
        self.assertEqual(code, ExitCode.CLI_OR_PATH)
        self.assertIn("symbolic link", stderr)

    def invoke_main_for_path(self, path: Path) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--scenario", str(path), "--dry-run"])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_hostile_control_malformed_unicode_and_oversized_text_fail_closed(self) -> None:
        mutations = (
            (
                "control",
                "UPDATE components SET description = ? WHERE rowid = "
                "(SELECT min(rowid) FROM components)",
                ("terminal\x1bcontrol",),
            ),
            (
                "malformed-unicode",
                "UPDATE scenario_config SET scenario_description = CAST(X'80' AS TEXT)",
                (),
            ),
            (
                "oversized",
                "UPDATE scenario_config SET scenario_description = ?",
                ("x" * (MAX_TEXT_BYTES + 1),),
            ),
        )
        for name, sql, parameters in mutations:
            with self.subTest(name=name):
                shutil.copy2(SOURCE, self.path)
                before = output_rows(self.path)
                with closing(sqlite3.connect(self.path)) as connection, connection:
                    connection.execute(sql, parameters)
                code, _, _ = self.invoke_main("--dry-run")
                self.assertEqual(code, ExitCode.INVALID_SCENARIO)
                self.assertEqual(output_rows(self.path), before)

    def test_timeout_and_validator_failure_exit_five_without_writes(self) -> None:
        before = output_rows(self.path)
        forced_optimizer = ProcurementOptimizer(
            IntegerScaledSolver(
                limits=SolverLimits(force_status=SolverStatus.TIMEOUT)
            )
        )
        with patch.object(
            cli_module, "ProcurementOptimizer", return_value=forced_optimizer
        ):
            timeout_code, _, timeout_stderr = self.invoke_main()
        self.assertEqual(timeout_code, ExitCode.SOLVER_OR_VALIDATOR)
        self.assertIn('"exit_code":5', timeout_stderr)
        self.assertEqual(output_rows(self.path), before)

        self.cover_inventory()
        before_validation = output_rows(self.path)
        invalid = ValidationResult(
            completed=False,
            exact_decimal_checks_completed=False,
            solver_results_verified=False,
            checked_invariants=(),
        )
        with patch.object(
            cli_module.IndependentPlanValidator,
            "validate",
            return_value=invalid,
        ):
            validator_code, _, validator_stderr = self.invoke_main()
        self.assertEqual(validator_code, ExitCode.SOLVER_OR_VALIDATOR)
        self.assertIn('"exit_code":5', validator_stderr)
        self.assertEqual(output_rows(self.path), before_validation)

    def test_commit_failure_rolls_back_and_exits_seven(self) -> None:
        self.cover_inventory()
        before = output_rows(self.path)

        def failing_commit(
            scenario_path: Path,
            snapshot: object,
            decisions: object,
            validation: object,
            policy_pack_version: str,
            **kwargs: object,
        ):
            def fail_after_alerts(step: CommitStep) -> None:
                if step is CommitStep.ALERTS_RECONCILED:
                    raise sqlite3.OperationalError("forced commit failure")

            writer = AtomicDecisionWriter(
                scenario_path,
                policy_pack_version,
                policy_registry=kwargs.get("policy_registry"),  # type: ignore[arg-type]
                active_directives=kwargs.get("active_directives", ()),  # type: ignore[arg-type]
                inactive_directives=kwargs.get("inactive_directives", ()),  # type: ignore[arg-type]
                visible_alert_prefixes=bool(kwargs.get("visible_alert_prefixes", False)),
                step_hook=fail_after_alerts,
            )
            return writer.commit(
                snapshot,  # type: ignore[arg-type]
                decisions,  # type: ignore[arg-type]
                validation,  # type: ignore[arg-type]
                dry_run=bool(kwargs.get("dry_run", False)),
            )

        with patch.object(cli_module, "commit_decisions", side_effect=failing_commit):
            code, _, stderr = self.invoke_main()

        self.assertEqual(code, ExitCode.COMMIT_FAILURE)
        self.assertIn('"exit_code":7', stderr)
        self.assertEqual(output_rows(self.path), before)


if __name__ == "__main__":
    unittest.main()
