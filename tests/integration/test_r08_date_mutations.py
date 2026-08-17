from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest

from tests.r08_mutation_fixtures import (
    build_pre_memo_date_fixture,
    build_pre_policy_date_fixture,
)
from tests.r08_test_support import owned_alerts, purchase_order_rows, run_cli


def test_r11_pre_memo_date_crosses_optimizer_validator_cleanly(
    tmp_path: Path,
) -> None:
    fixture = build_pre_memo_date_fixture(tmp_path)

    observed = run_cli(fixture.scenario_path)

    assert observed.exit_code == 0, observed.stderr
    for issue_code in (
        "DELIVERY_DATE_MISMATCH",
        "OBJECTIVE_VECTOR_MISMATCH",
        "RATIONALE_COMPARATOR_MISMATCH",
        "UPPER_BOUND_DERIVATION",
    ):
        assert issue_code not in observed.stderr
    assert purchase_order_rows(fixture.scenario_path)


@pytest.mark.parametrize(
    ("scenario_date", "memo_active"),
    (
        ("2025-04-14", False),
        ("2025-04-15", True),
        ("2025-04-16", True),
    ),
)
def test_r11_magnet_memo_boundary_preserves_planner_validator_parity(
    tmp_path: Path,
    scenario_date: str,
    memo_active: bool,
) -> None:
    fixture = build_pre_memo_date_fixture(tmp_path)
    with closing(sqlite3.connect(fixture.scenario_path)) as connection:
        connection.execute(
            'UPDATE scenario_config SET "current_date" = ?',
            (scenario_date,),
        )
        connection.commit()

    observed = run_cli(fixture.scenario_path)

    assert observed.exit_code == 0, observed.stderr
    assert "MISMATCH" not in observed.stderr
    assert purchase_order_rows(fixture.scenario_path)
    audit = json.loads(observed.stderr.splitlines()[0])
    active = set(audit["active_rule_ids"])
    memo_rule = "MEMO-2025-041.magnet_secondary_allocation"
    assert (memo_rule in active) is memo_active


def test_r11_pre_policy_date_is_a_clean_business_refusal(
    tmp_path: Path,
) -> None:
    fixture = build_pre_policy_date_fixture(tmp_path)

    observed = run_cli(fixture.scenario_path)

    assert observed.exit_code == 4, observed.stderr
    assert not purchase_order_rows(fixture.scenario_path)
    assert not owned_alerts(fixture.scenario_path)
    message = observed.stderr.lower()
    assert "no reviewed procurement policy" in message and "effective" in message
    assert "domestic premium rules require" not in message
