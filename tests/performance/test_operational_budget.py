from __future__ import annotations

from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import tempfile
from time import perf_counter
import tracemalloc
import unittest

from apex_procurement.cli import run
from apex_procurement.config import RuntimeConfig


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "data" / "scenarios" / "scenario_06_simple.sqlite"
GENERATED_SCHEDULE_ROWS = 3_000
RUNTIME_BUDGET_SECONDS = 10.0
PEAK_MEMORY_BUDGET_BYTES = 128 * 1024 * 1024


class OperationalBudgetTests(unittest.TestCase):
    def test_three_thousand_row_workload_stays_within_recorded_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = Path(directory) / "generated-workload.sqlite"
            shutil.copy2(SOURCE, scenario)
            with closing(sqlite3.connect(scenario)) as connection, connection:
                product_id, due_date = connection.execute(
                    "SELECT product_id, materials_needed_by "
                    "FROM production_schedule LIMIT 1"
                ).fetchone()
                connection.executemany(
                    "INSERT INTO production_schedule "
                    "(order_id, product_id, quantity, customer, materials_needed_by) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        (
                            f"generated-order-{index:05d}",
                            product_id,
                            1,
                            "generated-load",
                            due_date,
                        )
                        for index in range(GENERATED_SCHEDULE_ROWS - 1)
                    ),
                )
                connection.execute(
                    "UPDATE inventory SET quantity_on_hand = ?", (1_000_000_000,)
                )

            tracemalloc.start()
            started = perf_counter()
            try:
                artifacts = run(RuntimeConfig(scenario, dry_run=True))
                elapsed = perf_counter() - started
                _, peak_bytes = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

        self.assertTrue(artifacts.validation.is_valid)
        self.assertEqual(artifacts.outputs.purchase_orders, ())
        self.assertLess(
            elapsed,
            RUNTIME_BUDGET_SECONDS,
            f"generated workload took {elapsed:.3f}s",
        )
        self.assertLess(
            peak_bytes,
            PEAK_MEMORY_BUDGET_BYTES,
            f"generated workload peaked at {peak_bytes / (1024 * 1024):.1f} MiB",
        )


if __name__ == "__main__":
    unittest.main()
