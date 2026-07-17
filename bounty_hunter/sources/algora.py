import re
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ..models import Opportunity


ORG_URL = "https://algora.io/{org}/bounties?status=open"
ISSUE_RE = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/(?:issues|pull)/(\d+)"
)
AMOUNT_RE = re.compile(r"\$([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
CLAIM_RE = re.compile(r"\b(\d+)\s+claims?\b", re.IGNORECASE)


class _BountyRowParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: List[Dict[str, object]] = []
        self.current: Optional[Dict[str, object]] = None
        self.in_issue_link = False

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        if tag == "tr":
            self.current = {
                "text": [],
                "issue_text": [],
                "source_id": attributes.get("phx-value-id"),
            }
        if not self.current:
            return
        if not self.current.get("source_id") and attributes.get("phx-value-id"):
            self.current["source_id"] = attributes["phx-value-id"]
        if tag == "a":
            href = attributes.get("href", "")
            if ISSUE_RE.match(href):
                self.current["issue_url"] = href
                self.in_issue_link = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self.in_issue_link = False
        if tag == "tr" and self.current:
            if self.current.get("issue_url"):
                self.rows.append(self.current)
            self.current = None

    def handle_data(self, data: str) -> None:
        if not self.current:
            return
        text = " ".join(data.split())
        if not text:
            return
        self.current["text"].append(text)
        if self.in_issue_link:
            self.current["issue_text"].append(text)


def fetch_org(org: str, timeout_seconds: int = 20) -> str:
    request = Request(
        ORG_URL.format(org=org),
        headers={
            "Accept": "text/html",
            "User-Agent": "bounty-hunter/0.1",
        },
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        return response.read().decode("utf-8", errors="replace")


class AlgoraBoardClient:
    def __init__(
        self,
        cache_dir: str = "data/algora-cache",
        cache_ttl_seconds: int = 6 * 60 * 60,
        minimum_request_interval_seconds: float = 2.0,
        maximum_requests_per_run: Optional[int] = None,
        transport: Optional[Callable[[str, int], str]] = None,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cache_dir = Path(cache_dir).resolve()
        self.cache_ttl_seconds = cache_ttl_seconds
        self.minimum_request_interval_seconds = max(
            0.0, minimum_request_interval_seconds
        )
        self.maximum_requests_per_run = (
            max(0, maximum_requests_per_run)
            if maximum_requests_per_run is not None
            else None
        )
        self.requests_attempted = 0
        self.last_fallback_reason: Optional[str] = None
        self.transport = transport or fetch_org
        self.sleeper = sleeper
        self.monotonic = monotonic
        self.last_request_at: Optional[float] = None
        self.rate_limited_until: Optional[float] = None

    def _path(self, org: str) -> Path:
        if not org or not re.fullmatch(r"[A-Za-z0-9_.-]+", org):
            raise ValueError("Algora organization handle is unsafe")
        return self.cache_dir / f"{org}.html"

    def fetch(self, org: str, timeout_seconds: int = 20) -> Tuple[str, bool]:
        self.last_fallback_reason = None
        path = self._path(org)
        now = time.time()
        if path.exists() and now - path.stat().st_mtime <= self.cache_ttl_seconds:
            return path.read_text(encoding="utf-8"), False

        monotonic_now = self.monotonic()
        if (
            self.rate_limited_until is not None
            and monotonic_now < self.rate_limited_until
        ):
            if path.exists():
                self.last_fallback_reason = "rate_limit_cooldown"
                return path.read_text(encoding="utf-8"), True
            raise RuntimeError("Algora request cooldown is active after HTTP 429")

        if (
            self.maximum_requests_per_run is not None
            and self.requests_attempted >= self.maximum_requests_per_run
        ):
            if path.exists():
                self.last_fallback_reason = "request_budget_exhausted"
                return path.read_text(encoding="utf-8"), True
            raise RuntimeError("Algora request budget is exhausted for this scan")

        if self.last_request_at is not None:
            remaining = (
                self.minimum_request_interval_seconds
                - (self.monotonic() - self.last_request_at)
            )
            if remaining > 0:
                self.sleeper(remaining)
        try:
            self.requests_attempted += 1
            html = self.transport(org, timeout_seconds)
            self.last_request_at = self.monotonic()
        except Exception as error:
            self.last_request_at = self.monotonic()
            if isinstance(error, HTTPError) and error.code == 429:
                retry_after = 60.0
                try:
                    retry_after = float(error.headers.get("Retry-After", retry_after))
                except (TypeError, ValueError):
                    pass
                self.rate_limited_until = self.last_request_at + max(
                    1.0, retry_after
                )
            if path.exists():
                self.last_fallback_reason = (
                    "http_429"
                    if isinstance(error, HTTPError) and error.code == 429
                    else type(error).__name__
                )
                return path.read_text(encoding="utf-8"), True
            raise

        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(html, encoding="utf-8")
        temporary.replace(path)
        return html, False


def normalize_html(
    org: str, html: str, evidence_stale: bool = False
) -> List[Opportunity]:
    parser = _BountyRowParser()
    parser.feed(html)
    opportunities: List[Opportunity] = []
    for index, row in enumerate(parser.rows):
        text = " ".join(row["text"])
        amount_match = AMOUNT_RE.search(text)
        issue_match = ISSUE_RE.match(str(row["issue_url"]))
        if not amount_match or not issue_match:
            continue
        owner, repo, number = issue_match.groups()
        claim_match = CLAIM_RE.search(text)
        issue_text = " ".join(row["issue_text"])
        prefix = f"{repo}#{number}"
        title = issue_text
        if title.startswith(prefix):
            title = title[len(prefix) :].strip()
        opportunities.append(
            Opportunity(
                source="algora",
                source_id=str(
                    row.get("source_id")
                    or f"{org}:{owner}/{repo}#{number}:{index}"
                ),
                title=title or f"{owner}/{repo}#{number}",
                source_url=ORG_URL.format(org=org),
                bounty_usd=float(amount_match.group(1).replace(",", "")),
                status="open",
                repository=f"{owner}/{repo}",
                issue_url=str(row["issue_url"]),
                active_attempts=(
                    int(claim_match.group(1)) if claim_match else None
                ),
                platform_fee_rate=0.0,
                notes=[
                    f"Discovered on Algora organization board '{org}'.",
                    "Algora board rows do not expose a submission deadline.",
                ],
                evidence={
                    "algora_org": org,
                    "algora_evidence_stale": evidence_stale,
                },
            )
        )
    pooled: Dict[str, Opportunity] = {}
    order: List[str] = []
    for opportunity in opportunities:
        key = opportunity.issue_url or opportunity.source_id
        contribution = {
            "source_id": opportunity.source_id,
            "bounty_usd": opportunity.bounty_usd,
        }
        if key not in pooled:
            pooled[key] = opportunity
            pooled[key].evidence["algora_contributions"] = [contribution]
            order.append(key)
            continue
        existing = pooled[key]
        existing.bounty_usd += opportunity.bounty_usd
        existing.evidence["algora_contributions"].append(contribution)
        claims = [
            value
            for value in (existing.active_attempts, opportunity.active_attempts)
            if value is not None
        ]
        existing.active_attempts = max(claims) if claims else None

    for key in order:
        opportunity = pooled[key]
        contributions = opportunity.evidence["algora_contributions"]
        opportunity.evidence["algora_pooled_count"] = len(contributions)
        if len(contributions) > 1:
            parsed = ISSUE_RE.match(opportunity.issue_url or "")
            issue_key = (
                f"{parsed.group(1)}/{parsed.group(2)}#{parsed.group(3)}"
                if parsed
                else opportunity.source_id
            )
            opportunity.source_id = f"pool:{org}:{issue_key}"
            opportunity.notes.append(
                f"Pooled {len(contributions)} Algora rewards for the same issue."
            )
    return [pooled[key] for key in order]
