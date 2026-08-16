#!/usr/bin/env python3
"""Command-line entry point for the Apex procurement agent."""

from __future__ import annotations

from pathlib import Path
import sys


_PROJECT_ROOT = Path(__file__).resolve().parent
_SOURCE_ROOT = _PROJECT_ROOT / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from apex_procurement.cli import build_parser, main, parse_config


__all__ = ["build_parser", "main", "parse_config"]


if __name__ == "__main__":
    raise SystemExit(main())
