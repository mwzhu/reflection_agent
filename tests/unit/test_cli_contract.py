from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class CliContractTests(unittest.TestCase):
    def test_help_succeeds_from_unrelated_working_directory_in_isolated_mode(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-I", str(PROJECT_ROOT / "agent.py"), "--help"],
            cwd="/",
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        for flag in (
            "--scenario",
            "--contract",
            "--llm",
            "--dry-run",
            "--explain",
            "--strict",
            "--alert-prefixes",
            "--json",
        ):
            self.assertIn(flag, completed.stdout)
        self.assertNotIn("--recompile-policy", completed.stdout)
        self.assertIn("{benchmark,production}", completed.stdout)
        self.assertIn("{off,auto,required}", completed.stdout)
        self.assertIn("LLM_BASE_URL/LLM_MODEL", completed.stdout)
        self.assertIn("required exits 4", completed.stdout)
        self.assertIn("partial-survivor", completed.stdout)
        self.assertIn("production withholds", completed.stdout)
        self.assertIn("atomic commit 7", completed.stdout)

    def test_help_does_not_attempt_network_access(self) -> None:
        import agent

        output = StringIO()
        with patch("socket.socket", side_effect=AssertionError("network access attempted")):
            with redirect_stdout(output):
                with self.assertRaises(SystemExit) as exit_context:
                    agent.build_parser().parse_args(["--help"])
        self.assertEqual(exit_context.exception.code, 0)

    def test_help_succeeds_when_no_data_directory_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            isolated_root = Path(temporary_directory)
            shutil.copy2(PROJECT_ROOT / "agent.py", isolated_root / "agent.py")
            shutil.copytree(PROJECT_ROOT / "src", isolated_root / "src")
            self.assertFalse((isolated_root / "data").exists())

            completed = subprocess.run(
                [sys.executable, "-I", str(isolated_root / "agent.py"), "--help"],
                cwd="/",
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_defaults_are_benchmark_and_model_off(self) -> None:
        import agent
        from apex_procurement.config import EvidenceContract, ModelMode

        config = agent.parse_config(["--scenario", "input.sqlite"])

        self.assertEqual(config.scenario_path, Path("input.sqlite"))
        self.assertIs(config.contract, EvidenceContract.BENCHMARK)
        self.assertIs(config.model_mode, ModelMode.OFF)
        self.assertFalse(config.dry_run)

    def test_readme_documents_install_command_contracts_and_exit_codes(self) -> None:
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

        for expected in (
            "python3 -m pip install -e '.[test]'",
            "python3 agent.py",
            "--scenario data/scenarios/scenario_06_simple.sqlite",
            "--contract benchmark",
            "--llm off",
            "benchmark",
            "production",
            "deterministic and offline",
            "MODEL_RESIDUAL_CLASSIFICATION",
            "LLM_BASE_URL",
            "LLM_API_KEY",
            "partial result",
            "compiled_policy.json",
            "Monday–Friday",
            "no holiday calendar",
            "no supplier capacity history",
            "No safety-stock",
        ):
            self.assertIn(expected, readme)
        for code in range(8):
            if code == 1:
                continue
            self.assertIn(f"| {code} |", readme)
        self.assertNotIn("`--recompile-policy` flag is available", readme)


if __name__ == "__main__":
    unittest.main()
