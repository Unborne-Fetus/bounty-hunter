import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .history import History
from .github import GitHubClient
from .isolation import IsolatedRunner, resolve_image_reference
from .models import Opportunity
from .scoring import score_opportunity
from .submission import validate_marketplace_funding
from .verification import detect_verification_plan


MAX_CAPTURE_BYTES = 256 * 1024


def _bounded(value: str) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_CAPTURE_BYTES:
        return value
    return (
        encoded[:MAX_CAPTURE_BYTES].decode("utf-8", errors="replace")
        + "\n[truncated by Bounty Hunter]\n"
    )


def _repository_url(record: Dict[str, Any]) -> str:
    opportunity = record["opportunity"]
    score = record.get("score") or {}
    if not score.get("eligible"):
        raise ValueError("preflight requires an opportunity that passed scoring")
    current_score = score_opportunity(Opportunity.from_dict(opportunity))
    if not current_score.eligible:
        raise ValueError(
            "preflight current policy recheck rejected the queued opportunity: "
            + "; ".join(current_score.rejection_reasons)
        )
    if opportunity.get("public_repository") is not True:
        raise ValueError("preflight requires verified public repository evidence")
    if opportunity.get("issue_open") is not True:
        raise ValueError("preflight requires a freshly verified open issue")
    if (opportunity.get("evidence") or {}).get("github_evidence_stale"):
        raise ValueError("preflight refuses stale GitHub evidence")
    repository = opportunity.get("repository")
    if not isinstance(repository, str) or repository.count("/") != 1:
        raise ValueError("opportunity has no canonical GitHub repository")
    owner, name = repository.split("/", 1)
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
    if not owner or not name or not set(owner + name) <= allowed:
        raise ValueError("opportunity repository name is unsafe")
    return f"https://github.com/{owner}/{name}.git"


def validate_preflight_opportunity(
    record: Dict[str, Any],
    github_client: Optional[GitHubClient] = None,
    marketplace_validator: Callable[[Dict[str, Any]], Dict[str, Any]] = validate_marketplace_funding,
) -> Dict[str, Any]:
    # Imported lazily to keep the attempt and preflight command surfaces separate.
    from .attempt import _fresh_issue_text

    funding = marketplace_validator(record["opportunity"])
    client = github_client or GitHubClient(
        token=os.environ.get("GITHUB_TOKEN"),
        cache_path="data/github-cache.json",
        cache_ttl_seconds=0,
    )
    opportunity, score, _issue_text = _fresh_issue_text(record, client)
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "funding": funding,
        "opportunity": opportunity.to_dict(),
        "score": score,
    }


def run_preflight(
    history: History,
    attempt_id: int,
    workspace_root: str = "data/attempts",
    report_root: str = "reports/attempts",
    runner: Optional[IsolatedRunner] = None,
    image_resolver: Callable[..., str] = resolve_image_reference,
    fresh_validator: Optional[
        Callable[[Dict[str, Any]], Dict[str, Any]]
    ] = None,
) -> Dict[str, Any]:
    attempt = history.get_attempt(attempt_id)
    if attempt["status"] != "approved":
        raise ValueError("attempt must have explicit approval before preflight")
    record = history.get_attempt_record(attempt_id)
    if attempt.get("approved_record_sha256") != history.attempt_record_sha256(
        attempt_id
    ):
        raise ValueError("attempt approval is not bound to the queued record")
    repo_url = _repository_url(record)
    workspace = Path(workspace_root).resolve() / f"attempt-{attempt_id}"
    report_directory = Path(report_root).resolve() / f"attempt-{attempt_id}"
    if workspace.exists():
        raise ValueError("attempt workspace already exists; refusing to overwrite it")
    report_directory.mkdir(parents=True, exist_ok=True)
    validation_path = report_directory / "preflight-validation.json"
    try:
        validation = (fresh_validator or validate_preflight_opportunity)(record)
    except Exception as error:
        validation = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "status": "rejected_before_execution",
            "error": str(error),
        }
        validation_path.write_text(
            json.dumps(validation, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        digest = hashlib.sha256(validation_path.read_bytes()).hexdigest()
        history.add_artifact(
            attempt_id, "preflight_validation", str(validation_path), digest
        )
        history.transition(
            attempt_id,
            "abandoned",
            outcome_note=f"Fresh preflight validation failed: {error}",
        )
        raise
    validation["status"] = "passed"
    validation_path.write_text(
        json.dumps(validation, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    validation_digest = hashlib.sha256(validation_path.read_bytes()).hexdigest()
    history.add_artifact(
        attempt_id,
        "preflight_validation",
        str(validation_path),
        validation_digest,
    )
    workspace.mkdir(parents=True)
    report_path = report_directory / "preflight.json"
    started = time.monotonic()
    runner = runner or IsolatedRunner(history)
    runner.begin_attempt(attempt_id)
    manifest: Dict[str, Any] = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "repository_url": repo_url,
        "workspace": str(workspace),
        "source_path": str(workspace / "source"),
        "validation_path": str(validation_path),
        "validation_sha256": validation_digest,
        "status": "running",
    }
    try:
        git_image = image_resolver(
            runner.require_available(), "alpine/git:2.47.2"
        )
        clone = runner.clone_public_repository(
            attempt_id, repo_url, str(workspace), git_image
        )
        manifest["clone"] = {
            "image": git_image,
            "exit_code": clone.returncode,
            "stdout": _bounded(clone.stdout),
            "stderr": _bounded(clone.stderr),
        }
        if clone.returncode != 0:
            raise RuntimeError("isolated public repository clone failed")
        source = workspace / "source"
        commit_result = runner.run_command(
            attempt_id,
            str(source),
            git_image,
            ["rev-parse", "HEAD"],
        )
        if commit_result.returncode != 0:
            raise RuntimeError("unable to resolve cloned commit")
        commit = commit_result.stdout.strip()
        if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
            raise RuntimeError("cloned repository returned an invalid commit hash")

        plan = detect_verification_plan(str(source))
        execution_image = image_resolver(
            runner.require_available(), plan.image
        )
        dependencies = workspace / "dependencies"
        preparation = None
        if plan.requires_network_preparation:
            preparation_result = runner.prepare_dependencies(
                attempt_id,
                str(source),
                str(dependencies),
                execution_image,
                plan.stack,
                plan.install_command or [],
            )
            preparation = {
                "command": plan.install_command,
                "exit_code": preparation_result.returncode,
                "stdout": _bounded(preparation_result.stdout),
                "stderr": _bounded(preparation_result.stderr),
            }
            if preparation_result.returncode != 0:
                raise RuntimeError("safe dependency preparation failed")

        verification: List[Dict[str, Any]] = []
        for command in plan.verification_commands:
            result = runner.run_command(
                attempt_id,
                str(source),
                execution_image,
                command,
                dependency_path=(str(dependencies) if preparation else None),
                dependency_stack=(plan.stack if preparation else None),
            )
            verification.append(
                {
                    "command": command,
                    "exit_code": result.returncode,
                    "stdout": _bounded(result.stdout),
                    "stderr": _bounded(result.stderr),
                }
            )
            if result.returncode != 0:
                raise RuntimeError("baseline verification failed")

        manifest.update(
            {
                "status": "passed",
                "commit": commit,
                "verification_plan": plan.to_dict(),
                "execution_image": execution_image,
                "preparation": preparation,
                "verification": verification,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        )
    except Exception as error:
        manifest.update(
            {
                "status": "failed",
                "error": str(error),
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        )
        report_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
        history.add_artifact(attempt_id, "preflight", str(report_path), digest)
        history.transition(
            attempt_id,
            "abandoned",
            outcome_note=f"Preflight failed: {error}",
        )
        raise

    report_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
    history.add_artifact(attempt_id, "preflight", str(report_path), digest)
    return {"path": str(report_path), "sha256": digest, **manifest}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an approved opportunity's isolated repository preflight."
    )
    parser.add_argument("attempt_id", type=int)
    parser.add_argument("--history-db", default="data/bounty_hunter.db")
    parser.add_argument("--workspace-root", default="data/attempts")
    parser.add_argument("--report-root", default="reports/attempts")
    return parser


def main() -> int:
    args = _parser().parse_args()
    history = History(args.history_db)
    try:
        result = run_preflight(
            history,
            args.attempt_id,
            args.workspace_root,
            args.report_root,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception as error:
        print(json.dumps({"error": str(error)}, indent=2))
        return 1
    finally:
        history.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
