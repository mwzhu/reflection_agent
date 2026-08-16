"""Deterministic JSON serialization for frozen domain contracts.

Decimal values are encoded as JSON strings and reconstructed from those strings;
they are never routed through binary floating point.  Dates use ISO-8601 strings.
Deserialization requires an expected type, avoiding executable type tags in
untrusted JSON.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
import json
from pathlib import Path
import types
from typing import Any, TypeVar, Union, get_args, get_origin, get_type_hints


SerializableT = TypeVar("SerializableT")


def _to_primitive(value: object) -> object:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal values are not serializable")
        return str(value)
    if isinstance(value, datetime):
        raise TypeError("datetime is not part of the domain date contract")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _to_primitive(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_to_primitive(item) for item in value]
    if isinstance(value, list):
        return [_to_primitive(item) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("only text keys are supported in serialized mappings")
        return {key: _to_primitive(item) for key, item in value.items()}
    if isinstance(value, float):
        raise TypeError("float values are forbidden in deterministic domain serialization")
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported serialization type: {type(value).__name__}")


def canonical_dumps(value: object) -> str:
    """Serialize a value into stable, compact, UTF-8-friendly JSON text."""

    return json.dumps(
        _to_primitive(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _from_primitive(value: object, expected_type: Any) -> object:
    if expected_type is Any:
        return value

    origin = get_origin(expected_type)
    arguments = get_args(expected_type)
    if origin in (Union, types.UnionType):
        if value is None and type(None) in arguments:
            return None
        failures: list[Exception] = []
        for option in arguments:
            if option is type(None):
                continue
            try:
                return _from_primitive(value, option)
            except (TypeError, ValueError, KeyError) as error:
                failures.append(error)
        raise TypeError(f"value does not match {expected_type!r}") from failures[-1]

    if origin is tuple:
        if not isinstance(value, list):
            raise TypeError("serialized tuple must be a JSON array")
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            return tuple(_from_primitive(item, arguments[0]) for item in value)
        if len(value) != len(arguments):
            raise ValueError("serialized fixed tuple has the wrong length")
        return tuple(
            _from_primitive(item, item_type)
            for item, item_type in zip(value, arguments, strict=True)
        )

    if expected_type is Decimal:
        if not isinstance(value, str):
            raise TypeError("Decimal must be encoded as a JSON string")
        result = Decimal(value)
        if not result.is_finite():
            raise ValueError("non-finite Decimal values are not supported")
        return result
    if expected_type is date:
        if not isinstance(value, str):
            raise TypeError("date must be encoded as a JSON string")
        return date.fromisoformat(value)
    if expected_type is Path:
        if not isinstance(value, str):
            raise TypeError("Path must be encoded as a JSON string")
        return Path(value)
    if isinstance(expected_type, type) and issubclass(expected_type, Enum):
        return expected_type(value)
    if isinstance(expected_type, type) and is_dataclass(expected_type):
        if not isinstance(value, dict):
            raise TypeError("serialized dataclass must be a JSON object")
        hints = get_type_hints(expected_type)
        expected_names = {field.name for field in fields(expected_type)}
        actual_names = set(value)
        if actual_names != expected_names:
            missing = sorted(expected_names - actual_names)
            extra = sorted(actual_names - expected_names)
            raise ValueError(f"serialized fields differ; missing={missing}, extra={extra}")
        return expected_type(
            **{
                field.name: _from_primitive(value[field.name], hints[field.name])
                for field in fields(expected_type)
            }
        )
    if expected_type is bool:
        if not isinstance(value, bool):
            raise TypeError("expected bool")
        return value
    if expected_type is int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError("expected int")
        return value
    if expected_type is str:
        if not isinstance(value, str):
            raise TypeError("expected str")
        return value
    if expected_type is type(None):
        if value is not None:
            raise TypeError("expected null")
        return None
    raise TypeError(f"unsupported deserialization type: {expected_type!r}")


def canonical_loads(payload: str, expected_type: type[SerializableT]) -> SerializableT:
    """Deserialize deterministic JSON into an explicitly supplied safe type."""

    if not isinstance(payload, str):
        raise TypeError("payload must be str")
    primitive = json.loads(payload, parse_float=lambda _: (_ for _ in ()).throw(
        ValueError("JSON floating-point tokens are forbidden")
    ))
    return _from_primitive(primitive, expected_type)  # type: ignore[return-value]


__all__ = ["canonical_dumps", "canonical_loads"]
