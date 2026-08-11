import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .history import History


PUBLIC_SCHEMA_VERSION = 1


def _source_name(item: Dict[str, Any]) -> str:
    name = str(item.get("source") or "unknown")
    organization = item.get("organization")
    return f"{name}/{organization}" if organization else name


def _public_source_health(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    public = []
    for item in items:
        public.append(
            {
                "name": _source_name(item),
                "status": str(item.get("status") or "unknown"),
                "opportunities": int(item.get("opportunities") or 0),
                "available_reported": item.get("available_reported"),
                "organizations_added": item.get("organizations_added"),
                "fallback_reason": item.get("fallback_reason"),
                "attempted": item.get("attempted"),
                "skipped": item.get("skipped"),
                "requests": item.get("requests"),
                "errors": item.get("errors"),
                "request_budget": item.get("request_budget"),
            }
        )
    return public


def _public_opportunity(record: Dict[str, Any]) -> Dict[str, Any]:
    opportunity = record.get("opportunity") or {}
    score = record.get("score") or {}
    return {
        "title": str(opportunity.get("title") or "Untitled opportunity"),
        "source": str(opportunity.get("source") or "unknown"),
        "source_url": opportunity.get("source_url"),
        "issue_url": opportunity.get("issue_url"),
        "repository": opportunity.get("repository"),
        "bounty_usd": float(opportunity.get("bounty_usd") or 0),
        "decision": str(score.get("decision") or "unknown"),
        "eligible": bool(score.get("eligible")),
        "confidence": float(score.get("confidence") or 0),
        "payout_probability": float(score.get("payout_probability") or 0),
        "expected_value_usd": float(score.get("expected_value_usd") or 0),
        "expected_hourly_return_usd": float(
            score.get("expected_hourly_return_usd") or 0
        ),
        "gate_rejection_count": int(score.get("gate_rejection_count") or 0),
        "rejection_reasons": [
            str(value) for value in score.get("rejection_reasons") or []
        ],
        "warnings": [str(value) for value in score.get("warnings") or []],
    }


def build_public_payload(
    report: Dict[str, Any],
    monitor_status: Dict[str, Any],
    history_summary: Dict[str, Any],
    candidate_limit: int = 8,
) -> Dict[str, Any]:
    records = report.get("opportunities") or []
    reason_counts = Counter(
        str(reason)
        for record in records
        for reason in (record.get("score") or {}).get("rejection_reasons") or []
    )
    eligible_count = sum(
        bool((record.get("score") or {}).get("eligible")) for record in records
    )
    source_health = _public_source_health(report.get("source_health") or [])
    source_counts = Counter(item["status"] for item in source_health)
    calibration = report.get("calibration") or {}
    pilot = history_summary.get("pilot_evaluation") or {}

    return {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_at": report.get("generated_at"),
        "safety": {
            "mode": "supervised",
            "read_only": True,
            "autonomous_claims": False,
            "autonomous_submissions": False,
            "message": "Every attempt and submission requires explicit human approval.",
        },
        "monitor": {
            "healthy": monitor_status.get("exit_code") == 0
            and int(monitor_status.get("consecutive_failures") or 0) == 0,
            "attention_required": bool(monitor_status.get("attention_required")),
            "started_at": monitor_status.get("started_at"),
            "finished_at": monitor_status.get("finished_at"),
            "next_scan_at": monitor_status.get("next_scan_at"),
            "consecutive_failures": int(
                monitor_status.get("consecutive_failures") or 0
            ),
        },
        "current_scan": {
            "evaluated": int(report.get("count") or 0),
            "eligible": eligible_count,
            "source_status_counts": dict(sorted(source_counts.items())),
        },
        "lifetime": {
            "scans": int(history_summary.get("scans") or 0),
            "opportunity_observations": int(
                history_summary.get("opportunities_observed") or 0
            ),
            "distinct_opportunities": int(
                history_summary.get("distinct_opportunities") or 0
            ),
            "attempts": int(history_summary.get("attempts") or 0),
            "wins": int(history_summary.get("wins") or 0),
            "costs_usd": float(history_summary.get("costs_usd") or 0),
            "payouts_usd": float(history_summary.get("payouts_usd") or 0),
            "human_hours": float(history_summary.get("human_hours") or 0),
            "net_income_usd": float(history_summary.get("net_income_usd") or 0),
        },
        "pilot": {
            "status": pilot.get("status") or "collecting_data",
            "terminal_attempts": int(pilot.get("terminal_attempts") or 0),
            "minimum_terminal_attempts": int(
                pilot.get("minimum_terminal_attempts") or 10
            ),
            "positive_net_income_proven": bool(
                pilot.get("positive_net_income_proven")
            ),
            "probability_factor": float(
                calibration.get("probability_factor") or 1
            ),
        },
        "rejection_reasons": [
            {"reason": reason, "count": count}
            for reason, count in reason_counts.most_common(10)
        ],
        "source_health": source_health,
        "opportunities": [
            _public_opportunity(record) for record in records[: max(0, candidate_limit)]
        ],
    }


def export_public_snapshot(
    report_path: str,
    status_path: str,
    history_path: str,
    output_path: str,
    candidate_limit: int = 8,
) -> Dict[str, Any]:
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    status = json.loads(Path(status_path).read_text(encoding="utf-8"))
    history = History(history_path)
    try:
        payload = build_public_payload(report, status, history.summary(), candidate_limit)
    finally:
        history.close()
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Export sanitized public telemetry.")
    parser.add_argument("--report", default="reports/opportunities.json")
    parser.add_argument("--status", default="data/monitor-status.json")
    parser.add_argument("--history-db", default="data/bounty_hunter.db")
    parser.add_argument("--output", default="public/telemetry.json")
    parser.add_argument("--candidate-limit", type=int, default=8)
    args = parser.parse_args()
    payload = export_public_snapshot(
        args.report, args.status, args.history_db, args.output, args.candidate_limit
    )
    print(
        f"exported public telemetry: {payload['current_scan']['evaluated']} evaluated, "
        f"{payload['current_scan']['eligible']} eligible"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
