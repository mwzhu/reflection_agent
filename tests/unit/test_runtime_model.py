from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from apex_procurement.config import EvidenceContract, ModelMode
from apex_procurement.domain import EvidenceStatus, PlanDisposition
from apex_procurement.ledgers import build_ledgers
from apex_procurement.policy import load_policy_registry
from apex_procurement.policy.entity_resolution import EntityResolver
from apex_procurement.policy.model_adapter import (
    EntityClassification,
    ModelAdapter,
    ModelConfigurationError,
    ModelResponseError,
    ModelUnavailableError,
)
from apex_procurement.policy.runtime_model import (
    MODEL_ASSUMPTION_CODE,
    build_model_runtime,
    model_evidence_for_component,
)
from apex_procurement.repository import SQLiteRepository
from apex_procurement.validator import IndependentPlanValidator


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "data" / "scenarios" / "scenario_06_simple.sqlite"


class FixedClient:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls = 0
        self.model = "deterministic-test-model"

    def generate_structured(self, **_: object) -> object:
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _facts():
    snapshot = SQLiteRepository().load_snapshot(SOURCE)
    registry = load_policy_registry()
    demanded = tuple(
        item.component_id for item in build_ledgers(snapshot).supply_ledgers
    )
    return snapshot, registry, demanded


def test_high_confidence_residuals_are_fingerprint_bound_and_audited() -> None:
    snapshot, registry, demanded = _facts()
    client = FixedClient(
        EntityClassification(
            False,
            Decimal("0.95"),
            "The reviewed category requires an IC or blank not established here.",
        )
    )

    state = build_model_runtime(
        mode=ModelMode.AUTO,
        snapshot=snapshot,
        registry=registry,
        demanded_component_ids=demanded,
        adapter=ModelAdapter(client),
    )

    assert state.status == "used_residual_classification"
    assert state.attempted_count == 3
    assert len(state.resolutions) == 3
    assert client.calls == 3
    assert {
        (item.entity_id, item.concept_id) for item in state.resolutions
    } == {
        ("CMP-005", "critical_component"),
        ("CMP-014", "critical_component"),
        ("CMP-015", "critical_component"),
    }

    component = next(
        item for item in snapshot.components if item.component_id == "CMP-014"
    )
    deterministic = EntityResolver(registry)
    assisted = EntityResolver(
        registry, residual_results=state.classification_map
    )
    assert (
        deterministic.resolve_concept("critical_component", component).status
        is EvidenceStatus.UNKNOWN
    )
    resolved = assisted.resolve_concept("critical_component", component)
    assert resolved.status is EvidenceStatus.FAIL
    assert resolved.method == "model-assisted-residual"
    assert resolved.assumption_codes == (MODEL_ASSUMPTION_CODE,)

    benchmark = model_evidence_for_component(
        state, "CMP-014", EvidenceContract.BENCHMARK
    )
    production = model_evidence_for_component(
        state, "CMP-014", EvidenceContract.PRODUCTION
    )
    assert benchmark[0].contract_disposition is PlanDisposition.EXECUTE_WITH_ASSUMPTION
    assert production[0].contract_disposition is PlanDisposition.DECISION_REQUIRED
    assert benchmark[0].source_references == production[0].source_references

    tampered_fingerprints = dict(state.fingerprint_map)
    tampered_fingerprints[("critical_component", "CMP-014")] = "0" * 64
    independent = IndependentPlanValidator(
        registry,
        model_resolutions=state.classification_map,
        model_fingerprints=tampered_fingerprints,
    )
    assert (
        independent._concept("critical_component", component)
        is EvidenceStatus.UNKNOWN
    )


def test_auto_rejects_low_confidence_and_transport_failure_without_using_results() -> None:
    snapshot, registry, demanded = _facts()
    low = build_model_runtime(
        mode=ModelMode.AUTO,
        snapshot=snapshot,
        registry=registry,
        demanded_component_ids=demanded,
        adapter=ModelAdapter(
            FixedClient(EntityClassification(True, Decimal("0.40"), "weak"))
        ),
    )
    assert low.status == "model_output_insufficient_deterministic_fallback"
    assert low.low_confidence_count == 3
    assert not low.resolutions

    failed = build_model_runtime(
        mode=ModelMode.AUTO,
        snapshot=snapshot,
        registry=registry,
        demanded_component_ids=demanded,
        adapter=ModelAdapter(FixedClient(ModelUnavailableError("offline"))),
    )
    assert failed.status == "unavailable_deterministic_fallback"
    assert len(failed.failures) == 3
    assert not failed.resolutions


def test_required_rejects_a_low_confidence_runtime_result() -> None:
    snapshot, registry, demanded = _facts()
    with pytest.raises(ModelResponseError, match="confidence"):
        build_model_runtime(
            mode=ModelMode.REQUIRED,
            snapshot=snapshot,
            registry=registry,
            demanded_component_ids=demanded,
            adapter=ModelAdapter(
                FixedClient(
                    EntityClassification(False, Decimal("0.50"), "uncertain")
                )
            ),
        )


def test_off_never_calls_an_injected_model() -> None:
    snapshot, registry, demanded = _facts()
    client = FixedClient(AssertionError("model must remain off"))
    state = build_model_runtime(
        mode=ModelMode.OFF,
        snapshot=snapshot,
        registry=registry,
        demanded_component_ids=demanded,
        adapter=ModelAdapter(client),
    )
    assert state.status == "disabled"
    assert state.attempted_count == 0
    assert client.calls == 0


def test_auto_partial_environment_is_a_visible_configuration_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, registry, demanded = _facts()
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.example")
    monkeypatch.delenv("LLM_MODEL", raising=False)

    state = build_model_runtime(
        mode=ModelMode.AUTO,
        snapshot=snapshot,
        registry=registry,
        demanded_component_ids=demanded,
    )

    assert state.status == "unavailable_deterministic_fallback"
    assert state.attempted_count == 0
    assert state.failures[0].error_type == ModelConfigurationError.__name__
