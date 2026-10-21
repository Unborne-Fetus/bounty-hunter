"""Read-only accounting for the Windows earnings dashboard.

The database contains user-recorded payout events, not verified bank or wallet
settlement data. An offered bounty and a merged PR are NOT cash earnings.
"""
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


PROVIDERS = {
    "algora": ("Algora", "https://algora.io"),
    "bountypay": ("BountyPay", "https://bountypay.luccilabs.xyz/app"),
    "boss": ("Boss", "https://www.boss.dev/issues/open"),
    "opire": ("Opire", "https://app.opire.dev/home"),
    "taskbounty": ("TaskBounty", "https://www.task-bounty.com"),
}


def provider_info(source: str):
    """Return a trusted marketplace home page, never an untrusted issue URL."""
    return PROVIDERS.get(source.lower(), (source or "Other", None))


def _number(value) -> float:
    try:
        number = float(value or 0)
    except (ValueError, TypeError):
        return 0.0
    import math
    return number if math.isfinite(number) and number >= 0 else 0.0


def load_dashboard(database: str) -> dict:
    """Read an existing History SQLite DB without creating or modifying it."""
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"History database not found: {path}")
    # mode=ro is important: merely viewing the dashboard must not alter payouts.
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=4)
    connection.row_factory = sqlite3.Row
    try:
        attempts = connection.execute(
            """SELECT id, source, source_id, title, status, created_at, updated_at,
                      agent_cost_usd, compute_cost_usd, human_hours, payout_usd,
                      opportunity_json, pr_url
               FROM attempts ORDER BY updated_at DESC, id DESC"""
        ).fetchall()
    finally:
        connection.close()

    rows = []
    totals = defaultdict(float)
    by_provider = defaultdict(lambda: {"recorded": 0.0, "hours": 0.0, "count": 0})
    for row in attempts:
        item = dict(row)
        try:
            offer = json.loads(item.get("opportunity_json") or "{}")
            if not isinstance(offer, dict):
                offer = {}
        except (ValueError, TypeError):
            offer = {}
        paid = _number(item["payout_usd"])
        cost = _number(item["agent_cost_usd"]) + _number(item["compute_cost_usd"])
        hours = _number(item["human_hours"])
        offer_usd = _number(offer.get("bounty_usd"))
        # This is a potential reward, NOT a claim that the provider owes money.
        unconfirmed = max(offer_usd - paid, 0.0) if item["status"] == "merged" else 0.0
        item.update(
            recorded_usd=paid,
            direct_cost_usd=cost,
            hours=hours,
            offer_usd=offer_usd,
            unconfirmed_usd=unconfirmed,
            provider_name=provider_info(item["source"])[0],
        )
        rows.append(item)
        totals["recorded"] += paid
        totals["costs"] += cost
        totals["hours"] += hours
        totals["unconfirmed"] += unconfirmed
        totals["attempts"] += 1
        if item["status"] in {"merged", "paid"}:
            totals["completed"] += 1
        source = item["source"]
        by_provider[source]["recorded"] += paid
        by_provider[source]["hours"] += hours
        by_provider[source]["count"] += 1

    hours = totals["hours"]
    recorded = totals["recorded"]
    cash_net = recorded - totals["costs"]
    return {
        "database": str(path),
        "attempts": rows,
        "recorded_usd": round(recorded, 2),
        "direct_cost_usd": round(totals["costs"], 2),
        "cash_net_usd": round(cash_net, 2),
        "logged_hours": round(hours, 3),
        "gross_per_hour": round(recorded / hours, 2) if hours else None,
        "cash_net_per_hour": round(cash_net / hours, 2) if hours else None,
        "unconfirmed_usd": round(totals["unconfirmed"], 2),
        "completed_attempts": int(totals["completed"]),
        "attempt_count": int(totals["attempts"]),
        "providers": dict(by_provider),
    }


def default_database(search_roots, configured: Optional[str] = None) -> Optional[str]:
    """Prefer a previously selected DB, then an existing workspace database."""
    candidates = [configured] if configured else []
    for root in search_roots:
        if root:
            root = Path(root).resolve()
            candidates.extend([
                str(root / "data" / "bounty_hunter.db"),
                str(root.parent / "data" / "bounty_hunter.db"),
            ])
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    return None
