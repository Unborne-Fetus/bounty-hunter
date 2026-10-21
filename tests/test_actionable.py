"""Regression tests for the strict actionable-only bounty view."""
import unittest

from bounty_hunter.actionable import (
    actionable_diagnostics, actionable_indices, actionability_summary,
    is_actionable,
)


def sample(source, source_id, decision, eligible, *reasons):
    return {
        "opportunity": {"source": source, "source_id": source_id, "title": source_id},
        "score": {
            "decision": decision,
            "eligible": eligible,
            "rejection_reasons": list(reasons),
            "gate_rejection_count": len(reasons),
        },
    }


class ActionableFilterTests(unittest.TestCase):
    def test_only_fully_approved_records_are_actionable(self):
        records = [
            sample("opire", "a", "reject", False, "payout requires sponsor action"),
            sample("algora", "b", "approve_for_human_review", True),
            sample("taskbounty", "c", "needs_verification", False, "issue unknown"),
            sample("algora", "d", "needs_enrichment", True),
            sample("algora", "e", "approve_for_human_review", False),
            sample("algora", "f", "approve_for_human_review", True),
        ]
        self.assertEqual(actionable_indices(records), [1, 5])
        self.assertTrue(is_actionable(records[1]))
        self.assertFalse(is_actionable(records[3]))
        result = actionability_summary(records)
        self.assertEqual(result["total"], 6)
        self.assertEqual(result["actionable"], 2)
        self.assertEqual(result["pending"], 2)
        self.assertEqual(result["rejected"], 2)

    def test_zero_matches_explains_primary_gates_and_marketplaces(self):
        records = [
            sample("opire", "1", "reject", False, "payout requires sponsor action"),
            sample("opire", "2", "reject", False, "payout requires sponsor action"),
            sample("boss", "3", "reject", False, "issue has no test suite"),
        ]
        summary = actionability_summary(records)
        self.assertEqual(summary["actionable"], 0)
        self.assertEqual(
            summary["blockers"][0],
            ("payout requires sponsor action", 2),
        )
        report = actionable_diagnostics(records)
        self.assertIn("0 actionable bounties out of 3 discovered", report)
        self.assertIn("2 × payout requires sponsor action", report)
        self.assertIn("opire 2", report)
        self.assertIn("boss 1", report)
        self.assertIn("GitHub 'verified'", report)

    def test_only_primary_gates_count_not_secondary_expected_value(self):
        record = sample("opire", "1", "reject", False, "no escrow")
        record["score"]["rejection_reasons"] += [
            "estimated payout probability is below threshold",
            "expected value is below threshold",
        ]
        summary = actionability_summary([record])
        self.assertEqual(summary["blockers"], [("no escrow", 1)])

    def test_zero_matches_explains_unavailable_paid_sources(self):
        rows = [
            sample("boss", "1", "reject", False, "payout requires sponsor action"),
            sample("opire", "2", "reject", False, "payout requires sponsor action"),
        ]
        health = [
            {"source": "algora_auto_discovery", "status": "error",
             "message": "Algora board search failed."},
            {"source": "bountypay", "status": "fresh", "opportunities": 0},
            {"source": "boss", "status": "fresh", "opportunities": 1},
            {"source": "opire", "status": "fresh", "opportunities": 1},
        ]
        diagnosis = actionable_diagnostics(rows, health)
        self.assertIn("algora_auto_discovery: error", diagnosis)
        self.assertIn("Algora board search failed", diagnosis)
        self.assertIn("bountypay: fresh", diagnosis)
        self.assertIn("0 actionable bounties", diagnosis)

    def test_no_inventory_is_not_misreported_as_rejection(self):
        self.assertEqual(actionability_summary([])["total"], 0)
        self.assertIn("No marketplace listings were discovered", actionable_diagnostics([]))


if __name__ == "__main__":
    unittest.main()
