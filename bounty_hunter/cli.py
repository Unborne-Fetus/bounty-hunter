import argparse
import json
import os
import re
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from .github import GitHubClient, enrich_opportunity, parse_issue_url, resolve_github_token
from .config import ScoringConfig
from .history import History
from .models import Opportunity
from .report import ranked_records, write_json, write_markdown
from .sources import algora, boss, bountypay, opire, taskbounty


HISTORICAL_GITHUB_FIELDS = (
    "repository",
    "public_repository",
    "issue_open",
    "issue_activity_recent",
    "repository_archived",
    "repository_created_at",
    "repository_stars",
    "repository_license",
    "language",
    "maintainer_active",
    "has_test_suite",
    "acceptance_criteria_clear",
    "contribution_guide_found",
    "security_sensitive",
    "competing_prs",
)


def _discovered_algora_orgs(path: str, maximum_age_hours: float) -> tuple:
    report = Path(path).resolve()
    if not report.exists():
        return [], {"status": "missing", "path": path, "organizations_added": 0}
    try:
        payload = json.loads(report.read_text(encoding="utf-8"))
        generated_at = datetime.fromisoformat(
            str(payload["generated_at"]).replace("Z", "+00:00")
        )
        age_hours = (
            datetime.now(timezone.utc) - generated_at.astimezone(timezone.utc)
        ).total_seconds() / 3600
        if age_hours < 0 or age_hours > maximum_age_hours:
            return [], {
                "status": "stale",
                "path": path,
                "generated_at": generated_at.isoformat(),
                "organizations_added": 0,
            }
        organizations = []
        for item in payload.get("candidates") or []:
            if not isinstance(item, dict):
                continue
            organization = str(item.get("organization") or "")
            if (
                item.get("status") == "fresh"
                and item.get("promotion") == "review_for_trusted_list"
                and int(item.get("open_board_rows") or 0) > 0
                and re.fullmatch(r"[A-Za-z0-9_.-]+", organization)
            ):
                organizations.append(organization)
        return organizations, {
            "status": "fresh",
            "path": path,
            "generated_at": generated_at.isoformat(),
            "organizations_added": len(organizations),
        }
    except (OSError, ValueError, KeyError, TypeError) as error:
        return [], {
            "status": "error",
            "path": path,
            "error": str(error)[:300],
            "organizations_added": 0,
        }


def _needs_github_enrichment(opportunity: Opportunity) -> bool:
    """Verify linked open issues even if *another* policy blocks a payout.

    Previously the source-level payout gate prevented GitHub verification
    entirely for Boss and Opire, producing misleadingly low evidence
    coverage. Enrichment never overrides separate payout restrictions.
    """
    return bool(
        parse_issue_url(opportunity.issue_url)
        and opportunity.status.lower() in {"open", "active", "funded", "published"}
    )


def _github_priority(opportunity: Opportunity):
    """Use the limited API budget on actionable listings before risky ones."""
    preferred_reward = 50 <= opportunity.bounty_usd <= 500
    known_uncontested = opportunity.active_attempts == 0
    low_competition = opportunity.active_attempts in (None, 0, 1, 2)
    bug_like = any(
        term in opportunity.title.lower()
        for term in ("bug", "fix", "error", "test", "panic", "regression")
    )
    source_blocked = (
        opportunity.payout_requires_sponsor_action is True
        or opportunity.evidence.get("reward_outlier")
        or opportunity.evidence.get("payment_testnet")
        or any(opportunity.evidence.get(key) for key in (
            "algora_evidence_stale", "opire_evidence_stale",
            "taskbounty_evidence_stale", "taskbounty_detail_stale",
            "boss_evidence_stale", "bountypay_evidence_stale",
        ))
    )
    return (
        not source_blocked,
        preferred_reward,
        known_uncontested,
        low_competition,
        bug_like,
        opportunity.bounty_usd,
    )


def _apply_historical_github_fallback(
    opportunity: Opportunity, record: dict
) -> None:
    previous = record.get("opportunity") or {}
    for field in HISTORICAL_GITHUB_FIELDS:
        value = previous.get(field)
        if value is not None:
            setattr(opportunity, field, value)
    if previous.get("active_attempts") is not None:
        opportunity.active_attempts = max(
            opportunity.active_attempts or 0,
            int(previous["active_attempts"]),
        )
    opportunity.evidence["github_evidence_stale"] = True
    opportunity.evidence["github_evidence_scan_at"] = record.get(
        "historical_scan_at"
    )
    opportunity.notes.append(
        "GitHub API was unavailable; displaying prior evidence and rejecting "
        "the opportunity until fresh enrichment succeeds."
    )


def _load_manual(path: str) -> List[Opportunity]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    records = payload if isinstance(payload, list) else payload.get("opportunities", [])
    return [Opportunity.from_dict(record) for record in records]


def _collect(args: argparse.Namespace) -> List[Opportunity]:
    opportunities: List[Opportunity] = []
    source_health = []
    args.source_health = source_health
    progress = getattr(args, "progress", None) or (lambda message: None)
    if not args.no_bountypay:
        progress("Fetching BountyPay inventory")
        client = bountypay.BountyPayClient(
            cache_path=args.bountypay_cache,
            cache_ttl_seconds=int(args.bountypay_cache_ttl_hours * 60 * 60),
        )
        try:
            payload, stale = client.fetch(args.timeout)
            bountypay_opportunities = bountypay.normalize(
                payload, evidence_stale=stale
            )
            opportunities.extend(bountypay_opportunities)
            testnet_count = sum(
                1
                for item in bountypay_opportunities
                if item.evidence.get("payment_testnet")
            )
            source_health.append(
                {
                    "source": "bountypay",
                    "status": "stale" if stale else "fresh",
                    "opportunities": len(bountypay_opportunities),
                    "testnet_opportunities": testnet_count,
                    "mainnet_opportunities": (
                        len(bountypay_opportunities) - testnet_count
                    ),
                }
            )
            if stale:
                print(
                    "warning: BountyPay is using stale cached evidence",
                    file=sys.stderr,
                )
        except Exception as error:
            source_health.append(
                {
                    "source": "bountypay",
                    "status": "error",
                    "error": str(error)[:300],
                }
            )
            print(f"warning: BountyPay collection failed: {error}", file=sys.stderr)
    if not args.no_boss:
        progress("Fetching Boss inventory")
        client = boss.BossClient(
            cache_path=args.boss_cache,
            cache_ttl_seconds=int(args.boss_cache_ttl_hours * 60 * 60),
        )
        try:
            payload, stale = client.fetch(args.timeout)
            boss_opportunities = boss.normalize(
                payload,
                evidence_stale=stale,
                payout_country=args.boss_payout_country,
            )
            opportunities.extend(boss_opportunities)
            source_health.append(
                {
                    "source": "boss",
                    "status": "stale" if stale else "fresh",
                    "opportunities": len(boss_opportunities),
                    "payout_country": args.boss_payout_country,
                    "payout_country_confirmed": bool(args.boss_payout_country),
                }
            )
            if stale:
                print("warning: Boss is using stale cached evidence", file=sys.stderr)
        except Exception as error:
            source_health.append(
                {"source": "boss", "status": "error", "error": str(error)[:300]}
            )
            print(f"warning: Boss collection failed: {error}", file=sys.stderr)
    if not args.no_opire:
        progress("Fetching Opire inventory")
        client = opire.OpireClient(
            cache_path=args.opire_cache,
            cache_ttl_seconds=int(args.opire_cache_ttl_hours * 60 * 60),
        )
        try:
            html, stale = client.fetch(args.timeout)
            opire_opportunities = opire.normalize_html(html, evidence_stale=stale)
            metadata = opire.inventory_metadata(html)
            partial = (
                metadata["available_reported"] is not None
                and metadata["available_reported"] > len(opire_opportunities)
            )
            opportunities.extend(opire_opportunities)
            source_health.append(
                {
                    "source": "opire",
                    "status": "stale" if stale else ("partial" if partial else "fresh"),
                    "opportunities": len(opire_opportunities),
                    **metadata,
                }
            )
            if stale:
                print(
                    "warning: Opire is using stale cached evidence",
                    file=sys.stderr,
                )
        except Exception as error:
            source_health.append(
                {"source": "opire", "status": "error", "error": str(error)[:300]}
            )
            print(f"warning: Opire collection failed: {error}", file=sys.stderr)
    if not args.no_taskbounty:
        progress("Fetching TaskBounty inventory and issue details")
        client = taskbounty.TaskBountyClient(
            cache_dir=args.taskbounty_cache_dir,
            cache_ttl_seconds=int(args.taskbounty_cache_ttl_hours * 60 * 60),
        )
        try:
            payload, stale = client.fetch_summary(timeout_seconds=args.timeout)
            taskbounty_opportunities = taskbounty.normalize(
                payload, evidence_stale=stale
            )
            detail_errors = 0
            stale_details = 0
            for opportunity in taskbounty_opportunities:
                if opportunity.status in {"open", "active", "funded", "published"}:
                    try:
                        html, detail_stale = client.fetch_detail(
                            opportunity, args.timeout
                        )
                        taskbounty.enrich_from_detail_html(opportunity, html)
                        if detail_stale:
                            stale_details += 1
                            opportunity.evidence["taskbounty_detail_stale"] = True
                    except Exception as error:
                        detail_errors += 1
                        opportunity.notes.append(
                            f"TaskBounty detail enrichment failed: {error}"
                        )
            opportunities.extend(taskbounty_opportunities)
            source_health.append(
                {
                    "source": "taskbounty",
                    "status": (
                        "stale"
                        if stale
                        else ("partial" if detail_errors or stale_details else "fresh")
                    ),
                    "opportunities": len(taskbounty_opportunities),
                    "detail_errors": detail_errors,
                    "stale_details": stale_details,
                }
            )
        except Exception as error:
            source_health.append(
                {
                    "source": "taskbounty",
                    "status": "error",
                    "error": str(error)[:300],
                }
            )
            print(f"warning: TaskBounty collection failed: {error}", file=sys.stderr)
    algora_client = algora.AlgoraBoardClient(
        cache_dir=args.algora_cache_dir,
        cache_ttl_seconds=int(args.algora_cache_ttl_hours * 60 * 60),
        minimum_request_interval_seconds=args.algora_request_interval_seconds,
        maximum_requests_per_run=args.algora_maximum_requests_per_scan,
    )
    if getattr(args, "no_algora", False):
        discovered_orgs, discovery_health = [], {"status": "disabled"}
    else:
        discovered_orgs, discovery_health = _discovered_algora_orgs(
            args.algora_discovery_report, args.algora_discovery_report_max_age_hours
        )
    configured_lower = {org.lower() for org in args.algora_org}
    discovered_orgs = [
        org for org in discovered_orgs if org.lower() not in configured_lower
    ]
    discovery_health["organizations_added"] = len(discovered_orgs)
    source_health.append({"source": "algora_discovery_report", **discovery_health})
    discovery_client = algora.AlgoraBoardClient(
        cache_dir=args.algora_discovery_cache_dir,
        cache_ttl_seconds=int(args.algora_discovery_cache_ttl_hours * 60 * 60),
        minimum_request_interval_seconds=args.algora_request_interval_seconds,
        maximum_requests_per_run=args.algora_maximum_requests_per_scan,
    )
    org_clients = [] if getattr(args, "no_algora", False) else [
        (org, algora_client) for org in args.algora_org
    ]
    org_clients.extend((org, discovery_client) for org in discovered_orgs)
    if not org_clients and not getattr(args, "no_algora", False):
        has_audit = discovery_health.get("status") == "fresh"
        source_health.append({
            "source": "algora",
            "status": "no_verified_boards" if has_audit else "not_configured",
            "opportunities": 0,
            "message": (
                "Algora discovery completed but found no fresh public boards "
                "with parseable open bounties. The scan cannot invent listings."
                if has_audit else
                "No Algora boards are configured or recently discovered. "
                "Use Find actionable to refresh board discovery."
            ),
        })
    for org, board_client in org_clients:
        progress(f"Fetching Algora board: {org}")
        try:
            html, stale = board_client.fetch(org, args.timeout)
            org_opportunities = algora.normalize_html(
                org, html, evidence_stale=stale
            )
            opportunities.extend(org_opportunities)
            source_health.append(
                {
                    "source": "algora",
                    "organization": org,
                    "status": "stale" if stale else "fresh",
                    "opportunities": len(org_opportunities),
                    "fallback_reason": (
                        board_client.last_fallback_reason if stale else None
                    ),
                }
            )
            if stale:
                print(
                    f"warning: Algora org '{org}' is using stale cached evidence",
                    file=sys.stderr,
                )
        except Exception as error:
            source_health.append(
                {
                    "source": "algora",
                    "organization": org,
                    "status": "error",
                    "error": str(error)[:300],
                }
            )
            print(
                f"warning: Algora org '{org}' collection failed: {error}",
                file=sys.stderr,
            )
    for path in args.input:
        manual = _load_manual(path)
        opportunities.extend(manual)
        source_health.append(
            {
                "source": "manual",
                "path": path,
                "status": "fresh",
                "opportunities": len(manual),
            }
        )
    if not args.no_github_enrichment:
        progress("Verifying repositories and open issues with GitHub")
        client = GitHubClient(
            token=resolve_github_token(args.github_token_env),
            timeout_seconds=args.timeout,
            cache_path=args.github_cache,
            cache_ttl_seconds=int(args.github_cache_ttl_hours * 60 * 60),
            maximum_requests_per_run=args.github_maximum_requests_per_scan,
        )
        history = History(args.history_db)
        try:
            enrichment_candidates = sorted(
                (
                    opportunity
                    for opportunity in opportunities
                    if _needs_github_enrichment(opportunity)
                ),
                key=_github_priority,
                reverse=True,
            )
            candidate_ids = {id(item) for item in enrichment_candidates}
            skipped = len(opportunities) - len(enrichment_candidates)
            for opportunity in opportunities:
                if id(opportunity) not in candidate_ids:
                    opportunity.evidence["github_enrichment_skipped"] = True
                    if not parse_issue_url(opportunity.issue_url):
                        opportunity.evidence["github_verification_status"] = "missing_issue_link"
                        opportunity.notes.append(
                            "The marketplace did not provide a usable GitHub issue URL. "
                            "Open its provider listing to check the source link."
                        )
                    else:
                        opportunity.evidence["github_verification_status"] = "source_ineligible"
                        opportunity.notes.append(
                            "GitHub enrichment skipped because source-level evidence "
                            "already makes the opportunity ineligible."
                        )
            for opportunity in enrichment_candidates:
                if client.maximum_requests_per_run is not None and (
                    client.requests_made >= client.maximum_requests_per_run
                ):
                    opportunity.evidence["github_verification_status"] = "request_budget"
                    opportunity.notes.append(
                        "GitHub request budget reached before this issue could be checked. "
                        "Use Recheck all on the saved results or configure a GitHub token."
                    )
                    continue
                try:
                    enrich_opportunity(opportunity, client)
                except Exception as error:
                    opportunity.evidence["github_error"] = "unexpected"
                    opportunity.notes.append(
                        f"GitHub enrichment failed unexpectedly ({type(error).__name__})."
                    )
                if opportunity.evidence.get("github_error"):
                    opportunity.evidence["github_verification_status"] = "api_error"
                elif opportunity.evidence.get("github_not_found"):
                    opportunity.evidence["github_verification_status"] = "not_found"
                elif opportunity.evidence.get("github_enriched_at"):
                    opportunity.evidence["github_verification_status"] = "verified"
                else:
                    opportunity.evidence["github_verification_status"] = "incomplete"
                if opportunity.evidence.get("github_error"):
                    previous = history.latest_github_enriched_record(
                        opportunity.source, opportunity.source_id
                    )
                    if previous:
                        _apply_historical_github_fallback(opportunity, previous)
        finally:
            history.close()
        github_errors = sum(
            1 for item in opportunities if item.evidence.get("github_error")
        )
        github_stale = sum(
            1
            for item in opportunities
            if item.evidence.get("github_evidence_stale")
        )
        missing_links = sum(
            1 for item in opportunities
            if item.evidence.get("github_verification_status") == "missing_issue_link"
        )
        verified = sum(
            1 for item in opportunities
            if item.evidence.get("github_verification_status") == "verified"
        )
        unverified = sum(
            1 for item in opportunities
            if item.public_repository is None or item.issue_open is None
        )
        deferred = sum(
            1 for item in opportunities
            if item.evidence.get("github_verification_status") == "request_budget"
        )
        source_health.append(
            {
                "source": "github",
                "status": (
                    "partial" if github_errors or github_stale or unverified else "fresh"
                ),
                "opportunities": len(opportunities),
                "attempted": len(enrichment_candidates),
                "verified": verified,
                "unverified": unverified,
                "missing_issue_links": missing_links,
                "deferred_request_budget": deferred,
                "skipped": skipped,
                "errors": github_errors,
                "stale_fallbacks": github_stale,
                "requests": client.requests_made,
                "request_budget": args.github_maximum_requests_per_scan,
                "rate_limited_until": (
                    client.rate_limited_until.isoformat()
                    if client.rate_limited_until
                    else None
                ),
            }
        )
    return opportunities


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover and rank supervised coding-bounty opportunities."
    )
    parser.add_argument(
        "--input",
        action="append",
        default=[],
        help="Optional JSON file containing manually enriched opportunities.",
    )
    parser.add_argument(
        "--no-bountypay",
        action="store_true",
        help="Skip the live BountyPay public open-bounty API.",
    )
    parser.add_argument(
        "--bountypay-cache",
        default="data/bountypay-cache.json",
        help="Persistent cache for the BountyPay public open-bounty API.",
    )
    parser.add_argument(
        "--bountypay-cache-ttl-hours",
        type=float,
        default=6.0,
        help="Hours before cached BountyPay evidence must be refreshed.",
    )
    parser.add_argument(
        "--no-boss",
        action="store_true",
        help="Skip the live Boss public open-bounty API.",
    )
    parser.add_argument(
        "--boss-cache",
        default="data/boss-cache.json",
        help="Persistent cache for the Boss public open-bounty API.",
    )
    parser.add_argument(
        "--boss-cache-ttl-hours",
        type=float,
        default=6.0,
        help="Hours before cached Boss evidence must be refreshed.",
    )
    parser.add_argument(
        "--boss-payout-country",
        help=(
            "Two-letter payout-residency country code. Boss opportunities fail "
            "closed when omitted or unsupported."
        ),
    )
    parser.add_argument(
        "--no-opire",
        action="store_true",
        help="Skip the live Opire public rewards page.",
    )
    parser.add_argument(
        "--opire-cache",
        default="data/opire-cache.html",
        help="Persistent cache for the public Opire rewards page.",
    )
    parser.add_argument(
        "--opire-cache-ttl-hours",
        type=float,
        default=6.0,
        help="Hours before cached Opire evidence must be refreshed.",
    )
    parser.add_argument(
        "--no-taskbounty",
        action="store_true",
        help="Skip the live TaskBounty public API.",
    )
    parser.add_argument(
        "--taskbounty-cache-dir",
        default="data/taskbounty-cache",
        help="Persistent cache for TaskBounty summary and detail evidence.",
    )
    parser.add_argument(
        "--taskbounty-cache-ttl-hours",
        type=float,
        default=6.0,
        help="Hours before cached TaskBounty evidence must be refreshed.",
    )
    parser.add_argument(
        "--algora-org",
        action="append",
        default=[],
        help="Algora organization handle to scan; may be repeated.",
    )
    parser.add_argument(
        "--algora-cache-dir",
        default="data/algora-cache",
        help="Persistent cache for public Algora board HTML.",
    )
    parser.add_argument(
        "--algora-cache-ttl-hours",
        type=float,
        default=24.0,
        help="Hours before cached Algora board evidence must be refreshed.",
    )
    parser.add_argument(
        "--algora-request-interval-seconds",
        type=float,
        default=2.0,
        help="Minimum delay between uncached Algora board requests.",
    )
    parser.add_argument(
        "--algora-maximum-requests-per-scan",
        type=int,
        default=3,
        help="Maximum live Algora board refresh attempts per configured cache pool.",
    )
    parser.add_argument(
        "--algora-discovery-report",
        default="reports/algora-board-audit.json",
        help="Fresh weekly Algora discovery report whose verified boards are scanned.",
    )
    parser.add_argument(
        "--algora-discovery-report-max-age-hours",
        type=float,
        default=8 * 24,
        help="Maximum age of a discovery report used to add scan-only boards.",
    )
    parser.add_argument(
        "--algora-discovery-cache-dir",
        default="data/algora-discovery-cache",
        help="Persistent public board cache for scan-only discovered organizations.",
    )
    parser.add_argument(
        "--algora-discovery-cache-ttl-hours",
        type=float,
        default=24.0,
        help="Hours before a discovered Algora board cache must be refreshed.",
    )
    parser.add_argument(
        "--no-github-enrichment",
        action="store_true",
        help="Do not query GitHub for repository and issue evidence.",
    )
    parser.add_argument(
        "--github-token-env",
        default="GITHUB_TOKEN",
        help="Environment variable containing an optional GitHub token.",
    )
    parser.add_argument(
        "--github-cache",
        default="data/github-cache.json",
        help="Persistent cache for successful public GitHub API responses.",
    )
    parser.add_argument(
        "--github-cache-ttl-hours",
        type=float,
        default=24.0,
        help="Hours before cached GitHub evidence must be refreshed.",
    )
    parser.add_argument(
        "--github-maximum-requests-per-scan",
        type=int,
        default=45,
        help="Maximum live GitHub API requests in one opportunity scan.",
    )
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument("--history-db", default="data/bounty_hunter.db")
    return parser


def main() -> int:
    args = _parser().parse_args()
    os.makedirs(args.output_dir, exist_ok=True)
    try:
        opportunities = _collect(args)
    except Exception as error:
        print(f"collection failed: {error}", file=sys.stderr)
        return 1

    history = History(args.history_db)
    try:
        calibration = history.outcome_calibration()
        scoring_config = replace(
            ScoringConfig(),
            default_execution_cost_usd=calibration[
                "default_execution_cost_usd"
            ],
            default_human_hours=calibration["default_human_hours"],
        )
        records = ranked_records(
            opportunities,
            config=scoring_config,
            calibration_factor=calibration["probability_factor"],
        )
        json_path = os.path.join(args.output_dir, "opportunities.json")
        markdown_path = os.path.join(args.output_dir, "opportunities.md")
        write_json(
            records,
            json_path,
            source_health=args.source_health,
            calibration=calibration,
        )
        write_markdown(
            records,
            markdown_path,
            source_health=args.source_health,
            calibration=calibration,
        )
        scan_id = history.record_scan(records)
    finally:
        history.close()

    eligible = sum(1 for record in records if record["score"]["eligible"])
    print(
        f"evaluated {len(records)} opportunities; {eligible} eligible; "
        f"recorded scan {scan_id}; wrote {markdown_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

