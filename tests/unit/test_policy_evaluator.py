from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from itertools import product
from pathlib import Path
import unittest

from apex_procurement.config import EvidenceContract
from apex_procurement.domain import AlertCategory, EvidenceStatus, PlanDisposition, Supplier
from apex_procurement.policy.entity_resolution import EntityResolver
from apex_procurement.policy.evaluator import (
    CapacityConfirmation,
    EvaluationContext,
    PolicyEvaluator,
)
from apex_procurement.policy.registry import load_policy_registry
from apex_procurement.repository import load_snapshot


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENARIO = PROJECT_ROOT / "data" / "scenarios" / "scenario_01_baseline.sqlite"


class EvaluatorFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = load_policy_registry()
        cls.snapshot = load_snapshot(SCENARIO)

    def component(self, prefix: str):
        return next(item for item in self.snapshot.components if item.name.startswith(prefix))

    def supplier(self, prefix: str):
        return next(item for item in self.snapshot.suppliers if item.name.startswith(prefix))

    def catalog(self, component, supplier):
        return next(
            item
            for item in self.snapshot.catalog_lines
            if item.component_id == component.component_id and item.supplier_id == supplier.supplier_id
        )

    def context(
        self,
        *,
        component=None,
        supplier=None,
        suppliers=None,
        catalog_line=None,
        facts=None,
        confirmations=(),
    ) -> EvaluationContext:
        return EvaluationContext(
            scenario_date=self.snapshot.configuration.current_date,
            suppliers=tuple(suppliers if suppliers is not None else self.snapshot.suppliers),
            component=component,
            supplier=supplier,
            catalog_line=catalog_line,
            purchase_orders=self.snapshot.purchase_orders,
            confirmations=confirmations,
            facts=facts or {},
        )


class ThreeValuedTruthTableTests(EvaluatorFixture):
    def test_kleene_truth_tables_are_exhaustive(self) -> None:
        values = tuple(EvidenceStatus)
        expected_not = {
            EvidenceStatus.PASS: EvidenceStatus.FAIL,
            EvidenceStatus.FAIL: EvidenceStatus.PASS,
            EvidenceStatus.UNKNOWN: EvidenceStatus.UNKNOWN,
        }
        evaluator = PolicyEvaluator(self.registry)
        for value in values:
            self.assertIs(evaluator.truth_not(value), expected_not[value])

        for left, right in product(values, repeat=2):
            with self.subTest(left=left, right=right):
                expected_all = (
                    EvidenceStatus.FAIL
                    if EvidenceStatus.FAIL in (left, right)
                    else EvidenceStatus.PASS
                    if left is right is EvidenceStatus.PASS
                    else EvidenceStatus.UNKNOWN
                )
                expected_any = (
                    EvidenceStatus.PASS
                    if EvidenceStatus.PASS in (left, right)
                    else EvidenceStatus.FAIL
                    if left is right is EvidenceStatus.FAIL
                    else EvidenceStatus.UNKNOWN
                )
                self.assertIs(evaluator.truth_all((left, right)), expected_all)
                self.assertIs(evaluator.truth_any((left, right)), expected_any)

    def test_every_compiled_rule_kind_has_a_deterministic_truth_evaluator(self) -> None:
        evaluator = PolicyEvaluator(self.registry)
        compiled_kinds = {
            str((rule.data.get("constraint") or rule.data.get("directive"))["kind"])
            for rule in self.registry.rules
        }
        implemented_kinds = {
            name.removeprefix("_predicate_")
            for name in dir(evaluator)
            if name.startswith("_predicate_")
        }
        self.assertEqual(compiled_kinds, implemented_kinds)

    def test_every_rule_kind_evaluates_under_both_evidence_contracts(self) -> None:
        magnet = self.component("Neodymium")
        primary = self.supplier("Nanjing")
        secondary = self.supplier("MagnetPro")
        facts = {
            "allocations": {primary.supplier_id: Decimal("80"), secondary.supplier_id: Decimal("20")},
            "proposed_supplier_volumes": {primary.supplier_id: Decimal("50"), secondary.supplier_id: Decimal("50")},
            "rolling_supplier_volumes": {},
            "quantity": Decimal("1000"),
            "qualified_supplier_count": 2,
            "eligible_supplier_count": 2,
            "global_supplier_count": 2,
            "international_justification_provided": True,
            "all_tco_costs_represented": True,
            "order_total": Decimal("1"),
            "emergency_bypass_requested": False,
            "sustainability_preference_honored": True,
            "certification_preference_honored": True,
            "strategic_volume_maintained": True,
            "significant_strategic_shift": False,
            "material_available_date": date(2025, 9, 2),
            "required_date": date(2025, 9, 3),
            "order_date": date(2025, 9, 1),
            "expected_delivery_date": date(2025, 10, 6),
            "confirmed_production": True,
            "standard_lead_time_causes_production_delay": True,
            "air_freight_used": True,
            "air_freight_cost_documented": True,
            "air_freight_period_spend": Decimal("1"),
            "air_freight_request_approved": True,
            "approved_authorities": ("Procurement Manager", "VP of Operations"),
            "accepted_shipment_supplier_ids": (primary.supplier_id,),
        }
        context = self.context(
            component=magnet,
            supplier=primary,
            catalog_line=self.catalog(magnet, primary),
            facts=facts,
        )
        expected_active_kinds = {
            str((rule.data.get("constraint") or rule.data.get("directive"))["kind"])
            for rule in self.registry.active_rules(context.scenario_date)
        }
        for contract in EvidenceContract:
            with self.subTest(contract=contract):
                batch = PolicyEvaluator(self.registry, contract).evaluate(context)
                self.assertEqual(
                    {item.constraint_kind for item in batch.evaluations if item.active},
                    expected_active_kinds,
                )
                for item in batch.evaluations:
                    if item.evidence is not None:
                        self.assertNotIn("No deterministic evaluator", item.evidence.summary)

    def test_unknown_history_is_not_zero_and_prospective_rule_still_fails(self) -> None:
        component = self.component("Neodymium")
        primary = self.supplier("Nanjing")
        secondary = self.supplier("MagnetPro")
        context = self.context(
            component=component,
            supplier=primary,
            facts={
                "allocations": {primary.supplier_id: Decimal("90"), secondary.supplier_id: Decimal("10")},
                "eligible_supplier_count": 2,
                "proposed_supplier_volumes": {primary.supplier_id: Decimal("90"), secondary.supplier_id: Decimal("10")},
            },
        )
        for contract, disposition in (
            (EvidenceContract.BENCHMARK, PlanDisposition.EXECUTE_WITH_ASSUMPTION),
            (EvidenceContract.PRODUCTION, PlanDisposition.DECISION_REQUIRED),
        ):
            evaluator = PolicyEvaluator(self.registry, contract)
            rolling = evaluator.evaluate_rule("MEMO-2025-041.magnet_rolling_cap", context)
            prospective = evaluator.evaluate_rule("MEMO-2025-041.magnet_secondary_allocation", context)
            self.assertIs(rolling.evidence.status, EvidenceStatus.UNKNOWN)
            self.assertIs(rolling.evidence.contract_disposition, disposition)
            self.assertIn("not replaced with zero", rolling.evidence.summary)
            self.assertIs(prospective.evidence.status, EvidenceStatus.FAIL)
            self.assertIsNone(prospective.evidence.contract_disposition)

    def test_external_approval_contract_is_recommendation_in_both_modes(self) -> None:
        context = self.context(facts={"order_total": Decimal("50000.01")})
        for contract in EvidenceContract:
            result = PolicyEvaluator(self.registry, contract).evaluate_rule(
                "POL-PROC-001.section_7.manager_approval", context
            )
            self.assertIs(result.evidence.status, EvidenceStatus.UNKNOWN)
            self.assertIs(
                result.evidence.contract_disposition,
                PlanDisposition.RECOMMEND_APPROVAL,
            )


class ConceptResolutionTests(EvaluatorFixture):
    def test_canada_is_domestic_even_when_convenience_flag_disagrees(self) -> None:
        canada = next(item for item in self.snapshot.suppliers if item.country == "Canada")
        result = EntityResolver(self.registry).resolve_concept("domestic_supplier", canada)
        self.assertIs(result.status, EvidenceStatus.PASS)
        self.assertIn("DOMESTIC_FLAG_DISAGREEMENT", {alert.code for alert in result.alerts})

    def test_direct_and_inferred_critical_mappings_are_not_conflated(self) -> None:
        resolver = EntityResolver(self.registry)
        direct = resolver.resolve_concept("critical_component", self.component("Temperature Sensor IC"))
        inferred = tuple(
            resolver.resolve_concept("critical_component", self.component(prefix))
            for prefix in ("PCB Assembly", "Pressure Transducer", "Humidity Sensor")
        )
        self.assertIs(direct.status, EvidenceStatus.PASS)
        for result in inferred:
            self.assertIs(result.status, EvidenceStatus.UNKNOWN)
            self.assertIn("INFERRED_CONCEPT_MEMBERSHIP", result.assumption_codes)

    def test_lexical_boundaries_reject_unrelated_magnetic_part(self) -> None:
        template = self.component("Neodymium")
        unrelated = replace(
            template,
            component_id="component-unrelated",
            name="Magnetic reed switch",
            description="Switch",
            category="Electronic Component",
        )
        result = EntityResolver(self.registry).resolve_concept("neodymium_magnet", unrelated)
        self.assertIs(result.status, EvidenceStatus.FAIL)

    def test_unknown_country_never_falls_back_to_convenience_flag(self) -> None:
        template = self.supplier("Sterling")
        supplier = replace(template, supplier_id="supplier-unknown-country", country="Freedonia", is_domestic=True)
        result = EntityResolver(self.registry).resolve_concept("domestic_supplier", supplier)
        self.assertIs(result.status, EvidenceStatus.UNKNOWN)
        self.assertIn("stored is_domestic is supporting evidence only", result.evidence)

    def test_robust_both_ways_blocks_only_when_readings_change_executability(self) -> None:
        component = self.component("Pressure Transducer")
        international = self.supplier("Shenzhen")
        context = self.context(
            component=component,
            supplier=international,
            facts={"domestic_premium_fraction": Decimal("0.455")},
        )
        evaluator = PolicyEvaluator(self.registry, EvidenceContract.BENCHMARK)
        critical = evaluator.evaluate_rule(
            "POL-PROC-001.section_3.critical_premium_threshold", context
        )
        noncritical = evaluator.evaluate_rule(
            "POL-PROC-001.section_3.domestic_preference", context
        )
        self.assertIs(critical.selector_status, EvidenceStatus.UNKNOWN)
        self.assertIs(critical.evidence.status, EvidenceStatus.UNKNOWN)
        self.assertIs(critical.evidence.contract_disposition, PlanDisposition.DECISION_REQUIRED)
        self.assertIs(noncritical.selector_status, EvidenceStatus.UNKNOWN)
        self.assertIs(noncritical.evidence.contract_disposition, PlanDisposition.EXECUTE_WITH_ASSUMPTION)


class SourceNamedEntityTests(EvaluatorFixture):
    def setUp(self) -> None:
        self.resolver = EntityResolver(self.registry)
        self.rule = self.registry.rule("MEMO-2025-041.magnet_named_primary")
        self.primary_ref = self.rule.data["directive"]["supplier"]

    def test_four_case_ladder_and_reused_id(self) -> None:
        original = self.supplier("Nanjing")
        other = self.supplier("Sterling")

        exact = self.resolver.resolve_named_supplier(self.primary_ref, self.snapshot.suppliers)
        self.assertIs(exact.status, EvidenceStatus.PASS)
        self.assertEqual(exact.supplier, original)

        stale = replace(original, supplier_id="supplier-renumbered")
        stale_suppliers = tuple(stale if item is original else item for item in self.snapshot.suppliers)
        by_name = self.resolver.resolve_named_supplier(self.primary_ref, stale_suppliers)
        self.assertIs(by_name.status, EvidenceStatus.PASS)
        self.assertEqual(by_name.supplier, stale)
        self.assertIn("STALE_SOURCE_ID", {alert.code for alert in by_name.alerts})

        disagreement = tuple(
            replace(item, name=original.name)
            if item is other
            else replace(item, name="Different Company")
            if item is original
            else item
            for item in self.snapshot.suppliers
        )
        self.assertIs(
            self.resolver.resolve_named_supplier(self.primary_ref, disagreement).status,
            EvidenceStatus.UNKNOWN,
        )

        reused = tuple(
            replace(item, name="Different Company") if item is original else item
            for item in self.snapshot.suppliers
        )
        reused_result = self.resolver.resolve_named_supplier(self.primary_ref, reused)
        self.assertIs(reused_result.status, EvidenceStatus.UNKNOWN)
        self.assertIn("reused", reused_result.alerts[0].message)

        missing = tuple(item for item in self.snapshot.suppliers if item is not original)
        self.assertIs(
            self.resolver.resolve_named_supplier(self.primary_ref, missing).status,
            EvidenceStatus.UNKNOWN,
        )

        duplicate = replace(original, supplier_id="supplier-duplicate-name")
        self.assertIs(
            self.resolver.resolve_named_supplier(
                self.primary_ref, self.snapshot.suppliers + (duplicate,)
            ).status,
            EvidenceStatus.UNKNOWN,
        )

    def test_consistent_joint_renaming_preserves_semantic_resolution(self) -> None:
        original = self.supplier("Nanjing")
        renamed = replace(original, supplier_id="supplier-jointly-renamed")
        reference = {"source_id": renamed.supplier_id, "legal_name": renamed.name}
        result = self.resolver.resolve_named_supplier(reference, (renamed,))
        self.assertIs(result.status, EvidenceStatus.PASS)
        self.assertEqual(result.resolved_supplier_id, renamed.supplier_id)
        self.assertFalse(result.alerts)

    def test_unresolved_shaping_reference_drops_only_that_directive(self) -> None:
        magnet = self.component("Neodymium")
        suppliers = tuple(
            replace(item, name=f"Replacement {index}")
            if item.name.startswith(("Nanjing", "MagnetPro"))
            else item
            for index, item in enumerate(self.snapshot.suppliers)
        )
        context = self.context(
            component=magnet,
            supplier=suppliers[0],
            suppliers=suppliers,
            facts={"allocations": {}, "eligible_supplier_count": 2},
        )
        batch = PolicyEvaluator(self.registry).evaluate(context)
        by_id = {item.rule_id: item for item in batch.evaluations}
        named = by_id["MEMO-2025-041.magnet_named_primary"]
        rolling = by_id["MEMO-2025-041.magnet_rolling_cap"]
        secondary = by_id["MEMO-2025-041.magnet_secondary_allocation"]
        self.assertFalse(named.applicable)
        self.assertFalse(named.blocks_scope)
        self.assertIn(AlertCategory.POLICY_CONFLICT, {alert.category for alert in named.alerts})
        self.assertIsNotNone(rolling.evidence)
        self.assertIsNotNone(secondary.evidence)

        hard = replace(self.rule, severity="hard")
        hard_result = PolicyEvaluator(self.registry).evaluate_rule(hard, context)
        self.assertTrue(hard_result.blocks_scope)
        self.assertIs(hard_result.evidence.contract_disposition, PlanDisposition.DECISION_REQUIRED)

    def test_affirmative_runtime_confirmation_releases_without_pack_change(self) -> None:
        magnet = self.component("Neodymium")
        primary = self.supplier("Nanjing")
        subject = self.supplier("MagnetPro")
        facts = {
            "allocations": {primary.supplier_id: Decimal("80"), subject.supplier_id: Decimal("20")}
        }
        before_hash = self.registry.content_hash
        evaluator = PolicyEvaluator(self.registry)
        active = evaluator.evaluate_rule(
            self.rule,
            self.context(component=magnet, supplier=primary, facts=facts),
        )
        negative = evaluator.evaluate_rule(
            self.rule,
            self.context(
                component=magnet,
                supplier=primary,
                facts=facts,
                confirmations=(
                    CapacityConfirmation(
                        subject.supplier_id,
                        self.rule.data["release_condition"]["predicate"],
                        False,
                        self.rule.data["release_condition"]["evidence_source"],
                    ),
                ),
            ),
        )
        released = evaluator.evaluate_rule(
            self.rule,
            self.context(
                component=magnet,
                supplier=primary,
                facts=facts,
                confirmations=(
                    CapacityConfirmation(
                        subject.supplier_id,
                        self.rule.data["release_condition"]["predicate"],
                        True,
                        self.rule.data["release_condition"]["evidence_source"],
                    ),
                ),
            ),
        )
        self.assertFalse(active.released)
        self.assertFalse(negative.released)
        self.assertTrue(released.released)
        self.assertFalse(released.applicable)
        self.assertEqual(self.registry.content_hash, before_hash)

    def test_precedence_is_effective_date_and_scope_aware(self) -> None:
        magnet = self.component("Neodymium")
        primary = self.supplier("Nanjing")
        subject = self.supplier("MagnetPro")
        context = self.context(
            component=magnet,
            supplier=primary,
            facts={
                "allocations": {primary.supplier_id: Decimal("80"), subject.supplier_id: Decimal("20")},
                "rolling_supplier_volumes": {},
                "proposed_supplier_volumes": {primary.supplier_id: Decimal("50"), subject.supplier_id: Decimal("50")},
            },
        )
        batch = PolicyEvaluator(self.registry).evaluate(context)
        by_id = {item.rule_id: item for item in batch.evaluations}
        self.assertEqual(
            by_id["POL-PROC-001.section_4.critical_cap"].superseded_by,
            "MEMO-2025-041.magnet_rolling_cap",
        )
        self.assertFalse(by_id["POL-PROC-001.section_3.domestic_preference"].applicable)
        self.assertTrue(by_id["MEMO-2025-041.magnet_named_primary"].applicable)

        expired_context = replace(context, scenario_date=date(2025, 10, 1))
        expired = PolicyEvaluator(self.registry).evaluate(expired_context)
        air = [item for item in expired.evaluations if item.rule_id.startswith("MEMO-2025-072")]
        self.assertTrue(air)
        self.assertTrue(all(not item.active for item in air))


if __name__ == "__main__":
    unittest.main()
