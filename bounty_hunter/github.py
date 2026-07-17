import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Tuple
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .models import Opportunity, parse_datetime


ISSUE_URL_RE = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/(?:issues|pull)/(\d+)"
)
TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec)(/|$)|"
    r"(^|/)(pytest\.ini|tox\.ini|jest\.config\.|vitest\.config\.)",
    re.IGNORECASE,
)
CONTRIBUTING_RE = re.compile(
    r"(^|/)(contributing(\.[^/]+)?|code_of_conduct(\.[^/]+)?|"
    r"pull_request_template(\.[^/]+)?)$",
    re.IGNORECASE,
)
SECURITY_TERMS = {"cve", "exploit", "security", "vulnerability"}
CLARITY_TERMS = (
    "acceptance criteria",
    "expected behavior",
    "expected behaviour",
    "steps to reproduce",
    "requirements",
    "must:",
    "- [ ]",
)
BROAD_SCOPE_TERMS = (
    "complete working sample",
    "from scratch",
    "full integration",
    "migration",
    "new provider",
    "browser extension",
    "chrome extension",
    "team management",
    "organisation management",
    "organization management",
    "rebuild",
)
COMPONENT_TERMS = (
    " api",
    "backend",
    " cli",
    "database",
    "documentation",
    "frontend",
    "integration",
    " sdk",
    " ui",
)
CLAIM_RE = re.compile(
    r"(?:^|\s)/claim\b|\b(?:i(?:'m| am)|we are) (?:working|starting) on\b",
    re.IGNORECASE,
)
BOUNTY_WITHDRAWAL_RE = re.compile(
    r"\b(?:bounty|reward)\b.{0,80}\b(?:no longer active|withdrawn|cancelled|canceled)\b|"
    r"\b(?:no longer (?:offering|honou?ring)|will not honou?r|won't honou?r)\b"
    r".{0,80}\b(?:bounty|reward)\b",
    re.IGNORECASE | re.DOTALL,
)
AI_CONTRIBUTION_RISK_RE = re.compile(
    r"\b(?:ai slop|vibecod(?:e|ed|ing)|vibe[- ]cod(?:e|ed|ing))\b|"
    r"\b(?:no|do not|don't|won't|will not)\b.{0,80}"
    r"\b(?:ai|llm|claude|chatgpt|generated)\b.{0,80}"
    r"\b(?:code|contribution|pull request|pr)s?\b|"
    r"\b(?:ai|llm|claude|chatgpt)[ -]generated\b.{0,80}"
    r"\b(?:not accepted|not allowed|prohibited|banned|will be closed|will not be reviewed)\b",
    re.IGNORECASE | re.DOTALL,
)


class GitHubClient:
    def __init__(
        self,
        token: Optional[str] = None,
        timeout_seconds: int = 20,
        transport: Optional[Callable[[str, Dict[str, str]], Any]] = None,
        cache_path: Optional[str] = None,
        cache_ttl_seconds: int = 6 * 60 * 60,
        maximum_requests_per_run: Optional[int] = None,
    ) -> None:
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.requests_made = 0
        self._cache: Dict[str, Any] = {}
        self.cache_path = Path(cache_path).resolve() if cache_path else None
        self.cache_ttl_seconds = cache_ttl_seconds
        self.maximum_requests_per_run = (
            max(0, maximum_requests_per_run)
            if maximum_requests_per_run is not None
            else None
        )
        self._persistent_cache = self._load_persistent_cache()
        metadata = self._persistent_cache.get("__github_client_meta__", {})
        self.rate_limited_until = (
            parse_datetime(metadata.get("rate_limited_until"))
            if isinstance(metadata, dict)
            else None
        )

    def _load_persistent_cache(self) -> Dict[str, Any]:
        if not self.cache_path or not self.cache_path.exists():
            return {}
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _persist_cache(self) -> None:
        if not self.cache_path:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self._persistent_cache, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, self.cache_path)

    def get(self, path: str, accept: str = "application/vnd.github+json") -> Any:
        url = path if path.startswith("https://") else f"https://api.github.com{path}"
        key = f"{accept}\n{url}"
        if key in self._cache:
            return self._cache[key]
        persisted = self._persistent_cache.get(key)
        if isinstance(persisted, dict):
            cached_at = parse_datetime(persisted.get("cached_at"))
            if cached_at and cached_at >= datetime.now(timezone.utc) - timedelta(
                seconds=self.cache_ttl_seconds
            ):
                value = persisted.get("value")
                if (
                    isinstance(value, dict)
                    and value.get("__github_http_error__") in {404, 410}
                ):
                    raise HTTPError(
                        url,
                        int(value["__github_http_error__"]),
                        "cached terminal GitHub response",
                        None,
                        None,
                    )
                self._cache[key] = value
                return value
        now = datetime.now(timezone.utc)
        if self.rate_limited_until and now < self.rate_limited_until:
            raise HTTPError(
                url,
                403,
                "GitHub request cooldown is active",
                {"X-RateLimit-Remaining": "0"},
                None,
            )
        if self.rate_limited_until:
            self.rate_limited_until = None
            self._persistent_cache.pop("__github_client_meta__", None)
            self._persist_cache()
        if (
            self.maximum_requests_per_run is not None
            and self.requests_made >= self.maximum_requests_per_run
        ):
            raise HTTPError(
                url,
                503,
                "local GitHub request budget exhausted",
                {"X-RateLimit-Remaining": "unknown"},
                None,
            )
        headers = {
            "Accept": accept,
            "User-Agent": "bounty-hunter/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self.requests_made += 1
        try:
            if self.transport:
                result = self.transport(url, headers)
            else:
                request = Request(url, headers=headers)
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    result = json.load(response)
        except HTTPError as error:
            if self.cache_path and error.code in {404, 410}:
                self._persistent_cache[key] = {
                    "cached_at": datetime.now(timezone.utc).isoformat(),
                    "value": {"__github_http_error__": error.code},
                }
                self._persist_cache()
            headers = error.headers or {}
            if error.code == 429 or str(
                headers.get("X-RateLimit-Remaining", "")
            ) == "0":
                reset_at = None
                try:
                    reset_at = datetime.fromtimestamp(
                        int(headers.get("X-RateLimit-Reset")), tz=timezone.utc
                    )
                except (TypeError, ValueError, OverflowError):
                    try:
                        retry_after = max(
                            60, int(headers.get("Retry-After", "60"))
                        )
                    except (TypeError, ValueError):
                        retry_after = 60
                    reset_at = datetime.now(timezone.utc) + timedelta(
                        seconds=retry_after
                    )
                self.rate_limited_until = reset_at
                if self.cache_path:
                    self._persistent_cache["__github_client_meta__"] = {
                        "rate_limited_until": reset_at.isoformat()
                    }
                    self._persist_cache()
            raise
        self._cache[key] = result
        if self.cache_path:
            self._persistent_cache[key] = {
                "cached_at": datetime.now(timezone.utc).isoformat(),
                "value": result,
            }
            self._persist_cache()
        return result


def parse_issue_url(url: Optional[str]) -> Optional[Tuple[str, str, int]]:
    if not url:
        return None
    match = ISSUE_URL_RE.match(url)
    if not match:
        return None
    owner, repo, number = match.groups()
    return owner, repo, int(number)


def _recent(value: Optional[str], days: int = 90) -> Optional[bool]:
    parsed = parse_datetime(value)
    if not parsed:
        return None
    return parsed >= datetime.now(timezone.utc) - timedelta(days=days)


def _tree_signals(tree: Dict[str, Any]) -> Tuple[bool, bool]:
    paths = [item.get("path", "") for item in tree.get("tree", [])]
    return (
        any(TEST_PATH_RE.search(path) for path in paths),
        any(CONTRIBUTING_RE.search(path) for path in paths),
    )


def _competition(timeline: Iterable[Dict[str, Any]], issue_number: int) -> int:
    pull_numbers = set()
    for event in timeline:
        if event.get("event") != "cross-referenced":
            continue
        source_issue = event.get("source", {}).get("issue", {})
        if (
            "pull_request" in source_issue
            and source_issue.get("state") == "open"
            and source_issue.get("number") != issue_number
        ):
            pull_numbers.add(source_issue.get("number"))
    return len(pull_numbers)


def _active_claims(comments: Iterable[Dict[str, Any]]) -> int:
    claimants = {
        comment.get("user", {}).get("login")
        for comment in comments
        if CLAIM_RE.search(comment.get("body") or "")
    }
    claimants.discard(None)
    return len(claimants)


def reputation_policy_signals(
    issue: Dict[str, Any], comments: Iterable[Dict[str, Any]]
) -> Dict[str, Any]:
    """Extract explicit withdrawal and AI-contribution reputation evidence."""
    records = [
        {
            "body": issue.get("body") or "",
            "url": issue.get("html_url"),
            "author": issue.get("user", {}).get("login"),
            "association": issue.get("author_association"),
            "issue_body": True,
        }
    ]
    records.extend(
        {
            "body": comment.get("body") or "",
            "url": comment.get("html_url"),
            "author": comment.get("user", {}).get("login"),
            "association": comment.get("author_association"),
            "issue_body": False,
        }
        for comment in comments
        if isinstance(comment, dict)
    )
    withdrawal = []
    ai_risk = []
    trusted = {"OWNER", "MEMBER", "COLLABORATOR"}
    for record in records:
        body = record["body"]
        provenance = {
            key: value
            for key, value in record.items()
            if key != "body" and value is not None
        }
        if BOUNTY_WITHDRAWAL_RE.search(body):
            withdrawal.append(provenance)
        if (
            AI_CONTRIBUTION_RISK_RE.search(body)
            and (
                record["issue_body"]
                or str(record.get("association") or "").upper() in trusted
            )
        ):
            ai_risk.append(provenance)
    return {
        "bounty_withdrawal_signal": bool(withdrawal),
        "bounty_withdrawal_provenance": withdrawal,
        "ai_contribution_reputation_risk": bool(ai_risk),
        "ai_contribution_risk_provenance": ai_risk,
    }


def _estimate_agent_hours(
    title: str, body: str, labels: Iterable[str]
) -> Tuple[float, list]:
    """Return a conservative deterministic scope estimate and its evidence.

    This is a screening estimate, not a promise that an agent can finish the
    issue in this time. It deliberately raises the lower bound for broad
    features so they cannot inherit the optimistic two-hour default.
    """
    text = f" {title}\n{body}".lower()
    signals = []
    hours = 1.5

    broad = [term for term in BROAD_SCOPE_TERMS if term in text]
    if broad:
        hours += 2.0
        signals.append("broad scope: " + ", ".join(broad))

    label_set = {label.lower() for label in labels}
    feature_labels = sorted(
        label for label in label_set if "feature" in label or "enhancement" in label
    )
    if feature_labels:
        hours += 0.75
        signals.append("feature label: " + ", ".join(feature_labels))

    if len(body) > 3000:
        hours += 1.5
        signals.append("issue body exceeds 3000 characters")
    elif len(body) > 1200:
        hours += 0.75
        signals.append("issue body exceeds 1200 characters")

    checklist_items = len(re.findall(r"(?m)^\s*[-*]\s*\[[ xX]\]", body))
    if checklist_items > 2:
        increment = min(2.0, (checklist_items - 2) * 0.25)
        hours += increment
        signals.append(f"{checklist_items} checklist items")

    components = sorted({term.strip() for term in COMPONENT_TERMS if term in text})
    if len(components) >= 3:
        hours += 1.0
        signals.append("multiple components: " + ", ".join(components))

    return round(min(8.0, hours), 2), signals


def enrich_opportunity(
    opportunity: Opportunity, client: GitHubClient
) -> Opportunity:
    parsed = parse_issue_url(opportunity.issue_url)
    if not parsed:
        opportunity.notes.append("GitHub enrichment skipped: issue URL is unknown.")
        return opportunity
    owner, repo, number = parsed
    base = f"/repos/{quote(owner)}/{quote(repo)}"

    try:
        repository = client.get(base)
    except HTTPError as error:
        if error.code in {404, 410}:
            opportunity.public_repository = False
            opportunity.issue_open = False
            opportunity.evidence.update(
                {
                    "github_not_found": "repository",
                    "github_enriched_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            opportunity.notes.append(
                "GitHub repository is unavailable; the marketplace row is stale."
            )
            return opportunity
        opportunity.notes.append(
            f"GitHub enrichment failed with HTTP {error.code}; signals remain unknown."
        )
        opportunity.evidence["github_error"] = error.code
        return opportunity

    opportunity.repository = repository.get("full_name", f"{owner}/{repo}")
    opportunity.public_repository = not bool(repository.get("private"))
    opportunity.repository_archived = bool(
        repository.get("archived") or repository.get("disabled")
    )
    opportunity.repository_created_at = repository.get("created_at")
    stars = repository.get("stargazers_count")
    opportunity.repository_stars = stars if isinstance(stars, int) else None
    license_data = repository.get("license")
    if isinstance(license_data, dict):
        opportunity.repository_license = (
            license_data.get("spdx_id") or license_data.get("key")
        )
    opportunity.language = repository.get("language")
    opportunity.maintainer_active = _recent(repository.get("pushed_at"))

    try:
        issue = client.get(f"{base}/issues/{number}")
    except HTTPError as error:
        if error.code in {404, 410}:
            opportunity.issue_open = False
            opportunity.evidence.update(
                {
                    "github_not_found": "issue",
                    "github_enriched_at": datetime.now(timezone.utc).isoformat(),
                    "github_repo_pushed_at": repository.get("pushed_at"),
                }
            )
            opportunity.notes.append(
                "GitHub issue is unavailable; the marketplace row is stale."
            )
            return opportunity
        opportunity.notes.append(
            f"GitHub enrichment failed with HTTP {error.code}; signals remain unknown."
        )
        opportunity.evidence["github_error"] = error.code
        return opportunity

    opportunity.issue_open = issue.get("state") == "open"
    opportunity.issue_activity_recent = _recent(issue.get("updated_at"), days=180)
    opportunity.title = issue.get("title") or opportunity.title

    labels = {
        str(label.get("name", "")).lower()
        for label in issue.get("labels", [])
        if isinstance(label, dict)
    }
    label_text = " ".join(labels)
    opportunity.security_sensitive = any(
        term in label_text for term in SECURITY_TERMS
    )
    body = issue.get("body") or ""
    opportunity.acceptance_criteria_clear = (
        len(body.strip()) >= 120
        and any(term in body.lower() for term in CLARITY_TERMS)
    )
    scope_hours, scope_signals = _estimate_agent_hours(
        opportunity.title, body, labels
    )
    if opportunity.estimated_agent_hours is None:
        opportunity.estimated_agent_hours = scope_hours
    opportunity.evidence["scope_estimate"] = {
        "agent_hours": scope_hours,
        "signals": scope_signals,
    }

    default_branch = repository.get("default_branch")
    if default_branch:
        try:
            tree = client.get(f"{base}/git/trees/{quote(default_branch)}?recursive=1")
            (
                opportunity.has_test_suite,
                opportunity.contribution_guide_found,
            ) = _tree_signals(tree)
            opportunity.evidence["tree_truncated"] = bool(tree.get("truncated"))
        except HTTPError as error:
            opportunity.notes.append(
                f"Repository tree inspection failed with HTTP {error.code}."
            )

    try:
        timeline = client.get(
            f"{base}/issues/{number}/timeline?per_page=100",
            accept="application/vnd.github+json",
        )
        opportunity.competing_prs = _competition(timeline, number)
    except HTTPError as error:
        opportunity.notes.append(
            f"Linked-PR inspection failed with HTTP {error.code}."
        )

    try:
        comments = client.get(f"{base}/issues/{number}/comments?per_page=100")
        explicit_claims = _active_claims(comments)
        if opportunity.active_attempts is None:
            opportunity.active_attempts = explicit_claims
        else:
            opportunity.active_attempts = max(
                opportunity.active_attempts, explicit_claims
            )
        opportunity.evidence.update(reputation_policy_signals(issue, comments))
    except HTTPError as error:
        opportunity.notes.append(
            f"Claim-comment inspection failed with HTTP {error.code}."
        )

    opportunity.evidence.update(
        {
            "github_enriched_at": datetime.now(timezone.utc).isoformat(),
            "github_issue_updated_at": issue.get("updated_at"),
            "github_repo_pushed_at": repository.get("pushed_at"),
            "github_repo_created_at": repository.get("created_at"),
            "github_requests_used": client.requests_made,
        }
    )
    return opportunity
