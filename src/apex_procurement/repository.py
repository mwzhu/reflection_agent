"""Read-only SQLite scenario repository.

All SQL identifiers in this module come from the fixed schema contract.  The
user controls only the database path; the connection is opened in read-only
and query-only modes and no fixture is ever migrated or repaired in place.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import sqlite3
from typing import Callable, TypeVar

from .domain import (
    BomLine,
    Component,
    ExistingAlert,
    ExistingPurchaseOrder,
    InventoryPosition,
    Product,
    ProductionOrder,
    ScenarioConfiguration,
    ScenarioSnapshot,
    Supplier,
    SupplierCatalogLine,
)
from .snapshot import build_snapshot


class ScenarioLoadError(ValueError):
    """Base class for deterministic scenario-loading failures."""


class ScenarioPathError(ScenarioLoadError):
    """The supplied scenario path is not a readable regular file."""


class ScenarioSchemaError(ScenarioLoadError):
    """The SQLite schema does not satisfy the fixed input contract."""


class ScenarioDataError(ScenarioLoadError):
    """A known scenario field or semantic reference is invalid."""


@dataclass(frozen=True, slots=True)
class _Column:
    alias: str
    source: str
    required: bool = True
    cast_to_text: bool = False


_TABLE_COLUMNS: dict[str, tuple[_Column, ...]] = {
    "scenario_config": (
        _Column("current_date", "current_date"),
        _Column("description", "scenario_description", required=False),
    ),
    "products": (
        _Column("product_id", "product_id"),
        _Column("name", "name"),
        _Column("description", "description", required=False),
        _Column("category", "category", required=False),
        _Column("unit_price", "unit_price", required=False, cast_to_text=True),
    ),
    "components": (
        _Column("component_id", "component_id"),
        _Column("name", "name"),
        _Column("description", "description", required=False),
        _Column("category", "category", required=False),
        _Column("unit_of_measure", "unit_of_measure"),
        _Column("is_hazardous", "is_hazardous"),
        _Column(
            "required_certifications",
            "requires_certification",
            required=False,
        ),
    ),
    "suppliers": (
        _Column("supplier_id", "supplier_id"),
        _Column("name", "name"),
        _Column("country", "country", required=False),
        _Column("is_domestic", "is_domestic", required=False),
        _Column("certifications", "certifications", required=False),
        _Column("sustainability_rating", "sustainability_rating", required=False),
        _Column("relationship_tier", "relationship_tier", required=False),
        _Column("on_approved_list", "on_approved_list", required=False),
        _Column("notes", "notes", required=False),
    ),
    "bom": (
        _Column("product_id", "product_id"),
        _Column("component_id", "component_id"),
        _Column("quantity_per", "quantity_per", cast_to_text=True),
    ),
    "supplier_catalog": (
        _Column("supplier_id", "supplier_id"),
        _Column("component_id", "component_id"),
        _Column("unit_price", "unit_price", cast_to_text=True),
        _Column("lead_time_days", "lead_time_days", cast_to_text=True),
        _Column(
            "minimum_order_quantity",
            "minimum_order_qty",
            cast_to_text=True,
        ),
        _Column("notes", "notes", required=False),
    ),
    "production_schedule": (
        _Column("order_id", "order_id"),
        _Column("product_id", "product_id"),
        _Column("quantity", "quantity", cast_to_text=True),
        _Column("customer", "customer", required=False),
        _Column("materials_needed_by", "materials_needed_by"),
    ),
    "inventory": (
        _Column("component_id", "component_id"),
        _Column("quantity_on_hand", "quantity_on_hand", cast_to_text=True),
        _Column("warehouse_location", "warehouse_location", required=False),
    ),
    "purchase_orders": (
        _Column("po_number", "po_number"),
        _Column("component_id", "component_id"),
        _Column("supplier_id", "supplier_id"),
        _Column("quantity", "quantity", cast_to_text=True),
        _Column("unit_price", "unit_price", required=False, cast_to_text=True),
        _Column("order_date", "order_date", required=False),
        _Column(
            "expected_delivery_date",
            "expected_delivery_date",
            required=False,
        ),
        _Column("rationale", "rationale", required=False),
    ),
    "alerts": (
        _Column("alert_id", "alert_id", cast_to_text=True),
        _Column("description", "description"),
    ),
}

_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_DECIMAL_PATTERN = re.compile(
    r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z",
    re.ASCII,
)
_IDENTIFIER_PATTERN = re.compile(r"[a-z_]+\Z")
_SQLITE_INTEGER_MIN = Decimal("-9223372036854775808")
_SQLITE_INTEGER_MAX = Decimal("9223372036854775807")
_ItemT = TypeVar("_ItemT")


def _quoted(identifier: str) -> str:
    if not _IDENTIFIER_PATTERN.fullmatch(identifier):
        raise AssertionError(f"unsafe internal SQL identifier: {identifier!r}")
    return f'"{identifier}"'


def _display(value: object) -> str:
    rendered = ascii(value)
    return rendered if len(rendered) <= 120 else f"{rendered[:117]}..."


def _row_location(table: str, key: tuple[tuple[str, object], ...]) -> str:
    if not key:
        return f"table '{table}' row"
    rendered = ", ".join(f"{name}={_display(value)}" for name, value in key)
    return f"table '{table}' logical key ({rendered})"


def _data_error(
    table: str,
    key: tuple[tuple[str, object], ...],
    column: str,
    detail: str,
) -> ScenarioDataError:
    return ScenarioDataError(
        f"scenario data error: {_row_location(table, key)} column '{column}' {detail}"
    )


def _required_text(
    value: object,
    table: str,
    key: tuple[tuple[str, object], ...],
    column: str,
) -> str:
    if value is None:
        raise _data_error(table, key, column, "must not be NULL")
    if not isinstance(value, str):
        raise _data_error(table, key, column, "must be text")
    if not value.strip():
        raise _data_error(table, key, column, "must be non-empty text")
    if any(ord(character) < 32 for character in value):
        raise _data_error(table, key, column, "must not contain control characters")
    return value


def _optional_text(
    value: object,
    table: str,
    key: tuple[tuple[str, object], ...],
    column: str,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _data_error(table, key, column, "must be text or NULL")
    if any(ord(character) < 32 for character in value):
        raise _data_error(table, key, column, "must not contain control characters")
    return value


def _decimal(
    value: object,
    table: str,
    key: tuple[tuple[str, object], ...],
    column: str,
    *,
    optional: bool = False,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal | None:
    if value is None:
        if optional:
            return None
        raise _data_error(table, key, column, "must not be NULL")
    if not isinstance(value, str) or not _DECIMAL_PATTERN.fullmatch(value):
        raise _data_error(table, key, column, "must be a finite decimal")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise _data_error(
            table,
            key,
            column,
            f"must be a finite decimal; got {_display(value)}",
        ) from error
    if not parsed.is_finite():
        raise _data_error(
            table,
            key,
            column,
            f"must be a finite decimal; got {_display(value)}",
        )
    if positive and parsed <= 0:
        raise _data_error(table, key, column, "must be positive")
    if nonnegative and parsed < 0:
        raise _data_error(table, key, column, "must be nonnegative")
    return parsed


def _integer(
    value: object,
    table: str,
    key: tuple[tuple[str, object], ...],
    column: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> int:
    parsed = _decimal(value, table, key, column)
    assert parsed is not None
    if parsed != parsed.to_integral_value():
        raise _data_error(table, key, column, "must be an integer")
    if not _SQLITE_INTEGER_MIN <= parsed <= _SQLITE_INTEGER_MAX:
        raise _data_error(table, key, column, "must fit a signed 64-bit integer")
    result = int(parsed)
    if positive and result <= 0:
        raise _data_error(table, key, column, "must be positive")
    if nonnegative and result < 0:
        raise _data_error(table, key, column, "must be nonnegative")
    return result


def _boolean(
    value: object,
    table: str,
    key: tuple[tuple[str, object], ...],
    column: str,
    *,
    optional: bool = False,
) -> bool | None:
    if value is None and optional:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value in ("0", "1"):
        return value == "1"
    if value is None:
        raise _data_error(table, key, column, "must not be NULL")
    raise _data_error(table, key, column, "must be 0 or 1")


def _date(
    value: object,
    table: str,
    key: tuple[tuple[str, object], ...],
    column: str,
    *,
    optional: bool = False,
) -> date | None:
    if value is None:
        if optional:
            return None
        raise _data_error(table, key, column, "must not be NULL")
    if not isinstance(value, str) or not _DATE_PATTERN.fullmatch(value):
        raise _data_error(
            table,
            key,
            column,
            f"must be an ISO date (YYYY-MM-DD); got {_display(value)}",
        )
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise _data_error(
            table,
            key,
            column,
            f"must be an ISO date (YYYY-MM-DD); got {_display(value)}",
        ) from error
    return parsed


def _text_list(
    value: object,
    table: str,
    key: tuple[tuple[str, object], ...],
    column: str,
) -> tuple[str, ...]:
    if value is None:
        return ()
    text = _optional_text(value, table, key, column)
    assert text is not None
    if not text.strip():
        return ()
    items = tuple(item.strip() for item in text.split(","))
    if any(not item for item in items):
        raise _data_error(table, key, column, "contains an empty comma-separated value")
    if len(items) != len(set(items)):
        raise _data_error(table, key, column, "contains duplicate values")
    return tuple(sorted(items))


def _raw_sort_key(row: dict[str, object]) -> tuple[tuple[str, str], ...]:
    return tuple((type(value).__name__, _display(value)) for value in row.values())


def _construct(
    constructor: Callable[..., _ItemT],
    table: str,
    key: tuple[tuple[str, object], ...],
    **values: object,
) -> _ItemT:
    try:
        return constructor(**values)
    except (TypeError, ValueError) as error:
        raise ScenarioDataError(
            f"scenario data error: {_row_location(table, key)} is invalid: {error}"
        ) from error


def _ensure_unique(
    table: str,
    items: tuple[_ItemT, ...],
    key_names: tuple[str, ...],
    key_function: Callable[[_ItemT], object],
) -> None:
    seen: set[object] = set()
    for item in sorted(items, key=lambda candidate: repr(key_function(candidate))):
        logical_key = key_function(item)
        if logical_key in seen:
            values = logical_key if isinstance(logical_key, tuple) else (logical_key,)
            rendered = ", ".join(
                f"{name}={_display(value)}"
                for name, value in zip(key_names, values, strict=True)
            )
            raise ScenarioDataError(
                f"scenario data error: table '{table}' has duplicate logical key ({rendered})"
            )
        seen.add(logical_key)


class SQLiteRepository:
    """Load a validated immutable snapshot from a SQLite database, read-only."""

    def load_snapshot(self, scenario_path: Path, /) -> ScenarioSnapshot:
        if not isinstance(scenario_path, Path):
            raise TypeError("scenario_path must be pathlib.Path")
        try:
            resolved_path = scenario_path.resolve(strict=True)
        except (FileNotFoundError, OSError) as error:
            raise ScenarioPathError(
                f"scenario path is not a readable file: {scenario_path}"
            ) from error
        if not resolved_path.is_file():
            raise ScenarioPathError(
                f"scenario path is not a readable file: {scenario_path}"
            )

        uri = f"{resolved_path.as_uri()}?mode=ro"
        try:
            with closing(sqlite3.connect(uri, uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                connection.enable_load_extension(False)
                connection.execute("PRAGMA query_only = ON")
                connection.execute("PRAGMA trusted_schema = OFF")
                connection.execute("BEGIN")
                available_columns = self._validate_schema(connection)
                snapshot = self._load(connection, available_columns)
                connection.rollback()
                return snapshot
        except ScenarioLoadError:
            raise
        except (sqlite3.Error, UnicodeError) as error:
            raise ScenarioLoadError("scenario database could not be read safely") from error

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> dict[str, frozenset[str]]:
        rows = connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = ? ORDER BY name",
            ("table",),
        ).fetchall()
        tables = {
            row["name"].casefold()
            for row in rows
            if isinstance(row["name"], str)
        }
        missing_tables = sorted(set(_TABLE_COLUMNS) - tables)
        if missing_tables:
            joined = ", ".join(missing_tables)
            raise ScenarioSchemaError(
                f"scenario schema error: missing required tables: {joined}"
            )

        available: dict[str, frozenset[str]] = {}
        for table in sorted(_TABLE_COLUMNS):
            column_rows = connection.execute(
                "SELECT name FROM pragma_table_info(?) ORDER BY cid",
                (table,),
            ).fetchall()
            columns = frozenset(
                row["name"].casefold()
                for row in column_rows
                if isinstance(row["name"], str)
            )
            required = {
                column.source.casefold()
                for column in _TABLE_COLUMNS[table]
                if column.required
            }
            missing_columns = sorted(required - columns)
            if missing_columns:
                joined = ", ".join(missing_columns)
                raise ScenarioSchemaError(
                    f"scenario schema error: table '{table}' missing required columns: {joined}"
                )
            available[table] = columns
        return available

    @staticmethod
    def _read_rows(
        connection: sqlite3.Connection,
        table: str,
        available_columns: frozenset[str],
    ) -> tuple[dict[str, object], ...]:
        expressions: list[str] = []
        for column in _TABLE_COLUMNS[table]:
            if column.source.casefold() not in available_columns:
                expression = "NULL"
            else:
                source = _quoted(column.source)
                expression = f"CAST({source} AS TEXT)" if column.cast_to_text else source
            expressions.append(f"{expression} AS {_quoted(column.alias)}")
        sql = f"SELECT {', '.join(expressions)} FROM {_quoted(table)}"
        fetched = connection.execute(sql).fetchall()
        rows = tuple(
            {
                column.alias: row[column.alias]
                for column in _TABLE_COLUMNS[table]
            }
            for row in fetched
        )
        return tuple(sorted(rows, key=_raw_sort_key))

    def _load(
        self,
        connection: sqlite3.Connection,
        available: dict[str, frozenset[str]],
    ) -> ScenarioSnapshot:
        rows_by_table = {
            table: self._read_rows(connection, table, available[table])
            for table in sorted(_TABLE_COLUMNS)
        }

        configuration = self._configuration(rows_by_table["scenario_config"])
        products = tuple(self._product(row) for row in rows_by_table["products"])
        components = tuple(self._component(row) for row in rows_by_table["components"])
        suppliers = tuple(self._supplier(row) for row in rows_by_table["suppliers"])
        bom_lines = tuple(self._bom_line(row) for row in rows_by_table["bom"])
        catalog_lines = tuple(
            self._catalog_line(row) for row in rows_by_table["supplier_catalog"]
        )
        production_orders = tuple(
            self._production_order(row) for row in rows_by_table["production_schedule"]
        )
        inventory = tuple(
            self._inventory_position(row) for row in rows_by_table["inventory"]
        )
        purchase_orders = tuple(
            self._purchase_order(row) for row in rows_by_table["purchase_orders"]
        )
        alerts = tuple(self._alert(row) for row in rows_by_table["alerts"])

        self._validate_unique_keys(
            products,
            components,
            suppliers,
            bom_lines,
            catalog_lines,
            production_orders,
            inventory,
            purchase_orders,
            alerts,
        )
        self._validate_references(
            products,
            components,
            suppliers,
            bom_lines,
            catalog_lines,
            production_orders,
            inventory,
            purchase_orders,
        )
        return build_snapshot(
            configuration=configuration,
            products=products,
            components=components,
            suppliers=suppliers,
            bom_lines=bom_lines,
            catalog_lines=catalog_lines,
            production_orders=production_orders,
            inventory=inventory,
            purchase_orders=purchase_orders,
            alerts=alerts,
        )

    @staticmethod
    def _configuration(rows: tuple[dict[str, object], ...]) -> ScenarioConfiguration:
        if len(rows) != 1:
            raise ScenarioDataError(
                "scenario data error: table 'scenario_config' must contain exactly one row; "
                f"found {len(rows)}"
            )
        row = rows[0]
        key: tuple[tuple[str, object], ...] = ()
        current_date = _date(row["current_date"], "scenario_config", key, "current_date")
        assert current_date is not None
        return _construct(
            ScenarioConfiguration,
            "scenario_config",
            key,
            current_date=current_date,
            description=_optional_text(
                row["description"], "scenario_config", key, "scenario_description"
            ),
        )

    @staticmethod
    def _product(row: dict[str, object]) -> Product:
        key = (("product_id", row["product_id"]),)
        product_id = _required_text(row["product_id"], "products", key, "product_id")
        return _construct(
            Product,
            "products",
            (("product_id", product_id),),
            product_id=product_id,
            name=_required_text(row["name"], "products", key, "name"),
            description=_optional_text(
                row["description"], "products", key, "description"
            ),
            category=_optional_text(row["category"], "products", key, "category"),
            unit_price=_decimal(
                row["unit_price"],
                "products",
                key,
                "unit_price",
                optional=True,
                nonnegative=True,
            ),
        )

    @staticmethod
    def _component(row: dict[str, object]) -> Component:
        key = (("component_id", row["component_id"]),)
        component_id = _required_text(
            row["component_id"], "components", key, "component_id"
        )
        return _construct(
            Component,
            "components",
            (("component_id", component_id),),
            component_id=component_id,
            name=_required_text(row["name"], "components", key, "name"),
            description=_optional_text(
                row["description"], "components", key, "description"
            ),
            category=_optional_text(row["category"], "components", key, "category"),
            unit_of_measure=_required_text(
                row["unit_of_measure"], "components", key, "unit_of_measure"
            ),
            is_hazardous=_boolean(
                row["is_hazardous"], "components", key, "is_hazardous"
            ),
            required_certifications=_text_list(
                row["required_certifications"],
                "components",
                key,
                "requires_certification",
            ),
        )

    @staticmethod
    def _supplier(row: dict[str, object]) -> Supplier:
        key = (("supplier_id", row["supplier_id"]),)
        supplier_id = _required_text(row["supplier_id"], "suppliers", key, "supplier_id")
        return _construct(
            Supplier,
            "suppliers",
            (("supplier_id", supplier_id),),
            supplier_id=supplier_id,
            name=_required_text(row["name"], "suppliers", key, "name"),
            country=_optional_text(row["country"], "suppliers", key, "country"),
            is_domestic=_boolean(
                row["is_domestic"],
                "suppliers",
                key,
                "is_domestic",
                optional=True,
            ),
            certifications=_text_list(
                row["certifications"], "suppliers", key, "certifications"
            ),
            sustainability_rating=_optional_text(
                row["sustainability_rating"],
                "suppliers",
                key,
                "sustainability_rating",
            ),
            relationship_tier=_optional_text(
                row["relationship_tier"], "suppliers", key, "relationship_tier"
            ),
            on_approved_list=_boolean(
                row["on_approved_list"],
                "suppliers",
                key,
                "on_approved_list",
                optional=True,
            ),
            notes=_optional_text(row["notes"], "suppliers", key, "notes"),
        )

    @staticmethod
    def _bom_line(row: dict[str, object]) -> BomLine:
        key = (
            ("product_id", row["product_id"]),
            ("component_id", row["component_id"]),
        )
        product_id = _required_text(row["product_id"], "bom", key, "product_id")
        component_id = _required_text(row["component_id"], "bom", key, "component_id")
        quantity = _decimal(
            row["quantity_per"], "bom", key, "quantity_per", positive=True
        )
        assert quantity is not None
        return _construct(
            BomLine,
            "bom",
            (("product_id", product_id), ("component_id", component_id)),
            product_id=product_id,
            component_id=component_id,
            quantity_per=quantity,
        )

    @staticmethod
    def _catalog_line(row: dict[str, object]) -> SupplierCatalogLine:
        key = (
            ("supplier_id", row["supplier_id"]),
            ("component_id", row["component_id"]),
        )
        supplier_id = _required_text(
            row["supplier_id"], "supplier_catalog", key, "supplier_id"
        )
        component_id = _required_text(
            row["component_id"], "supplier_catalog", key, "component_id"
        )
        unit_price = _decimal(
            row["unit_price"],
            "supplier_catalog",
            key,
            "unit_price",
            nonnegative=True,
        )
        minimum_order_quantity = _decimal(
            row["minimum_order_quantity"],
            "supplier_catalog",
            key,
            "minimum_order_qty",
            positive=True,
        )
        assert unit_price is not None and minimum_order_quantity is not None
        return _construct(
            SupplierCatalogLine,
            "supplier_catalog",
            (("supplier_id", supplier_id), ("component_id", component_id)),
            supplier_id=supplier_id,
            component_id=component_id,
            unit_price=unit_price,
            lead_time_days=_integer(
                row["lead_time_days"],
                "supplier_catalog",
                key,
                "lead_time_days",
                nonnegative=True,
            ),
            minimum_order_quantity=minimum_order_quantity,
            notes=_optional_text(row["notes"], "supplier_catalog", key, "notes"),
        )

    @staticmethod
    def _production_order(row: dict[str, object]) -> ProductionOrder:
        key = (("order_id", row["order_id"]),)
        order_id = _required_text(
            row["order_id"], "production_schedule", key, "order_id"
        )
        product_id = _required_text(
            row["product_id"], "production_schedule", key, "product_id"
        )
        quantity = _decimal(
            row["quantity"],
            "production_schedule",
            key,
            "quantity",
            positive=True,
        )
        needed_by = _date(
            row["materials_needed_by"],
            "production_schedule",
            key,
            "materials_needed_by",
        )
        assert quantity is not None and needed_by is not None
        return _construct(
            ProductionOrder,
            "production_schedule",
            (("order_id", order_id),),
            order_id=order_id,
            product_id=product_id,
            quantity=quantity,
            customer=_optional_text(
                row["customer"], "production_schedule", key, "customer"
            ),
            materials_needed_by=needed_by,
        )

    @staticmethod
    def _inventory_position(row: dict[str, object]) -> InventoryPosition:
        key = (("component_id", row["component_id"]),)
        component_id = _required_text(
            row["component_id"], "inventory", key, "component_id"
        )
        quantity = _decimal(
            row["quantity_on_hand"],
            "inventory",
            key,
            "quantity_on_hand",
            nonnegative=True,
        )
        assert quantity is not None
        return _construct(
            InventoryPosition,
            "inventory",
            (("component_id", component_id),),
            component_id=component_id,
            quantity_on_hand=quantity,
            warehouse_location=_optional_text(
                row["warehouse_location"], "inventory", key, "warehouse_location"
            ),
        )

    @staticmethod
    def _purchase_order(row: dict[str, object]) -> ExistingPurchaseOrder:
        key = (("po_number", row["po_number"]),)
        po_number = _required_text(
            row["po_number"], "purchase_orders", key, "po_number"
        )
        component_id = _required_text(
            row["component_id"], "purchase_orders", key, "component_id"
        )
        supplier_id = _required_text(
            row["supplier_id"], "purchase_orders", key, "supplier_id"
        )
        quantity = _decimal(
            row["quantity"],
            "purchase_orders",
            key,
            "quantity",
            positive=True,
        )
        assert quantity is not None
        return _construct(
            ExistingPurchaseOrder,
            "purchase_orders",
            (("po_number", po_number),),
            po_number=po_number,
            component_id=component_id,
            supplier_id=supplier_id,
            quantity=quantity,
            unit_price=_decimal(
                row["unit_price"],
                "purchase_orders",
                key,
                "unit_price",
                optional=True,
                nonnegative=True,
            ),
            order_date=_date(
                row["order_date"],
                "purchase_orders",
                key,
                "order_date",
                optional=True,
            ),
            expected_delivery_date=_date(
                row["expected_delivery_date"],
                "purchase_orders",
                key,
                "expected_delivery_date",
                optional=True,
            ),
            rationale=_optional_text(
                row["rationale"], "purchase_orders", key, "rationale"
            ),
        )

    @staticmethod
    def _alert(row: dict[str, object]) -> ExistingAlert:
        key = (("alert_id", row["alert_id"]),)
        alert_id = _integer(row["alert_id"], "alerts", key, "alert_id", positive=True)
        return _construct(
            ExistingAlert,
            "alerts",
            (("alert_id", alert_id),),
            alert_id=alert_id,
            description=_required_text(
                row["description"], "alerts", key, "description"
            ),
        )

    @staticmethod
    def _validate_unique_keys(
        products: tuple[Product, ...],
        components: tuple[Component, ...],
        suppliers: tuple[Supplier, ...],
        bom_lines: tuple[BomLine, ...],
        catalog_lines: tuple[SupplierCatalogLine, ...],
        production_orders: tuple[ProductionOrder, ...],
        inventory: tuple[InventoryPosition, ...],
        purchase_orders: tuple[ExistingPurchaseOrder, ...],
        alerts: tuple[ExistingAlert, ...],
    ) -> None:
        _ensure_unique("products", products, ("product_id",), lambda item: item.product_id)
        _ensure_unique(
            "components", components, ("component_id",), lambda item: item.component_id
        )
        _ensure_unique(
            "suppliers", suppliers, ("supplier_id",), lambda item: item.supplier_id
        )
        _ensure_unique(
            "bom",
            bom_lines,
            ("product_id", "component_id"),
            lambda item: (item.product_id, item.component_id),
        )
        _ensure_unique(
            "supplier_catalog",
            catalog_lines,
            ("supplier_id", "component_id"),
            lambda item: (item.supplier_id, item.component_id),
        )
        _ensure_unique(
            "production_schedule",
            production_orders,
            ("order_id",),
            lambda item: item.order_id,
        )
        _ensure_unique(
            "inventory", inventory, ("component_id",), lambda item: item.component_id
        )
        _ensure_unique(
            "purchase_orders",
            purchase_orders,
            ("po_number",),
            lambda item: item.po_number,
        )
        _ensure_unique("alerts", alerts, ("alert_id",), lambda item: item.alert_id)

    @staticmethod
    def _validate_references(
        products: tuple[Product, ...],
        components: tuple[Component, ...],
        suppliers: tuple[Supplier, ...],
        bom_lines: tuple[BomLine, ...],
        catalog_lines: tuple[SupplierCatalogLine, ...],
        production_orders: tuple[ProductionOrder, ...],
        inventory: tuple[InventoryPosition, ...],
        purchase_orders: tuple[ExistingPurchaseOrder, ...],
    ) -> None:
        product_ids = {item.product_id for item in products}
        component_ids = {item.component_id for item in components}
        supplier_ids = {item.supplier_id for item in suppliers}

        def require_reference(
            table: str,
            key: tuple[tuple[str, object], ...],
            column: str,
            value: str,
            target: str,
            known: set[str],
        ) -> None:
            if value not in known:
                raise ScenarioDataError(
                    f"scenario data error: {_row_location(table, key)} column '{column}' "
                    f"references missing {target} {_display(value)}"
                )

        for line in sorted(bom_lines, key=lambda item: (item.product_id, item.component_id)):
            key = (("product_id", line.product_id), ("component_id", line.component_id))
            require_reference(
                "bom", key, "product_id", line.product_id, "products.product_id", product_ids
            )
            require_reference(
                "bom",
                key,
                "component_id",
                line.component_id,
                "components.component_id",
                component_ids,
            )
        for order in sorted(production_orders, key=lambda item: item.order_id):
            require_reference(
                "production_schedule",
                (("order_id", order.order_id),),
                "product_id",
                order.product_id,
                "products.product_id",
                product_ids,
            )
        for position in sorted(inventory, key=lambda item: item.component_id):
            require_reference(
                "inventory",
                (("component_id", position.component_id),),
                "component_id",
                position.component_id,
                "components.component_id",
                component_ids,
            )
        for line in sorted(
            catalog_lines, key=lambda item: (item.supplier_id, item.component_id)
        ):
            key = (("supplier_id", line.supplier_id), ("component_id", line.component_id))
            require_reference(
                "supplier_catalog",
                key,
                "supplier_id",
                line.supplier_id,
                "suppliers.supplier_id",
                supplier_ids,
            )
            require_reference(
                "supplier_catalog",
                key,
                "component_id",
                line.component_id,
                "components.component_id",
                component_ids,
            )
        for order in sorted(purchase_orders, key=lambda item: item.po_number):
            key = (("po_number", order.po_number),)
            require_reference(
                "purchase_orders",
                key,
                "component_id",
                order.component_id,
                "components.component_id",
                component_ids,
            )
            require_reference(
                "purchase_orders",
                key,
                "supplier_id",
                order.supplier_id,
                "suppliers.supplier_id",
                supplier_ids,
            )


def load_snapshot(scenario_path: Path, /) -> ScenarioSnapshot:
    """Convenience entry point for loading one read-only SQLite scenario."""

    return SQLiteRepository().load_snapshot(scenario_path)


__all__ = [
    "SQLiteRepository",
    "ScenarioDataError",
    "ScenarioLoadError",
    "ScenarioPathError",
    "ScenarioSchemaError",
    "load_snapshot",
]
