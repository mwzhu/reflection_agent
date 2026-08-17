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
from hashlib import sha256
from pathlib import Path
import re
import sqlite3
from typing import Callable, TypeVar
import unicodedata

from .domain import (
    BomLine,
    Component,
    ExistingAlert,
    ExistingPurchaseOrder,
    InventoryPosition,
    Product,
    ProductionOrder,
    RouteInputIssue,
    RouteQuarantineScope,
    ScenarioConfiguration,
    ScenarioSnapshot,
    Supplier,
    SupplierCatalogLine,
)
from .snapshot import build_snapshot
from .persistence import (
    ALERT_METADATA_COLUMNS,
    ALERT_METADATA_TABLE,
    DECISION_AUDIT_COLUMNS,
    DECISION_AUDIT_TABLE,
    PO_METADATA_COLUMNS,
    PO_METADATA_TABLE,
)


class RepositoryLoadError(ValueError):
    """Base class for repository failures that make planning unsafe."""


class ScenarioLoadError(RepositoryLoadError):
    """Base class for deterministic scenario-loading failures."""


class ScenarioPathError(ScenarioLoadError):
    """The supplied scenario path is not a readable regular file."""


class ScenarioSchemaError(ScenarioLoadError):
    """The SQLite schema does not satisfy the fixed input contract."""


class ScenarioDataError(ScenarioLoadError):
    """A known scenario field or semantic reference is invalid."""

    def __init__(
        self,
        message: str,
        *,
        table: str | None = None,
        key: tuple[tuple[str, object], ...] = (),
        column: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.table = table
        self.key = key
        self.column = column
        self.detail = detail


MAX_SCENARIO_BYTES = 256 * 1024 * 1024
MAX_TABLE_ROWS = 100_000
MAX_TOTAL_ROWS = 500_000
MAX_TEXT_BYTES = 256 * 1024


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
_RATING_PATTERN = re.compile(r"[A-F][+-]?\Z", re.ASCII)
_SQLITE_INTEGER_MIN = Decimal("-9223372036854775808")
_SQLITE_INTEGER_MAX = Decimal("9223372036854775807")
_DANGEROUS_FORMAT_CONTROLS = frozenset(
    {
        0x061C,
        0x200E,
        0x200F,
        0xFEFF,
        *range(0x202A, 0x202F),
        *range(0x2066, 0x206A),
    }
)
_ItemT = TypeVar("_ItemT")


def resolve_scenario_path(scenario_path: Path, /) -> Path:
    """Return one canonical regular-file path without following a final symlink.

    The final-component symlink check makes the path presented to the loader and
    writer unambiguous.  Parent directories are canonicalised so relative paths
    and platform aliases (for example macOS' ``/var``) remain usable.
    """

    if not isinstance(scenario_path, Path):
        raise TypeError("scenario_path must be pathlib.Path")
    try:
        if scenario_path.is_symlink():
            raise ScenarioPathError(
                f"scenario path must not be a symbolic link: {scenario_path}"
            )
        resolved_path = scenario_path.resolve(strict=True)
        status = resolved_path.stat(follow_symlinks=False)
    except ScenarioPathError:
        raise
    except (FileNotFoundError, OSError, ValueError) as error:
        raise ScenarioPathError(
            f"scenario path is not a readable file: {scenario_path}"
        ) from error
    if not resolved_path.is_file():
        raise ScenarioPathError(
            f"scenario path is not a readable file: {scenario_path}"
        )
    if status.st_size > MAX_SCENARIO_BYTES:
        raise ScenarioPathError(
            "scenario path exceeds the maximum supported database size "
            f"of {MAX_SCENARIO_BYTES} bytes"
        )
    return resolved_path


def _quoted(identifier: str) -> str:
    if not _IDENTIFIER_PATTERN.fullmatch(identifier):
        raise AssertionError(f"unsafe internal SQL identifier: {identifier!r}")
    return f'"{identifier}"'


def _display(value: object) -> str:
    rendered = ascii(value)
    return rendered if len(rendered) <= 120 else f"{rendered[:117]}..."


def _unsafe_unicode(value: str) -> bool:
    """Reject terminal controls, bidi controls, and malformed surrogate text."""

    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return True
    return any(
        unicodedata.category(character) in {"Cc", "Cs"}
        or ord(character) in _DANGEROUS_FORMAT_CONTROLS
        for character in value
    )


def _validate_text_size_and_unicode(
    value: str,
    table: str,
    key: tuple[tuple[str, object], ...],
    column: str,
) -> None:
    try:
        byte_length = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as error:
        raise _data_error(table, key, column, "contains malformed Unicode") from error
    if byte_length > MAX_TEXT_BYTES:
        raise _data_error(
            table,
            key,
            column,
            f"exceeds the maximum supported text size of {MAX_TEXT_BYTES} bytes",
        )
    if _unsafe_unicode(value):
        raise _data_error(table, key, column, "must not contain control characters")


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
        f"scenario data error: {_row_location(table, key)} column '{column}' {detail}",
        table=table,
        key=key,
        column=column,
        detail=detail,
    )


def _raw_value_fingerprint(value: object) -> tuple[str, str]:
    """Return SQLite storage-class provenance without retaining unsafe text."""

    if value is None:
        value_type, payload = "null", b""
    elif isinstance(value, str):
        value_type, payload = "text", value.encode("utf-8", errors="surrogatepass")
    elif isinstance(value, bool):
        value_type, payload = "integer", b"1" if value else b"0"
    elif isinstance(value, int):
        value_type, payload = "integer", str(value).encode("ascii")
    elif isinstance(value, float):
        value_type, payload = "real", repr(value).encode("ascii")
    elif isinstance(value, bytes):
        value_type, payload = "blob", value
    else:
        value_type = type(value).__name__
        payload = type(value).__qualname__.encode("utf-8")
    framed = value_type.encode("ascii", errors="backslashreplace") + b"\0" + payload
    return value_type, sha256(framed).hexdigest()


def _safe_route_reason(error: ScenarioDataError) -> tuple[str, str]:
    """Map parser diagnostics to fixed prose that never includes the raw value."""

    detail = error.detail or ""
    if "must not be NULL" in detail:
        return "MISSING_REQUIRED_VALUE", "the required value is null"
    if "finite decimal" in detail:
        return "INVALID_DECIMAL", "the value is not a finite decimal"
    if "must be an integer" in detail or "signed 64-bit integer" in detail:
        return "INVALID_INTEGER", "the value is not a supported whole number"
    if "must be 0 or 1" in detail:
        return "INVALID_BOOLEAN", "the value is not the required 0-or-1 boolean"
    if "rating grade" in detail:
        return "INVALID_RATING", "the value is not a supported rating grade"
    if "control characters" in detail or "malformed Unicode" in detail:
        return "UNSAFE_TEXT", "the value contains unsafe text characters"
    if "maximum supported text size" in detail:
        return "OVERSIZED_TEXT", "the value exceeds the supported text size"
    if "empty comma-separated value" in detail or "duplicate values" in detail:
        return "INVALID_CERTIFICATIONS", "the certification list is malformed"
    if "positive" in detail or "nonnegative" in detail:
        return "OUT_OF_RANGE", "the numeric value is outside its permitted range"
    if "text" in detail:
        return "INVALID_TEXT", "the value is not valid text"
    return "INVALID_ROUTE_VALUE", "the value does not satisfy the route input contract"


def _route_issue(
    error: ScenarioDataError,
    row: dict[str, object],
    *,
    supplier_id: str,
    component_id: str | None,
    affected_component_ids: tuple[str, ...],
) -> RouteInputIssue:
    if error.column is None or error.table not in {"suppliers", "supplier_catalog"}:
        raise error
    raw_alias = {
        "minimum_order_qty": "minimum_order_quantity",
    }.get(error.column, error.column)
    raw_type, raw_digest = _raw_value_fingerprint(row.get(raw_alias))
    reason_code, safe_reason = _safe_route_reason(error)
    supplier_wide = error.table == "suppliers"
    action = (
        "quarantined every catalog route for this supplier and excluded the supplier "
        "from candidate construction and optimization without substituting attributes"
        if supplier_wide
        else "quarantined only this supplier/component catalog offer and excluded it "
        "from candidate construction and optimization without substituting price, "
        "lead time, quantity, or dates"
    )
    remediation = (
        f"correct {error.table}.{error.column} for supplier_id {supplier_id} and rerun"
        if component_id is None
        else f"correct {error.table}.{error.column} for supplier_id {supplier_id} "
        f"and component_id {component_id}, then rerun"
    )
    return RouteInputIssue(
        source_table=error.table,
        supplier_id=supplier_id,
        component_id=component_id,
        affected_component_ids=affected_component_ids,
        field=error.column,
        reason_code=reason_code,
        safe_reason=safe_reason,
        blast_radius=(
            RouteQuarantineScope.SUPPLIER_ALL_ROUTES
            if supplier_wide
            else RouteQuarantineScope.CATALOG_OFFER
        ),
        action=action,
        remediation=remediation,
        raw_value_type=raw_type,
        raw_value_sha256=raw_digest,
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
    _validate_text_size_and_unicode(value, table, key, column)
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
    _validate_text_size_and_unicode(value, table, key, column)
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


def _rating_text(
    value: object,
    table: str,
    key: tuple[tuple[str, object], ...],
    column: str,
) -> str | None:
    text = _optional_text(value, table, key, column)
    if text is None:
        return None
    normalized = unicodedata.normalize("NFKC", text).strip().upper()
    if not _RATING_PATTERN.fullmatch(normalized):
        raise _data_error(
            table,
            key,
            column,
            "must be a rating grade A through F with an optional plus or minus",
        )
    return text


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


def _ensure_unique_source_keys(
    table: str,
    keys: tuple[tuple[str, ...], ...],
    key_names: tuple[str, ...],
) -> None:
    seen: set[tuple[str, ...]] = set()
    for logical_key in sorted(keys):
        if logical_key in seen:
            rendered = ", ".join(
                f"{name}={_display(value)}"
                for name, value in zip(key_names, logical_key, strict=True)
            )
            raise ScenarioDataError(
                f"scenario data error: table '{table}' has duplicate logical key ({rendered})"
            )
        seen.add(logical_key)


class SQLiteRepository:
    """Load a validated immutable snapshot from a SQLite database, read-only."""

    def load_snapshot(self, scenario_path: Path, /) -> ScenarioSnapshot:
        resolved_path = resolve_scenario_path(scenario_path)

        uri = f"{resolved_path.as_uri()}?mode=ro"
        try:
            with closing(sqlite3.connect(uri, uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                connection.enable_load_extension(False)
                if hasattr(connection, "setlimit"):
                    connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
                    connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 100_000)
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

    @staticmethod
    def _read_agent_ownership(
        connection: sqlite3.Connection,
    ) -> tuple[dict[str, str], dict[int, str]]:
        """Load optional agent metadata used to reconstruct internal ownership records."""

        table_rows = connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = ? ORDER BY name",
            ("table",),
        ).fetchall()
        available_tables = {
            row["name"].casefold()
            for row in table_rows
            if isinstance(row["name"], str)
        }
        expected = {
            PO_METADATA_TABLE: PO_METADATA_COLUMNS,
            ALERT_METADATA_TABLE: ALERT_METADATA_COLUMNS,
            DECISION_AUDIT_TABLE: DECISION_AUDIT_COLUMNS,
        }
        present = set(expected) & available_tables
        if not present:
            return {}, {}
        if present != set(expected):
            missing = ", ".join(sorted(set(expected) - present))
            raise ScenarioSchemaError(
                f"scenario schema error: incomplete agent metadata schema; missing: {missing}"
            )
        for table, required_columns in expected.items():
            column_rows = connection.execute(
                "SELECT name FROM pragma_table_info(?) ORDER BY cid",
                (table,),
            ).fetchall()
            columns = {
                row["name"].casefold()
                for row in column_rows
                if isinstance(row["name"], str)
            }
            missing_columns = sorted(required_columns - columns)
            if missing_columns:
                raise ScenarioSchemaError(
                    f"scenario schema error: table '{table}' missing required columns: "
                    + ", ".join(missing_columns)
                )
            count = connection.execute(
                f"SELECT COUNT(*) AS row_count FROM {_quoted(table)}"
            ).fetchone()["row_count"]
            if not isinstance(count, int) or count < 0 or count > MAX_TABLE_ROWS:
                raise ScenarioDataError(
                    f"scenario data error: table '{table}' exceeds the row limit"
                )

        po_markers: dict[str, str] = {}
        for row in connection.execute(
            f"SELECT po_number, marker FROM {_quoted(PO_METADATA_TABLE)} "
            "ORDER BY po_number"
        ).fetchall():
            po_number = _required_text(
                row["po_number"], PO_METADATA_TABLE, (), "po_number"
            )
            marker = _required_text(row["marker"], PO_METADATA_TABLE, (), "marker")
            if len(marker.encode("utf-8")) > MAX_TEXT_BYTES:
                raise ScenarioDataError(
                    f"scenario data error: table '{PO_METADATA_TABLE}' marker is too large"
                )
            if po_number in po_markers:
                raise ScenarioDataError(
                    f"scenario data error: table '{PO_METADATA_TABLE}' has duplicate PO metadata"
                )
            po_markers[po_number] = marker

        alert_markers: dict[int, str] = {}
        for row in connection.execute(
            f"SELECT alert_id, marker FROM {_quoted(ALERT_METADATA_TABLE)} "
            "ORDER BY alert_id"
        ).fetchall():
            raw_alert_id = row["alert_id"]
            if (
                not isinstance(raw_alert_id, int)
                or isinstance(raw_alert_id, bool)
                or raw_alert_id <= 0
            ):
                raise ScenarioDataError(
                    f"scenario data error: table '{ALERT_METADATA_TABLE}' has an invalid alert_id"
                )
            alert_id = raw_alert_id
            marker = _required_text(row["marker"], ALERT_METADATA_TABLE, (), "marker")
            if len(marker.encode("utf-8")) > MAX_TEXT_BYTES:
                raise ScenarioDataError(
                    f"scenario data error: table '{ALERT_METADATA_TABLE}' marker is too large"
                )
            if alert_id in alert_markers:
                raise ScenarioDataError(
                    f"scenario data error: table '{ALERT_METADATA_TABLE}' has duplicate alert metadata"
                )
            alert_markers[alert_id] = marker
        return po_markers, alert_markers

    @staticmethod
    def _validate_input_limits(
        connection: sqlite3.Connection,
        available: dict[str, frozenset[str]],
    ) -> None:
        total_rows = 0
        for table in sorted(_TABLE_COLUMNS):
            count = connection.execute(
                f"SELECT count(*) AS {_quoted('row_count')} FROM {_quoted(table)}"
            ).fetchone()["row_count"]
            if not isinstance(count, int) or count < 0:
                raise ScenarioLoadError("scenario database returned an invalid row count")
            if count > MAX_TABLE_ROWS:
                raise ScenarioDataError(
                    f"scenario data error: table '{table}' exceeds the row limit "
                    f"of {MAX_TABLE_ROWS}"
                )
            total_rows += count
            if total_rows > MAX_TOTAL_ROWS:
                raise ScenarioDataError(
                    "scenario data error: database exceeds the total row limit "
                    f"of {MAX_TOTAL_ROWS}"
                )

            present = tuple(
                column
                for column in _TABLE_COLUMNS[table]
                if column.source.casefold() in available[table]
            )
            if not present or count == 0:
                continue
            expressions = ", ".join(
                f"max(length(CAST({_quoted(column.source)} AS BLOB))) "
                f"AS {_quoted(column.alias)}"
                for column in present
            )
            maxima = connection.execute(
                f"SELECT {expressions} FROM {_quoted(table)}"
            ).fetchone()
            for column in present:
                maximum = maxima[column.alias]
                if isinstance(maximum, int) and maximum > MAX_TEXT_BYTES:
                    raise ScenarioDataError(
                        f"scenario data error: table '{table}' column "
                        f"'{column.source}' exceeds the maximum supported value size "
                        f"of {MAX_TEXT_BYTES} bytes"
                    )

    def _load(
        self,
        connection: sqlite3.Connection,
        available: dict[str, frozenset[str]],
    ) -> ScenarioSnapshot:
        self._validate_input_limits(connection, available)
        rows_by_table = {
            table: self._read_rows(connection, table, available[table])
            for table in sorted(_TABLE_COLUMNS)
        }
        po_markers, alert_markers = self._read_agent_ownership(connection)

        purchase_order_rows: list[dict[str, object]] = []
        purchase_order_ids = {row["po_number"] for row in rows_by_table["purchase_orders"]}
        po_markers = {
            po_number: marker
            for po_number, marker in po_markers.items()
            if po_number in purchase_order_ids
        }
        for row in rows_by_table["purchase_orders"]:
            enriched = dict(row)
            marker = po_markers.get(row["po_number"])
            if marker is not None:
                rationale = row["rationale"]
                if not isinstance(rationale, str) or not rationale:
                    raise ScenarioDataError(
                        "scenario data error: managed purchase order has no human rationale"
                    )
                if "[APEX_AGENT:" in rationale:
                    raise ScenarioDataError(
                        "scenario data error: managed purchase order duplicates embedded metadata"
                    )
                enriched["rationale"] = f"{marker} {rationale}"
            purchase_order_rows.append(enriched)

        alert_rows: list[dict[str, object]] = []
        alert_ids = {
            _integer(row["alert_id"], "alerts", (), "alert_id", positive=True)
            for row in rows_by_table["alerts"]
        }
        alert_markers = {
            alert_id: marker
            for alert_id, marker in alert_markers.items()
            if alert_id in alert_ids
        }
        for row in rows_by_table["alerts"]:
            enriched = dict(row)
            alert_id = _integer(row["alert_id"], "alerts", (), "alert_id", positive=True)
            marker = alert_markers.get(alert_id)
            if marker is not None:
                description = row["description"]
                if not isinstance(description, str) or not description:
                    raise ScenarioDataError(
                        "scenario data error: managed alert has no human description"
                    )
                if "[APEX_ALERT:" in description:
                    raise ScenarioDataError(
                        "scenario data error: managed alert duplicates embedded metadata"
                    )
                enriched["description"] = f"{description} {marker}"
            alert_rows.append(enriched)

        configuration = self._configuration(rows_by_table["scenario_config"])
        products = tuple(self._product(row) for row in rows_by_table["products"])
        components = tuple(self._component(row) for row in rows_by_table["components"])
        bom_lines = tuple(self._bom_line(row) for row in rows_by_table["bom"])
        production_orders = tuple(
            self._production_order(row) for row in rows_by_table["production_schedule"]
        )
        inventory = tuple(
            self._inventory_position(row) for row in rows_by_table["inventory"]
        )
        purchase_orders = tuple(
            self._purchase_order(row) for row in purchase_order_rows
        )
        alerts = tuple(self._alert(row) for row in alert_rows)

        supplier_rows = rows_by_table["suppliers"]
        supplier_source_ids = tuple(
            _required_text(
                row["supplier_id"],
                "suppliers",
                (("supplier_id", row["supplier_id"]),),
                "supplier_id",
            )
            for row in supplier_rows
        )
        _ensure_unique_source_keys(
            "suppliers",
            tuple((supplier_id,) for supplier_id in supplier_source_ids),
            ("supplier_id",),
        )

        catalog_rows = rows_by_table["supplier_catalog"]
        catalog_source_keys: list[tuple[str, str]] = []
        for row in catalog_rows:
            raw_key = (
                ("supplier_id", row["supplier_id"]),
                ("component_id", row["component_id"]),
            )
            catalog_source_keys.append(
                (
                    _required_text(
                        row["supplier_id"],
                        "supplier_catalog",
                        raw_key,
                        "supplier_id",
                    ),
                    _required_text(
                        row["component_id"],
                        "supplier_catalog",
                        raw_key,
                        "component_id",
                    ),
                )
            )
        catalog_key_tuple = tuple(catalog_source_keys)
        _ensure_unique_source_keys(
            "supplier_catalog",
            catalog_key_tuple,
            ("supplier_id", "component_id"),
        )
        component_ids = {item.component_id for item in components}
        source_supplier_id_set = set(supplier_source_ids)
        for supplier_id, component_id in catalog_key_tuple:
            key = (("supplier_id", supplier_id), ("component_id", component_id))
            if supplier_id not in source_supplier_id_set:
                raise ScenarioDataError(
                    f"scenario data error: {_row_location('supplier_catalog', key)} "
                    f"column 'supplier_id' references missing suppliers.supplier_id "
                    f"{_display(supplier_id)}"
                )
            if component_id not in component_ids:
                raise ScenarioDataError(
                    f"scenario data error: {_row_location('supplier_catalog', key)} "
                    f"column 'component_id' references missing components.component_id "
                    f"{_display(component_id)}"
                )

        components_by_supplier: dict[str, set[str]] = {
            supplier_id: set() for supplier_id in supplier_source_ids
        }
        for supplier_id, component_id in catalog_key_tuple:
            components_by_supplier[supplier_id].add(component_id)

        supplier_values: list[Supplier] = []
        route_issues: list[RouteInputIssue] = []
        for row, supplier_id in zip(supplier_rows, supplier_source_ids, strict=True):
            try:
                supplier_values.append(self._supplier(row))
            except ScenarioDataError as error:
                route_issues.append(
                    _route_issue(
                        error,
                        row,
                        supplier_id=supplier_id,
                        component_id=None,
                        affected_component_ids=tuple(
                            sorted(components_by_supplier[supplier_id])
                        ),
                    )
                )
        suppliers = tuple(supplier_values)

        catalog_values: list[SupplierCatalogLine] = []
        for row, (supplier_id, component_id) in zip(
            catalog_rows,
            catalog_key_tuple,
            strict=True,
        ):
            try:
                catalog_values.append(self._catalog_line(row))
            except ScenarioDataError as error:
                route_issues.append(
                    _route_issue(
                        error,
                        row,
                        supplier_id=supplier_id,
                        component_id=component_id,
                        affected_component_ids=(component_id,),
                    )
                )
        catalog_lines = tuple(catalog_values)

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
            source_supplier_ids=source_supplier_id_set,
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
            route_input_issues=tuple(route_issues),
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
            sustainability_rating=_rating_text(
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
        *,
        source_supplier_ids: set[str] | None = None,
    ) -> None:
        product_ids = {item.product_id for item in products}
        component_ids = {item.component_id for item in components}
        supplier_ids = (
            set(source_supplier_ids)
            if source_supplier_ids is not None
            else {item.supplier_id for item in suppliers}
        )

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
    "MAX_SCENARIO_BYTES",
    "MAX_TABLE_ROWS",
    "MAX_TEXT_BYTES",
    "MAX_TOTAL_ROWS",
    "RepositoryLoadError",
    "SQLiteRepository",
    "ScenarioDataError",
    "ScenarioLoadError",
    "ScenarioPathError",
    "ScenarioSchemaError",
    "load_snapshot",
    "resolve_scenario_path",
]
