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
            project = tomllib.load(handle)["project"]
        self.assertEqual(project["dependencies"], [])

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


if __name__ == "__main__":
    unittest.main()
