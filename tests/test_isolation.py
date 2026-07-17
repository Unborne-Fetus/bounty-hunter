import tempfile
import unittest
from pathlib import Path

from bounty_hunter.history import History
from bounty_hunter.isolation import (
    IsolationUnavailable,
    IsolatedRunner,
    baseline_clone_command,
    clone_command,
    execution_command,
    preparation_command,
    resolve_image_reference,
)
from tests.test_history import RECORD


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.history = History(f"{self.temporary.name}/history.db")

    def tearDown(self):
        self.history.close()
        self.temporary.cleanup()

    def test_refuses_execution_without_container_runtime(self):
        attempt_id = self.history.queue_from_record(RECORD)
        self.history.approve(
            attempt_id,
            "Approved.",
            self.history.attempt_record_sha256(attempt_id),
        )
        runner = IsolatedRunner(self.history, runtime=None)
        runner.runtime = None
        with self.assertRaises(IsolationUnavailable):
            runner.begin_attempt(attempt_id)
        self.assertEqual(
            self.history.get_attempt(attempt_id)["status"], "approved"
        )

    def test_begin_attempt_requires_approval(self):
        attempt_id = self.history.queue_from_record(RECORD)
        runner = IsolatedRunner(self.history, runtime="docker")
        with self.assertRaisesRegex(ValueError, "explicit human approval"):
            runner.begin_attempt(attempt_id)

    def test_execution_is_networkless_and_read_only(self):
        command = execution_command(
            "docker",
            self.temporary.name,
            "python:3.12-bookworm",
            ["python", "-m", "pytest"],
        )
        self.assertIn("none", command)
        self.assertIn("--read-only", command)
        self.assertIn("no-new-privileges", command)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", command)
        self.assertNotIn("--privileged", command)

    def test_clone_accepts_only_public_https_github(self):
        command = clone_command(
            "docker",
            "https://github.com/acme/parser.git",
            self.temporary.name,
        )
        self.assertIn("--depth", command)
        with self.assertRaisesRegex(ValueError, "public HTTPS GitHub"):
            clone_command(
                "docker",
                "git@github.com:acme/private.git",
                self.temporary.name,
            )

    def test_baseline_clone_is_networkless_and_source_read_only(self):
        destination = f"{self.temporary.name}/baseline-parent"
        command = baseline_clone_command(
            "docker",
            self.temporary.name,
            destination,
            "alpine/git@sha256:example",
        )
        resolved_source = str(Path(self.temporary.name).resolve())
        resolved_destination = str(Path(destination).resolve())
        self.assertIn("none", command)
        self.assertIn(f"{resolved_source}:/source:ro", command)
        self.assertIn(f"{resolved_destination}:/destination:rw", command)
        self.assertIn("--no-hardlinks", command)

    def test_preparation_is_allowlisted_and_separates_dependencies(self):
        dependencies = f"{self.temporary.name}/dependencies"
        resolved_source = str(Path(self.temporary.name).resolve())
        resolved_dependencies = str(Path(dependencies).resolve())
        command = preparation_command(
            "docker",
            self.temporary.name,
            dependencies,
            "node@sha256:example",
            "javascript",
            ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        )
        self.assertIn("bridge", command)
        self.assertIn(f"{resolved_source}:/workspace:ro", command)
        self.assertIn(f"{resolved_dependencies}:/dependencies:rw", command)
        self.assertEqual(
            command[command.index("--workdir") + 1], "/dependencies"
        )
        self.assertIn("NPM_CONFIG_USERCONFIG=/dev/null", command)
        with self.assertRaisesRegex(ValueError, "not allowlisted"):
            preparation_command(
                "docker",
                self.temporary.name,
                dependencies,
                "node@sha256:example",
                "javascript",
                ["npm", "install"],
            )

    def test_networkless_execution_mounts_prepared_dependencies_read_only(self):
        dependencies = f"{self.temporary.name}/dependencies"
        resolved_dependencies = str(Path(dependencies).resolve())
        command = execution_command(
            "docker",
            self.temporary.name,
            "python@sha256:example",
            ["python", "-m", "pytest"],
            dependency_path=dependencies,
            dependency_stack="python",
        )
        self.assertIn(
            f"{resolved_dependencies}/node_modules:/dependencies/node_modules:ro",
            execution_command(
                "docker",
                self.temporary.name,
                "node@sha256:example",
                ["npm", "run", "test"],
                dependency_path=dependencies,
                dependency_stack="javascript",
            ),
        )
        self.assertIn(f"{resolved_dependencies}:/dependencies:ro", command)
        self.assertIn("PYTHONPATH=/dependencies", command)
        self.assertIn("none", command)

    def test_image_tag_is_resolved_to_digest(self):
        class Result:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        calls = []

        def runner(command, **_kwargs):
            calls.append(command)
            if command[1] == "pull":
                return Result()
            return Result(stdout='["python@sha256:abc"]\n')

        resolved = resolve_image_reference(
            "docker", "python:3.12-alpine", runner
        )
        self.assertEqual(resolved, "python@sha256:abc")
        self.assertEqual(calls[0][:2], ["docker", "pull"])


if __name__ == "__main__":
    unittest.main()
