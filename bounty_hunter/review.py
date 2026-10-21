import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

from .history import History


MAX_LOG_BYTES = 256 * 1024
GENERATED_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache"}
GENERATED_SUFFIXES = {".pyc", ".pyo"}
BUNDLE_FILES = (
    "manifest.json",
    "changes.patch",
    "verification.stdout.txt",
    "verification.stderr.txt",
    "baseline.stdout.txt",
    "baseline.stderr.txt",
)


def review_bundle_sha256(directory: str) -> str:
    root = Path(directory).resolve()
    digest = hashlib.sha256()
    for name in BUNDLE_FILES:
        path = root / name
        if not path.is_file():
            raise ValueError(f"review bundle is missing {name}")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _run_git(
    repository: Path,
    arguments: Iterable[str],
    runner: Callable[..., subprocess.CompletedProcess],
) -> str:
    result = runner(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def working_tree_patch(
    repository: Path,
    runner: Callable[..., subprocess.CompletedProcess],
) -> str:
    tracked = _run_git(
        repository,
        ["diff", "--binary", "--no-ext-diff"],
        runner,
    )
    untracked_result = runner(
        [
            "git",
            "-C",
            str(repository),
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    pieces = [tracked]
    for relative in filter(None, untracked_result.stdout.split("\0")):
        relative_path = Path(relative)
        if (
            any(part in GENERATED_PARTS for part in relative_path.parts)
            or relative_path.suffix.lower() in GENERATED_SUFFIXES
        ):
            raise ValueError(
                f"generated artifact must be removed before review: {relative}"
            )
        path = (repository / relative).resolve()
        try:
            path.relative_to(repository)
        except ValueError as error:
            raise ValueError("untracked file escapes repository root") from error
        if path.is_symlink():
            raise ValueError("review bundles do not accept untracked symlinks")
        result = runner(
            [
                "git",
                "-C",
                str(repository),
                "diff",
                "--binary",
                "--no-index",
                "--",
                "/dev/null",
                relative,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode not in (0, 1):
            raise subprocess.CalledProcessError(
                result.returncode,
                result.args,
                result.stdout,
                result.stderr,
            )
        pieces.append(result.stdout)
    return "".join(pieces)


def _bounded_log(value: str) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_LOG_BYTES:
        return value
    suffix = "\n[truncated by Bounty Hunter]\n"
    return encoded[:MAX_LOG_BYTES].decode("utf-8", errors="replace") + suffix


def create_review_bundle(
    history: History,
    attempt_id: int,
    repository_path: str,
    output_directory: str,
    *,
    verification_command: Iterable[str],
    verification_exit_code: int,
    verification_stdout: str,
    verification_stderr: str,
    baseline_exit_code: int,
    baseline_stdout: str,
    baseline_stderr: str,
    image_reference: Optional[str] = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Dict[str, Any]:
    attempt = history.get_attempt(attempt_id)
    if attempt["status"] != "running":
        raise ValueError("attempt must be running to create a review bundle")

    repository = Path(repository_path).resolve()
    review_number = history.artifact_count(attempt_id, "review_bundle") + 1
    output = (
        Path(output_directory).resolve()
        / f"attempt-{attempt_id}"
        / f"review-{review_number}"
    )
    output.mkdir(parents=True, exist_ok=True)

    diff = working_tree_patch(repository, runner)
    if not diff.strip():
        raise ValueError("cannot verify an attempt with an empty diff")
    status = _run_git(repository, ["status", "--short"], runner)
    head = _run_git(repository, ["rev-parse", "HEAD"], runner).strip()

    patch_path = output / "changes.patch"
    stdout_path = output / "verification.stdout.txt"
    stderr_path = output / "verification.stderr.txt"
    baseline_stdout_path = output / "baseline.stdout.txt"
    baseline_stderr_path = output / "baseline.stderr.txt"
    manifest_path = output / "manifest.json"

    # Git patches and their approved hashes must use identical bytes on Windows.
    patch_path.write_bytes(diff.encode("utf-8"))
    stdout_path.write_text(_bounded_log(verification_stdout), encoding="utf-8")
    stderr_path.write_text(_bounded_log(verification_stderr), encoding="utf-8")
    baseline_stdout_path.write_text(
        _bounded_log(baseline_stdout), encoding="utf-8"
    )
    baseline_stderr_path.write_text(
        _bounded_log(baseline_stderr), encoding="utf-8"
    )
    patch_sha256 = hashlib.sha256(patch_path.read_bytes()).hexdigest()

    manifest = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "review_number": review_number,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "base_commit": head,
        "working_tree_status": status.splitlines(),
        "patch_path": patch_path.name,
        "patch_sha256": patch_sha256,
        "verification": {
            "command": list(verification_command),
            "exit_code": verification_exit_code,
            "stdout_path": stdout_path.name,
            "stderr_path": stderr_path.name,
            "image_reference": image_reference,
        },
        "regression_proof": {
            "baseline_exit_code": baseline_exit_code,
            "baseline_stdout_path": baseline_stdout_path.name,
            "baseline_stderr_path": baseline_stderr_path.name,
            "fails_before_fix": baseline_exit_code != 0,
            "passes_after_fix": verification_exit_code == 0,
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    bundle_sha256 = review_bundle_sha256(str(output))
    bundle_sha_path = output / "SHA256"
    bundle_sha_path.write_text(bundle_sha256 + "\n", encoding="utf-8")

    history.add_artifact(
        attempt_id,
        "review_bundle",
        str(output),
        bundle_sha256,
    )
    if verification_exit_code == 0 and baseline_exit_code != 0:
        history.transition(attempt_id, "verified")
    else:
        history.transition(
            attempt_id,
            "abandoned",
            outcome_note=(
                "Verification or failing-before-fix regression proof failed; "
                "review bundle retained."
            ),
        )
    return {
        "path": str(output),
        "sha256": bundle_sha256,
        "patch_sha256": patch_sha256,
        "verification_exit_code": verification_exit_code,
    }
