from __future__ import annotations

from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
import json
from decimal import Decimal
from pathlib import Path
import shutil
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from apex_procurement.cli import main, run
from apex_procurement.config import EvidenceContract, ModelMode, RuntimeConfig
from apex_procurement.policy.model_adapter import (
    EntityClassification,
    ModelAdapter,
    ModelResponseError,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "data" / "scenarios" / "scenario_06_simple.sqlite"


def output_rows(path: Path) -> tuple[tuple[object, ...], tuple[object, ...]]:
    with closing(sqlite3.connect(path)) as connection:
        return (
            tuple(connection.execute("SELECT * FROM purchase_orders ORDER BY 1")),
            tuple(connection.execute("SELECT * FROM alerts ORDER BY 1")),
        )


class OptionalModelCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)

    def scenario_copy(self, name: str) -> Path:
        path = Path(self.temporary_directory.name) / name
        shutil.copy2(SOURCE, path)
        return path

    def invoke_without_network(self, path: Path, mode: str) -> tuple[int, str, str]:
        output = StringIO()
        audit = StringIO()
        with patch.object(socket, "socket", side_effect=AssertionError("network attempted")):
            with redirect_stdout(output), redirect_stderr(audit):
                code = main(["--scenario", str(path), f"--llm={mode}", "--json"])
        return code, output.getvalue(), audit.getvalue()

    def test_auto_without_provider_falls_back_to_the_off_row_set(self) -> None:
        off_path = self.scenario_copy("off.sqlite")
        auto_path = self.scenario_copy("auto.sqlite")

        off_code, off_stdout, off_stderr = self.invoke_without_network(off_path, "off")
        auto_code, auto_stdout, auto_stderr = self.invoke_without_network(auto_path, "auto")

        self.assertEqual(off_code, 0)
        self.assertEqual(auto_code, 0)
        self.assertEqual(output_rows(auto_path), output_rows(off_path))
        off_payload = json.loads(off_stdout)
        auto_payload = json.loads(auto_stdout)
        self.assertEqual(off_payload["model_status"], "disabled")
        self.assertEqual(
            auto_payload["model_status"],
            "unavailable_deterministic_fallback",
        )
        self.assertEqual(json.loads(off_stderr)["model_status"], "disabled")
        self.assertEqual(
            json.loads(auto_stderr)["model_status"],
            "unavailable_deterministic_fallback",
        )
        for volatile in (
            "model_mode",
            "model_status",
            "model_resolution",
            "snapshot_digest",
            "commit",
        ):
            off_payload.pop(volatile)
            auto_payload.pop(volatile)
        self.assertEqual(auto_payload, off_payload)

    def test_required_without_provider_fails_before_writes(self) -> None:
        path = self.scenario_copy("required.sqlite")
        before = output_rows(path)

        code, _stdout, _stderr = self.invoke_without_network(path, "required")
        self.assertEqual(code, 4)
        failure_audit = json.loads(_stderr.splitlines()[0])
        self.assertEqual(failure_audit["model_mode"], "required")
        self.assertEqual(failure_audit["model_status"], "required_unavailable")
        self.assertEqual(output_rows(path), before)

    def test_configured_model_classifies_supplied_residuals_and_still_validates(self) -> None:
        path = self.scenario_copy("model-assisted.sqlite")

        class ReviewedNegativeClient:
            model = "deterministic-integration-model"

            def generate_structured(self, **_: object) -> EntityClassification:
                return EntityClassification(
                    False,
                    Decimal("0.95"),
                    "The explicit category requires an IC or blank not established by these facts.",
                )

        artifacts = run(
            RuntimeConfig(path, model_mode=ModelMode.AUTO),
            _test_model_adapter=ModelAdapter(ReviewedNegativeClient()),
        )

        self.assertEqual(
            artifacts.model_runtime.status,
            "used_residual_classification",
        )
        self.assertEqual(artifacts.model_runtime.attempted_count, 3)
        self.assertEqual(len(artifacts.model_runtime.resolutions), 3)
        self.assertTrue(artifacts.validation.is_valid)
        cmp014 = next(
            item for item in artifacts.decisions if item.component_id == "CMP-014"
        )
        self.assertIsNotNone(cmp014.selected_plan)
        assert cmp014.selected_plan is not None
        self.assertEqual(
            cmp014.selected_plan.disposition.value,
            "EXECUTE_WITH_ASSUMPTION",
        )
        self.assertIn(
            "MODEL.entity_resolution.critical_component",
            {item.rule_id for item in cmp014.evidence},
        )
        rationale = next(
            row[7] for row in output_rows(path)[0] if row[1] == "CMP-014"
        )
        self.assertIn("high-confidence optional model classification", rationale)

    def test_production_does_not_execute_a_model_dependent_classification(self) -> None:
        path = self.scenario_copy("model-production.sqlite")

        class PositiveClient:
            model = "deterministic-integration-model"

            def generate_structured(self, **_: object) -> EntityClassification:
                return EntityClassification(
                    True,
                    Decimal("0.95"),
                    "The component matches the reviewed category.",
                )

        artifacts = run(
            RuntimeConfig(
                path,
                contract=EvidenceContract.PRODUCTION,
                model_mode=ModelMode.AUTO,
            ),
            _test_model_adapter=ModelAdapter(PositiveClient()),
        )

        self.assertTrue(artifacts.validation.is_valid)
        self.assertEqual(artifacts.commit.committed_po_numbers, ())
        self.assertFalse(output_rows(path)[0])

    def test_auto_model_response_failure_falls_back_without_writes_from_model(self) -> None:
        off_path = self.scenario_copy("failure-off.sqlite")
        auto_path = self.scenario_copy("failure-auto.sqlite")

        class FailingClient:
            model = "failing-integration-model"

            def generate_structured(self, **_: object) -> object:
                raise ModelResponseError("malformed provider response")

        off = run(RuntimeConfig(off_path))
        auto = run(
            RuntimeConfig(auto_path, model_mode=ModelMode.AUTO),
            _test_model_adapter=ModelAdapter(FailingClient()),
        )

        self.assertEqual(
            auto.model_runtime.status,
            "unavailable_deterministic_fallback",
        )
        self.assertTrue(auto.validation.is_valid)
        self.assertEqual(output_rows(auto_path), output_rows(off_path))


if __name__ == "__main__":
    unittest.main()
