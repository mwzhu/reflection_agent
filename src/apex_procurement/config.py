"""Runtime configuration contracts for the procurement agent."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class EvidenceContract(str, Enum):
    """Named policy for resolving evidence absent from a scenario snapshot."""

    BENCHMARK = "benchmark"
    PRODUCTION = "production"


class ModelMode(str, Enum):
    """Whether optional, non-load-bearing model calls may be attempted."""

    OFF = "off"
    AUTO = "auto"
    REQUIRED = "required"


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Fully parsed command-line configuration.

    Policy rules and economic thresholds intentionally do not live here.  A
    command-line option must never become a policy-waiver mechanism.
    """

    scenario_path: Path
    contract: EvidenceContract = EvidenceContract.BENCHMARK
    model_mode: ModelMode = ModelMode.OFF
    recompile_policy: bool = False
    dry_run: bool = False
    explain_component_id: str | None = None
    strict: bool = False
    alert_prefixes: bool = False
    json_output: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.scenario_path, Path):
            raise TypeError("scenario_path must be pathlib.Path")
        if not isinstance(self.contract, EvidenceContract):
            raise TypeError("contract must be EvidenceContract")
        if not isinstance(self.model_mode, ModelMode):
            raise TypeError("model_mode must be ModelMode")
        if self.explain_component_id is not None and not self.explain_component_id.strip():
            raise ValueError("explain_component_id must be non-empty when provided")


__all__ = ["EvidenceContract", "ModelMode", "RuntimeConfig"]

