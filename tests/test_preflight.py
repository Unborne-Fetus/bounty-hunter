import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bounty_hunter.history import History
from bounty_hunter.preflight import _repository_url, run_preflight


def eligible_record():
    return {
        "opportunity": {
            "source": "manual",
            "source_id": "preflight",
            "title": "Fix parser",
            "source_url": "https://example.test/bounty/preflight",
            "bounty_usd": 250,
            "status": "open",
            "deadline": "2030-01-01T00:00:00Z",
            "repository": "acme/parser",
            "public_repository": True,
            "issue_open": True,
            "issue_activity_recent": True,
            "repository_archived": False,
            "repository_created_at": "2020-01-01T00:00:00Z",
            "repository_stars": 100,
            "repository_license": "MIT",
            "language": "Python",
            "has_test_suite": True,
            "contribution_guide_found": True,
            "acceptance_criteria_clear": True,
            "reproducible": True,
            "maintainer_active": True,
            "competing_prs": 0,
            "security_sensitive": False,
            "submission_count": 0,
            "estimated_agent_hours": 1.5,
            "estimated_human_hours": 0.2,
            "estimated_execution_cost_usd": 5.0,
            "evidence": {},
        },
        "score": {"eligible": True},
    }


def fresh_validation(record):
    return {
        "checked_at": "2026-07-16T00:00:00+00:00",
        "funding": {"source": "algora", "fresh": True, "bounty_usd": 250},
        "opportunity": record["opportunity"],
        "score": record["score"],
    }


class PreflightTests(unittest.TestCase):
    def test_repository_url_is_derived_from_queued_evidence(self):
        self.assertEqual(
            _repository_url(eligible_record()),
            "https://github.com/acme/parser.git",
        )

    def test_stale_or_rejected_evidence_is_refused(self):
        record = eligible_record()
        record["opportunity"]["evidence"]["github_evidence_stale"] = True
        with self.assertRaisesRegex(ValueError, "stale"):
            _repository_url(record)
        record = eligible_record()
        record["score"]["eligible"] = False
        with self.assertRaisesRegex(ValueError, "passed scoring"):
            _repository_url(record)

    def test_current_policy_recheck_refuses_old_eligible_snapshot(self):
        record = eligible_record()
        record["opportunity"]["repository_stars"] = 0
        with self.assertRaisesRegex(ValueError, "current policy recheck"):
            _repository_url(record)

    def test_preflight_refuses_record_changed_after_digest_bound_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            history = History(f"{temporary}/history.db")
            try:
                attempt_id = history.queue_from_record(eligible_record())
                history.approve(
                    attempt_id,
                    "Approved exact record.",
                    history.attempt_record_sha256(attempt_id),
                )
                changed = eligible_record()["opportunity"]
                changed["title"] = "Changed after approval"
                history.connection.execute(
                    "UPDATE attempts SET opportunity_json = ? WHERE id = ?",
                    (json.dumps(changed, sort_keys=True), attempt_id),
                )
                history.connection.commit()
                with self.assertRaisesRegex(ValueError, "not bound"):
                    run_preflight(
                        history,
                        attempt_id,
                        f"{temporary}/workspaces",
                        f"{temporary}/reports",
                    )
            finally:
                history.close()

    def test_preflight_requires_explicit_approval_before_runtime_use(self):
        with tempfile.TemporaryDirectory() as temporary:
            history = History(f"{temporary}/history.db")
            try:
                attempt_id = history.queue_from_record(eligible_record())
                with self.assertRaisesRegex(ValueError, "explicit approval"):
                    run_preflight(
                        history,
                        attempt_id,
                        f"{temporary}/workspaces",
                        f"{temporary}/reports",
                    )
                self.assertEqual(
                    history.get_attempt(attempt_id)["status"], "queued"
                )
            finally:
                history.close()

    def test_freshness_failure_abandons_before_workspace_or_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            history = History(f"{temporary}/history.db")
            try:
                attempt_id = history.queue_from_record(eligible_record())
                history.approve(
                    attempt_id,
                    "Approved exact record.",
                    history.attempt_record_sha256(attempt_id),
                )

                def fail(_record):
                    raise ValueError("bounty is no longer funded")

                workspace_root = f"{temporary}/workspaces"
                with self.assertRaisesRegex(ValueError, "no longer funded"):
                    run_preflight(
                        history,
                        attempt_id,
                        workspace_root,
                        f"{temporary}/reports",
                        fresh_validator=fail,
                    )
                self.assertFalse(
                    (Path(workspace_root) / f"attempt-{attempt_id}").exists()
                )
                self.assertEqual(
                    history.get_attempt(attempt_id)["status"], "abandoned"
                )
                artifact = history.latest_artifact(
                    attempt_id, "preflight_validation"
                )
                self.assertIsNotNone(artifact)
                payload = json.loads(Path(artifact["path"]).read_text())
                self.assertEqual(payload["status"], "rejected_before_execution")
            finally:
                history.close()

    def test_successful_preflight_persists_pinned_provenance(self):
        class Runner:
            runtime = "controlled-container"

            def __init__(self, history):
                self.history = history

            def require_available(self):
                return self.runtime

            def begin_attempt(self, attempt_id):
                self.history.transition(attempt_id, "running")

            def clone_public_repository(
                self, _attempt_id, _url, destination, _image
            ):
                source = Path(destination) / "source"
                (source / "tests").mkdir(parents=True)
                return SimpleNamespace(returncode=0, stdout="cloned\n", stderr="")

            def run_command(
                self,
                _attempt_id,
                _source,
                _image,
                command,
                **_kwargs,
            ):
                if command == ["rev-parse", "HEAD"]:
                    return SimpleNamespace(
                        returncode=0,
                        stdout="a" * 40 + "\n",
                        stderr="",
                    )
                return SimpleNamespace(returncode=0, stdout="OK\n", stderr="")

        with tempfile.TemporaryDirectory() as temporary:
            history = History(f"{temporary}/history.db")
            try:
                attempt_id = history.queue_from_record(eligible_record())
                history.approve(
                    attempt_id,
                    "Approved.",
                    history.attempt_record_sha256(attempt_id),
                )
                result = run_preflight(
                    history,
                    attempt_id,
                    f"{temporary}/workspaces",
                    f"{temporary}/reports",
                    runner=Runner(history),
                    image_resolver=lambda _runtime, image: f"{image}@sha256:pinned",
                    fresh_validator=fresh_validation,
                )
                self.assertEqual(result["status"], "passed")
                self.assertEqual(result["commit"], "a" * 40)
                self.assertIn("@sha256:pinned", result["execution_image"])
                self.assertTrue(result["validation_sha256"])
                self.assertEqual(
                    history.get_attempt(attempt_id)["status"], "running"
                )
                artifact = history.latest_artifact(attempt_id, "preflight")
                self.assertEqual(artifact["sha256"], result["sha256"])
            finally:
                history.close()


if __name__ == "__main__":
    unittest.main()
