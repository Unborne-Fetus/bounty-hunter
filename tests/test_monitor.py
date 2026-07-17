import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bounty_hunter.history import History
from bounty_hunter.monitor import _queue_top_eligible, run_monitor


class MonitorTests(unittest.TestCase):
    def test_once_records_successful_heartbeat(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = run_monitor(
                project_root=temporary,
                interval_seconds=3600,
                once=True,
                runner=lambda *args, **kwargs: subprocess.CompletedProcess(
                    args[0], 0, "scan ok", ""
                ),
                now=lambda: datetime(2026, 7, 16, tzinfo=timezone.utc),
            )
            status = json.loads(
                (Path(temporary) / "data/monitor-status.json").read_text()
            )
        self.assertEqual(result, 0)
        self.assertEqual(status["exit_code"], 0)
        self.assertEqual(status["consecutive_failures"], 0)
        self.assertIn("scan ok", status["stdout"])

    def test_failure_uses_backoff_and_returns_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = run_monitor(
                project_root=temporary,
                interval_seconds=3600,
                once=True,
                runner=lambda *args, **kwargs: subprocess.CompletedProcess(
                    args[0], 7, "", "network failed"
                ),
                now=lambda: datetime(2026, 7, 16, tzinfo=timezone.utc),
            )
            status = json.loads(
                (Path(temporary) / "data/monitor-status.json").read_text()
            )
        self.assertEqual(result, 7)
        self.assertEqual(status["consecutive_failures"], 1)
        self.assertTrue(status["next_scan_at"].endswith("00:05:00+00:00"))

    def test_eligible_candidate_sets_human_attention_flag(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_monitor(
                project_root=temporary,
                interval_seconds=3600,
                once=True,
                runner=lambda *args, **kwargs: subprocess.CompletedProcess(
                    args[0],
                    0,
                    "evaluated 84 opportunities; 2 eligible; recorded scan 20;",
                    "",
                ),
                now=lambda: datetime(2026, 7, 16, tzinfo=timezone.utc),
            )
            status = json.loads(
                (Path(temporary) / "data/monitor-status.json").read_text()
            )
        self.assertEqual(status["evaluated_count"], 84)
        self.assertEqual(status["eligible_count"], 2)
        self.assertTrue(status["attention_required"])

    def test_state_paths_cannot_escape_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "inside project"):
                run_monitor(
                    project_root=temporary,
                    interval_seconds=3600,
                    once=True,
                    status_path="../outside.json",
                )

    def test_due_discovery_runs_before_scan_without_controlling_scan_result(self):
        calls = []

        def runner(command, **_kwargs):
            calls.append(command)
            if command[0] == "discover":
                return subprocess.CompletedProcess(command, 8, "", "rate limited")
            return subprocess.CompletedProcess(
                command, 0, "evaluated 5 opportunities; 0 eligible;", ""
            )

        with tempfile.TemporaryDirectory() as temporary:
            result = run_monitor(
                project_root=temporary,
                interval_seconds=3600,
                once=True,
                runner=runner,
                discovery_command=("discover",),
                now=lambda: datetime(2026, 7, 16, tzinfo=timezone.utc),
            )
            status = json.loads(
                (Path(temporary) / "data/monitor-status.json").read_text()
            )
        self.assertEqual(result, 0)
        self.assertEqual(calls[0], ["discover"])
        self.assertEqual(status["board_discovery"]["exit_code"], 8)
        self.assertEqual(status["eligible_count"], 0)

    def test_fresh_discovery_report_skips_auxiliary_job(self):
        calls = []
        at = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "reports/algora-board-audit.json"
            report.parent.mkdir(parents=True)
            report.write_text("{}", encoding="utf-8")
            run_monitor(
                project_root=temporary,
                interval_seconds=3600,
                once=True,
                runner=lambda command, **_kwargs: (
                    calls.append(command)
                    or subprocess.CompletedProcess(command, 0, "scan ok", "")
                ),
                discovery_command=("discover",),
                now=lambda: at,
            )
            status = json.loads(
                (Path(temporary) / "data/monitor-status.json").read_text()
            )
        self.assertEqual(len(calls), 1)
        self.assertFalse(status["board_discovery"]["ran"])

    def test_auto_queue_selects_top_candidate_once_without_approval(self):
        records = []
        for source_id, priority in (("low", 10), ("high", 50)):
            records.append(
                {
                    "opportunity": {
                        "source": "algora",
                        "source_id": source_id,
                        "title": source_id,
                        "bounty_usd": 100,
                    },
                    "score": {
                        "eligible": True,
                        "priority_score": priority,
                        "expected_hourly_return_usd": priority,
                    },
                }
            )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "reports/opportunities.json"
            report.parent.mkdir(parents=True)
            report.write_text(
                json.dumps({"opportunities": records}), encoding="utf-8"
            )
            history_path = root / "data/history.db"
            first = _queue_top_eligible(report, history_path)
            second = _queue_top_eligible(report, history_path)
            history = History(str(history_path))
            try:
                summary = history.summary()
                attempt = history.get_attempt(first["attempt_id"])
            finally:
                history.close()
        self.assertEqual(first["source_id"], "high")
        self.assertTrue(first["created_by_monitor"])
        self.assertEqual(second["attempt_id"], first["attempt_id"])
        self.assertFalse(second["created_by_monitor"])
        self.assertEqual(summary["attempts"], 1)
        self.assertEqual(attempt["status"], "queued")
        self.assertIsNone(attempt["approved_at"])

    def test_monitor_heartbeat_exposes_auto_queued_attempt(self):
        record = {
            "opportunity": {
                "source": "algora",
                "source_id": "one",
                "title": "Fix parser",
                "bounty_usd": 100,
            },
            "score": {
                "eligible": True,
                "priority_score": 20,
                "expected_hourly_return_usd": 25,
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "reports/opportunities.json"
            report.parent.mkdir(parents=True)
            report.write_text(
                json.dumps({"opportunities": [record]}), encoding="utf-8"
            )
            result = run_monitor(
                project_root=temporary,
                interval_seconds=3600,
                once=True,
                auto_queue=True,
                runner=lambda *args, **kwargs: subprocess.CompletedProcess(
                    args[0],
                    0,
                    "evaluated 1 opportunities; 1 eligible;",
                    "",
                ),
                now=lambda: datetime(2026, 7, 16, tzinfo=timezone.utc),
            )
            status = json.loads(
                (root / "data/monitor-status.json").read_text()
            )
        self.assertEqual(result, 0)
        self.assertEqual(status["schema_version"], 4)
        self.assertEqual(status["queued_attempt"]["source_id"], "one")
        self.assertEqual(status["queued_attempt"]["status"], "queued")
        self.assertEqual(len(status["queued_attempt"]["record_sha256"]), 64)
        self.assertIn("workflow review", status["queued_attempt"]["review_command"])
        self.assertTrue(status["attention_required"])

    def test_auto_queue_withdraws_unapproved_candidate_that_loses_eligibility(self):
        record = {
            "opportunity": {
                "source": "algora",
                "source_id": "one",
                "title": "Fix parser",
                "bounty_usd": 100,
            },
            "score": {
                "eligible": True,
                "priority_score": 20,
                "expected_hourly_return_usd": 25,
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "report.json"
            history_path = root / "history.db"
            report.write_text(
                json.dumps({"opportunities": [record]}), encoding="utf-8"
            )
            queued = _queue_top_eligible(report, history_path)
            report.write_text(
                json.dumps({"opportunities": []}), encoding="utf-8"
            )
            current = _queue_top_eligible(report, history_path)
            history = History(str(history_path))
            try:
                attempt = history.get_attempt(queued["attempt_id"])
                calibration = history.outcome_calibration()
            finally:
                history.close()
        self.assertIsNone(current)
        self.assertEqual(attempt["status"], "abandoned")
        self.assertEqual(calibration["terminal_outcomes"], 0)


if __name__ == "__main__":
    unittest.main()
