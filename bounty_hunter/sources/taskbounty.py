import json
import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.request import Request, urlopen

from ..models import Opportunity


API_URL = "https://www.task-bounty.com/api/v1/tasks"
TASK_URL = "https://www.task-bounty.com/task/{slug}"
GITHUB_ISSUE_RE = re.compile(
    r"https://github\.com/([^/\s\"'<]+)/([^/\s\"'<]+)/issues/(\d+)"
)


def fetch(timeout_seconds: int = 20) -> Dict[str, Any]:
    request = Request(
        API_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": "bounty-hunter/0.1",
        },
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        return json.load(response)


class TaskBountyClient:
    def __init__(
        self,
        cache_dir: str = "data/taskbounty-cache",
        cache_ttl_seconds: int = 6 * 60 * 60,
        summary_transport: Optional[Callable[[int], Dict[str, Any]]] = None,
        detail_transport: Optional[Callable[[Opportunity, int], str]] = None,
    ) -> None:
        self.cache_dir = Path(cache_dir).resolve()
        self.cache_ttl_seconds = cache_ttl_seconds
        self.summary_transport = summary_transport or fetch
        self.detail_transport = detail_transport or fetch_detail

    def _fresh(self, path: Path) -> bool:
        return (
            path.exists()
            and time.time() - path.stat().st_mtime <= self.cache_ttl_seconds
        )

    def _write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)

    def fetch_summary(self, timeout_seconds: int = 20) -> Tuple[Dict[str, Any], bool]:
        path = self.cache_dir / "tasks.json"
        if self._fresh(path):
            return json.loads(path.read_text(encoding="utf-8")), False
        try:
            payload = self.summary_transport(timeout_seconds)
        except Exception:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8")), True
            raise
        self._write(path, json.dumps(payload, separators=(",", ":")))
        return payload, False

    def fetch_detail(
        self, opportunity: Opportunity, timeout_seconds: int = 20
    ) -> Tuple[str, bool]:
        key = hashlib.sha256(opportunity.source_url.encode("utf-8")).hexdigest()
        path = self.cache_dir / "details" / f"{key}.html"
        if self._fresh(path):
            return path.read_text(encoding="utf-8"), False
        try:
            html = self.detail_transport(opportunity, timeout_seconds)
        except Exception:
            if path.exists():
                return path.read_text(encoding="utf-8"), True
            raise
        self._write(path, html)
        return html, False


def normalize(
    payload: Dict[str, Any], evidence_stale: bool = False
) -> List[Opportunity]:
    records: Iterable[Dict[str, Any]] = payload.get("data", payload.get("tasks", []))
    opportunities: List[Opportunity] = []
    for item in records:
        bounty_cents = item.get("bounty_cents") or 0
        slug = item.get("slug", "")
        opportunities.append(
            Opportunity(
                source="taskbounty",
                source_id=str(item.get("id", slug)),
                title=item.get("title", "Untitled bounty"),
                source_url=TASK_URL.format(slug=slug),
                bounty_usd=float(bounty_cents) / 100,
                status=str(item.get("status", "unknown")).lower(),
                published_at=item.get("published_at"),
                deadline=item.get("submission_deadline"),
                category=item.get("category"),
                tags=list(item.get("tags") or []),
                submission_count=item.get("submission_count"),
                platform_fee_rate=0.20,
                notes=[
                    "Public summary API does not expose repository visibility, "
                    "language, issue URL, or active-attempt count."
                ],
                evidence={"taskbounty_evidence_stale": evidence_stale},
                raw=item,
            )
        )
    return opportunities


def load(path: str) -> List[Opportunity]:
    with open(path, "r", encoding="utf-8") as handle:
        return normalize(json.load(handle))


def enrich_from_detail_html(opportunity: Opportunity, html: str) -> Opportunity:
    """Resolve fields omitted by TaskBounty's summary endpoint."""
    match = GITHUB_ISSUE_RE.search(html)
    if match:
        owner, repo, number = match.groups()
        opportunity.repository = f"{owner}/{repo}"
        opportunity.issue_url = (
            f"https://github.com/{owner}/{repo}/issues/{number}"
        )
    lowered = html.lower()
    if "repo is private" in lowered or "repo is private." in lowered:
        opportunity.public_repository = False
    elif opportunity.issue_url:
        opportunity.public_repository = True
    opportunity.evidence["taskbounty_detail_checked"] = True
    return opportunity


def fetch_detail(opportunity: Opportunity, timeout_seconds: int = 20) -> str:
    request = Request(
        opportunity.source_url,
        headers={
            "Accept": "text/html",
            "User-Agent": "bounty-hunter/0.1",
        },
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        return response.read().decode("utf-8", errors="replace")
