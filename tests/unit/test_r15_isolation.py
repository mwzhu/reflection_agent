from __future__ import annotations

import unittest

from apex_procurement.domain import (
    InternalFailureExclusion,
    ValidationFailureScope,
    ValidationIssue,
    ValidationSeverity,
)
from apex_procurement.isolation import (
    COMPONENT_LOCAL_INTERNAL_VALIDATION_CODES,
    build_internal_failure_exclusions,
    exclusion_validation_invariant,
    is_containable_component_error,
    reviewed_validation_failure_scope,
)


class ReviewedIsolationScopeTests(unittest.TestCase):
    def issue(
        self,
        code: str,
        *,
        component_id: str | None = "component-generated",
        scope: ValidationFailureScope | None = None,
    ) -> ValidationIssue:
        return ValidationIssue(
            code=code,
            severity=ValidationSeverity.ERROR,
            message="Generated validator issue.",
            component_id=component_id,
            failure_scope=(
                reviewed_validation_failure_scope(code)
                if scope is None
                else scope
            ),
        )

    def test_allowlist_is_narrow_and_excludes_global_failure_families(self) -> None:
        self.assertIn(
            "RATIONALE_CITATION_MISSING",
            COMPONENT_LOCAL_INTERNAL_VALIDATION_CODES,
        )
        for global_code in (
            "SOURCE_DEMAND_MISMATCH",
            "DUPLICATE_ACTION",
            "SOLVER_UNPROVEN",
            "SOLVER_OBJECTIVE_DISAGREEMENT",
            "INDEPENDENT_SOLVE_UNPROVEN",
            "UNKNOWN_VALIDATION_CODE",
        ):
            with self.subTest(code=global_code):
                self.assertIs(
                    reviewed_validation_failure_scope(global_code),
                    ValidationFailureScope.GLOBAL,
                )
                self.assertFalse(is_containable_component_error(self.issue(global_code)))

    def test_component_id_without_reviewed_code_and_explicit_scope_never_contains(self) -> None:
        allowlisted = "RATIONALE_CITATION_MISSING"
        self.assertFalse(
            is_containable_component_error(
                self.issue(allowlisted, scope=ValidationFailureScope.UNKNOWN)
            )
        )
        self.assertFalse(
            is_containable_component_error(
                self.issue(
                    "UNKNOWN_VALIDATION_CODE",
                    scope=ValidationFailureScope.COMPONENT_LOCAL,
                )
            )
        )
        self.assertFalse(
            is_containable_component_error(
                self.issue(allowlisted, component_id=None)
            )
        )

    def test_exclusion_grouping_and_validation_digest_are_deterministic(self) -> None:
        issue = self.issue("RATIONALE_CITATION_MISSING")
        exclusions = build_internal_failure_exclusions(
            (issue,),
            {"component-generated": "requirement:component-generated"},
        )
        self.assertEqual(
            exclusions,
            (
                InternalFailureExclusion(
                    component_id="component-generated",
                    requirement_id="requirement:component-generated",
                    issues=(issue,),
                ),
            ),
        )
        first = exclusion_validation_invariant(exclusions)
        second = exclusion_validation_invariant(tuple(reversed(exclusions)))
        self.assertEqual(first, second)
        self.assertRegex(
            first,
            r"\Acomponent-internal-failure-exclusions-v1:[0-9a-f]{64}\Z",
        )


if __name__ == "__main__":
    unittest.main()
