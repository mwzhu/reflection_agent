"""Canonical construction and hashing of immutable scenario snapshots.

The digest represents only the typed scenario facts consumed by the planner.
It deliberately excludes SQLite storage details, unknown columns, row order,
and its own value.  Numerically equal ``Decimal`` values have one canonical
representation, so harmless differences in SQLite numeric spelling do not
change the digest.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
from typing import Any

from .domain import (
    BomLine,
    Component,
    ExistingAlert,
    ExistingPurchaseOrder,
    InventoryPosition,
    Product,
    ProductionOrder,
    RouteInputIssue,
    ScenarioConfiguration,
    ScenarioSnapshot,
    Supplier,
    SupplierCatalogLine,
)


_DIGEST_FORMAT_VERSION = 1
_PENDING_DIGEST = "pending"


def _canonical_decimal(value: Decimal) -> str:
    """Return a context-independent representation of a finite Decimal."""

    if not value.is_finite():
        raise ValueError("snapshot digest cannot contain a non-finite Decimal")
    decimal_tuple = value.as_tuple()
    digits = list(decimal_tuple.digits)
    if not digits or all(digit == 0 for digit in digits):
        return "0"

    exponent = decimal_tuple.exponent
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    coefficient = "".join(str(digit) for digit in digits)
    sign = "-" if decimal_tuple.sign else "+"
    return f"{sign}{coefficient}e{exponent}"


def _canonical_value(value: object) -> object:
    if isinstance(value, Decimal):
        return {"decimal": _canonical_decimal(value)}
    if isinstance(value, datetime):
        raise TypeError("datetime is not part of a scenario snapshot")
    if isinstance(value, date):
        return {"date": value.isoformat()}
    if isinstance(value, Enum):
        return {"enum": value.value}
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonical_value(getattr(value, field.name))
            for field in fields(value)
            if field.name != "state_digest"
        }
    if isinstance(value, tuple):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported snapshot digest value: {type(value).__name__}")


def canonical_snapshot_payload(snapshot: ScenarioSnapshot) -> bytes:
    """Encode the semantic snapshot state into canonical UTF-8 JSON bytes."""

    if not isinstance(snapshot, ScenarioSnapshot):
        raise TypeError("snapshot must be ScenarioSnapshot")
    payload: dict[str, Any] = {
        "format_version": _DIGEST_FORMAT_VERSION,
        "snapshot": _canonical_value(snapshot),
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def compute_state_digest(snapshot: ScenarioSnapshot) -> str:
    """Compute the SHA-256 digest of a typed snapshot, excluding its digest."""

    return sha256(canonical_snapshot_payload(snapshot)).hexdigest()


def build_snapshot(
    *,
    configuration: ScenarioConfiguration,
    products: tuple[Product, ...],
    components: tuple[Component, ...],
    suppliers: tuple[Supplier, ...],
    bom_lines: tuple[BomLine, ...],
    catalog_lines: tuple[SupplierCatalogLine, ...],
    production_orders: tuple[ProductionOrder, ...],
    inventory: tuple[InventoryPosition, ...],
    purchase_orders: tuple[ExistingPurchaseOrder, ...],
    alerts: tuple[ExistingAlert, ...],
    route_input_issues: tuple[RouteInputIssue, ...] = (),
) -> ScenarioSnapshot:
    """Construct, sort, validate, and digest an immutable scenario snapshot."""

    draft = ScenarioSnapshot(
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
        state_digest=_PENDING_DIGEST,
        route_input_issues=route_input_issues,
    )
    return replace(draft, state_digest=compute_state_digest(draft))


def has_valid_state_digest(snapshot: ScenarioSnapshot) -> bool:
    """Return whether a snapshot's attached digest matches its semantic state."""

    return snapshot.state_digest == compute_state_digest(snapshot)


__all__ = [
    "build_snapshot",
    "canonical_snapshot_payload",
    "compute_state_digest",
    "has_valid_state_digest",
]
