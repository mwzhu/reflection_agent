"""Deterministic runtime entity resolution for compiled policy concepts.

The policy pack contains the vocabulary; this module contains only the
resolution mechanics.  Resolution is deliberately offline.  A future model
adapter may add evidence for residuals, but model output is never consulted by
the default evaluator and cannot turn an unresolved classification into an
asserted fact here.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
import re
import unicodedata
from typing import Any, Mapping, Sequence

from ..domain import AlertCategory, Component, EvidenceStatus, Supplier
from .registry import PolicyRegistry


_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def normalized_tokens(value: str) -> tuple[str, ...]:
    """Return the reviewed NFKC/casefold token representation of text."""

    if not isinstance(value, str):
        raise TypeError("value must be str")
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return tuple(_TOKEN_RE.findall(normalized))


def normalize_legal_name(value: str) -> str:
    """Normalize a legal name for exact (not fuzzy) name matching."""

    return " ".join(normalized_tokens(value))


def canonical_certification(value: str) -> str:
    """Canonicalize certification spelling without weakening comparison."""

    return "".join(normalized_tokens(value)).upper()


def _contains_phrase(haystack: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    if not phrase or len(phrase) > len(haystack):
        return False
    width = len(phrase)
    return any(haystack[index : index + width] == phrase for index in range(len(haystack) - width + 1))


@dataclass(frozen=True, slots=True)
class ResolutionAlert:
    category: AlertCategory
    code: str
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.category, AlertCategory):
            raise TypeError("category must be AlertCategory")
        if not self.code or not self.message:
            raise ValueError("resolution alerts require a code and message")


@dataclass(frozen=True, slots=True)
class ConceptResolution:
    concept_id: str
    entity_id: str
    status: EvidenceStatus
    method: str
    evidence: tuple[str, ...] = ()
    alerts: tuple[ResolutionAlert, ...] = ()
    assumption_codes: tuple[str, ...] = ()

    @property
    def is_member(self) -> bool:
        return self.status is EvidenceStatus.PASS


@dataclass(frozen=True, slots=True)
class NamedEntityResolution:
    status: EvidenceStatus
    source_id: str
    legal_name: str
    supplier: Supplier | None
    alerts: tuple[ResolutionAlert, ...]

    @property
    def resolved_supplier_id(self) -> str | None:
        return self.supplier.supplier_id if self.supplier is not None else None


class EntityResolver:
    """Resolve concepts and source-named suppliers against arbitrary entities."""

    def __init__(self, registry: PolicyRegistry) -> None:
        if not isinstance(registry, PolicyRegistry):
            raise TypeError("registry must be PolicyRegistry")
        self.registry = registry
        self._concepts: Mapping[str, Mapping[str, Any]] = MappingProxyType(
            {str(item["concept_id"]): item for item in registry.concepts["concepts"]}
        )
        self._country_aliases = self._build_country_aliases()
        self._aggregate_members = self._build_aggregate_members()

    def _build_country_aliases(self) -> Mapping[tuple[str, ...], str]:
        aliases: dict[tuple[str, ...], str] = {}
        for canonical, raw_aliases in self.registry.concepts["country_aliases"].items():
            for alias in raw_aliases:
                key = normalized_tokens(str(alias))
                previous = aliases.get(key)
                if previous is not None and previous != canonical:
                    raise ValueError(f"country alias {alias!r} maps to multiple countries")
                aliases[key] = str(canonical)
        return MappingProxyType(aliases)

    def _build_aggregate_members(self) -> Mapping[str, tuple[str, ...]]:
        """Derive enumerated aggregate membership from the reviewed pack."""

        result: dict[str, tuple[str, ...]] = {}
        for rule in self.registry.rules:
            constraint = rule.data.get("constraint")
            if not isinstance(constraint, Mapping) or constraint.get("kind") != "critical_component_categories":
                continue
            members = tuple(str(item) for item in constraint.get("categories", ()))
            candidates = [
                concept_id
                for concept_id, concept in self._concepts.items()
                if concept.get("resolution") == "enumerated_both_ways"
                and any(normalized_tokens(str(term)) == ("critical",) for term in concept.get("source_terms", ()))
            ]
            if len(candidates) == 1:
                result[candidates[0]] = members
        return MappingProxyType(result)

    def concept(self, concept_id: str) -> Mapping[str, Any]:
        try:
            return self._concepts[concept_id]
        except KeyError as error:
            raise KeyError(f"unknown policy concept {concept_id!r}") from error

    def resolve_concept(
        self,
        concept_id: str,
        entity: Component | Supplier | Mapping[str, object],
    ) -> ConceptResolution:
        concept = self.concept(concept_id)
        entity_kind = str(concept["entity_kind"])
        if entity_kind == "component":
            if not isinstance(entity, Component):
                raise TypeError(f"concept {concept_id!r} requires a Component")
            return self._resolve_component(concept_id, concept, entity)
        if entity_kind == "supplier":
            if not isinstance(entity, Supplier):
                raise TypeError(f"concept {concept_id!r} requires a Supplier")
            return self._resolve_supplier(concept_id, concept, entity)
        if entity_kind == "demand":
            if not isinstance(entity, Mapping):
                raise TypeError(f"concept {concept_id!r} requires demand facts")
            return self._resolve_demand(concept_id, concept, entity)
        raise ValueError(f"unsupported concept entity kind {entity_kind!r}")

    def _resolve_component(
        self,
        concept_id: str,
        concept: Mapping[str, Any],
        component: Component,
    ) -> ConceptResolution:
        aggregate = self._aggregate_members.get(concept_id)
        if aggregate is not None:
            child_results = tuple(self.resolve_concept(child, component) for child in aggregate)
            passed = tuple(item for item in child_results if item.status is EvidenceStatus.PASS)
            if passed:
                return ConceptResolution(
                    concept_id,
                    component.component_id,
                    EvidenceStatus.PASS,
                    "enumerated-member",
                    tuple(f"{item.concept_id}:{item.method}" for item in passed),
                )
            unknown = tuple(item for item in child_results if item.status is EvidenceStatus.UNKNOWN)
            aggregate_tokens = normalized_tokens(
                " ".join(filter(None, (component.name, component.category)))
            )
            inferred_children = tuple(
                child
                for child in aggregate
                if self._has_source_term_overlap(
                    aggregate_tokens, self._concepts[child].get("source_terms", ())
                )
            )
            if unknown or inferred_children:
                alerts = tuple(alert for item in unknown for alert in item.alerts)
                if not alerts:
                    alerts = (
                        ResolutionAlert(
                            AlertCategory.ASSUMPTION,
                            "INFERRED_CONCEPT_MEMBERSHIP",
                            f"{component.name!r} is only an inferred possible member of {concept_id!r}",
                        ),
                    )
                return ConceptResolution(
                    concept_id,
                    component.component_id,
                    EvidenceStatus.UNKNOWN,
                    "enumerated-both-ways",
                    tuple(
                        sorted(
                            {f"possible:{item.concept_id}" for item in unknown}
                            | {f"possible:{child}" for child in inferred_children}
                        )
                    ),
                    alerts,
                    ("INFERRED_CONCEPT_MEMBERSHIP",),
                )
            return ConceptResolution(
                concept_id,
                component.component_id,
                EvidenceStatus.FAIL,
                "closed-enumeration",
                ("no enumerated category matched",),
            )

        # Descriptions often state where a part is used (for example a coating
        # used on a board) rather than what the part is.  Treating those words
        # as identity creates exactly the false-positive class guarded by the
        # concept pack's negative fixtures.  Name and category are identity
        # fields; descriptions remain available to a future, disclosed model
        # tier but are not deterministic membership proof.
        fields = {
            "name": component.name,
            "category": component.category or "",
        }
        target_tokens = normalized_tokens(" ".join(fields.values()))
        negative = self._matching_phrase(target_tokens, concept.get("negative_fixtures", ()))
        if negative is not None:
            return ConceptResolution(
                concept_id,
                component.component_id,
                EvidenceStatus.FAIL,
                "negative-fixture",
                (negative,),
            )

        for signal in concept.get("structured_signals", ()):
            if signal == "is_hazardous":
                return ConceptResolution(
                    concept_id,
                    component.component_id,
                    EvidenceStatus.PASS if component.is_hazardous else EvidenceStatus.FAIL,
                    "structured:is_hazardous",
                    (f"is_hazardous={component.is_hazardous}",),
                )
            if signal == "requires_certification" and component.required_certifications:
                return ConceptResolution(
                    concept_id,
                    component.component_id,
                    EvidenceStatus.PASS,
                    "structured:requires_certification",
                    tuple(component.required_certifications),
                )
            if signal == "category" and component.category:
                match = self._matching_phrase(
                    normalized_tokens(component.category), concept.get("synonyms", ())
                )
                if match is not None:
                    return ConceptResolution(
                        concept_id,
                        component.component_id,
                        EvidenceStatus.PASS,
                        "structured:category",
                        (f"category={component.category}", f"matched={match}"),
                    )

        lexical = self._matching_phrase(target_tokens, concept.get("synonyms", ()))
        if lexical is not None:
            return ConceptResolution(
                concept_id,
                component.component_id,
                EvidenceStatus.PASS,
                "lexical",
                (lexical,),
            )

        strategy = str(concept["resolution"])
        if strategy == "enumerated_both_ways" and self._has_source_term_overlap(
            target_tokens, concept.get("source_terms", ())
        ):
            alert = ResolutionAlert(
                AlertCategory.ASSUMPTION,
                "INFERRED_CONCEPT_MEMBERSHIP",
                f"{component.name!r} is only an inferred possible member of {concept_id!r}",
            )
            return ConceptResolution(
                concept_id,
                component.component_id,
                EvidenceStatus.UNKNOWN,
                "enumerated-both-ways",
                ("source terminology overlaps without an exact deterministic match",),
                (alert,),
                ("INFERRED_CONCEPT_MEMBERSHIP",),
            )
        if strategy == "positive_evidence_required":
            alert = ResolutionAlert(
                AlertCategory.ASSUMPTION,
                "CONCEPT_NOT_ESTABLISHED",
                f"No positive evidence establishes {component.name!r} as {concept_id!r}",
            )
            return ConceptResolution(
                concept_id,
                component.component_id,
                EvidenceStatus.FAIL,
                "not-established",
                ("positive evidence required",),
                (alert,),
                ("CONCEPT_NOT_ESTABLISHED",),
            )
        return ConceptResolution(
            concept_id,
            component.component_id,
            EvidenceStatus.FAIL,
            "deterministic-no-match",
            ("no structured or lexical match",),
        )

    def _resolve_supplier(
        self,
        concept_id: str,
        concept: Mapping[str, Any],
        supplier: Supplier,
    ) -> ConceptResolution:
        signals = tuple(str(item) for item in concept.get("structured_signals", ()))
        if "country" in signals:
            return self._resolve_country_concept(concept_id, concept, supplier)
        if "relationship_tier" in signals:
            if supplier.relationship_tier is None:
                return self._supplier_unknown(concept_id, supplier, "relationship tier is absent")
            matched = self._matching_phrase(
                normalized_tokens(supplier.relationship_tier), concept.get("synonyms", ())
            )
            return ConceptResolution(
                concept_id,
                supplier.supplier_id,
                EvidenceStatus.PASS if matched else EvidenceStatus.FAIL,
                "structured:relationship_tier",
                (f"relationship_tier={supplier.relationship_tier}",),
                () if matched else (),
            )
        target = normalized_tokens(" ".join(filter(None, (supplier.name, supplier.notes))))
        match = self._matching_phrase(target, concept.get("synonyms", ()))
        return ConceptResolution(
            concept_id,
            supplier.supplier_id,
            EvidenceStatus.PASS if match else EvidenceStatus.FAIL,
            "lexical" if match else "deterministic-no-match",
            (match,) if match else ("no structured or lexical match",),
        )

    def _resolve_country_concept(
        self,
        concept_id: str,
        concept: Mapping[str, Any],
        supplier: Supplier,
    ) -> ConceptResolution:
        if supplier.country is None:
            return self._supplier_unknown(concept_id, supplier, "country is absent")
        canonical = self._country_aliases.get(normalized_tokens(supplier.country))
        if canonical is None:
            # The reviewed concept fixtures explicitly enumerate the known
            # international spellings.  They are policy-pack data, not a
            # Python country list.  Unknown countries remain UNKNOWN.
            known_international = set()
            for candidate in self._concepts.values():
                if candidate.get("entity_kind") != "supplier" or "country" not in candidate.get("structured_signals", ()):
                    continue
                if not any(
                    normalized_tokens(str(item)) in {("international",), ("non", "domestic")}
                    for item in candidate.get("synonyms", ())
                ):
                    continue
                for fixture in candidate.get("positive_fixtures", ()):
                    fixture_tokens = normalized_tokens(str(fixture))
                    if fixture_tokens[:1] == ("country",) and len(fixture_tokens) > 1:
                        known_international.add(fixture_tokens[1:])
            if normalized_tokens(supplier.country) in known_international:
                canonical = supplier.country
            else:
                return self._supplier_unknown(
                    concept_id, supplier, f"country {supplier.country!r} has no configured alias"
                )

        domestic_canonicals = frozenset(str(key) for key in self.registry.concepts["country_aliases"])
        is_domestic = canonical in domestic_canonicals
        describes_international = any(
            normalized_tokens(str(item)) in {("international",), ("non", "domestic")}
            for item in concept.get("synonyms", ())
        )
        member = not is_domestic if describes_international else is_domestic
        alerts: tuple[ResolutionAlert, ...] = ()
        if supplier.is_domestic is not None and supplier.is_domestic != is_domestic:
            alerts = (
                ResolutionAlert(
                    AlertCategory.DATA_QUALITY,
                    "DOMESTIC_FLAG_DISAGREEMENT",
                    f"Configured country aliases classify {supplier.country!r} as "
                    f"{'domestic' if is_domestic else 'international'}, while the convenience flag disagrees",
                ),
            )
        return ConceptResolution(
            concept_id,
            supplier.supplier_id,
            EvidenceStatus.PASS if member else EvidenceStatus.FAIL,
            "structured:country_alias",
            (f"country={supplier.country}", f"canonical_country={canonical}"),
            alerts,
        )

    def _supplier_unknown(
        self, concept_id: str, supplier: Supplier, reason: str
    ) -> ConceptResolution:
        return ConceptResolution(
            concept_id,
            supplier.supplier_id,
            EvidenceStatus.UNKNOWN,
            "structured:unresolved",
            (reason, "stored is_domestic is supporting evidence only"),
            (
                ResolutionAlert(
                    AlertCategory.DATA_QUALITY,
                    "SUPPLIER_ATTRIBUTE_UNKNOWN",
                    f"Cannot resolve {concept_id!r} for {supplier.name!r}: {reason}",
                ),
            ),
            ("SUPPLIER_ATTRIBUTE_UNKNOWN",),
        )

    def _resolve_demand(
        self,
        concept_id: str,
        concept: Mapping[str, Any],
        facts: Mapping[str, object],
    ) -> ConceptResolution:
        entity_id = str(facts.get("demand_id", "demand"))
        confirmed = facts.get("confirmed_production")
        if isinstance(confirmed, bool):
            return ConceptResolution(
                concept_id,
                entity_id,
                EvidenceStatus.PASS if confirmed else EvidenceStatus.FAIL,
                "structured:production_schedule",
                (f"confirmed_production={confirmed}",),
            )
        return ConceptResolution(
            concept_id,
            entity_id,
            EvidenceStatus.UNKNOWN,
            "structured:unresolved",
            ("confirmed production state is absent",),
            assumption_codes=("DEMAND_CLASSIFICATION_UNKNOWN",),
        )

    @staticmethod
    def _matching_phrase(tokens: tuple[str, ...], phrases: Sequence[object]) -> str | None:
        matches = []
        for raw_phrase in phrases:
            phrase = normalized_tokens(str(raw_phrase))
            if _contains_phrase(tokens, phrase):
                matches.append((len(phrase), str(raw_phrase)))
        if not matches:
            return None
        return max(matches, key=lambda item: (item[0], item[1].casefold()))[1]

    @staticmethod
    def _has_source_term_overlap(tokens: tuple[str, ...], terms: Sequence[object]) -> bool:
        target = frozenset(tokens)
        for raw_term in terms:
            raw_text = str(raw_term)
            term_tokens = normalized_tokens(raw_text)
            significant = tuple(token for token in term_tokens if len(token) > 2)
            if significant and set(significant).issubset(target):
                return True
            acronyms = {
                token.casefold()
                for token in re.findall(r"\b[A-Z][A-Z0-9]{1,}\b", raw_text)
            }
            if acronyms.intersection(target):
                return True
        return False

    def resolve_named_supplier(
        self,
        reference: Mapping[str, object],
        suppliers: Sequence[Supplier],
    ) -> NamedEntityResolution:
        """Apply the asymmetric ID/name ladder required for source references."""

        if set(reference) != {"source_id", "legal_name"}:
            raise ValueError("a source-named supplier requires source_id and legal_name")
        source_id = reference["source_id"]
        legal_name = reference["legal_name"]
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_id must be non-empty text")
        if not isinstance(legal_name, str) or not legal_name.strip():
            raise ValueError("legal_name must be non-empty text")
        if any(not isinstance(supplier, Supplier) for supplier in suppliers):
            raise TypeError("suppliers must contain Supplier values")

        id_matches = tuple(supplier for supplier in suppliers if supplier.supplier_id == source_id)
        wanted_name = normalize_legal_name(legal_name)
        name_matches = tuple(
            supplier for supplier in suppliers if normalize_legal_name(supplier.name) == wanted_name
        )

        if len(id_matches) == 1 and len(name_matches) == 1 and id_matches[0] is name_matches[0]:
            return NamedEntityResolution(
                EvidenceStatus.PASS, source_id, legal_name, id_matches[0], ()
            )
        if not id_matches and len(name_matches) == 1:
            alert = ResolutionAlert(
                AlertCategory.DATA_QUALITY,
                "STALE_SOURCE_ID",
                f"Source ID {source_id!r} is stale; resolved {legal_name!r} by one exact normalized legal-name match",
            )
            return NamedEntityResolution(
                EvidenceStatus.PASS, source_id, legal_name, name_matches[0], (alert,)
            )

        if len(id_matches) == 1 and len(name_matches) == 1 and id_matches[0] is not name_matches[0]:
            reason = "source ID and normalized legal name resolve to different supplier rows"
        elif len(name_matches) > 1:
            reason = "normalized legal name is ambiguous"
        elif len(id_matches) > 1:
            reason = "source ID is duplicated"
        elif len(id_matches) == 1:
            reason = "source ID may have been reused because its row does not match the legal name"
        else:
            reason = "neither source ID nor one exact normalized legal name resolves"
        alert = ResolutionAlert(
            AlertCategory.DECISION_REQUIRED,
            "SOURCE_NAMED_ENTITY_UNRESOLVED",
            f"Cannot safely resolve source-named supplier {legal_name!r} ({source_id!r}): {reason}",
        )
        return NamedEntityResolution(
            EvidenceStatus.UNKNOWN, source_id, legal_name, None, (alert,)
        )


__all__ = [
    "ConceptResolution",
    "EntityResolver",
    "NamedEntityResolution",
    "ResolutionAlert",
    "canonical_certification",
    "normalize_legal_name",
    "normalized_tokens",
]
