import json
import re
import shutil
import ssl
import subprocess
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from urllib.error import URLError
from urllib.request import Request, urlopen

from ..models import Opportunity


HOME_URL = "https://app.opire.dev/home"
PAYMENT_MODEL_DOC_URL = "https://docs.opire.dev/rewards/introduction"
ISSUE_RE = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/(?:issues|pull)/(\d+)(?:[/?#].*)?$"
)
FLIGHT_RE = re.compile(r"self\.__next_f\.push\(\[1,(\"(?:\\.|[^\"\\])*\")\]\)")
OUTLIER_USD = 10_000.0


class _ScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_script = False
        self.current: List[str] = []
        self.scripts: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "script":
            self.in_script = True
            self.current = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.in_script:
            self.scripts.append("".join(self.current))
            self.in_script = False

    def handle_data(self, data: str) -> None:
        if self.in_script:
            self.current.append(data)


def fetch_home(timeout_seconds: int = 20) -> str:
    request = Request(
        HOME_URL,
        headers={"Accept": "text/html", "User-Agent": "bounty-hunter/0.1"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return response.read().decode("utf-8", errors="replace")
    except URLError as error:
        if not isinstance(error.reason, ssl.SSLError):
            raise
        curl = shutil.which("curl")
        if not curl:
            raise
        result = subprocess.run(
            [
                curl,
                "-fsSL",
                "--max-time",
                str(timeout_seconds),
                "--user-agent",
                "bounty-hunter/0.1",
                HOME_URL,
            ],
            check=True,
            capture_output=True,
            timeout=timeout_seconds + 5,
        )
        return result.stdout.decode("utf-8", errors="replace")


class OpireClient:
    def __init__(
        self,
        cache_path: str = "data/opire-cache.html",
        cache_ttl_seconds: int = 6 * 60 * 60,
        transport: Optional[Callable[[int], str]] = None,
    ) -> None:
        self.cache_path = Path(cache_path).resolve()
        self.cache_ttl_seconds = cache_ttl_seconds
        self.transport = transport or fetch_home

    def fetch(self, timeout_seconds: int = 20) -> Tuple[str, bool]:
        now = time.time()
        if (
            self.cache_path.exists()
            and now - self.cache_path.stat().st_mtime <= self.cache_ttl_seconds
        ):
            return self.cache_path.read_text(encoding="utf-8"), False
        try:
            html = self.transport(timeout_seconds)
        except Exception:
            if self.cache_path.exists():
                return self.cache_path.read_text(encoding="utf-8"), True
            raise
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        temporary.write_text(html, encoding="utf-8")
        temporary.replace(self.cache_path)
        return html, False


def _flight_payloads(html: str) -> List[str]:
    parser = _ScriptParser()
    parser.feed(html)
    payloads: List[str] = []
    for script in parser.scripts:
        for match in FLIGHT_RE.finditer(script):
            payloads.append(json.loads(match.group(1)))
    return payloads


def _extract_json_value(html: str, key: str):
    decoder = json.JSONDecoder()
    marker = f'"{key}":'
    for flight in _flight_payloads(html):
        start = flight.find(marker)
        if start >= 0:
            value, _ = decoder.raw_decode(flight[start + len(marker) :])
            return value
    return None


def _initial_rewards(html: str) -> List[dict]:
    value = _extract_json_value(html, "initialRewards")
    if isinstance(value, list):
        return value
    raise ValueError("Opire initialRewards payload was not found")


def inventory_metadata(html: str) -> dict:
    kpis = _extract_json_value(html, "homeKPIs")
    available = kpis.get("bountiesAvailable") if isinstance(kpis, dict) else None
    return {
        "available_reported": available if isinstance(available, int) else None,
        "initial_page_count": len(_initial_rewards(html)),
    }


def normalize_html(html: str, evidence_stale: bool = False) -> List[Opportunity]:
    opportunities: List[Opportunity] = []
    for reward in _initial_rewards(html):
        issue_url = str(reward.get("url") or "")
        issue = ISSUE_RE.match(issue_url)
        price = reward.get("pendingPrice") or {}
        if not issue or price.get("unit") != "USD_CENT":
            continue
        owner, repo, _ = issue.groups()
        try:
            bounty_usd = int(price["value"]) / 100
        except (KeyError, TypeError, ValueError):
            continue
        languages = [
            str(item) for item in reward.get("programmingLanguages") or [] if item
        ]
        claimers = reward.get("claimerUsers") or []
        trying = reward.get("tryingUsers") or []
        project = reward.get("project") or {}
        created_at = reward.get("createdAt")
        published_at = None
        if isinstance(created_at, (int, float)):
            published_at = datetime.fromtimestamp(
                created_at / 1000, tz=timezone.utc
            ).isoformat()
        outlier = bounty_usd > OUTLIER_USD
        opportunities.append(
            Opportunity(
                source="opire",
                source_id=str(reward.get("id") or issue_url),
                title=str(reward.get("title") or f"{owner}/{repo}"),
                source_url=HOME_URL,
                bounty_usd=bounty_usd,
                status="open",
                published_at=published_at,
                repository=f"{owner}/{repo}",
                issue_url=issue_url,
                language=languages[0] if languages else None,
                tags=languages,
                active_attempts=max(len(claimers), len(trying)),
                public_repository=project.get("isPublic"),
                platform_fee_rate=0.0,
                payout_requires_sponsor_action=True,
                notes=[
                    "Discovered in Opire's public server-rendered rewards inventory.",
                    "Opire pending rewards require creator payment after resolution; "
                    "that counterparty risk reduces estimated payout probability.",
                ],
                evidence={
                    "opire_evidence_stale": evidence_stale,
                    "payment_model": "creator_checkout_after_reward_claim",
                    "payment_assurance": "not_escrowed_or_guaranteed_by_public_evidence",
                    "payment_model_source": PAYMENT_MODEL_DOC_URL,
                    "opire_project_id": project.get("id"),
                    "opire_bot_installed": project.get("isBotInstalled"),
                    "opire_claimer_count": len(claimers),
                    "opire_trying_count": len(trying),
                    "reward_outlier": outlier,
                },
                raw=reward,
            )
        )
    return opportunities
