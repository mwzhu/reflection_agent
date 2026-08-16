"""Strict schema and provenance validation for compiled policy artifacts.

The live agent consumes checked-in JSON.  It never parses policy PDFs and it
never asks a model to interpret a rule.  This module deliberately uses only
the standard library so validation is available on the default offline path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Any


class PolicyValidationError(ValueError):
    """Raised when a compiled policy artifact is not safe to activate."""


SCHEMA_VERSION = "1.0.0"
HASH_PREFIX = "sha256:"

SEVERITIES = frozenset({"hard", "shaping", "advisory"})
EVIDENCE_BASES = frozenset(
    {"prospective_order", "entity_attribute", "rolling_window", "external_system"}
)
DISPOSITIONS = frozenset(
    {"EXECUTE", "EXECUTE_WITH_ASSUMPTION", "RECOMMEND_APPROVAL", "DECISION_REQUIRED"}
)
RULE_KINDS = frozenset(
    {
        "supplier_eligibility",
        "component_classification",
        "sourcing_preference",
        "allocation_constraint",
        "quantity_constraint",
        "lead_time_modifier",
        "approval_threshold",
        "documentation_requirement",
    }
)
CONSTRAINT_KINDS = frozenset(
    {
        "approved_supplier_required",
        "required_certification",
        "domestic_supplier_preference",
        "international_sourcing_justification",
        "supplier_volume_cap",
        "minimum_qualified_suppliers",
        "sole_source_justification",
        "catalog_minimum_order_quantity",
        "sub_moq_written_approval",
        "hazardous_receiving_and_storage",
        "hazardous_procurement_review",
        "critical_component_categories",
        "total_cost_of_ownership",
        "order_value_approval",
        "emergency_approval_bypass",
        "sustainability_preference",
        "below_rating_review",
        "certification_preference",
        "strategic_supplier_continuity",
        "strategic_volume_shift_approval",
        "on_time_arrival",
        "quoted_lead_time_delivery_date",
        "minimum_secondary_fraction",
        "air_freight_authorization",
        "air_freight_cost_documentation",
        "air_freight_period_spend_cap",
        "air_freight_individual_approval",
        "shipment_certificate_of_conformance",
        "incumbent_supplier_only",
    }
)
DIRECTIVE_KINDS = frozenset({"named_primary_supplier"})
SELECTOR_ENTITIES = frozenset(
    {"component", "supplier_route", "allocation_group", "purchase_order", "shipment"}
)
SELECTOR_OPERATORS = frozenset({"any", "all", "none"})
VALUE_FORMATS = frozenset(
    {
        "exact_text",
        "normalized_token",
        "percent_fraction",
        "decimal",
        "integer",
        "currency",
        "date_long",
        "affirmed_boolean",
    }
)

POLICY_PACK_SCHEMA: Mapping[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Apex compiled policy pack",
    "type": "object",
    "required": [
        "schema_version",
        "pack_id",
        "compiler",
        "review_status",
        "content_hash",
        "concepts",
        "source_documents",
        "evidence_bases",
        "contracts",
        "precedence_model",
        "derivations",
        "rules",
    ],
    "additionalProperties": False,
}

CONCEPTS_SCHEMA: Mapping[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Apex deterministic semantic concepts",
    "type": "object",
    "required": [
        "schema_version",
        "pack_id",
        "review_status",
        "content_hash",
        "normalization",
        "country_aliases",
        "concepts",
    ],
    "additionalProperties": False,
}

_ROOT_KEYS = frozenset(POLICY_PACK_SCHEMA["required"])
_CONCEPT_ROOT_KEYS = frozenset(CONCEPTS_SCHEMA["required"])
_COMPILER_KEYS = frozenset({"name", "version", "llm_used"})
_CONCEPT_REF_KEYS = frozenset({"path", "content_hash"})
_SOURCE_KEYS = frozenset(
    {
        "document_id",
        "document_type",
        "path",
        "sha256",
        "text_sha256",
        "effective_from",
        "effective_through",
        "source_text",
        "coverage",
    }
)
_RULE_KEYS = frozenset(
    {
        "rule_id",
        "source_document",
        "title",
        "review_status",
        "effective_from",
        "effective_through",
        "severity",
        "evidence_basis",
        "selector",
        "constraint",
        "directive",
        "release_condition",
        "risk_disclosure",
        "precedence",
        "coverage",
    }
)
_DERIVATION_KEYS = frozenset(
    {"derivation_id", "value", "source_pointer", "review_status", "reasoning"}
)
_DIRECT_PROOF_KEYS = frozenset({"source_quote", "value_literal", "value_format"})
_DERIVED_PROOF_KEYS = frozenset({"derived_from"})
_LOAD_BEARING_ROOTS = (
    "selector",
    "constraint",
    "directive",
    "release_condition",
    "risk_disclosure",
    "precedence",
)
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_ENTITY_PATTERN = re.compile(r"^(?P<prefix>[A-Z]+)-(?P<number>\d+)$")
_SELECTOR_IDENTIFIER_PATTERN = re.compile(
    r"\b(?:CMP|MFG|RM|SUP)-[A-Z0-9]+\b", re.IGNORECASE
)


def _fail(location: str, message: str) -> None:
    raise PolicyValidationError(f"{location}: {message}")


def _expect_mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(location, "must be an object")
    return value


def _expect_list(value: Any, location: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(location, "must be an array")
    return value


def _expect_keys(value: Mapping[str, Any], expected: frozenset[str], location: str) -> None:
    keys = frozenset(value)
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        _fail(location, f"property mismatch (missing={missing}, extra={extra})")


def _expect_text(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(location, "must be non-empty text")
    if any(ord(character) < 32 and character not in "\n\t\f" for character in value):
        _fail(location, "must not contain unsupported control characters")
    return value


def _expect_reviewed(value: Any, location: str) -> None:
    if value != "approved":
        _fail(location, "must be 'approved' before it can constrain live actions")


def _expect_date(value: Any, location: str, *, optional: bool = False) -> date | None:
    if value is None and optional:
        return None
    text = _expect_text(value, location)
    if not _DATE_PATTERN.fullmatch(text):
        _fail(location, "must use YYYY-MM-DD")
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise PolicyValidationError(f"{location}: invalid date {text!r}") from error


def _expect_hash(value: Any, location: str) -> str:
    text = _expect_text(value, location)
    if not _HASH_PATTERN.fullmatch(text):
        _fail(location, "must be a lowercase sha256 digest")
    return text


def _canonical_json(document: Mapping[str, Any], *, omit_content_hash: bool) -> bytes:
    value = dict(document)
    if omit_content_hash:
        value.pop("content_hash", None)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def compute_content_hash(document: Mapping[str, Any]) -> str:
    """Return the canonical self-excluding content hash for a JSON artifact."""

    return HASH_PREFIX + hashlib.sha256(
        _canonical_json(document, omit_content_hash=True)
    ).hexdigest()


def _verify_content_hash(document: Mapping[str, Any], location: str) -> None:
    declared = _expect_hash(document.get("content_hash"), f"{location}.content_hash")
    actual = compute_content_hash(document)
    if declared != actual:
        _fail(location, f"content hash mismatch: declared {declared}, computed {actual}")


def _normalise_span(value: str) -> str:
    return " ".join(value.split())


def _json_pointer(value: Any, pointer: str, location: str) -> Any:
    if not pointer.startswith("/"):
        _fail(location, "must be an absolute JSON pointer")
    current = value
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                _fail(location, f"does not resolve; missing {token!r}")
            current = current[token]
        elif isinstance(current, list):
            try:
                current = current[int(token)]
            except (ValueError, IndexError) as error:
                raise PolicyValidationError(
                    f"{location}: does not resolve list index {token!r}"
                ) from error
        else:
            _fail(location, f"does not resolve through scalar at {token!r}")
    return current


def _leaf_paths(value: Any, prefix: str) -> set[str]:
    if isinstance(value, Mapping):
        paths: set[str] = set()
        for key, child in value.items():
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            paths.update(_leaf_paths(child, f"{prefix}/{escaped}"))
        return paths
    if isinstance(value, list):
        paths = set()
        for index, child in enumerate(value):
            paths.update(_leaf_paths(child, f"{prefix}/{index}"))
        return paths
    return {prefix}


def _validate_source_named_entities(value: Any, location: str) -> None:
    if isinstance(value, Mapping):
        entity_keys = {"source_id", "legal_name"} & set(value)
        if entity_keys:
            if not {"source_id", "legal_name"} <= set(value):
                _fail(location, "source-named supplier references require source_id and legal_name")
            source_id = _expect_text(value["source_id"], f"{location}.source_id")
            _expect_text(value["legal_name"], f"{location}.legal_name")
            match = _SOURCE_ENTITY_PATTERN.fullmatch(source_id)
            if match is None or match.group("prefix") != "SUP":
                _fail(f"{location}.source_id", "must be a source-named supplier identifier")
        for key, child in value.items():
            _validate_source_named_entities(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_source_named_entities(child, f"{location}[{index}]")


def _required_coverage(rule: Mapping[str, Any]) -> set[str]:
    paths = {"/effective_from", "/severity", "/evidence_basis"}
    if rule.get("effective_through") is not None:
        paths.add("/effective_through")
    for root in _LOAD_BEARING_ROOTS:
        if root in rule:
            paths.update(_leaf_paths(rule[root], f"/{root}"))
    return paths


def _parse_literal(literal: str, value_format: str, location: str) -> Any:
    if value_format == "exact_text":
        return literal
    if value_format == "normalized_token":
        return "".join(character for character in literal.upper() if character.isalnum())
    if value_format == "percent_fraction":
        match = re.fullmatch(r"\s*([+-]?\d+(?:\.\d+)?)\s*%\s*", literal)
        if match is None:
            _fail(location, "percent_fraction literal must contain exactly one percentage")
        return Decimal(match.group(1)) / Decimal("100")
    if value_format in {"decimal", "currency"}:
        candidate = literal.strip().replace(",", "")
        if value_format == "currency":
            candidate = candidate.removeprefix("$")
        try:
            return Decimal(candidate)
        except InvalidOperation as error:
            raise PolicyValidationError(f"{location}: invalid decimal literal {literal!r}") from error
    if value_format == "integer":
        if not re.fullmatch(r"[+-]?\d+", literal.strip()):
            _fail(location, "integer literal must contain exactly one integer")
        return int(literal)
    if value_format == "date_long":
        try:
            return datetime.strptime(literal.strip(), "%B %d, %Y").date().isoformat()
        except ValueError as error:
            raise PolicyValidationError(f"{location}: invalid long-form date {literal!r}") from error
    if value_format == "affirmed_boolean":
        return True
    _fail(location, f"unsupported value format {value_format!r}")


def _values_equal(actual: Any, parsed: Any, value_format: str) -> bool:
    if value_format in {"percent_fraction", "decimal", "currency"}:
        if not isinstance(actual, str):
            return False
        try:
            return Decimal(actual) == parsed
        except InvalidOperation:
            return False
    if value_format == "normalized_token":
        if not isinstance(actual, str):
            return False
        normalized_actual = "".join(
            character for character in actual.upper() if character.isalnum()
        )
        return normalized_actual == parsed
    return actual == parsed


def _validate_direct_proof(
    proof: Mapping[str, Any],
    *,
    actual: Any,
    source_text: str,
    location: str,
) -> None:
    _expect_keys(proof, _DIRECT_PROOF_KEYS, location)
    quote = _expect_text(proof["source_quote"], f"{location}.source_quote")
    literal = _expect_text(proof["value_literal"], f"{location}.value_literal")
    value_format = _expect_text(proof["value_format"], f"{location}.value_format")
    if value_format not in VALUE_FORMATS:
        _fail(f"{location}.value_format", f"must be one of {sorted(VALUE_FORMATS)}")
    normalized_source = _normalise_span(source_text)
    normalized_quote = _normalise_span(quote)
    normalized_literal = _normalise_span(literal)
    if normalized_quote not in normalized_source:
        _fail(f"{location}.source_quote", "is not a literal contiguous source span")
    if normalized_literal not in normalized_quote:
        _fail(f"{location}.value_literal", "is not contained in its covering source span")
    parsed = _parse_literal(literal, value_format, f"{location}.value_literal")
    if not _values_equal(actual, parsed, value_format):
        _fail(location, f"literal proves {parsed!r}, not compiled value {actual!r}")


def _resolve_derived(
    reference: str,
    *,
    pack: Mapping[str, Any],
    concepts_by_id: Mapping[str, Mapping[str, Any]],
    rules_by_id: Mapping[str, Mapping[str, Any]],
    sources_by_id: Mapping[str, Mapping[str, Any]],
    derivations_by_id: Mapping[str, Mapping[str, Any]],
    location: str,
) -> Any:
    if reference.startswith("registry:"):
        body = reference.removeprefix("registry:")
        try:
            namespace, value = body.split("/", 1)
        except ValueError:
            _fail(location, "registry pointer must be registry:<namespace>/<value>")
        registries = {
            "severity": SEVERITIES,
            "evidence_basis": EVIDENCE_BASES,
            "rule_kind": RULE_KINDS,
            "constraint_kind": CONSTRAINT_KINDS,
            "directive_kind": DIRECTIVE_KINDS,
            "selector_entity": SELECTOR_ENTITIES,
            "selector_operator": SELECTOR_OPERATORS,
            "disposition": DISPOSITIONS,
        }
        if namespace not in registries or value not in registries[namespace]:
            _fail(location, f"unknown registry pointer {reference!r}")
        return value
    if reference.startswith("concept:"):
        concept_id = reference.removeprefix("concept:")
        if concept_id not in concepts_by_id:
            _fail(location, f"unknown concept pointer {reference!r}")
        return concept_id
    if reference.startswith("derivation:"):
        derivation_id = reference.removeprefix("derivation:")
        if derivation_id not in derivations_by_id:
            _fail(location, f"unknown derivation pointer {reference!r}")
        return derivations_by_id[derivation_id]["value"]
    if reference.startswith("source:"):
        body = reference.removeprefix("source:")
        if "#" not in body:
            _fail(location, "source pointer must include a JSON pointer fragment")
        document_id, pointer = body.split("#", 1)
        if document_id not in sources_by_id:
            _fail(location, f"unknown source pointer {reference!r}")
        return _json_pointer(sources_by_id[document_id], pointer, location)
    if reference.startswith("rule:"):
        body = reference.removeprefix("rule:")
        if "#" not in body:
            _fail(location, "rule pointer must include a JSON pointer fragment")
        rule_id, pointer = body.split("#", 1)
        if rule_id not in rules_by_id:
            _fail(location, f"unknown rule pointer {reference!r}")
        return _json_pointer(rules_by_id[rule_id], pointer, location)
    _fail(location, f"unsupported derived_from pointer {reference!r}")


def _validate_concepts(
    concepts: Mapping[str, Any], sources_by_id: Mapping[str, Mapping[str, Any]]
) -> Mapping[str, Mapping[str, Any]]:
    _expect_keys(concepts, _CONCEPT_ROOT_KEYS, "concepts")
    if concepts["schema_version"] != SCHEMA_VERSION:
        _fail("concepts.schema_version", f"must be {SCHEMA_VERSION!r}")
    _expect_text(concepts["pack_id"], "concepts.pack_id")
    _expect_reviewed(concepts["review_status"], "concepts.review_status")
    _verify_content_hash(concepts, "concepts")

    normalization = _expect_mapping(concepts["normalization"], "concepts.normalization")
    _expect_keys(
        normalization,
        frozenset({"casefold", "unicode_normalization", "token_boundary_matching"}),
        "concepts.normalization",
    )
    if normalization != {
        "casefold": True,
        "unicode_normalization": "NFKC",
        "token_boundary_matching": True,
    }:
        _fail("concepts.normalization", "must use the reviewed deterministic normalization")

    aliases = _expect_mapping(concepts["country_aliases"], "concepts.country_aliases")
    if not aliases:
        _fail("concepts.country_aliases", "must not be empty")
    for canonical, values in aliases.items():
        _expect_text(canonical, f"concepts.country_aliases.{canonical}")
        items = _expect_list(values, f"concepts.country_aliases.{canonical}")
        if not items:
            _fail(f"concepts.country_aliases.{canonical}", "must not be empty")
        for index, alias in enumerate(items):
            _expect_text(alias, f"concepts.country_aliases.{canonical}[{index}]")

    concept_items = _expect_list(concepts["concepts"], "concepts.concepts")
    concepts_by_id: dict[str, Mapping[str, Any]] = {}
    required_keys = frozenset(
        {
            "concept_id",
            "entity_kind",
            "resolution",
            "source_document",
            "source_quote",
            "source_terms",
            "structured_signals",
            "synonyms",
            "positive_fixtures",
            "negative_fixtures",
            "review_status",
        }
    )
    for index, raw_concept in enumerate(concept_items):
        location = f"concepts.concepts[{index}]"
        concept = _expect_mapping(raw_concept, location)
        _expect_keys(concept, required_keys, location)
        concept_id = _expect_text(concept["concept_id"], f"{location}.concept_id")
        if concept_id in concepts_by_id:
            _fail(f"{location}.concept_id", f"duplicate concept {concept_id!r}")
        if concept["entity_kind"] not in {"component", "supplier", "demand"}:
            _fail(f"{location}.entity_kind", "has an unsupported entity kind")
        if concept["resolution"] not in {
            "enumerated_both_ways",
            "positive_evidence_required",
            "deterministic",
        }:
            _fail(f"{location}.resolution", "has an unsupported resolution policy")
        _expect_reviewed(concept["review_status"], f"{location}.review_status")
        document_id = _expect_text(
            concept["source_document"], f"{location}.source_document"
        )
        if document_id not in sources_by_id:
            _fail(f"{location}.source_document", f"unknown source {document_id!r}")
        quote = _expect_text(concept["source_quote"], f"{location}.source_quote")
        if _normalise_span(quote) not in _normalise_span(
            sources_by_id[document_id]["source_text"]
        ):
            _fail(f"{location}.source_quote", "is not a literal contiguous source span")
        for key in (
            "source_terms",
            "structured_signals",
            "synonyms",
            "positive_fixtures",
            "negative_fixtures",
        ):
            values = _expect_list(concept[key], f"{location}.{key}")
            for item_index, item in enumerate(values):
                _expect_text(item, f"{location}.{key}[{item_index}]")
                if key == "source_terms" and _normalise_span(item).casefold() not in (
                    _normalise_span(sources_by_id[document_id]["source_text"]).casefold()
                ):
                    _fail(
                        f"{location}.{key}[{item_index}]",
                        "is not a literal term in the cited source document",
                    )
        if not concept["positive_fixtures"]:
            _fail(f"{location}.positive_fixtures", "must not be empty")
        if not concept["negative_fixtures"]:
            _fail(f"{location}.negative_fixtures", "must not be empty")
        if set(map(str.casefold, concept["positive_fixtures"])) & set(
            map(str.casefold, concept["negative_fixtures"])
        ):
            _fail(location, "positive and negative fixtures must be disjoint")
        concepts_by_id[concept_id] = concept
    return concepts_by_id


def _validate_source_documents(
    pack: Mapping[str, Any], project_root: Path | None
) -> Mapping[str, Mapping[str, Any]]:
    items = _expect_list(pack["source_documents"], "policy.source_documents")
    sources_by_id: dict[str, Mapping[str, Any]] = {}
    for index, raw_source in enumerate(items):
        location = f"policy.source_documents[{index}]"
        source = _expect_mapping(raw_source, location)
        _expect_keys(source, _SOURCE_KEYS, location)
        document_id = _expect_text(source["document_id"], f"{location}.document_id")
        if document_id in sources_by_id:
            _fail(f"{location}.document_id", f"duplicate source {document_id!r}")
        if source["document_type"] not in {"policy", "memo"}:
            _fail(f"{location}.document_type", "must be 'policy' or 'memo'")
        relative_path = _expect_text(source["path"], f"{location}.path")
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts or path.suffix.casefold() != ".pdf":
            _fail(f"{location}.path", "must be a repository-relative PDF path")
        declared_source_hash = _expect_hash(source["sha256"], f"{location}.sha256")
        declared_text_hash = _expect_hash(source["text_sha256"], f"{location}.text_sha256")
        source_text = _expect_text(source["source_text"], f"{location}.source_text")
        actual_text_hash = HASH_PREFIX + hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        if declared_text_hash != actual_text_hash:
            _fail(location, "embedded source text hash mismatch")
        effective_from = _expect_date(source["effective_from"], f"{location}.effective_from")
        effective_through = _expect_date(
            source["effective_through"], f"{location}.effective_through", optional=True
        )
        if effective_through is not None and effective_through < effective_from:
            _fail(location, "effective_through precedes effective_from")
        coverage = _expect_mapping(source["coverage"], f"{location}.coverage")
        required_coverage = {"/document_id", "/effective_from"}
        if source["effective_through"] is not None:
            required_coverage.add("/effective_through")
        if set(coverage) != required_coverage:
            _fail(
                f"{location}.coverage",
                "must cover document_id and every non-null effective-window endpoint",
            )
        for pointer, raw_proof in coverage.items():
            proof_location = f"{location}.coverage[{pointer!r}]"
            proof = _expect_mapping(raw_proof, proof_location)
            actual = _json_pointer(source, pointer, proof_location)
            _validate_direct_proof(
                proof,
                actual=actual,
                source_text=source_text,
                location=proof_location,
            )
        if project_root is not None:
            resolved = (project_root / path).resolve()
            try:
                resolved.relative_to(project_root.resolve())
            except ValueError:
                _fail(f"{location}.path", "resolves outside the project root")
            if not resolved.is_file():
                _fail(f"{location}.path", f"source file does not exist: {resolved}")
            actual_source_hash = HASH_PREFIX + hashlib.sha256(resolved.read_bytes()).hexdigest()
            if actual_source_hash != declared_source_hash:
                _fail(
                    f"{location}.sha256",
                    f"source hash mismatch: declared {declared_source_hash}, computed {actual_source_hash}",
                )
        sources_by_id[document_id] = source
    if not sources_by_id:
        _fail("policy.source_documents", "must not be empty")
    return sources_by_id


def _validate_evidence_contracts(pack: Mapping[str, Any]) -> None:
    bases = _expect_mapping(pack["evidence_bases"], "policy.evidence_bases")
    if frozenset(bases) != EVIDENCE_BASES:
        _fail("policy.evidence_bases", f"must define exactly {sorted(EVIDENCE_BASES)}")
    for basis, raw_definition in bases.items():
        location = f"policy.evidence_bases.{basis}"
        definition = _expect_mapping(raw_definition, location)
        _expect_keys(
            definition,
            frozenset({"resolution_strategy", "derived_from"}),
            location,
        )
        if definition["resolution_strategy"] not in {
            "always_satisfiable",
            "both_ways",
            "contract_disposition",
        }:
            _fail(f"{location}.resolution_strategy", "unsupported resolution strategy")
        _expect_text(definition["derived_from"], f"{location}.derived_from")

    contracts = _expect_mapping(pack["contracts"], "policy.contracts")
    if frozenset(contracts) != {"benchmark", "production"}:
        _fail("policy.contracts", "must define exactly benchmark and production")
    for contract_name, raw_contract in contracts.items():
        contract = _expect_mapping(raw_contract, f"policy.contracts.{contract_name}")
        if frozenset(contract) != {"rolling_window", "external_system", "rule_resolutions"}:
            _fail(
                f"policy.contracts.{contract_name}",
                "must map general evidence bases and explicit rule resolutions",
            )
        for basis in ("rolling_window", "external_system"):
            raw_resolution = contract[basis]
            location = f"policy.contracts.{contract_name}.{basis}"
            resolution = _expect_mapping(raw_resolution, location)
            _expect_keys(resolution, frozenset({"disposition", "derived_from"}), location)
            if resolution["disposition"] not in DISPOSITIONS:
                _fail(f"{location}.disposition", "unsupported disposition")
            _expect_text(resolution["derived_from"], f"{location}.derived_from")
        rule_resolutions = _expect_mapping(
            contract["rule_resolutions"], f"policy.contracts.{contract_name}.rule_resolutions"
        )
        for rule_id, raw_resolution in rule_resolutions.items():
            location = f"policy.contracts.{contract_name}.rule_resolutions.{rule_id}"
            resolution = _expect_mapping(raw_resolution, location)
            _expect_keys(
                resolution,
                frozenset(
                    {
                        "resolution_strategy",
                        "missing_disposition",
                        "strategy_derived_from",
                        "disposition_derived_from",
                    }
                ),
                location,
            )
            _expect_text(resolution["resolution_strategy"], f"{location}.resolution_strategy")
            if resolution["missing_disposition"] not in DISPOSITIONS:
                _fail(f"{location}.missing_disposition", "unsupported disposition")
            _expect_text(resolution["strategy_derived_from"], f"{location}.strategy_derived_from")
            _expect_text(
                resolution["disposition_derived_from"],
                f"{location}.disposition_derived_from",
            )


def _validate_rules(
    pack: Mapping[str, Any],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
    concepts_by_id: Mapping[str, Mapping[str, Any]],
) -> None:
    raw_derivations = _expect_list(pack["derivations"], "policy.derivations")
    derivations_by_id: dict[str, Mapping[str, Any]] = {}
    for index, raw_derivation in enumerate(raw_derivations):
        location = f"policy.derivations[{index}]"
        derivation = _expect_mapping(raw_derivation, location)
        _expect_keys(derivation, _DERIVATION_KEYS, location)
        derivation_id = _expect_text(
            derivation["derivation_id"], f"{location}.derivation_id"
        )
        if derivation_id in derivations_by_id:
            _fail(f"{location}.derivation_id", f"duplicate derivation {derivation_id!r}")
        _expect_reviewed(derivation["review_status"], f"{location}.review_status")
        source_pointer = _expect_text(
            derivation["source_pointer"], f"{location}.source_pointer"
        )
        if not source_pointer.startswith("MERGED_PLAN#"):
            _fail(f"{location}.source_pointer", "must point into MERGED_PLAN")
        _expect_text(derivation["reasoning"], f"{location}.reasoning")
        derivations_by_id[derivation_id] = derivation

    rule_items = _expect_list(pack["rules"], "policy.rules")
    rules_by_id: dict[str, Mapping[str, Any]] = {}
    for index, raw_rule in enumerate(rule_items):
        rule = _expect_mapping(raw_rule, f"policy.rules[{index}]")
        rule_id = _expect_text(rule.get("rule_id"), f"policy.rules[{index}].rule_id")
        if rule_id in rules_by_id:
            _fail(f"policy.rules[{index}].rule_id", f"duplicate rule {rule_id!r}")
        rules_by_id[rule_id] = rule
    if not rules_by_id:
        _fail("policy.rules", "must not be empty")

    for index, rule in enumerate(rule_items):
        rule_id = rule["rule_id"]
        location = f"policy.rules[{index}]({rule_id})"
        keys = frozenset(rule)
        if not keys <= _RULE_KEYS:
            _fail(location, f"unknown properties {sorted(keys - _RULE_KEYS)}")
        mandatory = {
            "rule_id",
            "source_document",
            "title",
            "review_status",
            "effective_from",
            "effective_through",
            "severity",
            "evidence_basis",
            "selector",
            "coverage",
        }
        missing = mandatory - keys
        if missing:
            _fail(location, f"missing properties {sorted(missing)}")
        if ("constraint" in rule) == ("directive" in rule):
            _fail(location, "must define exactly one of constraint or directive")
        _expect_text(rule["title"], f"{location}.title")
        _expect_reviewed(rule["review_status"], f"{location}.review_status")
        document_id = _expect_text(rule["source_document"], f"{location}.source_document")
        if document_id not in sources_by_id:
            _fail(f"{location}.source_document", f"unknown source {document_id!r}")
        effective_from = _expect_date(rule["effective_from"], f"{location}.effective_from")
        effective_through = _expect_date(
            rule["effective_through"], f"{location}.effective_through", optional=True
        )
        if effective_through is not None and effective_through < effective_from:
            _fail(location, "effective_through precedes effective_from")
        if rule["severity"] not in SEVERITIES:
            _fail(f"{location}.severity", "unsupported severity")
        if rule["evidence_basis"] not in EVIDENCE_BASES:
            _fail(f"{location}.evidence_basis", "unsupported evidence basis")

        selector = _expect_mapping(rule["selector"], f"{location}.selector")
        if selector.get("entity") not in SELECTOR_ENTITIES:
            _fail(f"{location}.selector.entity", "unsupported selector entity")
        if _SELECTOR_IDENTIFIER_PATTERN.search(json.dumps(selector, sort_keys=True)):
            _fail(f"{location}.selector", "must not contain component or supplier identifiers")
        if "semantic_tags" in selector:
            for concept_id in _expect_list(
                selector["semantic_tags"], f"{location}.selector.semantic_tags"
            ):
                if concept_id not in concepts_by_id:
                    _fail(
                        f"{location}.selector.semantic_tags",
                        f"unknown concept {concept_id!r}",
                    )
        if selector.get("operator") is not None and selector["operator"] not in SELECTOR_OPERATORS:
            _fail(f"{location}.selector.operator", "unsupported selector operator")

        payload = rule.get("constraint", rule.get("directive"))
        payload_location = "constraint" if "constraint" in rule else "directive"
        payload = _expect_mapping(payload, f"{location}.{payload_location}")
        kind = payload.get("kind")
        allowed_kinds = CONSTRAINT_KINDS if payload_location == "constraint" else DIRECTIVE_KINDS
        if kind not in allowed_kinds:
            _fail(f"{location}.{payload_location}.kind", f"unsupported kind {kind!r}")
        _validate_source_named_entities(rule, location)

        coverage = _expect_mapping(rule["coverage"], f"{location}.coverage")
        required = _required_coverage(rule)
        actual_coverage = set(coverage)
        if actual_coverage != required:
            _fail(
                f"{location}.coverage",
                "must cover exactly every load-bearing value "
                f"(missing={sorted(required - actual_coverage)}, "
                f"extra={sorted(actual_coverage - required)})",
            )
        source_text = sources_by_id[document_id]["source_text"]
        for pointer, raw_proof in coverage.items():
            proof_location = f"{location}.coverage[{pointer!r}]"
            proof = _expect_mapping(raw_proof, proof_location)
            actual = _json_pointer(rule, pointer, proof_location)
            proof_keys = frozenset(proof)
            if proof_keys == _DIRECT_PROOF_KEYS:
                _validate_direct_proof(
                    proof,
                    actual=actual,
                    source_text=source_text,
                    location=proof_location,
                )
            elif proof_keys == _DERIVED_PROOF_KEYS:
                reference = _expect_text(proof["derived_from"], f"{proof_location}.derived_from")
                derived = _resolve_derived(
                    reference,
                    pack=pack,
                    concepts_by_id=concepts_by_id,
                    rules_by_id=rules_by_id,
                    sources_by_id=sources_by_id,
                    derivations_by_id=derivations_by_id,
                    location=f"{proof_location}.derived_from",
                )
                if actual != derived:
                    _fail(proof_location, f"derived pointer proves {derived!r}, not {actual!r}")
            else:
                _fail(
                    proof_location,
                    "must be exactly a direct covering span or a derived_from pointer",
                )

        if "directive" in rule and rule["directive"]["kind"] == "named_primary_supplier":
            supplier = _expect_mapping(
                rule["directive"].get("supplier"), f"{location}.directive.supplier"
            )
            subject = _expect_mapping(
                _expect_mapping(
                    rule.get("release_condition"), f"{location}.release_condition"
                ).get("subject"),
                f"{location}.release_condition.subject",
            )
            for entity_location, entity in (
                (f"{location}.directive.supplier", supplier),
                (f"{location}.release_condition.subject", subject),
            ):
                if frozenset(entity) != {"source_id", "legal_name"}:
                    _fail(entity_location, "must carry source_id and legal_name together")
                source_id = _expect_text(entity["source_id"], f"{entity_location}.source_id")
                _expect_text(entity["legal_name"], f"{entity_location}.legal_name")
                match = _SOURCE_ENTITY_PATTERN.fullmatch(source_id)
                if match is None or match.group("prefix") != "SUP":
                    _fail(f"{entity_location}.source_id", "must be a source-named identifier")
            disclosure = _expect_mapping(
                rule.get("risk_disclosure"), f"{location}.risk_disclosure"
            )
            if disclosure.get("kind") != "CAPACITY_UNKNOWN":
                _fail(f"{location}.risk_disclosure.kind", "must be CAPACITY_UNKNOWN")
            if disclosure.get("subject_from") != "release_condition.subject":
                _fail(
                    f"{location}.risk_disclosure.subject_from",
                    "capacity disclosure must resolve only from release_condition.subject",
                )
            if disclosure.get("disposition_effect") != "none":
                _fail(
                    f"{location}.risk_disclosure.disposition_effect",
                    "capacity uncertainty is non-dispositive",
                )
            release = rule["release_condition"]
            if release.get("resolution") != "affirmative_record_required":
                _fail(
                    f"{location}.release_condition.resolution",
                    "release must require an affirmative record",
                )
            forbidden_state_keys = {"status", "established", "confirmed", "current_state"}
            if forbidden_state_keys & set(release):
                _fail(
                    f"{location}.release_condition",
                    "compiled pack must not store runtime release state",
                )

    for basis, definition in pack["evidence_bases"].items():
        location = f"policy.evidence_bases.{basis}.derived_from"
        reference = definition["derived_from"]
        derived = _resolve_derived(
            reference,
            pack=pack,
            concepts_by_id=concepts_by_id,
            rules_by_id=rules_by_id,
            sources_by_id=sources_by_id,
            derivations_by_id=derivations_by_id,
            location=location,
        )
        if definition["resolution_strategy"] != derived:
            _fail(location, "does not derive the configured resolution strategy")
    for contract_name, contract in pack["contracts"].items():
        for basis in ("rolling_window", "external_system"):
            resolution = contract[basis]
            location = f"policy.contracts.{contract_name}.{basis}.derived_from"
            derived = _resolve_derived(
                resolution["derived_from"],
                pack=pack,
                concepts_by_id=concepts_by_id,
                rules_by_id=rules_by_id,
                sources_by_id=sources_by_id,
                derivations_by_id=derivations_by_id,
                location=location,
            )
            if resolution["disposition"] != derived:
                _fail(location, "does not derive the configured contract disposition")
        for rule_id, resolution in contract["rule_resolutions"].items():
            if rule_id not in rules_by_id:
                _fail(
                    f"policy.contracts.{contract_name}.rule_resolutions",
                    f"references unknown rule {rule_id!r}",
                )
            strategy_location = (
                f"policy.contracts.{contract_name}.rule_resolutions.{rule_id}.strategy_derived_from"
            )
            strategy = _resolve_derived(
                resolution["strategy_derived_from"],
                pack=pack,
                concepts_by_id=concepts_by_id,
                rules_by_id=rules_by_id,
                sources_by_id=sources_by_id,
                derivations_by_id=derivations_by_id,
                location=strategy_location,
            )
            if resolution["resolution_strategy"] != strategy:
                _fail(strategy_location, "does not derive the configured rule strategy")
            disposition_location = (
                f"policy.contracts.{contract_name}.rule_resolutions.{rule_id}.disposition_derived_from"
            )
            disposition = _resolve_derived(
                resolution["disposition_derived_from"],
                pack=pack,
                concepts_by_id=concepts_by_id,
                rules_by_id=rules_by_id,
                sources_by_id=sources_by_id,
                derivations_by_id=derivations_by_id,
                location=disposition_location,
            )
            if resolution["missing_disposition"] != disposition:
                _fail(disposition_location, "does not derive the configured rule disposition")

    magnet_ids = {
        "MEMO-2025-041.magnet_rolling_cap",
        "MEMO-2025-041.magnet_secondary_allocation",
        "MEMO-2025-041.magnet_named_primary",
    }
    if not magnet_ids <= set(rules_by_id):
        _fail("policy.rules", "magnet memo must compile into three separate reviewed rules")
    if rules_by_id["MEMO-2025-041.magnet_rolling_cap"]["evidence_basis"] != "rolling_window":
        _fail("policy.rules", "magnet rolling cap must use rolling_window evidence")
    if (
        rules_by_id["MEMO-2025-041.magnet_secondary_allocation"]["evidence_basis"]
        != "prospective_order"
    ):
        _fail("policy.rules", "magnet secondary allocation must use prospective_order evidence")


def validate_policy_documents(
    pack: Mapping[str, Any],
    concepts: Mapping[str, Any],
    *,
    project_root: Path | None,
) -> None:
    """Validate both artifacts, their hashes, provenance, and evidence coverage.

    Pass ``project_root=None`` only for a packaged deployment that intentionally
    lacks the original PDFs.  Repository and CI callers should always provide
    the root so the checked-in source-document hashes are reverified.
    """

    policy = _expect_mapping(pack, "policy")
    concept_document = _expect_mapping(concepts, "concepts")
    _expect_keys(policy, _ROOT_KEYS, "policy")
    if policy["schema_version"] != SCHEMA_VERSION:
        _fail("policy.schema_version", f"must be {SCHEMA_VERSION!r}")
    _expect_text(policy["pack_id"], "policy.pack_id")
    _expect_reviewed(policy["review_status"], "policy.review_status")
    _verify_content_hash(policy, "policy")
    compiler = _expect_mapping(policy["compiler"], "policy.compiler")
    _expect_keys(compiler, _COMPILER_KEYS, "policy.compiler")
    _expect_text(compiler["name"], "policy.compiler.name")
    _expect_text(compiler["version"], "policy.compiler.version")
    if compiler["llm_used"] is not False:
        _fail("policy.compiler.llm_used", "must be false for this deterministic compiled pack")

    concept_ref = _expect_mapping(policy["concepts"], "policy.concepts")
    _expect_keys(concept_ref, _CONCEPT_REF_KEYS, "policy.concepts")
    if concept_ref["path"] != "concepts.json":
        _fail("policy.concepts.path", "must be concepts.json beside the policy pack")
    if concept_ref["content_hash"] != concept_document.get("content_hash"):
        _fail("policy.concepts.content_hash", "does not match concepts.json")

    sources_by_id = _validate_source_documents(policy, project_root)
    concepts_by_id = _validate_concepts(concept_document, sources_by_id)
    _validate_evidence_contracts(policy)

    precedence = _expect_mapping(policy["precedence_model"], "policy.precedence_model")
    _expect_keys(precedence, frozenset({"steps", "conflict_result", "derived_from"}), "policy.precedence_model")
    steps = _expect_list(precedence["steps"], "policy.precedence_model.steps")
    if steps != [
        "effective_window",
        "explicit_supersedes",
        "narrower_selector",
        "later_equal_authority",
    ]:
        _fail("policy.precedence_model.steps", "must use the reviewed deterministic order")
    if precedence["conflict_result"] != "UNKNOWN_BLOCK_AND_ALERT":
        _fail("policy.precedence_model.conflict_result", "must fail closed on unresolved conflict")
    _expect_text(precedence["derived_from"], "policy.precedence_model.derived_from")

    _validate_rules(
        policy,
        sources_by_id=sources_by_id,
        concepts_by_id=concepts_by_id,
    )


__all__ = [
    "CONCEPTS_SCHEMA",
    "POLICY_PACK_SCHEMA",
    "PolicyValidationError",
    "compute_content_hash",
    "validate_policy_documents",
]
