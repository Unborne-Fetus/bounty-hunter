#!/usr/bin/env python3
import json
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bounty_hunter.history import History
from bounty_hunter.isolation import IsolatedRunner, resolve_image_reference
from bounty_hunter.verification import detect_verification_plan


RECORD = {
    "opportunity": {
        "source": "validation",
        "source_id": "controlled-npm-preparation",
        "title": "Controlled npm dependency preparation",
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


def registry_metadata() -> dict:
    url = "https://registry.npmjs.org/is-number/7.0.0"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "Bounty-Hunter-Validation/1"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.geturl() != url:
            raise ValueError("npm registry metadata request redirected")
        metadata = json.load(response)
    distribution = metadata.get("dist") or {}
    tarball = distribution.get("tarball")
    integrity = distribution.get("integrity")
    if not isinstance(tarball, str) or not tarball.startswith(
        "https://registry.npmjs.org/"
    ):
        raise ValueError("unexpected npm tarball origin")
    if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
        raise ValueError("npm package metadata has no sha512 integrity")
    return {"tarball": tarball, "integrity": integrity}


def pypi_metadata() -> dict:
    url = "https://pypi.org/pypi/idna/3.10/json"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "Bounty-Hunter-Validation/1"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.geturl() != url:
            raise ValueError("PyPI metadata request redirected")
        metadata = json.load(response)
    wheels = [
        item
        for item in metadata.get("urls", [])
        if item.get("filename") == "idna-3.10-py3-none-any.whl"
        and item.get("packagetype") == "bdist_wheel"
    ]
    if len(wheels) != 1:
        raise ValueError("expected one universal idna wheel")
    digest = (wheels[0].get("digests") or {}).get("sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError("PyPI wheel has no sha256 digest")
    return {"sha256": digest}


def write_fixture(repository: Path, distribution: dict) -> None:
    package = {
        "name": "bounty-hunter-preparation-fixture",
        "version": "1.0.0",
        "private": True,
        "scripts": {"test": "node --test"},
        "dependencies": {"is-number": "7.0.0"},
    }
    lock = {
        "name": package["name"],
        "version": package["version"],
        "lockfileVersion": 3,
        "requires": True,
        "packages": {
            "": {
                "name": package["name"],
                "version": package["version"],
                "dependencies": package["dependencies"],
            },
            "node_modules/is-number": {
                "version": "7.0.0",
                "resolved": distribution["tarball"],
                "integrity": distribution["integrity"],
                "engines": {"node": ">=0.12.0"},
            },
        },
    }
    (repository / "package.json").write_text(
        json.dumps(package, indent=2) + "\n", encoding="utf-8"
    )
    (repository / "package-lock.json").write_text(
        json.dumps(lock, indent=2) + "\n", encoding="utf-8"
    )
    (repository / "fixture.test.js").write_text(
        "const test = require('node:test');\n"
        "const assert = require('node:assert/strict');\n"
        "const isNumber = require('is-number');\n\n"
        "test('prepared dependency is available offline', () => {\n"
        "  assert.equal(isNumber(42), true);\n"
        "});\n",
        encoding="utf-8",
    )


def write_python_fixture(repository: Path, distribution: dict) -> None:
    (repository / "requirements.txt").write_text(
        "idna==3.10 \\\n"
        f"  --hash=sha256:{distribution['sha256']}\n",
        encoding="utf-8",
    )
    (repository / "test_idna.py").write_text(
        "import unittest\n"
        "import idna\n\n"
        "class PreparedDependencyTests(unittest.TestCase):\n"
        "    def test_idna_wheel_is_available_offline(self):\n"
        "        self.assertEqual(idna.encode('example.com'), b'example.com')\n",
        encoding="utf-8",
    )


def main() -> int:
    npm_distribution = registry_metadata()
    python_distribution = pypi_metadata()
    root = Path("data/attempts").resolve()
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as temporary:
        workspace = Path(temporary)
        repository = workspace / "repository"
        dependencies = workspace / "dependencies"
        repository.mkdir()
        dependencies.mkdir()
        write_fixture(repository, npm_distribution)
        plan = detect_verification_plan(str(repository))

        history = History(str(workspace / "history.db"))
        try:
            attempt_id = history.queue_from_record(RECORD)
            history.approve(
                attempt_id,
                "Controlled dependency fixture; no external submission.",
                history.attempt_record_sha256(attempt_id),
            )
            runner = IsolatedRunner(history)
            runner.begin_attempt(attempt_id)
            image = resolve_image_reference(runner.require_available(), plan.image)
            preparation = runner.prepare_dependencies(
                attempt_id,
                str(repository),
                str(dependencies),
                image,
                plan.stack,
                plan.install_command or [],
            )
            verification = runner.run_command(
                attempt_id,
                str(repository),
                image,
                plan.verification_commands[0],
                dependency_path=str(dependencies),
                dependency_stack=plan.stack,
            )
            npm_outcome = {
                "runtime": runner.runtime,
                "image": image,
                "preparation_exit_code": preparation.returncode,
                "preparation_stdout": preparation.stdout,
                "preparation_stderr": preparation.stderr,
                "verification_exit_code": verification.returncode,
                "verification_stdout": verification.stdout,
                "verification_stderr": verification.stderr,
                "dependencies_separate_from_repository": not (
                    repository / "node_modules"
                ).exists(),
                "verification_network": "none",
            }
            python_repository = workspace / "python-repository"
            python_dependencies = workspace / "python-dependencies"
            python_repository.mkdir()
            python_dependencies.mkdir()
            write_python_fixture(python_repository, python_distribution)
            python_plan = detect_verification_plan(str(python_repository))
            python_record = json.loads(json.dumps(RECORD))
            python_record["opportunity"]["source_id"] = (
                "controlled-python-preparation"
            )
            python_record["opportunity"]["title"] = (
                "Controlled Python dependency preparation"
            )
            python_attempt_id = history.queue_from_record(python_record)
            history.approve(
                python_attempt_id,
                "Controlled dependency fixture; no external submission.",
                history.attempt_record_sha256(python_attempt_id),
            )
            runner.begin_attempt(python_attempt_id)
            python_image = resolve_image_reference(
                runner.require_available(), python_plan.image
            )
            python_preparation = runner.prepare_dependencies(
                python_attempt_id,
                str(python_repository),
                str(python_dependencies),
                python_image,
                python_plan.stack,
                python_plan.install_command or [],
            )
            python_verification = runner.run_command(
                python_attempt_id,
                str(python_repository),
                python_image,
                python_plan.verification_commands[0],
                dependency_path=str(python_dependencies),
                dependency_stack=python_plan.stack,
            )
            python_outcome = {
                "runtime": runner.runtime,
                "image": python_image,
                "preparation_exit_code": python_preparation.returncode,
                "preparation_stdout": python_preparation.stdout,
                "preparation_stderr": python_preparation.stderr,
                "verification_exit_code": python_verification.returncode,
                "verification_stdout": python_verification.stdout,
                "verification_stderr": python_verification.stderr,
                "dependencies_separate_from_repository": not (
                    python_repository / ".venv"
                ).exists(),
                "verification_network": "none",
                "binary_only": "--only-binary=:all:"
                in (python_plan.install_command or []),
                "hashes_required": "--require-hashes"
                in (python_plan.install_command or []),
            }
            outcome = {"npm": npm_outcome, "python": python_outcome}
            Path("reports/dependency-preparation-validation.json").write_text(
                json.dumps(outcome, indent=2) + "\n", encoding="utf-8"
            )
            print(json.dumps(outcome, indent=2))
            return 0 if all(
                (
                    npm_outcome["preparation_exit_code"] == 0,
                    npm_outcome["verification_exit_code"] == 0,
                    npm_outcome["dependencies_separate_from_repository"],
                    python_outcome["preparation_exit_code"] == 0,
                    python_outcome["verification_exit_code"] == 0,
                    python_outcome["dependencies_separate_from_repository"],
                    python_outcome["binary_only"],
                    python_outcome["hashes_required"],
                )
            ) else 1
        finally:
            history.close()


if __name__ == "__main__":
    raise SystemExit(main())
