from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass
class Opportunity:
    source: str
    source_id: str
    title: str
    source_url: str
    bounty_usd: float
    status: str
    published_at: Optional[str] = None
    deadline: Optional[str] = None
    repository: Optional[str] = None
    issue_url: Optional[str] = None
    language: Optional[str] = None
    category: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    submission_count: Optional[int] = None
    active_attempts: Optional[int] = None
    public_repository: Optional[bool] = None
    issue_open: Optional[bool] = None
    issue_activity_recent: Optional[bool] = None
    repository_archived: Optional[bool] = None
    repository_created_at: Optional[str] = None
    repository_stars: Optional[int] = None
    repository_license: Optional[str] = None
    has_test_suite: Optional[bool] = None
    contribution_guide_found: Optional[bool] = None
    acceptance_criteria_clear: Optional[bool] = None
    reproducible: Optional[bool] = None
    maintainer_active: Optional[bool] = None
    competing_prs: Optional[int] = None
    security_sensitive: Optional[bool] = None
    estimated_agent_hours: Optional[float] = None
    estimated_human_hours: Optional[float] = None
    estimated_execution_cost_usd: Optional[float] = None
    platform_fee_rate: Optional[float] = None
    platform_fee_fixed_usd: Optional[float] = None
    payout_requires_sponsor_action: Optional[bool] = None
    payout_country_eligible: Optional[bool] = None
    notes: List[str] = field(default_factory=list)
    evidence: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def deadline_at(self) -> Optional[datetime]:
        return parse_datetime(self.deadline)

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result.pop("raw", None)
        return result

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "Opportunity":
        allowed = set(cls.__dataclass_fields__)
        return cls(**{key: item for key, item in value.items() if key in allowed})


@dataclass
class Score:
    eligible: bool
    decision: str
    rejection_reasons: List[str]
    gate_rejection_count: int
    warnings: List[str]
    confidence: float
    probability_valid: float
    probability_verified: float
    probability_win_merge: float
    payout_probability: float
    net_payout_usd: float
    expected_cost_usd: float
    expected_value_usd: float
    expected_hourly_return_usd: float
    priority_score: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
