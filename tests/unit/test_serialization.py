from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
import unittest

from apex_procurement.config import EvidenceContract
from apex_procurement.domain import (
    DecisionRecord,
    DeadlineSupplyPosition,
    DemandBucket,
    DemandContribution,
    FulfillmentStatus,
    RequirementState,
    ResolutionStatus,
    ScenarioConfiguration,
    SourceEntityNormalizationDisclosure,
    SupplyLedger,
    UnitNormalizationDisclosure,
    UnitRoundingRule,
)
from apex_procurement.policy import load_policy_registry
from apex_procurement.serialization import (
    MAX_CANONICAL_JSON_BYTES,
    canonical_dumps,
    canonical_loads,
    sanitize_control_characters,
)


def make_record() -> DecisionRecord:
    due_date = date(2032, 4, 5)
    contribution = DemandContribution(
        order_id="order-beta",
        product_id="product-beta",
        quantity=Decimal("2.00"),
    )
    bucket = DemandBucket(
        component_id="component-beta",
        due_date=due_date,
        bucket_quantity=Decimal("2.00"),
        cumulative_quantity=Decimal("2.00"),
        contributions=(contribution,),
    )
    ledger = SupplyLedger(
        component_id="component-beta",
        total_demand=Decimal("2.00"),
        on_hand=Decimal("2.00"),
        committed_inbound=(),
        eventual_supply=Decimal("2.00"),
        eventual_gap=Decimal("0.00"),
        deadline_positions=(
            DeadlineSupplyPosition(
                due_date=due_date,
                cumulative_demand=Decimal("2.00"),
                on_time_supply=Decimal("2.00"),
                on_time_gap=Decimal("0.00"),
                recoverable_gap=Decimal("0.00"),
            ),
        ),
    )
    return DecisionRecord(
        requirement_id="requirement-beta",
        component_id="component-beta",
        evidence_contract=EvidenceContract.BENCHMARK,
        demand_buckets=(bucket,),
        supply_ledger=ledger,
        total_requirement=Decimal("2.00"),
        initial_eventual_gap=Decimal("0.00"),
        covered_quantity=Decimal("2.00"),
        residual_gap=Decimal("0.00"),
        requirement_state=RequirementState(
            FulfillmentStatus.FULFILLED,
            ResolutionStatus.RESOLVED,
        ),
        selected_plan=None,
        alternatives=(),
        evidence=(),
        alert_categories=(),
        rationale="Inventory already covers the requirement.",
    )


class SerializationTests(unittest.TestCase):
    def test_nested_record_is_deterministic_and_round_trips(self) -> None:
        registry = load_policy_registry()
        record = replace(
            make_record(),
            economic_autonomy=registry.economic_autonomy,
            normalization_disclosures=(
                SourceEntityNormalizationDisclosure(
                    source_id="source-old",
                    legal_name="Example Legal Name",
                    resolved_supplier_id="source-current",
                    rule_id="RULE.source-reference",
                    source_document="reviewed-policy.md",
                    reference_path="directive.supplier",
                ),
                UnitNormalizationDisclosure(
                    source_unit="box",
                    increment=Decimal("1"),
                    rounding_rule=(
                        UnitRoundingRule.DISCRETE_CEILING_AFTER_AGGREGATION
                    ),
                ),
            ),
        )

        first = canonical_dumps(record)
        second = canonical_dumps(record)
        restored = canonical_loads(first, DecisionRecord)

        self.assertEqual(first, second)
        self.assertEqual(restored, record)
        self.assertEqual(restored.economic_autonomy, registry.economic_autonomy)
        self.assertEqual(
            restored.normalization_disclosures,
            record.normalization_disclosures,
        )
        self.assertIsInstance(restored.total_requirement, Decimal)
        self.assertEqual(str(restored.total_requirement), "2.00")
        self.assertIsInstance(restored.demand_buckets[0].due_date, date)
        self.assertIn(
            '"current_date"',
            canonical_dumps(ScenarioConfiguration(date(2032, 4, 1))),
        )

        primitive = json.loads(first)

        def assert_no_float(value: object) -> None:
            self.assertNotIsInstance(value, float)
            if isinstance(value, dict):
                for item in value.values():
                    assert_no_float(item)
            elif isinstance(value, list):
                for item in value:
                    assert_no_float(item)

        assert_no_float(primitive)

    def test_json_float_token_is_rejected_for_decimal_field(self) -> None:
        payload = canonical_dumps(
            DemandContribution("order-gamma", "product-gamma", Decimal("1.50"))
        ).replace('"1.50"', "1.5")

        with self.assertRaises(ValueError):
            canonical_loads(payload, DemandContribution)

    def test_python_float_is_rejected_before_serialization(self) -> None:
        with self.assertRaises(TypeError):
            canonical_dumps({"quantity": 1.5})

    def test_control_sanitation_preserves_ordinary_unicode(self) -> None:
        printable_ascii = "APEX rationale 123: safe punctuation [ok]."
        self.assertIs(
            sanitize_control_characters(printable_ascii),
            printable_ascii,
        )
        self.assertEqual(
            sanitize_control_characters("café 東京 👨‍👩\x1b\u202e"),
            "café 東京 👨‍👩\ufffd\ufffd",
        )

    def test_untrusted_json_rejects_duplicates_and_oversized_payloads(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate JSON property"):
            canonical_loads('{"order_id":"a","order_id":"b"}', dict)
        with self.assertRaisesRegex(ValueError, "maximum supported size"):
            canonical_loads(" " * (MAX_CANONICAL_JSON_BYTES + 1), dict)


if __name__ == "__main__":
    unittest.main()
