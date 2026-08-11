import unittest

from bounty_hunter.public_export import build_public_payload


class PublicExportTests(unittest.TestCase):
    def test_export_is_allowlisted_and_excludes_operational_details(self):
        report = {
            "generated_at": "2026-08-11T00:00:00+00:00",
            "count": 1,
            "source_health": [
                {
                    "source": "algora",
                    "organization": "acme",
                    "status": "stale",
                    "opportunities": 1,
                    "fallback_reason": "request_budget_exhausted",
                    "error": "secret raw error",
                }
            ],
            "calibration": {"probability_factor": 0.8},
            "opportunities": [
                {
                    "opportunity": {
                        "title": "Fix parser",
                        "source": "algora",
                        "source_id": "private-id",
                        "source_url": "https://example.com/bounty",
                        "issue_url": "https://github.com/acme/parser/issues/1",
                        "repository": "acme/parser",
                        "bounty_usd": 100,
                        "evidence": {"private": "do not publish"},
                        "notes": ["internal note"],
                    },
                    "score": {
                        "decision": "reject",
                        "eligible": False,
                        "confidence": 0.8,
                        "payout_probability": 0.1,
                        "expected_value_usd": -5,
                        "expected_hourly_return_usd": -2,
                        "gate_rejection_count": 1,
                        "rejection_reasons": ["missing test suite"],
                        "warnings": ["deadline unknown"],
                        "private_score": "do not publish",
                    },
                }
            ],
        }
        status = {
            "exit_code": 0,
            "consecutive_failures": 0,
            "attention_required": False,
            "finished_at": "2026-08-11T00:00:00+00:00",
            "stderr": "secret raw stderr",
            "stdout": "secret raw stdout",
            "queued_attempt": {"approval_command": "never publish this"},
        }
        summary = {
            "scans": 4,
            "opportunities_observed": 40,
            "distinct_opportunities": 12,
            "attempts": 0,
            "pilot_evaluation": {
                "status": "collecting_data",
                "terminal_attempts": 0,
                "minimum_terminal_attempts": 10,
            },
        }

        payload = build_public_payload(report, status, summary)
        serialized = repr(payload)

        self.assertEqual(payload["source_health"][0]["name"], "algora/acme")
        self.assertEqual(payload["rejection_reasons"][0]["count"], 1)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("approval_command", serialized)
        self.assertNotIn("source_id", serialized)
        self.assertNotIn("evidence", serialized)
        self.assertNotIn("private_score", serialized)

    def test_candidate_limit_and_eligibility_are_reported(self):
        records = []
        for index in range(3):
            records.append(
                {
                    "opportunity": {"title": str(index), "bounty_usd": 10},
                    "score": {"eligible": index == 0},
                }
            )
        payload = build_public_payload(
            {"count": 3, "opportunities": records},
            {"exit_code": 1, "consecutive_failures": 1},
            {},
            candidate_limit=2,
        )
        self.assertEqual(payload["current_scan"]["eligible"], 1)
        self.assertEqual(len(payload["opportunities"]), 2)
        self.assertFalse(payload["monitor"]["healthy"])


if __name__ == "__main__":
    unittest.main()
