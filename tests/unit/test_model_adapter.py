from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from apex_procurement.policy.model_adapter import (
    EntityClassification,
    EntityEvaluationCase,
    ModelAdapter,
    ModelCacheError,
    ModelResponseError,
    NarrationGuardError,
    NarrationResponse,
    OpenAICompatibleModelClient,
    PolicyExtractionResponse,
    PolicyPatchChange,
    PolicyPatchError,
    PolicyRuleSetResponse,
    PolicySentenceEvaluationCase,
    ReviewedPolicyPatch,
    StructuredModelCache,
    evaluate_entity_cases,
    evaluate_policy_sentence_cases,
    guard_narration,
    review_and_sign_policy_patch,
    verify_policy_patch_for_activation,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


class QueueClient:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.requests: list[dict[str, object]] = []

    def generate_structured(self, **request: object) -> object:
        self.calls += 1
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected model call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ModelAdapterTests(unittest.TestCase):
    def test_residual_response_is_schema_validated_cached_and_traced(self) -> None:
        client = QueueClient(EntityClassification(True, Decimal("0.91"), "grade evidence"))
        adapter = ModelAdapter(client)
        arguments = {
            "concept_id": "rare_material",
            "component_fingerprint": "a" * 64,
            "document_hash": DIGEST_B,
            "entity_label": "unseen grade label",
            "evidence_text": "A compact description of the entity.",
        }

        first = adapter.resolve_residual(**arguments)
        second = adapter.resolve_residual(**arguments)

        self.assertTrue(first.classification.member)
        self.assertFalse(first.trace.cache_hit)
        self.assertTrue(second.trace.cache_hit)
        self.assertTrue(second.trace.accepted)
        self.assertEqual(first.trace.cache_key, second.trace.cache_key)
        self.assertEqual(client.calls, 1)
        messages = client.requests[0]["messages"]
        self.assertIsInstance(messages, tuple)
        system_prompt = messages[0].content
        self.assertIn("Every limiting qualifier", system_prompt)
        self.assertIn("generic sensor or transducer", system_prompt)
        self.assertIn("industry-standard grade", system_prompt)
        self.assertIn("confidence below 0.85", system_prompt)

    def test_malformed_and_numerically_invalid_responses_are_rejected(self) -> None:
        malformed = QueueClient(
            {"member": True, "confidence": "0.8", "reason": "ok", "supplier": "invented"}
        )
        with self.assertRaises(ModelResponseError):
            ModelAdapter(malformed).resolve_residual(
                concept_id="component_class",
                component_fingerprint="a" * 64,
                document_hash=DIGEST_B,
                entity_label="label",
                evidence_text="evidence",
            )

        inconsistent = QueueClient(
            {"member": True, "confidence": "1.01", "reason": "overconfident"}
        )
        with self.assertRaises(ModelResponseError):
            ModelAdapter(inconsistent).resolve_residual(
                concept_id="component_class",
                component_fingerprint="a" * 64,
                document_hash=DIGEST_B,
                entity_label="label",
                evidence_text="evidence",
            )

    def test_persistent_cache_rejects_schema_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = StructuredModelCache(Path(directory))
            client = QueueClient(EntityClassification(False, Decimal("0.4"), "weak evidence"))
            adapter = ModelAdapter(client, cache=cache)
            result = adapter.resolve_residual(
                concept_id="component_class",
                component_fingerprint="a" * 64,
                document_hash=DIGEST_B,
                entity_label="label",
                evidence_text="evidence",
            )
            new_cache = StructuredModelCache(Path(directory))

            with self.assertRaisesRegex(ModelCacheError, "schema"):
                new_cache.get(result.trace.cache_key, NarrationResponse)

    def test_narration_guard_rejects_invented_facts_and_dropped_caveats(self) -> None:
        template = (
            "Requirement item-alpha orders quantity 12 from vendor-beta. "
            "resolution UNRESOLVED; evidence contract benchmark."
        )
        facts = {
            "requirement_id": "item-alpha",
            "supplier_id": "vendor-beta",
            "quantity": "12",
            "resolution": "UNRESOLVED",
        }
        caveats = ("resolution UNRESOLVED", "evidence contract benchmark")
        self.assertIn(
            "quantity 12",
            guard_narration(template, template=template, facts=facts, required_caveats=caveats),
        )
        with self.assertRaises(NarrationGuardError):
            guard_narration(
                template.replace("12", "13"),
                template=template,
                facts=facts,
                required_caveats=caveats,
            )
        with self.assertRaises(NarrationGuardError):
            guard_narration(
                template.replace("vendor-beta", "vendor-gamma"),
                template=template,
                facts=facts,
                required_caveats=caveats,
            )
        with self.assertRaises(NarrationGuardError):
            guard_narration(
                template.replace("resolution UNRESOLVED; ", ""),
                template=template,
                facts=facts,
                required_caveats=caveats,
            )

    def test_adapter_rejects_model_narration_before_it_can_replace_template(self) -> None:
        client = QueueClient(NarrationResponse("item-alpha quantity 999; decision required."))
        with self.assertRaises(NarrationGuardError) as raised:
            ModelAdapter(client).polish_narration(
                template="item-alpha quantity 12; decision required.",
                facts={"item": "item-alpha", "quantity": "12"},
                required_caveats=("decision required",),
            )
        self.assertIsNotNone(raised.exception.trace)
        self.assertFalse(raised.exception.trace.accepted)

    def test_policy_patch_stays_inactive_until_spans_hashes_and_signature_verify(self) -> None:
        source_text = "Orders exceeding $50,000 require Procurement Manager approval."
        source_bytes = b"stable source document bytes"
        change = PolicyPatchChange(
            operation="replace",
            path="/rules/0/constraint/amount_exceeds",
            value="50000",
            source_quote=source_text,
            value_literal="$50,000",
            value_format="currency",
        )
        adapter = ModelAdapter(QueueClient(PolicyExtractionResponse((change,))))
        draft = adapter.generate_policy_patch(
            document_id="policy-document",
            source_text=source_text,
            source_bytes=source_bytes,
            base_pack_hash=DIGEST_A,
        )

        self.assertEqual(draft.review_status, "pending_review")
        with self.assertRaises(PolicyPatchError):
            review_and_sign_policy_patch(
                draft,
                source_text=source_text,
                source_bytes=source_bytes,
                reviewer="reviewer",
                signing_key=b"review-key-at-least-sixteen-bytes",
                approved=False,
            )

        reviewed = review_and_sign_policy_patch(
            draft,
            source_text=source_text,
            source_bytes=source_bytes,
            reviewer="reviewer",
            signing_key=b"review-key-at-least-sixteen-bytes",
            approved=True,
        )
        verify_policy_patch_for_activation(
            reviewed,
            source_text=source_text,
            source_bytes=source_bytes,
            signing_key=b"review-key-at-least-sixteen-bytes",
            expected_base_pack_hash=DIGEST_A,
        )
        forged = ReviewedPolicyPatch(
            draft=replace(draft, base_pack_hash=DIGEST_B),
            reviewer=reviewed.reviewer,
            review_status="approved",
            signature=reviewed.signature,
        )
        with self.assertRaises(PolicyPatchError):
            verify_policy_patch_for_activation(
                forged,
                source_text=source_text,
                source_bytes=source_bytes,
                signing_key=b"review-key-at-least-sixteen-bytes",
                expected_base_pack_hash=DIGEST_B,
            )

    def test_policy_patch_rejects_nonliteral_and_numerically_inconsistent_changes(self) -> None:
        source = "The stated limit is 50%."
        nonliteral = PolicyPatchChange(
            "replace", "/rules/0/constraint/value", "0.50", "limit is fifty percent", "50%", "percent_fraction"
        )
        inconsistent = PolicyPatchChange(
            "replace", "/rules/0/constraint/value", "0.40", source, "50%", "percent_fraction"
        )
        for change in (nonliteral, inconsistent):
            with self.subTest(change=change):
                with self.assertRaises(PolicyPatchError):
                    ModelAdapter(QueueClient(PolicyExtractionResponse((change,)))).generate_policy_patch(
                        document_id="policy-document",
                        source_text=source,
                        source_bytes=b"source bytes",
                        base_pack_hash=DIGEST_A,
                    )

    def test_openai_client_is_unconfigured_without_env_and_transport_is_injectable(self) -> None:
        self.assertIsNone(OpenAICompatibleModelClient.from_environment({}))
        calls: list[Mapping[str, object]] = []

        def transport(
            url: str,
            headers: Mapping[str, str],
            payload: Mapping[str, object],
            timeout: float,
        ) -> Mapping[str, object]:
            self.assertEqual(url, "http://model.invalid/v1/chat/completions")
            self.assertNotIn("Authorization", headers)
            self.assertGreater(timeout, 0)
            calls.append(payload)
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"member":true,"confidence":"0.75","reason":"label evidence"}'
                        }
                    }
                ]
            }

        client = OpenAICompatibleModelClient.from_environment(
            {"LLM_BASE_URL": "http://model.invalid", "LLM_MODEL": "test-model"},
            transport=transport,
        )
        assert client is not None
        result = ModelAdapter(client).resolve_residual(
            concept_id="component_class",
            component_fingerprint="a" * 64,
            document_hash=DIGEST_B,
            entity_label="label",
            evidence_text="evidence",
        )
        self.assertTrue(result.classification.member)
        self.assertEqual(len(calls), 1)

    def test_openai_client_omits_unsupported_temperature_for_gpt_5_6(self) -> None:
        calls: list[Mapping[str, object]] = []

        def transport(
            _url: str,
            _headers: Mapping[str, str],
            payload: Mapping[str, object],
            _timeout: float,
        ) -> Mapping[str, object]:
            calls.append(payload)
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"member":false,"confidence":"0.95",'
                            '"reason":"bounded evidence excludes membership"}'
                        }
                    }
                ]
            }

        client = OpenAICompatibleModelClient(
            base_url="https://api.openai.com",
            model="gpt-5.6-sol",
            transport=transport,
        )
        result = ModelAdapter(client).resolve_residual(
            concept_id="component_class",
            component_fingerprint="a" * 64,
            document_hash=DIGEST_B,
            entity_label="label",
            evidence_text="evidence",
        )

        self.assertFalse(result.classification.member)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("temperature", calls[0])
        self.assertEqual(calls[0]["seed"], 0)

    def test_held_out_evaluations_report_measured_accuracy(self) -> None:
        client = QueueClient(
            EntityClassification(True, Decimal("0.9"), "positive"),
            EntityClassification(True, Decimal("0.6"), "incorrect positive"),
            PolicyRuleSetResponse(("rule-a",)),
            PolicyRuleSetResponse(("rule-a",)),
        )
        adapter = ModelAdapter(client)
        entity_report = evaluate_entity_cases(
            adapter,
            (
                EntityEvaluationCase(
                    "label-variant-1", "concept", "a" * 64, DIGEST_B, "unseen positive", "context", True
                ),
                EntityEvaluationCase(
                    "label-variant-2", "concept", "b" * 64, DIGEST_B, "unseen negative", "context", False
                ),
            ),
        )
        policy_report = evaluate_policy_sentence_cases(
            adapter,
            (
                PolicySentenceEvaluationCase(
                    "sentence-variant-1", "Perturbed sentence one.", ("rule-a", "rule-b"), ("rule-a",), DIGEST_A
                ),
                PolicySentenceEvaluationCase(
                    "sentence-variant-2", "Perturbed sentence two.", ("rule-a", "rule-b"), ("rule-b",), DIGEST_A
                ),
            ),
        )

        self.assertEqual((entity_report.correct, entity_report.total), (1, 2))
        self.assertEqual(entity_report.accuracy, Decimal("0.5"))
        self.assertEqual(entity_report.failed_case_ids, ("label-variant-2",))
        self.assertEqual((policy_report.correct, policy_report.total), (1, 2))
        self.assertEqual(policy_report.accuracy, Decimal("0.5"))
        self.assertEqual(policy_report.failed_case_ids, ("sentence-variant-2",))


if __name__ == "__main__":
    unittest.main()
