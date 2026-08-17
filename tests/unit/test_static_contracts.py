from pathlib import Path
import importlib
import pkgutil
import re
import subprocess
import sys
import tomllib
import unittest

import apex_procurement


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src"


class StaticContractTests(unittest.TestCase):
    def test_all_package_modules_import_with_only_declared_runtime_dependencies(self) -> None:
        with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
            document = tomllib.load(handle)
        project = document["project"]
        self.assertEqual(project["dependencies"], ["scipy>=1.11"])
        self.assertEqual(
            project["scripts"],
            {"apex-procurement": "apex_procurement.cli:main"},
        )
        self.assertEqual(
            document["tool"]["setuptools"]["package-data"],
            {
                "apex_procurement.policy": [
                    "compiled_policy.json",
                    "concepts.json",
                ]
            },
        )

        module_names = [
            module.name
            for module in pkgutil.walk_packages(
                apex_procurement.__path__,
                prefix="apex_procurement.",
            )
        ]
        self.assertGreaterEqual(len(module_names), 3)
        for module_name in module_names:
            with self.subTest(module=module_name):
                importlib.import_module(module_name)

        script = (
            "import sys;"
            f"sys.path.insert(0,{str(SOURCE_ROOT)!r});"
            + ";".join(f"import {name}" for name in module_names)
        )
        completed = subprocess.run(
            [sys.executable, "-I", "-c", script],
            cwd="/",
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_production_sources_contain_no_supplied_entity_identifier_literals(self) -> None:
        supplied_entity_literal = re.compile(
            r"\b(?:CMP|SUP|FG|PO|EXIST|RM|MFG)-[A-Z0-9]+\b",
            re.IGNORECASE,
        )
        production_files = [PROJECT_ROOT / "agent.py", *sorted(SOURCE_ROOT.rglob("*.py"))]
        violations: list[str] = []
        for path in production_files:
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if supplied_entity_literal.search(line):
                    violations.append(f"{path.relative_to(PROJECT_ROOT)}:{line_number}: {line.strip()}")
        self.assertEqual(violations, [], "\n".join(violations))

    def test_planner_and_validator_do_not_duplicate_load_bearing_policy_literals(self) -> None:
        targets = (
            SOURCE_ROOT / "apex_procurement" / "candidates.py",
            SOURCE_ROOT / "apex_procurement" / "optimizer.py",
            SOURCE_ROOT / "apex_procurement" / "validator.py",
        )
        policy_value_literal = re.compile(
            r"(?<![\w.])(?:0\.35|0\.50|0\.15|0\.10|2500)(?![\w.])"
        )
        numeric_rule_selection = re.compile(
            r"maximum_(?:premium|alternative_savings)_fraction[^\n]{0,120}=="
        )
        delivery_window_literal = re.compile(
            r"(?:_business_day(?:_distance|s)?\([^)]*\)|"
            r"(?:comparable_delivery_days|delivery_day_window)[^\n]{0,40})\s*"
            r"(?:=|<=)\s*5",
            re.DOTALL,
        )
        violations: list[str] = []
        for path in targets:
            source = path.read_text(encoding="utf-8")
            for label, pattern in (
                ("policy value", policy_value_literal),
                ("numeric rule selection", numeric_rule_selection),
                ("delivery window", delivery_window_literal),
            ):
                if pattern.search(source):
                    violations.append(f"{path.name}: {label}")
        self.assertEqual(violations, [], "\n".join(violations))


if __name__ == "__main__":
    unittest.main()
