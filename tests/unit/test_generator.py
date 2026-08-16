from __future__ import annotations

from contextlib import closing
from decimal import Decimal
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from tests.generator import (
    AllocationAxis,
    MalformedInput,
    SchemaPerturbation,
    TinySupplierSpace,
    assert_database_integrity,
    canonical_logical_rows,
    commercial_tie_scenario,
    database_checks,
    exhaustive_allocations,
    fractional_demand_scenario,
    generated_scenario,
    late_inbound_scenario,
    minimal_scenario,
    missing_suppliers_scenario,
    moq_conflict_scenario,
    multiple_deadlines_scenario,
    permute_database_rows,
    perturb_database_schema,
    rename_identifiers,
    scenario_builder_strategy,
    supplier_space_from_catalog,
    supplied_fixture_path,
    temporary_fixture_copies,
    write_malformed_database,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def table_count(path: Path, table: str) -> int:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]


class GeneratedDatabaseTests(unittest.TestCase):
    def test_minimal_database_and_independent_master_cardinalities_are_valid(self) -> None:
        shapes = (
            (0, 0, 0),
            (2, 5, 3),
            (4, 1, 0),
            (0, 3, 4),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            minimal_path = minimal_scenario().write(root / "minimal.sqlite")
            assert_database_integrity(minimal_path)

            for index, (products, components, suppliers) in enumerate(shapes):
                with self.subTest(shape=(products, components, suppliers)):
                    path = root / f"shape-{index}.sqlite"
                    generated_scenario(
                        100 + index,
                        product_count=products,
                        component_count=components,
                        supplier_count=suppliers,
                        schedule_count=0 if products == 0 else None,
                    ).write(path)
                    self.assertTrue(database_checks(path).ok)
                    self.assertEqual(table_count(path, "products"), products)
                    self.assertEqual(table_count(path, "components"), components)
                    self.assertEqual(table_count(path, "suppliers"), suppliers)

    def test_seeded_generation_is_logically_byte_stable_and_changes_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = generated_scenario(11).write(root / "first.sqlite")
            second = generated_scenario(11).write(root / "second.sqlite")
            different = generated_scenario(12).write(root / "different.sqlite")

            self.assertEqual(
                canonical_logical_rows(first), canonical_logical_rows(second)
            )
            self.assertNotEqual(
                canonical_logical_rows(first), canonical_logical_rows(different)
            )
            first_shape = tuple(
                table_count(first, table)
                for table in ("products", "components", "suppliers")
            )
            different_shape = tuple(
                table_count(different, table)
                for table in ("products", "components", "suppliers")
            )
            self.assertNotEqual(first_shape, different_shape)

    def test_row_permutation_preserves_logical_rows_but_changes_rowid_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = generated_scenario(
                23,
                product_count=3,
                component_count=4,
                supplier_count=5,
            ).write(root / "source.sqlite")
            permuted = permute_database_rows(source, root / "permuted.sqlite", seed=77)

            self.assertEqual(
                canonical_logical_rows(source), canonical_logical_rows(permuted)
            )
            with closing(sqlite3.connect(source)) as connection:
                source_order = [
                    row[0]
                    for row in connection.execute(
                        "SELECT supplier_id FROM suppliers ORDER BY rowid"
                    )
                ]
            with closing(sqlite3.connect(permuted)) as connection:
                permuted_order = [
                    row[0]
                    for row in connection.execute(
                        "SELECT supplier_id FROM suppliers ORDER BY rowid"
                    )
                ]
            self.assertNotEqual(source_order, permuted_order)
            assert_database_integrity(permuted)


class FixtureCopyAndMutationTests(unittest.TestCase):
    def test_all_six_fixture_copies_are_disposable_and_sources_are_unchanged(self) -> None:
        source_hashes = {
            number: hashlib.sha256(supplied_fixture_path(number).read_bytes()).hexdigest()
            for number in range(1, 7)
        }
        copied_paths: tuple[Path, ...]
        with temporary_fixture_copies() as copies:
            self.assertEqual(len(copies), 6)
            copied_paths = tuple(copies.values())
            for copied_path in copied_paths:
                self.assertTrue(copied_path.is_file())
                assert_database_integrity(copied_path)
            with closing(sqlite3.connect(copied_paths[0])) as connection:
                connection.execute("DELETE FROM scenario_config")
                connection.commit()

        self.assertTrue(all(not path.exists() for path in copied_paths))
        self.assertEqual(
            source_hashes,
            {
                number: hashlib.sha256(supplied_fixture_path(number).read_bytes()).hexdigest()
                for number in range(1, 7)
            },
        )

    def test_consistent_and_intentionally_inconsistent_renames_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = minimal_scenario().write(root / "source.sqlite")
            consistent = rename_identifiers(
                source,
                root / "consistent.sqlite",
                {
                    "product-a": "product-renamed",
                    "component-a": "component-renamed",
                    "supplier-a": "supplier-renamed",
                },
            )
            self.assertTrue(database_checks(consistent).ok)
            with closing(sqlite3.connect(consistent)) as connection:
                catalog_ids = connection.execute(
                    "SELECT component_id, supplier_id FROM supplier_catalog"
                ).fetchone()
                bom_ids = connection.execute(
                    "SELECT product_id, component_id FROM bom"
                ).fetchone()
            self.assertEqual(catalog_ids, ("component-renamed", "supplier-renamed"))
            self.assertEqual(bom_ids, ("product-renamed", "component-renamed"))

            inconsistent = rename_identifiers(
                source,
                root / "inconsistent.sqlite",
                {"supplier-a": "supplier-renamed"},
                mode="master_only",
            )
            checks = database_checks(inconsistent)
            self.assertTrue(checks.integrity_ok)
            self.assertTrue(checks.foreign_key_violations)

    def test_schema_perturbations_cover_extra_optional_and_required_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = minimal_scenario().write(root / "source.sqlite")
            changed = perturb_database_schema(
                source,
                root / "changed.sqlite",
                (
                    SchemaPerturbation.add_column(
                        "products", "future_metadata", "TEXT DEFAULT 'future'"
                    ),
                    SchemaPerturbation.drop_column("components", "description"),
                    SchemaPerturbation.rename_column(
                        "scenario_config", "current_date", "unexpected_date_name"
                    ),
                ),
            )
            self.assertTrue(database_checks(changed).integrity_ok)
            with closing(sqlite3.connect(changed)) as connection:
                product_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(products)")
                }
                component_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(components)")
                }
                config_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(scenario_config)")
                }
            self.assertIn("future_metadata", product_columns)
            self.assertNotIn("description", component_columns)
            self.assertNotIn("current_date", config_columns)


class EdgeCaseBuilderTests(unittest.TestCase):
    def test_requested_commercial_and_demand_shapes_are_present(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ties = commercial_tie_scenario().write(root / "ties.sqlite")
            moq = moq_conflict_scenario().write(root / "moq.sqlite")
            missing = missing_suppliers_scenario().write(root / "missing.sqlite")
            deadlines = multiple_deadlines_scenario().write(root / "deadlines.sqlite")
            fractional = fractional_demand_scenario().write(root / "fractional.sqlite")
            inbound = late_inbound_scenario().write(root / "inbound.sqlite")

            for path in (ties, moq, missing, deadlines, fractional, inbound):
                assert_database_integrity(path)

            with closing(sqlite3.connect(ties)) as connection:
                commercial_terms = list(
                    connection.execute(
                        "SELECT unit_price, lead_time_days, minimum_order_qty "
                        "FROM supplier_catalog ORDER BY supplier_id"
                    )
                )
            self.assertEqual(len(commercial_terms), 2)
            self.assertEqual(commercial_terms[0], commercial_terms[1])

            with closing(sqlite3.connect(moq)) as connection:
                demand = connection.execute(
                    "SELECT quantity FROM production_schedule"
                ).fetchone()[0]
                minimums = [
                    row[0]
                    for row in connection.execute(
                        "SELECT minimum_order_qty FROM supplier_catalog"
                    )
                ]
            self.assertTrue(all(minimum > demand for minimum in minimums))
            self.assertEqual(table_count(missing, "suppliers"), 0)
            self.assertEqual(table_count(missing, "supplier_catalog"), 0)

            with closing(sqlite3.connect(deadlines)) as connection:
                due_dates = {
                    row[0]
                    for row in connection.execute(
                        "SELECT materials_needed_by FROM production_schedule"
                    )
                }
            self.assertGreater(len(due_dates), 1)

            with closing(sqlite3.connect(fractional)) as connection:
                quantity_per = connection.execute(
                    "SELECT quantity_per FROM bom"
                ).fetchone()[0]
                production_quantity = connection.execute(
                    "SELECT quantity FROM production_schedule"
                ).fetchone()[0]
            self.assertEqual(
                Decimal(str(quantity_per)) * Decimal(str(production_quantity)),
                Decimal("0.75"),
            )

            with closing(sqlite3.connect(inbound)) as connection:
                delivery = connection.execute(
                    "SELECT expected_delivery_date FROM purchase_orders"
                ).fetchone()[0]
                deadline = connection.execute(
                    "SELECT materials_needed_by FROM production_schedule"
                ).fetchone()[0]
            self.assertGreater(delivery, deadline)

    def test_malformed_inputs_are_constructible_and_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {
                kind: write_malformed_database(root / f"{kind.value}.sqlite", kind)
                for kind in MalformedInput
            }
            self.assertEqual(paths[MalformedInput.ZERO_BYTE].stat().st_size, 0)

            for kind, path in paths.items():
                if kind is not MalformedInput.ZERO_BYTE:
                    with self.subTest(kind=kind):
                        self.assertTrue(database_checks(path).integrity_ok)

            with closing(
                sqlite3.connect(paths[MalformedInput.NULL_REQUIRED])
            ) as connection:
                self.assertIsNone(
                    connection.execute(
                        "SELECT quantity FROM production_schedule"
                    ).fetchone()[0]
                )
            self.assertTrue(
                database_checks(paths[MalformedInput.ORPHAN_REFERENCE]).foreign_key_violations
            )
            with closing(
                sqlite3.connect(paths[MalformedInput.INFINITE_QUANTITY])
            ) as connection:
                self.assertEqual(
                    connection.execute("SELECT quantity_per FROM bom").fetchone()[0],
                    float("inf"),
                )
            with closing(
                sqlite3.connect(paths[MalformedInput.MISSING_REQUIRED_COLUMN])
            ) as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(scenario_config)")
                }
            self.assertNotIn("current_date", columns)


class ExhaustiveSpaceTests(unittest.TestCase):
    def test_tiny_decimal_space_is_bounded_and_filterable(self) -> None:
        space = TinySupplierSpace(
            (
                AllocationAxis("supplier-a", Decimal("1.0"), Decimal("0.5")),
                AllocationAxis(
                    "supplier-b",
                    Decimal("4"),
                    Decimal("1"),
                    minimum_nonzero=Decimal("3"),
                ),
            )
        )
        allocations = list(exhaustive_allocations(space))
        self.assertEqual(space.cardinality, 9)
        self.assertEqual(len(allocations), 9)
        self.assertEqual(
            allocations[0],
            {"supplier-a": Decimal("0"), "supplier-b": Decimal("0")},
        )
        filtered = list(
            exhaustive_allocations(
                space,
                where=lambda allocation: sum(allocation.values()) == Decimal("4"),
            )
        )
        self.assertEqual(
            filtered,
            [
                {"supplier-a": Decimal("0"), "supplier-b": Decimal("4")},
                {"supplier-a": Decimal("1.0"), "supplier-b": Decimal("3")},
            ],
        )

    def test_catalog_adapter_uses_facts_without_filtering_suppliers(self) -> None:
        space = supplier_space_from_catalog(
            (
                {"supplier_id": "supplier-b", "minimum_order_qty": 2},
                {"supplier_id": "supplier-a", "minimum_order_qty": 3},
            ),
            upper_bound=Decimal("4"),
        )
        self.assertEqual(
            tuple(axis.supplier_id for axis in space.axes),
            ("supplier-a", "supplier-b"),
        )
        self.assertEqual(space.axes[0].values(), (Decimal("0"), Decimal("3"), Decimal("4")))
        self.assertEqual(
            space.axes[1].values(),
            (Decimal("0"), Decimal("2"), Decimal("3"), Decimal("4")),
        )


class ImportBoundaryTests(unittest.TestCase):
    def test_generator_imports_without_production_or_hypothesis_modules(self) -> None:
        script = (
            "import sys;"
            f"sys.path.insert(0, {str(PROJECT_ROOT)!r});"
            "import tests.generator;"
            "forbidden = [name for name in sys.modules "
            "if name == 'hypothesis' or name.startswith('apex_procurement') "
            "or name.endswith('.optimizer') or name.endswith('.validator')];"
            "assert forbidden == [], forbidden"
        )
        completed = subprocess.run(
            [sys.executable, "-I", "-c", script],
            cwd="/",
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_hypothesis_strategy_is_lazy_when_dependency_is_absent(self) -> None:
        try:
            strategy = scenario_builder_strategy(max_products=1, max_components=1, max_suppliers=1)
        except RuntimeError as error:
            self.assertIn("hypothesis", str(error))
        else:
            self.assertIsNotNone(strategy)


if __name__ == "__main__":
    unittest.main()
