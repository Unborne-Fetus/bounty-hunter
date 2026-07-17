#!/usr/bin/env python3
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bounty_hunter.history import History
from bounty_hunter.agent import CodexContextPatchAgent
from bounty_hunter.isolation import (
    IsolatedRunner,
    resolve_image_reference,
)
from bounty_hunter.review import create_review_bundle


RECORD = {
    "opportunity": {
        "source": "validation",
        "source_id": "controlled-python-bug",
        "title": "Controlled addition regression",
        "bounty_usd": 100,
    },
    "score": {
        "eligible": True,
        "decision": "approve_for_human_review",
        "expected_value_usd": 50,
        "expected_hourly_return_usd": 25,
        "probability_valid": 1,
        "probability_verified": 1,
        "probability_win_merge": 1,
    },
}


def run(command):
    return subprocess.run(command, check=True, capture_output=True, text=True)


def write_fixture(repository: Path) -> None:
    (repository / "calculator.py").write_text(
        "def add(left, right):\n    return left - right\n",
        encoding="utf-8",
    )
    run(["git", "init", "-q", str(repository)])
    run(["git", "-C", str(repository), "config", "user.name", "Validation"])
    run(
        [
            "git",
            "-C",
            str(repository),
            "config",
            "user.email",
            "validation@example.test",
        ]
    )
    run(["git", "-C", str(repository), "add", "calculator.py"])
    run(["git", "-C", str(repository), "commit", "-qm", "buggy baseline"])


def add_agent_patch(repository: Path) -> None:
    (repository / "calculator.py").write_text(
        "def add(left, right):\n    return left + right\n",
        encoding="utf-8",
    )
    tests = repository / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / "test_calculator.py").write_text(
        "import unittest\n"
        "from calculator import add\n\n"
        "class CalculatorTests(unittest.TestCase):\n"
        "    def test_adds_positive_numbers(self):\n"
        "        self.assertEqual(add(5, 3), 8)\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n",
        encoding="utf-8",
    )


def copy_regression_tests(repository: Path, baseline: Path) -> None:
    candidates = set(repository.glob("test*.py"))
    candidates.update(repository.glob("tests/**/*.py"))
    copied = 0
    for source in sorted(candidates):
        if not source.is_file():
            continue
        relative = source.relative_to(repository)
        destination = baseline / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied += 1
    if copied == 0:
        raise ValueError("agent patch did not add a discoverable regression test")


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--use-codex-agent",
        action="store_true",
        help="Use a real ephemeral Codex patch session instead of the deterministic fixture patch.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path("data/attempts").resolve()
    root.mkdir(parents=True, exist_ok=True)
    artifacts_root = Path("reports/isolation-validation-artifacts").resolve()
    if artifacts_root.exists():
        shutil.rmtree(artifacts_root)
    artifacts_root.mkdir(parents=True)
    with tempfile.TemporaryDirectory(dir=root) as temporary:
        workspace = Path(temporary)
        repository = workspace / "repository"
        baseline = workspace / "baseline"
        repository.mkdir()
        baseline.mkdir()
        write_fixture(repository)

        history = History(str(workspace / "history.db"))
        try:
            attempt_id = history.queue_from_record(RECORD)
            history.approve(
                attempt_id,
                "Controlled fixture approved; no external submission.",
                history.attempt_record_sha256(attempt_id),
            )
            runner = IsolatedRunner(history)
            runner.begin_attempt(attempt_id)

            image = resolve_image_reference("docker", "python:3.12-alpine")
            agent_result = None
            if args.use_codex_agent:
                agent_result = CodexContextPatchAgent(history).run(
                    attempt_id,
                    str(repository),
                    (
                        "The add(left, right) function subtracts instead of "
                        "adding. Correct the implementation and add a unittest "
                        "regression test proving add(5, 3) returns 8."
                    ),
                    str(artifacts_root),
                    timeout_seconds=1800,
                )
                if agent_result.returncode != 0:
                    raise RuntimeError(
                        f"Codex patch agent failed: {agent_result.final_message}"
                    )
            else:
                add_agent_patch(repository)

            run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "archive",
                    "--format=tar",
                    "HEAD",
                    "-o",
                    str(workspace / "baseline.tar"),
                ]
            )
            run(["tar", "-xf", str(workspace / "baseline.tar"), "-C", str(baseline)])
            copy_regression_tests(repository, baseline)

            command = ["python", "-m", "unittest", "discover", "-v"]
            baseline_result = runner.run_command(
                attempt_id, str(baseline), image, command
            )
            fixed_result = runner.run_command(
                attempt_id, str(repository), image, command
            )
            bundle = create_review_bundle(
                history,
                attempt_id,
                str(repository),
                str(artifacts_root),
                verification_command=command,
                verification_exit_code=fixed_result.returncode,
                verification_stdout=fixed_result.stdout,
                verification_stderr=fixed_result.stderr,
                baseline_exit_code=baseline_result.returncode,
                baseline_stdout=baseline_result.stdout,
                baseline_stderr=baseline_result.stderr,
                image_reference=image,
            )
            outcome = {
                "attempt_id": attempt_id,
                "runtime": runner.runtime,
                "image": image,
                "baseline_exit_code": baseline_result.returncode,
                "baseline_stdout": baseline_result.stdout,
                "baseline_stderr": baseline_result.stderr,
                "fixed_exit_code": fixed_result.returncode,
                "fixed_stdout": fixed_result.stdout,
                "fixed_stderr": fixed_result.stderr,
                "attempt_status": history.get_attempt(attempt_id)["status"],
                "review_bundle": bundle,
                "agent": (
                    {
                        "used": True,
                        "returncode": agent_result.returncode,
                        "final_message": agent_result.final_message,
                        "event_log_path": agent_result.event_log_path,
                    }
                    if agent_result
                    else {"used": False, "mode": "deterministic fixture patch"}
                ),
            }
            Path("reports/isolation-validation.json").write_text(
                json.dumps(outcome, indent=2) + "\n",
                encoding="utf-8",
            )
            print(json.dumps(outcome, indent=2))
            if baseline_result.returncode == 0:
                return 1
            if fixed_result.returncode != 0:
                return 1
            if outcome["attempt_status"] != "verified":
                return 1
            return 0
        finally:
            history.close()


if __name__ == "__main__":
    raise SystemExit(main())
