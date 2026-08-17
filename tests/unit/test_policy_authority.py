from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal
import inspect
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from apex_procurement.candidates import (
    CandidateBuilder,
    DomesticGateCondition,
    evaluate_domestic_gate,
)
from apex_procurement.cli import (
    _component_evaluations,
    _merge_component_rule_evidence,
    _optimizer_problem,
    _planned_decision,
    _solver_results,
)
from apex_procurement.config import EvidenceContract
from apex_procurement.domain import (
    AlertCategory,
    BomLine,
    Component,
    InventoryPosition,
    Product,
    ProductionOrder,
    ScenarioConfiguration,
    ScenarioSnapshot,
    Supplier,
    SupplierCatalogLine,
)
from apex_procurement.explanations import render_alerts
from apex_procurement.ledgers import build_ledgers
from apex_procurement.optimizer import ProcurementOptimizer
from apex_procurement.policy import compute_content_hash, load_policy_registry
from apex_procurement.validator import IndependentPlanValidator, _SourceRequirement
import apex_procurement.validator as validator_module


PROJECT_ROOT = Path(__file__).resolve().parents[2]
POLICY_DIRECTORY = PROJECT_ROOT / "src" / "apex_procurement" / "policy"
PACK_PATH = POLICY_DIRECTORY / "compiled_policy.json"
CONCEPTS_PATH = POLICY_DIRECTORY / "concepts.json"
CURRENT = date(2025, 9, 1)
DUE = date(2025, 9, 20)


def _pack() -> dict[str, object]:
    return json.loads(PACK_PATH.read_text(encoding="utf-8"))


def _rule(pack: dict[str, object], rule_id: str) -> dict[str, object]:
    for rule in pack["rules"]:  # type: ignore[index,union-attr]
        if rule["rule_id"] == rule_id:
            return rule
    raise AssertionError(f"missing rule {rule_id}")


def _derived_override(
    pack: dict[str, object],
    *,
    rule_id: str,
    pointer: str,
    field: str,
    value: str,
) -> None:
    derivation_id = "r05_temporary_policy_override"
    pack["derivations"].append(  # type: ignore[index,union-attr]
        {
            "derivation_id": derivation_id,
            "value": value,
            "source_pointer": "MERGED_PLAN#R05/mutation-test",
            "review_status": "approved",
            "reasoning": "Temporary reviewed-pack mutation used to prove runtime authority.",
        }
    )
    rule = _rule(pack, rule_id)
    rule["constraint"][field] = value
    rule["coverage"][pointer] = {"derived_from": f"derivation:{derivation_id}"}
    pack["content_hash"] = compute_content_hash(pack)


def _registry_from(pack: dict[str, object]):
    directory = TemporaryDirectory()
    path = Path(directory.name) / "compiled_policy.json"
    path.write_text(json.dumps(pack), encoding="utf-8")
    registry = load_policy_registry(
        pack_path=path,
        concepts_path=CONCEPTS_PATH,
        project_root=None,
    )
    return directory, registry


def _approval_snapshot() -> ScenarioSnapshot:
    component = Component(
        "component-policy-authority",
        "Generated General Bracket",
        "General purpose bracket",
        "Raw Material",
        "each",
        False,
    )
    supplier = Supplier(
        "supplier-policy-authority",
        "Generated Domestic Supply",
        "USA",
        True,
        (),
        "A",
        "Standard",
        True,
    )
    product = Product(
        "product-policy-authority",
        "Generated Product",
        None,
        "Generated",
        None,
    )
    return ScenarioSnapshot(
        configuration=ScenarioConfiguration(CURRENT),
        products=(product,),
        components=(component,),
        suppliers=(supplier,),
        bom_lines=(BomLine(product.product_id, component.component_id, Decimal("1")),),
        catalog_lines=(
            SupplierCatalogLine(
                supplier.supplier_id,
                component.component_id,
                Decimal("100"),
                3,
                Decimal("1"),
            ),
        ),
        production_orders=(
            ProductionOrder(
                "order-policy-authority",
                product.product_id,
                Decimal("2"),
                None,
                DUE,
            ),
        ),
        inventory=(InventoryPosition(component.component_id, Decimal("0")),),
        purchase_orders=(),
        alerts=(),
        state_digest="policy-authority-snapshot",
    )


def _plan_with_registry(registry):
    snapshot = _approval_snapshot()
    parameters = registry.parameters_for(CURRENT)
    ledgers = build_ledgers(snapshot)
    evaluations = _component_evaluations(
        snapshot, registry, EvidenceContract.BENCHMARK, ledgers
    )
    builder = CandidateBuilder(
        registry,
        EvidenceContract.BENCHMARK,
        policy_parameters=parameters,
    )
    candidates = _merge_component_rule_evidence(
        builder.build(snapshot, ledgers), evaluations
    )
    component_id = snapshot.components[0].component_id
    problem = _optimizer_problem(
        snapshot,
        registry,
        ledgers,
        candidates,
        evaluations,
        component_id,
        parameters,
    )
    outcome = ProcurementOptimizer().optimize(problem)
    decision = _planned_decision(
        snapshot,
        registry,
        EvidenceContract.BENCHMARK,
        ledgers,
        candidates,
        evaluations[component_id],
        outcome,
        component_id,
    )
    validator = IndependentPlanValidator(
        registry, policy_parameters=parameters
    )
    validation = validator.validate(
        snapshot, (decision,), _solver_results(outcome)
    )
    return builder, problem, outcome, decision, validator, validation


class PolicyAuthorityTests(unittest.TestCase):
    def test_domestic_threshold_mutation_changes_planner_and_validator_readings(self) -> None:
        ordinary_rule = "POL-PROC-001.section_3.domestic_preference"
        original = load_policy_registry()
        mutated_pack = deepcopy(_pack())
        _derived_override(
            mutated_pack,
            rule_id=ordinary_rule,
            pointer="/constraint/maximum_premium_fraction",
            field="maximum_premium_fraction",
            value="0.45",
        )
        directory, mutated = _registry_from(mutated_pack)
        self.addCleanup(directory.cleanup)
        original_parameters = original.parameters_for(CURRENT)
        mutated_parameters = mutated.parameters_for(CURRENT)

        facts = dict(
            domestic_source_exists=True,
            domestic_can_meet_deadline=True,
            best_domestic_price=Decimal("140"),
            best_international_price=Decimal("100"),
            critical_status=False,
        )
        original_planner = evaluate_domestic_gate(
            **facts,
            premium_parameters=original_parameters.domestic_premiums,
        )
        mutated_planner = evaluate_domestic_gate(
            **facts,
            premium_parameters=mutated_parameters.domestic_premiums,
        )
        self.assertIs(original_planner.condition, DomesticGateCondition.PREMIUM)
        self.assertIs(mutated_planner.condition, DomesticGateCondition.SHUT)

        component = _approval_snapshot().components[0]
        domestic = _approval_snapshot().suppliers[0]
        international = Supplier(
            "supplier-international",
            "Generated International Supply",
            "China",
            False,
            (),
            "A",
            "Standard",
            True,
        )
        domestic_catalog = SupplierCatalogLine(
            domestic.supplier_id, component.component_id, Decimal("140"), 3, Decimal("1")
        )
        international_catalog = SupplierCatalogLine(
            international.supplier_id, component.component_id, Decimal("100"), 3, Decimal("1")
        )
        snapshot = ScenarioSnapshot(
            configuration=ScenarioConfiguration(CURRENT),
            products=(),
            components=(component,),
            suppliers=(domestic, international),
            bom_lines=(),
            catalog_lines=(domestic_catalog, international_catalog),
            production_orders=(),
            inventory=(),
            purchase_orders=(),
            alerts=(),
            state_digest="domestic-mutation",
        )
        requirement = _SourceRequirement(
            component,
            ((DUE, Decimal("2")),),
            Decimal("2"),
            Decimal("0"),
            (),
            Decimal("0"),
            Decimal("2"),
        )
        eligible = (
            (domestic, domestic_catalog),
            (international, international_catalog),
        )
        original_validator = IndependentPlanValidator(
            original, policy_parameters=original_parameters
        )
        mutated_validator = IndependentPlanValidator(
            mutated, policy_parameters=mutated_parameters
        )
        self.assertEqual(
            original_validator._domestic_gate(snapshot, requirement, DUE, eligible),
            "b",
        )
        self.assertIsNone(
            mutated_validator._domestic_gate(snapshot, requirement, DUE, eligible)
        )

    def test_approval_mutation_changes_planner_and_validator_without_code_edits(self) -> None:
        original = load_policy_registry()
        original_run = _plan_with_registry(original)
        self.assertIsNotNone(original_run[2].selected_plan)
        self.assertTrue(original_run[5].is_valid, original_run[5].issues)

        mutated_pack = deepcopy(_pack())
        _derived_override(
            mutated_pack,
            rule_id="POL-PROC-001.section_7.manager_approval",
            pointer="/constraint/amount_exceeds",
            field="amount_exceeds",
            value="150",
        )
        directory, mutated = _registry_from(mutated_pack)
        self.addCleanup(directory.cleanup)
        mutated_run = _plan_with_registry(mutated)
        outcome = mutated_run[2]
        self.assertIsNone(outcome.selected_plan)
        self.assertTrue(
            any(
                "POL-PROC-001.section_7.manager_approval"
                in plan.unresolved_approval_ids
                for plan in outcome.alternatives
            )
        )
        self.assertTrue(mutated_run[5].is_valid, mutated_run[5].issues)
        cross_validation = mutated_run[4].validate(
            _approval_snapshot(),
            (original_run[3],),
            _solver_results(original_run[2]),
        )
        self.assertFalse(cross_validation.is_valid)
        self.assertTrue(
            any(
                item.code == "UNAPPROVED_ORDER_VALUE"
                for item in cross_validation.issues
            ),
            cross_validation.issues,
        )

    def test_planner_and_validator_receive_one_typed_object_but_calculate_separately(self) -> None:
        run = _plan_with_registry(load_policy_registry())
        builder, problem, _outcome, decision, validator, validation = run
        self.assertIs(builder.policy_parameters, problem.policy_parameters)
        self.assertIs(problem.policy_parameters, validator.policy_parameters)
        self.assertIs(decision.economic_autonomy, problem.autonomy)
        self.assertTrue(validation.is_valid, validation.issues)
        self.assertIn("economic_autonomy(provisional=true", decision.rationale)
        alerts = render_alerts((decision,), policy_registry=load_policy_registry())
        provisional = tuple(
            item
            for item in alerts
            if item.category is AlertCategory.ASSUMPTION
            and "PROVISIONAL_ECONOMIC_AUTONOMY" in item.description
        )
        self.assertEqual(len(provisional), 1)
        self.assertIn("economic_autonomy(provisional=true", provisional[0].description)
        validator_source = inspect.getsource(validator_module)
        self.assertNotIn("from .optimizer import", validator_source)
        self.assertNotIn("import apex_procurement.optimizer", validator_source)


if __name__ == "__main__":
    unittest.main()
