import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

from .config import ScoringConfig
from .models import Opportunity
from .scoring import score_opportunity


def _ranking_key(record: Dict[str, Any]):
    score = record["score"]
    return (
        score["eligible"],
        score["priority_score"],
        score.get("payout_probability", 0),
        -score.get(
            "gate_rejection_count",
            len(score.get("rejection_reasons") or []),
        ),
        score["expected_value_usd"],
        score["confidence"],
    )


def ranked_records(
    opportunities: Iterable[Opportunity],
    config: ScoringConfig = ScoringConfig(),
    calibration_factor: float = 1.0,
) -> List[Dict[str, Any]]:
    records = [
        {
            "opportunity": opportunity.to_dict(),
            "score": score_opportunity(
                opportunity, config, calibration_factor
            ).to_dict(),
        }
        for opportunity in opportunities
    ]
    return sorted(
        records,
        key=_ranking_key,
        reverse=True,
    )


def write_json(
    records: List[Dict[str, Any]],
    path: str,
    source_health: List[Dict[str, Any]] = None,
    calibration: Dict[str, Any] = None,
) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(records),
        "source_health": source_health or [],
        "calibration": calibration or {},
        "opportunities": records,
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def write_markdown(
    records: List[Dict[str, Any]],
    path: str,
    source_health: List[Dict[str, Any]] = None,
    calibration: Dict[str, Any] = None,
) -> None:
    lines = [
        "# Bounty opportunity report",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        f"Opportunities evaluated: {len(records)}",
        "",
    ]
    if source_health:
        lines.extend(["## Source health", ""])
        for item in source_health:
            name = item["source"]
            if item.get("organization"):
                name += f"/{item['organization']}"
            detail = f"{item.get('opportunities', 0)} opportunities"
            if item.get("error"):
                detail = item["error"]
            elif name == "algora_discovery_report":
                detail = (
                    f"{item.get('organizations_added', 0)} scan-only "
                    "organizations added"
                )
            elif item.get("available_reported") is not None:
                detail = (
                    f"fetched {item.get('opportunities', 0)} of "
                    f"{item['available_reported']} reported opportunities"
                )
            elif name == "github":
                detail = (
                    f"{item.get('attempted', 0)} attempted, "
                    f"{item.get('skipped', 0)} skipped, "
                    f"{item.get('requests', 0)} requests, "
                    f"{item.get('errors', 0)} errors"
                )
                if item.get("request_budget") is not None:
                    detail += f", budget {item['request_budget']}"
                if item.get("rate_limited_until"):
                    detail += f", cooldown until {item['rate_limited_until']}"
            if item.get("fallback_reason"):
                detail += f", fallback: {item['fallback_reason']}"
            lines.append(f"- `{name}`: **{item['status']}** — {detail}")
        lines.append("")
    if calibration:
        lines.extend(
            [
                "## Outcome calibration",
                "",
                f"- Terminal outcomes: {calibration['terminal_outcomes']}",
                f"- Accepted/merged outcomes: {calibration['wins']}",
                f"- Probability factor: {calibration['probability_factor']:.2f}",
                f"- Cost samples: {calibration.get('cost_samples', 0)}",
                f"- Blended execution-cost default: "
                f"${calibration.get('default_execution_cost_usd', 0):.2f}",
                f"- Blended human-time default: "
                f"{calibration.get('default_human_hours', 0):.2f} hours",
                "",
            ]
        )
    if not records:
        lines.extend(
            [
                "No opportunities were returned by the configured sources.",
                "",
            ]
        )
    for index, record in enumerate(records, 1):
        item = record["opportunity"]
        score = record["score"]
        lines.extend(
            [
                f"## {index}. {item['title']}",
                "",
                f"- Source: [{item['source']}]({item['source_url']})",
                f"- Bounty: ${item['bounty_usd']:.2f}",
                f"- Status: {item['status']}",
                f"- Repository: {item.get('repository') or 'unknown'}",
                f"- Repository trust: stars "
                f"{item.get('repository_stars') if item.get('repository_stars') is not None else 'unknown'}; "
                f"created {item.get('repository_created_at') or 'unknown'}; "
                f"license {item.get('repository_license') or 'unknown'}; "
                f"contribution guidance "
                f"{('yes' if item.get('contribution_guide_found') is True else ('no' if item.get('contribution_guide_found') is False else 'unknown'))}",
                f"- Decision: `{score['decision']}`",
                f"- Primary gate blockers: "
                f"{score.get('gate_rejection_count', len(score.get('rejection_reasons') or []))}",
                f"- Confidence: {score['confidence']:.0%}",
                f"- Estimated payout probability: "
                f"{score.get('payout_probability', score['probability_valid'] * score['probability_verified'] * score['probability_win_merge']):.1%}",
                f"- Expected value: ${score['expected_value_usd']:.2f}",
                f"- Expected hourly return: "
                f"${score['expected_hourly_return_usd']:.2f}/hour",
                "",
            ]
        )
        if score["rejection_reasons"]:
            lines.append(
                "**Rejection reasons:** "
                + "; ".join(score["rejection_reasons"])
            )
            lines.append("")
        if score["warnings"]:
            lines.append("**Unknowns/warnings:** " + "; ".join(score["warnings"]))
            lines.append("")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
