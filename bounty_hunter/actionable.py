"""Actionable-only discovery views and transparent zero-result diagnostics.

A bounty is *actionable* only when the existing scorer has approved it for
human review. This module never changes eligibility, payment guarantees, or
submission authority; preflight and human approval are still required.
"""
from collections import Counter
from typing import Any, Dict, Iterable, List, Tuple


def is_actionable(record: Dict[str, Any]) -> bool:
    """Require both an eligible score and explicit human-review readiness."""
    score = record.get("score") or {}
    return (
        score.get("eligible") is True
        and score.get("decision") == "approve_for_human_review"
    )


def actionable_indices(records: Iterable[Dict[str, Any]]) -> List[int]:
    """Keep indices in the *original* report so row selection is never remapped."""
    return [index for index, record in enumerate(records) if is_actionable(record)]


def actionability_summary(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarize recorded blockers without claiming an unverified bounty is safe.

    A reason is counted at most once per bounty. Only explicit *gate* reasons
    contribute to the top-blocker ranking: secondary expected-value metrics
    derive from the gates and otherwise obscure the actionable problem.
    """
    rows = list(records)
    ready = sum(is_actionable(row) for row in rows)
    pending = sum(
        (row.get("score") or {}).get("decision")
        in {"needs_enrichment", "needs_verification"}
        for row in rows
    )
    sources = Counter(
        str((row.get("opportunity") or {}).get("source") or "unknown")
        for row in rows
    )
    blockers = Counter()
    for row in rows:
        if is_actionable(row):
            continue
        score = row.get("score") or {}
        reasons = score.get("rejection_reasons") or []
        gate_count = score.get("gate_rejection_count", len(reasons))
        if not isinstance(gate_count, int):
            gate_count = len(reasons)
        primary = reasons[:max(0, gate_count)]
        # Earlier scan reports can have incomplete gate metadata.
        if not primary:
            primary = reasons
        blockers.update(set(str(reason) for reason in primary))

    return {
        "total": len(rows),
        "actionable": ready,
        "pending": pending,
        "rejected": len(rows) - ready - pending,
        "sources": sources.most_common(),
        "blockers": blockers.most_common(),
    }


def actionable_diagnostics(records: Iterable[Dict[str, Any]]) -> str:
    """User-facing explanation for an empty filter and next debugging step."""
    result = actionability_summary(records)
    if not result["total"]:
        return (
            "No marketplace listings were discovered in this scan.\n"
            "Check Source diagnostics, enable suitable sources in Setup, "
            "and try Find Algora boards. No eligibility decision was made."
        )
    if result["actionable"]:
        return (
            f'{result["actionable"]} of {result["total"]} discovered bounties '
            "passed automated screening.\n"
            "Review each requirement and marketplace eligibility, then run "
            "the isolated preflight before attempting any work. "
            "A listing is not a guaranteed payment."
        )

    lines = [
        f'0 actionable bounties out of {result["total"]} discovered.',
        f'{result["pending"]} need more verification; '
        f'{result["rejected"]} failed one or more screening checks.',
        "",
        "Most common primary blockers (one bounty may have several):",
    ]
    for reason, count in result["blockers"][:6]:
        lines.append(f"  • {count} × {reason}")
    if not result["blockers"]:
        lines.append("  • No detailed blocker information was recorded.")

    lines.extend([
        "",
        "Listings by source: " + ", ".join(
            f"{source} {count}" for source, count in result["sources"]
        ),
        "",
        "GitHub 'verified' means its linked issue was checked; it does not "
        "verify the payout, scope, tests, or platform eligibility.",
        "Use Show all bounties to inspect rejects and Source diagnostics to "
        "check missing inventories. If the same gates block everything, "
        "review those specific scanner rules rather than marking unsafe "
        "bounties as eligible.",
    ])
    return "\n".join(lines)
