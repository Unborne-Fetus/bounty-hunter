"""Non-secret local settings and workspace for the all-in-one desktop app."""
import json
import os
import sys
from pathlib import Path

SOURCES = ("algora", "bountypay", "boss", "opire", "taskbounty")
DEFAULTS = {
    "workspace": "",
    "sources": {"algora": True, "bountypay": True, "boss": True,
                "opire": True, "taskbounty": True},
    "algora_organizations": [],
    "boss_payout_country": "",
    "github_token_env": "GITHUB_TOKEN",
    "timeout_seconds": 20,
    "auto_scan": False,
    "scan_interval_minutes": 360,
}


def settings_path() -> Path:
    folder = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(folder) / "BountyHunter" / "desktop.json"


def initial_workspace() -> str:
    # A source checkout shares its existing scan / history files.
    source_root = Path(__file__).resolve().parent.parent
    if (source_root / "pyproject.toml").is_file() and (
        source_root / "bounty_hunter" / "cli.py"
    ).is_file():
        return str(source_root)
    # Portable EXE beside an existing local repository can also reuse its data.
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        for candidate in (exe_dir, exe_dir.parent):
            if (candidate / "data" / "bounty_hunter.db").is_file():
                return str(candidate)
    root = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return str(Path(root) / "BountyHunter" / "workspace")


def normalize_settings(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Settings must be a JSON object")
    values = dict(DEFAULTS)
    values.update({key: value for key, value in payload.items() if key in DEFAULTS})
    folder = str(values.get("workspace") or initial_workspace()).strip()
    if not folder:
        raise ValueError("Workspace folder cannot be empty")
    values["workspace"] = str(Path(folder).expanduser().resolve())
    enabled = values.get("sources")
    if not isinstance(enabled, dict):
        raise ValueError("Source preferences must be an object")
    values["sources"] = {name: bool(enabled.get(name, False)) for name in SOURCES}
    if not any(values["sources"].values()):
        raise ValueError("Enable at least one bounty source")
    organizations = values.get("algora_organizations") or []
    if isinstance(organizations, str):
        organizations = organizations.replace(",", " ").split()
    if not isinstance(organizations, list):
        raise ValueError("Algora organizations must be a list")
    import re
    values["algora_organizations"] = list(dict.fromkeys(
        str(item).strip() for item in organizations
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", str(item).strip())
    ))[:20]
    country = str(values.get("boss_payout_country") or "").upper().strip()
    if country and not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError("Payout country must be a two-letter code or blank")
    values["boss_payout_country"] = country
    env = str(values.get("github_token_env") or "GITHUB_TOKEN").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,80}", env):
        raise ValueError("GitHub token environment-variable name is invalid")
    values["github_token_env"] = env
    try:
        timeout = int(values["timeout_seconds"])
        minutes = int(values["scan_interval_minutes"])
    except (TypeError, ValueError) as error:
        raise ValueError("Timeout and interval must be whole numbers") from error
    if not 5 <= timeout <= 120:
        raise ValueError("Request timeout must be between 5 and 120 seconds")
    if not 60 <= minutes <= 10080:
        raise ValueError("Automatic scan interval must be 60–10080 minutes")
    values["timeout_seconds"] = timeout
    values["scan_interval_minutes"] = minutes
    values["auto_scan"] = bool(values["auto_scan"])
    return values


def load_settings() -> dict:
    path = settings_path()
    if path.is_file():
        return normalize_settings(json.loads(path.read_text(encoding="utf-8")))
    return normalize_settings({})


def save_settings(payload: dict) -> dict:
    values = normalize_settings(payload)
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return values


def workspace_paths(settings: dict) -> dict:
    root = Path(settings["workspace"]).expanduser().resolve()
    return {
        "root": root, "db": root / "data" / "bounty_hunter.db",
        "reports": root / "reports",
        "opportunities": root / "reports" / "opportunities.json",
        "algora_audit": root / "reports" / "algora-board-audit.json",
        "attempts": root / "data" / "attempts",
        "submission_checkouts": root / "data" / "submissions",
        "attempt_reports": root / "reports" / "attempts",
    }
