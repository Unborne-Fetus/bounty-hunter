"""Desktop scan and supervised-workflow helpers; never store credentials."""
import json
import os
import re
import sys
import subprocess
from pathlib import Path


MODULES = {
    "scan": "bounty_hunter.cli",
    "workflow": "bounty_hunter.workflow",
    "preflight": "bounty_hunter.preflight",
    "attempt": "bounty_hunter.attempt",
    "submission": "bounty_hunter.submission",
}


def workspace_valid(path):
    root = Path(path).expanduser().resolve()
    return (root / "bounty_hunter" / "cli.py").is_file()


def scan_args(country="", algora_orgs="", disabled_sources=()):
    args = []
    country = country.strip().upper()
    if country:
        if not re.fullmatch(r"[A-Z]{2}", country):
            raise ValueError("Payout country must be a two-letter country code")
        args += ["--boss-payout-country", country]
    orgs = [x.strip() for x in algora_orgs.split(",") if x.strip()]
    if len(orgs) > 15:
        raise ValueError("At most 15 Algora organizations can be configured")
    for org in orgs:
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,60}", org):
            raise ValueError("Invalid Algora organization handle: " + org)
        args += ["--algora-org", org]
    allowed = {"bountypay", "boss", "opire", "taskbounty"}
    if not set(disabled_sources).issubset(allowed):
        raise ValueError("Invalid disabled source")
    for source in sorted(disabled_sources):
        args.append("--no-" + source)
    return args


def worker_command(name, args):
    if name not in MODULES:
        raise ValueError("Unsupported worker")
    if getattr(sys, "frozen", False):
        return [sys.executable, "--bounty-worker", name] + list(args)
    return [sys.executable, "-m", MODULES[name]] + list(args)


def run_worker(name, args, workspace, timeout=None):
    """Execute only known modules, as argv; no shell or secret args."""
    if not workspace_valid(workspace):
        raise ValueError("Choose the cloned Bounty Hunter repository in Setup")
    command = worker_command(name, args)
    env = dict(os.environ)
    # Bundled executable dispatches through dashboard.main; source uses -m.
    return subprocess.run(
        command, cwd=str(Path(workspace).resolve()), env=env, shell=False,
        capture_output=True, text=True, errors="replace", timeout=timeout
    )


def report_rows(workspace):
    path = Path(workspace).resolve() / "reports" / "opportunities.json"
    if not path.is_file():
        return [], "Run a scan first."
    with path.open(encoding="utf-8") as handle:
        report = json.load(handle)
    return report.get("opportunities", []), ""


def local_settings_path():
    return Path(os.environ.get("APPDATA") or Path.home()) / "BountyHunter" / "app-settings.json"


def load_settings():
    try:
        obj = json.loads(local_settings_path().read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except (ValueError, OSError):
        return {}


def save_settings(obj):
    allowed = {"workspace", "country", "algora_orgs", "disabled_sources"}
    filtered = {key: obj[key] for key in allowed if key in obj}
    target = local_settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(filtered, indent=2), encoding="utf-8")
    temp.replace(target)
    return target
