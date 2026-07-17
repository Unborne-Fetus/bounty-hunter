import argparse
import fcntl
import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional

from .history import History


MAX_LOG_BYTES = 64 * 1024
SCAN_RESULT_RE = re.compile(
    r"evaluated\s+(\d+)\s+opportunities;\s+(\d+)\s+eligible;"
)


def _bounded(value: str) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_LOG_BYTES:
        return value
    return encoded[:MAX_LOG_BYTES].decode("utf-8", errors="replace") + "\n[truncated]\n"


def _write_status(path: Path, status: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _scan_counts(stdout: str):
    match = SCAN_RESULT_RE.search(stdout)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _file_fresh(path: Path, at: datetime, interval_seconds: float) -> bool:
    if not path.exists():
        return False
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return modified >= at - timedelta(seconds=interval_seconds)


def _queue_top_eligible(report_path: Path, history_path: Path) -> Optional[Dict]:
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    records = [
        record
        for record in payload.get("opportunities") or []
        if (record.get("score") or {}).get("eligible") is True
    ]
    records.sort(
        key=lambda record: (
            (record.get("score") or {}).get("priority_score", 0),
            (record.get("score") or {}).get("expected_hourly_return_usd", 0),
        ),
        reverse=True,
    )
    history = History(str(history_path))
    try:
        active = history.active_attempt()
        if active and active["status"] == "queued":
            still_eligible = any(
                record["opportunity"].get("source") == active["source"]
                and record["opportunity"].get("source_id") == active["source_id"]
                for record in records
            )
            if not still_eligible:
                history.transition(
                    active["id"],
                    "abandoned",
                    outcome_note=(
                        "Monitor withdrew the unapproved queue entry because "
                        "the latest scan no longer marks it eligible."
                    ),
                )
                active = None
        created = False
        if not active and records:
            attempt_id = history.queue_from_record(records[0])
            active = history.get_attempt(attempt_id)
            created = True
        if not active:
            return None
        digest = history.attempt_record_sha256(active["id"])
        return {
            "attempt_id": active["id"],
            "source": active["source"],
            "source_id": active["source_id"],
            "title": active["title"],
            "status": active["status"],
            "created_by_monitor": created,
            "record_sha256": digest,
            "review_command": (
                "python3 -m bounty_hunter.workflow review "
                f"{active['id']}"
            ),
        }
    finally:
        history.close()


def run_monitor(
    *,
    project_root: str,
    interval_seconds: float,
    once: bool = False,
    status_path: str = "data/monitor-status.json",
    lock_path: str = "data/monitor.lock",
    command: Iterable[str] = ("sh", "scripts/daily_scan.sh"),
    discovery_command: Optional[Iterable[str]] = None,
    discovery_interval_seconds: float = 7 * 24 * 60 * 60,
    discovery_report_path: str = "reports/algora-board-audit.json",
    auto_queue: bool = False,
    opportunity_report_path: str = "reports/opportunities.json",
    history_db_path: str = "data/bounty_hunter.db",
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    sleeper: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> int:
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise ValueError("monitor project root does not exist")
    interval_seconds = max(60.0, float(interval_seconds))
    status_file = (root / status_path).resolve()
    lock_file = (root / lock_path).resolve()
    discovery_report = (root / discovery_report_path).resolve()
    opportunity_report = (root / opportunity_report_path).resolve()
    history_db = (root / history_db_path).resolve()
    for path in (
        status_file,
        lock_file,
        discovery_report,
        opportunity_report,
        history_db,
    ):
        try:
            path.relative_to(root)
        except ValueError as error:
            raise ValueError("monitor state paths must stay inside project root") from error
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    failures = 0
    last_discovery_success_at: Optional[datetime] = None
    with lock_file.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("another bounty monitor already holds the lock") from error
        while True:
            started = now()
            discovery_status = {
                "ran": False,
                "report_path": discovery_report_path,
            }
            discovery_due = bool(discovery_command) and not (
                (
                    last_discovery_success_at is not None
                    and last_discovery_success_at
                    >= started - timedelta(seconds=discovery_interval_seconds)
                )
                or _file_fresh(
                    discovery_report, started, discovery_interval_seconds
                )
            )
            if discovery_due:
                discovery_result = runner(
                    list(discovery_command or ()),
                    cwd=str(root),
                    check=False,
                    capture_output=True,
                    text=True,
                )
                discovery_status.update(
                    {
                        "ran": True,
                        "exit_code": discovery_result.returncode,
                        "stdout": _bounded(discovery_result.stdout),
                        "stderr": _bounded(discovery_result.stderr),
                    }
                )
                if discovery_result.returncode == 0:
                    last_discovery_success_at = started
            result = runner(
                list(command),
                cwd=str(root),
                check=False,
                capture_output=True,
                text=True,
            )
            finished = now()
            if result.returncode == 0:
                failures = 0
                delay = interval_seconds
            else:
                failures += 1
                delay = min(interval_seconds, 300.0 * (2 ** min(failures - 1, 6)))
            evaluated_count, eligible_count = _scan_counts(result.stdout)
            queue_status: Dict = {"enabled": auto_queue}
            queued_attempt = None
            if auto_queue and result.returncode == 0:
                try:
                    queued_attempt = _queue_top_eligible(
                        opportunity_report, history_db
                    )
                    queue_status["status"] = "ready" if queued_attempt else "idle"
                except Exception as error:
                    queue_status.update(
                        {"status": "error", "error": str(error)[:300]}
                    )
            status = {
                "schema_version": 4,
                "command": list(command),
                "started_at": started.isoformat(),
                "finished_at": finished.isoformat(),
                "exit_code": result.returncode,
                "consecutive_failures": failures,
                "evaluated_count": evaluated_count,
                "eligible_count": eligible_count,
                "attention_required": bool(eligible_count or queued_attempt),
                "queued_attempt": queued_attempt,
                "auto_queue": queue_status,
                "board_discovery": discovery_status,
                "next_scan_at": (finished + timedelta(seconds=delay)).isoformat(),
                "stdout": _bounded(result.stdout),
                "stderr": _bounded(result.stderr),
            }
            _write_status(status_file, status)
            if once:
                return result.returncode
            sleeper(delay)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Continuously run rate-safe bounty discovery scans."
    )
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--interval-hours", type=float, default=6.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status-path", default="data/monitor-status.json")
    parser.add_argument("--lock-path", default="data/monitor.lock")
    parser.add_argument(
        "--no-board-discovery",
        action="store_true",
        help="Disable the low-frequency candidate Algora board audit.",
    )
    parser.add_argument(
        "--discovery-interval-hours", type=float, default=168.0
    )
    parser.add_argument(
        "--no-auto-queue",
        action="store_true",
        help="Do not queue the top eligible record for explicit human approval.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        return run_monitor(
            project_root=args.project_root,
            interval_seconds=args.interval_hours * 60 * 60,
            once=args.once,
            status_path=args.status_path,
            lock_path=args.lock_path,
            discovery_command=(
                None
                if args.no_board_discovery
                else ("sh", "scripts/weekly_algora_discovery.sh")
            ),
            discovery_interval_seconds=max(
                3600.0, args.discovery_interval_hours * 60 * 60
            ),
            auto_queue=not args.no_auto_queue,
        )
    except Exception as error:
        print(json.dumps({"error": str(error)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
