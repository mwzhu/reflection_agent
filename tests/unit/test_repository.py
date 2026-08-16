from __future__ import annotations

from contextlib import closing
from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from apex_procurement.repository import (
    SQLiteRepository,
    ScenarioDataError,
    ScenarioSchemaError,
)
from apex_procurement.snapshot import has_valid_state_digest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_ROOT = PROJECT_ROOT / "data" / "scenarios"
SCENARIOS = tuple(sorted(SCENARIO_ROOT.glob("*.sqlite")))


def file_digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


class RepositoryFixtureTests(unittest.TestCase):
    def test_all_scenarios_load_from_temporary_copies_with_verified_counts(self) -> None:
        expected = {
            "scenario_01_baseline.sqlite": (date(2025, 9, 1), 4, 0),
            "scenario_02_partial_procurement.sqlite": (date(2025, 9, 1), 4, 4),
            "scenario_03_tight_timeline.sqlite": (date(2025, 9, 1), 5, 0),
            "scenario_04_low_inventory.sqlite": (date(2025, 9, 1), 4, 0),
            "scenario_05_competing_demand.sqlite": (date(2025, 10, 5), 4, 0),
            "scenario_06_simple.sqlite": (date(2025, 9, 1), 1, 0),
        }
        before = {path: file_digest(path) for path in SCENARIOS}

        with tempfile.TemporaryDirectory() as directory:
            temp_root = Path(directory)
            for source in SCENARIOS:
                target = temp_root / source.name
                shutil.copy2(source, target)
                copied_digest = file_digest(target)
                snapshot = SQLiteRepository().load_snapshot(target)
                expected_date, schedule_count, purchase_order_count = expected[source.name]
                with self.subTest(scenario=source.name):
                    self.assertEqual(snapshot.configuration.current_date, expected_date)
                    self.assertEqual(len(snapshot.products), 4)
                    self.assertEqual(len(snapshot.components), 19)
                    self.assertEqual(len(snapshot.suppliers), 13)
                    self.assertEqual(len(snapshot.bom_lines), 41)
                    self.assertEqual(len(snapshot.catalog_lines), 44)
                    self.assertEqual(len(snapshot.inventory), 19)
                    self.assertEqual(len(snapshot.production_orders), schedule_count)
                    self.assertEqual(len(snapshot.purchase_orders), purchase_order_count)
                    self.assertEqual(len(snapshot.alerts), 0)
                    self.assertRegex(snapshot.state_digest, r"\A[0-9a-f]{64}\Z")
                    self.assertTrue(has_valid_state_digest(snapshot))
                    self.assertEqual(file_digest(target), copied_digest)

        self.assertEqual({path: file_digest(path) for path in SCENARIOS}, before)

    def test_existing_purchase_order_dates_are_loaded_as_stored_nullable_facts(self) -> None:
        source = SCENARIO_ROOT / "scenario_02_partial_procurement.sqlite"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / source.name
            shutil.copy2(source, target)
            snapshot = SQLiteRepository().load_snapshot(target)

        self.assertEqual(
            [item.expected_delivery_date for item in snapshot.purchase_orders],
            [
                date(2025, 9, 2),
                date(2025, 9, 1),
                date(2025, 9, 8),
                date(2025, 9, 2),
            ],
        )
        self.assertEqual(snapshot.purchase_orders[1].order_date, date(2025, 8, 18))

    def test_loaded_snapshot_is_immutable(self) -> None:
        source = SCENARIO_ROOT / "scenario_06_simple.sqlite"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / source.name
            shutil.copy2(source, target)
            snapshot = SQLiteRepository().load_snapshot(target)

        with self.assertRaises(FrozenInstanceError):
            snapshot.state_digest = "changed"  # type: ignore[misc]


class RepositoryValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        source = SCENARIO_ROOT / "scenario_01_baseline.sqlite"
        self.path = Path(self.temporary_directory.name) / "scenario.sqlite"
        shutil.copy2(source, self.path)

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(sql, parameters)

    def load(self):
        return SQLiteRepository().load_snapshot(self.path)

    def test_missing_required_table_has_deterministic_message(self) -> None:
        self.execute("DROP TABLE bom")
        with self.assertRaisesRegex(
            ScenarioSchemaError,
            r"\Ascenario schema error: missing required tables: bom\Z",
        ):
            self.load()

    def test_missing_required_column_has_deterministic_message(self) -> None:
        self.execute("ALTER TABLE production_schedule RENAME TO old_schedule")
        self.execute(
            "CREATE TABLE production_schedule "
            "(order_id TEXT, product_id TEXT, customer TEXT, materials_needed_by TEXT)"
        )
        with self.assertRaisesRegex(
            ScenarioSchemaError,
            r"\Ascenario schema error: table 'production_schedule' "
            r"missing required columns: quantity\Z",
        ):
            self.load()

    def test_invalid_scenario_date_names_the_table_column_and_value(self) -> None:
        self.execute(
            'UPDATE scenario_config SET "current_date" = ?',
            ("2025-02-30",),
        )
        with self.assertRaisesRegex(
            ScenarioDataError,
            r"table 'scenario_config' row column 'current_date' must be an ISO date "
            r"\(YYYY-MM-DD\); got '2025-02-30'",
        ):
            self.load()

    def test_schedule_date_rejects_non_iso_basic_format(self) -> None:
        self.execute(
            "UPDATE production_schedule SET materials_needed_by = ? "
            "WHERE order_id = (SELECT min(order_id) FROM production_schedule)",
            ("20250912",),
        )
        with self.assertRaisesRegex(
            ScenarioDataError,
            r"column 'materials_needed_by' must be an ISO date \(YYYY-MM-DD\)",
        ):
            self.load()

    def test_non_finite_numeric_values_fail_precisely(self) -> None:
        for invalid in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=invalid):
                with closing(sqlite3.connect(self.path)) as connection, connection:
                    connection.execute(
                        "UPDATE bom SET quantity_per = ? "
                        "WHERE rowid = (SELECT min(rowid) FROM bom)",
                        (invalid,),
                    )
                with self.assertRaisesRegex(
                    ScenarioDataError,
                    r"table 'bom'.*column 'quantity_per' must be a finite decimal",
                ):
                    self.load()

    def test_duplicate_logical_key_fails_independent_of_database_constraints(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("ALTER TABLE bom RENAME TO old_bom")
            connection.execute(
                "CREATE TABLE bom "
                "(product_id TEXT, component_id TEXT, quantity_per REAL)"
            )
            connection.execute(
                "INSERT INTO bom SELECT product_id, component_id, quantity_per FROM old_bom"
            )
            connection.execute(
                "INSERT INTO bom SELECT product_id, component_id, quantity_per "
                "FROM old_bom ORDER BY product_id, component_id LIMIT 1"
            )
        with self.assertRaisesRegex(
            ScenarioDataError,
            r"\Ascenario data error: table 'bom' has duplicate logical key "
            r"\(product_id='FG-1001', component_id='CMP-001'\)\Z",
        ):
            self.load()

    def test_dangling_bom_reference_fails_application_level_validation(self) -> None:
        self.execute(
            "UPDATE bom SET product_id = ? WHERE rowid = (SELECT min(rowid) FROM bom)",
            ("missing-product",),
        )
        with self.assertRaisesRegex(
            ScenarioDataError,
            r"table 'bom'.*column 'product_id' references missing products.product_id "
            r"'missing-product'",
        ):
            self.load()

    def test_dangling_schedule_reference_fails_application_level_validation(self) -> None:
        self.execute(
            "UPDATE production_schedule SET product_id = ? "
            "WHERE order_id = (SELECT min(order_id) FROM production_schedule)",
            ("missing-product",),
        )
        with self.assertRaisesRegex(
            ScenarioDataError,
            r"table 'production_schedule'.*column 'product_id' references missing "
            r"products.product_id 'missing-product'",
        ):
            self.load()

    def test_exact_decimal_text_never_passes_through_binary_float(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("ALTER TABLE bom RENAME TO old_bom")
            connection.execute(
                "CREATE TABLE bom "
                "(quantity_per TEXT, component_id TEXT, product_id TEXT, extra TEXT)"
            )
            connection.execute(
                "INSERT INTO bom (quantity_per, component_id, product_id) "
                "SELECT CAST(quantity_per AS TEXT), component_id, product_id FROM old_bom"
            )
            connection.execute(
                "UPDATE bom SET quantity_per = ? "
                "WHERE rowid = (SELECT min(rowid) FROM bom)",
                ("0.123456789012345678901",),
            )

        snapshot = self.load()
        self.assertEqual(
            snapshot.bom_lines[0].quantity_per,
            Decimal("0.123456789012345678901"),
        )

    def test_extra_and_reordered_columns_and_rows_do_not_change_snapshot_or_digest(self) -> None:
        original = self.load()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("ALTER TABLE bom RENAME TO old_bom")
            connection.execute(
                "CREATE TABLE bom "
                "(extra TEXT, quantity_per REAL, component_id TEXT, product_id TEXT)"
            )
            connection.execute(
                "INSERT INTO bom (extra, quantity_per, component_id, product_id) "
                "SELECT 'ignored', quantity_per, component_id, product_id "
                "FROM old_bom ORDER BY product_id DESC, component_id DESC"
            )
            connection.execute("DROP TABLE old_bom")
            connection.execute("ALTER TABLE products ADD COLUMN ignored INTEGER")
            connection.execute("UPDATE products SET ignored = rowid * 17")

        variant = self.load()
        self.assertEqual(variant, original)
        self.assertEqual(variant.state_digest, original.state_digest)

        with closing(sqlite3.connect(self.path, isolation_level=None)) as connection:
            connection.execute("VACUUM")
        repacked = self.load()
        self.assertEqual(repacked, variant)
        self.assertEqual(repacked.state_digest, variant.state_digest)

    def test_equivalent_decimal_spellings_have_the_same_digest(self) -> None:
        original = self.load()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("ALTER TABLE bom RENAME TO old_bom")
            connection.execute(
                "CREATE TABLE bom "
                "(product_id TEXT, component_id TEXT, quantity_per TEXT)"
            )
            connection.execute(
                "INSERT INTO bom (product_id, component_id, quantity_per) "
                "SELECT product_id, component_id, CAST(quantity_per AS TEXT) || '00' "
                "FROM old_bom ORDER BY component_id DESC, product_id DESC"
            )
            connection.execute("DROP TABLE old_bom")

        equivalent = self.load()
        self.assertEqual(equivalent, original)
        self.assertEqual(equivalent.state_digest, original.state_digest)

    def test_database_date_not_host_date_is_loaded(self) -> None:
        self.execute(
            'UPDATE scenario_config SET "current_date" = ?',
            ("2037-04-19",),
        )
        self.assertEqual(self.load().configuration.current_date, date(2037, 4, 19))

    def test_optional_columns_may_be_absent_and_are_loaded_as_null(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("ALTER TABLE products RENAME TO old_products")
            connection.execute("CREATE TABLE products (name TEXT, product_id TEXT)")
            connection.execute(
                "INSERT INTO products (name, product_id) "
                "SELECT name, product_id FROM old_products ORDER BY product_id DESC"
            )
            connection.execute("DROP TABLE old_products")

        snapshot = self.load()
        self.assertTrue(
            all(
                product.description is None
                and product.category is None
                and product.unit_price is None
                for product in snapshot.products
            )
        )


if __name__ == "__main__":
    unittest.main()
