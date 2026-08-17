"""R16 contract/strictness sweep over every frozen R08 mutation."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import socket
from unittest.mock import patch

import pytest

import apex_procurement.cli as cli_module
from apex_procurement.cli import run
from apex_procurement.config import EvidenceContract, RuntimeConfig
from apex_procurement.policy import load_policy_registry
from apex_procurement.validator import IndependentPlanValidator
from tests.generator import logical_rows
from tests.r08_mutation_fixtures import (
    DATABASE_MUTATION_BUILDERS,
    build_pre_policy_date_fixture,
    build_renamed_magnet_fixture,
    build_replaced_magnet_suppliers_fixture,
    build_unrendered_withholding_policy_fixture,
)
from tests.r08_test_support import run_cli


CONTRACTS = ("benchmark", "production")


def _file_hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "builder",
    DATABASE_MUTATION_BUILDERS,
    ids=lambda builder: builder.__name__.removeprefix("build_").removesuffix("_fixture"),
)
@pytest.mark.parametrize("contract", CONTRACTS)
def test_every_database_mutation_has_standard_and_strict_contract_parity(
    tmp_path: Path,
    builder,
    contract: str,
) -> None:
    fixtures = {
        strict: builder(tmp_path / ("strict" if strict else "standard"))
        for strict in (False, True)
    }
    source = fixtures[False].source_scenario_path
    source_hash = _file_hash(source)
    source_rows = logical_rows(source)
    observations = {}

    with patch.object(socket, "socket", side_effect=AssertionError("network attempted")):
        for strict, fixture in fixtures.items():
            observations[strict] = run_cli(
                fixture.scenario_path,
                contract=contract,
                strict=strict,
            )

    expected_exit = 4 if builder is build_pre_policy_date_fixture else 0
    assert {item.exit_code for item in observations.values()} == {expected_exit}
    if expected_exit == 4:
        assert all(
            "no reviewed procurement policy" in item.stderr.lower()
            for item in observations.values()
        )
    else:
        payloads = {
            strict: json.loads(item.stdout)
            for strict, item in observations.items()
        }
        assert all(
            payload["validation"]["completed"]
            and payload["validation"]["exact_decimal_checks_completed"]
            and payload["validation"]["solver_results_verified"]
            and not any(
                issue["severity"] == "ERROR"
                for issue in payload["validation"]["issues"]
            )
            for payload in payloads.values()
        )
        assert logical_rows(
            fixtures[False].scenario_path,
            tables=("alerts", "purchase_orders"),
        ) == logical_rows(
            fixtures[True].scenario_path,
            tables=("alerts", "purchase_orders"),
        )
        if contract == "production":
            assert all(
                payload["commit"]["committed_po_numbers"] == []
                for payload in payloads.values()
            )
            if builder in {
                build_renamed_magnet_fixture,
                build_replaced_magnet_suppliers_fixture,
            }:
                for payload in payloads.values():
                    magnet = next(
                        item
                        for item in payload["decisions"]
                        if item["component_id"] == "CMP-003"
                    )
                    diagnostics = tuple(
                        plan
                        for plan in magnet["alternatives"]
                        if plan["disposition"] == "DECISION_REQUIRED"
                        and any(
                            evidence["basis"] == "rolling_window"
                            and evidence["status"] == "UNKNOWN"
                            for evidence in plan["evidence"]
                        )
                    )
                    assert diagnostics
                    assert magnet["selected_plan"] is None
                    if builder is build_replaced_magnet_suppliers_fixture:
                        assert any(
                            rule_id.endswith(":additional-review")
                            for plan in diagnostics
                            for line in plan["lines"]
                            for rule_id in line["approval_rule_ids"]
                        )

    assert _file_hash(source) == source_hash
    assert logical_rows(source) == source_rows


@pytest.mark.parametrize("contract", CONTRACTS)
@pytest.mark.parametrize("strict", (False, True), ids=("standard", "strict"))
def test_unrendered_policy_mutation_fails_at_cli_pack_boundary_in_every_mode(
    tmp_path: Path,
    contract: str,
    strict: bool,
) -> None:
    fixture = build_unrendered_withholding_policy_fixture(tmp_path)
    assert fixture.pack_path is not None
    assert fixture.concepts_path is not None
    before = logical_rows(fixture.scenario_path, tables=("alerts", "purchase_orders"))

    def load_mutated_registry():
        return load_policy_registry(
            pack_path=fixture.pack_path,
            concepts_path=fixture.concepts_path,
            project_root=None,
        )

    with patch.object(cli_module, "load_policy_registry", load_mutated_registry):
        observed = run_cli(
            fixture.scenario_path,
            contract=contract,
            strict=strict,
        )

    assert observed.exit_code == 4
    assert "render" in observed.stderr.lower()
    assert logical_rows(
        fixture.scenario_path,
        tables=("alerts", "purchase_orders"),
    ) == before


def test_production_diagnostic_cannot_drop_a_pending_below_b_review(
    tmp_path: Path,
) -> None:
    fixture = build_replaced_magnet_suppliers_fixture(tmp_path)
    artifacts = run(
        RuntimeConfig(
            fixture.scenario_path,
            contract=EvidenceContract.PRODUCTION,
            dry_run=True,
        )
    )
    magnet = next(
        item for item in artifacts.decisions if item.component_id == "CMP-003"
    )
    diagnostic = next(
        plan
        for plan in magnet.alternatives
        if any(
            rule_id.endswith(":additional-review")
            for line in plan.lines
            for rule_id in line.approval_rule_ids
        )
    )
    stripped_lines = tuple(
        replace(line, approval_rule_ids=()) for line in diagnostic.lines
    )
    stripped = replace(
        diagnostic,
        lines=stripped_lines,
        unresolved_approval_ids=(),
    )
    changed_decisions = tuple(
        replace(
            decision,
            alternatives=tuple(
                stripped if plan.plan_id == diagnostic.plan_id else plan
                for plan in decision.alternatives
            ),
        )
        if decision.component_id == magnet.component_id
        else decision
        for decision in artifacts.decisions
    )
    validator = IndependentPlanValidator(
        artifacts.registry,
        policy_parameters=artifacts.registry.parameters_for(
            artifacts.snapshot.configuration.current_date
        ),
    )

    validation = validator.validate(
        artifacts.snapshot,
        changed_decisions,
        artifacts.solver_results,
    )

    assert "BELOW_B_REVIEW_UNREPRESENTED" in {
        issue.code for issue in validation.issues
    }
