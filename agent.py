#!/usr/bin/env python3
"""Command-line entry point for the Apex procurement agent.

This scaffold parses the stable command contract.  Planning is integrated by a
later work package; help and imports remain completely offline and data-free.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from collections.abc import Sequence


_PROJECT_ROOT = Path(__file__).resolve().parent
_SOURCE_ROOT = _PROJECT_ROOT / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from apex_procurement.config import EvidenceContract, ModelMode, RuntimeConfig


def build_parser() -> argparse.ArgumentParser:
    """Build the stable public CLI without touching data or network services."""

    parser = argparse.ArgumentParser(
        prog="agent.py",
        description="Plan procurement deterministically from a SQLite scenario snapshot.",
    )
    parser.add_argument(
        "--scenario",
        type=Path,
        required=True,
        metavar="SCENARIO.sqlite",
        help="path to the scenario SQLite snapshot",
    )
    parser.add_argument(
        "--contract",
        choices=tuple(item.value for item in EvidenceContract),
        default=EvidenceContract.BENCHMARK.value,
        help="missing-evidence contract (default: benchmark)",
    )
    parser.add_argument(
        "--llm",
        choices=tuple(item.value for item in ModelMode),
        default=ModelMode.OFF.value,
        help="optional model behavior (default: off)",
    )
    parser.add_argument(
        "--recompile-policy",
        action="store_true",
        help="request offline policy-pack recompilation before planning",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and explain the plan without committing rows",
    )
    parser.add_argument(
        "--explain",
        metavar="COMPONENT_ID",
        help="limit detailed explanation output to one component",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="elevate validation warnings in the final run status",
    )
    parser.add_argument(
        "--alert-prefixes",
        action="store_true",
        help="include human-visible category prefixes in alert prose",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit the run result as deterministic JSON",
    )
    return parser


def parse_config(argv: Sequence[str] | None = None) -> RuntimeConfig:
    """Parse command-line arguments into an immutable runtime configuration."""

    args = build_parser().parse_args(argv)
    return RuntimeConfig(
        scenario_path=args.scenario,
        contract=EvidenceContract(args.contract),
        model_mode=ModelMode(args.llm),
        recompile_policy=args.recompile_policy,
        dry_run=args.dry_run,
        explain_component_id=args.explain,
        strict=args.strict,
        alert_prefixes=args.alert_prefixes,
        json_output=args.json_output,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the stable interface; execution is supplied by integration work."""

    parse_config(argv)
    print("planning pipeline is not yet integrated", file=sys.stderr)
    return 5


if __name__ == "__main__":
    raise SystemExit(main())
