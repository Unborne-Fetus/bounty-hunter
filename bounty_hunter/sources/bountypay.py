import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..models import Opportunity


API_URL = "https://bountypay.luccilabs.xyz/api/bounties/open"
OPEN_URL = "https://bountypay.luccilabs.xyz/app"
DOC_URL = "https://github.com/lucci-xyz/bounty"
MAINNET_CHAIN_IDS = {8453}
TOKEN_DECIMALS = {"USDC": 6, "MUSD": 18}


def fetch_inventory(timeout_seconds: int = 20) -> list:
    """Fetch open bounties; distinguish provider HTTP failures from local errors.

    urllib's HTTPError is also a URLError. Do not retry an HTTP 500 using
    another transport: it is a server response, not a TLS fallback condition.
    """
    request = Request(
        API_URL,
        headers={"Accept": "application/json", "User-Agent": "bounty-hunter/0.1"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.load(response)
    except HTTPError as error:
        category = "server error" if 500 <= error.code < 600 else "HTTP error"
        raise RuntimeError(
            f"BountyPay API GET failed: HTTP {error.code} ({category}). "
            "The provider endpoint did not return an open-bounty inventory. "
            "Other bounty sources can still be scanned."
        ) from error
    except URLError as error:
        # Preserve the existing curl fallback for Windows TLS/network differences.
        curl = shutil.which("curl")
        if not curl:
            raise
        result = subprocess.run(
            [
                curl,
                "-fsSL",
                "--max-time",
                str(timeout_seconds),
                "--user-agent",
                "bounty-hunter/0.1",
                "--write-out",
                "__BH_HTTP_STATUS__:%{http_code}",
                API_URL,
            ],
            check=False,
            capture_output=True,
            timeout=timeout_seconds + 5,
        )
        body, separator, status_text = result.stdout.decode(
            "utf-8", errors="replace"
        ).rpartition("__BH_HTTP_STATUS__:")
        status = status_text.strip() if separator else None
        if result.returncode:
            if result.returncode == 22 and status and status.isdigit() and int(status) >= 400:
                raise RuntimeError(
                    f"BountyPay API GET failed: HTTP {status} "
                    f"(curl exit 22). Other bounty sources can still be scanned."
                ) from error
            raise RuntimeError(
                f"BountyPay API GET transport failed (curl exit "
                f"{result.returncode}); the provider's HTTP status could not "
                "be confirmed."
            ) from error
        payload = json.loads(body if separator else result.stdout.decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError("BountyPay open-bounty response is not a list")
    return payload


class BountyPayClient:
    def __init__(
        self,
        cache_path: str = "data/bountypay-cache.json",
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
            raise ValueError("BountyPay open-bounty response is not a list")
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.cache_path)
        return payload, False


def _amount_usd(item: dict) -> Optional[float]:
    symbol = str(item.get("tokenSymbol") or "").upper()
    decimals = TOKEN_DECIMALS.get(symbol)
    if decimals is None:
        return None
    try:
        return int(str(item.get("amount"))) / (10 ** decimals)
    except (TypeError, ValueError):
        return None


def normalize(payload: list, evidence_stale: bool = False) -> List[Opportunity]:
    opportunities: List[Opportunity] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        repository = str(item.get("repoFullName") or "")
        issue_number = item.get("issueNumber")
        source_id = str(item.get("bountyId") or "")
        amount_usd = _amount_usd(item)
        if (
            repository.count("/") != 1
            or not isinstance(issue_number, int)
            or not source_id
            or amount_usd is None
            or amount_usd <= 0
        ):
            continue
        try:
            chain_id = int(item.get("chainId"))
        except (TypeError, ValueError):
            chain_id = None
        network = str(item.get("network") or "")
        testnet = (
            chain_id not in MAINNET_CHAIN_IDS
            or "TESTNET" in network.upper()
            or "SEPOLIA" in network.upper()
        )
        deadline = None
        try:
            deadline = datetime.fromtimestamp(
                int(item.get("deadline")), tz=timezone.utc
            ).isoformat()
        except (TypeError, ValueError, OSError):
            pass
        issue_url = f"https://github.com/{repository}/issues/{issue_number}"
        opportunities.append(
            Opportunity(
                source="bountypay",
                source_id=source_id,
                title=str(item.get("issueTitle") or f"{repository}#{issue_number}"),
                source_url=OPEN_URL,
                bounty_usd=amount_usd,
                status=str(item.get("status") or "open"),
                deadline=deadline,
                repository=repository,
                issue_url=issue_url,
                platform_fee_rate=0.0,
                payout_requires_sponsor_action=False,
                notes=[
                    "Discovered through BountyPay's public open-bounty API.",
                    (
                        "This record is testnet-only and cannot be treated as income."
                        if testnet
                        else "The platform reports funded smart-contract escrow and "
                        "automatic payout when a closing pull request is merged."
                    ),
                ],
                evidence={
                    "bountypay_evidence_stale": evidence_stale,
                    "payment_testnet": testnet,
                    "payment_model": "smart_contract_escrow_auto_payout_on_merge",
                    "payment_assurance": "platform_api_funded_record",
                    "payment_model_source": DOC_URL,
                    "bountypay_chain_id": chain_id,
                    "bountypay_network": network,
                    "bountypay_token": item.get("token"),
                    "bountypay_token_symbol": item.get("tokenSymbol"),
                    "bountypay_transaction_hash": item.get("txHash"),
                    "bountypay_sponsor_address": item.get("sponsorAddress"),
                },
                raw=item,
            )
        )
    return opportunities
