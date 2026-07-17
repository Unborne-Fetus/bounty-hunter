import json
import tempfile
import unittest
from pathlib import Path

from bounty_hunter.scoring import score_opportunity
from bounty_hunter.sources.opire import OpireClient, inventory_metadata, normalize_html


def fixture_html(rewards):
    flight = (
        'c:["$","$L19",null,{"initialRewards":'
        + json.dumps(rewards)
        + ',"homeKPIs":{"bountiesAvailable":290}}]'
    )
    script = "self.__next_f.push([1," + json.dumps(flight) + "])"
    return f"<html><script>{script}</script></html>"


class OpireAdapterTests(unittest.TestCase):
    def test_normalizes_server_rendered_reward(self):
        reward = {
            "id": "reward-1",
            "title": "Fix parser regression",
            "url": "https://github.com/acme/parser/issues/42",
            "programmingLanguages": ["TypeScript"],
            "createdAt": 1_700_000_000_000,
            "pendingPrice": {"value": 25_000, "unit": "USD_CENT"},
            "claimerUsers": [{"username": "one"}],
            "tryingUsers": [{"username": "one"}, {"username": "two"}],
            "project": {
                "id": "project-1",
                "isPublic": True,
                "isBotInstalled": True,
            },
        }
        opportunity = normalize_html(fixture_html([reward]))[0]
        self.assertEqual(opportunity.source_id, "reward-1")
        self.assertEqual(opportunity.repository, "acme/parser")
        self.assertEqual(opportunity.bounty_usd, 250)
        self.assertEqual(opportunity.language, "TypeScript")
        self.assertEqual(opportunity.active_attempts, 2)
        self.assertTrue(opportunity.public_repository)
        self.assertTrue(opportunity.payout_requires_sponsor_action)
        self.assertEqual(
            opportunity.evidence["payment_model"],
            "creator_checkout_after_reward_claim",
        )
        self.assertEqual(
            opportunity.evidence["payment_assurance"],
            "not_escrowed_or_guaranteed_by_public_evidence",
        )
        self.assertEqual(
            opportunity.evidence["payment_model_source"],
            "https://docs.opire.dev/rewards/introduction",
        )
        self.assertEqual(
            inventory_metadata(fixture_html([reward])),
            {"available_reported": 290, "initial_page_count": 1},
        )

    def test_creator_action_risk_is_fail_closed(self):
        reward = {
            "id": "reward-1",
            "title": "Fix parser",
            "url": "https://github.com/acme/parser/issues/42",
            "pendingPrice": {"value": 25_000, "unit": "USD_CENT"},
            "project": {"isPublic": True},
        }
        score = score_opportunity(normalize_html(fixture_html([reward]))[0])
        self.assertFalse(score.eligible)
        self.assertEqual(score.probability_valid, 0)
        self.assertIn(
            "payout requires sponsor action after completion and is not guaranteed",
            score.rejection_reasons,
        )

    def test_cache_falls_back_as_stale(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "opire.html"
            path.write_text("cached", encoding="utf-8")

            def fail(_timeout):
                raise RuntimeError("offline")

            client = OpireClient(
                cache_path=str(path), cache_ttl_seconds=-1, transport=fail
            )
            html, stale = client.fetch()
            self.assertEqual(html, "cached")
            self.assertTrue(stale)


if __name__ == "__main__":
    unittest.main()
