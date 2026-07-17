from dataclasses import dataclass


@dataclass(frozen=True)
class ScoringConfig:
    minimum_bounty_usd: float = 50.0
    maximum_bounty_usd: float = 500.0
    maximum_agent_hours: float = 3.0
    maximum_submission_count: int = 2
    minimum_repository_stars: int = 10
    minimum_repository_age_days: int = 180
    minimum_expected_value_usd: float = 25.0
    minimum_expected_hourly_return_usd: float = 20.0
    minimum_payout_probability: float = 0.35
    default_platform_fee_rate: float = 0.0
    default_execution_cost_usd: float = 8.0
    default_agent_hours: float = 2.0
    default_human_hours: float = 0.35
    human_hour_value_usd: float = 25.0
    revision_reserve_usd: float = 5.0
    risk_reserve_rate: float = 0.05
