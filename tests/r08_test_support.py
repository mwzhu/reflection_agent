"""Small observation helpers shared only by the focused R08 tests."""

from __future__ import annotations

from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
import sqlite3

from apex_procurement.cli import main
from apex_procurement.explanations import ParsedAlertMarker, parse_owned_alert


@dataclass(frozen=True, slots=True)
class CliObservation:
    exit_code: int
    stdout: str
    stderr: str


def run_cli(
    scenario_path: str | Path,
    *,
    contract: str = "benchmark",
    strict: bool = False,
    llm: str = "off",
) -> CliObservation:
    stdout = StringIO()
    stderr = StringIO()
    arguments = [
        "--scenario",
        str(scenario_path),
        f"--contract={contract}",
        f"--llm={llm}",
        "--json",
    ]
    if strict:
        arguments.append("--strict")
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(arguments)
    return CliObservation(int(exit_code), stdout.getvalue(), stderr.getvalue())


def purchase_order_rows(
    scenario_path: str | Path,
) -> tuple[tuple[str, str, float], ...]:
    with closing(sqlite3.connect(Path(scenario_path))) as connection:
        return tuple(
            connection.execute(
                "SELECT component_id, supplier_id, quantity "
                "FROM purchase_orders ORDER BY component_id, supplier_id, quantity"
            )
        )


def owned_alerts(scenario_path: str | Path) -> tuple[ParsedAlertMarker, ...]:
    with closing(sqlite3.connect(Path(scenario_path))) as connection:
        descriptions = tuple(
            row[0]
            for row in connection.execute(
                "SELECT description FROM alerts ORDER BY alert_id"
            )
        )
    parsed = tuple(parse_owned_alert(description) for description in descriptions)
    if any(item is None for item in parsed):
        raise AssertionError("an R08 fixture contains an unowned or malformed alert")
    return tuple(item for item in parsed if item is not None)


__all__ = [
    "CliObservation",
    "owned_alerts",
    "purchase_order_rows",
    "run_cli",
]
