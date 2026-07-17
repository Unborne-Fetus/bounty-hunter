import argparse
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

from .github import GitHubClient
from .sources.algora import AlgoraBoardClient, normalize_html


SEARCH_QUERY = 'is:issue is:open label:"💎 Bounty" archived:false'
TARGET_LABEL = "💎 bounty"


def configured_orgs(path: str) -> List[str]:
    values = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            values.append(value)
    return values


def candidate_org_counts(items: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """Count owners only when the exact expected bounty label is present.

    The label is a discovery hint, never payment evidence. A candidate must
    still expose a parseable row on its public Algora organization board.
    """
    counts: Counter = Counter()
    for item in items:
        labels = {
            str(label.get("name") or "").strip().lower()
            for label in item.get("labels") or []
            if isinstance(label, dict)
        }
        if TARGET_LABEL not in labels:
            continue
        repository_url = str(item.get("repository_url") or "")
        parts = repository_url.rstrip("/").split("/")
        if len(parts) < 2:
            continue
        owner = parts[-2]
        if owner:
            counts[owner] += 1
    return dict(counts)


def search_candidate_items(
    github: GitHubClient, maximum_pages: int = 3
) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    total_count = None
    pages_fetched = 0
    # GitHub exposes at most 1,000 search results, or ten 100-item pages.
    for page in range(1, min(10, max(1, maximum_pages)) + 1):
        search_path = (
            "/search/issues?q="
            + quote(SEARCH_QUERY)
            + "&sort=updated&order=desc&per_page=100"
            + (f"&page={page}" if page > 1 else "")
        )
        payload = github.get(search_path)
        page_items = payload.get("items") or []
        if total_count is None:
            total_count = payload.get("total_count")
        items.extend(page_items)
        pages_fetched += 1
        if len(page_items) < 100:
            break
    return {
        "total_count": total_count,
        "items": items,
        "pages_fetched": pages_fetched,
    }


def audit_candidates(
    counts: Dict[str, int],
    board_client: AlgoraBoardClient,
    configured: Iterable[str] = (),
    maximum_candidates: int = 20,
    timeout_seconds: int = 20,
) -> List[Dict[str, Any]]:
    configured_lower = {item.lower() for item in configured}
    candidates = sorted(counts, key=lambda org: (-counts[org], org.lower()))
    results = []
    for org in candidates:
        if org.lower() in configured_lower:
            continue
        if len(results) >= maximum_candidates:
            break
        record: Dict[str, Any] = {
            "organization": org,
            "github_open_labeled_issues": counts[org],
            "board_url": f"https://algora.io/{org}/bounties?status=open",
            "promotion": "not_verified",
        }
        try:
            html, stale = board_client.fetch(org, timeout_seconds)
            opportunities = normalize_html(org, html, evidence_stale=stale)
            record.update(
                {
                    "status": "stale" if stale else "fresh",
                    "open_board_rows": len(opportunities),
                    "promotion": (
                        "review_for_trusted_list"
                        if opportunities and not stale
                        else "do_not_promote"
                    ),
                    "sample_issues": [
                        {
                            "issue_url": item.issue_url,
                            "bounty_usd": item.bounty_usd,
                        }
                        for item in opportunities[:3]
                    ],
                }
            )
        except Exception as error:
            record.update(
                {
                    "status": "error",
                    "error": f"{type(error).__name__}: {error}"[:300],
                    "open_board_rows": None,
                    "promotion": "do_not_promote",
                }
            )
        results.append(record)
    return results


def write_report(path: str, payload: Dict[str, Any]) -> None:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Find candidate Algora organizations from GitHub labels, then "
            "require direct public board evidence before suggesting review."
        )
    )
    parser.add_argument("--config", default="config/algora_orgs.txt")
    parser.add_argument("--output", default="reports/algora-board-audit.json")
    parser.add_argument(
        "--github-token-env", default="GITHUB_TOKEN"
    )
    parser.add_argument(
        "--github-cache", default="data/github-discovery-cache.json"
    )
    parser.add_argument(
        "--algora-cache-dir", default="data/algora-discovery-cache"
    )
    parser.add_argument("--maximum-candidates", type=int, default=20)
    parser.add_argument("--search-pages", type=int, default=6)
    parser.add_argument("--request-interval-seconds", type=float, default=2.0)
    parser.add_argument("--timeout", type=int, default=20)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    github = GitHubClient(
        token=os.environ.get(args.github_token_env),
        timeout_seconds=args.timeout,
        cache_path=args.github_cache,
        cache_ttl_seconds=24 * 60 * 60,
    )
    board_client = AlgoraBoardClient(
        cache_dir=args.algora_cache_dir,
        cache_ttl_seconds=24 * 60 * 60,
        minimum_request_interval_seconds=args.request_interval_seconds,
    )
    try:
        search = search_candidate_items(github, args.search_pages)
        counts = candidate_org_counts(search.get("items") or [])
        results = audit_candidates(
            counts,
            board_client,
            configured=configured_orgs(args.config),
            maximum_candidates=max(1, args.maximum_candidates),
            timeout_seconds=args.timeout,
        )
    except Exception as error:
        print(f"Algora discovery failed: {error}")
        return 1

    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "search_query": SEARCH_QUERY,
        "search_total_count": search.get("total_count"),
        "search_items_examined": len(search.get("items") or []),
        "search_pages_fetched": search["pages_fetched"],
        "configured_organizations": configured_orgs(args.config),
        "candidates": results,
        "review_candidates": [
            item["organization"]
            for item in results
            if item["promotion"] == "review_for_trusted_list"
        ],
        "policy": (
            "GitHub labels are hints only. Promotion requires fresh, parseable "
            "public Algora board rows and deliberate trusted-list review."
        ),
    }
    write_report(args.output, payload)
    print(
        f"audited {len(results)} candidate organizations; "
        f"{len(payload['review_candidates'])} require review; wrote {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
