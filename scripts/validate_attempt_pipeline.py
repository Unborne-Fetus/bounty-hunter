#!/usr/bin/env python3
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bounty_hunter.attempt import generate_patch, verify_patch
from bounty_hunter.github import GitHubClient
from bounty_hunter.history import History
from bounty_hunter.isolation import IsolatedRunner, resolve_image_reference
from bounty_hunter.submission import prepare_submission, submission_package_sha256
from bounty_hunter.verification import detect_verification_plan


RECORD = {
    "opportunity": {
        "source": "validation",
        "source_id": "controlled-attempt-pipeline",
        "title": "Fix addition regression",
        "source_url": "https://example.test/controlled-bounty",
        "bounty_usd": 250,
        "status": "open",
        "deadline": "2030-01-01T00:00:00Z",
        "repository": "acme/calculator",
        "issue_url": "https://github.com/acme/calculator/issues/42",
        "language": "Python",
        "active_attempts": 0,
        "public_repository": True,
        "issue_open": True,
        "repository_archived": False,
        "has_test_suite": True,
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


def run(command):
    return subprocess.run(command, check=True, capture_output=True, text=True)


def write_fixture(source: Path) -> str:
    (source / "calculator.py").write_text(
        "def add(left, right):\n    return left - right\n",
        encoding="utf-8",
    )
    (source / "tests").mkdir()
    (source / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (source / "tests" / "test_calculator.py").write_text(
        "import unittest\nfrom calculator import add\n\n"
        "class CalculatorTests(unittest.TestCase):\n"
        "    def test_identity(self):\n"
        "        self.assertEqual(add(1, 0), 1)\n",
        encoding="utf-8",
    )
    run(["git", "init", "-q", str(source)])
    run(["git", "-C", str(source), "config", "user.name", "Validation"])
    run(
        [
            "git",
            "-C",
            str(source),
            "config",
            "user.email",
            "validation@example.test",
        ]
    )
    run(["git", "-C", str(source), "add", "."])
    run(["git", "-C", str(source), "commit", "-qm", "buggy baseline"])
    return run(["git", "-C", str(source), "rev-parse", "HEAD"]).stdout.strip()


def transport(url, _headers):
    if url.endswith("/repos/acme/calculator"):
        return {
            "full_name": "acme/calculator",
            "private": False,
            "archived": False,
            "disabled": False,
            "language": "Python",
            "pushed_at": "2030-01-01T00:00:00Z",
            "default_branch": "main",
        }
    if url.endswith("/issues/42"):
        return {
            "state": "open",
            "title": "add() subtracts instead of adding",
            "body": (
                "Steps to reproduce: call add(5, 3). It returns 2.\n"
                "Expected behavior: add(5, 3) must return 8.\n"
                "Requirements: correct the implementation and add a unittest "
                "regression test proving this exact case."
            ),
            "labels": [{"name": "bug"}],
            "updated_at": "2030-01-01T00:00:00Z",
        }
    if "/git/trees/main" in url:
        return {"tree": [{"path": "tests/test_calculator.py"}]}
    if "/timeline?" in url or "/comments?" in url:
        return []
    raise AssertionError(f"unexpected URL: {url}")


def main() -> int:
    artifacts = Path("reports/attempt-pipeline-validation-artifacts").resolve()
    if artifacts.exists():
        shutil.rmtree(artifacts)
    artifacts.mkdir(parents=True)
    attempts_root = Path("data/attempts").resolve()
    attempts_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=attempts_root) as temporary:
        workspace = Path(temporary)
        source = workspace / "source"
        source.mkdir()
        commit = write_fixture(source)
        history = History(str(workspace / "history.db"))
        try:
            attempt_id = history.queue_from_record(RECORD)
            history.approve(
                attempt_id,
                "Controlled fixture approved.",
                history.attempt_record_sha256(attempt_id),
            )
            runner = IsolatedRunner(history)
            runner.begin_attempt(attempt_id)
            plan = detect_verification_plan(str(source))
            git_image = resolve_image_reference(
                runner.require_available(), "alpine/git:2.47.2"
            )
            execution_image = resolve_image_reference(
                runner.require_available(), plan.image
            )
            baseline = runner.run_command(
                attempt_id,
                str(source),
                execution_image,
                plan.verification_commands[0],
            )
            if baseline.returncode != 0:
                raise RuntimeError("controlled preflight baseline did not pass")
            preflight = {
                "schema_version": 1,
                "attempt_id": attempt_id,
                "repository_url": "https://github.com/acme/calculator.git",
                "workspace": str(workspace),
                "source_path": str(source),
                "status": "passed",
                "commit": commit,
                "clone": {"image": git_image, "exit_code": 0},
                "execution_image": execution_image,
                "preparation": None,
                "verification_plan": plan.to_dict(),
                "verification": [
                    {
                        "command": plan.verification_commands[0],
                        "exit_code": baseline.returncode,
                        "stdout": baseline.stdout,
                        "stderr": baseline.stderr,
                    }
                ],
            }
            preflight_path = artifacts / f"attempt-{attempt_id}" / "preflight.json"
            preflight_path.parent.mkdir(parents=True)
            preflight_path.write_text(
                json.dumps(preflight, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            history.add_artifact(
                attempt_id,
                "preflight",
                str(preflight_path),
                hashlib.sha256(preflight_path.read_bytes()).hexdigest(),
            )
            patch = generate_patch(
                history,
                attempt_id,
                str(artifacts),
                github_client=GitHubClient(transport=transport),
                runner=runner,
            )
            verification = verify_patch(
                history,
                attempt_id,
                str(artifacts),
                runner=runner,
            )
            first_bundle = verification["review_bundle"]
            history.approve_submission(
                attempt_id,
                "Controlled first bundle approved.",
                first_bundle["sha256"],
            )
            history.transition(
                attempt_id,
                "submitted",
                pr_url="https://github.com/acme/calculator/pull/1",
                outcome_note="Controlled submission; no external write occurred.",
            )
            history.transition(
                attempt_id,
                "revision",
                outcome_note="Controlled maintainer requested a clearer test name.",
            )
            feedback_path = artifacts / f"attempt-{attempt_id}" / "feedback.txt"
            feedback_path.write_text(
                "Rename the regression test method to "
                "test_adds_positive_operands. Make no other behavior changes.",
                encoding="utf-8",
            )
            from bounty_hunter.attempt import generate_revision_patch

            revision = generate_revision_patch(
                history,
                attempt_id,
                str(feedback_path),
                str(artifacts),
            )
            revised_verification = verify_patch(
                history,
                attempt_id,
                str(artifacts),
                runner=runner,
            )
            revised_bundle = revised_verification["review_bundle"]
            history.approve_submission(
                attempt_id,
                "Controlled revised bundle approved.",
                revised_bundle["sha256"],
            )
            prepared_submission = prepare_submission(
                history,
                attempt_id,
                str(artifacts / "submission-checkouts"),
                str(artifacts),
            )
            outcome = {
                "preflight_baseline_exit_code": baseline.returncode,
                "patch": patch,
                "first_verification": verification,
                "revision": revision,
                "revised_verification": revised_verification,
                "prepared_submission": prepared_submission,
                "attempt_status": history.get_attempt(attempt_id)["status"],
                "state_events": history.state_events(attempt_id),
                "agent_metrics": history.attempt_metrics(attempt_id),
            }
            Path("reports/attempt-pipeline-validation.json").write_text(
                json.dumps(outcome, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(json.dumps(outcome, indent=2, sort_keys=True))
            return 0 if (
                outcome["attempt_status"] == "submission_approved"
                and verification["baseline_results"][0]["exit_code"] != 0
                and verification["fixed_results"][0]["exit_code"] == 0
                and revised_verification["baseline_results"][0]["exit_code"] != 0
                and revised_verification["fixed_results"][0]["exit_code"] == 0
                and first_bundle["path"] != revised_bundle["path"]
                and submission_package_sha256(prepared_submission["path"])
                == prepared_submission["sha256"]
                and prepared_submission["approved_bundle_sha256"]
                == revised_bundle["sha256"]
            ) else 1
        finally:
            history.close()


if __name__ == "__main__":
    raise SystemExit(main())
