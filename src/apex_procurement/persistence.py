"""Agent-owned auxiliary SQLite schema for metadata and structured audit data."""

from __future__ import annotations

import sqlite3


PO_METADATA_TABLE = "apex_po_metadata"
ALERT_METADATA_TABLE = "apex_alert_metadata"
DECISION_AUDIT_TABLE = "apex_decision_audit"

PO_METADATA_COLUMNS = frozenset(
    {
        "po_number",
        "marker",
        "marker_version",
        "action_key",
        "demand_fingerprint",
        "source_fingerprint",
        "evidence_contract",
        "requirement_id",
        "route_id",
        "policy_pack_version",
        "line_index",
        "line_count",
        "group_digest",
        "field_digest",
    }
)
ALERT_METADATA_COLUMNS = frozenset(
    {
        "alert_id",
        "marker",
        "marker_version",
        "alert_key",
        "category",
        "scope",
        "audit_description",
    }
)
DECISION_AUDIT_COLUMNS = frozenset(
    {
        "requirement_id",
        "component_id",
        "policy_pack_version",
        "decision_digest",
        "decision_json",
    }
)


def ensure_agent_tables(connection: sqlite3.Connection) -> None:
    """Create the agent-owned schema inside the caller's active transaction."""

    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PO_METADATA_TABLE} (
            po_number TEXT PRIMARY KEY,
            marker TEXT NOT NULL,
            marker_version INTEGER NOT NULL,
            action_key TEXT NOT NULL UNIQUE,
            demand_fingerprint TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL,
            evidence_contract TEXT NOT NULL,
            requirement_id TEXT NOT NULL,
            route_id TEXT NOT NULL,
            policy_pack_version TEXT NOT NULL,
            line_index INTEGER NOT NULL,
            line_count INTEGER NOT NULL,
            group_digest TEXT NOT NULL,
            field_digest TEXT NOT NULL,
            FOREIGN KEY (po_number) REFERENCES purchase_orders(po_number) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {ALERT_METADATA_TABLE} (
            alert_id INTEGER PRIMARY KEY,
            marker TEXT NOT NULL,
            marker_version INTEGER NOT NULL,
            alert_key TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL,
            scope TEXT NOT NULL,
            audit_description TEXT NOT NULL,
            FOREIGN KEY (alert_id) REFERENCES alerts(alert_id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {DECISION_AUDIT_TABLE} (
            requirement_id TEXT PRIMARY KEY,
            component_id TEXT NOT NULL,
            policy_pack_version TEXT NOT NULL,
            decision_digest TEXT NOT NULL,
            decision_json TEXT NOT NULL
        )
        """
    )


__all__ = [
    "ALERT_METADATA_COLUMNS",
    "ALERT_METADATA_TABLE",
    "DECISION_AUDIT_COLUMNS",
    "DECISION_AUDIT_TABLE",
    "PO_METADATA_COLUMNS",
    "PO_METADATA_TABLE",
    "ensure_agent_tables",
]
