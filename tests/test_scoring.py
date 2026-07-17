import unittest
from unittest.mock import patch

from bounty_hunter.models import Opportunity
from bounty_hunter.scoring import score_opportunity


def strong_opportunity(**overrides):
    values = {
        "source": "manual",
        "source_id": "1",
        "title": "Fix deterministic parser bug",
        "source_url": "https://example.test/bounty/1",
        "bounty_usd": 250.0,
        "status": "open",
        "deadline": "2030-01-01T00:00:00Z",
        "language": "Python",
        "submission_count": 0,
        "public_repository": True,
        "issue_open": True,
        "issue_activity_recent": True,
        "repository_archived": False,
        "repository_created_at": "2020-01-01T00:00:00Z",
        "repository_stars": 100,
        "repository_license": "MIT",
        "has_test_suite": True,
        "contribution_guide_found": True,
        "acceptance_criteria_clear": True,
        "reproducible": True,
        "maintainer_active": True,
        "competing_prs": 0,
        "security_sensitive": False,
        "estimated_agent_hours": 1.5,
        "estimated_human_hours": 0.2,
        "estimated_execution_cost_usd": 5.0,
    }
    values.update(overrides)
    return Opportunity(**values)


class ScoringTests(unittest.TestCase):
    def test_strong_opportunity_reaches_human_review(self):
        score = score_opportunity(strong_opportunity())
        self.assertTrue(score.eligible)
        self.assertEqual(score.gate_rejection_count, 0)
        self.assertEqual(score.decision, "approve_for_human_review")
        self.assertGreater(score.expected_value_usd, 25)

    def test_historical_calibration_changes_expected_value(self):
        opportunity = strong_opportunity()
        baseline = score_opportunity(opportunity)
        calibrated = score_opportunity(opportunity, calibration_factor=0.5)
        self.assertLess(calibrated.probability_win_merge, baseline.probability_win_merge)
        self.assertLess(calibrated.expected_value_usd, baseline.expected_value_usd)

    def test_closed_bounty_is_rejected(self):
        score = score_opportunity(strong_opportunity(status="awarded"))
        self.assertFalse(score.eligible)
        self.assertIn("bounty is not open", score.rejection_reasons)
        self.assertEqual(score.probability_valid, 0)
        self.assertLess(score.expected_value_usd, 0)
        self.assertEqual(score.priority_score, 0)
        self.assertGreater(score.gate_rejection_count, 0)

    def test_closed_issue_has_zero_payout_probability(self):
        score = score_opportunity(
            strong_opportunity(issue_open=False, bounty_usd=20_000)
        )
        self.assertFalse(score.eligible)
        self.assertEqual(score.probability_valid, 0)
        self.assertLess(score.expected_value_usd, 0)

    def test_withdrawal_or_ai_reputation_signal_has_zero_probability(self):
        for evidence, reason in (
            (
                {"bounty_withdrawal_signal": True},
                "GitHub discussion explicitly indicates the bounty was withdrawn",
            ),
            (
                {"ai_contribution_reputation_risk": True},
                "repository discussion indicates AI-assisted contributions create reputation risk",
            ),
        ):
            with self.subTest(evidence=evidence):
                score = score_opportunity(strong_opportunity(evidence=evidence))
                self.assertFalse(score.eligible)
                self.assertEqual(score.probability_valid, 0)
                self.assertIn(reason, score.rejection_reasons)

    def test_competing_pr_is_rejected(self):
        score = score_opportunity(strong_opportunity(competing_prs=1))
        self.assertFalse(score.eligible)
        self.assertIn(
            "a competing pull request already exists", score.rejection_reasons
        )

    def test_stale_issue_activity_is_rejected(self):
        score = score_opportunity(strong_opportunity(issue_activity_recent=False))
        self.assertFalse(score.eligible)
        self.assertIn(
            "bounty issue has had no activity in the last 180 days",
            score.rejection_reasons,
        )

    def test_many_active_claims_are_rejected(self):
        score = score_opportunity(strong_opportunity(active_attempts=3))
        self.assertFalse(score.eligible)
        self.assertIn("too many active bounty claims", score.rejection_reasons)

    def test_unknown_signals_reduce_confidence(self):
        opportunity = strong_opportunity(
            public_repository=None,
            has_test_suite=None,
            acceptance_criteria_clear=None,
            reproducible=None,
            maintainer_active=None,
            language=None,
            submission_count=None,
            deadline=None,
            repository_created_at=None,
            repository_stars=None,
            repository_license=None,
            contribution_guide_found=None,
        )
        score = score_opportunity(opportunity)
        self.assertLess(score.confidence, 0.5)
        self.assertTrue(score.warnings)
        self.assertFalse(score.eligible)
        self.assertIn(
            "public repository evidence is required", score.rejection_reasons
        )
        self.assertIn(
            "an existing test suite is required", score.rejection_reasons
        )

    def test_unknown_issue_state_and_clarity_are_rejected(self):
        score = score_opportunity(
            strong_opportunity(issue_open=None, acceptance_criteria_clear=None)
        )
        self.assertFalse(score.eligible)
        self.assertIn(
            "fresh evidence of an open GitHub issue is required",
            score.rejection_reasons,
        )
        self.assertIn(
            "clear acceptance criteria are required", score.rejection_reasons
        )
        self.assertEqual(score.probability_valid, 0)

    def test_security_sensitive_issue_is_rejected(self):
        score = score_opportunity(strong_opportunity(security_sensitive=True))
        self.assertFalse(score.eligible)
        self.assertIn(
            "security-sensitive work requires a separately authorized program",
            score.rejection_reasons,
        )

    def test_unestablished_or_policyless_repository_is_rejected(self):
        score = score_opportunity(
            strong_opportunity(
                repository_created_at="2026-07-01T00:00:00Z",
                repository_stars=2,
                repository_license=None,
                contribution_guide_found=False,
            )
        )
        self.assertFalse(score.eligible)
        self.assertIn(
            "repository is below the initial community-adoption threshold",
            score.rejection_reasons,
        )
        self.assertIn(
            "repository is too new for the initial reputation-safe policy",
            score.rejection_reasons,
        )
        self.assertIn(
            "an identifiable open-source license is required",
            score.rejection_reasons,
        )
        self.assertIn(
            "visible repository contribution guidance is required",
            score.rejection_reasons,
        )

    def test_stale_algora_board_has_zero_payout_probability(self):
        score = score_opportunity(
            strong_opportunity(evidence={"algora_evidence_stale": True})
        )
        self.assertFalse(score.eligible)
        self.assertEqual(score.probability_valid, 0)
        self.assertIn(
            "Algora board evidence is stale and requires refresh",
            score.rejection_reasons,
        )


if __name__ == "__main__":
    unittest.main()
