"""Typed, immutable behavioral parameters extracted from the policy pack.

The compiled JSON remains the authority.  These values contain no defaults;
they can only be constructed from a validated pack (or explicitly by tests).
Planner and validator code share this parameter surface, not calculations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Mapping


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")


def _date(value: date, name: str) -> None:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"{name} must be datetime.date")


def _decimal(
    value: Decimal,
    name: str,
    *,
    positive: bool = False,
    maximum: Decimal | None = None,
) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise TypeError(f"{name} must be a finite Decimal")
    if value < 0 or (positive and value == 0):
        qualifier = "positive" if positive else "nonnegative"
        raise ValueError(f"{name} must be {qualifier}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must not exceed {maximum}")


@dataclass(frozen=True, slots=True)
class SemanticScope:
    """The selector facts needed to choose parameters without value matching."""

    entity: str
    semantic_tags: tuple[str, ...] = ()
    operator: str | None = None
    match: str | None = None
    route_conditions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.entity, "entity")
        if self.operator not in {None, "all", "any", "none"}:
            raise ValueError("operator must be all, any, none, or None")
        if self.match is not None:
            _text(self.match, "match")
        if any(not isinstance(item, str) or not item for item in self.semantic_tags):
            raise ValueError("semantic_tags must contain non-empty text")
        if any(not isinstance(item, str) or not item for item in self.route_conditions):
            raise ValueError("route_conditions must contain non-empty text")

    def matches(self, semantic_status: Mapping[str, bool | None]) -> bool | None:
        """Apply the selector over tri-state semantic membership."""

        if not self.semantic_tags:
            return True
        statuses = tuple(semantic_status.get(tag) for tag in self.semantic_tags)
        operator = self.operator or "all"
        if operator == "any":
            if True in statuses:
                return True
            return None if None in statuses else False
        if operator == "none":
            if True in statuses:
                return False
            return None if None in statuses else True
        if False in statuses:
            return False
        return None if None in statuses else True


@dataclass(frozen=True, slots=True)
class RuleParameter:
    rule_id: str
    kind: str
    scope: SemanticScope

    def __post_init__(self) -> None:
        _text(self.rule_id, "rule_id")
        _text(self.kind, "kind")
        if not isinstance(self.scope, SemanticScope):
            raise TypeError("scope must be SemanticScope")


@dataclass(frozen=True, slots=True)
class DomesticPremiumParameter(RuleParameter):
    maximum_premium_fraction: Decimal

    def __post_init__(self) -> None:
        super(DomesticPremiumParameter, self).__post_init__()
        _decimal(
            self.maximum_premium_fraction,
            "maximum_premium_fraction",
            maximum=Decimal(1),
        )


@dataclass(frozen=True, slots=True)
class DomesticPremiumParameters:
    ordinary: DomesticPremiumParameter
    critical: DomesticPremiumParameter

    def __post_init__(self) -> None:
        if not isinstance(self.ordinary, DomesticPremiumParameter) or not isinstance(
            self.critical, DomesticPremiumParameter
        ):
            raise TypeError("ordinary and critical must be domestic premium parameters")
        ordinary_scope = self.ordinary.scope
        critical_scope = self.critical.scope
        if ordinary_scope.semantic_tags != ("critical_component",) or ordinary_scope.operator != "none":
            raise ValueError("ordinary domestic premium requires the non-critical semantic scope")
        if critical_scope.semantic_tags != ("critical_component",) or critical_scope.operator != "any":
            raise ValueError("critical domestic premium requires the critical semantic scope")

    def for_critical_status(self, critical: bool | None) -> DomesticPremiumParameter:
        # Unknown classification uses the critical scope: that is the robust,
        # stricter semantic reading, selected without inspecting either value.
        return self.ordinary if critical is False else self.critical


@dataclass(frozen=True, slots=True)
class StrategicContinuityParameters(RuleParameter):
    maximum_alternative_savings_fraction: Decimal

    def __post_init__(self) -> None:
        super(StrategicContinuityParameters, self).__post_init__()
        _decimal(
            self.maximum_alternative_savings_fraction,
            "maximum_alternative_savings_fraction",
            maximum=Decimal(1),
        )


@dataclass(frozen=True, slots=True)
class SustainabilityParameters(RuleParameter):
    comparable_price_fraction: Decimal
    comparable_delivery_days: int

    def __post_init__(self) -> None:
        super(SustainabilityParameters, self).__post_init__()
        _decimal(
            self.comparable_price_fraction,
            "comparable_price_fraction",
            maximum=Decimal(1),
        )
        if (
            not isinstance(self.comparable_delivery_days, int)
            or isinstance(self.comparable_delivery_days, bool)
            or self.comparable_delivery_days < 0
        ):
            raise ValueError("comparable_delivery_days must be a nonnegative int")


@dataclass(frozen=True, slots=True)
class ApprovalThresholdParameters(RuleParameter):
    amount_exceeds: Decimal
    authority: str

    def __post_init__(self) -> None:
        super(ApprovalThresholdParameters, self).__post_init__()
        _decimal(self.amount_exceeds, "amount_exceeds")
        _text(self.authority, "authority")


@dataclass(frozen=True, slots=True)
class EmergencyApprovalParameters(RuleParameter):
    amount_up_to: Decimal
    authority: str | None
    retroactive_approval_business_days: int
    implementation_status: str

    def __post_init__(self) -> None:
        super(EmergencyApprovalParameters, self).__post_init__()
        _decimal(self.amount_up_to, "amount_up_to")
        if self.authority is not None:
            _text(self.authority, "authority")
        _text(self.implementation_status, "implementation_status")
        if (
            not isinstance(self.retroactive_approval_business_days, int)
            or isinstance(self.retroactive_approval_business_days, bool)
            or self.retroactive_approval_business_days < 0
        ):
            raise ValueError("retroactive_approval_business_days must be a nonnegative int")


@dataclass(frozen=True, slots=True)
class SupplierVolumeCapParameters(RuleParameter):
    maximum_fraction: Decimal
    window_months: int
    supersedes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super(SupplierVolumeCapParameters, self).__post_init__()
        _decimal(self.maximum_fraction, "maximum_fraction", positive=True, maximum=Decimal(1))
        if (
            not isinstance(self.window_months, int)
            or isinstance(self.window_months, bool)
            or self.window_months <= 0
        ):
            raise ValueError("window_months must be a positive int")


@dataclass(frozen=True, slots=True)
class SecondaryAllocationParameters(RuleParameter):
    minimum_fraction: Decimal

    def __post_init__(self) -> None:
        super(SecondaryAllocationParameters, self).__post_init__()
        _decimal(self.minimum_fraction, "minimum_fraction", positive=True, maximum=Decimal(1))


@dataclass(frozen=True, slots=True)
class AirFreightPeriodCapParameters(RuleParameter):
    maximum_amount: Decimal

    def __post_init__(self) -> None:
        super(AirFreightPeriodCapParameters, self).__post_init__()
        _decimal(self.maximum_amount, "maximum_amount")


@dataclass(frozen=True, slots=True)
class EconomicAutonomyParameters:
    """Reviewed pack-owned bounds; forced surplus review remains advisory."""

    max_surplus_fraction: Decimal
    max_surplus_units: Decimal | None
    max_excess_cost_usd: Decimal
    forced_surplus_review_usd: Decimal
    boundary: str
    provisional: bool
    review_status: str
    source_pointer: str

    def __post_init__(self) -> None:
        _decimal(
            self.max_surplus_fraction,
            "max_surplus_fraction",
            maximum=Decimal(1),
        )
        if self.max_surplus_units is not None:
            _decimal(self.max_surplus_units, "max_surplus_units")
        _decimal(self.max_excess_cost_usd, "max_excess_cost_usd")
        _decimal(self.forced_surplus_review_usd, "forced_surplus_review_usd")
        if self.boundary != "inclusive":
            raise ValueError("economic autonomy boundary must be inclusive")
        if not isinstance(self.provisional, bool):
            raise TypeError("provisional must be bool")
        if self.review_status != "approved":
            raise ValueError("economic autonomy must be reviewed and approved")
        if not self.source_pointer.startswith("MERGED_PLAN#"):
            raise ValueError("economic autonomy source_pointer must point into MERGED_PLAN")

    def disclosure(self) -> str:
        units = "none" if self.max_surplus_units is None else str(self.max_surplus_units)
        return (
            "economic_autonomy(provisional="
            f"{str(self.provisional).lower()}, boundary={self.boundary}, "
            f"max_surplus_fraction={self.max_surplus_fraction}, "
            f"max_surplus_units={units}, max_excess_cost_usd={self.max_excess_cost_usd}, "
            f"forced_surplus_review_usd={self.forced_surplus_review_usd})"
        )


@dataclass(frozen=True, slots=True)
class ApplicablePolicyParameters:
    """All behaviorally consumed policy values active on one scenario date."""

    scenario_date: date
    pack_id: str
    content_hash: str
    domestic_premiums: DomesticPremiumParameters
    strategic_continuity: StrategicContinuityParameters
    sustainability: SustainabilityParameters
    approval_thresholds: tuple[ApprovalThresholdParameters, ...]
    emergency_approval: EmergencyApprovalParameters
    supplier_volume_caps: tuple[SupplierVolumeCapParameters, ...]
    secondary_allocations: tuple[SecondaryAllocationParameters, ...]
    air_freight_period_caps: tuple[AirFreightPeriodCapParameters, ...]
    economic_autonomy: EconomicAutonomyParameters

    def __post_init__(self) -> None:
        _date(self.scenario_date, "scenario_date")
        _text(self.pack_id, "pack_id")
        _text(self.content_hash, "content_hash")
        if not self.approval_thresholds:
            raise ValueError("approval_thresholds must not be empty")
        if len({item.rule_id for item in self.approval_thresholds}) != len(
            self.approval_thresholds
        ):
            raise ValueError("approval_thresholds contains duplicate rule IDs")

    def matching_secondary_allocations(
        self, semantic_status: Mapping[str, bool | None]
    ) -> tuple[SecondaryAllocationParameters, ...]:
        return tuple(
            item
            for item in self.secondary_allocations
            if item.scope.matches(semantic_status) is True
        )

    def robust_secondary_allocations(
        self, semantic_status: Mapping[str, bool | None]
    ) -> tuple[SecondaryAllocationParameters, ...]:
        """Return constraints that apply under either membership reading.

        A prospective hard allocation rule is part of the conservative
        intersection when its selector is proven or unresolved.  Proven
        non-membership is the only state that removes it.
        """

        return tuple(
            item
            for item in self.secondary_allocations
            if item.scope.matches(semantic_status) is not False
        )


__all__ = [
    "AirFreightPeriodCapParameters",
    "ApplicablePolicyParameters",
    "ApprovalThresholdParameters",
    "DomesticPremiumParameter",
    "DomesticPremiumParameters",
    "EconomicAutonomyParameters",
    "EmergencyApprovalParameters",
    "RuleParameter",
    "SecondaryAllocationParameters",
    "SemanticScope",
    "StrategicContinuityParameters",
    "SupplierVolumeCapParameters",
    "SustainabilityParameters",
]
