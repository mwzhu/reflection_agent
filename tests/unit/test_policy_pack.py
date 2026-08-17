from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import re
import unittest

from apex_procurement.policy import (
    PolicyValidationError,
    compute_content_hash,
    load_policy_registry,
    validate_policy_documents,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
POLICY_DIRECTORY = PROJECT_ROOT / "src" / "apex_procurement" / "policy"
PACK_PATH = POLICY_DIRECTORY / "compiled_policy.json"
CONCEPTS_PATH = POLICY_DIRECTORY / "concepts.json"


def _documents() -> tuple[dict[str, object], dict[str, object]]:
    return (
        json.loads(PACK_PATH.read_text(encoding="utf-8")),
        json.loads(CONCEPTS_PATH.read_text(encoding="utf-8")),
    )


def _rehash(document: dict[str, object]) -> None:
    document["content_hash"] = compute_content_hash(document)


def _rule(pack: dict[str, object], rule_id: str) -> dict[str, object]:
    for rule in pack["rules"]:  # type: ignore[index,union-attr]
        if rule["rule_id"] == rule_id:
            return rule
    raise AssertionError(f"missing rule {rule_id}")


class PolicyPackTests(unittest.TestCase):
    def test_checked_in_pack_validates_with_pdf_provenance(self) -> None:
        registry = load_policy_registry()
        parameters = registry.parameters_for(date(2025, 9, 1))

        self.assertEqual(registry.pack_id, "apex-procurement-policy-2025-08-20")
        self.assertEqual(len(registry.rules), 35)
        self.assertTrue(registry.content_hash.startswith("sha256:"))
        self.assertTrue(registry.concepts_hash.startswith("sha256:"))
        self.assertFalse(registry.pack["compiler"]["llm_used"])
        self.assertEqual(
            parameters.domestic_premiums.ordinary.maximum_premium_fraction,
            Decimal("0.35"),
        )
        self.assertEqual(
            parameters.domestic_premiums.critical.maximum_premium_fraction,
            Decimal("0.50"),
        )
        self.assertEqual(
            parameters.strategic_continuity.maximum_alternative_savings_fraction,
            Decimal("0.15"),
        )
        self.assertEqual(
            parameters.sustainability.comparable_price_fraction,
            Decimal("0.10"),
        )
        self.assertEqual(parameters.sustainability.comparable_delivery_days, 5)
        self.assertEqual(
            tuple((item.amount_exceeds, item.authority) for item in parameters.approval_thresholds),
            (
                (Decimal("50000"), "Procurement Manager"),
                (Decimal("150000"), "VP of Operations"),
            ),
        )
        self.assertEqual(parameters.emergency_approval.amount_up_to, Decimal("75000"))
        self.assertEqual(parameters.emergency_approval.retroactive_approval_business_days, 5)
        self.assertEqual(
            tuple(
                (item.rule_id, item.maximum_fraction, item.window_months)
                for item in parameters.supplier_volume_caps
            ),
            (
                ("MEMO-2025-041.magnet_rolling_cap", Decimal("0.50"), 12),
                ("POL-PROC-001.section_4.critical_cap", Decimal("0.70"), 12),
                ("POL-PROC-001.section_4.noncritical_cap", Decimal("0.85"), 12),
            ),
        )
        self.assertEqual(
            tuple(
                (item.rule_id, item.minimum_fraction)
                for item in parameters.secondary_allocations
            ),
            (("MEMO-2025-041.magnet_secondary_allocation", Decimal("0.20")),),
        )
        self.assertEqual(
            tuple(item.maximum_amount for item in parameters.air_freight_period_caps),
            (Decimal("25000"),),
        )
        self.assertEqual(parameters.economic_autonomy.max_surplus_fraction, Decimal("0.10"))
        self.assertIsNone(parameters.economic_autonomy.max_surplus_units)
        self.assertEqual(parameters.economic_autonomy.max_excess_cost_usd, Decimal("2500"))
        self.assertEqual(parameters.economic_autonomy.forced_surplus_review_usd, Decimal("2500"))
        self.assertTrue(parameters.economic_autonomy.provisional)

    def test_air_freight_memo_is_active_on_last_day_and_inactive_next_day(self) -> None:
        registry = load_policy_registry()

        last_day = {
            rule.rule_id
            for rule in registry.active_rules(date(2025, 9, 30))
            if rule.source_document == "MEMO-2025-072"
        }
        next_day = {
            rule.rule_id
            for rule in registry.active_rules(date(2025, 10, 1))
            if rule.source_document == "MEMO-2025-072"
        }

        self.assertEqual(
            last_day,
            {
                "MEMO-2025-072.air_freight_authorization",
                "MEMO-2025-072.air_freight_approval",
                "MEMO-2025-072.air_freight_cost_documentation",
                "MEMO-2025-072.air_freight_period_cap",
            },
        )
        self.assertEqual(next_day, set())

    def test_magnet_memo_has_three_distinct_evidence_models(self) -> None:
        registry = load_policy_registry()
        rolling = registry.rule("MEMO-2025-041.magnet_rolling_cap")
        secondary = registry.rule("MEMO-2025-041.magnet_secondary_allocation")
        primary = registry.rule("MEMO-2025-041.magnet_named_primary")

        self.assertEqual(rolling.evidence_basis, "rolling_window")
        self.assertEqual(secondary.evidence_basis, "prospective_order")
        self.assertEqual(primary.evidence_basis, "prospective_order")
        self.assertEqual(primary.severity, "shaping")
        self.assertEqual(primary.data["release_condition"]["resolution"], "affirmative_record_required")
        self.assertEqual(primary.data["risk_disclosure"]["kind"], "CAPACITY_UNKNOWN")
        self.assertEqual(
            primary.data["risk_disclosure"]["subject_from"],
            "release_condition.subject",
        )
        self.assertEqual(primary.data["risk_disclosure"]["disposition_effect"], "none")
        self.assertEqual(
            registry.pack["contracts"]["benchmark"]["rule_resolutions"]
            ["MEMO-2025-085.pcb_incumbent_only"]["resolution_strategy"],
            "documented_inference",
        )

    def test_selectors_are_semantic_and_source_named_suppliers_are_complete(self) -> None:
        pack, _ = _documents()
        database_component_id = re.compile(
            r"\b(?:CMP|MFG|RM|SUP)-[A-Z0-9]+\b", re.IGNORECASE
        )
        named_entities: list[dict[str, object]] = []

        def visit(value: object) -> None:
            if isinstance(value, dict):
                if "source_id" in value or "legal_name" in value:
                    named_entities.append(value)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        for rule in pack["rules"]:  # type: ignore[index,union-attr]
            selector_json = json.dumps(rule["selector"], sort_keys=True)
            self.assertIsNone(database_component_id.search(selector_json), rule["rule_id"])
            visit(rule)

        self.assertGreaterEqual(len(named_entities), 2)
        for entity in named_entities:
            self.assertEqual(set(entity), {"source_id", "legal_name"})
            self.assertTrue(str(entity["source_id"]).strip())
            self.assertTrue(str(entity["legal_name"]).strip())

    def test_threshold_mutation_fails_even_after_rehash(self) -> None:
        pack, concepts = _documents()
        mutated = deepcopy(pack)
        _rule(mutated, "MEMO-2025-041.magnet_rolling_cap")["constraint"][
            "maximum_fraction"
        ] = "0.40"
        _rehash(mutated)

        with self.assertRaisesRegex(PolicyValidationError, "literal proves"):
            validate_policy_documents(mutated, concepts, project_root=PROJECT_ROOT)

    def test_literal_quote_mutation_fails_even_after_rehash(self) -> None:
        pack, concepts = _documents()
        mutated = deepcopy(pack)
        rule = _rule(mutated, "MEMO-2025-041.magnet_secondary_allocation")
        rule["coverage"]["/constraint/value"]["source_quote"] += " altered"
        _rehash(mutated)

        with self.assertRaisesRegex(PolicyValidationError, "literal contiguous source span"):
            validate_policy_documents(mutated, concepts, project_root=PROJECT_ROOT)

    def test_source_hash_mutation_fails_even_after_rehash(self) -> None:
        pack, concepts = _documents()
        mutated = deepcopy(pack)
        source = mutated["source_documents"][0]
        original = source["sha256"]
        source["sha256"] = original[:-1] + ("0" if original[-1] != "0" else "1")
        _rehash(mutated)

        with self.assertRaisesRegex(PolicyValidationError, "source hash mismatch"):
            validate_policy_documents(mutated, concepts, project_root=PROJECT_ROOT)

    def test_unreviewed_rule_mutation_fails_even_after_rehash(self) -> None:
        pack, concepts = _documents()
        mutated = deepcopy(pack)
        _rule(mutated, "POL-PROC-001.section_2.approved_supplier")[
            "review_status"
        ] = "draft"
        _rehash(mutated)

        with self.assertRaisesRegex(PolicyValidationError, "must be 'approved'"):
            validate_policy_documents(mutated, concepts, project_root=PROJECT_ROOT)

    def test_economic_autonomy_is_strictly_typed_and_reviewed(self) -> None:
        pack, concepts = _documents()
        mutations = (
            ("max_surplus_fraction", 0.25, "decimal string"),
            ("max_surplus_units", "NaN", "finite"),
            ("review_status", "draft", "approved"),
            ("boundary", "exclusive", "inclusive"),
        )
        for key, value, message in mutations:
            with self.subTest(key=key):
                mutated = deepcopy(pack)
                mutated["economic_autonomy"][key] = value
                _rehash(mutated)
                with self.assertRaisesRegex(PolicyValidationError, message):
                    validate_policy_documents(
                        mutated, concepts, project_root=PROJECT_ROOT
                    )


if __name__ == "__main__":
    unittest.main()
