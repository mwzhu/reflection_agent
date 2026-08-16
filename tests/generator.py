"""Reusable SQLite scenario builders for procurement tests.

This module deliberately contains data construction and generic enumeration only.  It
does not import the production package, and it does not decide whether an allocation
is eligible, compliant, feasible, or optimal.  Tests supply those predicates.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import sqlite3
import tempfile
from typing import Any, Literal


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPPLIED_SCENARIO_DIRECTORY = PROJECT_ROOT / "data" / "scenarios"
SUPPLIED_SCENARIO_NAMES = tuple(
    f"scenario_{number:02d}_{suffix}.sqlite"
    for number, suffix in (
        (1, "baseline"),
        (2, "partial_procurement"),
        (3, "tight_timeline"),
        (4, "low_inventory"),
        (5, "competing_demand"),
        (6, "simple"),
    )
)

TABLE_ORDER = (
    "scenario_config",
    "products",
    "components",
    "suppliers",
    "bom",
    "supplier_catalog",
    "inventory",
    "production_schedule",
    "purchase_orders",
    "alerts",
)

TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "scenario_config": ("current_date", "scenario_description"),
    "products": ("product_id", "name", "description", "category", "unit_price"),
    "components": (
        "component_id",
        "name",
        "description",
        "category",
        "unit_of_measure",
        "is_hazardous",
        "requires_certification",
    ),
    "suppliers": (
        "supplier_id",
        "name",
        "country",
        "is_domestic",
        "certifications",
        "sustainability_rating",
        "relationship_tier",
        "on_approved_list",
        "notes",
    ),
    "bom": ("product_id", "component_id", "quantity_per"),
    "supplier_catalog": (
        "supplier_id",
        "component_id",
        "unit_price",
        "lead_time_days",
        "minimum_order_qty",
        "notes",
    ),
    "inventory": ("component_id", "quantity_on_hand", "warehouse_location"),
    "production_schedule": (
        "order_id",
        "product_id",
        "quantity",
        "customer",
        "materials_needed_by",
    ),
    "purchase_orders": (
        "po_number",
        "component_id",
        "supplier_id",
        "quantity",
        "unit_price",
        "order_date",
        "expected_delivery_date",
        "rationale",
    ),
    "alerts": ("alert_id", "description"),
}

SQLITE_SCHEMA = """
CREATE TABLE scenario_config (
    current_date TEXT NOT NULL,
    scenario_description TEXT
);
CREATE TABLE products (
    product_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    category TEXT,
    unit_price REAL
);
CREATE TABLE components (
    component_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    category TEXT,
    unit_of_measure TEXT,
    is_hazardous INTEGER DEFAULT 0,
    requires_certification TEXT
);
CREATE TABLE suppliers (
    supplier_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    country TEXT,
    is_domestic INTEGER,
    certifications TEXT,
    sustainability_rating TEXT,
    relationship_tier TEXT,
    on_approved_list INTEGER DEFAULT 1,
    notes TEXT
);
CREATE TABLE bom (
    product_id TEXT NOT NULL,
    component_id TEXT NOT NULL,
    quantity_per REAL NOT NULL,
    PRIMARY KEY (product_id, component_id)
);
CREATE TABLE supplier_catalog (
    supplier_id TEXT NOT NULL,
    component_id TEXT NOT NULL,
    unit_price REAL NOT NULL,
    lead_time_days INTEGER NOT NULL,
    minimum_order_qty INTEGER DEFAULT 1,
    notes TEXT,
    PRIMARY KEY (supplier_id, component_id),
    FOREIGN KEY (supplier_id) REFERENCES suppliers(supplier_id),
    FOREIGN KEY (component_id) REFERENCES components(component_id)
);
CREATE TABLE inventory (
    component_id TEXT PRIMARY KEY,
    quantity_on_hand REAL NOT NULL,
    warehouse_location TEXT,
    FOREIGN KEY (component_id) REFERENCES components(component_id)
);
CREATE TABLE production_schedule (
    order_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    customer TEXT,
    materials_needed_by TEXT
);
CREATE TABLE purchase_orders (
    po_number TEXT PRIMARY KEY,
    component_id TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    quantity REAL NOT NULL,
    unit_price REAL,
    order_date TEXT,
    expected_delivery_date TEXT,
    rationale TEXT,
    FOREIGN KEY (component_id) REFERENCES components(component_id),
    FOREIGN KEY (supplier_id) REFERENCES suppliers(supplier_id)
);
CREATE TABLE alerts (
    alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL
);
"""

SQLiteScalar = None | int | float | str | bytes | Decimal
Row = dict[str, SQLiteScalar]
RowsByTable = dict[str, list[Row]]


def _empty_rows() -> RowsByTable:
    return {table: [] for table in TABLE_ORDER}


def _quoted(identifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise ValueError(f"unsafe SQLite identifier: {identifier!r}")
    return f'"{identifier}"'


def _sqlite_value(value: SQLiteScalar) -> None | int | float | str | bytes:
    if isinstance(value, Decimal):
        if not value.is_finite():
            return str(value)
        return format(value, "f")
    return value


def _canonical_value(value: object) -> object:
    if isinstance(value, bytes):
        return {"$bytes": value.hex()}
    if isinstance(value, float):
        if math.isnan(value):
            return {"$float": "nan"}
        if math.isinf(value):
            return {"$float": "inf" if value > 0 else "-inf"}
        return {"$float": value.hex()}
    return value


@dataclass(frozen=True)
class DatabaseChecks:
    """Results of SQLite's structural and referential checks."""

    integrity_messages: tuple[str, ...]
    foreign_key_violations: tuple[tuple[object, ...], ...]

    @property
    def integrity_ok(self) -> bool:
        return self.integrity_messages == ("ok",)

    @property
    def ok(self) -> bool:
        return self.integrity_ok and not self.foreign_key_violations


def database_checks(path: str | Path) -> DatabaseChecks:
    """Run SQLite integrity and foreign-key checks without modifying *path*."""

    database_path = Path(path)
    uri = f"file:{database_path.resolve().as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        integrity = tuple(row[0] for row in connection.execute("PRAGMA integrity_check"))
        foreign_keys = tuple(tuple(row) for row in connection.execute("PRAGMA foreign_key_check"))
    return DatabaseChecks(integrity, foreign_keys)


def assert_database_integrity(path: str | Path, *, check_foreign_keys: bool = True) -> None:
    checks = database_checks(path)
    if not checks.integrity_ok:
        raise AssertionError(f"SQLite integrity check failed: {checks.integrity_messages!r}")
    if check_foreign_keys and checks.foreign_key_violations:
        raise AssertionError(
            f"SQLite foreign-key check failed: {checks.foreign_key_violations!r}"
        )


def logical_rows(
    path: str | Path,
    *,
    tables: Iterable[str] | None = None,
) -> dict[str, tuple[tuple[object, ...], ...]]:
    """Return rows in a deterministic order, independent of physical insertion order."""

    with closing(sqlite3.connect(Path(path))) as connection:
        if tables is None:
            selected_tables = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            )
        else:
            selected_tables = tuple(sorted(tables))

        result: dict[str, tuple[tuple[object, ...], ...]] = {}
        for table in selected_tables:
            quoted_table = _quoted(table)
            rows = [
                tuple(_canonical_value(value) for value in row)
                for row in connection.execute(f"SELECT * FROM {quoted_table}")
            ]
            rows.sort(
                key=lambda row: json.dumps(
                    row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
            )
            result[table] = tuple(rows)
    return result


def canonical_logical_rows(
    path: str | Path,
    *,
    tables: Iterable[str] | None = None,
) -> bytes:
    """Serialize logical business rows to stable UTF-8 bytes."""

    payload = logical_rows(path, tables=tables)
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def logical_rows_digest(path: str | Path, *, tables: Iterable[str] | None = None) -> str:
    return hashlib.sha256(canonical_logical_rows(path, tables=tables)).hexdigest()


@dataclass
class ScenarioBuilder:
    """Mutable rows for one test scenario, with deterministic SQLite output."""

    rows: RowsByTable = field(default_factory=_empty_rows)

    def __post_init__(self) -> None:
        unknown_tables = set(self.rows) - set(TABLE_ORDER)
        if unknown_tables:
            raise ValueError(f"unknown scenario tables: {sorted(unknown_tables)!r}")
        for table in TABLE_ORDER:
            self.rows.setdefault(table, [])

    def clone(self) -> "ScenarioBuilder":
        return ScenarioBuilder(
            {
                table: [dict(row) for row in self.rows[table]]
                for table in TABLE_ORDER
            }
        )

    def add(self, table: str, /, **values: SQLiteScalar) -> "ScenarioBuilder":
        if table not in TABLE_COLUMNS:
            raise ValueError(f"unknown scenario table: {table!r}")
        unknown_columns = set(values) - set(TABLE_COLUMNS[table])
        if unknown_columns:
            raise ValueError(
                f"unknown columns for {table}: {sorted(unknown_columns)!r}"
            )
        self.rows[table].append(dict(values))
        return self

    def row_count(self, table: str) -> int:
        return len(self.rows[table])

    def write(
        self,
        path: str | Path,
        *,
        overwrite: bool = False,
        schema_sql: str = SQLITE_SCHEMA,
        check_foreign_keys: bool = True,
    ) -> Path:
        """Create a database atomically and return its path."""

        destination = Path(path)
        if destination.exists() and not overwrite:
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            with closing(sqlite3.connect(temporary_path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.executescript(schema_sql)
                for table in TABLE_ORDER:
                    for row in self.rows[table]:
                        if not row:
                            raise ValueError(f"empty row for table {table}")
                        ordered_columns = tuple(
                            column for column in TABLE_COLUMNS[table] if column in row
                        )
                        unknown_columns = set(row) - set(ordered_columns)
                        if unknown_columns:
                            raise ValueError(
                                f"unknown columns for {table}: {sorted(unknown_columns)!r}"
                            )
                        columns_sql = ", ".join(map(_quoted, ordered_columns))
                        placeholders = ", ".join("?" for _ in ordered_columns)
                        connection.execute(
                            f"INSERT INTO {_quoted(table)} ({columns_sql}) "
                            f"VALUES ({placeholders})",
                            tuple(_sqlite_value(row[column]) for column in ordered_columns),
                        )
                connection.commit()

            assert_database_integrity(
                temporary_path, check_foreign_keys=check_foreign_keys
            )
            os.replace(temporary_path, destination)
        finally:
            temporary_path.unlink(missing_ok=True)
        return destination


def minimal_scenario(*, current_date: str = "2030-01-02") -> ScenarioBuilder:
    """One product/component/supplier scenario with a single demand row."""

    builder = ScenarioBuilder()
    builder.add(
        "scenario_config",
        current_date=current_date,
        scenario_description="Generated minimal scenario",
    )
    builder.add(
        "products",
        product_id="product-a",
        name="Generated Product A",
        description="Minimal generated product",
        category="Generated",
        unit_price=Decimal("25.00"),
    )
    builder.add(
        "components",
        component_id="component-a",
        name="Generated Component A",
        description="Minimal generated component",
        category="Generated",
        unit_of_measure="each",
        is_hazardous=0,
        requires_certification=None,
    )
    builder.add(
        "suppliers",
        supplier_id="supplier-a",
        name="Generated Supplier A",
        country="USA",
        is_domestic=1,
        certifications="ISO-9001",
        sustainability_rating="A",
        relationship_tier="Standard",
        on_approved_list=1,
        notes=None,
    )
    builder.add(
        "bom",
        product_id="product-a",
        component_id="component-a",
        quantity_per=Decimal("1"),
    )
    builder.add(
        "supplier_catalog",
        supplier_id="supplier-a",
        component_id="component-a",
        unit_price=Decimal("2.50"),
        lead_time_days=3,
        minimum_order_qty=1,
        notes=None,
    )
    builder.add(
        "inventory",
        component_id="component-a",
        quantity_on_hand=Decimal("0"),
        warehouse_location="Generated Warehouse",
    )
    builder.add(
        "production_schedule",
        order_id="order-a",
        product_id="product-a",
        quantity=1,
        customer="Generated Customer",
        materials_needed_by="2030-01-15",
    )
    return builder


def generated_scenario(
    seed: int,
    *,
    product_count: int | None = None,
    component_count: int | None = None,
    supplier_count: int | None = None,
    schedule_count: int | None = None,
) -> ScenarioBuilder:
    """Generate a valid shape from a seed and independently chosen master counts."""

    if not isinstance(seed, int):
        raise TypeError("seed must be an int")
    seed_magnitude = abs(seed)
    product_count = 1 + seed_magnitude % 3 if product_count is None else product_count
    component_count = (
        1 + (seed_magnitude * 3 + 1) % 5
        if component_count is None
        else component_count
    )
    supplier_count = (
        (seed_magnitude * 5 + 2) % 5 if supplier_count is None else supplier_count
    )
    for name, count in (
        ("product_count", product_count),
        ("component_count", component_count),
        ("supplier_count", supplier_count),
    ):
        if not isinstance(count, int) or count < 0:
            raise ValueError(f"{name} must be a nonnegative integer")

    if schedule_count is None:
        schedule_count = product_count + (seed_magnitude % 2 if product_count else 0)
    if not isinstance(schedule_count, int) or schedule_count < 0:
        raise ValueError("schedule_count must be a nonnegative integer")
    if schedule_count and not product_count:
        raise ValueError("schedule_count must be zero when product_count is zero")

    rng = random.Random(seed)
    builder = ScenarioBuilder()
    planning_date = date(2031, 1, 1) + timedelta(days=seed_magnitude % 28)
    builder.add(
        "scenario_config",
        current_date=planning_date.isoformat(),
        scenario_description=f"Generated seed {seed}",
    )

    product_ids = [f"product-{index:03d}" for index in range(product_count)]
    component_ids = [f"component-{index:03d}" for index in range(component_count)]
    supplier_ids = [f"supplier-{index:03d}" for index in range(supplier_count)]

    for index, product_id in enumerate(product_ids):
        builder.add(
            "products",
            product_id=product_id,
            name=f"Generated Product {index}",
            description=f"Seeded product variant {rng.randrange(10_000)}",
            category=rng.choice(("Assembly", "Controller", "Sensor")),
            unit_price=Decimal(rng.randrange(1_000, 50_001)) / Decimal("100"),
        )

    unit_choices = ("each", "kg", "meter", "box")
    for index, component_id in enumerate(component_ids):
        builder.add(
            "components",
            component_id=component_id,
            name=f"Generated Component {index}",
            description=f"Seeded component variant {rng.randrange(10_000)}",
            category=rng.choice(("Raw Material", "Electrical", "Mechanical")),
            unit_of_measure=rng.choice(unit_choices),
            is_hazardous=rng.randrange(2),
            requires_certification=rng.choice((None, None, "ISO-9001", "UL")),
        )
        if rng.random() < 0.75:
            builder.add(
                "inventory",
                component_id=component_id,
                quantity_on_hand=rng.choice(
                    (Decimal("0"), Decimal("0.5"), Decimal("2"), Decimal("7.25"))
                ),
                warehouse_location=f"Warehouse {rng.randrange(1, 4)}",
            )

    countries = (("USA", 1), ("Canada", 0), ("Germany", 0), ("Freedonia", None))
    for index, supplier_id in enumerate(supplier_ids):
        country, is_domestic = rng.choice(countries)
        builder.add(
            "suppliers",
            supplier_id=supplier_id,
            name=f"Generated Supplier {index}",
            country=country,
            is_domestic=is_domestic,
            certifications=rng.choice((None, "ISO-9001", "ISO-9001,UL")),
            sustainability_rating=rng.choice((None, "A", "B", "C")),
            relationship_tier=rng.choice((None, "Strategic", "Preferred", "Standard")),
            on_approved_list=rng.randrange(2),
            notes=f"Seeded supplier variant {rng.randrange(10_000)}",
        )

    quantity_per_choices = (
        Decimal("0.25"),
        Decimal("0.5"),
        Decimal("1"),
        Decimal("1.5"),
        Decimal("2"),
    )
    for product_id in product_ids:
        if not component_ids:
            break
        relationship_count = rng.randint(1, len(component_ids))
        for component_id in rng.sample(component_ids, relationship_count):
            builder.add(
                "bom",
                product_id=product_id,
                component_id=component_id,
                quantity_per=rng.choice(quantity_per_choices),
            )

    for supplier_id in supplier_ids:
        for component_id in component_ids:
            if rng.random() < 0.65:
                builder.add(
                    "supplier_catalog",
                    supplier_id=supplier_id,
                    component_id=component_id,
                    unit_price=Decimal(rng.randrange(50, 10_001)) / Decimal("100"),
                    lead_time_days=rng.randrange(1, 46),
                    minimum_order_qty=rng.choice((1, 2, 5, 10)),
                    notes=None,
                )

    for index in range(schedule_count):
        due_date = planning_date + timedelta(days=5 + rng.randrange(50))
        builder.add(
            "production_schedule",
            order_id=f"order-{index:03d}",
            product_id=product_ids[index % len(product_ids)],
            quantity=rng.randrange(1, 21),
            customer=f"Generated Customer {rng.randrange(1, 8)}",
            materials_needed_by=due_date.isoformat(),
        )

    return builder


def commercial_tie_scenario() -> ScenarioBuilder:
    builder = minimal_scenario()
    builder.add(
        "suppliers",
        supplier_id="supplier-b",
        name="Generated Supplier B",
        country="USA",
        is_domestic=1,
        certifications="ISO-9001",
        sustainability_rating="A",
        relationship_tier="Standard",
        on_approved_list=1,
        notes=None,
    )
    builder.add(
        "supplier_catalog",
        supplier_id="supplier-b",
        component_id="component-a",
        unit_price=Decimal("2.50"),
        lead_time_days=3,
        minimum_order_qty=1,
        notes=None,
    )
    return builder


def moq_conflict_scenario() -> ScenarioBuilder:
    builder = commercial_tie_scenario()
    builder.rows["production_schedule"][0]["quantity"] = 5
    builder.rows["supplier_catalog"][0]["minimum_order_qty"] = 7
    builder.rows["supplier_catalog"][1]["minimum_order_qty"] = 6
    return builder


def missing_suppliers_scenario() -> ScenarioBuilder:
    builder = minimal_scenario()
    builder.rows["suppliers"].clear()
    builder.rows["supplier_catalog"].clear()
    return builder


def multiple_deadlines_scenario() -> ScenarioBuilder:
    builder = minimal_scenario()
    builder.rows["production_schedule"][0]["materials_needed_by"] = "2030-01-08"
    builder.add(
        "production_schedule",
        order_id="order-b",
        product_id="product-a",
        quantity=2,
        customer="Generated Customer B",
        materials_needed_by="2030-02-12",
    )
    return builder


def fractional_demand_scenario() -> ScenarioBuilder:
    builder = minimal_scenario()
    builder.rows["bom"][0]["quantity_per"] = Decimal("0.25")
    builder.rows["production_schedule"][0]["quantity"] = 3
    builder.rows["components"][0]["unit_of_measure"] = "kg"
    return builder


def late_inbound_scenario() -> ScenarioBuilder:
    builder = minimal_scenario()
    builder.rows["production_schedule"][0]["materials_needed_by"] = "2030-01-10"
    builder.add(
        "purchase_orders",
        po_number="inbound-a",
        component_id="component-a",
        supplier_id="supplier-a",
        quantity=Decimal("1"),
        unit_price=Decimal("2.50"),
        order_date="2030-01-02",
        expected_delivery_date="2030-01-12",
        rationale="Generated committed inbound",
    )
    return builder


EDGE_CASE_BUILDERS: dict[str, Callable[[], ScenarioBuilder]] = {
    "commercial_tie": commercial_tie_scenario,
    "moq_conflict": moq_conflict_scenario,
    "missing_suppliers": missing_suppliers_scenario,
    "multiple_deadlines": multiple_deadlines_scenario,
    "fractional_demand": fractional_demand_scenario,
    "late_inbound": late_inbound_scenario,
}


def edge_case_scenario(name: str) -> ScenarioBuilder:
    try:
        return EDGE_CASE_BUILDERS[name]()
    except KeyError as error:
        raise ValueError(
            f"unknown edge case {name!r}; expected one of {sorted(EDGE_CASE_BUILDERS)!r}"
        ) from error


def supplied_fixture_path(identifier: int | str) -> Path:
    """Resolve only one of the six immutable supplied scenario fixtures."""

    if isinstance(identifier, int):
        if not 1 <= identifier <= len(SUPPLIED_SCENARIO_NAMES):
            raise ValueError("fixture number must be between 1 and 6")
        name = SUPPLIED_SCENARIO_NAMES[identifier - 1]
    else:
        candidates = [
            name
            for name in SUPPLIED_SCENARIO_NAMES
            if identifier in {name, Path(name).stem}
        ]
        if len(candidates) != 1:
            raise ValueError(f"unknown supplied fixture: {identifier!r}")
        name = candidates[0]
    path = SUPPLIED_SCENARIO_DIRECTORY / name
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def copy_supplied_fixtures(
    destination_directory: str | Path,
    *,
    overwrite: bool = False,
) -> dict[str, Path]:
    destination = Path(destination_directory)
    destination.mkdir(parents=True, exist_ok=True)
    copies: dict[str, Path] = {}
    for name in SUPPLIED_SCENARIO_NAMES:
        source = supplied_fixture_path(name)
        target = destination / name
        if target.exists() and not overwrite:
            raise FileExistsError(target)
        shutil.copy2(source, target)
        copies[Path(name).stem] = target
    return copies


@contextmanager
def temporary_fixture_copies() -> Iterator[dict[str, Path]]:
    """Yield disposable copies of all supplied fixtures, then remove them."""

    with tempfile.TemporaryDirectory(prefix="apex-scenarios-") as directory:
        yield copy_supplied_fixtures(directory)


@contextmanager
def temporary_fixture_copy(identifier: int | str) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="apex-scenario-") as directory:
        source = supplied_fixture_path(identifier)
        target = Path(directory) / source.name
        shutil.copy2(source, target)
        yield target


def _atomic_mutated_copy(
    source: str | Path,
    destination: str | Path,
    mutation: Callable[[Path], None],
    *,
    overwrite: bool,
) -> Path:
    source_path = Path(source).resolve()
    destination_path = Path(destination)
    if destination_path.exists() and not overwrite:
        raise FileExistsError(destination_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.", suffix=".tmp", dir=destination_path.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        shutil.copy2(source_path, temporary_path)
        mutation(temporary_path)
        os.replace(temporary_path, destination_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return destination_path


def permute_database_rows(
    source: str | Path,
    destination: str | Path,
    seed: int,
    *,
    overwrite: bool = False,
) -> Path:
    """Copy a database and deterministically change table insertion order."""

    rng = random.Random(seed)

    def mutate(path: Path) -> None:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            tables = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            )
            for table in tables:
                quoted_table = _quoted(table)
                columns = tuple(
                    row[1]
                    for row in connection.execute(f"PRAGMA table_info({quoted_table})")
                )
                rows = list(connection.execute(f"SELECT * FROM {quoted_table} ORDER BY rowid"))
                if len(rows) < 2:
                    continue
                shuffled = list(rows)
                rng.shuffle(shuffled)
                if shuffled == rows:
                    shuffled = shuffled[1:] + shuffled[:1]
                connection.execute(f"DELETE FROM {quoted_table}")
                placeholders = ", ".join("?" for _ in columns)
                column_sql = ", ".join(map(_quoted, columns))
                connection.executemany(
                    f"INSERT INTO {quoted_table} ({column_sql}) VALUES ({placeholders})",
                    shuffled,
                )
            connection.commit()
        assert_database_integrity(path)

    return _atomic_mutated_copy(
        source, destination, mutate, overwrite=overwrite
    )


class RenameMode(str, Enum):
    CONSISTENT = "consistent"
    MASTER_ONLY = "master_only"
    REFERENCES_ONLY = "references_only"


_MASTER_ID_LOCATIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "product_id": (("products", "product_id"),),
    "component_id": (("components", "component_id"),),
    "supplier_id": (("suppliers", "supplier_id"),),
    "order_id": (("production_schedule", "order_id"),),
    "po_number": (("purchase_orders", "po_number"),),
}
_REFERENCE_ID_LOCATIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "product_id": (
        ("bom", "product_id"),
        ("production_schedule", "product_id"),
    ),
    "component_id": (
        ("bom", "component_id"),
        ("supplier_catalog", "component_id"),
        ("inventory", "component_id"),
        ("purchase_orders", "component_id"),
    ),
    "supplier_id": (
        ("supplier_catalog", "supplier_id"),
        ("purchase_orders", "supplier_id"),
    ),
    "order_id": (),
    "po_number": (),
}


def rename_identifiers(
    source: str | Path,
    destination: str | Path,
    replacements: Mapping[str, str],
    *,
    mode: RenameMode | Literal["consistent", "master_only", "references_only"] = (
        RenameMode.CONSISTENT
    ),
    overwrite: bool = False,
) -> Path:
    """Rename IDs everywhere, or intentionally leave one side stale for tests."""

    rename_mode = RenameMode(mode)
    replacement_items = tuple(replacements.items())
    if any(not old or not new for old, new in replacement_items):
        raise ValueError("identifier replacements must be non-empty strings")
    if len({new for _, new in replacement_items}) != len(replacement_items):
        raise ValueError("replacement targets must be unique")

    def mutate(path: Path) -> None:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            schema_tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            locations: list[tuple[str, str]] = []
            for id_kind in _MASTER_ID_LOCATIONS:
                if rename_mode in {RenameMode.CONSISTENT, RenameMode.MASTER_ONLY}:
                    locations.extend(_MASTER_ID_LOCATIONS[id_kind])
                if rename_mode in {RenameMode.CONSISTENT, RenameMode.REFERENCES_ONLY}:
                    locations.extend(_REFERENCE_ID_LOCATIONS[id_kind])

            for location_index, (table, column) in enumerate(locations):
                if table not in schema_tables:
                    continue
                quoted_table = _quoted(table)
                quoted_column = _quoted(column)
                existing_columns = {
                    row[1]
                    for row in connection.execute(f"PRAGMA table_info({quoted_table})")
                }
                if column not in existing_columns:
                    continue
                placeholders: list[tuple[str, str]] = []
                for replacement_index, (old, new) in enumerate(replacement_items):
                    token = (
                        f"__apex_rename_{location_index}_{replacement_index}_"
                        f"{hashlib.sha256(old.encode()).hexdigest()[:12]}__"
                    )
                    collision = connection.execute(
                        f"SELECT 1 FROM {quoted_table} WHERE {quoted_column} = ? LIMIT 1",
                        (token,),
                    ).fetchone()
                    if collision:
                        raise ValueError(f"temporary rename token collision in {table}.{column}")
                    connection.execute(
                        f"UPDATE {quoted_table} SET {quoted_column} = ? "
                        f"WHERE {quoted_column} = ?",
                        (token, old),
                    )
                    placeholders.append((token, new))
                for token, new in placeholders:
                    connection.execute(
                        f"UPDATE {quoted_table} SET {quoted_column} = ? "
                        f"WHERE {quoted_column} = ?",
                        (new, token),
                    )
            connection.commit()

        assert_database_integrity(
            path, check_foreign_keys=rename_mode is RenameMode.CONSISTENT
        )

    return _atomic_mutated_copy(
        source, destination, mutate, overwrite=overwrite
    )


@dataclass(frozen=True)
class SchemaPerturbation:
    table: str
    operation: Literal["add_column", "drop_column", "rename_column"]
    column: str
    replacement: str | None = None

    @classmethod
    def add_column(
        cls, table: str, column: str, declaration: str = "TEXT"
    ) -> "SchemaPerturbation":
        return cls(table, "add_column", column, declaration)

    @classmethod
    def drop_column(cls, table: str, column: str) -> "SchemaPerturbation":
        return cls(table, "drop_column", column)

    @classmethod
    def rename_column(
        cls, table: str, column: str, replacement: str
    ) -> "SchemaPerturbation":
        return cls(table, "rename_column", column, replacement)


def perturb_database_schema(
    source: str | Path,
    destination: str | Path,
    perturbations: Iterable[SchemaPerturbation],
    *,
    overwrite: bool = False,
) -> Path:
    """Apply explicit extra/missing/renamed-column perturbations to a copy."""

    operations = tuple(perturbations)

    def mutate(path: Path) -> None:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            for operation in operations:
                table = _quoted(operation.table)
                column = _quoted(operation.column)
                if operation.operation == "add_column":
                    declaration = operation.replacement or "TEXT"
                    if ";" in declaration or "\x00" in declaration:
                        raise ValueError("column declaration must be a single SQL fragment")
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
                    )
                elif operation.operation == "drop_column":
                    connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
                elif operation.operation == "rename_column":
                    if operation.replacement is None:
                        raise ValueError("rename_column requires a replacement")
                    connection.execute(
                        f"ALTER TABLE {table} RENAME COLUMN {column} "
                        f"TO {_quoted(operation.replacement)}"
                    )
                else:  # pragma: no cover - dataclass accepts only the declared literals
                    raise ValueError(f"unknown schema operation: {operation.operation!r}")
            connection.commit()
        assert_database_integrity(path, check_foreign_keys=False)

    return _atomic_mutated_copy(
        source, destination, mutate, overwrite=overwrite
    )


class MalformedInput(str, Enum):
    ZERO_BYTE = "zero_byte"
    INVALID_CURRENT_DATE = "invalid_current_date"
    NON_NUMERIC_QUANTITY = "non_numeric_quantity"
    NON_FINITE_QUANTITY = "non_finite_quantity"
    INFINITE_QUANTITY = "infinite_quantity"
    NULL_REQUIRED = "null_required"
    ORPHAN_REFERENCE = "orphan_reference"
    MISSING_REQUIRED_COLUMN = "missing_required_column"


def write_malformed_database(
    path: str | Path,
    kind: MalformedInput | str,
    *,
    overwrite: bool = False,
) -> Path:
    """Write one deliberately malformed input without involving production code."""

    malformed_kind = MalformedInput(kind)
    destination = Path(path)
    if destination.exists() and not overwrite:
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if malformed_kind is MalformedInput.ZERO_BYTE:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        os.close(descriptor)
        os.replace(temporary_name, destination)
        return destination

    builder = minimal_scenario()
    if malformed_kind is MalformedInput.NULL_REQUIRED:
        relaxed_schema = SQLITE_SCHEMA.replace(
            "quantity INTEGER NOT NULL,\n    customer TEXT",
            "quantity INTEGER,\n    customer TEXT",
        )
        if relaxed_schema == SQLITE_SCHEMA:
            raise AssertionError("failed to construct relaxed malformed schema")
        builder.rows["production_schedule"][0]["quantity"] = None
        return builder.write(
            destination,
            overwrite=overwrite,
            schema_sql=relaxed_schema,
        )

    builder.write(destination, overwrite=overwrite)
    if malformed_kind is MalformedInput.MISSING_REQUIRED_COLUMN:
        temporary = destination.with_name(f"{destination.name}.schema-perturbed")
        try:
            perturb_database_schema(
                destination,
                temporary,
                [
                    SchemaPerturbation.rename_column(
                        "scenario_config", "current_date", "renamed_current_date"
                    )
                ],
            )
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    with closing(sqlite3.connect(destination)) as connection:
        if malformed_kind is MalformedInput.INVALID_CURRENT_DATE:
            connection.execute(
                'UPDATE scenario_config SET "current_date" = ?', ("not-a-date",)
            )
        elif malformed_kind is MalformedInput.NON_NUMERIC_QUANTITY:
            connection.execute(
                "UPDATE production_schedule SET quantity = ?", ("many",)
            )
        elif malformed_kind is MalformedInput.NON_FINITE_QUANTITY:
            connection.execute("UPDATE bom SET quantity_per = ?", ("NaN",))
        elif malformed_kind is MalformedInput.INFINITE_QUANTITY:
            connection.execute("UPDATE bom SET quantity_per = ?", (float("inf"),))
        elif malformed_kind is MalformedInput.ORPHAN_REFERENCE:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                "UPDATE supplier_catalog SET supplier_id = ?", ("supplier-missing",)
            )
        connection.commit()
    return destination


@dataclass(frozen=True)
class AllocationAxis:
    """One bounded quantity grid; zero and a nonzero range are explicit test data."""

    supplier_id: str
    upper_bound: Decimal
    step: Decimal = Decimal("1")
    minimum_nonzero: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        for name, value in (
            ("upper_bound", self.upper_bound),
            ("step", self.step),
            ("minimum_nonzero", self.minimum_nonzero),
        ):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise TypeError(f"{name} must be a finite Decimal")
        if not self.supplier_id:
            raise ValueError("supplier_id must be non-empty")
        if self.upper_bound < 0:
            raise ValueError("upper_bound must be nonnegative")
        if self.step <= 0:
            raise ValueError("step must be positive")
        if self.minimum_nonzero < 0:
            raise ValueError("minimum_nonzero must be nonnegative")

    def values(self) -> tuple[Decimal, ...]:
        values = [Decimal("0")]
        start = self.minimum_nonzero if self.minimum_nonzero > 0 else self.step
        current = start
        while current <= self.upper_bound:
            values.append(current)
            current += self.step
        return tuple(values)


@dataclass(frozen=True)
class TinySupplierSpace:
    """A deliberately small Cartesian allocation space for differential tests."""

    axes: tuple[AllocationAxis, ...]
    max_combinations: int = 100_000

    def __post_init__(self) -> None:
        if len({axis.supplier_id for axis in self.axes}) != len(self.axes):
            raise ValueError("supplier IDs must be unique within an allocation space")
        if self.max_combinations <= 0:
            raise ValueError("max_combinations must be positive")
        if self.cardinality > self.max_combinations:
            raise ValueError(
                f"allocation space has {self.cardinality} combinations; "
                f"limit is {self.max_combinations}"
            )

    @property
    def cardinality(self) -> int:
        result = 1
        for axis in self.axes:
            result *= len(axis.values())
        return result

    def quantity_tuples(self) -> Iterator[tuple[Decimal, ...]]:
        if not self.axes:
            yield ()
            return

        def visit(index: int, prefix: tuple[Decimal, ...]) -> Iterator[tuple[Decimal, ...]]:
            if index == len(self.axes):
                yield prefix
                return
            for value in self.axes[index].values():
                yield from visit(index + 1, prefix + (value,))

        yield from visit(0, ())

    def allocations(self) -> Iterator[dict[str, Decimal]]:
        supplier_ids = tuple(axis.supplier_id for axis in self.axes)
        for quantities in self.quantity_tuples():
            yield dict(zip(supplier_ids, quantities, strict=True))


def exhaustive_allocations(
    space: TinySupplierSpace,
    *,
    where: Callable[[Mapping[str, Decimal]], bool] | None = None,
) -> Iterator[dict[str, Decimal]]:
    """Enumerate a space, applying only a caller-provided predicate if supplied."""

    for allocation in space.allocations():
        if where is None or where(allocation):
            yield allocation


def supplier_space_from_catalog(
    catalog_rows: Iterable[Mapping[str, object]],
    *,
    upper_bound: Decimal | Mapping[str, Decimal],
    step: Decimal = Decimal("1"),
    max_combinations: int = 100_000,
) -> TinySupplierSpace:
    """Build a quantity grid from catalog facts, without applying eligibility rules."""

    axes: list[AllocationAxis] = []
    seen: set[str] = set()
    for row in sorted(catalog_rows, key=lambda item: str(item["supplier_id"])):
        supplier_id = str(row["supplier_id"])
        if supplier_id in seen:
            raise ValueError(f"duplicate catalog supplier: {supplier_id}")
        seen.add(supplier_id)
        maximum = (
            upper_bound[supplier_id]
            if isinstance(upper_bound, Mapping)
            else upper_bound
        )
        minimum = Decimal(str(row.get("minimum_order_qty", 0)))
        axes.append(
            AllocationAxis(
                supplier_id=supplier_id,
                upper_bound=maximum,
                step=step,
                minimum_nonzero=minimum,
            )
        )
    return TinySupplierSpace(tuple(axes), max_combinations=max_combinations)


def scenario_builder_strategy(
    *,
    max_products: int = 4,
    max_components: int = 6,
    max_suppliers: int = 4,
) -> Any:
    """Return a shrinkable Hypothesis strategy, importing Hypothesis lazily."""

    try:
        from hypothesis import strategies as st
    except ImportError as error:  # pragma: no cover - depends on the test environment
        raise RuntimeError(
            "scenario_builder_strategy requires the test-only 'hypothesis' package"
        ) from error
    for name, maximum in (
        ("max_products", max_products),
        ("max_components", max_components),
        ("max_suppliers", max_suppliers),
    ):
        if maximum < 0:
            raise ValueError(f"{name} must be nonnegative")
    return st.builds(
        generated_scenario,
        seed=st.integers(min_value=0, max_value=2**32 - 1),
        product_count=st.integers(min_value=0, max_value=max_products),
        component_count=st.integers(min_value=0, max_value=max_components),
        supplier_count=st.integers(min_value=0, max_value=max_suppliers),
        schedule_count=st.just(None),
    )


def allocation_strategy(space: TinySupplierSpace) -> Any:
    """Return a shrinkable Hypothesis strategy over the exact exhaustive grid."""

    try:
        from hypothesis import strategies as st
    except ImportError as error:  # pragma: no cover - depends on the test environment
        raise RuntimeError(
            "allocation_strategy requires the test-only 'hypothesis' package"
        ) from error
    supplier_ids = tuple(axis.supplier_id for axis in space.axes)
    quantities = st.tuples(*(st.sampled_from(axis.values()) for axis in space.axes))
    return quantities.map(
        lambda values: dict(zip(supplier_ids, values, strict=True))
    )


__all__ = [
    "AllocationAxis",
    "DatabaseChecks",
    "EDGE_CASE_BUILDERS",
    "MalformedInput",
    "RenameMode",
    "SQLITE_SCHEMA",
    "SUPPLIED_SCENARIO_NAMES",
    "ScenarioBuilder",
    "SchemaPerturbation",
    "TinySupplierSpace",
    "allocation_strategy",
    "assert_database_integrity",
    "canonical_logical_rows",
    "commercial_tie_scenario",
    "copy_supplied_fixtures",
    "database_checks",
    "edge_case_scenario",
    "exhaustive_allocations",
    "fractional_demand_scenario",
    "generated_scenario",
    "late_inbound_scenario",
    "logical_rows",
    "logical_rows_digest",
    "minimal_scenario",
    "missing_suppliers_scenario",
    "moq_conflict_scenario",
    "multiple_deadlines_scenario",
    "permute_database_rows",
    "perturb_database_schema",
    "rename_identifiers",
    "scenario_builder_strategy",
    "supplier_space_from_catalog",
    "supplied_fixture_path",
    "temporary_fixture_copies",
    "temporary_fixture_copy",
    "write_malformed_database",
]
