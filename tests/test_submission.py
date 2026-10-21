import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bounty_hunter.history import History
from bounty_hunter.review import create_review_bundle
from bounty_hunter.submission import (
    _github_publication_guard,
    prepare_submission,
    publish_submission,
    submission_package_sha256,
    validate_marketplace_funding,
)


def git(repository, *arguments):
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class SponsorMarketplaceValidationTests(unittest.TestCase):
    def listing(self, source, amount=250, issue="42", country="US"):
        url = f"https://github.com/acme/parser/issues/{issue}"
        if source == "boss":
            payload = [{"gid": "bounty-1", "hId": "acme/parser#" + issue,
                        "usd": amount, "url": url, "status": "open"}]
            return payload
        flight = 'x:{"initialRewards":' + json.dumps([{
            "id": "bounty-1", "url": url,
            "pendingPrice": {"value": int(amount * 100), "unit": "USD_CENT"},
            "project": {"isPublic": True}}]) + '}'
        return '<script>self.__next_f.push([1,' + json.dumps(flight) + '])</script>'

    def check(self, source, payload, stale=False, country="US"):
        target = ("bounty_hunter.submission.boss.BossClient.fetch" if source == "boss"
                  else "bounty_hunter.submission.opire.OpireClient.fetch")
        row = {"source": source, "source_id": "bounty-1", "bounty_usd": 250,
               "issue_url": "https://github.com/acme/parser/issues/42",
               "evidence": {"payout_country": country}}
        with patch(target, return_value=(payload, stale)):
            return validate_marketplace_funding(row)

    def test_fresh_sponsor_listings_validate_without_claiming_escrow(self):
        for source in ("boss", "opire"):
            with self.subTest(source=source):
                result = self.check(source, self.listing(source))
                self.assertTrue(result["fresh"])
                self.assertTrue(result["payout_requires_sponsor_action"])
                self.assertIn("not_escrowed", result["payment_assurance"])

    def test_stale_changed_or_reduced_rewards_are_refused(self):
        for source in ("boss", "opire"):
            for amount, issue, stale in ((250, "42", True), (200, "42", False),
                                         (250, "43", False)):
                with self.subTest(source=source, amount=amount, issue=issue, stale=stale):
                    with self.assertRaises(ValueError):
                        self.check(source, self.listing(source, amount, issue), stale)

    def test_boss_still_requires_supported_payout_country(self):
        for country in (None, "DO"):
            with self.subTest(country=country):
                with self.assertRaisesRegex(ValueError, "payout-country"):
                    self.check("boss", self.listing("boss"), country=country)


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.repository = root / "source"
        self.repository.mkdir()
        git(self.repository, "init")
        git(self.repository, "config", "user.name", "Test")
        git(self.repository, "config", "user.email", "test@example.test")
        (self.repository / "logic.py").write_text(
            "def value():\n    return 1\n", encoding="utf-8"
        )
        git(self.repository, "add", "logic.py")
        git(self.repository, "commit", "-m", "base")
        self.base_commit = git(self.repository, "rev-parse", "HEAD")

        self.history = History(str(root / "history.db"))
        record = {
            "opportunity": {
                "source": "algora",
                "source_id": "bounty-one",
                "title": "Return the corrected value",
                "source_url": "https://algora.io/acme/bounties",
                "bounty_usd": 250,
                "status": "open",
                "repository": "acme/parser",
                "issue_url": "https://github.com/acme/parser/issues/42",
                "public_repository": True,
                "issue_open": True,
            },
            "score": {
                "eligible": True,
                "decision": "approve_for_human_review",
                "expected_value_usd": 100,
                "expected_hourly_return_usd": 50,
                "probability_valid": 0.9,
                "probability_verified": 0.8,
                "probability_win_merge": 0.7,
            },
        }
        self.attempt_id = self.history.queue_from_record(record)
        self.history.approve(
            self.attempt_id,
            "Approved implementation attempt.",
            self.history.attempt_record_sha256(self.attempt_id),
        )
        self.history.transition(self.attempt_id, "running")

        preflight = root / "preflight.json"
        preflight.write_text(
            json.dumps(
                {
                    "status": "passed",
                    "commit": self.base_commit,
                    "source_path": str(self.repository),
                }
            ),
            encoding="utf-8",
        )
        self.history.add_artifact(
            self.attempt_id,
            "preflight",
            str(preflight),
            hashlib.sha256(preflight.read_bytes()).hexdigest(),
        )

        (self.repository / "logic.py").write_text(
            "def value():\n    return 2\n", encoding="utf-8"
        )
        tests = self.repository / "tests"
        tests.mkdir()
        (tests / "test_logic.py").write_text(
            "from logic import value\n\ndef test_value():\n    assert value() == 2\n",
            encoding="utf-8",
        )
        bundle = create_review_bundle(
            self.history,
            self.attempt_id,
            str(self.repository),
            str(root / "reports"),
            verification_command=["python3", "-m", "unittest"],
            verification_exit_code=0,
            verification_stdout="passed",
            verification_stderr="",
            baseline_exit_code=1,
            baseline_stdout="failed before fix",
            baseline_stderr="",
        )
        self.bundle_sha256 = bundle["sha256"]
        self.history.approve_submission(
            self.attempt_id,
            "Exact final diff and disclosure reviewed.",
            self.bundle_sha256,
        )
        self.root = root

    def tearDown(self):
        self.history.close()
        self.temporary.cleanup()

    def test_prepare_creates_exact_hash_bound_commit(self):
        result = prepare_submission(
            self.history,
            self.attempt_id,
            str(self.root / "submissions"),
            str(self.root / "reports"),
        )
        self.assertEqual(
            self.history.get_attempt(self.attempt_id)["status"],
            "submission_approved",
        )
        self.assertEqual(
            submission_package_sha256(result["path"]), result["sha256"]
        )
        manifest = json.loads(
            (Path(result["path"]) / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            manifest["approved_bundle_sha256"], self.bundle_sha256
        )
        checkout = Path(manifest["checkout_path"])
        self.assertEqual(git(checkout, "status", "--short"), "")
        self.assertEqual(git(checkout, "rev-parse", "HEAD"), manifest["prepared_commit"])
        self.assertIn("AI assistance", (Path(result["path"]) / "PR_BODY.md").read_text())

    def test_publish_requires_exact_explicit_confirmation(self):
        package = prepare_submission(
            self.history,
            self.attempt_id,
            str(self.root / "submissions"),
            str(self.root / "reports"),
        )
        with self.assertRaisesRegex(ValueError, "exact confirmation"):
            publish_submission(
                self.history,
                self.attempt_id,
                self.bundle_sha256,
                package["sha256"],
                "yes",
            )
        self.assertEqual(
            self.history.get_attempt(self.attempt_id)["status"],
            "submission_approved",
        )

    def test_publish_records_pr_only_after_exact_commit_is_pushed(self):
        package = prepare_submission(
            self.history,
            self.attempt_id,
            str(self.root / "submissions"),
            str(self.root / "reports"),
        )
        observed = []

        def runner(arguments, **kwargs):
            observed.append(list(arguments))
            if arguments == ["gh", "api", "repos/acme/parser"]:
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    json.dumps(
                        {
                            "private": False,
                            "archived": False,
                            "disabled": False,
                            "pushed_at": "2030-01-01T00:00:00Z",
                        }
                    ),
                    "",
                )
            if arguments == ["gh", "api", "repos/acme/parser/issues/42"]:
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    json.dumps(
                        {
                            "state": "open",
                            "locked": False,
                            "updated_at": "2030-01-01T00:00:00Z",
                        }
                    ),
                    "",
                )
            if arguments[:3] == ["gh", "api", "repos/acme/parser/issues/42/comments"]:
                return subprocess.CompletedProcess(arguments, 0, "[[]]", "")
            if arguments[:3] == ["gh", "api", "repos/acme/parser/issues/42/timeline"]:
                return subprocess.CompletedProcess(arguments, 0, "[[]]", "")
            if arguments[:3] == ["gh", "api", "user"]:
                return subprocess.CompletedProcess(arguments, 0, "alice\n", "")
            if arguments[:4] == ["gh", "repo", "view", "alice/parser"]:
                return subprocess.CompletedProcess(arguments, 1, "", "missing")
            if arguments[:3] == ["gh", "repo", "fork"]:
                return subprocess.CompletedProcess(arguments, 0, "", "")
            if arguments[:4] == ["gh", "repo", "view", "acme/parser"]:
                return subprocess.CompletedProcess(arguments, 0, "main\n", "")
            if arguments[:3] == ["gh", "pr", "create"]:
                return subprocess.CompletedProcess(
                    arguments, 0, "https://github.com/acme/parser/pull/7\n", ""
                )
            if arguments[0] == "git" and "push" in arguments:
                return subprocess.CompletedProcess(arguments, 0, "", "")
            return subprocess.run(arguments, **kwargs)

        receipt = publish_submission(
            self.history,
            self.attempt_id,
            self.bundle_sha256,
            package["sha256"],
            f"publish:{self.attempt_id}:{self.bundle_sha256}:{package['sha256']}",
            runner=runner,
            marketplace_validator=lambda _opportunity: {
                "source": "algora",
                "fresh": True,
                "bounty_usd": 250,
            },
        )
        self.assertEqual(receipt["pr_url"], "https://github.com/acme/parser/pull/7")
        attempt = self.history.get_attempt(self.attempt_id)
        self.assertEqual(attempt["status"], "submitted")
        self.assertEqual(attempt["pr_url"], receipt["pr_url"])
        self.assertTrue(
            self.history.latest_artifact(self.attempt_id, "publication_receipt")
        )
        self.assertTrue(any(command[:3] == ["gh", "pr", "create"] for command in observed))

    def test_publish_rechecks_closed_issue_before_any_push(self):
        package = prepare_submission(
            self.history,
            self.attempt_id,
            str(self.root / "submissions"),
            str(self.root / "reports"),
        )
        observed = []

        def runner(arguments, **kwargs):
            observed.append(list(arguments))
            if arguments == ["gh", "api", "repos/acme/parser"]:
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    '{"private":false,"archived":false,"disabled":false}',
                    "",
                )
            if arguments == ["gh", "api", "repos/acme/parser/issues/42"]:
                return subprocess.CompletedProcess(
                    arguments, 0, '{"state":"closed","locked":false}', ""
                )
            return subprocess.run(arguments, **kwargs)

        with self.assertRaisesRegex(ValueError, "no longer open"):
            publish_submission(
                self.history,
                self.attempt_id,
                self.bundle_sha256,
                package["sha256"],
                f"publish:{self.attempt_id}:{self.bundle_sha256}:{package['sha256']}",
                runner=runner,
                marketplace_validator=lambda _opportunity: {
                    "source": "algora",
                    "fresh": True,
                    "bounty_usd": 250,
                },
            )
        self.assertFalse(
            any(command[0] == "git" and "push" in command for command in observed)
        )

    def test_algora_funding_guard_requires_fresh_open_board_row(self):
        html = """
        <table><tr><td>$250
        <a href="https://github.com/acme/parser/issues/42">parser#42 Fix</a>
        </td></tr></table>
        """
        opportunity = self.history.get_attempt_record(self.attempt_id)["opportunity"]
        with patch(
            "bounty_hunter.submission.algora.AlgoraBoardClient.fetch",
            return_value=(html, False),
        ):
            snapshot = validate_marketplace_funding(opportunity)
        self.assertTrue(snapshot["fresh"])
        self.assertEqual(snapshot["bounty_usd"], 250)

        with patch(
            "bounty_hunter.submission.algora.AlgoraBoardClient.fetch",
            return_value=(html, True),
        ):
            with self.assertRaisesRegex(ValueError, "stale Algora"):
                validate_marketplace_funding(opportunity)

    def test_bountypay_funding_guard_rejects_testnet_and_accepts_mainnet(self):
        opportunity = {
            "source": "bountypay",
            "source_id": "0xabc",
            "issue_url": "https://github.com/acme/parser/issues/42",
            "bounty_usd": 250,
        }
        base = {
            "bountyId": "0xabc",
            "repoFullName": "acme/parser",
            "issueNumber": 42,
            "amount": "250000000",
            "deadline": 4_102_444_800,
            "status": "open",
            "txHash": "0xtx",
            "tokenSymbol": "USDC",
            "token": "0xtoken",
        }
        with patch(
            "bounty_hunter.submission.bountypay.BountyPayClient.fetch",
            return_value=([base | {"network": "MEZO_TESTNET", "chainId": 31611}], False),
        ):
            with self.assertRaisesRegex(ValueError, "testnet BountyPay"):
                validate_marketplace_funding(opportunity)

        with patch(
            "bounty_hunter.submission.bountypay.BountyPayClient.fetch",
            return_value=([base | {"network": "BASE", "chainId": 8453}], False),
        ):
            snapshot = validate_marketplace_funding(opportunity)
        self.assertTrue(snapshot["fresh"])
        self.assertEqual(snapshot["chain_id"], 8453)
        self.assertEqual(snapshot["transaction_hash"], "0xtx")

    def test_github_guard_rejects_new_competing_pull_request(self):
        def runner(arguments, **_kwargs):
            if arguments == ["gh", "api", "repos/acme/parser"]:
                payload = {"private": False, "archived": False, "disabled": False}
            elif arguments == ["gh", "api", "repos/acme/parser/issues/42"]:
                payload = {"state": "open", "locked": False}
            elif arguments[:3] == [
                "gh",
                "api",
                "repos/acme/parser/issues/42/comments",
            ]:
                payload = [[]]
            else:
                payload = [
                    [
                        {
                            "event": "cross-referenced",
                            "will_close_target": True,
                            "source": {
                                "issue": {
                                    "state": "open",
                                    "html_url": "https://github.com/acme/parser/pull/9",
                                    "pull_request": {"url": "https://api.github.test/pulls/9"},
                                }
                            },
                        }
                    ]
                ]
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

        with self.assertRaisesRegex(ValueError, "competing pull request"):
            _github_publication_guard(
                "acme/parser",
                "https://github.com/acme/parser/issues/42",
                runner,
            )

    def test_github_guard_rejects_withdrawal_before_publication(self):
        def runner(arguments, **_kwargs):
            if arguments == ["gh", "api", "repos/acme/parser"]:
                payload = {"private": False, "archived": False, "disabled": False}
            elif arguments == ["gh", "api", "repos/acme/parser/issues/42"]:
                payload = {"state": "open", "locked": False, "body": ""}
            elif arguments[:3] == [
                "gh",
                "api",
                "repos/acme/parser/issues/42/comments",
            ]:
                payload = [[
                    {
                        "body": "I am no longer offering this bounty.",
                        "user": {"login": "sponsor"},
                        "author_association": "NONE",
                    }
                ]]
            else:
                payload = [[]]
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

        with self.assertRaisesRegex(ValueError, "withdrawn"):
            _github_publication_guard(
                "acme/parser",
                "https://github.com/acme/parser/issues/42",
                runner,
            )


if __name__ == "__main__":
    unittest.main()
