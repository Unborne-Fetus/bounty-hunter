import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bounty_hunter.desktop_readiness import allowed_actions, check_readiness
from bounty_hunter.desktop_settings import normalize_settings, workspace_paths
from bounty_hunter.desktop_service import run_scan, scan_arguments
from bounty_hunter.desktop_workflow import perform_step
from bounty_hunter.history import History
from bounty_hunter import cli
from tests.test_preflight import eligible_record


class ReadinessTests(unittest.TestCase):
    def test_missing_tools_and_optional_token_do_not_require_coding_to_scan(self):
        settings = normalize_settings({"github_token_env": "BH_TEST_ABSENT_TOKEN"})
        checks = check_readiness(settings, finder=lambda command: None)
        self.assertEqual(next(c for c in checks if c["name"] == "GitHub scan token")["status"], "optional")
        self.assertTrue(any(c["name"] == "Container runtime" for c in checks))

    def test_auth_output_is_not_returned_and_commands_are_bounded(self):
        calls = []
        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout="SECRET_ACCOUNT_DATA", stderr="SECRET_TOKEN")
        checks = check_readiness(normalize_settings({}), finder=lambda name: name, runner=runner)
        self.assertNotIn("SECRET", json.dumps(checks))
        self.assertIn(["gh", "auth", "status"], [c for c, kw in calls])
        for command, kwargs in calls:
            self.assertEqual(kwargs["timeout"], 10)
            self.assertFalse(kwargs["shell"])

    def test_stopped_daemon_and_timeout_are_actionable(self):
        def runner(command, **kwargs):
            if command[0] == "docker":
                return SimpleNamespace(returncode=1)
            raise subprocess.TimeoutExpired(command, 10)
        checks = check_readiness(normalize_settings({}), finder=lambda name: name if name != "podman" else None,
                                 runner=runner)
        self.assertEqual(next(c for c in checks if c["name"] == "docker daemon")["status"], "needs_attention")
        self.assertTrue(any(c["name"] == "git" and c["status"] == "needs_attention" for c in checks))

    def test_workflow_buttons_follow_state_and_artifacts(self):
        self.assertEqual(allowed_actions("queued"), {"inspect", "approve"})
        self.assertIn("preflight", allowed_actions("approved"))
        self.assertNotIn("publish", allowed_actions("verified"))
        self.assertIn("patch", allowed_actions("running"))
        self.assertNotIn("verify", allowed_actions("running"))
        self.assertIn("verify", allowed_actions("running", has_patch=True))
        self.assertNotIn("publish", allowed_actions("submission_approved"))
        self.assertIn("publish", allowed_actions("submission_approved", has_package=True))
        self.assertEqual(allowed_actions("paid"), {"inspect"})


class WorkflowConfigurationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.settings = normalize_settings({"workspace": folder.name,
            "github_token_env": "BH_CUSTOM_TOKEN", "timeout_seconds": 45,
            "sources": {"bountypay": True}})

    def test_disabled_algora_never_reads_discovery_file(self):
        args = scan_arguments(self.settings)
        args.no_bountypay = True
        args.no_github_enrichment = True
        with patch.object(cli, "_discovered_algora_orgs", side_effect=AssertionError("disabled source read")):
            self.assertEqual(cli._collect(args), [])
        self.assertEqual(args.source_health[0]["status"], "disabled")

    def test_provider_outage_does_not_stop_other_sources_or_progress(self):
        settings = dict(self.settings, sources={"bountypay": True, "boss": True})
        stages = []
        with patch("bounty_hunter.sources.bountypay.BountyPayClient.fetch", side_effect=RuntimeError("HTTP 500")), \
             patch("bounty_hunter.sources.boss.BossClient.fetch", return_value=([], False)):
            result = run_scan(settings, progress=stages.append)
        health = {row["source"]: row for row in result["source_health"]}
        self.assertEqual(health["bountypay"]["status"], "error")
        self.assertEqual(health["boss"]["status"], "fresh")
        self.assertEqual(result["count"], 0)
        self.assertTrue(any("Boss" in stage for stage in stages))

    def test_patch_uses_configured_token_timeout_and_workspace(self):
        with patch.dict(os.environ, {"BH_CUSTOM_TOKEN": "test-only-token"}), \
             patch("bounty_hunter.github.GitHubClient") as client, \
             patch("bounty_hunter.attempt.generate_patch", return_value={}) as generate:
            perform_step(self.settings, 1, "patch")
        self.assertEqual(client.call_args.kwargs["token"], "test-only-token")
        self.assertEqual(client.call_args.kwargs["timeout_seconds"], 45)
        self.assertEqual(client.call_args.kwargs["cache_ttl_seconds"], 0)
        self.assertTrue(Path(client.call_args.kwargs["cache_path"]).is_relative_to(Path(self.settings["workspace"])))
        self.assertIs(generate.call_args.kwargs["github_client"], client.return_value)

    def test_missing_runtime_leaves_approved_attempt_unchanged(self):
        history = History(str(workspace_paths(self.settings)["db"]))
        aid = history.queue_from_record(eligible_record())
        history.approve(aid, "Approved fixture", history.attempt_record_sha256(aid))
        history.close()
        with patch("bounty_hunter.isolation.IsolatedRunner.require_available", side_effect=RuntimeError("Start Docker")), \
             patch("bounty_hunter.preflight.run_preflight") as preflight:
            with self.assertRaisesRegex(RuntimeError, "Start Docker"):
                perform_step(self.settings, aid, "preflight")
            preflight.assert_not_called()
        history = History(str(workspace_paths(self.settings)["db"]))
        try:
            self.assertEqual(history.get_attempt(aid)["status"], "approved")
        finally:
            history.close()


if __name__ == "__main__":
    unittest.main()
