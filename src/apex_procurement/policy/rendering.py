"""Reviewed terminal-rendering contract for compiled policy rules.

The compiled pack may declare how missing evidence changes a plan disposition,
but a disposition is safe to activate only when the runtime has a deterministic
terminal renderer for that rule kind.  This module is deliberately data-only:
schema validation, evaluation, and explanation rendering all consume the same
typed matrix without sharing a policy-evaluation result.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping


class RuleKind(str, Enum):
    APPROVED_SUPPLIER_REQUIRED = "approved_supplier_required"
    REQUIRED_CERTIFICATION = "required_certification"
    DOMESTIC_SUPPLIER_PREFERENCE = "domestic_supplier_preference"
    INTERNATIONAL_SOURCING_JUSTIFICATION = "international_sourcing_justification"
    SUPPLIER_VOLUME_CAP = "supplier_volume_cap"
    MINIMUM_QUALIFIED_SUPPLIERS = "minimum_qualified_suppliers"
    SOLE_SOURCE_JUSTIFICATION = "sole_source_justification"
    CATALOG_MINIMUM_ORDER_QUANTITY = "catalog_minimum_order_quantity"
    SUB_MOQ_WRITTEN_APPROVAL = "sub_moq_written_approval"
    HAZARDOUS_RECEIVING_AND_STORAGE = "hazardous_receiving_and_storage"
    HAZARDOUS_PROCUREMENT_REVIEW = "hazardous_procurement_review"
    CRITICAL_COMPONENT_CATEGORIES = "critical_component_categories"
    TOTAL_COST_OF_OWNERSHIP = "total_cost_of_ownership"
    ORDER_VALUE_APPROVAL = "order_value_approval"
    EMERGENCY_APPROVAL_BYPASS = "emergency_approval_bypass"
    SUSTAINABILITY_PREFERENCE = "sustainability_preference"
    BELOW_RATING_REVIEW = "below_rating_review"
    CERTIFICATION_PREFERENCE = "certification_preference"
    STRATEGIC_SUPPLIER_CONTINUITY = "strategic_supplier_continuity"
    STRATEGIC_VOLUME_SHIFT_APPROVAL = "strategic_volume_shift_approval"
    ON_TIME_ARRIVAL = "on_time_arrival"
    QUOTED_LEAD_TIME_DELIVERY_DATE = "quoted_lead_time_delivery_date"
    MINIMUM_SECONDARY_FRACTION = "minimum_secondary_fraction"
    NAMED_PRIMARY_SUPPLIER = "named_primary_supplier"
    AIR_FREIGHT_AUTHORIZATION = "air_freight_authorization"
    AIR_FREIGHT_COST_DOCUMENTATION = "air_freight_cost_documentation"
    AIR_FREIGHT_PERIOD_SPEND_CAP = "air_freight_period_spend_cap"
    AIR_FREIGHT_INDIVIDUAL_APPROVAL = "air_freight_individual_approval"
    SHIPMENT_CERTIFICATE_OF_CONFORMANCE = "shipment_certificate_of_conformance"
    INCUMBENT_SUPPLIER_ONLY = "incumbent_supplier_only"


class ContractDisposition(str, Enum):
    EXECUTE = "EXECUTE"
    EXECUTE_WITH_ASSUMPTION = "EXECUTE_WITH_ASSUMPTION"
    RECOMMEND_APPROVAL = "RECOMMEND_APPROVAL"
    DECISION_REQUIRED = "DECISION_REQUIRED"


class TerminalRenderingPath(str, Enum):
    """Reviewed terminal output path, or an internal rejection boundary."""

    ASSUMPTION_ALERT_AND_EXECUTABLE_DECISION = (
        "assumption_alert_and_executable_decision"
    )
    COMPLETE_APPROVAL_PROPOSAL = "complete_approval_proposal"
    DECISION_REQUIRED_ALERT = "decision_required_alert"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class RuleRenderingContract:
    rule_kind: RuleKind
    disposition_paths: Mapping[ContractDisposition, TerminalRenderingPath]

    def __post_init__(self) -> None:
        paths = dict(self.disposition_paths)
        if set(paths) != set(ContractDisposition):
            raise ValueError(
                f"rendering contract for {self.rule_kind.value!r} must classify "
                "every contract disposition"
            )
        if any(not isinstance(path, TerminalRenderingPath) for path in paths.values()):
            raise TypeError("disposition paths must contain TerminalRenderingPath values")
        object.__setattr__(self, "disposition_paths", MappingProxyType(paths))

    def path_for(self, disposition: ContractDisposition) -> TerminalRenderingPath:
        if not isinstance(disposition, ContractDisposition):
            raise TypeError("disposition must be ContractDisposition")
        return self.disposition_paths[disposition]


# These are the rule kinds for which a missing external-system fact can become
# a complete approval/evidence proposal.  The list is reviewed explicitly;
# changing a different kind to external_system therefore fails pack loading.
_APPROVAL_RENDERED_RULE_KINDS = frozenset(
    {
        RuleKind.SUB_MOQ_WRITTEN_APPROVAL,
        RuleKind.HAZARDOUS_PROCUREMENT_REVIEW,
        RuleKind.ORDER_VALUE_APPROVAL,
        RuleKind.EMERGENCY_APPROVAL_BYPASS,
        RuleKind.BELOW_RATING_REVIEW,
        RuleKind.STRATEGIC_VOLUME_SHIFT_APPROVAL,
        RuleKind.AIR_FREIGHT_PERIOD_SPEND_CAP,
        RuleKind.AIR_FREIGHT_INDIVIDUAL_APPROVAL,
    }
)


def _paths_for(rule_kind: RuleKind) -> Mapping[ContractDisposition, TerminalRenderingPath]:
    return MappingProxyType(
        {
            # UNKNOWN evidence may never authorize unconditional execution.
            ContractDisposition.EXECUTE: TerminalRenderingPath.INTERNAL_ERROR,
            ContractDisposition.EXECUTE_WITH_ASSUMPTION: (
                TerminalRenderingPath.ASSUMPTION_ALERT_AND_EXECUTABLE_DECISION
            ),
            ContractDisposition.RECOMMEND_APPROVAL: (
                TerminalRenderingPath.COMPLETE_APPROVAL_PROPOSAL
                if rule_kind in _APPROVAL_RENDERED_RULE_KINDS
                else TerminalRenderingPath.INTERNAL_ERROR
            ),
            ContractDisposition.DECISION_REQUIRED: (
                TerminalRenderingPath.DECISION_REQUIRED_ALERT
            ),
        }
    )


REVIEWED_RULE_RENDERING_CONTRACTS: Mapping[
    RuleKind, RuleRenderingContract
] = MappingProxyType(
    {
        rule_kind: RuleRenderingContract(rule_kind, _paths_for(rule_kind))
        for rule_kind in RuleKind
    }
)


CONSTRAINT_RULE_KINDS = frozenset(
    rule_kind.value
    for rule_kind in RuleKind
    if rule_kind is not RuleKind.NAMED_PRIMARY_SUPPLIER
)
DIRECTIVE_RULE_KINDS = frozenset({RuleKind.NAMED_PRIMARY_SUPPLIER.value})
SUPPORTED_RULE_KINDS = frozenset(rule_kind.value for rule_kind in RuleKind)


def terminal_rendering_path(
    rule_kind: str | RuleKind,
    disposition: str | ContractDisposition,
) -> TerminalRenderingPath:
    """Return the reviewed path for one kind/disposition pair.

    Unknown enum values are programmer or policy-pack errors, never ordinary
    procurement decisions.
    """

    typed_kind = rule_kind if isinstance(rule_kind, RuleKind) else RuleKind(rule_kind)
    typed_disposition = (
        disposition
        if isinstance(disposition, ContractDisposition)
        else ContractDisposition(disposition)
    )
    return REVIEWED_RULE_RENDERING_CONTRACTS[typed_kind].path_for(
        typed_disposition
    )


__all__ = [
    "CONSTRAINT_RULE_KINDS",
    "ContractDisposition",
    "DIRECTIVE_RULE_KINDS",
    "REVIEWED_RULE_RENDERING_CONTRACTS",
    "RuleKind",
    "RuleRenderingContract",
    "SUPPORTED_RULE_KINDS",
    "TerminalRenderingPath",
    "terminal_rendering_path",
]
