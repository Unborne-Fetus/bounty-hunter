import tempfile
import unittest

from bounty_hunter.history import History, pilot_evaluation


RECORD = {
    "opportunity": {
        "source": "manual",
        "source_id": "one",
        "title": "Fix parser",
        "bounty_usd": 250,
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


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.history = History(f"{self.temporary.name}/history.db")

    def tearDown(self):
        self.history.close()
        self.temporary.cleanup()

    def test_records_scan_and_summary(self):
        scan_id = self.history.record_scan([RECORD])
        self.assertGreater(scan_id, 0)
        summary = self.history.summary()
        self.assertEqual(summary["scans"], 1)
        self.assertEqual(summary["opportunities_observed"], 1)
        self.assertEqual(summary["eligible_observations"], 1)
        self.assertEqual(
            summary["outcome_calibration"]["probability_factor"], 1.0
        )
        self.assertEqual(
            summary["pilot_evaluation"]["status"], "collecting_data"
        )
        self.assertEqual(
            summary["pilot_evaluation"]["remaining_terminal_attempts"], 10
        )
        latest = self.history.latest_opportunity_record("manual", "one")
        self.assertEqual(latest["opportunity"]["title"], "Fix parser")
        self.assertIsNotNone(latest["historical_scan_at"])
        self.assertIsNone(
            self.history.latest_github_enriched_record("manual", "one")
        )

    def test_active_attempt_returns_only_unfinished_work(self):
        self.assertIsNone(self.history.active_attempt())
        attempt_id = self.history.queue_from_record(RECORD)
        self.assertEqual(self.history.active_attempt()["id"], attempt_id)
        self.history.transition(attempt_id, "abandoned", outcome_note="Not viable.")
        self.assertIsNone(self.history.active_attempt())
        self.assertEqual(
            self.history.outcome_calibration()["terminal_outcomes"], 0
        )

    def test_terminal_outcome_recalibrates_future_probabilities(self):
        initial = self.history.outcome_calibration()
        self.assertEqual(initial["terminal_outcomes"], 0)
        self.assertEqual(initial["probability_factor"], 1.0)
        self.assertEqual(initial["default_execution_cost_usd"], 8.0)
        attempt_id = self.history.queue_from_record(RECORD)
        self.history.transition(
            attempt_id,
            "abandoned",
            agent_cost_usd=4.0,
            human_hours=0.2,
            outcome_note="Preflight could not reproduce the issue.",
        )
        calibrated = self.history.outcome_calibration()
        self.assertEqual(calibrated["terminal_outcomes"], 1)
        self.assertEqual(calibrated["wins"], 0)
        self.assertLess(calibrated["probability_factor"], 1.0)
        self.assertEqual(calibrated["cost_samples"], 1)
        self.assertLess(calibrated["default_execution_cost_usd"], 8.0)

    def test_distinct_count_uses_canonical_issue_url(self):
        first = {
            "opportunity": {
                **RECORD["opportunity"],
                "source": "algora",
                "source_id": "individual-bounty",
                "issue_url": "https://github.com/acme/parser/issues/42",
            },
            "score": dict(RECORD["score"]),
        }
        pooled = {
            "opportunity": {
                **first["opportunity"],
                "source_id": "pool:acme:acme/parser#42",
                "bounty_usd": 350,
            },
            "score": dict(RECORD["score"]),
        }
        self.history.record_scan([first])
        self.history.record_scan([pooled])
        summary = self.history.summary()
        self.assertEqual(summary["opportunities_observed"], 2)
        self.assertEqual(summary["distinct_opportunities"], 1)

    def test_running_requires_explicit_approval(self):
        attempt_id = self.history.queue_from_record(RECORD)
        queued_record = self.history.get_attempt_record(attempt_id)
        self.assertEqual(queued_record["opportunity"]["title"], "Fix parser")
        RECORD["opportunity"]["title"] = "Mutated outside ledger"
        self.assertEqual(
            self.history.get_attempt_record(attempt_id)["opportunity"]["title"],
            "Fix parser",
        )
        RECORD["opportunity"]["title"] = "Fix parser"
        with self.assertRaisesRegex(ValueError, "queued -> running"):
            self.history.transition(attempt_id, "running")
        digest = self.history.attempt_record_sha256(attempt_id)
        with self.assertRaisesRegex(ValueError, "wrong opportunity record"):
            self.history.approve(
                attempt_id, "Diff and approach approved.", "0" * 64
            )
        self.assertEqual(self.history.get_attempt(attempt_id)["status"], "queued")
        self.history.approve(
            attempt_id, "Diff and approach approved.", digest
        )
        self.assertEqual(
            self.history.get_attempt(attempt_id)["approved_record_sha256"],
            digest,
        )
        self.history.transition(attempt_id, "running")
        self.assertEqual(
            self.history.get_attempt(attempt_id)["status"], "running"
        )

    def test_outcome_accounting(self):
        attempt_id = self.history.queue_from_record(RECORD)
        self.history.approve(
            attempt_id,
            "Approved.",
            self.history.attempt_record_sha256(attempt_id),
        )
        self.history.transition(
            attempt_id,
            "running",
            agent_cost_usd=6,
            compute_cost_usd=2,
            human_hours=0.25,
        )
        self.history.transition(attempt_id, "verified")
        with self.assertRaisesRegex(ValueError, "verified -> submitted"):
            self.history.transition(attempt_id, "submitted")
        self.history.add_artifact(
            attempt_id,
            "review_bundle",
            "/tmp/review",
            "abc123",
        )
        self.history.approve_submission(
            attempt_id, "Final diff approved.", "abc123"
        )
        self.history.transition(
            attempt_id,
            "submitted",
            pr_url="https://github.com/acme/parser/pull/1",
        )
        self.history.transition(attempt_id, "merged")
        self.history.transition(attempt_id, "paid", payout_usd=200)
        summary = self.history.summary()
        self.assertEqual(summary["wins"], 1)
        self.assertEqual(summary["attempt_events"], 4)
        self.assertEqual(summary["state_events"], 8)
        self.assertEqual(summary["cash_net_income_usd"], 192)
        self.assertEqual(summary["valued_human_cost_usd"], 6.25)
        self.assertEqual(summary["net_income_usd"], 185.75)
        self.assertFalse(
            summary["pilot_evaluation"]["positive_net_income_proven"]
        )
        self.assertEqual(
            summary["pilot_evaluation"]["status"], "collecting_data"
        )

    def test_pilot_evaluation_requires_sample_and_economic_profit(self):
        early = pilot_evaluation(
            terminal_attempts=1,
            wins=1,
            direct_costs_usd=8,
            human_hours=0.25,
            payouts_usd=100,
        )
        self.assertEqual(early["status"], "collecting_data")
        self.assertFalse(early["positive_net_income_proven"])
        positive = pilot_evaluation(
            terminal_attempts=10,
            wins=4,
            direct_costs_usd=80,
            human_hours=2,
            payouts_usd=300,
        )
        self.assertEqual(positive["status"], "positive_net_income")
        self.assertTrue(positive["positive_net_income_proven"])
        self.assertEqual(positive["observed_win_rate"], 0.4)
        self.assertGreater(positive["economic_roi"], 0)
        negative = pilot_evaluation(
            terminal_attempts=10,
            wins=1,
            direct_costs_usd=100,
            human_hours=4,
            payouts_usd=150,
        )
        self.assertEqual(negative["status"], "negative_net_income")
        self.assertGreater(negative["break_even_payout_remaining_usd"], 0)

    def test_state_history_and_usage_metrics_are_immutable(self):
        attempt_id = self.history.queue_from_record(RECORD)
        self.history.approve(
            attempt_id,
            "Approved after issue review.",
            self.history.attempt_record_sha256(attempt_id),
        )
        self.history.transition(attempt_id, "running")
        states = self.history.state_events(attempt_id)
        self.assertEqual(
            [state["to_status"] for state in states],
            ["queued", "approved", "running"],
        )
        self.history.record_metric(
            attempt_id,
            "agent_input_tokens",
            1200,
            "Context patch generation.",
        )
        self.history.record_metric(
            attempt_id,
            "agent_runs",
            1,
            "Context patch generation.",
        )
        metrics = self.history.attempt_metrics(attempt_id)
        self.assertEqual(len(metrics), 2)
        summary = self.history.summary()
        self.assertEqual(summary["agent_usage"]["runs"], 1)
        self.assertEqual(summary["agent_usage"]["input_tokens"], 1200)

    def test_revision_costs_accumulate_as_immutable_events(self):
        attempt_id = self.history.queue_from_record(RECORD)
        self.history.approve(
            attempt_id,
            "Approved.",
            self.history.attempt_record_sha256(attempt_id),
        )
        self.history.transition(
            attempt_id,
            "running",
            agent_cost_usd=4,
            human_hours=0.1,
        )
        self.history.transition(attempt_id, "verified")
        self.history.add_artifact(
            attempt_id, "review_bundle", "/tmp/review", "bundle-one"
        )
        self.history.approve_submission(
            attempt_id, "Reviewed.", "bundle-one"
        )
        self.history.transition(
            attempt_id,
            "submitted",
            pr_url="https://github.com/acme/parser/pull/1",
        )
        self.history.transition(attempt_id, "revision")
        self.history.transition(
            attempt_id,
            "running",
            agent_cost_usd=3,
            compute_cost_usd=1,
            human_hours=0.2,
            outcome_note="Reviewer-requested revision.",
        )
        attempt = self.history.get_attempt(attempt_id)
        self.assertEqual(attempt["agent_cost_usd"], 7)
        self.assertEqual(attempt["compute_cost_usd"], 1)
        self.assertAlmostEqual(attempt["human_hours"], 0.3)
        events = self.history.attempt_events(attempt_id)
        self.assertEqual(len(events), 5)
        self.assertEqual(
            [
                event["amount"]
                for event in events
                if event["kind"] == "agent_cost_usd"
            ],
            [4, 3],
        )

    def test_event_validation_and_payout_gate(self):
        attempt_id = self.history.queue_from_record(RECORD)
        with self.assertRaisesRegex(ValueError, "positive"):
            self.history.record_event(
                attempt_id, "agent_cost_usd", -1, "Invalid."
            )
        with self.assertRaisesRegex(ValueError, "after merge"):
            self.history.record_event(
                attempt_id, "payout_usd", 100, "Too early."
            )


if __name__ == "__main__":
    unittest.main()
