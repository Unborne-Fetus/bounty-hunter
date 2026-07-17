import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse


TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec)(/|$)|"
    r"(^|/)(test_[^/]+\.py|[^/]+_(?:test|spec)\.[^/]+|"
    r"[^/]+\.(?:test|spec)\.[^/]+)$",
    re.IGNORECASE,
)


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH_RE.search(path.replace("\\", "/")))


@dataclass(frozen=True)
class VerificationPlan:
    stack: str
    image: str
    install_command: Optional[List[str]]
    verification_commands: List[List[str]]
    requires_network_preparation: bool
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def detect_verification_plan(repository_path: str) -> VerificationPlan:
    repository = Path(repository_path)
    package_json = repository / "package.json"
    pyproject = repository / "pyproject.toml"
    requirements = repository / "requirements.txt"
    setup_py = repository / "setup.py"

    if package_json.exists():
        return _javascript_plan(repository, package_json)
    if pyproject.exists() or requirements.exists() or setup_py.exists():
        return _python_plan(repository)
    if any(repository.glob("test*.py")) or (repository / "tests").exists():
        return VerificationPlan(
            stack="python",
            image="python:3.12-alpine",
            install_command=None,
            verification_commands=[
                ["python", "-m", "unittest", "discover", "-v"]
            ],
            requires_network_preparation=False,
        )
    raise ValueError("unable to determine a supported verification stack")


def _javascript_plan(
    repository: Path, package_json: Path
) -> VerificationPlan:
    package = json.loads(package_json.read_text(encoding="utf-8"))
    scripts = package.get("scripts") or {}
    warnings: List[str] = []

    if (repository / "package-lock.json").exists():
        _validate_npm_lockfile(repository / "package-lock.json")
        install = ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"]
        runner = ["npm", "run"]
    elif (repository / "pnpm-lock.yaml").exists() or (
        repository / "yarn.lock"
    ).exists():
        raise ValueError(
            "pnpm and Yarn lockfiles are not yet accepted for safe preparation"
        )
    else:
        raise ValueError(
            "JavaScript repositories require a committed lockfile"
        )

    commands: List[List[str]] = []
    for script in ("test", "lint", "typecheck"):
        if script not in scripts:
            continue
        if runner[-1] == "run":
            commands.append([*runner, script])
        else:
            commands.append([*runner, script])
    if "test" not in scripts:
        warnings.append("package.json has no test script")
    if not commands:
        raise ValueError(
            "JavaScript repository has no test, lint, or typecheck scripts"
        )
    return VerificationPlan(
        stack="javascript",
        image="node:22-bookworm-slim",
        install_command=install,
        verification_commands=commands,
        requires_network_preparation=True,
        warnings=warnings
        + [
            "dependency lifecycle scripts are disabled during preparation",
            "repositories requiring postinstall builds need explicit review",
        ],
    )


def _python_plan(repository: Path) -> VerificationPlan:
    warnings: List[str] = []
    install: Optional[List[str]] = None
    if (repository / "requirements.txt").exists():
        _validate_hashed_requirements(repository / "requirements.txt")
        install = [
            "python",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--require-hashes",
            "--only-binary=:all:",
            "--no-deps",
            "--target",
            "/dependencies",
            "-r",
            "requirements.txt",
        ]
    elif (repository / "setup.py").exists():
        warnings.append(
            "setup.py is not executed; tests must import directly from the checkout"
        )

    pytest_signal = any(
        path.exists()
        for path in (
            repository / "pytest.ini",
            repository / "conftest.py",
        )
    )
    if not pytest_signal and (repository / "pyproject.toml").exists():
        pytest_signal = "[tool.pytest" in (
            repository / "pyproject.toml"
        ).read_text(encoding="utf-8", errors="replace")
    command = (
        ["python", "-m", "pytest", "-q"]
        if pytest_signal
        else ["python", "-m", "unittest", "discover", "-v"]
    )
    return VerificationPlan(
        stack="python",
        image="python:3.12-bookworm",
        install_command=install,
        verification_commands=[command],
        requires_network_preparation=install is not None,
        warnings=warnings,
    )


_PINNED_REQUIREMENT_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9,._-]+\])?==[^\s;]+"
    r"(?:\s*;\s*[^#]+)?(?:\s+--hash=sha256:[0-9a-fA-F]{64})+$"
)


def _logical_requirement_lines(text: str) -> List[str]:
    logical: List[str] = []
    pending = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        continued = stripped.endswith("\\")
        part = stripped[:-1].strip() if continued else stripped
        pending = f"{pending} {part}".strip()
        if not continued:
            logical.append(pending)
            pending = ""
    if pending:
        raise ValueError("requirements.txt has an unterminated continuation")
    return logical


def _validate_hashed_requirements(path: Path) -> None:
    lines = _logical_requirement_lines(path.read_text(encoding="utf-8"))
    requirements = [line for line in lines if line != "--require-hashes"]
    if not requirements:
        raise ValueError("requirements.txt has no hash-locked requirements")
    for line in requirements:
        if line.startswith("-") or not _PINNED_REQUIREMENT_RE.fullmatch(line):
            raise ValueError(
                "Python preparation requires exact == pins and sha256 hashes "
                "for every requirement; URLs, editable installs, and directives "
                "are rejected"
            )


def _validate_npm_lockfile(path: Path) -> None:
    lock = json.loads(path.read_text(encoding="utf-8"))
    if int(lock.get("lockfileVersion", 0)) < 2:
        raise ValueError("npm preparation requires package-lock version 2 or newer")
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        raise ValueError("npm lockfile has no packages map")
    for package_path, metadata in packages.items():
        if package_path == "" or not isinstance(metadata, dict):
            continue
        if metadata.get("link") is True:
            continue
        resolved = metadata.get("resolved")
        integrity = metadata.get("integrity")
        if not isinstance(resolved, str) or not isinstance(integrity, str):
            raise ValueError("every npm package must have a resolved URL and integrity")
        parsed = urlparse(resolved)
        if parsed.scheme != "https" or parsed.hostname != "registry.npmjs.org":
            raise ValueError(
                "npm dependencies must resolve from https://registry.npmjs.org"
            )
        if not integrity.startswith(("sha512-", "sha256-")):
            raise ValueError("npm dependencies require sha256 or sha512 integrity")
