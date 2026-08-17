from __future__ import annotations

from contextlib import closing
from decimal import Decimal
import json
from pathlib import Path
import shutil
import sqlite3

from apex_procurement.cli import run
from apex_procurement.config import ModelMode, RuntimeConfig
from apex_procurement.policy.model_adapter import (
    EntityClassification,
    ModelAdapter,
)
from apex_procurement.protocols import Message


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = tuple(
    sorted((PROJECT_ROOT / "data" / "scenarios").glob("scenario_*.sqlite"))
)


class HighConfidenceNegativeClassifier:
    """Deterministic stand-in for accepted negative residual classifications."""

    model = "negative-fixture-test-double"

    def __init__(self) -> None:
        self.calls = 0

    def generate_structured(self, **_: object) -> EntityClassification:
        self.calls += 1
        return EntityClassification(
            False,
            Decimal("0.95"),
            "The bounded component facts do not establish membership in the "
            "reviewed critical-component categories.",
        )


class HeldOutSemanticClassifier:
    model = "semantic-heldout-test-double"

    def generate_structured(self, **kwargs: object) -> EntityClassification:
        messages = kwargs["messages"]
        if (
            not isinstance(messages, tuple)
            or not messages
            or not isinstance(messages[-1], Message)
        ):
            raise TypeError("messages must contain the model request")
        payload = json.loads(messages[-1].content)
        renamed_magnet = str(payload["entity_label"]).startswith(
            "High-Coercivity Sintered Puck"
        )
        return EntityClassification(
            renamed_magnet,
            Decimal("0.96"),
            (
                "Grade 52H plus the rare-earth permanent-magnet description "
                "establishes the reviewed magnet category."
                if renamed_magnet
                else "The bounded facts do not establish this reviewed category."
            ),
        )


def _business_rows(
    path: Path,
) -> tuple[tuple[tuple[object, ...], ...], tuple[tuple[object, ...], ...]]:
    with closing(sqlite3.connect(path)) as connection:
        return (
            tuple(
                connection.execute(
                    "SELECT component_id, supplier_id, quantity, unit_price, "
                    "order_date, expected_delivery_date FROM purchase_orders "
                    "ORDER BY component_id, supplier_id, quantity"
                )
            ),
            tuple(
                connection.execute(
                    "SELECT description FROM alerts ORDER BY description"
                )
            ),
        )


def test_model_expands_candidates_for_the_held_out_renamed_magnet(
    tmp_path: Path,
) -> None:
    source = next(
        item for item in SCENARIOS if item.name == "scenario_01_baseline.sqlite"
    )
    off_path = tmp_path / "renamed-magnet-off.sqlite"
    on_path = tmp_path / "renamed-magnet-on.sqlite"
    shutil.copy2(source, off_path)
    shutil.copy2(source, on_path)
    for path in (off_path, on_path):
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE components SET name = ? WHERE component_id = ?",
                ("High-Coercivity Sintered Puck, Grade 52H", "CMP-003"),
            )

    off = run(RuntimeConfig(off_path, model_mode=ModelMode.OFF))
    on = run(
        RuntimeConfig(on_path, model_mode=ModelMode.AUTO),
        _test_model_adapter=ModelAdapter(HeldOutSemanticClassifier()),
    )

    off_magnet = next(item for item in off.decisions if item.component_id == "CMP-003")
    on_magnet = next(item for item in on.decisions if item.component_id == "CMP-003")
    assert off.validation.is_valid
    assert on.validation.is_valid
    assert off_magnet.selected_plan is None
    assert on_magnet.selected_plan is not None
    assert {
        (item.supplier_id, item.quantity)
        for item in on_magnet.selected_plan.lines
    } == {("SUP-107", Decimal("128")), ("SUP-108", Decimal("80"))}
    assert {
        (item.concept_id, item.result.classification.member)
        for item in on.model_runtime.resolutions
        if item.entity_id == "CMP-003"
    } == {("critical_component", True), ("neodymium_magnet", True)}
    assert {
        item.rule_id for item in on_magnet.evidence if item.rule_id.startswith("MODEL.")
    } == {
        "MODEL.entity_resolution.critical_component",
        "MODEL.entity_resolution.neodymium_magnet",
    }


def test_model_on_off_matrix_uses_every_supplied_scenario_and_preserves_safety(
    tmp_path: Path,
) -> None:
    assert len(SCENARIOS) == 6
    expected_attempts = {
        "scenario_01_baseline.sqlite": 4,
        "scenario_02_partial_procurement.sqlite": 4,
        "scenario_03_tight_timeline.sqlite": 4,
        "scenario_04_low_inventory.sqlite": 4,
        "scenario_05_competing_demand.sqlite": 4,
        "scenario_06_simple.sqlite": 3,
    }

    for source in SCENARIOS:
        off_path = tmp_path / f"off-{source.name}"
        on_path = tmp_path / f"on-{source.name}"
        shutil.copy2(source, off_path)
        shutil.copy2(source, on_path)

        off = run(RuntimeConfig(off_path, model_mode=ModelMode.OFF))
        client = HighConfidenceNegativeClassifier()
        on = run(
            RuntimeConfig(on_path, model_mode=ModelMode.AUTO),
            _test_model_adapter=ModelAdapter(client),
        )

        assert off.validation.is_valid
        assert on.validation.is_valid
        assert on.model_runtime.status == "used_residual_classification"
        assert on.model_runtime.attempted_count == expected_attempts[source.name]
        assert len(on.model_runtime.resolutions) == expected_attempts[source.name]
        assert client.calls == expected_attempts[source.name]
        assert _business_rows(off_path)[1] == _business_rows(on_path)[1]

        off_rows = _business_rows(off_path)[0]
        on_rows = _business_rows(on_path)[0]
        if source.name != "scenario_05_competing_demand.sqlite":
            assert on_rows == off_rows
            continue

        off_pressure = next(row for row in off_rows if row[0] == "CMP-014")
        on_pressure = next(row for row in on_rows if row[0] == "CMP-014")
        assert off_pressure[1:4] == ("SUP-112", 80, 32)
        assert on_pressure[1:4] == ("SUP-103", 80, 22)
        assert on_pressure[5] <= "2025-11-10"
        assert Decimal(str(off_pressure[2])) * Decimal(
            str(off_pressure[3])
        ) - Decimal(str(on_pressure[2])) * Decimal(str(on_pressure[3])) == Decimal(
            "800"
        )
