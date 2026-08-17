from __future__ import annotations

from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from apex_procurement.cli import main


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
        for volatile in ("model_mode", "model_status", "snapshot_digest", "commit"):
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


if __name__ == "__main__":
    unittest.main()
