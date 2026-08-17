#!/usr/bin/env python3
"""Compare deterministic and live residual-model outputs on all scenarios."""

from __future__ import annotations

import argparse
from contextlib import closing
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AGENT = PROJECT_ROOT / "agent.py"
SCENARIOS = tuple(sorted((PROJECT_ROOT / "data" / "scenarios").glob("*.sqlite")))


def _business_rows(path: Path) -> dict[str, tuple[tuple[object, ...], ...]]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            "purchase_order_actions": tuple(
                connection.execute(
                    "SELECT component_id, supplier_id, quantity, unit_price, "
                    "order_date, expected_delivery_date "
                    "FROM purchase_orders ORDER BY component_id, supplier_id, "
                    "quantity, unit_price, order_date, expected_delivery_date"
                )
            ),
            "purchase_orders": tuple(
                connection.execute(
                    "SELECT component_id, supplier_id, quantity, unit_price, "
                    "order_date, expected_delivery_date, rationale "
                    "FROM purchase_orders ORDER BY component_id, supplier_id, "
                    "quantity, unit_price, order_date, expected_delivery_date"
                )
            ),
            "alerts": tuple(
                connection.execute(
                    "SELECT description FROM alerts ORDER BY description"
                )
            ),
        }


def _known_order_cost(rows: tuple[tuple[object, ...], ...]) -> str:
    total = sum(
        (Decimal(str(row[2])) * Decimal(str(row[3])) for row in rows),
        Decimal(),
    )
    return str(total)


def _invoke(path: Path, mode: str, contract: str) -> tuple[int, dict[str, object], str]:
    completed = subprocess.run(
        (
            sys.executable,
            str(AGENT),
            "--scenario",
            str(path),
            f"--contract={contract}",
            f"--llm={mode}",
            "--json",
        ),
        cwd=PROJECT_ROOT,
        env=os.environ.copy(),
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        payload = json.loads(completed.stdout) if completed.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    return completed.returncode, payload, completed.stderr


def _selected_signature(decision: dict[str, object]) -> object:
    selected = decision.get("selected_plan")
    if not isinstance(selected, dict):
        return None
    return {
        "disposition": selected.get("disposition"),
        "lines": tuple(
            (
                line.get("supplier_id"),
                line.get("quantity"),
                line.get("unit_price"),
                line.get("expected_delivery_date"),
            )
            for line in selected.get("lines", ())
            if isinstance(line, dict)
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--contract", choices=("benchmark", "production"), default="benchmark"
    )
    parser.add_argument(
        "--on-mode",
        choices=("auto", "required"),
        default="required",
        help="required makes an incomplete live comparison fail closed",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if not os.environ.get("LLM_BASE_URL") or not os.environ.get("LLM_MODEL"):
        parser.error("LLM_BASE_URL and LLM_MODEL must be configured")

    report: dict[str, object] = {
        "contract": args.contract,
        "on_mode": args.on_mode,
        "scenario_count": len(SCENARIOS),
        "scenarios": [],
    }
    with tempfile.TemporaryDirectory(prefix="apex-model-comparison-") as directory:
        temporary = Path(directory)
        for source in SCENARIOS:
            off_path = temporary / f"off-{source.name}"
            on_path = temporary / f"on-{source.name}"
            shutil.copy2(source, off_path)
            shutil.copy2(source, on_path)
            off_code, off_payload, off_error = _invoke(
                off_path, "off", args.contract
            )
            on_code, on_payload, on_error = _invoke(
                on_path, args.on_mode, args.contract
            )
            off_decisions = {
                item["component_id"]: item
                for item in off_payload.get("decisions", ())
                if isinstance(item, dict) and "component_id" in item
            }
            on_decisions = {
                item["component_id"]: item
                for item in on_payload.get("decisions", ())
                if isinstance(item, dict) and "component_id" in item
            }
            changed_components = tuple(
                component_id
                for component_id in sorted(set(off_decisions) | set(on_decisions))
                if _selected_signature(off_decisions.get(component_id, {}))
                != _selected_signature(on_decisions.get(component_id, {}))
            )
            off_rows = _business_rows(off_path)
            on_rows = _business_rows(on_path)
            model_resolution = on_payload.get("model_resolution", {})
            off_cost = Decimal(_known_order_cost(off_rows["purchase_order_actions"]))
            on_cost = Decimal(_known_order_cost(on_rows["purchase_order_actions"]))
            report["scenarios"].append(
                {
                    "scenario": source.name,
                    "off_exit": off_code,
                    "on_exit": on_code,
                    "model_status": on_payload.get("model_status"),
                    "attempted_count": model_resolution.get("attempted_count"),
                    "accepted_count": model_resolution.get("accepted_count"),
                    "failure_count": model_resolution.get("failure_count"),
                    "changed_selected_components": changed_components,
                    "purchase_order_action_rows_equal": (
                        off_rows["purchase_order_actions"]
                        == on_rows["purchase_order_actions"]
                    ),
                    "purchase_order_full_rows_equal": (
                        off_rows["purchase_orders"] == on_rows["purchase_orders"]
                    ),
                    "alert_rows_equal": off_rows["alerts"] == on_rows["alerts"],
                    "off_known_order_cost": str(off_cost),
                    "on_known_order_cost": str(on_cost),
                    "known_order_cost_delta_on_minus_off": str(on_cost - off_cost),
                    "off_purchase_order_count": len(off_rows["purchase_orders"]),
                    "on_purchase_order_count": len(on_rows["purchase_orders"]),
                    "off_alert_count": len(off_rows["alerts"]),
                    "on_alert_count": len(on_rows["alerts"]),
                    "off_error": off_error if off_code else "",
                    "on_error": on_error if on_code else "",
                }
            )

    encoded = json.dumps(report, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if all(
        item["off_exit"] == 0 and item["on_exit"] == 0
        for item in report["scenarios"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
