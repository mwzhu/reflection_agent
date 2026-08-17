from __future__ import annotations

from pathlib import Path

import pytest

from apex_procurement.policy import PolicyValidationError, load_policy_registry
from tests.r08_mutation_fixtures import (
    build_unrendered_withholding_policy_fixture,
)


def test_r13_pack_load_rejects_known_unrendered_withholding_disposition(
    tmp_path: Path,
) -> None:
    fixture = build_unrendered_withholding_policy_fixture(tmp_path)
    assert fixture.pack_path is not None
    assert fixture.concepts_path is not None

    with pytest.raises(PolicyValidationError, match=r"render|renderer"):
        load_policy_registry(
            pack_path=fixture.pack_path,
            concepts_path=fixture.concepts_path,
            project_root=None,
        )
