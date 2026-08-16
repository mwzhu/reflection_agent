"""Dependency-inversion contracts for infrastructure and planning engines."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar, runtime_checkable

from .domain import (
    CommitResult,
    DecisionRecord,
    ScenarioSnapshot,
    SolverResult,
    ValidationResult,
)


@dataclass(frozen=True, slots=True)
class Message:
    """Provider-neutral model message used only by optional model adapters."""

    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant"}:
            raise ValueError("role must be system, user, or assistant")
        if not isinstance(self.content, str) or not self.content:
            raise ValueError("content must be non-empty text")


@runtime_checkable
class Repository(Protocol):
    """Scenario snapshot and atomic decision persistence boundary."""

    def load_snapshot(self, scenario_path: Path, /) -> ScenarioSnapshot:
        """Validate and load one immutable scenario snapshot."""

        ...

    def commit(
        self,
        snapshot: ScenarioSnapshot,
        decisions: Sequence[DecisionRecord],
        validation: ValidationResult,
        /,
        *,
        dry_run: bool = False,
    ) -> CommitResult:
        """Atomically reconcile validated decisions against the loaded digest."""

        ...


ProblemT = TypeVar("ProblemT", contravariant=True)


@runtime_checkable
class Solver(Protocol[ProblemT]):
    """Certified optimization boundary.

    The concrete integer-scaled problem belongs to the optimizer module.  This
    generic protocol keeps the shared contract typed without putting business
    constraints into the scaffold.
    """

    def solve(self, problem: ProblemT, /) -> SolverResult:
        """Return proof state and any explicitly disposed candidate plan."""

        ...


@runtime_checkable
class PlanValidator(Protocol):
    """Independent validation boundary for proposed decision records."""

    def validate(
        self,
        snapshot: ScenarioSnapshot,
        decisions: Sequence[DecisionRecord],
        solver_results: Sequence[SolverResult],
        /,
    ) -> ValidationResult:
        """Recompute invariants independently from source facts."""

        ...


StructuredT = TypeVar("StructuredT")


@runtime_checkable
class ModelClient(Protocol):
    """Optional, non-load-bearing structured-generation boundary."""

    def generate_structured(
        self,
        *,
        messages: Sequence[Message],
        response_schema: type[StructuredT],
        temperature: float = 0.0,
        seed: int | None = 0,
    ) -> StructuredT:
        """Generate a schema-constrained value using an explicitly enabled model."""

        ...


__all__ = ["Message", "ModelClient", "PlanValidator", "Repository", "Solver"]
