"""Small observation helpers shared only by the focused R08 tests."""

from __future__ import annotations

from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
import sqlite3

from apex_procurement.cli import main
from apex_procurement.domain import AlertCategory
from apex_procurement.explanations import ParsedAlertMarker


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
        if connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema "
            "WHERE type = 'table' AND name = 'apex_alert_metadata'"
        ).fetchone()[0] == 0:
            return ()
        rows = tuple(
            row
            for row in connection.execute(
                "SELECT alert_key, category, scope, audit_description "
                "FROM apex_alert_metadata ORDER BY alert_id"
            )
        )
    return tuple(
        ParsedAlertMarker(
            key=key,
            category=AlertCategory(category),
            scope=scope,
            body=audit_description,
        )
        for key, category, scope, audit_description in rows
    )


__all__ = [
    "CliObservation",
    "owned_alerts",
    "purchase_order_rows",
    "run_cli",
]
