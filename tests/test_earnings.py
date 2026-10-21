"""Accounting dashboard tests: never mistake offers for paid money."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bounty_hunter.earnings import default_database, load_dashboard, provider_info


class EarningsDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "data" / "bounty_hunter.db"
        self.path.parent.mkdir()
        connection = sqlite3.connect(self.path)
        connection.executescript(
            """CREATE TABLE attempts (
                id INTEGER PRIMARY KEY,
                source TEXT, source_id TEXT, title TEXT, status TEXT,
                created_at TEXT, updated_at TEXT,
                agent_cost_usd REAL, compute_cost_usd REAL, human_hours REAL,
                payout_usd REAL, opportunity_json TEXT, pr_url TEXT
            );"""
        )
        rows = [
            (1, "algora", "1", "Shipped fix", "paid", "2026-01-01", "2026-01-05",
             4, 1, 2, 100, json.dumps({"bounty_usd": 100}), None),
            (2, "boss", "2", "Merged, unpaid", "merged", "2026-01-02", "2026-01-05",
             3, 0, 1, 0, json.dumps({"bounty_usd": 250}), None),
            (3, "opire", "3", "Open offer", "queued", "2026-01-02", "2026-01-04",
             0, 0, 0, 0, json.dumps({"bounty_usd": 1000}), None),
        ]
        connection.executemany(
            """INSERT INTO attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        connection.commit()
        connection.close()

    def test_amounts_never_count_offers(self):
        view = load_dashboard(str(self.path))
        self.assertEqual(view["recorded_usd"], 100)
        self.assertEqual(view["direct_cost_usd"], 8)
        self.assertEqual(view["cash_net_usd"], 92)
        self.assertEqual(view["logged_hours"], 3)
        self.assertEqual(view["gross_per_hour"], 33.33)
        self.assertEqual(view["cash_net_per_hour"], 30.67)
        self.assertEqual(view["unconfirmed_usd"], 250)
        self.assertEqual(view["attempt_count"], 3)
        self.assertEqual(view["completed_attempts"], 2)

    def test_no_logged_hours_is_not_zero_earning_rate(self):
        con = sqlite3.connect(self.path)
        con.execute("UPDATE attempts SET human_hours = 0")
        con.commit()
        con.close()
        view = load_dashboard(str(self.path))
        self.assertIsNone(view["gross_per_hour"])
        self.assertIsNone(view["cash_net_per_hour"])

    def test_read_only_missing_db(self):
        missing = self.path.parent / "missing.db"
        with self.assertRaises(FileNotFoundError):
            load_dashboard(str(missing))
        self.assertFalse(missing.exists())

    def test_invalid_offer_json_is_safe(self):
        con = sqlite3.connect(self.path)
        con.execute("UPDATE attempts SET opportunity_json = 'broken' WHERE id = 2")
        con.commit()
        con.close()
        view = load_dashboard(str(self.path))
        self.assertEqual(view["unconfirmed_usd"], 0)
        self.assertEqual(view["recorded_usd"], 100)

    def test_default_database_finds_existing_workspace(self):
        self.assertTrue(Path(default_database([self.path.parent.parent])).samefile(self.path))
        self.assertIsNone(default_database([self.path.parent / "missing"]))

    def test_provider_links_are_allowlisted(self):
        self.assertEqual(provider_info("algora")[1], "https://algora.io")
        self.assertIsNone(provider_info("untrusted-provider")[1])


if __name__ == "__main__":
    unittest.main()
