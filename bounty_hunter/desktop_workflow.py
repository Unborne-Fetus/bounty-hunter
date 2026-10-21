"""Supervised attempt operations exposed as GUI commands, not shell strings.

Every operation uses the existing History safety state machine, immutable
review hashes and public publication confirmation. No approval is automatic.
"""
import json
from pathlib import Path

from .desktop_settings import normalize_settings, workspace_paths
from .desktop_service import read_report
from .history import History


def _history(settings):
    path = workspace_paths(normalize_settings(settings))["db"]
    return History(str(path))


def queue_opportunity(settings, source: str, source_id: str) -> int:
    report = read_report(settings)
    matching = [
        r for r in report["opportunities"]
        if r["opportunity"]["source"] == source
        and str(r["opportunity"]["source_id"]) == str(source_id)
    ]
    if len(matching) != 1:
        raise ValueError("Select an opportunity from the most recent scan")
    record = matching[0]
    if not record["score"]["eligible"]:
        raise ValueError(
            "This bounty did not pass payout/repository checks. "
            "You may read its reasons, but cannot send it to the agent."
        )
    history = _history(settings)
    try:
        return history.queue_from_record(record)
    finally:
        history.close()


def review_attempt(settings, attempt_id: int) -> dict:
    history = _history(settings)
    try:
        attempt = history.get_attempt(attempt_id)
        record = history.get_attempt_record(attempt_id)
        result = {
            "attempt": attempt,
            "record": record,
            "record_sha256": history.attempt_record_sha256(attempt_id),
            "bundle": history.latest_artifact(attempt_id, "review_bundle"),
            "package": history.latest_artifact(attempt_id, "submission_package"),
        }
        return result
    finally:
        history.close()


def approve_attempt(settings, attempt_id: int, record_sha256: str, note: str):
    if not note.strip():
        raise ValueError("Approval note is required")
    history = _history(settings)
    try:
        history.approve(attempt_id, note.strip(), record_sha256)
    finally:
        history.close()


def approve_review_bundle(settings, attempt_id: int, bundle_sha256: str, note: str):
    """Require reviewed bundle hash, in addition to UI checkbox confirmation."""
    if not note.strip():
        raise ValueError("Submission approval note is required")
    history = _history(settings)
    try:
        history.approve_submission(attempt_id, note.strip(), bundle_sha256)
    finally:
        history.close()


def perform_step(settings, attempt_id: int, step: str):
    """May block for many minutes. Must run off the GUI thread."""
    from .preflight import run_preflight
    from .attempt import generate_patch, verify_patch
    from .submission import prepare_submission
    paths = workspace_paths(normalize_settings(settings))
    history = History(str(paths["db"]))
    try:
        if step == "preflight":
            return run_preflight(
                history, attempt_id,
                workspace_root=str(paths["attempts"]),
                report_root=str(paths["attempt_reports"]),
            )
        if step == "patch":
            return generate_patch(
                history, attempt_id, report_root=str(paths["attempt_reports"])
            )
        if step == "verify":
            return verify_patch(
                history, attempt_id, report_root=str(paths["attempt_reports"])
            )
        if step == "prepare":
            return prepare_submission(
                history, attempt_id,
                checkout_root=str(paths["submission_checkouts"]),
                report_root=str(paths["attempt_reports"]),
            )
        raise ValueError("Unknown attempt step")
    finally:
        history.close()


def publish_approved(settings, attempt_id: int, bundle_sha256: str, package_sha256: str):
    """Call only after an explicit, separate user confirmation in the GUI."""
    from .submission import publish_submission
    history = _history(settings)
    try:
        return publish_submission(
            history, attempt_id, bundle_sha256, package_sha256,
            f"publish:{attempt_id}:{bundle_sha256}:{package_sha256}",
        )
    finally:
        history.close()


def bundle_preview(settings, attempt_id: int, limit: int = 70000) -> dict:
    history = _history(settings)
    try:
        attempt = history.get_attempt(attempt_id)
        artifact = history.latest_artifact(attempt_id, "review_bundle")
        if not artifact:
            raise ValueError("No reviewed verification bundle exists")
        from .review import review_bundle_sha256
        path = Path(artifact["path"]).resolve()
        if review_bundle_sha256(str(path)) != artifact["sha256"]:
            raise ValueError("Verification bundle integrity check failed")
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        patch = (path / "changes.patch").read_text(encoding="utf-8")
        return {
            "attempt": attempt,
            "bundle_sha256": artifact["sha256"],
            "manifest": manifest,
            "patch_preview": patch[:limit],
            "patch_truncated": len(patch) > limit,
            "path": str(path),
        }
    finally:
        history.close()


def package_preview(settings, attempt_id: int) -> dict:
    history = _history(settings)
    try:
        attempt = history.get_attempt(attempt_id)
        artifact = history.latest_artifact(attempt_id, "submission_package")
        if not artifact:
            raise ValueError("No prepared submission package exists")
        from .submission import submission_package_sha256
        path = Path(artifact["path"]).resolve()
        if submission_package_sha256(str(path)) != artifact["sha256"]:
            raise ValueError("Prepared submission package integrity check failed")
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        pr_body = (path / "PR_BODY.md").read_text(encoding="utf-8")
        return {
            "attempt": attempt,
            "package_sha256": artifact["sha256"],
            "bundle_sha256": attempt["approved_bundle_sha256"],
            "manifest": manifest,
            "body_preview": pr_body[:12000],
            "path": str(path),
        }
    finally:
        history.close()
