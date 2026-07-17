from datetime import datetime, timezone
from typing import List, Tuple

from .config import ScoringConfig
from .models import Opportunity, Score, parse_datetime


OPEN_STATUSES = {"active", "funded", "open", "published"}
SUPPORTED_LANGUAGES = {
    "javascript",
    "js",
    "python",
    "typescript",
    "ts",
}


def _probability(
    opportunity: Opportunity, calibration_factor: float = 1.0,
) -> Tuple[float, float, float, float, List[str]]:
    warnings: List[str] = []

    valid = 0.82
    if opportunity.deadline_at is None:
        valid -= 0.08
        warnings.append("deadline is unknown")
    if opportunity.maintainer_active is True:
        valid += 0.08
    elif opportunity.maintainer_active is False:
        valid -= 0.35
    else:
        warnings.append("maintainer activity is unknown")
    if opportunity.issue_activity_recent is True:
        valid += 0.05
    elif opportunity.issue_activity_recent is False:
        valid -= 0.25
    else:
        warnings.append("recent bounty-issue activity is unknown")

    verified = 0.58
    if opportunity.reproducible is True:
        verified += 0.22
    elif opportunity.reproducible is False:
        verified = 0.05
    else:
        warnings.append("reproduction has not been confirmed")
    if opportunity.has_test_suite is True:
        verified += 0.08
    elif opportunity.has_test_suite is False:
        verified -= 0.18
    else:
        warnings.append("test-suite availability is unknown")
    if opportunity.acceptance_criteria_clear is True:
        verified += 0.08
    elif opportunity.acceptance_criteria_clear is False:
        verified -= 0.25
    else:
        warnings.append("acceptance-criteria clarity is unknown")

    win_merge = 0.62
    competition = opportunity.active_attempts
    if competition is None:
        competition = opportunity.submission_count
    if competition is None:
        warnings.append("competition level is unknown")
    elif competition == 0:
        win_merge += 0.10
    else:
        win_merge *= 1 / (competition + 1)
    if opportunity.competing_prs:
        win_merge *= 1 / (opportunity.competing_prs + 1)
    win_merge *= max(0.25, min(1.5, calibration_factor))

    known_signals = [
        opportunity.deadline is not None,
        opportunity.maintainer_active is not None,
        opportunity.reproducible is not None,
        opportunity.has_test_suite is not None,
        opportunity.acceptance_criteria_clear is not None,
        competition is not None,
        opportunity.language is not None,
        opportunity.public_repository is not None,
        opportunity.issue_open is not None,
        opportunity.issue_activity_recent is not None,
        opportunity.repository_archived is not None,
        opportunity.repository_created_at is not None,
        opportunity.repository_stars is not None,
        opportunity.repository_license is not None,
        opportunity.contribution_guide_found is not None,
        opportunity.security_sensitive is not None,
    ]
    confidence = sum(known_signals) / len(known_signals)

    clamp = lambda value: max(0.01, min(0.98, value))
    return clamp(valid), clamp(verified), clamp(win_merge), confidence, warnings


def score_opportunity(
    opportunity: Opportunity,
    config: ScoringConfig = ScoringConfig(),
    calibration_factor: float = 1.0,
) -> Score:
    reasons: List[str] = []
    warnings: List[str] = []
    status = opportunity.status.lower()

    if status not in OPEN_STATUSES:
        reasons.append("bounty is not open")
    if opportunity.deadline_at and opportunity.deadline_at <= datetime.now(timezone.utc):
        reasons.append("submission deadline has passed")
    if opportunity.public_repository is not True:
        reasons.append("public repository evidence is required")
    if opportunity.issue_open is not True:
        reasons.append("fresh evidence of an open GitHub issue is required")
    if opportunity.issue_activity_recent is False:
        reasons.append("bounty issue has had no activity in the last 180 days")
    if opportunity.repository_archived is True:
        reasons.append("repository is archived")
    created_at = parse_datetime(opportunity.repository_created_at)
    repository_age_days = (
        (datetime.now(timezone.utc) - created_at).days if created_at else None
    )
    if opportunity.repository_stars is None:
        reasons.append("repository adoption evidence is required")
    elif opportunity.repository_stars < config.minimum_repository_stars:
        reasons.append("repository is below the initial community-adoption threshold")
    if repository_age_days is None:
        reasons.append("repository age evidence is required")
    elif repository_age_days < config.minimum_repository_age_days:
        reasons.append("repository is too new for the initial reputation-safe policy")
    if not opportunity.repository_license:
        reasons.append("an identifiable open-source license is required")
    if opportunity.evidence.get("github_evidence_stale"):
        reasons.append("GitHub evidence is stale and requires refresh")
    if opportunity.evidence.get("algora_evidence_stale"):
        reasons.append("Algora board evidence is stale and requires refresh")
    if opportunity.evidence.get("opire_evidence_stale"):
        reasons.append("Opire evidence is stale and requires refresh")
    if opportunity.evidence.get("taskbounty_evidence_stale"):
        reasons.append("TaskBounty evidence is stale and requires refresh")
    if opportunity.evidence.get("taskbounty_detail_stale"):
        reasons.append("TaskBounty detail evidence is stale and requires refresh")
    if opportunity.evidence.get("boss_evidence_stale"):
        reasons.append("Boss evidence is stale and requires refresh")
    if opportunity.evidence.get("bountypay_evidence_stale"):
        reasons.append("BountyPay evidence is stale and requires refresh")
    if opportunity.evidence.get("payment_testnet"):
        reasons.append("testnet bounty cannot produce real income")
    if opportunity.evidence.get("reward_outlier"):
        reasons.append("reward amount is an unverified marketplace outlier")
    if opportunity.evidence.get("bounty_withdrawal_signal"):
        reasons.append("GitHub discussion explicitly indicates the bounty was withdrawn")
    if opportunity.evidence.get("ai_contribution_reputation_risk"):
        reasons.append(
            "repository discussion indicates AI-assisted contributions create reputation risk"
        )
    if opportunity.payout_requires_sponsor_action is True:
        reasons.append(
            "payout requires sponsor action after completion and is not guaranteed"
        )
    if opportunity.evidence.get("payout_country_restrictions_apply"):
        if opportunity.payout_country_eligible is False:
            reasons.append("configured payout country is not supported")
        elif opportunity.payout_country_eligible is not True:
            reasons.append("payout-country eligibility must be confirmed")
    if opportunity.security_sensitive is True:
        reasons.append("security-sensitive work requires a separately authorized program")
    if opportunity.acceptance_criteria_clear is not True:
        reasons.append("clear acceptance criteria are required")
    if opportunity.reproducible is False:
        reasons.append("reported failure could not be reproduced")
    if opportunity.maintainer_active is False:
        reasons.append("maintainer appears inactive")
    if opportunity.language and opportunity.language.lower() not in SUPPORTED_LANGUAGES:
        reasons.append("language is outside the initial supported set")
    if opportunity.has_test_suite is not True:
        reasons.append("an existing test suite is required")
    if opportunity.contribution_guide_found is not True:
        reasons.append("visible repository contribution guidance is required")
    if opportunity.bounty_usd < config.minimum_bounty_usd:
        reasons.append("bounty is below the initial minimum")
    if opportunity.bounty_usd > config.maximum_bounty_usd:
        warnings.append("bounty exceeds the preferred range")
    if (
        opportunity.submission_count is not None
        and opportunity.submission_count > config.maximum_submission_count
    ):
        reasons.append("too many existing submissions")
    if (
        opportunity.active_attempts is not None
        and opportunity.active_attempts > config.maximum_submission_count
    ):
        reasons.append("too many active bounty claims")
    if opportunity.competing_prs and opportunity.competing_prs > 0:
        reasons.append("a competing pull request already exists")

    agent_hours = opportunity.estimated_agent_hours or config.default_agent_hours
    human_hours = opportunity.estimated_human_hours or config.default_human_hours
    if agent_hours > config.maximum_agent_hours:
        reasons.append("estimated agent time exceeds the initial limit")

    gate_rejection_count = len(reasons)

    valid, verified, win_merge, confidence, probability_warnings = _probability(
        opportunity, calibration_factor
    )
    warnings.extend(probability_warnings)
    deadline_passed = bool(
        opportunity.deadline_at
        and opportunity.deadline_at <= datetime.now(timezone.utc)
    )
    if (
        status not in OPEN_STATUSES
        or deadline_passed
        or opportunity.public_repository is not True
        or opportunity.issue_open is not True
        or opportunity.repository_archived is True
        or opportunity.evidence.get("github_evidence_stale")
        or opportunity.evidence.get("algora_evidence_stale")
        or opportunity.evidence.get("opire_evidence_stale")
        or opportunity.evidence.get("taskbounty_evidence_stale")
        or opportunity.evidence.get("taskbounty_detail_stale")
        or opportunity.evidence.get("boss_evidence_stale")
        or opportunity.evidence.get("bountypay_evidence_stale")
        or opportunity.evidence.get("payment_testnet")
        or opportunity.evidence.get("reward_outlier")
        or opportunity.evidence.get("bounty_withdrawal_signal")
        or opportunity.evidence.get("ai_contribution_reputation_risk")
        or opportunity.payout_requires_sponsor_action is True
        or (
            opportunity.evidence.get("payout_country_restrictions_apply")
            and opportunity.payout_country_eligible is not True
        )
    ):
        valid = 0.0
    payout_probability = valid * verified * win_merge

    fee_rate = (
        opportunity.platform_fee_rate
        if opportunity.platform_fee_rate is not None
        else config.default_platform_fee_rate
    )
    fixed_fee = opportunity.platform_fee_fixed_usd or 0.0
    net_payout = max(0.0, opportunity.bounty_usd * (1 - fee_rate) - fixed_fee)
    execution_cost = (
        opportunity.estimated_execution_cost_usd
        if opportunity.estimated_execution_cost_usd is not None
        else config.default_execution_cost_usd
    )
    expected_cost = (
        execution_cost
        + human_hours * config.human_hour_value_usd
        + config.revision_reserve_usd
        + net_payout * config.risk_reserve_rate
    )
    expected_value = net_payout * payout_probability - expected_cost
    total_hours = max(0.1, agent_hours + human_hours)
    hourly_return = expected_value / total_hours

    if payout_probability < config.minimum_payout_probability:
        reasons.append("estimated payout probability is below threshold")
    if expected_value < config.minimum_expected_value_usd:
        reasons.append("expected value is below threshold")
    if hourly_return < config.minimum_expected_hourly_return_usd:
        reasons.append("expected hourly return is below threshold")

    eligible = not reasons
    if eligible and confidence >= 0.75:
        decision = "approve_for_human_review"
    elif eligible:
        decision = "needs_enrichment"
    else:
        decision = "reject"

    priority_score = (
        max(0.0, expected_value) * (0.5 + confidence / 2)
        if eligible
        else 0.0
    )
    return Score(
        eligible=eligible,
        decision=decision,
        rejection_reasons=reasons,
        gate_rejection_count=gate_rejection_count,
        warnings=warnings,
        confidence=round(confidence, 4),
        probability_valid=round(valid, 4),
        probability_verified=round(verified, 4),
        probability_win_merge=round(win_merge, 4),
        payout_probability=round(payout_probability, 4),
        net_payout_usd=round(net_payout, 2),
        expected_cost_usd=round(expected_cost, 2),
        expected_value_usd=round(expected_value, 2),
        expected_hourly_return_usd=round(hourly_return, 2),
        priority_score=round(priority_score, 2),
    )
