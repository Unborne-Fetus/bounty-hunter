import argparse
import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

from .github import reputation_policy_signals, _competition
from .history import History
from .review import review_bundle_sha256
from .sources import algora, boss, bountypay, opire, taskbounty


PACKAGE_FILES = ("manifest.json", "changes.patch", "PR_BODY.md")
GITHUB_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PULL_REQUEST_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)$"
)
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def validate_marketplace_funding(
    opportunity: Dict[str, Any], timeout_seconds: int = 20
) -> Dict[str, Any]:
    """Require a fresh source-of-payment record immediately before publish."""
    source = opportunity.get("source")
    source_id = str(opportunity.get("source_id") or "")
    issue_url = opportunity.get("issue_url")
    approved_bounty = float(opportunity.get("bounty_usd") or 0)
    if source == "algora":
        org = (opportunity.get("evidence") or {}).get("algora_org")
        if not org:
            match = re.match(r"^https://algora\.io/([^/]+)/bounties", str(opportunity.get("source_url") or ""))
            org = match.group(1) if match else None
        if not org:
            raise ValueError("publication cannot identify the Algora organization")
        client = algora.AlgoraBoardClient(
            cache_ttl_seconds=0,
            minimum_request_interval_seconds=0,
        )
        html, stale = client.fetch(org, timeout_seconds)
        if stale:
            raise ValueError("publication refuses stale Algora funding evidence")
        rows = algora.normalize_html(org, html)
        matches = [item for item in rows if item.issue_url == issue_url]
        if not matches:
            raise ValueError("approved issue is no longer on the open Algora board")
        current = matches[0]
        if current.bounty_usd < approved_bounty:
            raise ValueError("Algora funding is lower than the approved bounty")
        return {
            "source": source,
            "source_id": current.source_id,
            "issue_url": current.issue_url,
            "bounty_usd": current.bounty_usd,
            "fresh": True,
        }
    if source in {"boss", "opire"}:
        if source == "boss":
            payload, stale = boss.BossClient(cache_ttl_seconds=0).fetch(timeout_seconds)
            country = (opportunity.get("evidence") or {}).get("payout_country")
            rows = boss.normalize(payload, payout_country=country)
        else:
            html, stale = opire.OpireClient(cache_ttl_seconds=0).fetch(timeout_seconds)
            rows = opire.normalize_html(html)
        if stale:
            raise ValueError(f"publication refuses stale {source} listing evidence")
        matches = [row for row in rows if row.source_id == source_id
                   and row.issue_url == issue_url
                   and row.status in {"open", "active", "funded", "published"}]
        if not matches:
            raise ValueError(f"approved bounty is no longer open on {source}")
        current = matches[0]
        if current.bounty_usd < approved_bounty:
            raise ValueError(f"{source} reward is lower than the approved bounty")
        if source == "boss" and current.payout_country_eligible is not True:
            raise ValueError("Boss payout-country eligibility must be confirmed")
        return {
            "source": source, "source_id": current.source_id,
            "issue_url": current.issue_url, "bounty_usd": current.bounty_usd,
            "fresh": True, "payout_requires_sponsor_action": True,
            "payment_assurance": current.evidence.get("payment_assurance"),
        }
    if source == "taskbounty":
        client = taskbounty.TaskBountyClient(cache_ttl_seconds=0)
        payload, stale = client.fetch_summary(timeout_seconds)
        if stale:
            raise ValueError("publication refuses stale TaskBounty funding evidence")
        matches = [
            item
            for item in taskbounty.normalize(payload)
            if item.source_id == source_id and item.status in {"open", "active", "funded", "published"}
        ]
        if not matches:
            raise ValueError("approved task is no longer open on TaskBounty")
        current = matches[0]
        html, detail_stale = client.fetch_detail(current, timeout_seconds)
        if detail_stale:
            raise ValueError("publication refuses stale TaskBounty detail evidence")
        taskbounty.enrich_from_detail_html(current, html)
        if current.issue_url != issue_url or current.bounty_usd < approved_bounty:
            raise ValueError("TaskBounty issue or funding no longer matches approval")
        return {
            "source": source,
            "source_id": current.source_id,
            "issue_url": current.issue_url,
            "bounty_usd": current.bounty_usd,
            "fresh": True,
        }
    if source == "bountypay":
        client = bountypay.BountyPayClient(cache_ttl_seconds=0)
        payload, stale = client.fetch(timeout_seconds)
        if stale:
            raise ValueError("publication refuses stale BountyPay funding evidence")
        matches = [
            item
            for item in bountypay.normalize(payload)
            if item.source_id == source_id
            and item.issue_url == issue_url
            and item.status in {"open", "active", "funded", "published"}
        ]
        if not matches:
            raise ValueError("approved bounty is no longer open on BountyPay")
        current = matches[0]
        if current.evidence.get("payment_testnet"):
            raise ValueError("publication refuses testnet BountyPay funding")
        if current.bounty_usd < approved_bounty:
            raise ValueError("BountyPay funding is lower than the approved bounty")
        return {
            "source": source,
            "source_id": current.source_id,
            "issue_url": current.issue_url,
            "bounty_usd": current.bounty_usd,
            "network": current.evidence.get("bountypay_network"),
            "chain_id": current.evidence.get("bountypay_chain_id"),
            "transaction_hash": current.evidence.get(
                "bountypay_transaction_hash"
            ),
            "fresh": True,
        }
    raise ValueError(
        "public publication requires fresh validation from a supported funded marketplace"
    )


def _json_output(result: subprocess.CompletedProcess, description: str) -> Any:
    try:
        return json.loads(result.stdout)
    except (TypeError, ValueError) as error:
        raise ValueError(f"GitHub CLI returned invalid {description} JSON") from error


def _github_publication_guard(
    repository: str,
    issue_url: str,
    runner: Callable[..., subprocess.CompletedProcess],
) -> Dict[str, Any]:
    issue_match = re.fullmatch(
        rf"https://github\.com/{re.escape(repository)}/issues/(\d+)", issue_url
    )
    if not issue_match:
        raise ValueError("publication issue URL does not match the repository")
    issue_number = int(issue_match.group(1))
    repo_payload = _json_output(
        _run(["gh", "api", f"repos/{repository}"], runner), "repository"
    )
    if repo_payload.get("private") or repo_payload.get("archived") or repo_payload.get("disabled"):
        raise ValueError("repository is no longer public and active")
    issue_payload = _json_output(
        _run(["gh", "api", f"repos/{repository}/issues/{issue_number}"], runner),
        "issue",
    )
    if issue_payload.get("state") != "open" or issue_payload.get("locked"):
        raise ValueError("approved GitHub issue is no longer open and unlocked")
    if "pull_request" in issue_payload:
        raise ValueError("approved GitHub issue URL now resolves to a pull request")
    comments_payload = _json_output(
        _run(
            [
                "gh",
                "api",
                f"repos/{repository}/issues/{issue_number}/comments",
                "--paginate",
                "--slurp",
            ],
            runner,
        ),
        "issue comments",
    )
    comment_pages = comments_payload if isinstance(comments_payload, list) else []
    comments = [
        comment
        for page in comment_pages
        for comment in (page if isinstance(page, list) else [])
    ]
    reputation = reputation_policy_signals(issue_payload, comments)
    if reputation["bounty_withdrawal_signal"]:
        raise ValueError("GitHub discussion now indicates the bounty was withdrawn")
    if reputation["ai_contribution_reputation_risk"]:
        raise ValueError(
            "GitHub discussion now indicates AI-assisted contribution reputation risk"
        )
    timeline_payload = _json_output(
        _run(
            [
                "gh",
                "api",
                f"repos/{repository}/issues/{issue_number}/timeline",
                "--paginate",
                "--slurp",
                "-H",
                "Accept: application/vnd.github+json",
            ],
            runner,
        ),
        "issue timeline",
    )
    pages = timeline_payload if isinstance(timeline_payload, list) else []
    events = [event for page in pages for event in (page if isinstance(page, list) else [])]
    if _competition(events, issue_number):
        raise ValueError("an open competing pull request appeared after approval")
    return {
        "repository": repository,
        "issue_number": issue_number,
        "issue_updated_at": issue_payload.get("updated_at"),
        "repository_pushed_at": repo_payload.get("pushed_at"),
        "competing_pull_requests": [],
        "reputation_policy": reputation,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def submission_package_sha256(directory: str) -> str:
    root = Path(directory).resolve()
    digest = hashlib.sha256()
    for name in PACKAGE_FILES:
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"submission package is missing regular file {name}")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _run(
    arguments: Iterable[str],
    runner: Callable[..., subprocess.CompletedProcess],
    *,
    cwd: Optional[Path] = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    return runner(
        list(arguments),
        cwd=str(cwd) if cwd else None,
        check=check,
        capture_output=True,
        text=True,
    )


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug[:40].rstrip("-") or "bounty-fix")


def _approved_context(history: History, attempt_id: int) -> Dict[str, Any]:
    attempt = history.get_attempt(attempt_id)
    if attempt["status"] != "submission_approved":
        raise ValueError("submission preparation requires second human approval")
    bundle = history.latest_artifact(attempt_id, "review_bundle")
    approved = attempt.get("approved_bundle_sha256")
    if not bundle or not approved or bundle["sha256"] != approved:
        raise ValueError("latest review bundle is not the approved bundle")
    if review_bundle_sha256(bundle["path"]) != approved:
        raise ValueError("approved review bundle integrity check failed")
    record = history.get_attempt_record(attempt_id)
    opportunity = record["opportunity"]
    repository = opportunity.get("repository")
    issue_url = opportunity.get("issue_url")
    if not isinstance(repository, str) or not GITHUB_REPOSITORY_RE.fullmatch(
        repository
    ):
        raise ValueError("approved opportunity has no safe GitHub repository")
    expected_prefix = f"https://github.com/{repository}/issues/"
    if not isinstance(issue_url, str) or not issue_url.startswith(expected_prefix):
        raise ValueError("approved opportunity has no matching GitHub issue")
    manifest_path = Path(bundle["path"]).resolve() / "manifest.json"
    review_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    base_commit = review_manifest.get("base_commit")
    if not isinstance(base_commit, str) or not COMMIT_RE.fullmatch(base_commit):
        raise ValueError("approved review bundle has an invalid base commit")
    preflight = history.latest_artifact(attempt_id, "preflight")
    if not preflight:
        raise ValueError("attempt has no preflight provenance")
    preflight_path = Path(preflight["path"]).resolve()
    if not preflight_path.is_file() or _sha256(preflight_path) != preflight["sha256"]:
        raise ValueError("preflight provenance integrity check failed")
    preflight_manifest = json.loads(preflight_path.read_text(encoding="utf-8"))
    if preflight_manifest.get("commit") != base_commit:
        raise ValueError("approved bundle base differs from preflight commit")
    source = Path(preflight_manifest.get("source_path", "")).resolve()
    if not source.is_dir():
        raise ValueError("preflight source checkout is missing")
    return {
        "attempt": attempt,
        "bundle": bundle,
        "record": record,
        "opportunity": opportunity,
        "review_manifest": review_manifest,
        "base_commit": base_commit,
        "source": source,
        "repository": repository,
        "issue_url": issue_url,
    }


def prepare_submission(
    history: History,
    attempt_id: int,
    checkout_root: str = "data/submissions",
    report_root: str = "reports/attempts",
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Dict[str, Any]:
    context = _approved_context(history, attempt_id)
    number = history.artifact_count(attempt_id, "submission_package") + 1
    checkout = (
        Path(checkout_root).resolve()
        / f"attempt-{attempt_id}"
        / f"submission-{number}"
    )
    package = (
        Path(report_root).resolve()
        / f"attempt-{attempt_id}"
        / f"submission-{number}"
    )
    if checkout.exists() or package.exists():
        raise ValueError("submission paths already exist; refusing to overwrite")
    package.mkdir(parents=True)
    patch_source = Path(context["bundle"]["path"]).resolve() / "changes.patch"
    patch_path = package / "changes.patch"
    shutil.copyfile(patch_source, patch_path)
    patch_sha256 = _sha256(patch_path)
    if patch_sha256 != context["review_manifest"].get("patch_sha256"):
        raise ValueError("approved patch hash differs from review manifest")

    branch = f"bounty-hunter/{attempt_id}-{_slug(context['opportunity']['title'])}"
    _run(
        ["git", "clone", "--no-hardlinks", str(context["source"]), str(checkout)],
        runner,
    )
    try:
        _run(["git", "checkout", "--detach", context["base_commit"]], runner, cwd=checkout)
        _run(["git", "checkout", "-b", branch], runner, cwd=checkout)
        _run(["git", "apply", "--index", str(patch_path)], runner, cwd=checkout)
        _run(["git", "config", "user.name", "Bounty Hunter"], runner, cwd=checkout)
        _run(
            ["git", "config", "user.email", "bounty-hunter@localhost"],
            runner,
            cwd=checkout,
        )
        title = context["opportunity"]["title"].strip()
        commit_title = f"fix: {title}"[:72].rstrip()
        _run(
            ["git", "-c", "core.hooksPath=/dev/null", "commit", "-m", commit_title],
            runner,
            cwd=checkout,
        )
        commit = _run(["git", "rev-parse", "HEAD"], runner, cwd=checkout).stdout.strip()
        generated_patch = _run(
            [
                "git",
                "diff",
                "--binary",
                "--no-ext-diff",
                context["base_commit"],
                commit,
            ],
            runner,
            cwd=checkout,
        ).stdout
        if hashlib.sha256(generated_patch.encode("utf-8")).hexdigest() != patch_sha256:
            raise ValueError("prepared commit differs from the approved patch")
    except Exception:
        shutil.rmtree(checkout, ignore_errors=True)
        shutil.rmtree(package, ignore_errors=True)
        raise

    body = (
        f"Closes {context['issue_url']}\n\n"
        "## Verification\n\n"
        "- Regression test fails against the approved base commit.\n"
        "- Full configured verification passes with this patch.\n"
        f"- Review bundle: `{context['bundle']['sha256']}`\n\n"
        "## Review disclosure\n\n"
        "This patch was prepared with AI assistance in an isolated workflow "
        "and was reviewed by a human before publication.\n"
    )
    body_path = package / "PR_BODY.md"
    body_path.write_text(body, encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repository": context["repository"],
        "issue_url": context["issue_url"],
        "title": context["opportunity"]["title"],
        "branch": branch,
        "base_commit": context["base_commit"],
        "prepared_commit": commit,
        "checkout_path": str(checkout),
        "approved_bundle_sha256": context["bundle"]["sha256"],
        "patch_sha256": patch_sha256,
        "pr_body_sha256": _sha256(body_path),
    }
    manifest_path = package / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    digest = submission_package_sha256(str(package))
    (package / "SHA256").write_text(digest + "\n", encoding="utf-8")
    history.add_artifact(attempt_id, "submission_package", str(package), digest)
    return {"path": str(package), "sha256": digest, **manifest}


def publish_submission(
    history: History,
    attempt_id: int,
    bundle_sha256: str,
    package_sha256: str,
    confirmation: str,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    marketplace_validator: Callable[[Dict[str, Any]], Dict[str, Any]] = validate_marketplace_funding,
) -> Dict[str, Any]:
    context = _approved_context(history, attempt_id)
    if bundle_sha256 != context["bundle"]["sha256"]:
        raise ValueError("publication confirmation references the wrong bundle")
    artifact = history.latest_artifact(attempt_id, "submission_package")
    if not artifact or submission_package_sha256(artifact["path"]) != artifact["sha256"]:
        raise ValueError("submission package integrity check failed")
    if package_sha256 != artifact["sha256"]:
        raise ValueError("publication confirmation references the wrong package")
    required = f"publish:{attempt_id}:{bundle_sha256}:{package_sha256}"
    if confirmation != required:
        raise ValueError(f"public submission requires exact confirmation: {required}")
    package = Path(artifact["path"]).resolve()
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("approved_bundle_sha256") != bundle_sha256:
        raise ValueError("submission package is not bound to the approved bundle")
    checkout = Path(manifest.get("checkout_path", "")).resolve()
    commit = _run(["git", "rev-parse", "HEAD"], runner, cwd=checkout).stdout.strip()
    if commit != manifest.get("prepared_commit"):
        raise ValueError("prepared submission checkout has drifted")
    diff = _run(
        ["git", "diff", "--binary", "--no-ext-diff", manifest["base_commit"], commit],
        runner,
        cwd=checkout,
    ).stdout
    if hashlib.sha256(diff.encode("utf-8")).hexdigest() != manifest["patch_sha256"]:
        raise ValueError("prepared submission patch no longer matches approval")

    funding_snapshot = marketplace_validator(context["opportunity"])
    github_snapshot = _github_publication_guard(
        manifest["repository"], manifest["issue_url"], runner
    )

    owner, repo = manifest["repository"].split("/", 1)
    login = _run(["gh", "api", "user", "--jq", ".login"], runner).stdout.strip()
    if not re.fullmatch(r"[A-Za-z0-9-]+", login):
        raise ValueError("GitHub CLI returned an unsafe user login")
    fork_name = f"{login}/{repo}"
    fork = _run(
        ["gh", "repo", "view", fork_name, "--json", "name"],
        runner,
        check=False,
    )
    if fork.returncode != 0:
        _run(["gh", "repo", "fork", manifest["repository"], "--clone=false"], runner)
    remotes = _run(["git", "remote"], runner, cwd=checkout).stdout.splitlines()
    if "bounty-fork" not in remotes:
        _run(
            ["git", "remote", "add", "bounty-fork", f"https://github.com/{fork_name}.git"],
            runner,
            cwd=checkout,
        )
    _run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "push",
            "bounty-fork",
            f"HEAD:refs/heads/{manifest['branch']}",
        ],
        runner,
        cwd=checkout,
    )
    base_branch = _run(
        [
            "gh",
            "repo",
            "view",
            manifest["repository"],
            "--json",
            "defaultBranchRef",
            "--jq",
            ".defaultBranchRef.name",
        ],
        runner,
    ).stdout.strip()
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", base_branch):
        raise ValueError("GitHub CLI returned an unsafe default branch")
    result = _run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            manifest["repository"],
            "--head",
            f"{login}:{manifest['branch']}",
            "--base",
            base_branch,
            "--title",
            manifest["title"],
            "--body-file",
            str(package / "PR_BODY.md"),
        ],
        runner,
        cwd=checkout,
    )
    pr_url = result.stdout.strip().splitlines()[-1]
    match = PULL_REQUEST_RE.fullmatch(pr_url)
    if not match or f"{match.group(1)}/{match.group(2)}".lower() != manifest[
        "repository"
    ].lower():
        raise ValueError("GitHub CLI did not return the expected upstream PR URL")
    history.transition(attempt_id, "submitted", pr_url=pr_url)
    receipt_path = package.parent / f"publication-{history.artifact_count(attempt_id, 'publication_receipt') + 1}.json"
    receipt = {
        "attempt_id": attempt_id,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "pr_url": pr_url,
        "repository": manifest["repository"],
        "branch": manifest["branch"],
        "prepared_commit": commit,
        "approved_bundle_sha256": bundle_sha256,
        "submission_package_sha256": artifact["sha256"],
        "funding_snapshot": funding_snapshot,
        "github_snapshot": github_snapshot,
    }
    receipt_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    history.add_artifact(
        attempt_id, "publication_receipt", str(receipt_path), _sha256(receipt_path)
    )
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare or explicitly publish an approved bounty PR."
    )
    parser.add_argument("--history-db", default="data/bounty_hunter.db")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("attempt_id", type=int)
    prepare.add_argument("--checkout-root", default="data/submissions")
    prepare.add_argument("--report-root", default="reports/attempts")
    publish = subparsers.add_parser("publish")
    publish.add_argument("attempt_id", type=int)
    publish.add_argument("--bundle-sha256", required=True)
    publish.add_argument("--package-sha256", required=True)
    publish.add_argument("--confirm-public-submission", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    history = History(args.history_db)
    try:
        if args.command == "prepare":
            result = prepare_submission(
                history, args.attempt_id, args.checkout_root, args.report_root
            )
        else:
            result = publish_submission(
                history,
                args.attempt_id,
                args.bundle_sha256,
                args.package_sha256,
                args.confirm_public_submission,
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps({"error": str(error)}, indent=2))
        return 1
    finally:
        history.close()


if __name__ == "__main__":
    raise SystemExit(main())
