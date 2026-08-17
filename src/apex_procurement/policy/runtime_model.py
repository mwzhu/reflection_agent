"""Runtime wiring for bounded residual entity classification.

The live model may classify only policy-concept membership that the reviewed
deterministic resolver leaves ``UNKNOWN``.  It cannot introduce suppliers,
commercial facts, policy rules, approvals, or quantities.  Accepted results
are converted into explicit evidence-contract assumptions before they can
reach optimization or persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping, Sequence

from ..candidates import component_fingerprint
from ..config import EvidenceContract, ModelMode
from ..domain import (
    Component,
    EvidenceBasis,
    EvidenceResult,
    EvidenceScope,
    EvidenceStatus,
    PlanDisposition,
    RuleSeverity,
    ScenarioSnapshot,
)
from ..serialization import canonical_dumps, sanitize_control_characters
from .entity_resolution import EntityResolver
from .model_adapter import (
    ModelAdapter,
    ModelAdapterError,
    ModelConfigurationError,
    ModelResponseError,
    OpenAICompatibleModelClient,
    ResidualEntityResult,
)
from .registry import PolicyRegistry


MODEL_CLASSIFICATION_MIN_CONFIDENCE = Decimal("0.85")
MODEL_ASSUMPTION_CODE = "MODEL_RESIDUAL_CLASSIFICATION"
MODEL_RULE_PREFIX = "MODEL.entity_resolution."


@dataclass(frozen=True, slots=True)
class RuntimeModelResolution:
    """One accepted, fingerprint-bound residual classification."""

    concept_id: str
    entity_id: str
    entity_fingerprint: str
    document_hash: str
    result: ResidualEntityResult

    def __post_init__(self) -> None:
        if not self.concept_id or not self.entity_id:
            raise ValueError("runtime model resolution IDs must be non-empty")
        if len(self.entity_fingerprint) != 64 or any(
            character not in "0123456789abcdef"
            for character in self.entity_fingerprint
        ):
            raise ValueError("entity_fingerprint must be a lowercase SHA-256 digest")
        document = self.document_hash.removeprefix("sha256:")
        if len(document) != 64 or any(
            character not in "0123456789abcdef" for character in document
        ):
            raise ValueError("document_hash must be a SHA-256 digest")
        if not isinstance(self.result, ResidualEntityResult):
            raise TypeError("result must be ResidualEntityResult")
        if self.result.classification.confidence < MODEL_CLASSIFICATION_MIN_CONFIDENCE:
            raise ValueError("runtime model resolution is below the reviewed confidence floor")
        if self.result.trace.output_hash is None:
            raise ValueError("accepted runtime model resolution requires an output hash")

    @property
    def key(self) -> tuple[str, str]:
        return self.concept_id, self.entity_id


@dataclass(frozen=True, slots=True)
class RuntimeModelFailure:
    concept_id: str
    entity_id: str
    error_type: str

    def __post_init__(self) -> None:
        if not self.concept_id or not self.entity_id or not self.error_type:
            raise ValueError("runtime model failure fields must be non-empty")


@dataclass(frozen=True, slots=True)
class ModelRuntimeState:
    """Safe model telemetry plus the accepted classifications for one run."""

    status: str
    model_identifier: str | None
    attempted_count: int = 0
    low_confidence_count: int = 0
    resolutions: tuple[RuntimeModelResolution, ...] = ()
    failures: tuple[RuntimeModelFailure, ...] = ()

    def __post_init__(self) -> None:
        if not self.status:
            raise ValueError("model runtime status must be non-empty")
        if self.model_identifier is not None and not self.model_identifier.strip():
            raise ValueError("model_identifier must be non-empty when provided")
        for name in ("attempted_count", "low_confidence_count"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if any(
            not isinstance(item, RuntimeModelResolution)
            for item in self.resolutions
        ):
            raise TypeError("resolutions must contain RuntimeModelResolution values")
        if any(not isinstance(item, RuntimeModelFailure) for item in self.failures):
            raise TypeError("failures must contain RuntimeModelFailure values")
        keys = tuple(item.key for item in self.resolutions)
        if len(keys) != len(set(keys)):
            raise ValueError("runtime model resolutions contain duplicate keys")
        call_failure_count = sum(
            item.concept_id != "runtime" for item in self.failures
        )
        if (
            len(self.resolutions) + self.low_confidence_count + call_failure_count
            > self.attempted_count
        ):
            raise ValueError("model outcome counts exceed attempted_count")
        object.__setattr__(
            self,
            "resolutions",
            tuple(sorted(self.resolutions, key=lambda item: item.key)),
        )
        object.__setattr__(
            self,
            "failures",
            tuple(
                sorted(
                    self.failures,
                    key=lambda item: (item.concept_id, item.entity_id, item.error_type),
                )
            ),
        )

    @property
    def classification_map(
        self,
    ) -> Mapping[tuple[str, str], ResidualEntityResult]:
        return MappingProxyType(
            {item.key: item.result for item in self.resolutions}
        )

    @property
    def fingerprint_map(self) -> Mapping[tuple[str, str], str]:
        return MappingProxyType(
            {item.key: item.entity_fingerprint for item in self.resolutions}
        )


def disabled_model_runtime() -> ModelRuntimeState:
    return ModelRuntimeState("disabled", None)


def _active_component_concepts(
    registry: PolicyRegistry,
    snapshot: ScenarioSnapshot,
) -> tuple[str, ...]:
    concepts = {
        str(item["concept_id"]): item for item in registry.concepts["concepts"]
    }
    result: set[str] = set()
    for rule in registry.active_rules(snapshot.configuration.current_date):
        selector = rule.data.get("selector")
        if not isinstance(selector, Mapping):
            continue
        for raw_id in selector.get("semantic_tags", ()):
            concept_id = str(raw_id)
            concept = concepts[concept_id]
            if concept.get("entity_kind") == "component":
                result.add(concept_id)
    return tuple(sorted(result))


def _component_evidence_text(
    component: Component,
    concept: Mapping[str, object],
) -> str:
    """Serialize bounded source facts as data, never model instructions."""

    return canonical_dumps(
        {
            "component": {
                "name": component.name,
                "description": component.description,
                "category": component.category,
                "unit_of_measure": component.unit_of_measure,
                "is_hazardous": component.is_hazardous,
                "required_certifications": component.required_certifications,
            },
            "reviewed_concept": {
                "source_quote": concept.get("source_quote"),
                "source_terms": concept.get("source_terms", ()),
                "synonyms": concept.get("synonyms", ()),
                "positive_fixtures": concept.get("positive_fixtures", ()),
                "negative_fixtures": concept.get("negative_fixtures", ()),
            },
        }
    )


def _model_identifier(adapter: ModelAdapter) -> str:
    value = getattr(adapter.client, "model", None)
    return str(value) if isinstance(value, str) and value.strip() else "injected-model-client"


def build_model_runtime(
    *,
    mode: ModelMode,
    snapshot: ScenarioSnapshot,
    registry: PolicyRegistry,
    demanded_component_ids: Sequence[str],
    adapter: ModelAdapter | None = None,
) -> ModelRuntimeState:
    """Resolve active component-concept residuals under the selected mode.

    ``auto`` treats configuration, transport, and response failures as a
    visible deterministic fallback. ``required`` propagates those failures so
    the CLI can stop before optimization or writes.
    """

    if not isinstance(mode, ModelMode):
        raise TypeError("mode must be ModelMode")
    if mode is ModelMode.OFF:
        return disabled_model_runtime()
    if adapter is None:
        try:
            client = OpenAICompatibleModelClient.from_environment()
        except ModelConfigurationError:
            if mode is ModelMode.REQUIRED:
                raise
            return ModelRuntimeState(
                "unavailable_deterministic_fallback",
                None,
                failures=(
                    RuntimeModelFailure("runtime", "configuration", "ModelConfigurationError"),
                ),
            )
        if client is None:
            if mode is ModelMode.REQUIRED:
                raise ModelConfigurationError(
                    "LLM_BASE_URL and LLM_MODEL are required for --llm=required"
                )
            return ModelRuntimeState("unavailable_deterministic_fallback", None)
        adapter = ModelAdapter(client)

    model_identifier = _model_identifier(adapter)
    deterministic = EntityResolver(registry)
    concepts = {
        str(item["concept_id"]): item for item in registry.concepts["concepts"]
    }
    component_ids = frozenset(str(item) for item in demanded_component_ids)
    components = tuple(
        item for item in snapshot.components if item.component_id in component_ids
    )
    concept_ids = _active_component_concepts(registry, snapshot)
    resolutions: list[RuntimeModelResolution] = []
    failures: list[RuntimeModelFailure] = []
    low_confidence_count = 0
    attempted_count = 0

    for component in sorted(components, key=lambda item: item.component_id):
        fingerprint = component_fingerprint(component)
        for concept_id in concept_ids:
            baseline = deterministic.resolve_concept(concept_id, component)
            if baseline.status is not EvidenceStatus.UNKNOWN:
                continue
            attempted_count += 1
            concept = concepts[concept_id]
            try:
                result = adapter.resolve_residual(
                    concept_id=concept_id,
                    component_fingerprint=fingerprint,
                    document_hash=registry.concepts_hash,
                    entity_label=component.name,
                    evidence_text=_component_evidence_text(component, concept),
                )
            except ModelAdapterError as error:
                if mode is ModelMode.REQUIRED:
                    raise
                failures.append(
                    RuntimeModelFailure(
                        concept_id,
                        component.component_id,
                        type(error).__name__,
                    )
                )
                continue
            if result.classification.confidence < MODEL_CLASSIFICATION_MIN_CONFIDENCE:
                if mode is ModelMode.REQUIRED:
                    raise ModelResponseError(
                        "residual classification confidence is below the reviewed "
                        f"floor {MODEL_CLASSIFICATION_MIN_CONFIDENCE}",
                        result.trace,
                    )
                low_confidence_count += 1
                continue
            resolutions.append(
                RuntimeModelResolution(
                    concept_id,
                    component.component_id,
                    fingerprint,
                    registry.concepts_hash,
                    result,
                )
            )

    if resolutions and not failures and low_confidence_count == 0:
        status = "used_residual_classification"
    elif resolutions:
        status = "partially_used_residual_classification"
    elif attempted_count == 0:
        status = "available_not_needed"
    elif failures:
        status = "unavailable_deterministic_fallback"
    else:
        status = "model_output_insufficient_deterministic_fallback"
    return ModelRuntimeState(
        status,
        model_identifier,
        attempted_count,
        low_confidence_count,
        tuple(resolutions),
        tuple(failures),
    )


def model_evidence_for_component(
    state: ModelRuntimeState,
    component_id: str,
    contract: EvidenceContract,
) -> tuple[EvidenceResult, ...]:
    """Convert accepted classifications into explicit contract evidence."""

    disposition = (
        PlanDisposition.EXECUTE_WITH_ASSUMPTION
        if contract is EvidenceContract.BENCHMARK
        else PlanDisposition.DECISION_REQUIRED
    )
    result: list[EvidenceResult] = []
    for resolution in state.resolutions:
        if resolution.entity_id != component_id:
            continue
        classification = resolution.result.classification
        trace = resolution.result.trace
        output_hash = trace.output_hash
        if output_hash is None:
            raise ValueError("accepted runtime model evidence requires an output hash")
        result.append(
            EvidenceResult(
                rule_id=f"{MODEL_RULE_PREFIX}{resolution.concept_id}",
                status=EvidenceStatus.UNKNOWN,
                basis=EvidenceBasis.ENTITY_ATTRIBUTE,
                scope=EvidenceScope.RULE,
                severity=RuleSeverity.HARD,
                summary=(
                    "Optional model evidence classified concept membership as "
                    f"{'member' if classification.member else 'non-member'} with "
                    f"confidence {classification.confidence}; the classification "
                    "remains an explicit evidence-contract assumption."
                ),
                source_references=(
                    f"concept:{resolution.concept_id}",
                    f"entity_fingerprint:sha256:{resolution.entity_fingerprint}",
                    f"model_input:{trace.input_hash}",
                    f"model_output:{output_hash}",
                ),
                assumption_codes=(MODEL_ASSUMPTION_CODE,),
                contract_disposition=disposition,
            )
        )
    return tuple(sorted(result, key=lambda item: item.rule_id))


def safe_model_failure_message(error: Exception) -> str:
    """Return bounded provider failure text suitable for the CLI boundary."""

    return sanitize_control_characters(str(error))[:2_000]


__all__ = [
    "MODEL_ASSUMPTION_CODE",
    "MODEL_CLASSIFICATION_MIN_CONFIDENCE",
    "MODEL_RULE_PREFIX",
    "ModelRuntimeState",
    "RuntimeModelFailure",
    "RuntimeModelResolution",
    "build_model_runtime",
    "disabled_model_runtime",
    "model_evidence_for_component",
    "safe_model_failure_message",
]
