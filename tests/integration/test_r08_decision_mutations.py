from __future__ import annotations

from pathlib import Path

import pytest

from apex_procurement.domain import AlertCategory
from tests.r08_mutation_fixtures import (
    build_moq_25_net_need_5_fixture,
    build_stale_named_supplier_id_fixture,
    build_unknown_uom_fixture,
)
from tests.r08_test_support import owned_alerts, purchase_order_rows, run_cli


SUB_MOQ_RULE = "POL-PROC-001.section_4_1.sub_moq_approval"


def _live_sub_moq_alerts(path: Path):
    return tuple(
        alert
        for alert in owned_alerts(path)
        if alert.category is AlertCategory.APPROVAL_REQUIRED
        and SUB_MOQ_RULE in alert.body
    )


@pytest.mark.xfail(
    strict=True,
    reason="R12: coherent MOQ alternatives and alert applicability",
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


@pytest.mark.xfail(
    strict=True,
    reason="R12: coherent MOQ alternatives and alert applicability",
)
def test_r12_second_run_removes_obsolete_sub_moq_approval(
    tmp_path: Path,
) -> None:
    fixture = build_moq_25_net_need_5_fixture(tmp_path)

    first = run_cli(fixture.scenario_path)
    second = run_cli(fixture.scenario_path)

    assert first.exit_code == 0, first.stderr
    assert second.exit_code == 0, second.stderr
    assert '"no_op":true' in second.stdout
    assert purchase_order_rows(fixture.scenario_path) == (
        ("CMP-005", "SUP-101", 25.0),
    )
    assert not _live_sub_moq_alerts(fixture.scenario_path)


@pytest.mark.xfail(
    strict=True,
    reason="R14: required data-quality and assumption disclosures",
)
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


@pytest.mark.xfail(
    strict=True,
    reason="R14: required data-quality and assumption disclosures",
)
def test_r14_unknown_uom_discloses_discrete_rounding(
    tmp_path: Path,
) -> None:
    fixture = build_unknown_uom_fixture(tmp_path)

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
        and "box" in alert.body.lower()
        and "discrete" in alert.body.lower()
    )
    assert len(disclosures) == 1
