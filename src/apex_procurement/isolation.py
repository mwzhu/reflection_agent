"""Reviewed validation-failure scope and exclusion binding for R15.

Containment is deliberately opt-in.  Every code not present in the narrow
component-local allowlist is global, even when a validator issue happens to
carry a component ID.  Structural, policy, accounting, solver-proof,
ownership, snapshot, and commit failures are intentionally absent.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from hashlib import sha256

from .domain import (
    InternalFailureExclusion,
    ValidationFailureScope,
    ValidationIssue,
    ValidationSeverity,
)
from .serialization import canonical_dumps


# These failures describe component-specific explanation or disclosure output
# that can be made safe by removing the complete component action/result set.
# Solver proof, requirement accounting, policy semantics, source structure,
# ownership, and commit codes are intentionally never added here.
COMPONENT_LOCAL_INTERNAL_VALIDATION_CODES = frozenset(
    {
        "APPROVAL_ALERT_MISSING",
        "CAPACITY_UNKNOWN_DISPOSITIVE",
        "CAPACITY_UNKNOWN_MISSING",
        "CAPACITY_UNKNOWN_UNSCOPED",
        "EVIDENCE_CONTRACT_ALERT_MISSING",
        "EVIDENCE_DECISION_ALERT_MISSING",
        "LATE_ARRIVAL_ALERT_MISSING",
        "LATE_ARRIVAL_ALERT_UNSCOPED",
        "NORMALIZATION_DISCLOSURE_MISMATCH",
        "RATIONALE_CITATION_MISSING",
        "RATIONALE_COMPARATOR_MISMATCH",
        "RATIONALE_MATERIAL_REJECTION_MISMATCH",
        "RATIONALE_MATERIAL_REJECTION_MISSING",
        "RATIONALE_QUANTITY_CALIBRATION_MISMATCH",
        "RATIONALE_SELECTED_ROUTE_MISMATCH",
        "SOURCE_ID_NORMALIZATION_ALERT_MISSING",
        "SOURCE_ID_NORMALIZATION_DISCLOSURE_MISSING",
        "SUPPLIER_ATTRIBUTE_DISCLOSURE_MISSING",
        "UNIT_NORMALIZATION_ALERT_MISSING",
        "UNIT_NORMALIZATION_DISCLOSURE_MISSING",
    }
)

_EXCLUSION_INVARIANT_PREFIX = "component-internal-failure-exclusions-v1:"


def reviewed_validation_failure_scope(code: str, /) -> ValidationFailureScope:
    """Return the reviewed scope; unknown codes fail closed as global."""

    if not isinstance(code, str) or not code:
        return ValidationFailureScope.GLOBAL
    if code in COMPONENT_LOCAL_INTERNAL_VALIDATION_CODES:
        return ValidationFailureScope.COMPONENT_LOCAL
    return ValidationFailureScope.GLOBAL


def is_containable_component_error(issue: ValidationIssue, /) -> bool:
    """Require code, explicit scope, severity, and component to agree."""

    if not isinstance(issue, ValidationIssue):
        return False
    return (
        issue.severity is ValidationSeverity.ERROR
        and issue.component_id is not None
        and issue.failure_scope is ValidationFailureScope.COMPONENT_LOCAL
        and reviewed_validation_failure_scope(issue.code)
        is ValidationFailureScope.COMPONENT_LOCAL
    )


def build_internal_failure_exclusions(
    issues: Iterable[ValidationIssue],
    requirement_ids_by_component: dict[str, str],
    /,
) -> tuple[InternalFailureExclusion, ...]:
    """Group a wholly containable first-pass failure set by component."""

    issue_tuple = tuple(issues)
    if not issue_tuple or not all(
        is_containable_component_error(item) for item in issue_tuple
    ):
        raise ValueError("every excluded validation issue must be reviewed component-local")
    grouped: dict[str, list[ValidationIssue]] = {}
    for issue in issue_tuple:
        assert issue.component_id is not None
        grouped.setdefault(issue.component_id, []).append(issue)
    exclusions: list[InternalFailureExclusion] = []
    for component_id, component_issues in sorted(grouped.items()):
        requirement_id = requirement_ids_by_component.get(component_id)
        if requirement_id is None:
            raise ValueError(
                "a component-local issue does not identify one planned requirement"
            )
        exclusions.append(
            InternalFailureExclusion(
                component_id=component_id,
                requirement_id=requirement_id,
                issues=tuple(component_issues),
            )
        )
    return tuple(exclusions)


def exclusion_validation_invariant(
    exclusions: Sequence[InternalFailureExclusion],
    /,
) -> str:
    """Bind the final validation result to the exact explicit exclusions."""

    exclusion_tuple = tuple(exclusions)
    if not exclusion_tuple or any(
        not isinstance(item, InternalFailureExclusion) for item in exclusion_tuple
    ):
        raise ValueError("a non-empty typed exclusion set is required")
    digest = sha256(canonical_dumps(exclusion_tuple).encode("utf-8")).hexdigest()
    return f"{_EXCLUSION_INVARIANT_PREFIX}{digest}"


__all__ = [
    "COMPONENT_LOCAL_INTERNAL_VALIDATION_CODES",
    "build_internal_failure_exclusions",
    "exclusion_validation_invariant",
    "is_containable_component_error",
    "reviewed_validation_failure_scope",
]
