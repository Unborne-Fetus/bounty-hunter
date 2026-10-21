import tempfile
import unittest
from pathlib import Path

from bounty_hunter.scoring import score_opportunity
from bounty_hunter.sources.boss import BossClient, normalize


ITEM = {
    "gid": "github/issue-id",
    "hId": "acme/parser#42",
    "sByC": {"USD": 250},
    "status": "open",
    "title": "Fix parser regression",
    "url": "https://github.com/acme/parser/issues/42",
    "usd": 250,
}


class BossAdapterTests(unittest.TestCase):
    def test_normalizes_public_inventory_and_fees(self):
        opportunity = normalize([ITEM], payout_country="US")[0]
        self.assertEqual(opportunity.source_id, "github/issue-id")
        self.assertEqual(opportunity.repository, "acme/parser")
        self.assertEqual(opportunity.bounty_usd, 250)
        self.assertEqual(opportunity.platform_fee_rate, 0.05)
        self.assertEqual(opportunity.platform_fee_fixed_usd, 0.60)
        self.assertTrue(opportunity.payout_country_eligible)
        self.assertTrue(opportunity.payout_requires_sponsor_action)
        self.assertEqual(
            opportunity.evidence["payment_assurance"],
            "not_escrowed_by_public_evidence",
        )

    def test_missing_or_unsupported_country_fails_closed(self):
        unknown = score_opportunity(normalize([ITEM])[0])
        unsupported = score_opportunity(normalize([ITEM], payout_country="DO")[0])
        self.assertIn(
            "payout-country eligibility must be confirmed",
            unknown.rejection_reasons,
        )
        self.assertIn(
            "configured payout country is not supported",
            unsupported.rejection_reasons,
        )
        self.assertEqual(unknown.probability_valid, 0)
        self.assertEqual(unsupported.probability_valid, 0)

    def test_sponsor_charge_warns_without_replacing_evidence_checks(self):
        score = score_opportunity(normalize([ITEM], payout_country="US")[0])
        self.assertFalse(score.eligible)
        self.assertEqual(score.decision, "needs_verification")
        self.assertEqual(score.probability_valid, 0)
        self.assertIn(
            "payout requires sponsor action after completion and is not guaranteed",
            score.warnings,
        )

    def test_zero_value_placeholder_is_not_a_bounty(self):
        item = {**ITEM, "usd": 0, "sByC": {}}
        self.assertEqual(normalize([item]), [])

    def test_fixed_fee_is_included_in_net_payout(self):
        opportunity = normalize([ITEM], payout_country="US")[0]
        opportunity.payout_requires_sponsor_action = False
        opportunity.evidence["payout_country_restrictions_apply"] = False
        score = score_opportunity(opportunity)
        self.assertEqual(score.net_payout_usd, 236.9)

    def test_cache_falls_back_as_stale(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "boss.json"
            path.write_text("[]", encoding="utf-8")

            def fail(_timeout):
                raise RuntimeError("offline")

            client = BossClient(
                cache_path=str(path), cache_ttl_seconds=-1, transport=fail
            )
            payload, stale = client.fetch()
            self.assertEqual(payload, [])
            self.assertTrue(stale)


if __name__ == "__main__":
    unittest.main()
