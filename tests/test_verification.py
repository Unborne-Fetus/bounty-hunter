import json
import tempfile
import unittest
from pathlib import Path

from bounty_hunter.verification import detect_verification_plan, is_test_path


class VerificationPlanTests(unittest.TestCase):
    def test_recognizes_regression_test_paths(self):
        self.assertTrue(is_test_path("test_parser.py"))
        self.assertTrue(is_test_path("tests/parser.test.ts"))
        self.assertTrue(is_test_path("src/parser.spec.js"))
        self.assertFalse(is_test_path("src/parser.py"))

    def test_detects_dependency_free_python(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "tests").mkdir()
            plan = detect_verification_plan(str(repository))
            self.assertEqual(plan.stack, "python")
            self.assertFalse(plan.requires_network_preparation)

    def test_javascript_requires_lockfile(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "package.json").write_text(
                json.dumps({"scripts": {"test": "node --test"}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "lockfile"):
                detect_verification_plan(str(repository))

    def test_npm_disables_lifecycle_scripts(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "package.json").write_text(
                json.dumps(
                    {
                        "scripts": {
                            "test": "node --test",
                            "lint": "eslint .",
                        }
                    }
                ),
                encoding="utf-8",
            )
            (repository / "package-lock.json").write_text(
                json.dumps(
                    {
                        "lockfileVersion": 3,
                        "packages": {
                            "": {"name": "fixture", "version": "1.0.0"},
                            "node_modules/example": {
                                "version": "1.0.0",
                                "resolved": "https://registry.npmjs.org/example/-/example-1.0.0.tgz",
                                "integrity": "sha512-example",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            plan = detect_verification_plan(str(repository))
            self.assertIn("--ignore-scripts", plan.install_command)
            self.assertEqual(
                plan.verification_commands,
                [["npm", "run", "test"], ["npm", "run", "lint"]],
            )

    def test_npm_rejects_non_registry_dependency(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "package.json").write_text(
                json.dumps({"scripts": {"test": "node --test"}}),
                encoding="utf-8",
            )
            (repository / "package-lock.json").write_text(
                json.dumps(
                    {
                        "lockfileVersion": 3,
                        "packages": {
                            "": {},
                            "node_modules/unsafe": {
                                "resolved": "https://example.test/unsafe.tgz",
                                "integrity": "sha512-example",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "registry.npmjs.org"):
                detect_verification_plan(str(repository))

    def test_python_requires_hash_locked_binary_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "requirements.txt").write_text(
                "example==1.0.0\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "sha256 hashes"):
                detect_verification_plan(str(repository))

            (repository / "requirements.txt").write_text(
                "example==1.0.0 \\\n"
                "  --hash=sha256:" + "a" * 64 + "\n",
                encoding="utf-8",
            )
            plan = detect_verification_plan(str(repository))
            self.assertIn("--require-hashes", plan.install_command)
            self.assertIn("--only-binary=:all:", plan.install_command)
            self.assertIn("--no-deps", plan.install_command)

    def test_yarn_and_pnpm_are_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "package.json").write_text(
                json.dumps({"scripts": {"test": "node --test"}}),
                encoding="utf-8",
            )
            (repository / "yarn.lock").write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not yet accepted"):
                detect_verification_plan(str(repository))


if __name__ == "__main__":
    unittest.main()
