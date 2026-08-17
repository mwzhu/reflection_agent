from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path

import pytest

from apex_procurement.domain import AlertCategory, PlanDisposition, ResolutionStatus
from apex_procurement.explanations import (
    ExplanationError,
    approval_rule,
    parse_owned_alert,
    render_alerts,
)
from apex_procurement.policy import (
    ContractDisposition,
    PolicyValidationError,
    REVIEWED_RULE_RENDERING_CONTRACTS,
    RuleKind,
    TerminalRenderingPath,
    compute_content_hash,
    load_policy_registry,
    validate_policy_documents,
)
from apex_procurement.policy.evaluator import (
    EvaluationContext,
    PolicyEvaluationError,
    PolicyEvaluator,
)
from apex_procurement.policy.rendering import (
    CONSTRAINT_RULE_KINDS,
    DIRECTIVE_RULE_KINDS,
)
from apex_procurement.repository import load_snapshot
from tests.unit.test_decisions import make_decision, make_plan


PROJECT_ROOT = Path(__file__).resolve().parents[2]
POLICY_DIRECTORY = PROJECT_ROOT / "src" / "apex_procurement" / "policy"
PACK_PATH = POLICY_DIRECTORY / "compiled_policy.json"
CONCEPTS_PATH = POLICY_DIRECTORY / "concepts.json"
SCENARIO_PATH = PROJECT_ROOT / "data" / "scenarios" / "scenario_01_baseline.sqlite"


def _documents() -> tuple[dict[str, object], dict[str, object]]:
    return (
        json.loads(PACK_PATH.read_text(encoding="utf-8")),
        json.loads(CONCEPTS_PATH.read_text(encoding="utf-8")),
    )


def _rehash(pack: dict[str, object]) -> None:
    pack["content_hash"] = compute_content_hash(pack)


def test_reviewed_rendering_matrix_is_typed_and_complete() -> None:
    assert set(REVIEWED_RULE_RENDERING_CONTRACTS) == set(RuleKind)
    assert CONSTRAINT_RULE_KINDS | DIRECTIVE_RULE_KINDS == {
        kind.value for kind in RuleKind
    }
    assert not CONSTRAINT_RULE_KINDS & DIRECTIVE_RULE_KINDS

    for kind, contract in REVIEWED_RULE_RENDERING_CONTRACTS.items():
        assert contract.rule_kind is kind
        assert set(contract.disposition_paths) == set(ContractDisposition)
        assert all(
            isinstance(path, TerminalRenderingPath)
            for path in contract.disposition_paths.values()
        )
        assert (
            contract.path_for(ContractDisposition.EXECUTE_WITH_ASSUMPTION)
            is TerminalRenderingPath.ASSUMPTION_ALERT_AND_EXECUTABLE_DECISION
        )
        assert (
            contract.path_for(ContractDisposition.DECISION_REQUIRED)
            is TerminalRenderingPath.DECISION_REQUIRED_ALERT
        )
        assert (
            contract.path_for(ContractDisposition.EXECUTE)
            is TerminalRenderingPath.INTERNAL_ERROR
        )


def test_every_checked_in_declared_disposition_has_a_terminal_renderer() -> None:
    registry = load_policy_registry()
    for rule in registry.rules:
        body = rule.data.get("constraint", rule.data.get("directive"))
        assert body is not None
        kind = RuleKind(str(body["kind"]))
        for contract in registry.pack["contracts"].values():
            rule_resolution = contract["rule_resolutions"].get(rule.rule_id)
            if rule_resolution is not None:
                disposition = ContractDisposition(
                    str(rule_resolution["missing_disposition"])
                )
            else:
                basis_resolution = contract.get(rule.evidence_basis)
                if basis_resolution is None:
                    continue
                disposition = ContractDisposition(str(basis_resolution["disposition"]))
            assert (
                REVIEWED_RULE_RENDERING_CONTRACTS[kind].path_for(disposition)
                is not TerminalRenderingPath.INTERNAL_ERROR
            )
            if disposition is ContractDisposition.RECOMMEND_APPROVAL:
                rendered = approval_rule(registry, rule.rule_id, "supplier-under-test")
                assert rendered.rule_id == rule.rule_id


def test_pack_load_rejects_unsupported_rule_kind() -> None:
    pack, concepts = _documents()
    mutated = deepcopy(pack)
    rule = next(
        item
        for item in mutated["rules"]  # type: ignore[index,union-attr]
        if item.get("constraint", {}).get("kind") == "total_cost_of_ownership"
    )
    rule["constraint"]["kind"] = "unreviewed_cost_rule"
    _rehash(mutated)

    with pytest.raises(PolicyValidationError, match="unsupported kind"):
        validate_policy_documents(mutated, concepts, project_root=PROJECT_ROOT)


def test_pack_load_rejects_unknown_evidence_that_declares_execute() -> None:
    pack, concepts = _documents()
    mutated = deepcopy(pack)
    derivation_id = "r13_unsafe_unknown_execute"
    mutated["derivations"].append(  # type: ignore[index,union-attr]
        {
            "derivation_id": derivation_id,
            "value": "EXECUTE",
            "source_pointer": "MERGED_PLAN#R13/negative-load-test",
            "review_status": "approved",
            "reasoning": "Synthetic mutation proves missing evidence cannot authorize execution.",
        }
    )
    production = mutated["contracts"]["production"]["rolling_window"]  # type: ignore[index]
    production["disposition"] = "EXECUTE"
    production["derived_from"] = f"derivation:{derivation_id}"
    _rehash(mutated)

    with pytest.raises(PolicyValidationError, match="no reviewed terminal renderer"):
        validate_policy_documents(mutated, concepts, project_root=PROJECT_ROOT)


def test_runtime_rejects_unrendered_disposition_as_an_internal_error() -> None:
    registry = load_policy_registry()
    snapshot = load_snapshot(SCENARIO_PATH)
    original = next(
        rule
        for rule in registry.rules
        if rule.data.get("constraint", {}).get("kind")
        == "total_cost_of_ownership"
    )
    bypassed_load_validation = replace(original, evidence_basis="external_system")
    context = EvaluationContext(
        scenario_date=snapshot.configuration.current_date,
        suppliers=snapshot.suppliers,
        component=snapshot.components[0],
    )

    with pytest.raises(PolicyEvaluationError, match="internal policy rendering"):
        PolicyEvaluator(registry).evaluate_rule(bypassed_load_validation, context)


def test_runtime_rejects_missing_evaluator_as_an_internal_error() -> None:
    registry = load_policy_registry()
    snapshot = load_snapshot(SCENARIO_PATH)
    original = next(
        rule
        for rule in registry.rules
        if rule.data.get("constraint", {}).get("kind")
        == "total_cost_of_ownership"
    )
    data = dict(original.data)
    data["constraint"] = {"kind": "missing_software_implementation"}
    bypassed_load_validation = replace(original, data=data)
    context = EvaluationContext(
        scenario_date=snapshot.configuration.current_date,
        suppliers=snapshot.suppliers,
        component=snapshot.components[0],
        facts={"all_tco_costs_represented": None},
    )

    with pytest.raises(PolicyEvaluationError, match="internal policy evaluator"):
        PolicyEvaluator(registry).evaluate_rule(bypassed_load_validation, context)


def test_residual_without_a_rendered_terminal_path_fails_before_output() -> None:
    decision = make_decision(
        selected_plan=None,
        residual=Decimal("20"),
        resolution=ResolutionStatus.UNRESOLVED,
        alerts=(AlertCategory.UNMET_DEMAND,),
    )

    with pytest.raises(ExplanationError, match="lack a deterministic terminal explanation"):
        render_alerts((decision,))


@pytest.mark.parametrize(
    ("category", "owner_phrase"),
    (
        (AlertCategory.NO_ELIGIBLE_SUPPLIER, "Human action:"),
        (AlertCategory.POLICY_CONFLICT, "Human action:"),
        (AlertCategory.SOLVER_UNPROVEN, "Engineering action:"),
    ),
)
def test_runtime_terminal_explanations_are_complete(
    category: AlertCategory,
    owner_phrase: str,
) -> None:
    decision = make_decision(
        selected_plan=None,
        residual=Decimal("20"),
        resolution=ResolutionStatus.UNRESOLVED,
        alerts=(AlertCategory.UNMET_DEMAND, category),
    )
    rendered = render_alerts((decision,))
    terminal = next(item for item in rendered if item.category is category)
    parsed = parse_owned_alert(terminal.description)
    assert parsed is not None
    assert "The agent" in terminal.audit_description
    assert "Applicable rule IDs [" in terminal.audit_description
    assert owner_phrase in terminal.audit_description
    assert f"Component {decision.component_id}" in terminal.audit_description


def test_decision_required_terminal_explanation_names_withheld_action_and_rules() -> None:
    alternative = make_plan(
        disposition=PlanDisposition.DECISION_REQUIRED,
        plan_id="policy-decision-alternative",
    )
    decision = make_decision(
        selected_plan=None,
        alternatives=(alternative,),
        residual=Decimal("20"),
        resolution=ResolutionStatus.UNRESOLVED,
        alerts=(AlertCategory.UNMET_DEMAND, AlertCategory.DECISION_REQUIRED),
    )
    rendered = render_alerts((decision,))
    terminal = next(
        item for item in rendered if item.category is AlertCategory.DECISION_REQUIRED
    )
    parsed = parse_owned_alert(terminal.description)
    assert parsed is not None
    assert "alternative policy-decision-alternative" in terminal.audit_description
    assert "The agent did not place the alternative" in terminal.audit_description
    assert "Applicable rule IDs [" in terminal.audit_description
    assert "Human action:" in terminal.audit_description
