"""GUI-independent scan engine, sharing the existing fail-closed CLI policies."""
import json
import os
from dataclasses import replace
from pathlib import Path

from . import cli
from .config import ScoringConfig
from .desktop_settings import normalize_settings, workspace_paths
from .history import History
from .report import ranked_records, write_json, write_markdown


def scan_arguments(settings: dict):
    settings = normalize_settings(settings)
    paths = workspace_paths(settings)
    root = paths["root"]
    args = [
        "--history-db", str(paths["db"]),
        "--output-dir", str(paths["reports"]),
        "--timeout", str(settings["timeout_seconds"]),
        "--github-token-env", settings["github_token_env"],
        "--github-cache", str(root / "data" / "github-cache.json"),
        "--bountypay-cache", str(root / "data" / "bountypay-cache.json"),
        "--boss-cache", str(root / "data" / "boss-cache.json"),
        "--opire-cache", str(root / "data" / "opire-cache.html"),
        "--taskbounty-cache-dir", str(root / "data" / "taskbounty-cache"),
        "--algora-cache-dir", str(root / "data" / "algora-cache"),
        "--algora-discovery-cache-dir", str(root / "data" / "algora-discovery-cache"),
        "--algora-discovery-report", str(paths["algora_audit"]),
    ]
    for source in ("bountypay", "boss", "opire", "taskbounty"):
        if not settings["sources"][source]:
            args.append("--no-" + source)
    # Algora defaults to no organizations if the user did not add any.
    if settings["sources"]["algora"]:
        for organization in settings["algora_organizations"]:
            args.extend(["--algora-org", organization])
    if settings["boss_payout_country"]:
        args.extend(["--boss-payout-country", settings["boss_payout_country"]])
    parsed = cli._parser().parse_args(args)
    parsed.no_algora = not settings["sources"]["algora"]
    return parsed


def run_scan(settings: dict, progress=None) -> dict:
    """Run synchronously; desktop runs it in a worker, never the UI thread."""
    args = scan_arguments(settings)
    args.progress = progress
    paths = workspace_paths(normalize_settings(settings))
    paths["reports"].mkdir(parents=True, exist_ok=True)
    args.history_db = str(paths["db"])
    opportunities = cli._collect(args)
    history = History(args.history_db)
    try:
        calibration = history.outcome_calibration()
        config = replace(
            ScoringConfig(),
            default_execution_cost_usd=calibration["default_execution_cost_usd"],
            default_human_hours=calibration["default_human_hours"],
        )
        records = ranked_records(
            opportunities,
            config=config,
            calibration_factor=calibration["probability_factor"],
        )
        write_json(records, str(paths["opportunities"]),
                   source_health=getattr(args, "source_health", []), calibration=calibration)
        write_markdown(records, str(paths["reports"] / "opportunities.md"),
                       source_health=getattr(args, "source_health", []), calibration=calibration)
        scan_id = history.record_scan(records)
    finally:
        history.close()
    return {
        "count": len(records),
        "eligible": sum(1 for item in records if item["score"]["eligible"]),
        "scan_id": scan_id,
        "source_health": getattr(args, "source_health", []),
        "report_path": str(paths["opportunities"]),
    }


def read_report(settings: dict) -> dict:
    path = workspace_paths(normalize_settings(settings))["opportunities"]
    if not path.is_file():
        return {"opportunities": [], "source_health": [], "generated_at": None}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("opportunities"), list):
        raise ValueError("Saved opportunity report has invalid structure")
    return payload



def recheck_github_opportunity(settings: dict, source: str, source_id: str) -> dict:
    """Refresh one selected GitHub issue from live API evidence.

    This does not bypass any rejection gates. Unavailable, stale, or
    rate-limited evidence cannot make an opportunity eligible.
    """
    from .github import GitHubClient, enrich_opportunity, parse_issue_url
    from .models import Opportunity

    settings = normalize_settings(settings)
    paths = workspace_paths(settings)
    report = read_report(settings)
    selected = next(
        (
            item for item in report["opportunities"]
            if item["opportunity"]["source"] == source
            and str(item["opportunity"]["source_id"]) == str(source_id)
        ),
        None,
    )
    if selected is None:
        raise ValueError("Selected bounty is no longer in the latest scan. Scan again.")
    opportunity = Opportunity.from_dict(selected["opportunity"])
    if not parse_issue_url(opportunity.issue_url):
        raise ValueError(
            "This marketplace listing has no usable GitHub issue URL. "
            "Open the provider listing to check whether it links to a public issue."
        )
    # Clear only prior GitHub-derived signals. Marketplace facts and payout
    # restrictions are retained; old GitHub errors must not poison a retry.
    from .cli import HISTORICAL_GITHUB_FIELDS
    for field in HISTORICAL_GITHUB_FIELDS:
        setattr(opportunity, field, None)
    for key in (
        "github_error", "github_enriched_at", "github_not_found",
        "github_evidence_stale", "github_enrichment_skipped",
        "github_verification_status", "github_issue_updated_at", "github_repo_pushed_at",
        "github_repo_created_at", "bounty_withdrawal_signal",
        "bounty_withdrawal_provenance", "ai_contribution_reputation_risk",
        "ai_contribution_risk_provenance", "tree_truncated",
    ):
        opportunity.evidence.pop(key, None)
    # Force fresh evidence for a deliberate single-item recheck, while
    # retaining request limits and failing closed on GitHub errors.
    client = GitHubClient(
        token=os.environ.get(settings["github_token_env"]),
        timeout_seconds=settings["timeout_seconds"],
        cache_path=str(paths["root"] / "data" / "github-cache.json"),
        cache_ttl_seconds=0,
        maximum_requests_per_run=15,
    )
    enrich_opportunity(opportunity, client)
    if opportunity.evidence.get("github_error"):
        raise RuntimeError(
            "GitHub verification was unsuccessful (HTTP {}). "
            "Check GitHub connectivity, authentication or API limits; "
            "this opportunity remains ineligible.".format(
                opportunity.evidence["github_error"]
            )
        )
    if (
        not opportunity.evidence.get("github_enriched_at")
        and not opportunity.evidence.get("github_not_found")
    ):
        raise RuntimeError("GitHub could not provide fresh verification.")
    opportunity.evidence["github_verification_status"] = (
        "not_found" if opportunity.evidence.get("github_not_found") else "verified"
    )
    updated = []
    for item in report["opportunities"]:
        prior = item["opportunity"]
        if prior["source"] == source and str(prior["source_id"]) == str(source_id):
            updated.append(opportunity)
        else:
            updated.append(Opportunity.from_dict(prior))
    calibration = report.get("calibration") or {}
    config = replace(
        ScoringConfig(),
        default_execution_cost_usd=calibration.get(
            "default_execution_cost_usd", ScoringConfig().default_execution_cost_usd
        ),
        default_human_hours=calibration.get(
            "default_human_hours", ScoringConfig().default_human_hours
        ),
    )
    records = ranked_records(
        updated,
        config=config,
        calibration_factor=calibration.get("probability_factor", 1.0),
    )
    health = list(report.get("source_health") or [])
    health.append({
        "source": "github_selected_recheck",
        "status": "fresh" if opportunity.evidence.get("github_enriched_at") else "not_found",
        "attempted": 1,
        "requests": client.requests_made,
    })
    paths["reports"].mkdir(parents=True, exist_ok=True)
    write_json(records, str(paths["opportunities"]), source_health=health, calibration=calibration)
    write_markdown(
        records, str(paths["reports"] / "opportunities.md"),
        source_health=health, calibration=calibration,
    )
    history = History(str(paths["db"]))
    try:
        history.record_scan(records)
    finally:
        history.close()
    outcome = next(
        item["score"] for item in records
        if item["opportunity"]["source"] == source
        and str(item["opportunity"]["source_id"]) == str(source_id)
    )
    return {
        "decision": outcome["decision"],
        "eligible": outcome["eligible"],
        "requests": client.requests_made,
    }


def run_algora_audit(settings: dict) -> dict:
    """Use public GitHub evidence to discover boards; results remain untrusted hints."""
    from .algora_discovery import (
        AlgoraBoardClient, GitHubClient, SEARCH_QUERY, audit_candidates,
        candidate_org_counts, search_candidate_items, write_report,
    )
    from datetime import datetime, timezone

    settings = normalize_settings(settings)
    paths = workspace_paths(settings)
    root = paths["root"]
    github = GitHubClient(
        token=os.environ.get(settings["github_token_env"]),
        timeout_seconds=settings["timeout_seconds"],
        cache_path=str(root / "data" / "github-discovery-cache.json"),
        cache_ttl_seconds=24 * 60 * 60,
    )
    client = AlgoraBoardClient(
        cache_dir=str(root / "data" / "algora-discovery-cache"),
        cache_ttl_seconds=24 * 60 * 60,
        minimum_request_interval_seconds=2,
    )
    found = search_candidate_items(github, 3)
    counts = candidate_org_counts(found.get("items") or [])
    candidates = audit_candidates(
        counts, client,
        configured=settings["algora_organizations"],
        maximum_candidates=12, timeout_seconds=settings["timeout_seconds"],
    )
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "search_query": SEARCH_QUERY,
        "search_total_count": found.get("total_count"),
        "search_items_examined": len(found.get("items") or []),
        "search_pages_fetched": found["pages_fetched"],
        "configured_organizations": settings["algora_organizations"],
        "candidates": candidates,
        "review_candidates": [
            item["organization"] for item in candidates
            if item.get("promotion") == "review_for_trusted_list"
        ],
        "policy": "GitHub hints need fresh Algora board evidence; manual approval is still required.",
    }
    paths["reports"].mkdir(parents=True, exist_ok=True)
    write_report(str(paths["algora_audit"]), report)
    return report

