from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from apex_procurement.cli import run
from apex_procurement.config import RuntimeConfig
from apex_procurement.domain import AlertCategory
from apex_procurement.validator import IndependentPlanValidator
from tests.generator import supplied_fixture_path
from tests.r08_mutation_fixtures import (
    build_renamed_magnet_fixture,
    build_replaced_magnet_suppliers_fixture,
    build_unknown_country_fixture,
)
from tests.r08_test_support import owned_alerts, purchase_order_rows, run_cli


def _component_from_scope(scope: str) -> str | None:
    return next((part for part in scope.split(":") if part.startswith("CMP-")), None)


def test_r09_replaced_named_magnet_suppliers_are_component_scoped(
    tmp_path: Path,
) -> None:
    fixture = build_replaced_magnet_suppliers_fixture(tmp_path)

    observed = run_cli(fixture.scenario_path)

    assert observed.exit_code == 0, observed.stderr
    conflicts = {
        _component_from_scope(alert.scope)
        for alert in owned_alerts(fixture.scenario_path)
        if alert.category is AlertCategory.POLICY_CONFLICT
    }
    assert conflicts == {"CMP-003"}

    # The frozen R08 replacement retains the original primary's below-B
    # rating.  Once its memo discharge is correctly dropped, that independent
    # hard gate makes the 20% split infeasible.  Isolate the R09 executability
    # assertion on a second temporary replacement whose remaining hard facts
    # are feasible; the source fixture and the R08 baseline stay unchanged.
    feasible = build_replaced_magnet_suppliers_fixture(tmp_path / "feasible")
    with closing(sqlite3.connect(feasible.scenario_path)) as connection:
        connection.execute(
            "UPDATE suppliers SET sustainability_rating = ? WHERE supplier_id = ?",
            ("B", "SUP-207"),
        )
        connection.commit()

    feasible_observed = run_cli(feasible.scenario_path)

    assert feasible_observed.exit_code == 0, feasible_observed.stderr
    magnet_rows = tuple(
        row for row in purchase_order_rows(feasible.scenario_path) if row[0] == "CMP-003"
    )
    assert magnet_rows, "remaining hard magnet rules must still permit procurement"
    payload = json.loads(feasible_observed.stdout)
    magnet_decision = next(
        item for item in payload["decisions"] if item["component_id"] == "CMP-003"
    )
    assert magnet_decision["selected_plan"] is not None
    quantities = tuple(
        Decimal(line["quantity"])
        for line in magnet_decision["selected_plan"]["lines"]
    )
    assert len(quantities) >= 2
    assert max(quantities) <= Decimal("0.80") * sum(quantities)
    assert "MEMO-2025-041.magnet_rolling_cap" in {
        item["rule_id"] for item in magnet_decision["evidence"]
    }


def test_r10_unknown_supplier_country_crosses_optimizer_validator_cleanly(
    tmp_path: Path,
) -> None:
    fixture = build_unknown_country_fixture(tmp_path)

    observed = run_cli(fixture.scenario_path)

    assert observed.exit_code == 0, observed.stderr
    assert "RATIONALE_MATERIAL_REJECTION_MISMATCH" not in observed.stderr
    assert any(
        component_id == "CMP-014"
        for component_id, _supplier_id, _quantity in purchase_order_rows(
            fixture.scenario_path
        )
    )


def test_r10_unknown_country_route_executes_when_both_readings_are_safe(
    tmp_path: Path,
) -> None:
    fixture = build_unknown_country_fixture(tmp_path)
    with closing(sqlite3.connect(fixture.scenario_path)) as connection:
        connection.execute(
            "UPDATE supplier_catalog SET unit_price = ? "
            "WHERE supplier_id = ? AND component_id = ?",
            (10, "SUP-103", "CMP-014"),
        )
        connection.commit()

    observed = run_cli(fixture.scenario_path)

    assert observed.exit_code == 0, observed.stderr
    payload = json.loads(observed.stdout)
    decision = next(
        item for item in payload["decisions"] if item["component_id"] == "CMP-014"
    )
    assert decision["selected_plan"] is not None
    rejection = next(
        item
        for item in decision["material_rejections"]
        if item["supplier_id"] == "SUP-103"
    )
    assert rejection["eligibility"] == "PASS"
    assert rejection["reason_code"] == "NOT_SELECTED_BY_CERTIFIED_ALLOCATION"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("sustainability_rating", None),
        ("relationship_tier", None),
        ("on_approved_list", None),
    ),
)
def test_r10_supplier_attribute_unknowns_cross_independent_validation(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    scenario = tmp_path / f"unknown-{field}.sqlite"
    shutil.copy2(supplied_fixture_path(6), scenario)
    with closing(sqlite3.connect(scenario)) as connection:
        connection.execute(
            f"UPDATE suppliers SET {field} = ? WHERE supplier_id = ?",
            (value, "SUP-103"),
        )
        connection.commit()

    observed = run_cli(scenario)

    assert observed.exit_code == 0, observed.stderr
    assert "RATIONALE_MATERIAL_REJECTION_MISMATCH" not in observed.stderr
    assert "SOLVER_OBJECTIVE_DISAGREEMENT" not in observed.stderr
    scoped_data_quality = tuple(
        alert
        for alert in owned_alerts(scenario)
        if alert.category is AlertCategory.DATA_QUALITY
        and _component_from_scope(alert.scope) == "CMP-014"
    )
    assert scoped_data_quality
    payload = json.loads(observed.stdout)
    decision = next(
        item for item in payload["decisions"] if item["component_id"] == "CMP-014"
    )
    assert decision["selected_plan"] is not None or decision["requirement_state"][
        "resolution"
    ] == "UNRESOLVED"


def test_r10_independent_validator_rejects_missing_attribute_disclosure(
    tmp_path: Path,
) -> None:
    scenario = tmp_path / "unknown-rating.sqlite"
    shutil.copy2(supplied_fixture_path(6), scenario)
    with closing(sqlite3.connect(scenario)) as connection:
        connection.execute(
            "UPDATE suppliers SET sustainability_rating = NULL "
            "WHERE supplier_id = ?",
            ("SUP-103",),
        )
        connection.commit()

    artifacts = run(RuntimeConfig(scenario, dry_run=True))
    decision = next(
        item
        for item in artifacts.decisions
        if item.component_id == "CMP-014"
    )
    assert AlertCategory.DATA_QUALITY in decision.alert_categories
    stripped = replace(
        decision,
        alert_categories=tuple(
            category
            for category in decision.alert_categories
            if category is not AlertCategory.DATA_QUALITY
        ),
    )
    decisions = tuple(
        stripped if item.component_id == stripped.component_id else item
        for item in artifacts.decisions
    )
    validator = IndependentPlanValidator(
        artifacts.registry,
        policy_parameters=artifacts.registry.parameters_for(
            artifacts.snapshot.configuration.current_date
        ),
    )

    validation = validator.validate(
        artifacts.snapshot,
        decisions,
        artifacts.solver_results,
    )

    assert "SUPPLIER_ATTRIBUTE_DISCLOSURE_MISSING" in {
        issue.code for issue in validation.issues
    }


def test_r10_renamed_magnet_has_a_validated_conservative_outcome(
    tmp_path: Path,
) -> None:
    fixture = build_renamed_magnet_fixture(tmp_path)

    observed = run_cli(fixture.scenario_path)

    assert observed.exit_code == 0, observed.stderr
    assert "RATIONALE_MATERIAL_REJECTION_MISMATCH" not in observed.stderr
    rows = purchase_order_rows(fixture.scenario_path)
    magnet_orders = tuple(
        row for row in rows if row[0] == "CMP-003"
    )
    magnet_terminal_alerts = tuple(
        alert
        for alert in owned_alerts(fixture.scenario_path)
        if _component_from_scope(alert.scope) == "CMP-003"
        and alert.category
        in {AlertCategory.DECISION_REQUIRED, AlertCategory.POLICY_CONFLICT}
    )
    assert not magnet_orders
    assert magnet_terminal_alerts
    assert any(row[0] != "CMP-003" for row in rows), (
        "rule-scoped membership uncertainty must not stop unrelated components"
    )
    payload = json.loads(observed.stdout)
    magnet_decision = next(
        item for item in payload["decisions"] if item["component_id"] == "CMP-003"
    )
    assert magnet_decision["selected_plan"] is None
    assert magnet_decision["requirement_state"]["resolution"] == "UNRESOLVED"
    assumptions = {
        code
        for evidence in magnet_decision["evidence"]
        for code in evidence["assumption_codes"]
    }
    assert {"INFERRED_CONCEPT_MEMBERSHIP", "ROBUST_BOTH_WAYS"} <= assumptions
