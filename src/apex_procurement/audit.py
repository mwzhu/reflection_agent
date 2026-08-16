"""Bounded structured audit helpers for the deterministic runtime."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Mapping

from .serialization import canonical_dumps, sanitize_control_characters


AUDIT_SCHEMA_VERSION = 1
_HASH_CHUNK_BYTES = 1024 * 1024


def _sanitized(value: object) -> object:
    if isinstance(value, str):
        return sanitize_control_characters(value)
    if isinstance(value, tuple):
        return tuple(_sanitized(item) for item in value)
    if isinstance(value, list):
        return [_sanitized(item) for item in value]
    if isinstance(value, dict):
        return {
            sanitize_control_characters(key): _sanitized(item)
            for key, item in value.items()
        }
    return value


def file_sha256(path: Path, /) -> str:
    """Hash exactly one already-validated input file without following other paths."""

    if not isinstance(path, Path):
        raise TypeError("path must be pathlib.Path")
    digest = sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def deterministic_run_id(
    *, input_hash: str, snapshot_digest: str, contract: str, attempt: int
) -> str:
    payload = canonical_dumps(
        {
            "attempt": attempt,
            "contract": contract,
            "input_hash": input_hash,
            "snapshot_digest": snapshot_digest,
        }
    )
    return sha256(payload.encode("utf-8")).hexdigest()[:24]


def audit_json_line(event: str, fields: Mapping[str, object], /) -> str:
    """Render one single-line JSON audit artifact from explicitly selected facts."""

    if not isinstance(event, str) or not event.strip():
        raise ValueError("event must be non-empty text")
    safe_event = sanitize_control_characters(event)
    payload = {
        "audit_schema_version": AUDIT_SCHEMA_VERSION,
        "event": safe_event,
        **{
            sanitize_control_characters(key): _sanitized(value)
            for key, value in fields.items()
        },
    }
    return canonical_dumps(payload)


__all__ = [
    "AUDIT_SCHEMA_VERSION",
    "audit_json_line",
    "deterministic_run_id",
    "file_sha256",
]
