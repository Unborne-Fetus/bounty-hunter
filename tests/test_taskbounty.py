import tempfile
import unittest

from bounty_hunter.scoring import score_opportunity
from bounty_hunter.sources.taskbounty import (
    TaskBountyClient,
    enrich_from_detail_html,
    normalize,
)


class TaskBountyAdapterTests(unittest.TestCase):
    def test_normalizes_public_api_shape(self):
        opportunities = normalize(
            {
                "data": [
                    {
                        "id": "abc",
                        "slug": "fix-parser",
                        "title": "Fix parser",
                        "bounty_cents": 5000,
                        "status": "OPEN",
                        "submission_deadline": "2030-01-01T00:00:00Z",
                        "submission_count": 1,
                        "tags": ["bug-fix"],
                    }
                ]
            }
        )
        self.assertEqual(len(opportunities), 1)
        self.assertEqual(opportunities[0].bounty_usd, 50)
        self.assertEqual(opportunities[0].status, "open")
        self.assertEqual(opportunities[0].platform_fee_rate, 0.20)

    def test_detail_html_resolves_public_github_issue(self):
        opportunity = normalize(
            {
                "data": [
                    {
                        "id": "abc",
                        "slug": "fix-parser",
                        "title": "Fix parser",
                        "bounty_cents": 5000,
                        "status": "OPEN",
                    }
                ]
            }
        )[0]
        enrich_from_detail_html(
            opportunity,
            '<a href="https://github.com/acme/parser/issues/42">issue</a>',
        )
        self.assertEqual(opportunity.repository, "acme/parser")
        self.assertEqual(
            opportunity.issue_url,
            "https://github.com/acme/parser/issues/42",
        )
        self.assertTrue(opportunity.public_repository)

    def test_summary_and_detail_failures_use_stale_cache(self):
        payload = {
            "data": [
                {
                    "id": "abc",
                    "slug": "fix-parser",
                    "title": "Fix parser",
                    "bounty_cents": 5000,
                    "status": "OPEN",
                }
            ]
        }

        def summary(_timeout):
            return payload

        def detail(_opportunity, _timeout):
            return '<a href="https://github.com/acme/parser/issues/42">issue</a>'

        with tempfile.TemporaryDirectory() as temporary:
            warm = TaskBountyClient(
                cache_dir=temporary,
                cache_ttl_seconds=-1,
                summary_transport=summary,
                detail_transport=detail,
            )
            cached_payload, _ = warm.fetch_summary()
            opportunity = normalize(cached_payload)[0]
            warm.fetch_detail(opportunity)

            def fail(*_args):
                raise RuntimeError("offline")

            cold = TaskBountyClient(
                cache_dir=temporary,
                cache_ttl_seconds=-1,
                summary_transport=fail,
                detail_transport=fail,
            )
            stale_payload, summary_stale = cold.fetch_summary()
            stale_opportunity = normalize(
                stale_payload, evidence_stale=summary_stale
            )[0]
            html, detail_stale = cold.fetch_detail(stale_opportunity)
        self.assertTrue(summary_stale)
        self.assertTrue(detail_stale)
        self.assertIn("github.com/acme/parser", html)
        score = score_opportunity(stale_opportunity)
        self.assertIn(
            "TaskBounty evidence is stale and requires refresh",
            score.rejection_reasons,
        )
        self.assertEqual(score.probability_valid, 0)


if __name__ == "__main__":
    unittest.main()
