"""Loader and date-aware registry for the compiled policy pack.

This is intentionally not a policy evaluator.  It validates and indexes the
reviewed artifact, and it applies only the first precedence step: effective
window filtering by the scenario's supplied date.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .schema import PolicyValidationError, validate_policy_documents


POLICY_DIRECTORY = Path(__file__).resolve().parent
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PolicyValidationError(f"duplicate JSON property {key!r}")
        result[key] = value
    return result


def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(
                handle,
                parse_float=Decimal,
                parse_constant=lambda token: (_ for _ in ()).throw(
                    PolicyValidationError(f"non-finite JSON number {token!r} in {path}")
                ),
                object_pairs_hook=_reject_duplicate_keys,
            )
    except (OSError, json.JSONDecodeError) as error:
        raise PolicyValidationError(f"cannot load policy artifact {path}: {error}") from error
    if not isinstance(value, Mapping):
        raise PolicyValidationError(f"policy artifact {path} must contain a JSON object")
    return value


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(child) for child in value)
    return value


@dataclass(frozen=True, slots=True)
class PolicyRule:
    rule_id: str
    source_document: str
    effective_from: date
    effective_through: date | None
    severity: str
    evidence_basis: str
    data: Mapping[str, Any]

    def is_active(self, scenario_date: date) -> bool:
        if not isinstance(scenario_date, date) or isinstance(scenario_date, datetime):
            raise TypeError("scenario_date must be datetime.date")
        return self.effective_from <= scenario_date and (
            self.effective_through is None or scenario_date <= self.effective_through
        )


@dataclass(frozen=True, slots=True)
class PolicyRegistry:
    pack_id: str
    content_hash: str
    concepts_hash: str
    rules: tuple[PolicyRule, ...]
    pack: Mapping[str, Any]
    concepts: Mapping[str, Any]

    def active_rules(self, scenario_date: date) -> tuple[PolicyRule, ...]:
        """Return active rules in stable rule-id order using inclusive windows."""

        if not isinstance(scenario_date, date) or isinstance(scenario_date, datetime):
            raise TypeError("scenario_date must be datetime.date")
        return tuple(rule for rule in self.rules if rule.is_active(scenario_date))

    def rule(self, rule_id: str) -> PolicyRule:
        for rule in self.rules:
            if rule.rule_id == rule_id:
                return rule
        raise KeyError(rule_id)


def load_policy_registry(
    *,
    pack_path: Path | None = None,
    concepts_path: Path | None = None,
    project_root: Path | None = PROJECT_ROOT,
) -> PolicyRegistry:
    """Load and fully validate the checked-in policy and concept artifacts."""

    resolved_pack_path = pack_path or POLICY_DIRECTORY / "compiled_policy.json"
    resolved_concepts_path = concepts_path or POLICY_DIRECTORY / "concepts.json"
    pack = _load_json(resolved_pack_path)
    concepts = _load_json(resolved_concepts_path)
    validate_policy_documents(pack, concepts, project_root=project_root)

    rules = tuple(
        PolicyRule(
            rule_id=raw["rule_id"],
            source_document=raw["source_document"],
            effective_from=date.fromisoformat(raw["effective_from"]),
            effective_through=(
                date.fromisoformat(raw["effective_through"])
                if raw["effective_through"] is not None
                else None
            ),
            severity=raw["severity"],
            evidence_basis=raw["evidence_basis"],
            data=_deep_freeze(raw),
        )
        for raw in sorted(pack["rules"], key=lambda item: item["rule_id"])
    )
    return PolicyRegistry(
        pack_id=pack["pack_id"],
        content_hash=pack["content_hash"],
        concepts_hash=concepts["content_hash"],
        rules=rules,
        pack=_deep_freeze(pack),
        concepts=_deep_freeze(concepts),
    )


__all__ = ["PolicyRegistry", "PolicyRule", "load_policy_registry"]
