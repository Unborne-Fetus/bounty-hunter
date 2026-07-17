import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from .agent import CodexContextPatchAgent, validate_patch
from .github import GitHubClient, enrich_opportunity, parse_issue_url
from .history import History
from .isolation import IsolatedRunner
from .models import Opportunity
from .review import (
    create_review_bundle,
    review_bundle_sha256,
    working_tree_patch,
)
from .scoring import score_opportunity
from .verification import detect_verification_plan, is_test_path


PATCH_NEW_PATH_RE = re.compile(r"^\+\+\+ (?:b/)?(.+)$", re.MULTILINE)
MAX_ISSUE_BYTES = 64 * 1024


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json_artifact(
    history: History, attempt_id: int, kind: str
) -> Dict[str, Any]:
    artifact = history.latest_artifact(attempt_id, kind)
    if not artifact:
        raise ValueError(f"attempt has no {kind} artifact")
    path = Path(artifact["path"]).resolve()
    if not path.is_file() or _sha256(path) != artifact["sha256"]:
        raise ValueError(f"{kind} artifact integrity check failed")
    return json.loads(path.read_text(encoding="utf-8"))


def _fresh_issue_text(
    record: Dict[str, Any], client: GitHubClient
) -> tuple[Opportunity, Dict[str, Any], str]:
    opportunity = Opportunity.from_dict(record["opportunity"])
    opportunity.evidence.pop("github_evidence_stale", None)
    opportunity.evidence.pop("github_error", None)
    enrich_opportunity(opportunity, client)
    if opportunity.evidence.get("github_error"):
        raise ValueError("fresh GitHub evidence is unavailable")
    score = score_opportunity(opportunity)
    if not score.eligible:
        raise ValueError(
            "opportunity no longer clears the gate: "
            + "; ".join(score.rejection_reasons)
        )
    parsed = parse_issue_url(opportunity.issue_url)
    if not parsed:
        raise ValueError("opportunity has no canonical GitHub issue URL")
    owner, repo, number = parsed
    issue = client.get(
        f"/repos/{quote(owner)}/{quote(repo)}/issues/{number}"
    )
    if issue.get("state") != "open" or "pull_request" in issue:
        raise ValueError("fresh GitHub issue is not open")
    body = issue.get("body") or ""
    if len(body.encode("utf-8")) > MAX_ISSUE_BYTES:
        raise ValueError("GitHub issue body exceeds 64 KiB")
    issue_text = (
        f"Issue URL: {opportunity.issue_url}\n"
        f"Title: {issue.get('title') or opportunity.title}\n\n{body}"
    )
    return opportunity, score.to_dict(), issue_text


def generate_patch(
    history: History,
    attempt_id: int,
    report_root: str = "reports/attempts",
    github_client: Optional[GitHubClient] = None,
    agent: Optional[CodexContextPatchAgent] = None,
    runner: Optional[IsolatedRunner] = None,
) -> Dict[str, Any]:
    attempt = history.get_attempt(attempt_id)
    if attempt["status"] != "running":
        raise ValueError("attempt must pass preflight before patch generation")
    if history.latest_artifact(attempt_id, "agent_patch"):
        raise ValueError("attempt already has an agent patch")
    preflight = _load_json_artifact(history, attempt_id, "preflight")
    if preflight.get("status") != "passed":
        raise ValueError("attempt preflight did not pass")
    source = Path(preflight["source_path"]).resolve()
    if not source.is_dir():
        raise ValueError("preflight source workspace is missing")

    runner = runner or IsolatedRunner(history)
    commit_result = runner.run_command(
        attempt_id,
        str(source),
        preflight["clone"]["image"],
        ["rev-parse", "HEAD"],
    )
    if (
        commit_result.returncode != 0
        or commit_result.stdout.strip() != preflight.get("commit")
    ):
        raise ValueError("preflight source commit no longer matches provenance")

    record = history.get_attempt_record(attempt_id)
    client = github_client or GitHubClient(
        token=os.environ.get("GITHUB_TOKEN"),
        cache_path="data/github-cache.json",
        cache_ttl_seconds=0,
    )
    opportunity, fresh_score, issue_text = _fresh_issue_text(record, client)
    report_directory = Path(report_root).resolve() / f"attempt-{attempt_id}"
    report_directory.mkdir(parents=True, exist_ok=True)
    issue_path = report_directory / "issue-snapshot.json"
    issue_path.write_text(
        json.dumps(
            {
                "opportunity": opportunity.to_dict(),
                "score": fresh_score,
                "issue_text": issue_text,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    history.add_artifact(
        attempt_id, "issue_snapshot", str(issue_path), _sha256(issue_path)
    )

    agent = agent or CodexContextPatchAgent(history)
    result = agent.run(
        attempt_id,
        str(source),
        issue_text,
        str(Path(report_root).resolve()),
    )
    if result.returncode != 0:
        raise RuntimeError("context-only patch generation failed")
    patch = history.latest_artifact(attempt_id, "agent_patch")
    if not patch:
        raise RuntimeError("patch agent produced no immutable patch artifact")
    return {
        "attempt_id": attempt_id,
        "source_path": str(source),
        "issue_snapshot": str(issue_path),
        "agent_patch": patch,
        "summary": result.final_message,
    }


def _regression_test_paths(patch: str) -> List[str]:
    paths = []
    for raw in PATCH_NEW_PATH_RE.findall(patch):
        path = raw.split("\t", 1)[0]
        if path != "/dev/null" and is_test_path(path) and path not in paths:
            paths.append(path)
    if not paths:
        raise ValueError("agent patch has no recognizable regression test file")
    return paths


def _combined_results(results: List[Dict[str, Any]], stream: str) -> str:
    pieces = []
    for result in results:
        pieces.append(
            f"$ {json.dumps(result['command'])}\n{result[stream]}"
        )
    return "\n\n".join(pieces)


def generate_revision_patch(
    history: History,
    attempt_id: int,
    feedback_file: str,
    report_root: str = "reports/attempts",
    agent: Optional[CodexContextPatchAgent] = None,
) -> Dict[str, Any]:
    attempt = history.get_attempt(attempt_id)
    if attempt["status"] != "revision":
        raise ValueError("attempt must be awaiting revision")
    bundle = history.latest_artifact(attempt_id, "review_bundle")
    if not bundle or bundle["sha256"] != attempt.get("approved_bundle_sha256"):
        raise ValueError("revision must start from the submitted approved bundle")
    if review_bundle_sha256(bundle["path"]) != bundle["sha256"]:
        raise ValueError("submitted review bundle integrity check failed")
    manifest = json.loads(
        (Path(bundle["path"]) / "manifest.json").read_text(encoding="utf-8")
    )
    preflight = _load_json_artifact(history, attempt_id, "preflight")
    source = Path(preflight["source_path"]).resolve()
    current_patch = working_tree_patch(source, subprocess.run)
    current_patch_sha256 = hashlib.sha256(current_patch.encode("utf-8")).hexdigest()
    if current_patch_sha256 != manifest.get("patch_sha256"):
        raise ValueError("working tree drifted from the submitted approved patch")

    feedback_path = Path(feedback_file).resolve()
    if not feedback_path.is_file() or feedback_path.is_symlink():
        raise ValueError("revision feedback must be a regular file")
    feedback = feedback_path.read_text(encoding="utf-8")
    if not feedback.strip() or len(feedback.encode("utf-8")) > MAX_ISSUE_BYTES:
        raise ValueError("revision feedback must contain 1 to 65536 bytes")
    report_directory = Path(report_root).resolve() / f"attempt-{attempt_id}"
    report_directory.mkdir(parents=True, exist_ok=True)
    stored_feedback = report_directory / (
        f"revision-{history.artifact_count(attempt_id, 'revision_feedback') + 1}.txt"
    )
    stored_feedback.write_text(feedback, encoding="utf-8")
    history.add_artifact(
        attempt_id,
        "revision_feedback",
        str(stored_feedback),
        _sha256(stored_feedback),
    )
    history.transition(
        attempt_id,
        "running",
        outcome_note="Applying recorded reviewer feedback.",
    )
    patch_count = history.artifact_count(attempt_id, "agent_patch")
    prompt = (
        "Revise the existing bounty patch in response to reviewer feedback. "
        "Preserve the original fix and regression coverage, make only the "
        "requested changes, and do not alter dependency metadata.\n\n"
        f"Reviewer feedback:\n{feedback}"
    )
    agent = agent or CodexContextPatchAgent(history)
    result = agent.run(
        attempt_id,
        str(source),
        prompt,
        str(Path(report_root).resolve()),
    )
    if result.returncode != 0:
        raise RuntimeError("context-only revision generation failed")
    if history.artifact_count(attempt_id, "agent_patch") != patch_count + 1:
        raise RuntimeError("revision agent did not append a new patch artifact")
    patch = history.latest_artifact(attempt_id, "agent_patch")
    if not patch:
        raise RuntimeError("revision agent produced no patch artifact")
    return {
        "attempt_id": attempt_id,
        "feedback_artifact": history.latest_artifact(
            attempt_id, "revision_feedback"
        ),
        "agent_patch": patch,
        "summary": result.final_message,
    }


def verify_patch(
    history: History,
    attempt_id: int,
    report_root: str = "reports/attempts",
    runner: Optional[IsolatedRunner] = None,
) -> Dict[str, Any]:
    attempt = history.get_attempt(attempt_id)
    if attempt["status"] != "running":
        raise ValueError("attempt must be running before verification")
    preflight = _load_json_artifact(history, attempt_id, "preflight")
    patch_artifact = history.latest_artifact(attempt_id, "agent_patch")
    if not patch_artifact:
        raise ValueError("attempt has no agent patch")
    patch_path = Path(patch_artifact["path"]).resolve()
    if not patch_path.is_file() or _sha256(patch_path) != patch_artifact["sha256"]:
        raise ValueError("agent patch artifact integrity check failed")
    source = Path(preflight["source_path"]).resolve()
    complete_patch = working_tree_patch(source, subprocess.run)
    validate_patch(complete_patch)
    test_paths = _regression_test_paths(complete_patch)
    workspace = Path(preflight["workspace"]).resolve()
    verification_number = history.artifact_count(attempt_id, "review_bundle") + 1
    baseline_parent = workspace / f"baseline-checkout-{verification_number}"
    runner = runner or IsolatedRunner(history)
    clone = runner.clone_baseline(
        attempt_id,
        str(source),
        str(baseline_parent),
        preflight["clone"]["image"],
    )
    if clone.returncode != 0:
        raise RuntimeError("isolated baseline clone failed")
    baseline = baseline_parent / "baseline"
    for relative in test_paths:
        candidate = (source / relative).resolve()
        try:
            candidate.relative_to(source)
        except ValueError as error:
            raise ValueError("regression test escapes source workspace") from error
        if not candidate.is_file() or candidate.is_symlink():
            raise ValueError(f"regression test is not a regular file: {relative}")
        destination = baseline / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidate, destination)

    plan = detect_verification_plan(str(source))
    if plan.to_dict() != preflight.get("verification_plan"):
        raise ValueError("verification plan changed after preflight")
    dependencies = workspace / "dependencies"
    dependency_path = (
        str(dependencies) if preflight.get("preparation") is not None else None
    )
    dependency_stack = plan.stack if dependency_path else None

    baseline_results: List[Dict[str, Any]] = []
    fixed_results: List[Dict[str, Any]] = []
    for command in plan.verification_commands:
        baseline_result = runner.run_command(
            attempt_id,
            str(baseline),
            preflight["execution_image"],
            command,
            dependency_path=dependency_path,
            dependency_stack=dependency_stack,
        )
        fixed_result = runner.run_command(
            attempt_id,
            str(source),
            preflight["execution_image"],
            command,
            dependency_path=dependency_path,
            dependency_stack=dependency_stack,
        )
        baseline_results.append(
            {
                "command": command,
                "exit_code": baseline_result.returncode,
                "stdout": baseline_result.stdout,
                "stderr": baseline_result.stderr,
            }
        )
        fixed_results.append(
            {
                "command": command,
                "exit_code": fixed_result.returncode,
                "stdout": fixed_result.stdout,
                "stderr": fixed_result.stderr,
            }
        )

    baseline_exit = 1 if any(
        result["exit_code"] != 0 for result in baseline_results
    ) else 0
    fixed_exit = 1 if any(
        result["exit_code"] != 0 for result in fixed_results
    ) else 0
    bundle = create_review_bundle(
        history,
        attempt_id,
        str(source),
        str(Path(report_root).resolve()),
        verification_command=[
            "bounty-hunter-plan",
            json.dumps(plan.verification_commands),
        ],
        verification_exit_code=fixed_exit,
        verification_stdout=_combined_results(fixed_results, "stdout"),
        verification_stderr=_combined_results(fixed_results, "stderr"),
        baseline_exit_code=baseline_exit,
        baseline_stdout=_combined_results(baseline_results, "stdout"),
        baseline_stderr=_combined_results(baseline_results, "stderr"),
        image_reference=preflight["execution_image"],
    )
    return {
        "attempt_id": attempt_id,
        "regression_test_paths": test_paths,
        "baseline_results": baseline_results,
        "fixed_results": fixed_results,
        "review_bundle": bundle,
        "status": history.get_attempt(attempt_id)["status"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate and verify a supervised bounty patch."
    )
    parser.add_argument("--history-db", default="data/bounty_hunter.db")
    parser.add_argument("--report-root", default="reports/attempts")
    subparsers = parser.add_subparsers(dest="command", required=True)
    patch = subparsers.add_parser("patch")
    patch.add_argument("attempt_id", type=int)
    verify = subparsers.add_parser("verify")
    verify.add_argument("attempt_id", type=int)
    revise = subparsers.add_parser("revise")
    revise.add_argument("attempt_id", type=int)
    revise.add_argument("--feedback-file", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    history = History(args.history_db)
    try:
        if args.command == "patch":
            result = generate_patch(
                history, args.attempt_id, args.report_root
            )
        elif args.command == "verify":
            result = verify_patch(
                history, args.attempt_id, args.report_root
            )
        else:
            result = generate_revision_patch(
                history,
                args.attempt_id,
                args.feedback_file,
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
