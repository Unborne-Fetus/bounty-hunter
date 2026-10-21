import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bounty_hunter.desktop_settings import normalize_settings, workspace_paths, load_settings, settings_path
from bounty_hunter.desktop_service import scan_arguments, read_report, run_scan, run_actionable_scan, _algora_discovery_is_recent, recheck_github_opportunity, recheck_all_github_opportunities
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

    def test_recheck_all_verifies_links_without_override_of_payment_gates(self):
        from bounty_hunter.models import Opportunity
        paths = workspace_paths(self.settings)
        paths["reports"].mkdir(parents=True, exist_ok=True)
        items = [
            Opportunity(
                source="algora", source_id="1", title="Fix parser",
                source_url="https://algora.io/acme/bounties", bounty_usd=250,
                status="open", issue_url="https://github.com/acme/parser/issues/1",
                language="Python",
            ),
            Opportunity(
                source="opire", source_id="2", title="Fix tests",
                source_url="https://opire.dev", bounty_usd=150,
                status="open", issue_url="https://github.com/acme/parser/issues/2",
                payout_requires_sponsor_action=True,
            ),
            Opportunity(
                source="taskbounty", source_id="3", title="No linked repository",
                source_url="https://example.test", bounty_usd=200, status="open",
            ),
        ]
        paths["opportunities"].write_text(json.dumps({
            "opportunities": [
                {"opportunity": item.to_dict(), "score": {"eligible": False, "decision": "reject"}}
                for item in items
            ],
            "source_health": [],
        }), encoding="utf-8")
        client = type("FakeClient", (), {"requests_made": 0, "rate_limited_until": None})()
        notices = []

        def enrich(opp, github):
            self.assertIs(github, client)
            client.requests_made += 3
            opp.public_repository = True
            opp.issue_open = True
            opp.issue_activity_recent = True
            opp.repository_archived = False
            opp.repository_created_at = "2020-01-01T00:00:00Z"
            opp.repository_stars = 100
            opp.repository_license = "MIT"
            opp.has_test_suite = True
            opp.contribution_guide_found = True
            opp.acceptance_criteria_clear = True
            opp.maintainer_active = True
            opp.competing_prs = 0
            opp.evidence["github_enriched_at"] = "2026-10-21T00:00:00Z"

        with patch("bounty_hunter.github.GitHubClient", return_value=client):
            with patch("bounty_hunter.github.enrich_opportunity", side_effect=enrich):
                result = recheck_all_github_opportunities(
                    self.settings, progress=notices.append
                )
        self.assertEqual(result["attempted"], 2)
        self.assertEqual(result["verified"], 2)
        self.assertEqual(result["missing_issue_links"], 1)
        self.assertEqual(result["requests"], 6)
        self.assertEqual(len(notices), 2)
        records = read_report(self.settings)["opportunities"]
        by_id = {r["opportunity"]["source_id"]: r for r in records}
        self.assertGreater(by_id["1"]["score"]["confidence"], 0.6)
        self.assertEqual(
            by_id["2"]["opportunity"]["evidence"]["github_verification_status"],
            "verified",
        )
        self.assertFalse(by_id["2"]["score"]["eligible"])
        self.assertIn(
            "payout requires sponsor action after completion and is not guaranteed",
            by_id["2"]["score"]["rejection_reasons"],
        )
        self.assertEqual(
            by_id["3"]["opportunity"]["evidence"]["github_verification_status"],
            "missing_issue_link",
        )

    def test_recheck_all_defers_on_budget_and_never_keeps_stale_eligibility(self):
        from bounty_hunter.models import Opportunity
        paths = workspace_paths(self.settings)
        paths["reports"].mkdir(parents=True, exist_ok=True)
        issue = Opportunity(
            source="algora", source_id="1", title="Prior verified",
            source_url="https://algora.io/acme/bounties", bounty_usd=250,
            status="open", issue_url="https://github.com/acme/parser/issues/1",
            public_repository=True, issue_open=True, repository_stars=100,
            repository_license="MIT", repository_created_at="2020-01-01T00:00:00Z",
            evidence={"github_enriched_at": "2026-10-20T00:00:00Z"},
        )
        paths["opportunities"].write_text(json.dumps({
            "opportunities": [{"opportunity": issue.to_dict(), "score": {
                "eligible": True, "decision": "approve_for_human_review",
            }}],
        }), encoding="utf-8")
        client = type("FakeClient", (), {"requests_made": 200, "rate_limited_until": None})()
        with patch("bounty_hunter.github.GitHubClient", return_value=client):
            with patch("bounty_hunter.github.enrich_opportunity") as enrich:
                result = recheck_all_github_opportunities(self.settings)
                enrich.assert_not_called()
        self.assertEqual(result["deferred"], 1)
        record = read_report(self.settings)["opportunities"][0]
        self.assertFalse(record["score"]["eligible"])
        self.assertEqual(record["score"]["probability_valid"], 0)
        self.assertTrue(record["opportunity"]["evidence"]["github_evidence_stale"])

    def test_actionable_scan_discovers_trusted_boards_before_collection(self):
        settings = normalize_settings({
            **self.settings,
            "sources": {"algora": True, "bountypay": True, "boss": True,
                        "opire": True, "taskbounty": False},
        })
        paths = workspace_paths(settings)
        seen = []

        def discover(values):
            seen.append("audit")
            paths["reports"].mkdir(parents=True, exist_ok=True)
            paths["algora_audit"].write_text(json.dumps({
                "generated_at": "2030-01-01T00:00:00Z",
                "candidates": [
                    {"organization": "acme", "status": "fresh",
                     "promotion": "review_for_trusted_list", "open_board_rows": 1},
                ],
            }), encoding="utf-8")
            return {
                "candidates": [{}],
                "review_candidates": ["acme"],
            }

        def collect(args):
            seen.append("scan")
            self.assertFalse(args.no_algora)
            self.assertEqual(args.algora_org, [])
            self.assertTrue(paths["algora_audit"].exists())
            args.source_health = [{"source": "boss", "status": "fresh", "opportunities": 0}]
            return []

        with patch("bounty_hunter.desktop_service.run_algora_audit", side_effect=discover):
            with patch("bounty_hunter.desktop_service.cli._collect", side_effect=collect):
                result = run_actionable_scan(settings)
        self.assertEqual(seen, ["audit", "scan"])
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["algora_auto_discovery"]["verified_boards"], 1)
        report = read_report(settings)
        self.assertTrue(any(row["source"] == "algora_auto_discovery" and
                            row["status"] == "fresh" for row in report["source_health"]))

    def test_actionable_scan_skips_recent_board_audit(self):
        from datetime import datetime, timezone
        settings = normalize_settings({
            **self.settings,
            "sources": {"algora": True, "bountypay": False, "boss": False,
                        "opire": False, "taskbounty": False},
        })
        paths = workspace_paths(settings)
        paths["reports"].mkdir(parents=True, exist_ok=True)
        paths["algora_audit"].write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "candidates": [
                {"organization": "acme", "status": "fresh",
                 "promotion": "review_for_trusted_list", "open_board_rows": 2},
            ],
        }), encoding="utf-8")
        with patch("bounty_hunter.desktop_service.run_algora_audit") as audit:
            with patch("bounty_hunter.desktop_service.cli._collect", return_value=[]):
                result = run_actionable_scan(settings)
            audit.assert_not_called()
        self.assertEqual(result["algora_auto_discovery"]["status"], "cached")
        self.assertTrue(_algora_discovery_is_recent(paths["algora_audit"]))

    def test_recent_empty_algora_audit_forces_fresh_discovery(self):
        from datetime import datetime, timezone
        settings = normalize_settings({
            **self.settings,
            "sources": {"algora": True, "bountypay": False, "boss": False,
                        "opire": False, "taskbounty": False},
        })
        paths = workspace_paths(settings)
        paths["reports"].mkdir(parents=True, exist_ok=True)
        paths["algora_audit"].write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "candidates": [],
            "review_candidates": [],
        }), encoding="utf-8")
        self.assertFalse(_algora_discovery_is_recent(paths["algora_audit"]))
        with patch("bounty_hunter.desktop_service.run_algora_audit",
                   return_value={"candidates": [{"organization": "one"}],
                                 "review_candidates": []}) as audit:
            with patch("bounty_hunter.desktop_service.cli._collect", return_value=[]):
                result = run_actionable_scan(settings)
            audit.assert_called_once()
        self.assertEqual(result["algora_auto_discovery"]["status"], "no_verified_boards")
        self.assertEqual(result["algora_auto_discovery"]["verified_boards"], 0)

    def test_actionable_scan_marks_disabled_algora_instead_of_silent_omission(self):
        with patch("bounty_hunter.desktop_service.run_algora_audit") as audit:
            with patch("bounty_hunter.desktop_service.cli._collect", return_value=[]):
                result = run_actionable_scan(self.settings)
            audit.assert_not_called()
        self.assertEqual(result["algora_auto_discovery"]["status"], "disabled")
        report = read_report(self.settings)
        self.assertTrue(any(
            row["source"] == "algora_auto_discovery" and row["status"] == "disabled"
            for row in report["source_health"]
        ))

    def test_actionable_scan_records_discovery_failure_but_still_scans(self):
        settings = normalize_settings({
            **self.settings,
            "sources": {"algora": True, "bountypay": True, "boss": False,
                        "opire": False, "taskbounty": False},
        })
        with patch("bounty_hunter.desktop_service.run_algora_audit",
                   side_effect=RuntimeError("GitHub is unavailable")):
            with patch("bounty_hunter.desktop_service.cli._collect", return_value=[]):
                result = run_actionable_scan(settings)
        self.assertEqual(result["algora_auto_discovery"]["status"], "error")
        self.assertEqual(result["count"], 0)
        report = read_report(settings)
        self.assertTrue(any(row["source"] == "algora_auto_discovery" and
                            row["status"] == "error" for row in report["source_health"]))

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
