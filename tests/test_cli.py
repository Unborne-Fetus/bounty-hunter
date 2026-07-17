import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bounty_hunter.cli import (
    _apply_historical_github_fallback,
    _discovered_algora_orgs,
    _github_priority,
    _needs_github_enrichment,
)
from bounty_hunter.models import Opportunity
from bounty_hunter.scoring import score_opportunity


class HistoricalFallbackTests(unittest.TestCase):
    def test_fresh_discovery_report_adds_only_verified_safe_org_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "audit.json"
            path.write_text(
                json.dumps(
                    {
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        "candidates": [
                            {
                                "organization": "good-org",
                                "status": "fresh",
                                "promotion": "review_for_trusted_list",
                                "open_board_rows": 2,
                            },
                            {
                                "organization": "bad/org",
                                "status": "fresh",
                                "promotion": "review_for_trusted_list",
                                "open_board_rows": 2,
                            },
                            {
                                "organization": "stale-org",
                                "status": "stale",
                                "promotion": "review_for_trusted_list",
                                "open_board_rows": 1,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            organizations, health = _discovered_algora_orgs(str(path), 24)
        self.assertEqual(organizations, ["good-org"])
        self.assertEqual(health["status"], "fresh")

    def test_stale_discovery_report_adds_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "audit.json"
            path.write_text(
                json.dumps(
                    {
                        "generated_at": (
                            datetime.now(timezone.utc) - timedelta(days=10)
                        ).isoformat(),
                        "candidates": [],
                    }
                ),
                encoding="utf-8",
            )
            organizations, health = _discovered_algora_orgs(str(path), 24)
        self.assertEqual(organizations, [])
        self.assertEqual(health["status"], "stale")

    def test_stale_evidence_is_displayed_but_never_eligible(self):
        opportunity = Opportunity(
            source="algora",
            source_id="one",
            title="Stale listing",
            source_url="https://algora.io/acme/bounties",
            bounty_usd=20_000,
            status="open",
        )
        record = {
            "historical_scan_at": "2026-01-01T00:00:00+00:00",
            "opportunity": {
                "repository": "acme/project",
                "public_repository": True,
                "issue_open": False,
                "repository_archived": False,
                "language": "TypeScript",
                "has_test_suite": True,
                "acceptance_criteria_clear": True,
                "security_sensitive": False,
                "competing_prs": 0,
            },
        }
        _apply_historical_github_fallback(opportunity, record)
        score = score_opportunity(opportunity)
        self.assertFalse(score.eligible)
        self.assertEqual(score.probability_valid, 0)
        self.assertEqual(score.priority_score, 0)
        self.assertIn(
            "GitHub evidence is stale and requires refresh",
            score.rejection_reasons,
        )

    def test_github_enrichment_skips_conclusive_source_rejections(self):
        base = {
            "source": "algora",
            "source_id": "one",
            "title": "Fix parser",
            "source_url": "https://algora.io/acme/bounties",
            "bounty_usd": 250,
            "status": "open",
            "issue_url": "https://github.com/acme/parser/issues/1",
        }
        self.assertTrue(_needs_github_enrichment(Opportunity(**base)))
        self.assertFalse(
            _needs_github_enrichment(Opportunity(**{**base, "active_attempts": 3}))
        )
        self.assertFalse(
            _needs_github_enrichment(Opportunity(**{**base, "bounty_usd": 20}))
        )
        self.assertFalse(
            _needs_github_enrichment(Opportunity(**{**base, "status": "awarded"}))
        )
        self.assertFalse(
            _needs_github_enrichment(
                Opportunity(**{**base, "payout_requires_sponsor_action": True})
            )
        )

    def test_github_priority_prefers_small_uncontested_bug(self):
        bug = Opportunity(
            source="algora",
            source_id="bug",
            title="Fix parser regression",
            source_url="https://example.test",
            bounty_usd=250,
            status="open",
            active_attempts=0,
        )
        feature = Opportunity(
            source="algora",
            source_id="feature",
            title="Large integration",
            source_url="https://example.test",
            bounty_usd=600,
            status="open",
            active_attempts=2,
        )
        self.assertGreater(_github_priority(bug), _github_priority(feature))


if __name__ == "__main__":
    unittest.main()
