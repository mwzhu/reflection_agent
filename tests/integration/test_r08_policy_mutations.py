from __future__ import annotations

from contextlib import closing
from decimal import Decimal
import json
from pathlib import Path
import sqlite3

import pytest

from apex_procurement.domain import AlertCategory
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


@pytest.mark.xfail(
    strict=True,
    reason="R10: rule-scoped UNKNOWN and route-fact parity",
)
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


@pytest.mark.xfail(
    strict=True,
    reason="R10: rule-scoped UNKNOWN and route-fact parity",
)
def test_r10_renamed_magnet_has_a_validated_conservative_outcome(
    tmp_path: Path,
) -> None:
    fixture = build_renamed_magnet_fixture(tmp_path)

    observed = run_cli(fixture.scenario_path)

    assert observed.exit_code == 0, observed.stderr
    assert "RATIONALE_MATERIAL_REJECTION_MISMATCH" not in observed.stderr
    magnet_orders = tuple(
        row for row in purchase_order_rows(fixture.scenario_path) if row[0] == "CMP-003"
    )
    magnet_terminal_alerts = tuple(
        alert
        for alert in owned_alerts(fixture.scenario_path)
        if _component_from_scope(alert.scope) == "CMP-003"
        and alert.category
        in {AlertCategory.DECISION_REQUIRED, AlertCategory.POLICY_CONFLICT}
    )
    assert magnet_orders or magnet_terminal_alerts
