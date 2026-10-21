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
    else:
        # Ignore any discovery audit when Algora is disabled, and do not
        # read old cache/report as an enabled discovery source.
        args.extend(["--algora-discovery-report", str(root / "disabled-algora.json")])
    if settings["boss_payout_country"]:
        args.extend(["--boss-payout-country", settings["boss_payout_country"]])
    return cli._parser().parse_args(args)


def run_scan(settings: dict) -> dict:
    """Run synchronously; desktop runs it in a worker, never the UI thread."""
    args = scan_arguments(settings)
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
                   source_health=args.source_health, calibration=calibration)
        write_markdown(records, str(paths["reports"] / "opportunities.md"),
                       source_health=args.source_health, calibration=calibration)
        scan_id = history.record_scan(records)
    finally:
        history.close()
    return {
        "count": len(records),
        "eligible": sum(1 for item in records if item["score"]["eligible"]),
        "scan_id": scan_id,
        "source_health": args.source_health,
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
