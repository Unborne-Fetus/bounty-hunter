import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from unittest.mock import patch

from bounty_hunter.github import GitHubClient, enrich_opportunity, resolve_github_token
from bounty_hunter.models import Opportunity


class GitHubEnrichmentTests(unittest.TestCase):
    def test_mentions_are_not_competitors_and_cross_repo_number_is_not_ignored(self):
        from bounty_hunter.github import _competition
        def event(number, closes=None, state='open', repo='acme/other'):
            return {'event': 'cross-referenced', 'will_close_target': closes,
                    'source': {'issue': {'number': number, 'state': state,
                    'repository_url': repo, 'pull_request': {}}}}
        events = [event(9, False), event(10), event(11, True, 'closed'),
                  event(42, True), event(42, True)]
        self.assertEqual(_competition(events, 42), 1)

    def test_old_push_does_not_prove_maintainer_inactivity(self):
        comments = []
        def transport(url, _headers):
            if url.endswith('/repos/acme/parser'):
                return {'private': False, 'pushed_at': '2001-01-01T00:00:00Z'}
            if url.endswith('/issues/42'):
                return {'state': 'open'}
            return comments if '/comments?' in url else []
        def enrich():
            row = Opportunity(source='manual', source_id='42', title='Fix',
                source_url='https://example.test', bounty_usd=250, status='open',
                issue_url='https://github.com/acme/parser/issues/42')
            return enrich_opportunity(row, GitHubClient(transport=transport))
        first = enrich()
        self.assertIsNone(first.maintainer_active)
        self.assertEqual(first.evidence['maintainer_activity_basis'], 'requires_confirmation')
        comments.append({'author_association': 'OWNER', 'body': 'Still accepting fixes',
                         'updated_at': datetime.now(timezone.utc).isoformat()})
        second = enrich()
        self.assertTrue(second.maintainer_active)
        self.assertEqual(second.evidence['maintainer_activity_basis'], 'recent_maintainer_comment')

    def test_terminal_not_found_response_is_cached(self):
        calls = []

        def transport(url, _headers):
            calls.append(url)
            raise HTTPError(url, 404, "not found", None, None)

        with tempfile.TemporaryDirectory() as temporary:
            cache = f"{temporary}/github-cache.json"
            first = GitHubClient(transport=transport, cache_path=cache)
            with self.assertRaises(HTTPError):
                first.get("/repos/acme/missing")
            second = GitHubClient(transport=transport, cache_path=cache)
            with self.assertRaises(HTTPError) as caught:
                second.get("/repos/acme/missing")
        self.assertEqual(caught.exception.code, 404)
        self.assertEqual(len(calls), 1)

    def test_rate_limit_cooldown_persists_across_clients(self):
        calls = []
        reset = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())

        def transport(url, _headers):
            calls.append(url)
            raise HTTPError(
                url,
                403,
                "rate limited",
                {
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(reset),
                },
                None,
            )

        with tempfile.TemporaryDirectory() as temporary:
            cache = f"{temporary}/github-cache.json"
            first = GitHubClient(transport=transport, cache_path=cache)
            with self.assertRaises(HTTPError):
                first.get("/repos/acme/one")
            second = GitHubClient(transport=transport, cache_path=cache)
            with self.assertRaises(HTTPError):
                second.get("/repos/acme/two")
        self.assertEqual(len(calls), 1)
        self.assertEqual(second.requests_made, 0)

    def test_local_request_budget_stops_network_without_opening_rate_cooldown(self):
        calls = []

        def transport(url, _headers):
            calls.append(url)
            return {"ok": True}

        client = GitHubClient(transport=transport, maximum_requests_per_run=1)
        self.assertEqual(client.get("/repos/acme/one"), {"ok": True})
        with self.assertRaises(HTTPError) as caught:
            client.get("/repos/acme/two")
        self.assertEqual(caught.exception.code, 503)
        self.assertIsNone(client.rate_limited_until)
        self.assertEqual(calls, ["https://api.github.com/repos/acme/one"])

    def test_missing_issue_is_marked_closed_without_stale_fallback(self):
        def transport(url, _headers):
            if url.endswith("/repos/acme/parser"):
                return {
                    "full_name": "acme/parser",
                    "private": False,
                    "archived": False,
                    "disabled": False,
                    "language": "Python",
                    "pushed_at": "2030-01-01T00:00:00Z",
                }
            raise HTTPError(url, 404, "not found", None, None)

        opportunity = Opportunity(
            source="algora",
            source_id="missing",
            title="stale bounty",
            source_url="https://algora.io/acme/bounties",
            bounty_usd=100,
            status="open",
            issue_url="https://github.com/acme/parser/issues/404",
        )
        enrich_opportunity(opportunity, GitHubClient(transport=transport))
        self.assertTrue(opportunity.public_repository)
        self.assertFalse(opportunity.issue_open)
        self.assertEqual(opportunity.evidence["github_not_found"], "issue")
        self.assertNotIn("github_error", opportunity.evidence)

    def test_successful_responses_persist_across_clients(self):
        calls = []

        def transport(url, _headers):
            calls.append(url)
            return {"full_name": "acme/parser"}

        with tempfile.TemporaryDirectory() as temporary:
            cache = f"{temporary}/github-cache.json"
            first = GitHubClient(transport=transport, cache_path=cache)
            self.assertEqual(
                first.get("/repos/acme/parser")["full_name"], "acme/parser"
            )
            second = GitHubClient(transport=transport, cache_path=cache)
            self.assertEqual(
                second.get("/repos/acme/parser")["full_name"], "acme/parser"
            )
        self.assertEqual(len(calls), 1)

    def test_truncated_tree_and_unrecognized_body_remain_unknown(self):
        def transport(url, _headers):
            if url.endswith("/repos/acme/parser"):
                return {"private": False, "default_branch": "main"}
            if url.endswith("/issues/42"):
                return {"state": "open", "body": "A concise request without template headings"}
            if "/git/trees/main" in url:
                return {"truncated": True, "tree": [{"path": "README.md"}]}
            return []
        opportunity = Opportunity(
            source="manual", source_id="42", title="Fix parser",
            source_url="https://example.test", bounty_usd=250, status="open",
            issue_url="https://github.com/acme/parser/issues/42",
        )
        enrich_opportunity(opportunity, GitHubClient(transport=transport))
        self.assertIsNone(opportunity.has_test_suite)
        self.assertIsNone(opportunity.contribution_guide_found)
        self.assertIsNone(opportunity.acceptance_criteria_clear)
        self.assertEqual(opportunity.evidence["acceptance_criteria_check"],
                         "manual_review_required")

    def test_tree_detects_root_and_nested_test_files(self):
        from bounty_hunter.github import _tree_signals
        for path in ("test_parser.py", "src/parser.spec.ts", "lib/parser_test.py"):
            with self.subTest(path=path):
                self.assertEqual(_tree_signals({"tree": [{"path": path}]}),
                                 (True, False))

    def test_enriches_repository_issue_and_competition_signals(self):
        def transport(url, _headers):
            if url.endswith("/repos/acme/parser"):
                return {
                    "full_name": "acme/parser",
                    "private": False,
                    "archived": False,
                    "disabled": False,
                    "language": "TypeScript",
                    "pushed_at": "2030-01-01T00:00:00Z",
                    "created_at": "2020-01-01T00:00:00Z",
                    "stargazers_count": 100,
                    "license": {"spdx_id": "MIT"},
                    "default_branch": "main",
                }
            if url.endswith("/issues/42"):
                return {
                    "state": "open",
                    "title": "Fix parser",
                    "body": (
                        "Steps to reproduce:\n1. Parse escaped input.\n"
                        "Expected behavior: delimiter remains escaped. " * 4
                    ),
                    "labels": [{"name": "bug"}],
                    "updated_at": "2030-01-01T00:00:00Z",
                }
            if "/git/trees/main" in url:
                return {
                    "truncated": False,
                    "tree": [
                        {"path": "tests/parser.test.ts"},
                        {"path": "CONTRIBUTING.md"},
                    ],
                }
            if "/timeline?" in url:
                return [
                    {
                        "event": "cross-referenced",
                        "will_close_target": True,
                        "source": {
                            "issue": {
                                "number": 81,
                                "state": "open",
                                "pull_request": {},
                            }
                        },
                    }
                ]
            if "/comments?" in url:
                return [
                    {
                        "body": "/claim",
                        "user": {"login": "solver-one"},
                    }
                ]
            raise AssertionError(f"unexpected URL: {url}")

        opportunity = Opportunity(
            source="algora",
            source_id="1",
            title="old title",
            source_url="https://algora.io/acme/bounties",
            bounty_usd=250,
            status="open",
            issue_url="https://github.com/acme/parser/issues/42",
        )
        enriched = enrich_opportunity(
            opportunity, GitHubClient(transport=transport)
        )
        self.assertTrue(enriched.public_repository)
        self.assertTrue(enriched.issue_open)
        self.assertTrue(enriched.issue_activity_recent)
        self.assertTrue(enriched.has_test_suite)
        self.assertTrue(enriched.contribution_guide_found)
        self.assertEqual(enriched.repository_stars, 100)
        self.assertEqual(enriched.repository_license, "MIT")
        self.assertEqual(
            enriched.repository_created_at, "2020-01-01T00:00:00Z"
        )
        self.assertTrue(enriched.acceptance_criteria_clear)
        self.assertEqual(enriched.competing_prs, 1)
        self.assertEqual(enriched.active_attempts, 1)
        self.assertEqual(enriched.estimated_agent_hours, 1.5)

    def test_broad_multi_component_issue_gets_conservative_scope_estimate(self):
        def transport(url, _headers):
            if url.endswith("/repos/acme/platform"):
                return {
                    "full_name": "acme/platform",
                    "private": False,
                    "archived": False,
                    "disabled": False,
                    "language": "Python",
                    "pushed_at": "2030-01-01T00:00:00Z",
                    "default_branch": None,
                }
            if url.endswith("/issues/9"):
                return {
                    "state": "open",
                    "title": "Build complete working sample",
                    "body": (
                        "Requirements: create a complete working sample with "
                        "a database, backend API, frontend UI, and integration."
                    ),
                    "labels": [{"name": "enhancement"}],
                    "updated_at": "2030-01-01T00:00:00Z",
                }
            if "/timeline?" in url or "/comments?" in url:
                return []
            raise AssertionError(f"unexpected URL: {url}")

        opportunity = Opportunity(
            source="algora",
            source_id="broad",
            title="old title",
            source_url="https://algora.io/acme/bounties",
            bounty_usd=250,
            status="open",
            issue_url="https://github.com/acme/platform/issues/9",
        )
        enrich_opportunity(opportunity, GitHubClient(transport=transport))
        self.assertGreater(opportunity.estimated_agent_hours, 3.0)
        self.assertTrue(opportunity.evidence["scope_estimate"]["signals"])

    def test_explicit_withdrawal_and_owner_ai_hostility_are_fail_closed(self):
        def transport(url, _headers):
            if url.endswith("/repos/acme/parser"):
                return {
                    "full_name": "acme/parser",
                    "private": False,
                    "archived": False,
                    "disabled": False,
                    "language": "Python",
                    "pushed_at": "2030-01-01T00:00:00Z",
                    "created_at": "2020-01-01T00:00:00Z",
                    "stargazers_count": 100,
                    "license": {"spdx_id": "MIT"},
                }
            if url.endswith("/issues/42"):
                return {
                    "state": "open",
                    "title": "Fix parser",
                    "body": "Steps to reproduce and expected behavior. " * 5,
                    "labels": [{"name": "bug"}],
                    "updated_at": "2030-01-01T00:00:00Z",
                }
            if "/timeline?" in url:
                return []
            if "/comments?" in url:
                return [
                    {
                        "body": "The bounty is no longer active; I will not honor it.",
                        "html_url": "https://github.com/acme/parser/issues/42#comment-1",
                        "user": {"login": "sponsor"},
                        "author_association": "NONE",
                    },
                    {
                        "body": "Untested AI slop will not be reviewed.",
                        "html_url": "https://github.com/acme/parser/issues/42#comment-2",
                        "user": {"login": "maintainer"},
                        "author_association": "OWNER",
                    },
                ]
            raise AssertionError(f"unexpected URL: {url}")

        opportunity = Opportunity(
            source="algora",
            source_id="withdrawn",
            title="old title",
            source_url="https://algora.io/acme/bounties",
            bounty_usd=250,
            status="open",
            issue_url="https://github.com/acme/parser/issues/42",
        )
        enriched = enrich_opportunity(
            opportunity, GitHubClient(transport=transport)
        )
        self.assertTrue(enriched.evidence["bounty_withdrawal_signal"])
        self.assertTrue(enriched.evidence["ai_contribution_reputation_risk"])
        self.assertNotIn(
            "body", enriched.evidence["bounty_withdrawal_provenance"][0]
        )

    def test_untrusted_comment_cannot_create_ai_policy_signal(self):
        from bounty_hunter.github import reputation_policy_signals

        signals = reputation_policy_signals(
            {"body": "Ordinary issue body"},
            [
                {
                    "body": "I think all AI slop should be banned.",
                    "user": {"login": "random"},
                    "author_association": "NONE",
                }
            ],
        )
        self.assertFalse(signals["ai_contribution_reputation_risk"])

    def test_manual_scope_estimate_is_not_overwritten(self):
        def transport(url, _headers):
            if url.endswith("/repos/acme/platform"):
                return {
                    "full_name": "acme/platform",
                    "private": False,
                    "archived": False,
                    "disabled": False,
                    "language": "Python",
                    "pushed_at": "2030-01-01T00:00:00Z",
                    "default_branch": None,
                }
            if url.endswith("/issues/10"):
                return {
                    "state": "open",
                    "title": "Complete working sample",
                    "body": "Requirements: complete working sample from scratch.",
                    "labels": [{"name": "enhancement"}],
                    "updated_at": "2030-01-01T00:00:00Z",
                }
            if "/timeline?" in url or "/comments?" in url:
                return []
            raise AssertionError(f"unexpected URL: {url}")

        opportunity = Opportunity(
            source="manual",
            source_id="manual-scope",
            title="old title",
            source_url="https://example.test",
            bounty_usd=250,
            status="open",
            issue_url="https://github.com/acme/platform/issues/10",
            estimated_agent_hours=2.25,
        )
        enrich_opportunity(opportunity, GitHubClient(transport=transport))
        self.assertEqual(opportunity.estimated_agent_hours, 2.25)

    def test_security_label_marks_issue_sensitive(self):
        def transport(url, _headers):
            if url.endswith("/repos/acme/parser"):
                return {
                    "full_name": "acme/parser",
                    "private": False,
                    "archived": False,
                    "disabled": False,
                    "language": "Python",
                    "pushed_at": "2030-01-01T00:00:00Z",
                    "default_branch": None,
                }
            if url.endswith("/issues/1"):
                return {
                    "state": "open",
                    "title": "Patch vulnerability",
                    "body": "",
                    "labels": [{"name": "security"}],
                    "updated_at": "2030-01-01T00:00:00Z",
                }
            if "/timeline?" in url or "/comments?" in url:
                return []
            raise AssertionError(f"unexpected URL: {url}")

        opportunity = Opportunity(
            source="manual",
            source_id="security",
            title="security",
            source_url="https://example.test",
            bounty_usd=500,
            status="open",
            issue_url="https://github.com/acme/parser/issues/1",
        )
        enrich_opportunity(opportunity, GitHubClient(transport=transport))
        self.assertTrue(opportunity.security_sensitive)


class GithubAuthReuseTests(unittest.TestCase):
    def test_explicit_environment_token_precedes_cli(self):
        with patch("bounty_hunter.github.shutil.which") as locate:
            with patch.dict("os.environ", {"GH_BOUNTY_TOKEN": "explicit-test-token"}):
                self.assertEqual(
                    resolve_github_token("GH_BOUNTY_TOKEN"), "explicit-test-token"
                )
            locate.assert_not_called()

    def test_reuses_cli_login_without_storing_or_printing_token(self):
        result = type("Completed", (), {
            "returncode": 0, "stdout": "github_pat_test12345678901234567890\n",
        })()
        with patch("bounty_hunter.github.shutil.which", return_value="gh"):
            with patch("bounty_hunter.github.subprocess.run", return_value=result) as run:
                with patch.dict("os.environ", {}, clear=True):
                    self.assertEqual(
                        resolve_github_token("GH_BOUNTY_TOKEN"),
                        "github_pat_test12345678901234567890",
                    )
                self.assertEqual(
                    run.call_args.args[0], ["gh", "auth", "token"]
                )
                self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_missing_cli_login_falls_back_to_anonymous(self):
        with patch.dict("os.environ", {}, clear=True):
            with patch("bounty_hunter.github.shutil.which", return_value=None):
                self.assertIsNone(resolve_github_token("GH_BOUNTY_TOKEN"))



if __name__ == "__main__":
    unittest.main()
