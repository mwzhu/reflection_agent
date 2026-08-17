"""Deterministic temporary fixtures for the post-R07 mutation suite.

Every database starts as a copy of one of the six supplied scenarios.  The
builders accept their destination directory explicitly so tests own cleanup;
no mutated SQLite artifact is ever written under ``data/scenarios``.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import sqlite3

from apex_procurement.policy import compute_content_hash
from tests.generator import assert_database_integrity, supplied_fixture_path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
POLICY_DIRECTORY = PROJECT_ROOT / "src" / "apex_procurement" / "policy"
PACK_SOURCE = POLICY_DIRECTORY / "compiled_policy.json"
CONCEPTS_SOURCE = POLICY_DIRECTORY / "concepts.json"


@dataclass(frozen=True, slots=True)
class R08MutationFixture:
    """Paths and immutable source provenance for one disposable mutation."""

    name: str
    scenario_path: Path
    source_scenario_path: Path
    pack_path: Path | None = None
    concepts_path: Path | None = None


DatabaseMutation = Callable[[sqlite3.Connection], None]


def _database_fixture(
    destination_directory: str | Path,
    *,
    name: str,
    source_identifier: int | str,
    mutate: DatabaseMutation,
) -> R08MutationFixture:
    destination = Path(destination_directory)
    destination.mkdir(parents=True, exist_ok=True)
    source = supplied_fixture_path(source_identifier)
    scenario = destination / f"{name}.sqlite"
    if scenario.exists():
        raise FileExistsError(scenario)
    shutil.copy2(source, scenario)
    with closing(sqlite3.connect(scenario)) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        mutate(connection)
        connection.commit()
    assert_database_integrity(scenario)
    return R08MutationFixture(name, scenario, source)


def _rename_supplier(
    connection: sqlite3.Connection,
    *,
    old_id: str,
    new_id: str,
    new_name: str | None = None,
) -> None:
    if new_name is None:
        connection.execute(
            "UPDATE suppliers SET supplier_id = ? WHERE supplier_id = ?",
            (new_id, old_id),
        )
    else:
        connection.execute(
            "UPDATE suppliers SET supplier_id = ?, name = ? WHERE supplier_id = ?",
            (new_id, new_name, old_id),
        )
    for table in ("supplier_catalog", "purchase_orders"):
        connection.execute(
            f"UPDATE {table} SET supplier_id = ? WHERE supplier_id = ?",
            (new_id, old_id),
        )


def build_unknown_country_fixture(
    destination_directory: str | Path,
) -> R08MutationFixture:
    def mutate(connection: sqlite3.Connection) -> None:
        connection.execute(
            "UPDATE suppliers SET country = ? WHERE supplier_id = ?",
            ("Freedonia", "SUP-103"),
        )

    return _database_fixture(
        destination_directory,
        name="unknown-country",
        source_identifier=6,
        mutate=mutate,
    )


def build_renamed_magnet_fixture(
    destination_directory: str | Path,
) -> R08MutationFixture:
    def mutate(connection: sqlite3.Connection) -> None:
        connection.execute(
            "UPDATE components SET name = ? WHERE component_id = ?",
            ("High-Coercivity Sintered Puck, Grade 52H", "CMP-003"),
        )

    return _database_fixture(
        destination_directory,
        name="renamed-magnet",
        source_identifier=1,
        mutate=mutate,
    )


def build_replaced_magnet_suppliers_fixture(
    destination_directory: str | Path,
) -> R08MutationFixture:
    def mutate(connection: sqlite3.Connection) -> None:
        _rename_supplier(
            connection,
            old_id="SUP-107",
            new_id="SUP-207",
            new_name="Alloy Magnetics Group",
        )
        _rename_supplier(
            connection,
            old_id="SUP-108",
            new_id="SUP-208",
            new_name="Precision Field Materials",
        )

    return _database_fixture(
        destination_directory,
        name="replaced-magnet-suppliers",
        source_identifier=1,
        mutate=mutate,
    )


def build_pre_memo_date_fixture(
    destination_directory: str | Path,
) -> R08MutationFixture:
    def mutate(connection: sqlite3.Connection) -> None:
        connection.execute(
            'UPDATE scenario_config SET "current_date" = ?',
            ("2025-04-14",),
        )

    return _database_fixture(
        destination_directory,
        name="pre-memo-date",
        source_identifier=1,
        mutate=mutate,
    )


def build_pre_policy_date_fixture(
    destination_directory: str | Path,
) -> R08MutationFixture:
    def mutate(connection: sqlite3.Connection) -> None:
        connection.execute(
            'UPDATE scenario_config SET "current_date" = ?',
            ("2025-01-14",),
        )

    return _database_fixture(
        destination_directory,
        name="pre-policy-date",
        source_identifier=1,
        mutate=mutate,
    )


def build_moq_25_net_need_5_fixture(
    destination_directory: str | Path,
) -> R08MutationFixture:
    """Isolate the supplied PCB offer whose MOQ is 25 against a net need of 5."""

    def mutate(connection: sqlite3.Connection) -> None:
        connection.execute(
            "DELETE FROM bom WHERE product_id = ? AND component_id <> ?",
            ("FG-1004", "CMP-005"),
        )
        connection.execute(
            "UPDATE inventory SET quantity_on_hand = ? WHERE component_id = ?",
            (5, "CMP-005"),
        )

    return _database_fixture(
        destination_directory,
        name="moq-25-net-need-5",
        source_identifier=6,
        mutate=mutate,
    )


def build_stale_named_supplier_id_fixture(
    destination_directory: str | Path,
) -> R08MutationFixture:
    def mutate(connection: sqlite3.Connection) -> None:
        _rename_supplier(
            connection,
            old_id="SUP-107",
            new_id="SUP-207",
        )

    return _database_fixture(
        destination_directory,
        name="stale-named-supplier-id",
        source_identifier=1,
        mutate=mutate,
    )


def build_unknown_uom_fixture(
    destination_directory: str | Path,
) -> R08MutationFixture:
    def mutate(connection: sqlite3.Connection) -> None:
        connection.execute(
            "UPDATE components SET unit_of_measure = ? WHERE component_id = ?",
            ("box", "CMP-011"),
        )

    return _database_fixture(
        destination_directory,
        name="unknown-uom",
        source_identifier=1,
        mutate=mutate,
    )


def build_unrendered_withholding_policy_fixture(
    destination_directory: str | Path,
) -> R08MutationFixture:
    """Declare external withholding for a known kind with no approval renderer.

    ``total_cost_of_ownership`` is already a reviewed, known constraint kind.
    The mutation changes only its evidence basis to the contract-mapped
    ``RECOMMEND_APPROVAL`` basis.  R13 will require policy-pack loading to
    reject that unrenderable declaration before planning starts.
    """

    fixture = _database_fixture(
        destination_directory,
        name="unrendered-withholding-policy",
        source_identifier=1,
        mutate=lambda _connection: None,
    )
    destination = Path(destination_directory)
    pack_path = destination / "compiled_policy-unrendered-withholding.json"
    concepts_path = destination / "concepts.json"
    if pack_path.exists() or concepts_path.exists():
        raise FileExistsError(pack_path if pack_path.exists() else concepts_path)

    pack = deepcopy(json.loads(PACK_SOURCE.read_text(encoding="utf-8")))
    rule = next(
        item
        for item in pack["rules"]
        if item["rule_id"] == "POL-PROC-001.section_7.total_cost"
    )
    derivation_id = "r08_unrendered_withholding_basis"
    pack["derivations"].append(
        {
            "derivation_id": derivation_id,
            "value": "external_system",
            "source_pointer": "MERGED_PLAN#R13/rendering-totality-mutation-test",
            "review_status": "approved",
            "reasoning": (
                "Synthetic reviewed mutation declares a withholding disposition "
                "for a known kind without adding a renderer."
            ),
        }
    )
    rule["evidence_basis"] = "external_system"
    rule["coverage"]["/evidence_basis"] = {
        "derived_from": f"derivation:{derivation_id}"
    }
    pack["content_hash"] = compute_content_hash(pack)
    pack_path.write_text(
        json.dumps(pack, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    shutil.copy2(CONCEPTS_SOURCE, concepts_path)
    return R08MutationFixture(
        fixture.name,
        fixture.scenario_path,
        fixture.source_scenario_path,
        pack_path,
        concepts_path,
    )


DATABASE_MUTATION_BUILDERS: tuple[
    Callable[[str | Path], R08MutationFixture], ...
] = (
    build_unknown_country_fixture,
    build_renamed_magnet_fixture,
    build_replaced_magnet_suppliers_fixture,
    build_pre_memo_date_fixture,
    build_pre_policy_date_fixture,
    build_moq_25_net_need_5_fixture,
    build_stale_named_supplier_id_fixture,
    build_unknown_uom_fixture,
)


__all__ = [
    "CONCEPTS_SOURCE",
    "DATABASE_MUTATION_BUILDERS",
    "PACK_SOURCE",
    "R08MutationFixture",
    "build_moq_25_net_need_5_fixture",
    "build_pre_memo_date_fixture",
    "build_pre_policy_date_fixture",
    "build_renamed_magnet_fixture",
    "build_replaced_magnet_suppliers_fixture",
    "build_stale_named_supplier_id_fixture",
    "build_unknown_country_fixture",
    "build_unknown_uom_fixture",
    "build_unrendered_withholding_policy_fixture",
]
