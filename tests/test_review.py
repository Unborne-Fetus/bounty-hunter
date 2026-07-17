import subprocess
import tempfile
import unittest
from pathlib import Path

from bounty_hunter.history import History
from bounty_hunter.review import create_review_bundle
from tests.test_history import RECORD


class ReviewBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.repository = root / "repository"
        self.artifacts = root / "artifacts"
        self.repository.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(self.repository)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repository), "config", "user.name", "Test"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repository),
                "config",
                "user.email",
                "test@example.test",
            ],
            check=True,
        )
        (self.repository / "parser.py").write_text(
            "def parse(value):\n    return value\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(self.repository), "add", "parser.py"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repository), "commit", "-qm", "initial"],
            check=True,
        )
        self.history = History(str(root / "history.db"))
        self.attempt_id = self.history.queue_from_record(RECORD)
        self.history.approve(
            self.attempt_id,
            "Approved.",
            self.history.attempt_record_sha256(self.attempt_id),
        )
        self.history.transition(self.attempt_id, "running")

    def tearDown(self):
        self.history.close()
        self.temporary.cleanup()

    def test_bundle_binds_diff_and_verification_logs(self):
        (self.repository / "parser.py").write_text(
            "def parse(value):\n    return value.strip()\n",
            encoding="utf-8",
        )
        (self.repository / "test_parser.py").write_text(
            "from parser import parse\n",
            encoding="utf-8",
        )
        result = create_review_bundle(
            self.history,
            self.attempt_id,
            str(self.repository),
            str(self.artifacts),
            verification_command=["python", "-m", "unittest"],
            verification_exit_code=0,
            verification_stdout="OK\n",
            verification_stderr="",
            baseline_exit_code=1,
            baseline_stdout="FAILED\n",
            baseline_stderr="",
            image_reference="python@sha256:example",
        )
        self.assertEqual(
            self.history.get_attempt(self.attempt_id)["status"], "verified"
        )
        self.assertTrue(Path(result["path"], "manifest.json").exists())
        self.assertTrue(Path(result["path"], "changes.patch").exists())
        patch = Path(result["path"], "changes.patch").read_text(
            encoding="utf-8"
        )
        self.assertIn("test_parser.py", patch)
        self.history.approve_submission(
            self.attempt_id,
            "Reviewed.",
            result["sha256"],
        )
        self.assertEqual(
            self.history.get_attempt(self.attempt_id)["status"],
            "submission_approved",
        )

    def test_approval_rejects_wrong_bundle_hash(self):
        self.history.transition(
            self.attempt_id,
            "abandoned",
            outcome_note="No valid patch.",
        )
        with self.assertRaisesRegex(ValueError, "latest review bundle"):
            self.history.approve_submission(
                self.attempt_id,
                "Reviewed.",
                "wrong",
            )

    def test_bundle_rejects_generated_bytecode(self):
        cache = self.repository / "__pycache__"
        cache.mkdir()
        (cache / "parser.pyc").write_bytes(b"generated")
        with self.assertRaisesRegex(ValueError, "generated artifact"):
            create_review_bundle(
                self.history,
                self.attempt_id,
                str(self.repository),
                str(self.artifacts),
                verification_command=["python", "-m", "unittest"],
                verification_exit_code=0,
                verification_stdout="OK\n",
                verification_stderr="",
                baseline_exit_code=1,
                baseline_stdout="FAILED\n",
                baseline_stderr="",
            )

    def test_revision_bundle_does_not_overwrite_prior_approval(self):
        (self.repository / "parser.py").write_text(
            "def parse(value):\n    return value.strip()\n",
            encoding="utf-8",
        )
        first = create_review_bundle(
            self.history,
            self.attempt_id,
            str(self.repository),
            str(self.artifacts),
            verification_command=["python", "-m", "unittest"],
            verification_exit_code=0,
            verification_stdout="OK\n",
            verification_stderr="",
            baseline_exit_code=1,
            baseline_stdout="FAILED\n",
            baseline_stderr="",
        )
        first_manifest = Path(first["path"], "manifest.json").read_bytes()
        self.history.approve_submission(
            self.attempt_id, "First review.", first["sha256"]
        )
        self.history.transition(
            self.attempt_id,
            "submitted",
            pr_url="https://github.com/acme/parser/pull/1",
        )
        self.history.transition(self.attempt_id, "revision")
        self.history.transition(self.attempt_id, "running")
        (self.repository / "parser.py").write_text(
            "def parse(value):\n    return value.strip().lower()\n",
            encoding="utf-8",
        )
        second = create_review_bundle(
            self.history,
            self.attempt_id,
            str(self.repository),
            str(self.artifacts),
            verification_command=["python", "-m", "unittest"],
            verification_exit_code=0,
            verification_stdout="OK again\n",
            verification_stderr="",
            baseline_exit_code=1,
            baseline_stdout="FAILED\n",
            baseline_stderr="",
        )
        self.assertNotEqual(first["path"], second["path"])
        self.assertTrue(first["path"].endswith("review-1"))
        self.assertTrue(second["path"].endswith("review-2"))
        self.assertEqual(
            Path(first["path"], "manifest.json").read_bytes(), first_manifest
        )


if __name__ == "__main__":
    unittest.main()
