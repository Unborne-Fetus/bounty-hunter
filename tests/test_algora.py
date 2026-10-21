import tempfile
import unittest
from urllib.error import HTTPError

from bounty_hunter.sources.algora import AlgoraBoardClient, normalize_html


class AlgoraAdapterTests(unittest.TestCase):
    def test_current_cards_are_isolated_and_pool_rewards(self):
        def card(amount, number, title):
            return (f'<div class="bg-white p-4"><div class="min-w-0 pl-2">'
                    f'<div>${amount}</div><a class="group/issue inline-flex" '
                    f'href="https://github.com/acme/parser/issues/{number}">'
                    f'<p>parser#{number}</p><p>{title}</p></a></div></div>')
        html = '<div class="divide-y">' + ''.join([
            card('50', 42, 'Fix parser'), card('100', 42, 'Fix parser'),
            card('200', 43, 'Fix another bug'),
        ]) + '</div>'
        rows = normalize_html('acme', html)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r.bounty_usd for r in rows], [150, 200])
        self.assertEqual(rows[0].title, 'Fix parser')
        self.assertEqual(rows[0].evidence['algora_pooled_count'], 2)
        self.assertEqual(rows[1].issue_url, 'https://github.com/acme/parser/issues/43')

    def test_card_does_not_borrow_money_from_another_element(self):
        html = '''<div><div>$900</div><div class="min-w-0">
          <a class="group/issue" href="https://github.com/acme/parser/issues/42">
          parser#42 No amount in this card</a></div></div>'''
        self.assertEqual(normalize_html('acme', html), [])

    def test_navigation_links_do_not_become_bounties(self):
        html = '''<div class="min-w-0">$100
          <a href="https://github.com/acme/parser/issues/42">Issue</a></div>'''
        self.assertEqual(normalize_html('acme', html), [])

    def test_card_and_legacy_row_are_each_counted_once(self):
        html = '''<table><tr><td>$50
          <a class="group/issue" href="https://github.com/acme/parser/issues/42">
          parser#42 Fix</a></td></tr></table>
          <div class="min-w-0">$100
          <a class="group/issue" href="https://github.com/acme/parser/issues/43">
          parser#43 Another fix</a></div>'''
        self.assertEqual([r.bounty_usd for r in normalize_html('acme', html)], [50, 100])

    def test_normalizes_liveview_bounty_row(self):
        html = """
        <table><tbody><tr>
          <td>
            <div class="font-extrabold">$250</div>
            <a href="https://github.com/acme/parser/issues/42">
              <p>parser#42</p><p>Fix escaped delimiter handling</p>
            </a>
          </td>
          <td><div phx-value-id="bounty-123">2 claims</div></td>
        </tr></tbody></table>
        """
        opportunities = normalize_html("acme", html)
        self.assertEqual(len(opportunities), 1)
        opportunity = opportunities[0]
        self.assertEqual(opportunity.bounty_usd, 250)
        self.assertEqual(opportunity.repository, "acme/parser")
        self.assertEqual(opportunity.active_attempts, 2)
        self.assertEqual(opportunity.source_id, "bounty-123")
        self.assertEqual(opportunity.title, "Fix escaped delimiter handling")

    def test_persistent_cache_avoids_duplicate_board_request(self):
        calls = []

        def transport(org, _timeout):
            calls.append(org)
            return "<html>one</html>"

        with tempfile.TemporaryDirectory() as temporary:
            first = AlgoraBoardClient(
                cache_dir=temporary,
                transport=transport,
                minimum_request_interval_seconds=0,
            )
            self.assertEqual(first.fetch("acme"), ("<html>one</html>", False))
            second = AlgoraBoardClient(
                cache_dir=temporary,
                transport=transport,
                minimum_request_interval_seconds=0,
            )
            self.assertEqual(second.fetch("acme"), ("<html>one</html>", False))
        self.assertEqual(calls, ["acme"])

    def test_failed_refresh_uses_stale_cache_fail_closed(self):
        def fail(_org, _timeout):
            raise RuntimeError("rate limited")

        with tempfile.TemporaryDirectory() as temporary:
            client = AlgoraBoardClient(
                cache_dir=temporary,
                cache_ttl_seconds=-1,
                transport=fail,
                minimum_request_interval_seconds=0,
            )
            path = client._path("acme")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("<html>stale</html>", encoding="utf-8")
            self.assertEqual(client.fetch("acme"), ("<html>stale</html>", True))

    def test_stale_rows_are_marked(self):
        html = """
        <table><tr><td>$250
        <a href="https://github.com/acme/parser/issues/42">
        parser#42 Fix parser</a></td></tr></table>
        """
        opportunity = normalize_html("acme", html, evidence_stale=True)[0]
        self.assertTrue(opportunity.evidence["algora_evidence_stale"])

    def test_rate_limit_opens_circuit_for_later_uncached_boards(self):
        calls = []

        def rate_limited(org, _timeout):
            calls.append(org)
            raise HTTPError(
                "https://algora.io/test",
                429,
                "Too Many Requests",
                {"Retry-After": "60"},
                None,
            )

        with tempfile.TemporaryDirectory() as temporary:
            client = AlgoraBoardClient(
                cache_dir=temporary,
                transport=rate_limited,
                minimum_request_interval_seconds=0,
                monotonic=lambda: 100.0,
            )
            with self.assertRaises(HTTPError):
                client.fetch("first")
            with self.assertRaisesRegex(RuntimeError, "cooldown"):
                client.fetch("second")
        self.assertEqual(calls, ["first"])

    def test_request_budget_rotates_stale_caches_without_extra_network_calls(self):
        calls = []

        def transport(org, _timeout):
            calls.append(org)
            return f"<html>{org}-fresh</html>"

        with tempfile.TemporaryDirectory() as temporary:
            client = AlgoraBoardClient(
                cache_dir=temporary,
                cache_ttl_seconds=-1,
                transport=transport,
                minimum_request_interval_seconds=0,
                maximum_requests_per_run=1,
            )
            for org in ("first", "second"):
                path = client._path(org)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"<html>{org}-stale</html>", encoding="utf-8")
            self.assertEqual(
                client.fetch("first"), ("<html>first-fresh</html>", False)
            )
            self.assertEqual(
                client.fetch("second"), ("<html>second-stale</html>", True)
            )
            self.assertEqual(client.last_fallback_reason, "request_budget_exhausted")
        self.assertEqual(calls, ["first"])

    def test_multiple_rewards_for_same_issue_are_pooled(self):
        html = """
        <table><tbody>
          <tr><td>$50 <a href="https://github.com/acme/parser/issues/42">
            parser#42 Fix parser</a></td><td phx-value-id="one">1 claim</td></tr>
          <tr><td>$100 <a href="https://github.com/acme/parser/issues/42">
            parser#42 Fix parser</a></td><td phx-value-id="two">1 claim</td></tr>
        </tbody></table>
        """
        opportunities = normalize_html("acme", html)
        self.assertEqual(len(opportunities), 1)
        opportunity = opportunities[0]
        self.assertEqual(opportunity.bounty_usd, 150)
        self.assertEqual(opportunity.source_id, "pool:acme:acme/parser#42")
        self.assertEqual(opportunity.evidence["algora_pooled_count"], 2)
        self.assertEqual(
            [item["source_id"] for item in opportunity.evidence["algora_contributions"]],
            ["one", "two"],
        )


if __name__ == "__main__":
    unittest.main()
