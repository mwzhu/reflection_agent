from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from apex_procurement.candidates import build_candidate_routes
from apex_procurement.config import EvidenceContract
from apex_procurement.domain import (
    AlertCategory,
    Component,
    DeadlineSupplyPosition,
    DemandBucket,
    DemandContribution,
    EvidenceStatus,
    ScenarioConfiguration,
    ScenarioSnapshot,
    Supplier,
    SupplierCatalogLine,
    SupplyLedger,
)
from apex_procurement.ledgers import LedgerBuildResult
from apex_procurement.policy import load_policy_registry
from apex_procurement.policy.entity_resolution import EntityResolver
from apex_procurement.validator import IndependentPlanValidator


CURRENT = date(2025, 9, 1)
DUE = date(2025, 9, 20)


def _supplier(supplier_id: str, name: str) -> Supplier:
    return Supplier(
        supplier_id=supplier_id,
        name=name,
        country="USA",
        is_domestic=True,
        certifications=(),
        sustainability_rating="B",
        relationship_tier="Standard",
        on_approved_list=True,
    )


def _case(
    changed: Supplier,
    *,
    changed_price: str = "10",
    other: Supplier | None = None,
    other_price: str = "10",
) -> tuple[ScenarioSnapshot, LedgerBuildResult]:
    component = Component(
        component_id="component-local",
        name="General Bracket",
        description="General purpose bracket",
        category="Raw Material",
        unit_of_measure="each",
        is_hazardous=False,
    )
    other = other or _supplier("supplier-known", "Known Domestic Supply")
    suppliers = (changed, other)
    catalogs = (
        SupplierCatalogLine(
            changed.supplier_id,
            component.component_id,
            Decimal(changed_price),
            5,
            Decimal("1"),
        ),
        SupplierCatalogLine(
            other.supplier_id,
            component.component_id,
            Decimal(other_price),
            5,
            Decimal("1"),
        ),
    )
    snapshot = ScenarioSnapshot(
        configuration=ScenarioConfiguration(CURRENT),
        products=(),
        components=(component,),
        suppliers=suppliers,
        bom_lines=(),
        catalog_lines=catalogs,
        production_orders=(),
        inventory=(),
        purchase_orders=(),
        alerts=(),
        state_digest="r10-fact-matrix",
    )
    bucket = DemandBucket(
        component.component_id,
        DUE,
        Decimal("10"),
        Decimal("10"),
        (DemandContribution("order-local", "product-local", Decimal("10")),),
    )
    ledger = SupplyLedger(
        component.component_id,
        Decimal("10"),
        Decimal("0"),
        (),
        Decimal("0"),
        Decimal("10"),
        (
            DeadlineSupplyPosition(
                DUE,
                Decimal("10"),
                Decimal("0"),
                Decimal("10"),
                Decimal("0"),
            ),
        ),
    )
    return snapshot, LedgerBuildResult((bucket,), (ledger,), ())


@pytest.mark.parametrize(
    ("fact", "changed", "expected", "alert_code"),
    (
        (
            "unknown_country",
            replace(
                _supplier("supplier-changed", "Unknown Country Supply"),
                country="Freedonia",
                is_domestic=True,
            ),
            EvidenceStatus.UNKNOWN,
            "SUPPLIER_COUNTRY_UNKNOWN",
        ),
        (
            "unknown_sustainability_rating",
            replace(
                _supplier("supplier-changed", "Unknown Rating Supply"),
                sustainability_rating=None,
            ),
            EvidenceStatus.UNKNOWN,
            "SUSTAINABILITY_RATING_UNKNOWN",
        ),
        (
            "unknown_relationship_tier",
            replace(
                _supplier("supplier-changed", "Unknown Tier Supply"),
                relationship_tier=None,
            ),
            EvidenceStatus.UNKNOWN,
            "RELATIONSHIP_TIER_UNKNOWN",
        ),
        (
            "null_approved_list",
            replace(
                _supplier("supplier-changed", "Unknown ASL Supply"),
                on_approved_list=None,
            ),
            EvidenceStatus.FAIL,
            "APPROVED_LIST_STATE_UNKNOWN",
        ),
    ),
)
def test_supplier_unknown_fact_matrix_is_attribute_specific(
    fact: str,
    changed: Supplier,
    expected: EvidenceStatus,
    alert_code: str,
) -> None:
    snapshot, ledgers = _case(changed)

    result = build_candidate_routes(
        snapshot,
        ledgers,
        contract=EvidenceContract.BENCHMARK,
    )

    changed_routes = tuple(
        route
        for route in result.routes
        if route.supplier_id == changed.supplier_id
    )
    assert changed_routes, fact
    assert {route.eligibility for route in changed_routes} == {expected}
    assert alert_code in {alert.code for alert in result.alerts}
    assert AlertCategory.DATA_QUALITY in {
        alert.category for alert in result.alerts if alert.code == alert_code
    }


def test_unknown_country_executes_only_when_both_readings_admit_the_route() -> None:
    unknown = replace(
        _supplier("supplier-changed", "Unknown Country Supply"),
        country="Freedonia",
        is_domestic=False,
    )
    snapshot, ledgers = _case(
        unknown,
        changed_price="5",
        other_price="10",
    )

    result = build_candidate_routes(
        snapshot,
        ledgers,
        contract=EvidenceContract.BENCHMARK,
    )
    route = next(
        item for item in result.routes if item.supplier_id == unknown.supplier_id
    )

    assert route.eligibility is EvidenceStatus.PASS
    assert any(code.endswith("condition_b") for code in route.exception_codes)
    assert {
        code
        for evidence in route.evidence
        for code in evidence.assumption_codes
    } >= {"ROBUST_BOTH_WAYS", "SUPPLIER_ATTRIBUTE_UNKNOWN"}

    production = build_candidate_routes(
        snapshot,
        ledgers,
        contract=EvidenceContract.PRODUCTION,
    )
    production_route = next(
        item
        for item in production.routes
        if item.supplier_id == unknown.supplier_id
    )
    assert production_route.eligibility is EvidenceStatus.UNKNOWN


@pytest.mark.parametrize(
    ("name", "description", "expected"),
    (
        (
            "High-Coercivity Sintered Puck, Grade 52H",
            "Rare earth permanent magnets",
            EvidenceStatus.UNKNOWN,
        ),
        (
            "Magnetic reed switch",
            "Switch",
            EvidenceStatus.FAIL,
        ),
    ),
)
def test_load_bearing_membership_requires_positive_or_negative_evidence(
    name: str,
    description: str,
    expected: EvidenceStatus,
) -> None:
    component = Component(
        "component-unseen",
        name,
        description,
        "Raw Material",
        "each",
        False,
    )
    registry = load_policy_registry()

    planner = EntityResolver(registry).resolve_concept(
        "neodymium_magnet", component
    ).status
    validator = IndependentPlanValidator(registry)._concept(
        "neodymium_magnet", component
    )

    assert planner is expected
    assert validator is expected


def test_unknown_membership_keeps_prospective_hard_rule_in_robust_set() -> None:
    registry = load_policy_registry()
    parameters = registry.parameters_for(CURRENT)

    proven = parameters.robust_secondary_allocations(
        {"neodymium_magnet": True}
    )
    unresolved = parameters.robust_secondary_allocations(
        {"neodymium_magnet": None}
    )
    disproven = parameters.robust_secondary_allocations(
        {"neodymium_magnet": False}
    )

    assert {item.rule_id for item in unresolved} == {
        item.rule_id for item in proven
    }
    assert disproven == ()


def test_unknown_tier_does_not_block_when_comparator_cannot_change() -> None:
    unknown = replace(
        _supplier("supplier-changed", "Unknown Tier Supply"),
        relationship_tier=None,
    )
    strategic = replace(
        _supplier("supplier-known", "Known Strategic Supply"),
        relationship_tier="Strategic",
    )
    snapshot, ledgers = _case(
        unknown,
        changed_price="5",
        other=strategic,
        other_price="10",
    )

    result = build_candidate_routes(snapshot, ledgers)

    assert {
        route.eligibility
        for route in result.routes
        if route.supplier_id == unknown.supplier_id
    } == {EvidenceStatus.PASS}
    assert "RELATIONSHIP_TIER_COMPARATOR_UNRESOLVED" not in {
        alert.code for alert in result.alerts
    }
