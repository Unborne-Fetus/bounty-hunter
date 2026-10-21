import json
import tempfile
import unittest
from pathlib import Path

from bounty_hunter.algora_discovery import (
    audit_candidates,
    candidate_org_counts,
    configured_orgs,
    search_candidate_items,
    write_report,
)


class FakeBoardClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def fetch(self, org, _timeout):
        self.calls.append(org)
        value = self.pages[org]
        if isinstance(value, Exception):
            raise value
        return value


class FakeGitHubClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        return self.pages.pop(0)


class AlgoraDiscoveryTests(unittest.TestCase):
    def test_exact_label_produces_owner_counts(self):
        items = [
            {
                "repository_url": "https://api.github.com/repos/acme/one",
                "labels": [{"name": "💎 Bounty"}],
            },
            {
                "repository_url": "https://api.github.com/repos/acme/two",
                "labels": [{"name": "💎 Bounty"}],
            },
            {
                "repository_url": "https://api.github.com/repos/noise/one",
                "labels": [{"name": "bounty"}],
            },
        ]
        self.assertEqual(candidate_org_counts(items), {"acme": 2})

    def test_only_fresh_nonempty_board_is_suggested(self):
        row = """
        <table><tr><td>$100
        <a href="https://github.com/good/repo/issues/1">repo#1 Fix</a>
        </td></tr></table>
        """
        client = FakeBoardClient(
            {
                "good": (row, False),
                "empty": ("<html></html>", False),
                "stale": (row, True),
                "broken": RuntimeError("rate limited"),
            }
        )
        results = audit_candidates(
            {"configured": 9, "good": 5, "empty": 4, "stale": 3, "broken": 2},
            client,
            configured=["configured"],
            maximum_candidates=10,
        )
        promotions = {item["organization"]: item["promotion"] for item in results}
        self.assertEqual(promotions["good"], "review_for_trusted_list")
        self.assertEqual(promotions["empty"], "do_not_promote")
        self.assertEqual(promotions["stale"], "do_not_promote")
        self.assertEqual(promotions["broken"], "do_not_promote")
        self.assertNotIn("configured", client.calls)

    def test_public_board_hints_are_verified_before_import(self):
        from bounty_hunter.algora_discovery import PUBLIC_ALGORA_BOARD_HINTS
        self.assertIn("projectdiscovery", PUBLIC_ALGORA_BOARD_HINTS)
        row = """
        <table><tr><td>$100
        <a href="https://github.com/example/tools/issues/42">tools#42 Fix a bug</a>
        </td></tr></table>
        """
        client = FakeBoardClient({
            "projectdiscovery": (row, False),
            "empty": ("<html></html>", False),
        })
        found = audit_candidates(
            {"empty": 3}, client,
            maximum_candidates=2,
            seed_organizations=["projectdiscovery"],
        )
        self.assertEqual([item["organization"] for item in found],
                         ["projectdiscovery", "empty"])
        self.assertEqual(found[0]["promotion"], "review_for_trusted_list")
        self.assertEqual(found[1]["promotion"], "do_not_promote")
        self.assertEqual(client.calls, ["projectdiscovery", "empty"])

    def test_stale_public_board_hints_never_promote(self):
        row = """
        <table><tr><td>$300
        <a href="https://github.com/example/tools/issues/99">tools#99 Fix a bug</a>
        </td></tr></table>
        """
        client = FakeBoardClient({"seed": (row, True)})
        found = audit_candidates({}, client, seed_organizations=["seed"])
        self.assertEqual(found[0]["status"], "stale")
        self.assertEqual(found[0]["promotion"], "do_not_promote")

    def test_search_reads_bounded_pages_and_stops_on_short_page(self):
        client = FakeGitHubClient(
            [
                {"total_count": 150, "items": [{"id": i} for i in range(100)]},
                {"total_count": 150, "items": [{"id": i} for i in range(50)]},
            ]
        )
        result = search_candidate_items(client, maximum_pages=5)
        self.assertEqual(result["total_count"], 150)
        self.assertEqual(len(result["items"]), 150)
        self.assertEqual(result["pages_fetched"], 2)
        self.assertEqual(len(client.calls), 2)
        self.assertIn("page=2", client.calls[1])

    def test_config_and_report_are_deterministic_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "orgs.txt"
            config.write_text("# comment\nacme\n\nother\n", encoding="utf-8")
            self.assertEqual(configured_orgs(str(config)), ["acme", "other"])
            report = Path(temporary) / "report.json"
            write_report(str(report), {"ok": True})
            self.assertEqual(json.loads(report.read_text()), {"ok": True})


if __name__ == "__main__":
    unittest.main()
