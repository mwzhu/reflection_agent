from __future__ import annotations

from pathlib import Path

import pytest

from tests.r08_mutation_fixtures import (
    build_pre_memo_date_fixture,
    build_pre_policy_date_fixture,
)
from tests.r08_test_support import owned_alerts, purchase_order_rows, run_cli


@pytest.mark.xfail(
    strict=True,
    reason="R11: effective-date and delivery reconstruction parity",
)
def test_r11_pre_memo_date_crosses_optimizer_validator_cleanly(
    tmp_path: Path,
) -> None:
    fixture = build_pre_memo_date_fixture(tmp_path)

    observed = run_cli(fixture.scenario_path)

    assert observed.exit_code == 0, observed.stderr
    assert "DELIVERY_DATE_MISMATCH" not in observed.stderr
    assert purchase_order_rows(fixture.scenario_path)


@pytest.mark.xfail(
    strict=True,
    reason="R11: effective-date and delivery reconstruction parity",
)
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
