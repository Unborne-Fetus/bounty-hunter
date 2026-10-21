import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bounty_hunter.desktop_settings import normalize_settings, workspace_paths, load_settings, settings_path
from bounty_hunter.desktop_service import scan_arguments, read_report, run_scan, recheck_github_opportunity
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

    def _write_one_unverified_bounty(self, issue_url="https://github.com/acme/parser/issues/42"):
        from bounty_hunter.models import Opportunity
        opp = Opportunity(
            source="algora", source_id="42", title="Fix parser bug",
            source_url="https://algora.io/acme/bounties", bounty_usd=250,
            status="open", issue_url=issue_url, language="Python",
            evidence={"github_error": 503},
        )
        paths = workspace_paths(self.settings)
        paths["reports"].mkdir(parents=True, exist_ok=True)
        paths["opportunities"].write_text(json.dumps({
            "opportunities": [{
                "opportunity": opp.to_dict(),
                "score": {"eligible": False, "decision": "needs_verification"},
            }],
            "source_health": [],
        }), encoding="utf-8")
        return paths

    def test_selected_github_recheck_replaces_failed_evidence(self):
        paths = self._write_one_unverified_bounty()
        fake_client = type("FakeGitHub", (), {"requests_made": 5})()

        def verified(opportunity, client):
            self.assertNotIn("github_error", opportunity.evidence)
            self.assertIs(client, fake_client)
            opportunity.public_repository = True
            opportunity.issue_open = True
            opportunity.issue_activity_recent = True
            opportunity.repository_archived = False
            opportunity.repository_created_at = "2020-01-01T00:00:00Z"
            opportunity.repository_stars = 100
            opportunity.repository_license = "MIT"
            opportunity.has_test_suite = True
            opportunity.contribution_guide_found = True
            opportunity.acceptance_criteria_clear = True
            opportunity.reproducible = True
            opportunity.maintainer_active = True
            opportunity.competing_prs = 0
            opportunity.evidence["github_enriched_at"] = "2026-10-21T00:00:00Z"

        with patch("bounty_hunter.github.GitHubClient", return_value=fake_client):
            with patch("bounty_hunter.github.enrich_opportunity", side_effect=verified):
                result = recheck_github_opportunity(self.settings, "algora", "42")
        self.assertEqual(result["decision"], "approve_for_human_review")
        self.assertTrue(result["eligible"])
        self.assertEqual(result["requests"], 5)
        record = read_report(self.settings)["opportunities"][0]
        self.assertTrue(record["score"]["eligible"])
        self.assertTrue(record["opportunity"]["issue_open"])
        self.assertNotIn("github_error", record["opportunity"]["evidence"])
        self.assertTrue(paths["db"].exists())

    def test_selected_recheck_requires_real_issue_link(self):
        self._write_one_unverified_bounty(issue_url=None)
        with self.assertRaisesRegex(ValueError, "GitHub issue URL"):
            recheck_github_opportunity(self.settings, "algora", "42")

    def test_selected_recheck_does_not_promote_failed_api_evidence(self):
        self._write_one_unverified_bounty()
        fake_client = type("FakeGitHub", (), {"requests_made": 1})()

        def rate_limited(opportunity, client):
            opportunity.evidence["github_error"] = 403

        with patch("bounty_hunter.github.GitHubClient", return_value=fake_client):
            with patch("bounty_hunter.github.enrich_opportunity", side_effect=rate_limited):
                with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                    recheck_github_opportunity(self.settings, "algora", "42")
        record = read_report(self.settings)["opportunities"][0]
        self.assertFalse(record["score"]["eligible"])
        self.assertFalse(workspace_paths(self.settings)["db"].exists())

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

    def test_old_gui_settings_survive_upgrade(self):
        with patch.dict(os.environ, {"APPDATA": self.workspace}):
            legacy = Path(self.workspace) / "BountyHunter" / "app-settings.json"
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_text(json.dumps({
                "workspace": self.workspace,
                "country": "us",
                "algora_orgs": "sample-org, second-org",
                "disabled_sources": ["boss", "opire"],
            }), encoding="utf-8")
            values = load_settings()
            self.assertEqual(values["workspace"], str(Path(self.workspace).resolve()))
            self.assertEqual(values["boss_payout_country"], "US")
            self.assertEqual(values["algora_organizations"], ["sample-org", "second-org"])
            self.assertFalse(values["sources"]["boss"])
            self.assertFalse(values["sources"]["opire"])
            self.assertTrue(values["sources"]["taskbounty"])

    def test_invalid_desktop_settings_does_not_crash(self):
        with patch.dict(os.environ, {"APPDATA": self.workspace}):
            path = settings_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{ invalid json", encoding="utf-8")
            values = load_settings()
            self.assertTrue(values["sources"]["algora"])
            self.assertFalse(path.exists())
            self.assertEqual(len(list(path.parent.glob("desktop.invalid-*.json"))), 1)

    def test_old_dashboard_launches_unified_desktop(self):
        from bounty_hunter import dashboard
        with patch("bounty_hunter.desktop.main") as launch:
            dashboard.main()
            launch.assert_called_once_with()

    def test_exe_startup_error_writes_safe_diagnostics(self):
        from scripts import dashboard_entry
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {"LOCALAPPDATA": folder}):
                # Do not open a real native MessageBoxW on Windows CI runners.
                with patch("scripts.dashboard_entry.sys.platform", "linux"):
                    with patch("bounty_hunter.desktop.main", side_effect=RuntimeError("secret example text")):
                        with self.assertRaises(SystemExit) as caught:
                            dashboard_entry.launch()
                self.assertEqual(caught.exception.code, 1)
                log = (Path(folder) / "BountyHunter" / "startup-error.log").read_text(encoding="utf-8")
                self.assertIn("RuntimeError", log)
                self.assertIn("Traceback locations:", log)
                self.assertNotIn("secret example text", log)

    def test_settings_do_not_include_secret_token(self):
        normalized = normalize_settings({
            "workspace": self.workspace, "github_token_env": "GH_BOUNTY_TOKEN"
        })
        self.assertEqual(normalized["github_token_env"], "GH_BOUNTY_TOKEN")
        self.assertNotIn("github_token", normalized)
        self.assertNotIn("wallet", normalized)


if __name__ == "__main__":
    unittest.main()
