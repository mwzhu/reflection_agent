from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import apex_procurement.cli as cli_module
from apex_procurement.cli import PlanningFailure, render_result, run
from apex_procurement.config import RuntimeConfig
from apex_procurement.decisions import AtomicDecisionWriter, CommitFailure, DecisionError
from apex_procurement.domain import (
    AlertCategory,
    ValidationFailureScope,
    ValidationIssue,
    ValidationSeverity,
)
from apex_procurement.explanations import ParsedAlertMarker
from apex_procurement.isolation import (
    build_internal_failure_exclusions,
    reviewed_validation_failure_scope,
)
from apex_procurement.repository import RepositoryLoadError
from apex_procurement.validator import IndependentPlanValidator


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "data" / "scenarios" / "scenario_06_simple.sqlite"
LOCAL_CODE = "RATIONALE_CITATION_MISSING"


def output_rows(path: Path) -> tuple[tuple[object, ...], tuple[object, ...]]:
    with closing(sqlite3.connect(path)) as connection:
        return (
            tuple(connection.execute("SELECT * FROM purchase_orders ORDER BY 1")),
            tuple(connection.execute("SELECT * FROM alerts ORDER BY 1")),
        )


def owned_alerts(path: Path) -> tuple[ParsedAlertMarker, ...]:
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


def injected_issue(
    code: str,
    *,
    component_id: str | None,
    scope: ValidationFailureScope,
) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        severity=ValidationSeverity.ERROR,
        message="Deterministic R15 boundary injection.",
        component_id=component_id,
        failure_scope=scope,
    )


class R15ComponentIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "scenario.sqlite"
        shutil.copy2(SOURCE, self.path)
        baseline = run(RuntimeConfig(self.path, dry_run=True))
        executable = tuple(
            item
            for item in baseline.decisions
            if item.selected_plan is not None
        )
        self.assertGreaterEqual(len(executable), 2)
        self.affected_component = executable[0].component_id

    def local_first_pass_injector(self, seen: list[int]):
        affected = self.affected_component

        def inject(pass_number, _snapshot, _decisions, _results, _exclusions):
            seen.append(pass_number)
            if pass_number != 1:
                return ()
            return (
                injected_issue(
                    LOCAL_CODE,
                    component_id=affected,
                    scope=reviewed_validation_failure_scope(LOCAL_CODE),
                ),
            )

        return inject

    def test_local_failure_commits_only_freshly_revalidated_survivors_and_reruns_no_op(self) -> None:
        seen: list[int] = []
        created: list[IndependentPlanValidator] = []
        actual_validator = IndependentPlanValidator

        def validator_factory(*args, **kwargs):
            validator = actual_validator(*args, **kwargs)
            if kwargs.get("test_issue_injector") is not None:
                created.append(validator)
            return validator

        config = RuntimeConfig(self.path, json_output=True)
        with patch.object(
            cli_module,
            "IndependentPlanValidator",
            side_effect=validator_factory,
        ):
            first = run(
                config,
                _test_validation_issue_injector=self.local_first_pass_injector(
                    seen
                ),
            )

        self.assertEqual(seen, [1, 2])
        self.assertEqual(len(created), 2)
        self.assertIsNot(created[0], created[1])
        self.assertEqual(first.validation_pass_count, 2)
        self.assertTrue(first.validation.is_valid)
        self.assertTrue(
            any(
                item.startswith("component-internal-failure-exclusions-v1:")
                for item in first.validation.checked_invariants
            )
        )
        self.assertEqual(
            tuple(item.component_id for item in first.internal_failure_exclusions),
            (self.affected_component,),
        )
        self.assertNotIn(
            self.affected_component,
            {item.component_id for item in first.decisions},
        )
        self.assertNotIn(
            self.affected_component,
            {item.component_id for item in first.solver_results},
        )
        self.assertNotIn(
            self.affected_component,
            {item.component_id for item in first.outputs.purchase_orders},
        )

        payload = json.loads(render_result(config, first))
        self.assertEqual(payload["status"], "partially-committed")
        self.assertEqual(
            payload["partial_run"]["status"],
            "COMPLETED_WITH_COMPONENT_EXCLUSIONS",
        )
        self.assertEqual(payload["partial_run"]["validation_pass_count"], 2)
        self.assertEqual(
            payload["partial_run"]["excluded_components"][0]["component_id"],
            self.affected_component,
        )
        audit = json.loads(first.audit_json_lines[0])
        self.assertEqual(audit["partial_run"]["validation_pass_count"], 2)
        self.assertEqual(
            audit["partial_run"]["excluded_component_ids"],
            [self.affected_component],
        )

        rows_after_first = output_rows(self.path)
        self.assertTrue(rows_after_first[0])
        self.assertFalse(
            any(row[1] == self.affected_component for row in rows_after_first[0])
        )
        self.assertEqual(len(rows_after_first[0]), len({row[0] for row in rows_after_first[0]}))
        parsed_alerts = owned_alerts(self.path)
        internal = tuple(
            item
            for item in parsed_alerts
            if item.category is AlertCategory.INTERNAL_FAILURE
        )
        self.assertEqual(len(internal), 1)
        self.assertIn(self.affected_component, internal[0].body)
        self.assertIn("Owner: PROCUREMENT_ENGINEERING", internal[0].body)
        self.assertIn("removed every executable action and solver result", internal[0].body)
        self.assertFalse(
            any(
                item.category
                in {AlertCategory.DECISION_REQUIRED, AlertCategory.APPROVAL_REQUIRED}
                and self.affected_component in item.body
                for item in parsed_alerts
            )
        )
        accounting = next(
            item
            for item in parsed_alerts
            if item.category is AlertCategory.RUN_ACCOUNTING
        )
        self.assertIn("INTERNAL_FAILURE_EXCLUDED=1", accounting.body)
        self.assertIn(self.affected_component, accounting.body)
        self.assertIn(LOCAL_CODE, accounting.body)

        second_seen: list[int] = []
        second = run(
            config,
            _test_validation_issue_injector=self.local_first_pass_injector(
                second_seen
            ),
        )
        self.assertEqual(second_seen, [1, 2])
        self.assertTrue(second.commit.no_op)
        self.assertEqual(output_rows(self.path), rows_after_first)
        self.assertEqual(
            len(rows_after_first[0]), len({row[0] for row in rows_after_first[0]})
        )

    def test_second_pass_failure_aborts_every_write(self) -> None:
        before = output_rows(self.path)
        seen: list[int] = []
        affected = self.affected_component

        def inject(pass_number, _snapshot, decisions, _results, _exclusions):
            seen.append(pass_number)
            if pass_number == 1:
                return (
                    injected_issue(
                        LOCAL_CODE,
                        component_id=affected,
                        scope=ValidationFailureScope.COMPONENT_LOCAL,
                    ),
                )
            survivor = decisions[0].component_id
            return (
                injected_issue(
                    "UNKNOWN_SECOND_PASS_CODE",
                    component_id=survivor,
                    scope=ValidationFailureScope.GLOBAL,
                ),
            )

        with self.assertRaisesRegex(
            PlanningFailure, "independent survivor revalidation failed"
        ):
            run(
                RuntimeConfig(self.path),
                _test_validation_issue_injector=inject,
            )
        self.assertEqual(seen, [1, 2])
        self.assertEqual(output_rows(self.path), before)

    def test_commit_boundary_rejects_first_pass_validation_for_exclusions(self) -> None:
        baseline = run(RuntimeConfig(self.path, dry_run=True))
        issue = injected_issue(
            LOCAL_CODE,
            component_id=self.affected_component,
            scope=ValidationFailureScope.COMPONENT_LOCAL,
        )
        exclusions = build_internal_failure_exclusions(
            (issue,),
            {
                item.component_id: item.requirement_id
                for item in baseline.decisions
            },
        )
        survivors = tuple(
            item
            for item in baseline.decisions
            if item.component_id != self.affected_component
        )
        writer = AtomicDecisionWriter(
            self.path,
            baseline.registry.content_hash,
            internal_failure_exclusions=exclusions,
        )

        with self.assertRaisesRegex(
            DecisionError,
            "not bound to the final independent validation",
        ):
            writer.commit(
                baseline.snapshot,
                survivors,
                baseline.validation,
                dry_run=True,
            )

    def test_unknown_unscoped_structural_and_ambiguous_failures_are_global(self) -> None:
        cases = (
            (
                "unscoped",
                LOCAL_CODE,
                None,
                ValidationFailureScope.COMPONENT_LOCAL,
            ),
            (
                "unknown-code",
                "UNKNOWN_VALIDATION_CODE",
                self.affected_component,
                ValidationFailureScope.COMPONENT_LOCAL,
            ),
            (
                "structural",
                "SOURCE_DEMAND_MISMATCH",
                self.affected_component,
                ValidationFailureScope.GLOBAL,
            ),
            (
                "ambiguous-scope",
                LOCAL_CODE,
                self.affected_component,
                ValidationFailureScope.UNKNOWN,
            ),
        )
        for label, code, component_id, scope in cases:
            with self.subTest(label=label):
                before = output_rows(self.path)

                def inject(
                    pass_number,
                    _snapshot,
                    _decisions,
                    _results,
                    _exclusions,
                    *,
                    injected_code=code,
                    injected_component=component_id,
                    injected_scope=scope,
                ):
                    if pass_number != 1:
                        return ()
                    return (
                        injected_issue(
                            injected_code,
                            component_id=injected_component,
                            scope=injected_scope,
                        ),
                    )

                with self.assertRaisesRegex(
                    PlanningFailure, "independent validation failed"
                ):
                    run(
                        RuntimeConfig(self.path),
                        _test_validation_issue_injector=inject,
                    )
                self.assertEqual(output_rows(self.path), before)

    def test_strict_local_failure_writes_nothing(self) -> None:
        before = output_rows(self.path)
        with self.assertRaisesRegex(PlanningFailure, "independent validation failed"):
            run(
                RuntimeConfig(self.path, strict=True),
                _test_validation_issue_injector=self.local_first_pass_injector(
                    []
                ),
            )
        self.assertEqual(output_rows(self.path), before)

    def test_existing_managed_po_on_excluded_component_stops_partial_run(self) -> None:
        first = run(RuntimeConfig(self.path))
        affected = next(
            item.component_id
            for item in first.outputs.purchase_orders
        )
        before = output_rows(self.path)

        def inject(pass_number, _snapshot, _decisions, _results, _exclusions):
            if pass_number != 1:
                return ()
            return (
                injected_issue(
                    LOCAL_CODE,
                    component_id=affected,
                    scope=ValidationFailureScope.COMPONENT_LOCAL,
                ),
            )

        with self.assertRaisesRegex(
            PlanningFailure, "existing managed purchase order"
        ):
            run(
                RuntimeConfig(self.path),
                _test_validation_issue_injector=inject,
            )
        self.assertEqual(output_rows(self.path), before)

    def test_snapshot_ownership_and_commit_failures_never_enter_containment(self) -> None:
        # Structural snapshot failure happens before the test-only validator seam.
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "UPDATE bom SET quantity_per = 'invalid' "
                "WHERE rowid = (SELECT min(rowid) FROM bom)"
            )
        before_snapshot = output_rows(self.path)
        with self.assertRaises(RepositoryLoadError):
            run(
                RuntimeConfig(self.path),
                _test_validation_issue_injector=self.local_first_pass_injector(
                    []
                ),
            )
        self.assertEqual(output_rows(self.path), before_snapshot)

        shutil.copy2(SOURCE, self.path)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            component_id, supplier_id = connection.execute(
                "SELECT component_id, supplier_id FROM supplier_catalog ORDER BY 1, 2 LIMIT 1"
            ).fetchone()
            connection.execute(
                "INSERT INTO purchase_orders "
                "(po_number, component_id, supplier_id, quantity, unit_price, order_date, "
                "expected_delivery_date, rationale) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "APX-badowner",
                    component_id,
                    supplier_id,
                    1,
                    1,
                    "2025-09-01",
                    "2025-09-02",
                    "malformed managed ownership claim",
                ),
            )
        before_ownership = output_rows(self.path)
        with self.assertRaises(DecisionError):
            run(
                RuntimeConfig(self.path),
                _test_validation_issue_injector=self.local_first_pass_injector(
                    []
                ),
            )
        self.assertEqual(output_rows(self.path), before_ownership)

        shutil.copy2(SOURCE, self.path)
        before_commit = output_rows(self.path)
        with patch.object(
            cli_module,
            "commit_decisions",
            side_effect=CommitFailure("forced R15 commit failure"),
        ):
            with self.assertRaises(CommitFailure):
                run(
                    RuntimeConfig(self.path),
                    _test_validation_issue_injector=self.local_first_pass_injector(
                        []
                    ),
                )
        self.assertEqual(output_rows(self.path), before_commit)


if __name__ == "__main__":
    unittest.main()
