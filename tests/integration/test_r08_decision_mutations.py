from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import sqlite3

import pytest

from apex_procurement.cli import run
from apex_procurement.config import RuntimeConfig
from apex_procurement.domain import (
    AlertCategory,
    SourceEntityNormalizationDisclosure,
    UnitNormalizationDisclosure,
)
from apex_procurement.explanations import parse_owned_alert
from apex_procurement.validator import IndependentPlanValidator
from tests.r08_mutation_fixtures import (
    build_moq_25_net_need_5_fixture,
    build_stale_named_supplier_id_fixture,
    build_unknown_uom_fixture,
)
from tests.r08_test_support import owned_alerts, purchase_order_rows, run_cli


SUB_MOQ_RULE = "POL-PROC-001.section_4_1.sub_moq_approval"


def _component_from_scope(scope: str) -> str | None:
    return next(
        (part for part in scope.split(":") if part.startswith("CMP-")),
        None,
    )


def _live_sub_moq_alerts(path: Path):
    return tuple(
        alert
        for alert in owned_alerts(path)
        if alert.category is AlertCategory.APPROVAL_REQUIRED
        and SUB_MOQ_RULE in alert.body
    )


def _business_rows(path: Path):
    with closing(sqlite3.connect(path)) as connection:
        return (
            tuple(
                connection.execute(
                    "SELECT * FROM purchase_orders ORDER BY po_number"
                )
            ),
            tuple(
                connection.execute(
                    "SELECT * FROM alerts ORDER BY alert_id"
                )
            ),
            tuple(
                connection.execute(
                    "SELECT name, seq FROM sqlite_sequence ORDER BY name"
                )
            ),
        )


def test_r12_executed_moq_has_no_mutually_exclusive_live_approval(
    tmp_path: Path,
) -> None:
    fixture = build_moq_25_net_need_5_fixture(tmp_path)

    observed = run_cli(fixture.scenario_path)

    assert observed.exit_code == 0, observed.stderr
    assert purchase_order_rows(fixture.scenario_path) == (
        ("CMP-005", "SUP-101", 25.0),
    )
    assert not _live_sub_moq_alerts(fixture.scenario_path)
    forced = next(
        alert
        for alert in owned_alerts(fixture.scenario_path)
        if alert.category is AlertCategory.FORCED_SURPLUS
    )
    for expected in (
        "supplier SUP-101, quantity 25.0",
        "total cost 300.00",
        "forced surplus 20.0 against net requirement 5.0",
        "no mutually exclusive sub-MOQ approval request remains live",
        "future cancellation contract",
    ):
        assert expected in forced.body


def test_r12_second_run_removes_obsolete_sub_moq_approval(
    tmp_path: Path,
) -> None:
    fixture = build_moq_25_net_need_5_fixture(tmp_path)

    first = run_cli(fixture.scenario_path)
    before = _business_rows(fixture.scenario_path)
    second = run_cli(fixture.scenario_path)

    assert first.exit_code == 0, first.stderr
    assert second.exit_code == 0, second.stderr
    assert '"no_op":true' in second.stdout
    assert purchase_order_rows(fixture.scenario_path) == (
        ("CMP-005", "SUP-101", 25.0),
    )
    assert not _live_sub_moq_alerts(fixture.scenario_path)
    assert _business_rows(fixture.scenario_path) == before


def test_r12_changed_inbound_expires_live_request_and_preserves_human_rows(
    tmp_path: Path,
) -> None:
    fixture = build_moq_25_net_need_5_fixture(tmp_path)
    with closing(sqlite3.connect(fixture.scenario_path)) as connection, connection:
        connection.execute(
            "UPDATE supplier_catalog SET unit_price = ? "
            "WHERE component_id = ? AND supplier_id = ?",
            (3000, "CMP-005", "SUP-101"),
        )

    first = run_cli(fixture.scenario_path)

    assert first.exit_code == 0, first.stderr
    assert not purchase_order_rows(fixture.scenario_path)
    assert _live_sub_moq_alerts(fixture.scenario_path)

    external_commitment = (
        "HUMAN-PO-R12",
        "CMP-005",
        "SUP-101",
        5,
        3000,
        "2025-09-01",
        "2025-09-11",
        "Human-approved inbound commitment; do not modify.",
    )
    human_alert = "Human planner note: preserve this review history."
    with closing(sqlite3.connect(fixture.scenario_path)) as connection, connection:
        connection.execute(
            "INSERT INTO purchase_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            external_commitment,
        )
        cursor = connection.execute(
            "INSERT INTO alerts (description) VALUES (?)",
            (human_alert,),
        )
        human_alert_id = cursor.lastrowid

    changed = run_cli(fixture.scenario_path)

    assert changed.exit_code == 0, changed.stderr
    with closing(sqlite3.connect(fixture.scenario_path)) as connection:
        assert connection.execute(
            "SELECT * FROM purchase_orders WHERE po_number = ?",
            (external_commitment[0],),
        ).fetchone() == external_commitment
        assert connection.execute(
            "SELECT alert_id, description FROM alerts WHERE alert_id = ?",
            (human_alert_id,),
        ).fetchone() == (human_alert_id, human_alert)
        current_owned = owned_alerts(fixture.scenario_path)
    assert not any(
        alert.category is AlertCategory.APPROVAL_REQUIRED
        and SUB_MOQ_RULE in alert.body
        for alert in current_owned
    )

    before_unchanged = _business_rows(fixture.scenario_path)
    unchanged = run_cli(fixture.scenario_path)

    assert unchanged.exit_code == 0, unchanged.stderr
    assert '"no_op":true' in unchanged.stdout
    assert _business_rows(fixture.scenario_path) == before_unchanged


def test_r14_stale_named_supplier_id_discloses_legal_name_resolution(
    tmp_path: Path,
) -> None:
    fixture = build_stale_named_supplier_id_fixture(tmp_path)

    observed = run_cli(fixture.scenario_path)

    assert observed.exit_code == 0, observed.stderr
    magnet_rows = {
        row for row in purchase_order_rows(fixture.scenario_path) if row[0] == "CMP-003"
    }
    assert {row[1] for row in magnet_rows} == {"SUP-108", "SUP-207"}
    disclosures = tuple(
        alert
        for alert in owned_alerts(fixture.scenario_path)
        if alert.category is AlertCategory.DATA_QUALITY
        and "SUP-107" in alert.body
        and "SUP-207" in alert.body
    )
    assert len(disclosures) == 1
    disclosure = disclosures[0]
    assert _component_from_scope(disclosure.scope) == "CMP-003"
    for expected in (
        "Nanjing Rare Earth Co.",
        "MEMO-2025-041",
        "MEMO-2025-041.magnet_named_primary",
        "directive.supplier",
        "one unique exact normalized legal-name match",
    ):
        assert expected in disclosure.body


@pytest.mark.parametrize("source_unit", ("box", "roll"))
def test_r14_unknown_uom_discloses_discrete_rounding(
    tmp_path: Path,
    source_unit: str,
) -> None:
    fixture = build_unknown_uom_fixture(tmp_path)
    if source_unit != "box":
        with closing(sqlite3.connect(fixture.scenario_path)) as connection:
            connection.execute(
                "UPDATE components SET unit_of_measure = ? WHERE component_id = ?",
                (source_unit, "CMP-011"),
            )
            connection.commit()

    observed = run_cli(fixture.scenario_path)

    assert observed.exit_code == 0, observed.stderr
    coating_rows = tuple(
        row for row in purchase_order_rows(fixture.scenario_path) if row[0] == "CMP-011"
    )
    assert coating_rows == (("CMP-011", "SUP-101", 11.0),)
    disclosures = tuple(
        alert
        for alert in owned_alerts(fixture.scenario_path)
        if alert.category is AlertCategory.ASSUMPTION
        and source_unit in alert.body.lower()
        and "discrete" in alert.body.lower()
    )
    assert len(disclosures) == 1
    disclosure = disclosures[0]
    assert _component_from_scope(disclosure.scope) == "CMP-011"
    for expected in (
        "components.unit_of_measure",
        "aggregate exact demand once",
        "round up by ceiling to increment 1",
        "before applying MOQ",
        "No pack size or conversion factor was guessed",
    ):
        assert expected in disclosure.body


def test_r14_validator_rejects_missing_or_incorrect_source_normalization(
    tmp_path: Path,
) -> None:
    fixture = build_stale_named_supplier_id_fixture(tmp_path)
    artifacts = run(RuntimeConfig(fixture.scenario_path, dry_run=True))
    decision = next(
        item for item in artifacts.decisions if item.component_id == "CMP-003"
    )
    disclosure = next(
        item
        for item in decision.normalization_disclosures
        if isinstance(item, SourceEntityNormalizationDisclosure)
    )
    validator = IndependentPlanValidator(
        artifacts.registry,
        policy_parameters=artifacts.registry.parameters_for(
            artifacts.snapshot.configuration.current_date
        ),
    )

    missing = replace(decision, normalization_disclosures=())
    incorrect = replace(
        decision,
        normalization_disclosures=(
            replace(disclosure, resolved_supplier_id="SUP-999"),
        ),
    )
    missing_alert = replace(
        decision,
        alert_categories=tuple(
            category
            for category in decision.alert_categories
            if category is not AlertCategory.DATA_QUALITY
        ),
    )

    for changed, expected_code in (
        (missing, "SOURCE_ID_NORMALIZATION_DISCLOSURE_MISSING"),
        (incorrect, "NORMALIZATION_DISCLOSURE_MISMATCH"),
        (missing_alert, "SOURCE_ID_NORMALIZATION_ALERT_MISSING"),
    ):
        validation = validator.validate(
            artifacts.snapshot,
            tuple(
                changed if item.component_id == changed.component_id else item
                for item in artifacts.decisions
            ),
            artifacts.solver_results,
        )
        assert expected_code in {issue.code for issue in validation.issues}


def test_r14_validator_rejects_missing_or_incorrect_unit_normalization(
    tmp_path: Path,
) -> None:
    fixture = build_unknown_uom_fixture(tmp_path)
    artifacts = run(RuntimeConfig(fixture.scenario_path, dry_run=True))
    decision = next(
        item for item in artifacts.decisions if item.component_id == "CMP-011"
    )
    disclosure = next(
        item
        for item in decision.normalization_disclosures
        if isinstance(item, UnitNormalizationDisclosure)
    )
    validator = IndependentPlanValidator(
        artifacts.registry,
        policy_parameters=artifacts.registry.parameters_for(
            artifacts.snapshot.configuration.current_date
        ),
    )

    missing = replace(decision, normalization_disclosures=())
    incorrect = replace(
        decision,
        normalization_disclosures=(replace(disclosure, increment=Decimal("2")),),
    )
    missing_alert = replace(
        decision,
        alert_categories=tuple(
            category
            for category in decision.alert_categories
            if category is not AlertCategory.ASSUMPTION
        ),
    )

    for changed, expected_code in (
        (missing, "UNIT_NORMALIZATION_DISCLOSURE_MISSING"),
        (incorrect, "NORMALIZATION_DISCLOSURE_MISMATCH"),
        (missing_alert, "UNIT_NORMALIZATION_ALERT_MISSING"),
    ):
        validation = validator.validate(
            artifacts.snapshot,
            tuple(
                changed if item.component_id == changed.component_id else item
                for item in artifacts.decisions
            ),
            artifacts.solver_results,
        )
        assert expected_code in {issue.code for issue in validation.issues}
