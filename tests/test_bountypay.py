import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from bounty_hunter.scoring import score_opportunity
from bounty_hunter.sources.bountypay import (
    BountyPayClient,
    fetch_inventory,
    normalize,
)


def item(network="BASE", chain_id=8453, deadline=4_102_444_800):
    return {
        "bountyId": "0xabc",
        "repoFullName": "acme/parser",
        "issueNumber": 42,
        "amount": "250000000",
        "deadline": deadline,
        "status": "open",
        "txHash": "0xtx",
        "network": network,
        "chainId": chain_id,
        "tokenSymbol": "USDC",
        "token": "0xtoken",
        "sponsorAddress": "0xsponsor",
        "issueTitle": "Fix parser regression",
    }


class BountyPayAdapterTests(unittest.TestCase):
    def test_normalizes_mainnet_funded_record(self):
        opportunity = normalize([item()])[0]
        self.assertEqual(opportunity.source_id, "0xabc")
        self.assertEqual(opportunity.repository, "acme/parser")
        self.assertEqual(opportunity.issue_url, "https://github.com/acme/parser/issues/42")
        self.assertEqual(opportunity.bounty_usd, 250)
        self.assertFalse(opportunity.evidence["payment_testnet"])
        self.assertFalse(opportunity.payout_requires_sponsor_action)

    def test_testnet_record_has_zero_probability(self):
        opportunity = normalize([item("MEZO_TESTNET", 31611)])[0]
        score = score_opportunity(opportunity)
        self.assertTrue(opportunity.evidence["payment_testnet"])
        self.assertEqual(score.probability_valid, 0)
        self.assertIn(
            "testnet bounty cannot produce real income", score.rejection_reasons
        )

    def test_unknown_token_is_ignored(self):
        payload = item()
        payload["tokenSymbol"] = "UNKNOWN"
        self.assertEqual(normalize([payload]), [])

    def test_cache_falls_back_as_stale(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bountypay.json"
            path.write_text("[]", encoding="utf-8")

            def fail(_timeout):
                raise RuntimeError("offline")

            client = BountyPayClient(
                cache_path=str(path), cache_ttl_seconds=-1, transport=fail
            )
            payload, stale = client.fetch()
            self.assertEqual(payload, [])
            self.assertTrue(stale)

    def test_transport_uses_bounded_curl_when_urlopen_fails(self):
        result = subprocess.CompletedProcess(
            ["curl"], 0, stdout=b"[]", stderr=b""
        )
        with patch(
            "bounty_hunter.sources.bountypay.urlopen",
            side_effect=URLError("offline"),
        ), patch(
            "bounty_hunter.sources.bountypay.shutil.which",
            return_value="/usr/bin/curl",
        ), patch(
            "bounty_hunter.sources.bountypay.subprocess.run",
            return_value=result,
        ) as run:
            self.assertEqual(fetch_inventory(7), [])
        command = run.call_args.args[0]
        self.assertIn("--max-time", command)
        self.assertIn("7", command)


if __name__ == "__main__":
    unittest.main()
