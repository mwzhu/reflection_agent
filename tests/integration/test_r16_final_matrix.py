"""Permanent final scenario/contract composition matrix for R16."""

from __future__ import annotations

from contextlib import closing, redirect_stderr, redirect_stdout
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import shutil
import socket
import sqlite3
from unittest.mock import patch

import pytest

from apex_procurement.cli import main
from tests.generator import logical_rows, supplied_fixture_path


SCENARIOS = tuple(supplied_fixture_path(index) for index in range(1, 7))
CONTRACTS = ("benchmark", "production")
BUSINESS_TABLES = ("alerts", "purchase_orders")


def _file_hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _write_state(path: Path) -> tuple[object, ...]:
    """Include surrogate alert IDs and sequence state in the zero-write proof."""

    with closing(sqlite3.connect(path)) as connection:
        return (
            logical_rows(path, tables=BUSINESS_TABLES),
            tuple(
                connection.execute(
                    "SELECT name, seq FROM sqlite_sequence ORDER BY name"
                )
            ),
        )


def _invoke(path: Path, contract: str) -> tuple[int, dict[str, object], dict[str, object]]:
    stdout = StringIO()
    stderr = StringIO()
    with patch.object(socket, "socket", side_effect=AssertionError("network attempted")):
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(
                (
                    "--scenario",
                    str(path),
                    f"--contract={contract}",
                    "--llm=off",
                    "--json",
                )
            )
    assert code == 0, stderr.getvalue()
    audit_lines = stderr.getvalue().splitlines()
    assert len(audit_lines) == 1
    return code, json.loads(stdout.getvalue()), json.loads(audit_lines[0])


def _assert_semantic_dispositions(payload: dict[str, object], contract: str) -> None:
    """Derive executable disposition expectations from each plan's evidence."""

    decisions = payload["decisions"]
    assert isinstance(decisions, list)
    for decision in decisions:
        selected = decision["selected_plan"]
        decision_evidence = decision["evidence"]
        required_unknowns = tuple(
            item
            for item in decision_evidence
            if item["severity"] == "hard"
            and item["status"] == "UNKNOWN"
            and item["scope"] == "rule"
        )
        if contract == "production" and required_unknowns:
            assert selected is None
            assert "DECISION_REQUIRED" in decision["alert_categories"]
            assert all(
                item["contract_disposition"] == "DECISION_REQUIRED"
                for item in required_unknowns
                if item["basis"] == "rolling_window"
            )
            continue
        if selected is None:
            continue

        plan_unknowns = tuple(
            item
            for item in selected["evidence"]
            if item["severity"] == "hard"
            and item["status"] == "UNKNOWN"
            and item["scope"] == "rule"
        )
        rolling_unknowns = tuple(
            item for item in plan_unknowns if item["basis"] == "rolling_window"
        )
        if rolling_unknowns:
            assert contract == "benchmark"
            assert selected["disposition"] == "EXECUTE_WITH_ASSUMPTION"
            assert all(
                item["contract_disposition"] == "EXECUTE_WITH_ASSUMPTION"
                for item in rolling_unknowns
            )
            assert selected["assumption_codes"]
        elif selected["disposition"] == "EXECUTE":
            # Plain execution is allowed only when no hard requirement remains
            # unknown; this is intentionally evidence-derived, not PO-count based.
            assert not plan_unknowns
        else:
            assert selected["disposition"] == "EXECUTE_WITH_ASSUMPTION"
            # Other reviewed benchmark assumptions (for example semantic
            # classification) may also require the stronger disposition.
            assert selected["assumption_codes"]


def _payload_validation_is_valid(payload: dict[str, object]) -> bool:
    validation = payload["validation"]
    return (
        validation["completed"]
        and validation["exact_decimal_checks_completed"]
        and validation["solver_results_verified"]
        and not any(
            item["severity"] == "ERROR" for item in validation["issues"]
        )
    )


@pytest.mark.parametrize("source", SCENARIOS, ids=lambda path: path.stem)
@pytest.mark.parametrize("contract", CONTRACTS)
def test_six_scenario_contract_matrix_is_source_safe_and_zero_write_idempotent(
    tmp_path: Path,
    source: Path,
    contract: str,
) -> None:
    source_hash = _file_hash(source)
    source_rows = logical_rows(source)
    scenario = tmp_path / f"{contract}-{source.name}"
    shutil.copy2(source, scenario)
    initial_business_rows = logical_rows(scenario, tables=BUSINESS_TABLES)

    _first_code, first, first_audit = _invoke(scenario, contract)
    first_state = _write_state(scenario)

    assert first["status"] == "committed"
    assert first["contract"] == contract
    assert first["model_mode"] == "off"
    assert first["model_status"] == "disabled"
    assert _payload_validation_is_valid(first)
    assert first_audit["contract"] == contract
    assert first_audit["model_status"] == "disabled"
    assert first_audit["validation"]["is_valid"]
    _assert_semantic_dispositions(first, contract)

    if contract == "benchmark":
        assert first["commit"]["committed_po_numbers"]
        assert any(
            item["selected_plan"] is not None for item in first["decisions"]
        )
    else:
        assert first["commit"]["committed_po_numbers"] == []
        assert logical_rows(scenario, tables=("purchase_orders",)) == {
            "purchase_orders": initial_business_rows["purchase_orders"]
        }

    _second_code, second, second_audit = _invoke(scenario, contract)
    second_state = _write_state(scenario)

    assert second["commit"]["no_op"] is True
    assert second["commit"]["committed_po_numbers"] == []
    assert second["commit"]["inserted_alert_count"] == 0
    assert second["commit"]["deleted_alert_count"] == 0
    assert second_audit["commit"]["no_op"] is True
    assert second_audit["commit"]["inserted_alert_count"] == 0
    assert second_audit["commit"]["deleted_alert_count"] == 0
    assert second_state == first_state
    assert _file_hash(source) == source_hash
    assert logical_rows(source) == source_rows
