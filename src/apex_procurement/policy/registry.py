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

from .parameters import (
    AirFreightLeadTimeParameters,
    AirFreightPeriodCapParameters,
    ApplicablePolicyParameters,
    ApprovalThresholdParameters,
    DomesticPremiumParameter,
    DomesticPremiumParameters,
    EconomicAutonomyParameters,
    EmergencyApprovalParameters,
    RuleParameter,
    SecondaryAllocationParameters,
    SemanticScope,
    StandardLeadTimeParameters,
    StrategicContinuityParameters,
    SupplierVolumeCapParameters,
    SustainabilityParameters,
)
from .schema import PolicyValidationError, validate_policy_documents


POLICY_DIRECTORY = Path(__file__).resolve().parent
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_VALIDATION_ROOT = (
    PROJECT_ROOT
    if (PROJECT_ROOT / "pyproject.toml").is_file()
    and (PROJECT_ROOT / "data" / "policies").is_dir()
    and (PROJECT_ROOT / "data" / "memos").is_dir()
    else None
)


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
    economic_autonomy: EconomicAutonomyParameters

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

    def parameters_for(self, scenario_date: date) -> ApplicablePolicyParameters:
        """Return the strictly typed behavioral values active on ``scenario_date``."""

        if not isinstance(scenario_date, date) or isinstance(scenario_date, datetime):
            raise TypeError("scenario_date must be datetime.date")
        active = self.active_rules(scenario_date)
        if not active:
            raise PolicyValidationError(
                "No reviewed procurement policy is effective on scenario date "
                f"{scenario_date.isoformat()}; procurement is withheld."
            )

        def kind(rule: PolicyRule) -> str | None:
            for key in ("constraint", "directive"):
                payload = rule.data.get(key)
                if isinstance(payload, Mapping) and isinstance(payload.get("kind"), str):
                    return str(payload["kind"])
            return None

        def body(rule: PolicyRule) -> Mapping[str, Any]:
            payload = rule.data.get("constraint", rule.data.get("directive"))
            if not isinstance(payload, Mapping):
                raise PolicyValidationError(f"rule {rule.rule_id!r} has no typed payload")
            return payload

        def scope(rule: PolicyRule) -> SemanticScope:
            selector = rule.data.get("selector")
            if not isinstance(selector, Mapping):
                raise PolicyValidationError(f"rule {rule.rule_id!r} has no selector")
            return SemanticScope(
                entity=str(selector["entity"]),
                semantic_tags=tuple(str(item) for item in selector.get("semantic_tags", ())),
                operator=(str(selector["operator"]) if "operator" in selector else None),
                match=(str(selector["match"]) if "match" in selector else None),
                route_conditions=tuple(
                    str(item) for item in selector.get("route_conditions", ())
                ),
            )

        def decimal_value(value: Any, location: str) -> Decimal:
            if not isinstance(value, str):
                raise PolicyValidationError(f"{location} must be a decimal string")
            try:
                parsed = Decimal(value)
            except Exception as error:
                raise PolicyValidationError(f"{location} is not a decimal") from error
            if not parsed.is_finite():
                raise PolicyValidationError(f"{location} must be finite")
            return parsed

        def one(rule_kind: str) -> PolicyRule:
            matches = tuple(rule for rule in active if kind(rule) == rule_kind)
            if len(matches) != 1:
                raise PolicyValidationError(
                    f"expected one active {rule_kind!r} rule, found {len(matches)}"
                )
            return matches[0]

        standard_lead_rule = one("quoted_lead_time_delivery_date")
        standard_lead = StandardLeadTimeParameters(
            standard_lead_rule.rule_id,
            "quoted_lead_time_delivery_date",
            scope(standard_lead_rule),
            str(body(standard_lead_rule).get("calculation", "")),
        )

        air_lead_rules = tuple(
            rule for rule in active if kind(rule) == "air_freight_authorization"
        )
        if len(air_lead_rules) > 1:
            raise PolicyValidationError(
                "expected at most one active 'air_freight_authorization' rule, "
                f"found {len(air_lead_rules)}"
            )
        air_lead: AirFreightLeadTimeParameters | None = None
        if air_lead_rules:
            air_rule = air_lead_rules[0]
            air_body = body(air_rule)
            reduction = air_body.get("lead_time_reduction_days")
            minimum = air_body.get("minimum_lead_time_days")
            for field_name, value in (
                ("lead_time_reduction_days", reduction),
                ("minimum_lead_time_days", minimum),
            ):
                if not isinstance(value, int) or isinstance(value, bool):
                    raise PolicyValidationError(
                        f"{air_rule.rule_id}.constraint.{field_name} must be int"
                    )
            air_lead = AirFreightLeadTimeParameters(
                air_rule.rule_id,
                "air_freight_authorization",
                scope(air_rule),
                str(air_body.get("shipping_mode", "")),
                str(air_body.get("standard_lead_time_condition", "")),
                reduction,
                minimum,
            )

        domestic_rules = tuple(
            rule for rule in active if kind(rule) == "domestic_supplier_preference"
        )
        domestic: dict[str, DomesticPremiumParameter] = {}
        for rule in domestic_rules:
            parameter = DomesticPremiumParameter(
                rule.rule_id,
                "domestic_supplier_preference",
                scope(rule),
                decimal_value(
                    body(rule).get("maximum_premium_fraction"),
                    f"{rule.rule_id}.constraint.maximum_premium_fraction",
                ),
            )
            operator = parameter.scope.operator
            if operator not in {"any", "none"} or operator in domestic:
                raise PolicyValidationError(
                    "domestic premium rules require unique critical(any) and ordinary(none) scopes"
                )
            domestic[operator] = parameter
        if set(domestic) != {"any", "none"}:
            raise PolicyValidationError(
                "domestic premium rules require critical(any) and ordinary(none) scopes"
            )

        strategic_rule = one("strategic_supplier_continuity")
        strategic = StrategicContinuityParameters(
            strategic_rule.rule_id,
            "strategic_supplier_continuity",
            scope(strategic_rule),
            decimal_value(
                body(strategic_rule).get("maximum_alternative_savings_fraction"),
                f"{strategic_rule.rule_id}.constraint.maximum_alternative_savings_fraction",
            ),
        )
        sustainability_rule = one("sustainability_preference")
        sustainability_body = body(sustainability_rule)
        delivery_days = sustainability_body.get("comparable_delivery_days")
        if not isinstance(delivery_days, int) or isinstance(delivery_days, bool):
            raise PolicyValidationError(
                f"{sustainability_rule.rule_id}.constraint.comparable_delivery_days must be int"
            )
        sustainability = SustainabilityParameters(
            sustainability_rule.rule_id,
            "sustainability_preference",
            scope(sustainability_rule),
            decimal_value(
                sustainability_body.get("comparable_price_fraction"),
                f"{sustainability_rule.rule_id}.constraint.comparable_price_fraction",
            ),
            delivery_days,
        )

        approvals = tuple(
            ApprovalThresholdParameters(
                rule.rule_id,
                "order_value_approval",
                scope(rule),
                decimal_value(
                    body(rule).get("amount_exceeds"),
                    f"{rule.rule_id}.constraint.amount_exceeds",
                ),
                str(body(rule).get("authority", "")),
            )
            for rule in active
            if kind(rule) == "order_value_approval"
        )
        authorities = {item.authority for item in approvals}
        if not {"Procurement Manager", "VP of Operations"} <= authorities:
            raise PolicyValidationError(
                "active order approval parameters must include manager and VP authorities"
            )

        emergency_rule = one("emergency_approval_bypass")
        emergency_body = body(emergency_rule)
        retroactive_days = emergency_body.get("retroactive_approval_business_days")
        if not isinstance(retroactive_days, int) or isinstance(retroactive_days, bool):
            raise PolicyValidationError(
                f"{emergency_rule.rule_id}.constraint.retroactive_approval_business_days must be int"
            )
        emergency = EmergencyApprovalParameters(
            emergency_rule.rule_id,
            "emergency_approval_bypass",
            scope(emergency_rule),
            decimal_value(
                emergency_body.get("amount_up_to"),
                f"{emergency_rule.rule_id}.constraint.amount_up_to",
            ),
            (
                str(emergency_body["authority"])
                if emergency_body.get("authority") is not None
                else None
            ),
            retroactive_days,
            str(emergency_body.get("implementation_status", "")),
        )

        volume_caps: list[SupplierVolumeCapParameters] = []
        for rule in active:
            if kind(rule) != "supplier_volume_cap":
                continue
            rule_body = body(rule)
            window = rule_body.get("window_months")
            if not isinstance(window, int) or isinstance(window, bool):
                raise PolicyValidationError(
                    f"{rule.rule_id}.constraint.window_months must be int"
                )
            precedence = rule.data.get("precedence")
            supersedes = (
                tuple(str(item) for item in precedence.get("supersedes", ()))
                if isinstance(precedence, Mapping)
                else ()
            )
            volume_caps.append(
                SupplierVolumeCapParameters(
                    rule.rule_id,
                    "supplier_volume_cap",
                    scope(rule),
                    decimal_value(
                        rule_body.get("maximum_fraction"),
                        f"{rule.rule_id}.constraint.maximum_fraction",
                    ),
                    window,
                    supersedes,
                )
            )

        secondary = tuple(
            SecondaryAllocationParameters(
                rule.rule_id,
                "minimum_secondary_fraction",
                scope(rule),
                decimal_value(
                    body(rule).get("value"),
                    f"{rule.rule_id}.constraint.value",
                ),
            )
            for rule in active
            if kind(rule) == "minimum_secondary_fraction"
        )
        air_caps = tuple(
            AirFreightPeriodCapParameters(
                rule.rule_id,
                "air_freight_period_spend_cap",
                scope(rule),
                decimal_value(
                    body(rule).get("maximum_amount"),
                    f"{rule.rule_id}.constraint.maximum_amount",
                ),
            )
            for rule in active
            if kind(rule) == "air_freight_period_spend_cap"
        )
        return ApplicablePolicyParameters(
            scenario_date=scenario_date,
            pack_id=self.pack_id,
            content_hash=self.content_hash,
            standard_lead_time=standard_lead,
            air_freight_lead_time=air_lead,
            domestic_premiums=DomesticPremiumParameters(
                ordinary=domestic["none"], critical=domestic["any"]
            ),
            strategic_continuity=strategic,
            sustainability=sustainability,
            approval_thresholds=tuple(
                sorted(approvals, key=lambda item: (item.amount_exceeds, item.rule_id))
            ),
            emergency_approval=emergency,
            supplier_volume_caps=tuple(sorted(volume_caps, key=lambda item: item.rule_id)),
            secondary_allocations=tuple(sorted(secondary, key=lambda item: item.rule_id)),
            air_freight_period_caps=tuple(sorted(air_caps, key=lambda item: item.rule_id)),
            economic_autonomy=self.economic_autonomy,
        )


def _economic_autonomy(pack: Mapping[str, Any]) -> EconomicAutonomyParameters:
    raw = pack.get("economic_autonomy")
    if not isinstance(raw, Mapping):
        raise PolicyValidationError("policy.economic_autonomy must be an object")

    def decimal_value(key: str, *, optional: bool = False) -> Decimal | None:
        value = raw.get(key)
        if optional and value is None:
            return None
        if not isinstance(value, str):
            raise PolicyValidationError(
                f"policy.economic_autonomy.{key} must be a decimal string"
            )
        try:
            result = Decimal(value)
        except Exception as error:
            raise PolicyValidationError(
                f"policy.economic_autonomy.{key} is not a decimal"
            ) from error
        if not result.is_finite():
            raise PolicyValidationError(
                f"policy.economic_autonomy.{key} must be finite"
            )
        return result

    return EconomicAutonomyParameters(
        max_surplus_fraction=decimal_value("max_surplus_fraction"),  # type: ignore[arg-type]
        max_surplus_units=decimal_value("max_surplus_units", optional=True),
        max_excess_cost_usd=decimal_value("max_excess_cost_usd"),  # type: ignore[arg-type]
        forced_surplus_review_usd=decimal_value("forced_surplus_review_usd"),  # type: ignore[arg-type]
        boundary=str(raw.get("boundary", "")),
        provisional=raw.get("provisional"),  # type: ignore[arg-type]
        review_status=str(raw.get("review_status", "")),
        source_pointer=str(raw.get("source_pointer", "")),
    )


def load_policy_registry(
    *,
    pack_path: Path | None = None,
    concepts_path: Path | None = None,
    project_root: Path | None = DEFAULT_VALIDATION_ROOT,
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
        economic_autonomy=_economic_autonomy(pack),
    )


__all__ = [
    "ApplicablePolicyParameters",
    "EconomicAutonomyParameters",
    "PolicyRegistry",
    "PolicyRule",
    "load_policy_registry",
]
