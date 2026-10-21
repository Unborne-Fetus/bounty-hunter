import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from .history import History


PUBLIC_GITHUB_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?$"
)


def _host_cli_environment() -> dict:
    """Minimal host environment for Docker/Podman, including Windows runtime keys."""
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME") or os.environ.get("USERPROFILE", ""),
    }
    if os.name == "nt":
        for key in ("SystemRoot", "WINDIR", "TEMP", "TMP", "USERPROFILE", "PATHEXT"):
            if key in os.environ:
                environment[key] = os.environ[key]
    return environment


class IsolationUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Limits:
    cpus: float = 2.0
    memory: str = "4g"
    pids: int = 256
    timeout_seconds: int = 7200


def detect_runtime() -> Optional[str]:
    for runtime in ("docker", "podman"):
        if not shutil.which(runtime):
            continue
        try:
            result = subprocess.run(
                [runtime, "info"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return runtime
    return None


def validate_public_repo_url(repo_url: str) -> None:
    if not PUBLIC_GITHUB_RE.fullmatch(repo_url):
        raise ValueError(
            "only public HTTPS GitHub repository URLs are accepted"
        )


def _common_isolation_args(limits: Limits) -> List[str]:
    return [
        "--rm",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(limits.pids),
        "--memory",
        limits.memory,
        "--cpus",
        str(limits.cpus),
        "--init",
    ]


def resolve_image_reference(
    runtime: str,
    image: str,
    process_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    """Pull an image tag and return its immutable repository digest."""
    pull = process_runner(
        [runtime, "pull", image],
        check=False,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if pull.returncode != 0:
        raise IsolationUnavailable(
            f"failed to pull execution image {image}: {pull.stderr.strip()}"
        )
    inspect = process_runner(
        [
            runtime,
            "image",
            "inspect",
            image,
            "--format",
            "{{json .RepoDigests}}",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if inspect.returncode != 0:
        raise IsolationUnavailable(
            f"failed to inspect execution image {image}: {inspect.stderr.strip()}"
        )
    import json

    digests = json.loads(inspect.stdout or "[]")
    if not digests:
        raise IsolationUnavailable(
            f"execution image {image} has no immutable repository digest"
        )
    return str(digests[0])


def clone_command(
    runtime: str,
    repo_url: str,
    destination: str,
    limits: Limits = Limits(),
    image_reference: str = "alpine/git:2.47.2",
) -> List[str]:
    validate_public_repo_url(repo_url)
    destination_path = str(Path(destination).resolve())
    identity = (f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") and hasattr(os, "getgid") else None)
    return [
        runtime,
        "run",
        *_common_isolation_args(limits),
        "--network",
        "bridge",
        *(["--user", identity] if identity else []),
        "--volume",
        f"{destination_path}:/workspace:rw",
        "--workdir",
        "/workspace",
        image_reference,
        "clone",
        "--depth",
        "1",
        "--",
        repo_url,
        "source",
    ]


def baseline_clone_command(
    runtime: str,
    source_path: str,
    destination: str,
    image_reference: str,
    limits: Limits = Limits(timeout_seconds=300),
) -> List[str]:
    resolved_source = str(Path(source_path).resolve())
    resolved_destination = str(Path(destination).resolve())
    identity = (f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") and hasattr(os, "getgid") else None)
    return [
        runtime,
        "run",
        *_common_isolation_args(limits),
        "--network",
        "none",
        "--read-only",
        *(["--user", identity] if identity else []),
        "--env",
        "HOME=/tmp/home",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",
        "--volume",
        f"{resolved_source}:/source:ro",
        "--volume",
        f"{resolved_destination}:/destination:rw",
        "--workdir",
        "/destination",
        image_reference,
        "clone",
        "--no-hardlinks",
        "--",
        "/source",
        "baseline",
    ]


def execution_command(
    runtime: str,
    source_path: str,
    image: str,
    command: Sequence[str],
    limits: Limits = Limits(),
    dependency_path: Optional[str] = None,
    dependency_stack: Optional[str] = None,
) -> List[str]:
    if not command:
        raise ValueError("execution command cannot be empty")
    resolved_source = str(Path(source_path).resolve())
    identity = (f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") and hasattr(os, "getgid") else None)
    arguments = [
        runtime,
        "run",
        *_common_isolation_args(limits),
        "--network",
        "none",
        "--read-only",
        *(["--user", identity] if identity else []),
        "--env",
        "HOME=/tmp/home",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=1g",
        "--volume",
        f"{resolved_source}:/workspace:rw",
    ]
    if dependency_path:
        resolved_dependencies = str(Path(dependency_path).resolve())
        if dependency_stack == "javascript":
            arguments.extend(
                [
                    "--volume",
                    f"{resolved_dependencies}/node_modules:/dependencies/node_modules:ro",
                    "--env",
                    "NODE_PATH=/dependencies/node_modules",
                    "--env",
                    "PATH=/dependencies/node_modules/.bin:/usr/local/bin:/usr/bin:/bin",
                ]
            )
        elif dependency_stack == "python":
            arguments.extend(
                [
                    "--volume",
                    f"{resolved_dependencies}:/dependencies:ro",
                    "--env",
                    "PYTHONPATH=/dependencies",
                ]
            )
        else:
            raise ValueError("dependency stack must be javascript or python")
    arguments.extend(["--workdir", "/workspace", image, *command])
    return arguments


SAFE_NPM_PREPARATION = (
    "npm",
    "ci",
    "--ignore-scripts",
    "--no-audit",
    "--no-fund",
)
SAFE_PYTHON_PREPARATION = (
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
)


def preparation_command(
    runtime: str,
    source_path: str,
    dependency_path: str,
    image: str,
    stack: str,
    command: Sequence[str],
    limits: Limits = Limits(timeout_seconds=1800),
) -> List[str]:
    expected = (
        SAFE_NPM_PREPARATION
        if stack == "javascript"
        else SAFE_PYTHON_PREPARATION
        if stack == "python"
        else None
    )
    if expected is None or tuple(command) != expected:
        raise ValueError("dependency preparation command is not allowlisted")
    resolved_source = str(Path(source_path).resolve())
    resolved_dependencies = str(Path(dependency_path).resolve())
    identity = (f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") and hasattr(os, "getgid") else None)
    dependency_target = "/dependencies"
    workdir = "/dependencies" if stack == "javascript" else "/workspace"
    return [
        runtime,
        "run",
        *_common_isolation_args(limits),
        "--network",
        "bridge",
        "--read-only",
        *(["--user", identity] if identity else []),
        "--env",
        "HOME=/tmp/home",
        "--env",
        "NPM_CONFIG_USERCONFIG=/dev/null",
        "--env",
        "NPM_CONFIG_CACHE=/tmp/npm-cache",
        "--env",
        "PIP_CONFIG_FILE=/dev/null",
        "--env",
        "PIP_NO_CACHE_DIR=1",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=1g",
        "--volume",
        f"{resolved_source}:/workspace:ro",
        "--volume",
        f"{resolved_dependencies}:{dependency_target}:rw",
        "--workdir",
        workdir,
        image,
        *command,
    ]


class IsolatedRunner:
    def __init__(
        self,
        history: History,
        runtime: Optional[str] = None,
        process_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.history = history
        self.runtime = runtime or detect_runtime()
        self.process_runner = process_runner

    def require_available(self) -> str:
        if not self.runtime:
            raise IsolationUnavailable(
                "Docker or Podman is required; refusing to execute untrusted "
                "repository code on the host"
            )
        return self.runtime

    def begin_attempt(self, attempt_id: int) -> None:
        self.require_available()
        attempt = self.history.get_attempt(attempt_id)
        if attempt["status"] != "approved":
            raise ValueError("attempt requires explicit human approval")
        self.history.transition(attempt_id, "running")

    def clone_public_repository(
        self,
        attempt_id: int,
        repo_url: str,
        destination: str,
        image_reference: str,
        limits: Limits = Limits(timeout_seconds=900),
    ) -> subprocess.CompletedProcess:
        attempt = self.history.get_attempt(attempt_id)
        if attempt["status"] != "running":
            raise ValueError("attempt must be running")
        runtime = self.require_available()
        destination_path = Path(destination).resolve()
        destination_path.mkdir(parents=True, exist_ok=True)
        if any(destination_path.iterdir()):
            raise ValueError("clone destination must be empty")
        isolated = clone_command(
            runtime,
            repo_url,
            str(destination_path),
            limits,
            image_reference,
        )
        return self.process_runner(
            isolated,
            check=False,
            capture_output=True,
            text=True,
            timeout=limits.timeout_seconds,
            env=_host_cli_environment(),
        )

    def clone_baseline(
        self,
        attempt_id: int,
        source_path: str,
        destination: str,
        image_reference: str,
        limits: Limits = Limits(timeout_seconds=300),
    ) -> subprocess.CompletedProcess:
        attempt = self.history.get_attempt(attempt_id)
        if attempt["status"] != "running":
            raise ValueError("attempt must be running")
        runtime = self.require_available()
        destination_path = Path(destination).resolve()
        destination_path.mkdir(parents=True, exist_ok=True)
        if any(destination_path.iterdir()):
            raise ValueError("baseline destination must be empty")
        isolated = baseline_clone_command(
            runtime,
            source_path,
            str(destination_path),
            image_reference,
            limits,
        )
        return self.process_runner(
            isolated,
            check=False,
            capture_output=True,
            text=True,
            timeout=limits.timeout_seconds,
            env=_host_cli_environment(),
        )

    def run_command(
        self,
        attempt_id: int,
        source_path: str,
        image: str,
        command: Sequence[str],
        limits: Limits = Limits(),
        dependency_path: Optional[str] = None,
        dependency_stack: Optional[str] = None,
    ) -> subprocess.CompletedProcess:
        attempt = self.history.get_attempt(attempt_id)
        if attempt["status"] != "running":
            raise ValueError("attempt must be running")
        runtime = self.require_available()
        isolated = execution_command(
            runtime,
            source_path,
            image,
            command,
            limits,
            dependency_path,
            dependency_stack,
        )
        environment = _host_cli_environment()
        return self.process_runner(
            isolated,
            check=False,
            capture_output=True,
            text=True,
            timeout=limits.timeout_seconds,
            env=environment,
        )

    def prepare_dependencies(
        self,
        attempt_id: int,
        source_path: str,
        dependency_path: str,
        image: str,
        stack: str,
        command: Sequence[str],
        limits: Limits = Limits(timeout_seconds=1800),
    ) -> subprocess.CompletedProcess:
        attempt = self.history.get_attempt(attempt_id)
        if attempt["status"] != "running":
            raise ValueError("attempt must be running")
        runtime = self.require_available()
        dependencies = Path(dependency_path).resolve()
        dependencies.mkdir(parents=True, exist_ok=True)
        if any(dependencies.iterdir()):
            raise ValueError("dependency workspace must be empty")
        if stack == "javascript":
            source = Path(source_path).resolve()
            for name in ("package.json", "package-lock.json"):
                manifest = source / name
                if not manifest.is_file() or manifest.is_symlink():
                    raise ValueError(f"safe npm preparation requires {name}")
                shutil.copy2(manifest, dependencies / name)
        isolated = preparation_command(
            runtime,
            source_path,
            str(dependencies),
            image,
            stack,
            command,
            limits,
        )
        environment = _host_cli_environment()
        return self.process_runner(
            isolated,
            check=False,
            capture_output=True,
            text=True,
            timeout=limits.timeout_seconds,
            env=environment,
        )
