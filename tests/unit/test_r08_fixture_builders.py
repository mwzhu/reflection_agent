from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests.generator import logical_rows_digest
from tests.r08_mutation_fixtures import (
    CONCEPTS_SOURCE,
    DATABASE_MUTATION_BUILDERS,
    PACK_SOURCE,
    build_unrendered_withholding_policy_fixture,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = PROJECT_ROOT / "tests" / "r08_post_r07_baseline.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "builder",
    DATABASE_MUTATION_BUILDERS,
    ids=lambda builder: builder.__name__.removeprefix("build_").removesuffix("_fixture"),
)
def test_r08_database_fixture_builders_are_deterministic_and_source_safe(
    tmp_path: Path,
    builder,
) -> None:
    first = builder(tmp_path / "first")
    source_before = _sha256(first.source_scenario_path)
    second = builder(tmp_path / "second")

    assert first.scenario_path.is_relative_to(tmp_path)
    assert second.scenario_path.is_relative_to(tmp_path)
    assert logical_rows_digest(first.scenario_path) == logical_rows_digest(
        second.scenario_path
    )
    assert _sha256(first.source_scenario_path) == source_before


def test_r08_policy_fixture_is_deterministic_and_source_safe(tmp_path: Path) -> None:
    source_hashes = {
        path: _sha256(path)
        for path in (PACK_SOURCE, CONCEPTS_SOURCE)
    }

    first = build_unrendered_withholding_policy_fixture(tmp_path / "first")
    second = build_unrendered_withholding_policy_fixture(tmp_path / "second")

    assert first.pack_path is not None and second.pack_path is not None
    assert first.concepts_path is not None and second.concepts_path is not None
    assert logical_rows_digest(first.scenario_path) == logical_rows_digest(
        second.scenario_path
    )
    assert first.pack_path.read_bytes() == second.pack_path.read_bytes()
    assert first.concepts_path.read_bytes() == second.concepts_path.read_bytes()
    assert {_path: _sha256(_path) for _path in source_hashes} == source_hashes


def test_r08_baseline_evidence_is_complete_and_tied_to_later_packages() -> None:
    evidence = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    observations = evidence["observations"]

    assert evidence["schema_version"] == 1
    assert {item["case"] for item in observations} == {
        "unknown_country",
        "renamed_magnet",
        "replaced_magnet_suppliers",
        "pre_memo_date",
        "pre_policy_date",
        "moq_25_net_need_5",
        "stale_named_supplier_id",
        "unknown_uom",
        "unrendered_withholding_policy",
    }
    assert {item["owner_package"] for item in observations} == {
        "R09",
        "R10",
        "R11",
        "R12",
        "R13",
        "R14",
    }
    for observation in observations:
        target_tests = observation.get(
            "target_tests", (observation.get("target_test"),)
        )
        assert all(test and "test_r" in test for test in target_tests)
        for run in observation["runs"]:
            assert isinstance(run["exit_code"], int)
            assert isinstance(run["validation_issues"], dict)
            assert isinstance(run["affected_component_ids"], list)
            assert isinstance(run["po_rows"], list)
            assert isinstance(run["alert_categories"], dict)


def test_r08_baseline_source_hashes_still_match() -> None:
    evidence = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))

    assert {
        relative: _sha256(PROJECT_ROOT / relative)
        for relative in evidence["source_sha256"]
    } == evidence["source_sha256"]
