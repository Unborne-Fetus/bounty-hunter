"""Read-only, verified-origin desktop update discovery.

There is intentionally NO silent EXE replacement or unauthenticated installer
execution. Users confirm installation through GitHub's release page.
"""
import json
import re
from urllib.error import HTTPError
from urllib.request import Request, urlopen

APP_VERSION = "0.2.0"
RELEASES_URL = "https://github.com/Unborne-Fetus/bounty-hunter/releases"
ACTIONS_URL = "https://github.com/Unborne-Fetus/bounty-hunter/actions/workflows/build-windows-dashboard.yml"
API_URL = "https://api.github.com/repos/Unborne-Fetus/bounty-hunter/releases/latest"
TAG_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
RELEASE_PREFIX = "https://github.com/Unborne-Fetus/bounty-hunter/releases/tag/"


def _version(text):
    match = TAG_PATTERN.fullmatch(str(text or ""))
    return tuple(map(int, match.groups())) if match else None


def check_for_updates(fetcher=None) -> dict:
    """Only official public GitHub Release metadata is trusted for updates."""
    fetcher = fetcher or urlopen
    request = Request(
        API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "bounty-hunter-desktop",
        },
    )
    try:
        with fetcher(request, timeout=15) as response:
            data = json.load(response)
    except HTTPError as error:
        if error.code == 404:
            return {
                "available": False, "current_version": APP_VERSION,
                "message": "No packaged GitHub release is published yet.",
                "url": ACTIONS_URL,
            }
        raise

    if not isinstance(data, dict):
        raise ValueError("GitHub release metadata is invalid")
    tag = str(data.get("tag_name") or "")
    url = str(data.get("html_url") or "")
    version = _version(tag)
    current = _version(APP_VERSION)
    if not version or not current:
        raise ValueError("GitHub release uses an unsupported version tag")
    if not url.startswith(RELEASE_PREFIX) or url != RELEASE_PREFIX + tag:
        raise ValueError("GitHub release returned an unexpected download location")
    return {
        "available": version > current,
        "current_version": APP_VERSION,
        "latest_version": tag,
        "url": url,
        "message": (
            f"Version {tag} is available."
            if version > current else "You're using the latest published release."
        ),
    }
