import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bounty_hunter.attempt import (
    _regression_test_paths,
    generate_patch,
    generate_revision_patch,
    verify_patch,
)
from bounty_hunter.history import History
from bounty_hunter.review import create_review_bundle
from bounty_hunter.verification import detect_verification_plan


def record(repository="acme/parser"):
    return {
        "opportunity": {
            "source": "manual",
            "source_id": "attempt",
            "title": "Fix parser",
            "source_url": "https://example.test/bounty",
            "bounty_usd": 250,
            "status": "open",
            "deadline": "2030-01-01T00:00:00Z",
            "repository": repository,
            "issue_url": f"https://github.com/{repository}/issues/42",
            "language": "Python",
            "active_attempts": 0,
            "public_repository": True,
            "issue_open": True,
            "issue_activity_recent": True,
            "repository_archived": False,
            "repository_created_at": "2020-01-01T00:00:00Z",
            "repository_stars": 100,
            "repository_license": "MIT",
            "has_test_suite": True,
            "contribution_guide_found": True,
            "acceptance_criteria_clear": True,
            "reproducible": True,
            "maintainer_active": True,
            "competing_prs": 0,
            "security_sensitive": False,
            "estimated_agent_hours": 1,
            "estimated_human_hours": 0.2,
            "estimated_execution_cost_usd": 3,
            "evidence": {},
        },
        "score": {"eligible": True},
    }


def write_artifact(history, attempt_id, kind, path):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    history.add_artifact(attempt_id, kind, str(path), digest)


class AttemptTests(unittest.TestCase):
    def test_extracts_only_recognizable_test_paths(self):
        patch = (
            "diff --git a/src/parser.py b/src/parser.py\n"
            "--- a/src/parser.py\n+++ b/src/parser.py\n@@ -1 +1 @@\n-a\n+b\n"
            "diff --git a/tests/test_parser.py b/tests/test_parser.py\n"
            "--- /dev/null\n+++ b/tests/test_parser.py\n@@ -0,0 +1 @@\n+x\n"
        )
        self.assertEqual(
            _regression_test_paths(patch), ["tests/test_parser.py"]
        )

    def test_generate_patch_rechecks_issue_and_records_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            reports = root / "reports"
            source.mkdir()
            (source / "parser.py").write_text("VALUE = 1\n", encoding="utf-8")
            history = History(str(root / "history.db"))
            try:
                attempt_id = history.queue_from_record(record())
                history.approve(
                    attempt_id,
                    "Approved.",
                    history.attempt_record_sha256(attempt_id),
                )
                history.transition(attempt_id, "running")
                preflight_path = root / "preflight.json"
                preflight_path.write_text(
                    json.dumps(
                        {
                            "status": "passed",
                            "source_path": str(source),
                            "commit": "a" * 40,
                            "clone": {"image": "git@sha256:pinned"},
                        }
                    ),
                    encoding="utf-8",
                )
                write_artifact(history, attempt_id, "preflight", preflight_path)

                def transport(url, _headers):
                    if url.endswith("/repos/acme/parser"):
                        return {
                            "full_name": "acme/parser",
                            "private": False,
                            "archived": False,
                            "disabled": False,
                            "language": "Python",
                            "pushed_at": "2030-01-01T00:00:00Z",
                            "created_at": "2020-01-01T00:00:00Z",
                            "stargazers_count": 100,
                            "license": {"spdx_id": "MIT"},
                            "default_branch": "main",
                        }
                    if url.endswith("/issues/42"):
                        return {
                            "state": "open",
                            "title": "Fix parser",
                            "body": (
                                "Steps to reproduce the parser failure. "
                                "Expected behavior: parsing succeeds. " * 4
                            ),
                            "labels": [{"name": "bug"}],
                            "updated_at": "2030-01-01T00:00:00Z",
                        }
                    if "/git/trees/main" in url:
                        return {
                            "tree": [
                                {"path": "tests/test_parser.py"},
                                {"path": "CONTRIBUTING.md"},
                            ]
                        }
                    if "/timeline?" in url or "/comments?" in url:
                        return []
                    raise AssertionError(url)

                class Runner:
                    def run_command(self, *_args, **_kwargs):
                        return SimpleNamespace(
                            returncode=0, stdout="a" * 40 + "\n", stderr=""
                        )

                class Agent:
                    def run(
                        self,
                        _attempt_id,
                        _repository,
                        issue_text,
                        output_root,
                    ):
                        self.issue_text = issue_text
                        directory = Path(output_root) / f"attempt-{attempt_id}"
                        directory.mkdir(parents=True, exist_ok=True)
                        patch_path = directory / "agent.patch"
                        patch_path.write_text(
                            "diff --git a/test_parser.py b/test_parser.py\n"
                            "--- /dev/null\n+++ b/test_parser.py\n"
                            "@@ -0,0 +1 @@\n+assert True\n",
                            encoding="utf-8",
                        )
                        write_artifact(
                            history, attempt_id, "agent_patch", patch_path
                        )
                        return SimpleNamespace(returncode=0, final_message="done")

                from bounty_hunter.github import GitHubClient

                agent = Agent()
                result = generate_patch(
                    history,
                    attempt_id,
                    str(reports),
                    github_client=GitHubClient(transport=transport),
                    agent=agent,
                    runner=Runner(),
                )
                self.assertIn("Expected behavior", agent.issue_text)
                self.assertTrue(Path(result["issue_snapshot"]).exists())
                self.assertIsNotNone(
                    history.latest_artifact(attempt_id, "issue_snapshot")
                )
            finally:
                history.close()

    def test_verify_patch_proves_failure_then_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            source = workspace / "source"
            reports = root / "reports"
            source.mkdir(parents=True)
            (source / "calculator.py").write_text(
                "def add(left, right):\n    return left - right\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(
                ["git", "-C", str(source), "config", "user.name", "Test"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(source),
                    "config",
                    "user.email",
                    "test@example.test",
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(source), "add", "calculator.py"], check=True
            )
            subprocess.run(
                ["git", "-C", str(source), "commit", "-qm", "baseline"],
                check=True,
            )
            commit = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            (source / "calculator.py").write_text(
                "def add(left, right):\n    return left + right\n",
                encoding="utf-8",
            )
            (source / "test_calculator.py").write_text(
                "import unittest\nfrom calculator import add\n\n"
                "class Tests(unittest.TestCase):\n"
                "    def test_add(self):\n"
                "        self.assertEqual(add(5, 3), 8)\n",
                encoding="utf-8",
            )
            plan = detect_verification_plan(str(source))
            history = History(str(root / "history.db"))
            try:
                attempt_id = history.queue_from_record(record())
                history.approve(
                    attempt_id,
                    "Approved.",
                    history.attempt_record_sha256(attempt_id),
                )
                history.transition(attempt_id, "running")
                preflight_path = root / "preflight.json"
                preflight_path.write_text(
                    json.dumps(
                        {
                            "status": "passed",
                            "workspace": str(workspace),
                            "source_path": str(source),
                            "commit": commit,
                            "clone": {"image": "git@sha256:pinned"},
                            "execution_image": "python@sha256:pinned",
                            "preparation": None,
                            "verification_plan": plan.to_dict(),
                        }
                    ),
                    encoding="utf-8",
                )
                write_artifact(history, attempt_id, "preflight", preflight_path)
                patch_dir = reports / f"attempt-{attempt_id}"
                patch_dir.mkdir(parents=True)
                patch_path = patch_dir / "agent.patch"
                patch_path.write_text(
                    "diff --git a/calculator.py b/calculator.py\n"
                    "--- a/calculator.py\n+++ b/calculator.py\n"
                    "@@ -1,2 +1,2 @@\n def add(left, right):\n"
                    "-    return left - right\n+    return left + right\n"
                    "diff --git a/test_calculator.py b/test_calculator.py\n"
                    "--- /dev/null\n+++ b/test_calculator.py\n"
                    "@@ -0,0 +1 @@\n+regression test\n",
                    encoding="utf-8",
                )
                write_artifact(history, attempt_id, "agent_patch", patch_path)

                class Runner:
                    def clone_baseline(
                        self, _attempt_id, source_path, destination, _image
                    ):
                        Path(destination).mkdir(parents=True)
                        result = subprocess.run(
                            [
                                "git",
                                "clone",
                                "-q",
                                "--no-hardlinks",
                                source_path,
                                str(Path(destination) / "baseline"),
                            ],
                            check=False,
                            capture_output=True,
                            text=True,
                        )
                        return result

                    def run_command(
                        self, _attempt_id, source_path, _image, command, **_kwargs
                    ):
                        baseline = "baseline-checkout" in source_path
                        return SimpleNamespace(
                            returncode=1 if baseline else 0,
                            stdout="",
                            stderr="FAILED\n" if baseline else "OK\n",
                        )

                result = verify_patch(
                    history,
                    attempt_id,
                    str(reports),
                    runner=Runner(),
                )
                self.assertEqual(result["status"], "verified")
                self.assertEqual(
                    result["regression_test_paths"], ["test_calculator.py"]
                )
                self.assertEqual(result["baseline_results"][0]["exit_code"], 1)
                self.assertEqual(result["fixed_results"][0]["exit_code"], 0)
            finally:
                history.close()

    def test_revision_starts_from_approved_bundle_and_records_feedback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            source = workspace / "source"
            reports = root / "reports"
            source.mkdir(parents=True)
            (source / "parser.py").write_text(
                "def parse(value):\n    return value\n", encoding="utf-8"
            )
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(
                ["git", "-C", str(source), "add", "parser.py"], check=True
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(source),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.test",
                    "commit",
                    "-qm",
                    "baseline",
                ],
                check=True,
            )
            (source / "parser.py").write_text(
                "def parse(value):\n    return value.strip()\n",
                encoding="utf-8",
            )
            history = History(str(root / "history.db"))
            try:
                attempt_id = history.queue_from_record(record())
                history.approve(
                    attempt_id,
                    "Approved.",
                    history.attempt_record_sha256(attempt_id),
                )
                history.transition(attempt_id, "running")
                preflight_path = root / "preflight.json"
                preflight_path.write_text(
                    json.dumps(
                        {
                            "status": "passed",
                            "workspace": str(workspace),
                            "source_path": str(source),
                        }
                    ),
                    encoding="utf-8",
                )
                write_artifact(history, attempt_id, "preflight", preflight_path)
                bundle = create_review_bundle(
                    history,
                    attempt_id,
                    str(source),
                    str(reports),
                    verification_command=["python", "-m", "unittest"],
                    verification_exit_code=0,
                    verification_stdout="OK\n",
                    verification_stderr="",
                    baseline_exit_code=1,
                    baseline_stdout="FAILED\n",
                    baseline_stderr="",
                )
                history.approve_submission(
                    attempt_id, "Approved first bundle.", bundle["sha256"]
                )
                history.transition(
                    attempt_id,
                    "submitted",
                    pr_url="https://github.com/acme/parser/pull/1",
                )
                history.transition(
                    attempt_id,
                    "revision",
                    outcome_note="Maintainer requested lowercase output.",
                )
                feedback = root / "feedback.txt"
                feedback.write_text(
                    "Please lowercase the stripped parser output.", encoding="utf-8"
                )

                class Agent:
                    def run(
                        self,
                        _attempt_id,
                        repository,
                        prompt,
                        output_root,
                    ):
                        self.prompt = prompt
                        Path(repository, "parser.py").write_text(
                            "def parse(value):\n    return value.strip().lower()\n",
                            encoding="utf-8",
                        )
                        directory = Path(output_root) / f"attempt-{attempt_id}"
                        directory.mkdir(parents=True, exist_ok=True)
                        patch = directory / "revision.patch"
                        patch.write_text(
                            "diff --git a/parser.py b/parser.py\n"
                            "--- a/parser.py\n+++ b/parser.py\n"
                            "@@ -1,2 +1,2 @@\n def parse(value):\n"
                            "-    return value.strip()\n"
                            "+    return value.strip().lower()\n",
                            encoding="utf-8",
                        )
                        write_artifact(history, attempt_id, "agent_patch", patch)
                        return SimpleNamespace(
                            returncode=0, final_message="Applied feedback."
                        )

                agent = Agent()
                result = generate_revision_patch(
                    history,
                    attempt_id,
                    str(feedback),
                    str(reports),
                    agent=agent,
                )
                self.assertIn("lowercase", agent.prompt)
                self.assertEqual(
                    history.get_attempt(attempt_id)["status"], "running"
                )
                self.assertEqual(
                    result["feedback_artifact"]["kind"], "revision_feedback"
                )
            finally:
                history.close()


if __name__ == "__main__":
    unittest.main()
