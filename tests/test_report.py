import json
import tempfile
import unittest
from pathlib import Path

from bounty_hunter.report import _ranking_key, write_json, write_markdown


class ReportTests(unittest.TestCase):
    def test_rejected_ranking_prefers_nonzero_probability_before_gate_count(self):
        base = {
            "eligible": False,
            "priority_score": 0,
            "expected_value_usd": -20,
            "confidence": 0.8,
            "rejection_reasons": [],
        }
        closed = {
            "score": {
                **base,
                "payout_probability": 0,
                "gate_rejection_count": 1,
            }
        }
        active_near_miss = {
            "score": {
                **base,
                "payout_probability": 0.1,
                "gate_rejection_count": 3,
            }
        }
        self.assertGreater(
            _ranking_key(active_near_miss), _ranking_key(closed)
        )

    def test_source_health_is_written_to_both_reports(self):
        health = [
            {
                "source": "algora",
                "organization": "acme",
                "status": "error",
                "error": "HTTP 429",
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            json_path = Path(temporary) / "report.json"
            markdown_path = Path(temporary) / "report.md"
            write_json([], str(json_path), source_health=health)
            write_markdown([], str(markdown_path), source_health=health)
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            markdown = markdown_path.read_text(encoding="utf-8")
        self.assertEqual(payload["source_health"], health)
        self.assertIn("`algora/acme`: **error**", markdown)
        self.assertIn("HTTP 429", markdown)

    def test_partial_inventory_and_github_request_details_are_visible(self):
        health = [
            {
                "source": "opire",
                "status": "partial",
                "opportunities": 30,
                "available_reported": 290,
            },
            {
                "source": "github",
                "status": "partial",
                "attempted": 16,
                "skipped": 61,
                "requests": 3,
                "request_budget": 45,
                "errors": 3,
                "rate_limited_until": "2030-01-01T00:00:00+00:00",
            },
            {
                "source": "algora_discovery_report",
                "status": "fresh",
                "organizations_added": 7,
            },
            {
                "source": "algora",
                "organization": "deferred",
                "status": "stale",
                "opportunities": 2,
                "fallback_reason": "request_budget_exhausted",
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.md"
            write_markdown([], str(path), source_health=health)
            markdown = path.read_text(encoding="utf-8")
        self.assertIn("fetched 30 of 290 reported opportunities", markdown)
        self.assertIn("16 attempted, 61 skipped, 3 requests, 3 errors", markdown)
        self.assertIn("budget 45", markdown)
        self.assertIn("cooldown until 2030-01-01T00:00:00+00:00", markdown)
        self.assertIn("7 scan-only organizations added", markdown)
        self.assertIn("fallback: request_budget_exhausted", markdown)

    def test_outcome_calibration_is_written_to_reports(self):
        calibration = {
            "terminal_outcomes": 4,
            "wins": 1,
            "probability_factor": 0.8,
            "cost_samples": 4,
            "default_execution_cost_usd": 7.25,
            "default_human_hours": 0.3,
        }
        with tempfile.TemporaryDirectory() as temporary:
            json_path = Path(temporary) / "report.json"
            markdown_path = Path(temporary) / "report.md"
            write_json([], str(json_path), calibration=calibration)
            write_markdown([], str(markdown_path), calibration=calibration)
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            markdown = markdown_path.read_text(encoding="utf-8")
        self.assertEqual(payload["calibration"], calibration)
        self.assertIn("Terminal outcomes: 4", markdown)
        self.assertIn("Probability factor: 0.80", markdown)
        self.assertIn("Blended execution-cost default: $7.25", markdown)

    def test_repository_trust_evidence_is_visible(self):
        record = {
            "opportunity": {
                "title": "Fix parser",
                "source": "algora",
                "source_url": "https://algora.io/acme/bounties",
                "bounty_usd": 100.0,
                "status": "open",
                "repository": "acme/parser",
                "repository_stars": 125,
                "repository_created_at": "2020-01-01T00:00:00Z",
                "repository_license": "MIT",
                "contribution_guide_found": True,
            },
            "score": {
                "decision": "reject",
                "confidence": 0.8,
                "probability_valid": 0.8,
                "probability_verified": 0.7,
                "probability_win_merge": 0.6,
                "expected_value_usd": 10.0,
                "expected_hourly_return_usd": 5.0,
                "rejection_reasons": ["example"],
                "warnings": [],
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.md"
            write_markdown([record], str(path))
            markdown = path.read_text(encoding="utf-8")
        self.assertIn(
            "Repository trust: stars 125; created 2020-01-01T00:00:00Z; "
            "license MIT; contribution guidance yes",
            markdown,
        )
        self.assertIn("Primary gate blockers: 1", markdown)


if __name__ == "__main__":
    unittest.main()
