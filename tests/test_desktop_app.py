import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bounty_hunter.desktop_settings import normalize_settings, workspace_paths
from bounty_hunter.desktop_service import scan_arguments, read_report, run_scan
from bounty_hunter.desktop_workflow import queue_opportunity, review_attempt


class DesktopAppTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.workspace = self.folder.name
        self.settings = normalize_settings({
            "workspace": self.workspace,
            "sources": {"algora": False, "bountypay": True, "boss": False,
                        "opire": False, "taskbounty": False},
            "auto_scan": False,
        })

    def test_settings_are_local_and_source_preferences_applied(self):
        args = scan_arguments(self.settings)
        self.assertTrue(args.no_boss)
        self.assertTrue(args.no_opire)
        self.assertTrue(args.no_taskbounty)
        self.assertFalse(args.no_bountypay)
        self.assertEqual(Path(args.history_db), workspace_paths(self.settings)["db"])

    def test_scan_creates_compatible_report_and_history(self):
        with patch("bounty_hunter.desktop_service.cli._collect", return_value=[]):
            result = run_scan(self.settings)
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["eligible"], 0)
        self.assertEqual(result["scan_id"], 1)
        report = read_report(self.settings)
        self.assertEqual(report["opportunities"], [])
        self.assertTrue(workspace_paths(self.settings)["db"].exists())

    def test_ineligible_bounty_does_not_queue(self):
        paths = workspace_paths(self.settings)
        paths["reports"].mkdir(parents=True)
        record = {
            "opportunity": {
                "source": "boss", "source_id": "42", "title": "Rejected issue",
                "status": "open", "bounty_usd": 100,
            },
            "score": {"eligible": False, "decision": "reject"}
        }
        paths["opportunities"].write_text(json.dumps({
            "opportunities": [record], "source_health": []
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "did not pass"):
            queue_opportunity(self.settings, "boss", "42")
        self.assertFalse(paths["db"].exists())

    def test_bad_scan_settings_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            normalize_settings({"sources": {name: False for name in
                                ("algora", "bountypay", "boss", "opire", "taskbounty")}})
        with self.assertRaisesRegex(ValueError, "timeout"):
            normalize_settings({"timeout_seconds": 1})

    def test_settings_do_not_include_secret_token(self):
        normalized = normalize_settings({
            "workspace": self.workspace, "github_token_env": "GH_BOUNTY_TOKEN"
        })
        self.assertEqual(normalized["github_token_env"], "GH_BOUNTY_TOKEN")
        self.assertNotIn("github_token", normalized)
        self.assertNotIn("wallet", normalized)


if __name__ == "__main__":
    unittest.main()
