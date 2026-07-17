import json
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from urllib.request import Request, urlopen

from ..models import Opportunity


API_URL = "https://api.boss.dev/rpc/issues/gh/unsolved"
OPEN_URL = "https://www.boss.dev/issues/open"
DOC_URL = "https://www.boss.dev/doc/"
PRICING_URL = "https://www.boss.dev/doc/pricing"
SUPPORTED_PAYOUT_COUNTRIES = {
    "AT", "AU", "BE", "CA", "CH", "DE", "DK", "ES", "FI", "FR", "GB",
    "HK", "IE", "IT", "JP", "LU", "NL", "NO", "NZ", "PT", "SE", "SG",
    "US",
}


def fetch_inventory(timeout_seconds: int = 20) -> list:
    request = Request(
        API_URL,
        headers={"Accept": "application/json", "User-Agent": "bounty-hunter/0.1"},
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = json.load(response)
    if not isinstance(payload, list):
        raise ValueError("Boss open-bounty response is not a list")
    return payload


class BossClient:
    def __init__(
        self,
        cache_path: str = "data/boss-cache.json",
        cache_ttl_seconds: int = 6 * 60 * 60,
        transport: Optional[Callable[[int], list]] = None,
    ) -> None:
        self.cache_path = Path(cache_path).resolve()
        self.cache_ttl_seconds = cache_ttl_seconds
        self.transport = transport or fetch_inventory

    def fetch(self, timeout_seconds: int = 20) -> Tuple[list, bool]:
        now = time.time()
        if (
            self.cache_path.exists()
            and now - self.cache_path.stat().st_mtime <= self.cache_ttl_seconds
        ):
            return json.loads(self.cache_path.read_text(encoding="utf-8")), False
        try:
            payload = self.transport(timeout_seconds)
        except Exception:
            if self.cache_path.exists():
                return json.loads(
                    self.cache_path.read_text(encoding="utf-8")
                ), True
            raise
        if not isinstance(payload, list):
            raise ValueError("Boss open-bounty response is not a list")
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.cache_path)
        return payload, False


def normalize(
    payload: list,
    *,
    evidence_stale: bool = False,
    payout_country: Optional[str] = None,
) -> List[Opportunity]:
    country = payout_country.upper() if payout_country else None
    country_eligible = (
        country in SUPPORTED_PAYOUT_COUNTRIES if country is not None else None
    )
    opportunities: List[Opportunity] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        issue_url = str(item.get("url") or "")
        human_id = str(item.get("hId") or "")
        if not issue_url.startswith("https://github.com/") or "#" not in human_id:
            continue
        repository = human_id.rsplit("#", 1)[0]
        try:
            bounty_usd = float(item.get("usd") or 0)
        except (TypeError, ValueError):
            continue
        if bounty_usd <= 0:
            continue
        opportunities.append(
            Opportunity(
                source="boss",
                source_id=str(item.get("gid") or issue_url),
                title=str(item.get("title") or human_id),
                source_url=OPEN_URL,
                bounty_usd=bounty_usd,
                status=str(item.get("status") or "open"),
                repository=repository,
                issue_url=issue_url,
                platform_fee_rate=0.05,
                platform_fee_fixed_usd=0.60,
                payout_requires_sponsor_action=True,
                payout_country_eligible=country_eligible,
                notes=[
                    "Discovered through Boss's public open-bounty API.",
                    "Boss documents that auto bounties charge the sponsor's card "
                    "only after issue closure; public inventory does not prove "
                    "escrow or expose the bounty type, so the initial policy "
                    "rejects these opportunities.",
                ],
                evidence={
                    "boss_evidence_stale": evidence_stale,
                    "payment_model": "sponsor_card_charge_after_issue_closure",
                    "payment_assurance": "not_escrowed_by_public_evidence",
                    "payment_model_source": DOC_URL,
                    "pricing_source": PRICING_URL,
                    "payout_country_restrictions_apply": True,
                    "payout_country": country,
                    "supported_payout_countries": sorted(
                        SUPPORTED_PAYOUT_COUNTRIES
                    ),
                    "boss_currency_amounts": item.get("sByC") or {},
                    "boss_reported_usd": item.get("usd"),
                },
                raw=item,
            )
        )
    return opportunities
