import json
import hashlib
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import ScoringConfig


ATTEMPTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'queued',
            'approved',
            'running',
            'verified',
            'submission_approved',
            'submitted',
            'revision',
            'merged',
            'paid',
            'rejected',
            'abandoned'
        )
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    approved_at TEXT,
    approval_note TEXT,
    approved_record_sha256 TEXT,
    submission_approved_at TEXT,
    submission_approval_note TEXT,
    approved_bundle_sha256 TEXT,
    agent_cost_usd REAL NOT NULL DEFAULT 0,
    compute_cost_usd REAL NOT NULL DEFAULT 0,
    human_hours REAL NOT NULL DEFAULT 0,
    payout_usd REAL NOT NULL DEFAULT 0,
    pr_url TEXT,
    outcome_note TEXT,
    opportunity_json TEXT,
    score_json TEXT
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at TEXT NOT NULL,
    opportunity_count INTEGER NOT NULL,
    eligible_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS opportunity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL REFERENCES scans(id),
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    bounty_usd REAL NOT NULL,
    decision TEXT NOT NULL,
    expected_value_usd REAL NOT NULL,
    expected_hourly_return_usd REAL NOT NULL,
    payout_probability REAL NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_source_id
ON opportunity_snapshots(source, source_id);

CREATE TABLE IF NOT EXISTS attempt_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL REFERENCES attempts(id),
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);

"""

ATTEMPT_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS attempt_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL REFERENCES attempts(id),
    kind TEXT NOT NULL CHECK (
        kind IN (
            'agent_cost_usd',
            'compute_cost_usd',
            'human_hours',
            'payout_usd'
        )
    ),
    amount REAL NOT NULL CHECK (amount > 0),
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attempt_events_attempt_id
ON attempt_events(attempt_id, id);
"""

ATTEMPT_AUDIT_SQL = """
CREATE TABLE IF NOT EXISTS attempt_state_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL REFERENCES attempts(id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attempt_state_events_attempt_id
ON attempt_state_events(attempt_id, id);

CREATE TABLE IF NOT EXISTS attempt_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL REFERENCES attempts(id),
    kind TEXT NOT NULL,
    value REAL NOT NULL CHECK (value > 0),
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attempt_metrics_attempt_id
ON attempt_metrics(attempt_id, id);
"""

EVENT_COLUMNS = {
    "agent_cost_usd": "agent_cost_usd",
    "compute_cost_usd": "compute_cost_usd",
    "human_hours": "human_hours",
    "payout_usd": "payout_usd",
}

MINIMUM_PILOT_TERMINAL_ATTEMPTS = 10


def pilot_evaluation(
    *,
    terminal_attempts: int,
    wins: int,
    direct_costs_usd: float,
    human_hours: float,
    payouts_usd: float,
    human_hour_value_usd: float = ScoringConfig().human_hour_value_usd,
    minimum_terminal_attempts: int = MINIMUM_PILOT_TERMINAL_ATTEMPTS,
) -> Dict[str, Any]:
    human_cost = human_hours * human_hour_value_usd
    economic_cost = direct_costs_usd + human_cost
    cash_net = payouts_usd - direct_costs_usd
    economic_net = payouts_usd - economic_cost
    sufficient_sample = terminal_attempts >= minimum_terminal_attempts
    if not sufficient_sample:
        status = "collecting_data"
    elif cash_net > 0 and economic_net > 0:
        status = "positive_net_income"
    else:
        status = "negative_net_income"
    return {
        "status": status,
        "positive_net_income_proven": status == "positive_net_income",
        "terminal_attempts": terminal_attempts,
        "minimum_terminal_attempts": minimum_terminal_attempts,
        "remaining_terminal_attempts": max(
            0, minimum_terminal_attempts - terminal_attempts
        ),
        "wins": wins,
        "observed_win_rate": (
            round(wins / terminal_attempts, 4) if terminal_attempts else None
        ),
        "economic_cost_usd": round(economic_cost, 2),
        "cash_net_income_usd": round(cash_net, 2),
        "net_income_usd": round(economic_net, 2),
        "economic_roi": (
            round(economic_net / economic_cost, 4) if economic_cost > 0 else None
        ),
        "net_income_per_terminal_attempt_usd": (
            round(economic_net / terminal_attempts, 2)
            if terminal_attempts
            else None
        ),
        "net_income_per_human_hour_usd": (
            round(economic_net / human_hours, 2) if human_hours > 0 else None
        ),
        "break_even_payout_remaining_usd": round(max(0.0, -economic_net), 2),
        "criteria": (
            f"At least {minimum_terminal_attempts} measured terminal attempts, "
            "positive cash net income, and positive net income after valuing "
            "human review time."
        ),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class History:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self._migrate_attempts()
        self.connection.executescript(ATTEMPTS_TABLE_SQL)
        self._ensure_attempt_columns()
        self.connection.executescript(ATTEMPT_EVENTS_SQL)
        self.connection.executescript(ATTEMPT_AUDIT_SQL)
        self._backfill_attempt_events()
        self._backfill_state_events()
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS attempt_artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id INTEGER NOT NULL REFERENCES attempts(id),
                kind TEXT NOT NULL,
                path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )

    def _backfill_attempt_events(self) -> None:
        for kind, column in EVENT_COLUMNS.items():
            rows = self.connection.execute(
                f"""
                SELECT id, {column} AS amount
                FROM attempts
                WHERE {column} > 0
                  AND NOT EXISTS (
                      SELECT 1 FROM attempt_events
                      WHERE attempt_events.attempt_id = attempts.id
                        AND attempt_events.kind = ?
                  )
                """,
                (kind,),
            ).fetchall()
            for row in rows:
                self.connection.execute(
                    """
                    INSERT INTO attempt_events(
                        attempt_id, kind, amount, note, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        kind,
                        row["amount"],
                        "Imported from pre-event aggregate ledger.",
                        _now(),
                    ),
                )
        self.connection.commit()

    def _backfill_state_events(self) -> None:
        rows = self.connection.execute(
            """
            SELECT id, status, created_at
            FROM attempts
            WHERE NOT EXISTS (
                SELECT 1 FROM attempt_state_events
                WHERE attempt_state_events.attempt_id = attempts.id
            )
            """
        ).fetchall()
        for row in rows:
            self.connection.execute(
                """
                INSERT INTO attempt_state_events(
                    attempt_id, from_status, to_status, note, created_at
                ) VALUES (?, NULL, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["status"],
                    "Imported current state from pre-audit ledger.",
                    row["created_at"],
                ),
            )
        self.connection.commit()

    def _record_state_without_commit(
        self,
        attempt_id: int,
        from_status: Optional[str],
        to_status: str,
        note: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO attempt_state_events(
                attempt_id, from_status, to_status, note, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (attempt_id, from_status, to_status, note.strip() or "State changed.", _now()),
        )

    def _ensure_attempt_columns(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(attempts)"
            ).fetchall()
        }
        for column in ("approved_bundle_sha256", "approved_record_sha256"):
            if column not in columns:
                self.connection.execute(
                    f"ALTER TABLE attempts ADD COLUMN {column} TEXT"
                )
        for column in ("opportunity_json", "score_json"):
            if column not in columns:
                self.connection.execute(
                    f"ALTER TABLE attempts ADD COLUMN {column} TEXT"
                )
        self.connection.commit()

    def _migrate_attempts(self) -> None:
        row = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'attempts'"
        ).fetchone()
        if not row:
            return
        if "submission_approved" in row["sql"]:
            return
        self.connection.execute("ALTER TABLE attempts RENAME TO attempts_legacy")
        self.connection.executescript(ATTEMPTS_TABLE_SQL)
        self.connection.execute(
            """
            INSERT INTO attempts(
                id, source, source_id, title, status, created_at, updated_at,
                approved_at, approval_note, agent_cost_usd, compute_cost_usd,
                human_hours, payout_usd, pr_url, outcome_note
            )
            SELECT
                id, source, source_id, title, status, created_at, updated_at,
                approved_at, approval_note, agent_cost_usd, compute_cost_usd,
                human_hours, payout_usd, pr_url, outcome_note
            FROM attempts_legacy
            """
        )
        self.connection.execute("DROP TABLE attempts_legacy")
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def record_scan(self, records: List[Dict[str, Any]]) -> int:
        generated_at = _now()
        eligible_count = sum(
            1 for record in records if record["score"]["eligible"]
        )
        cursor = self.connection.execute(
            """
            INSERT INTO scans(generated_at, opportunity_count, eligible_count)
            VALUES (?, ?, ?)
            """,
            (generated_at, len(records), eligible_count),
        )
        scan_id = int(cursor.lastrowid)
        for record in records:
            opportunity = record["opportunity"]
            score = record["score"]
            payout_probability = (
                score["probability_valid"]
                * score["probability_verified"]
                * score["probability_win_merge"]
            )
            self.connection.execute(
                """
                INSERT INTO opportunity_snapshots(
                    scan_id, source, source_id, title, bounty_usd, decision,
                    expected_value_usd, expected_hourly_return_usd,
                    payout_probability, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_id,
                    opportunity["source"],
                    opportunity["source_id"],
                    opportunity["title"],
                    opportunity["bounty_usd"],
                    score["decision"],
                    score["expected_value_usd"],
                    score["expected_hourly_return_usd"],
                    payout_probability,
                    json.dumps(record, sort_keys=True),
                ),
            )
        self.connection.commit()
        return scan_id

    def latest_opportunity_record(
        self, source: str, source_id: str
    ) -> Optional[Dict[str, Any]]:
        row = self.connection.execute(
            """
            SELECT opportunity_snapshots.payload_json, scans.generated_at
            FROM opportunity_snapshots
            JOIN scans ON scans.id = opportunity_snapshots.scan_id
            WHERE opportunity_snapshots.source = ?
              AND opportunity_snapshots.source_id = ?
            ORDER BY opportunity_snapshots.id DESC
            LIMIT 1
            """,
            (source, source_id),
        ).fetchone()
        if not row:
            return None
        record = json.loads(row["payload_json"])
        record["historical_scan_at"] = row["generated_at"]
        return record

    def latest_github_enriched_record(
        self, source: str, source_id: str
    ) -> Optional[Dict[str, Any]]:
        row = self.connection.execute(
            """
            SELECT opportunity_snapshots.payload_json, scans.generated_at
            FROM opportunity_snapshots
            JOIN scans ON scans.id = opportunity_snapshots.scan_id
            WHERE opportunity_snapshots.source = ?
              AND opportunity_snapshots.source_id = ?
              AND json_extract(
                    opportunity_snapshots.payload_json,
                    '$.opportunity.evidence.github_enriched_at'
                  ) IS NOT NULL
            ORDER BY opportunity_snapshots.id DESC
            LIMIT 1
            """,
            (source, source_id),
        ).fetchone()
        if not row:
            return None
        record = json.loads(row["payload_json"])
        record["historical_scan_at"] = row["generated_at"]
        return record

    def queue_from_record(self, record: Dict[str, Any]) -> int:
        opportunity = record["opportunity"]
        now = _now()
        existing = self.connection.execute(
            """
            SELECT id FROM attempts
            WHERE source = ? AND source_id = ?
              AND status NOT IN ('paid', 'rejected', 'abandoned')
            ORDER BY id DESC LIMIT 1
            """,
            (opportunity["source"], opportunity["source_id"]),
        ).fetchone()
        if existing:
            return int(existing["id"])
        cursor = self.connection.execute(
            """
            INSERT INTO attempts(
                source, source_id, title, status, created_at, updated_at,
                opportunity_json, score_json
            ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)
            """,
            (
                opportunity["source"],
                opportunity["source_id"],
                opportunity["title"],
                now,
                now,
                json.dumps(opportunity, sort_keys=True),
                json.dumps(record.get("score") or {}, sort_keys=True),
            ),
        )
        attempt_id = int(cursor.lastrowid)
        self._record_state_without_commit(
            attempt_id, None, "queued", "Opportunity queued for human review."
        )
        self.connection.commit()
        return attempt_id

    def active_attempt(self) -> Optional[Dict[str, Any]]:
        row = self.connection.execute(
            """
            SELECT * FROM attempts
            WHERE status NOT IN ('paid', 'rejected', 'abandoned')
            ORDER BY id ASC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None

    def attempt_record_sha256(self, attempt_id: int) -> str:
        record = self.get_attempt_record(attempt_id)
        canonical = json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def approve(self, attempt_id: int, note: str, record_sha256: str) -> None:
        expected = self.attempt_record_sha256(attempt_id)
        if record_sha256 != expected:
            raise ValueError(
                "attempt approval references the wrong opportunity record digest"
            )
        now = _now()
        cursor = self.connection.execute(
            """
            UPDATE attempts
            SET status = 'approved', approved_at = ?, approval_note = ?,
                approved_record_sha256 = ?, updated_at = ?
            WHERE id = ? AND status = 'queued'
            """,
            (now, note, record_sha256, now, attempt_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("attempt must exist and be queued before approval")
        self._record_state_without_commit(
            attempt_id, "queued", "approved", note
        )
        self.connection.commit()

    def add_artifact(
        self, attempt_id: int, kind: str, path: str, sha256: str
    ) -> int:
        self.get_attempt(attempt_id)
        cursor = self.connection.execute(
            """
            INSERT INTO attempt_artifacts(
                attempt_id, kind, path, sha256, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (attempt_id, kind, path, sha256, _now()),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def latest_artifact(
        self, attempt_id: int, kind: str
    ) -> Optional[Dict[str, Any]]:
        row = self.connection.execute(
            """
            SELECT * FROM attempt_artifacts
            WHERE attempt_id = ? AND kind = ?
            ORDER BY id DESC LIMIT 1
            """,
            (attempt_id, kind),
        ).fetchone()
        return dict(row) if row else None

    def artifact_count(self, attempt_id: int, kind: str) -> int:
        self.get_attempt(attempt_id)
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS count FROM attempt_artifacts
            WHERE attempt_id = ? AND kind = ?
            """,
            (attempt_id, kind),
        ).fetchone()
        return int(row["count"])

    def attempt_events(self, attempt_id: int) -> List[Dict[str, Any]]:
        self.get_attempt(attempt_id)
        rows = self.connection.execute(
            """
            SELECT * FROM attempt_events
            WHERE attempt_id = ?
            ORDER BY id
            """,
            (attempt_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def state_events(self, attempt_id: int) -> List[Dict[str, Any]]:
        self.get_attempt(attempt_id)
        rows = self.connection.execute(
            """
            SELECT * FROM attempt_state_events
            WHERE attempt_id = ?
            ORDER BY id
            """,
            (attempt_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def record_metric(
        self, attempt_id: int, kind: str, value: float, note: str
    ) -> int:
        self.get_attempt(attempt_id)
        if not kind.strip():
            raise ValueError("attempt metric kind is required")
        if not math.isfinite(value) or value <= 0:
            raise ValueError("attempt metric value must be finite and positive")
        if not note.strip():
            raise ValueError("attempt metric note is required")
        cursor = self.connection.execute(
            """
            INSERT INTO attempt_metrics(
                attempt_id, kind, value, note, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (attempt_id, kind.strip(), value, note.strip(), _now()),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def attempt_metrics(self, attempt_id: int) -> List[Dict[str, Any]]:
        self.get_attempt(attempt_id)
        rows = self.connection.execute(
            """
            SELECT * FROM attempt_metrics
            WHERE attempt_id = ?
            ORDER BY id
            """,
            (attempt_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _validate_event(kind: str, amount: float) -> None:
        if kind not in EVENT_COLUMNS:
            raise ValueError(f"unsupported attempt event: {kind}")
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("attempt event amount must be finite and positive")

    def _record_event_without_commit(
        self,
        attempt_id: int,
        kind: str,
        amount: float,
        note: str,
    ) -> None:
        self._validate_event(kind, amount)
        if not note.strip():
            raise ValueError("attempt event note is required")
        column = EVENT_COLUMNS[kind]
        self.connection.execute(
            f"UPDATE attempts SET {column} = {column} + ?, updated_at = ? WHERE id = ?",
            (amount, _now(), attempt_id),
        )
        self.connection.execute(
            """
            INSERT INTO attempt_events(
                attempt_id, kind, amount, note, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (attempt_id, kind, amount, note.strip(), _now()),
        )

    def record_event(
        self,
        attempt_id: int,
        kind: str,
        amount: float,
        note: str,
    ) -> int:
        attempt = self.get_attempt(attempt_id)
        if kind == "payout_usd" and attempt["status"] not in {"merged", "paid"}:
            raise ValueError("payouts can only be recorded after merge")
        self._record_event_without_commit(attempt_id, kind, amount, note)
        event_id = int(self.connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        self.connection.commit()
        return event_id

    def approve_submission(
        self, attempt_id: int, note: str, bundle_sha256: str
    ) -> None:
        artifact = self.latest_artifact(attempt_id, "review_bundle")
        if not artifact or artifact["sha256"] != bundle_sha256:
            raise ValueError(
                "submission approval must reference the latest review bundle SHA-256"
            )
        now = _now()
        cursor = self.connection.execute(
            """
            UPDATE attempts
            SET status = 'submission_approved',
                submission_approved_at = ?,
                submission_approval_note = ?,
                approved_bundle_sha256 = ?,
                updated_at = ?
            WHERE id = ? AND status = 'verified'
            """,
            (
                now,
                f"{note}\nBundle-SHA256: {bundle_sha256}",
                bundle_sha256,
                now,
                attempt_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError(
                "attempt must be verified before submission approval"
            )
        self._record_state_without_commit(
            attempt_id, "verified", "submission_approved", note
        )
        self.connection.commit()

    def transition(
        self,
        attempt_id: int,
        status: str,
        *,
        agent_cost_usd: Optional[float] = None,
        compute_cost_usd: Optional[float] = None,
        human_hours: Optional[float] = None,
        payout_usd: Optional[float] = None,
        pr_url: Optional[str] = None,
        outcome_note: Optional[str] = None,
    ) -> None:
        allowed = {
            "running",
            "verified",
            "submission_approved",
            "submitted",
            "revision",
            "merged",
            "paid",
            "rejected",
            "abandoned",
        }
        if status not in allowed:
            raise ValueError(f"unsupported transition status: {status}")
        current = self.get_attempt(attempt_id)
        transitions = {
            "queued": {"abandoned"},
            "approved": {"running", "abandoned"},
            "running": {"verified", "abandoned"},
            "verified": {"abandoned"},
            "submission_approved": {"submitted", "abandoned"},
            "submitted": {"revision", "merged", "rejected"},
            "revision": {"running", "abandoned"},
            "merged": {"paid"},
            "paid": set(),
            "rejected": set(),
            "abandoned": set(),
        }
        if status not in transitions[current["status"]]:
            raise ValueError(
                f"invalid attempt transition: {current['status']} -> {status}"
            )
        if status == "submitted":
            if not pr_url:
                raise ValueError("submitted attempts require an upstream PR URL")
            artifact = self.latest_artifact(attempt_id, "review_bundle")
            if (
                not artifact
                or artifact["sha256"] != current["approved_bundle_sha256"]
            ):
                raise ValueError(
                    "submitted attempt must match the approved review bundle"
                )
        for name, value in (
            ("agent_cost_usd", agent_cost_usd),
            ("compute_cost_usd", compute_cost_usd),
            ("human_hours", human_hours),
            ("payout_usd", payout_usd),
        ):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} must be finite and non-negative")

        values: Dict[str, Any] = {
            "status": status,
            "updated_at": _now(),
        }
        optional = {
            "pr_url": pr_url,
            "outcome_note": outcome_note,
        }
        values.update({key: value for key, value in optional.items() if value is not None})
        assignments = ", ".join(f"{key} = ?" for key in values)
        cursor = self.connection.execute(
            f"UPDATE attempts SET {assignments} WHERE id = ?",
            (*values.values(), attempt_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("attempt does not exist")
        self._record_state_without_commit(
            attempt_id,
            current["status"],
            status,
            outcome_note or f"Transitioned to {status}.",
        )
        event_note = outcome_note or f"Recorded during transition to {status}."
        for kind, amount in (
            ("agent_cost_usd", agent_cost_usd),
            ("compute_cost_usd", compute_cost_usd),
            ("human_hours", human_hours),
            ("payout_usd", payout_usd),
        ):
            if amount:
                self._record_event_without_commit(
                    attempt_id, kind, amount, event_note
                )
        self.connection.commit()

    def get_attempt(self, attempt_id: int) -> Dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if not row:
            raise ValueError("attempt does not exist")
        return dict(row)

    def get_attempt_record(self, attempt_id: int) -> Dict[str, Any]:
        attempt = self.get_attempt(attempt_id)
        if not attempt.get("opportunity_json"):
            raise ValueError("attempt has no immutable opportunity snapshot")
        return {
            "opportunity": json.loads(attempt["opportunity_json"]),
            "score": json.loads(attempt.get("score_json") or "{}"),
        }

    def outcome_calibration(
        self,
        prior_strength: float = 10.0,
        config: ScoringConfig = ScoringConfig(),
    ) -> Dict[str, Any]:
        """Calibrate predicted payout odds against terminal attempt outcomes.

        The prior assumes the model is calibrated and prevents a handful of
        early attempts from causing a large probability swing.
        """
        outcomes = 0
        wins = 0
        predicted_wins = 0.0
        cost_samples = 0
        execution_cost_total = 0.0
        human_hours_total = 0.0
        rows = self.connection.execute(
            """
            SELECT status, score_json, agent_cost_usd, compute_cost_usd,
                   human_hours
            FROM attempts
            WHERE status IN ('merged', 'paid', 'rejected')
               OR (
                    status = 'abandoned'
                    AND (
                        agent_cost_usd > 0
                        OR compute_cost_usd > 0
                        OR human_hours > 0
                        OR EXISTS (
                            SELECT 1 FROM attempt_state_events
                            WHERE attempt_state_events.attempt_id = attempts.id
                              AND attempt_state_events.to_status = 'running'
                        )
                    )
               )
            """
        ).fetchall()
        for row in rows:
            cost_samples += 1
            execution_cost_total += float(row["agent_cost_usd"]) + float(
                row["compute_cost_usd"]
            )
            human_hours_total += float(row["human_hours"])
            try:
                score = json.loads(row["score_json"] or "{}")
                predicted = (
                    float(score["probability_valid"])
                    * float(score["probability_verified"])
                    * float(score["probability_win_merge"])
                )
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(predicted) or predicted < 0 or predicted > 1:
                continue
            outcomes += 1
            predicted_wins += predicted
            if row["status"] in {"merged", "paid"}:
                wins += 1
        prior_strength = max(1.0, float(prior_strength))
        factor = (prior_strength + wins) / (prior_strength + predicted_wins)
        factor = max(0.25, min(1.5, factor))
        execution_cost = (
            prior_strength * config.default_execution_cost_usd
            + execution_cost_total
        ) / (prior_strength + cost_samples)
        human_hours = (
            prior_strength * config.default_human_hours + human_hours_total
        ) / (prior_strength + cost_samples)
        return {
            "method": "bayesian_observed_to_expected",
            "prior_strength": prior_strength,
            "terminal_outcomes": outcomes,
            "wins": wins,
            "predicted_wins": round(predicted_wins, 4),
            "probability_factor": round(factor, 4),
            "cost_samples": cost_samples,
            "default_execution_cost_usd": round(execution_cost, 4),
            "default_human_hours": round(human_hours, 4),
        }

    def summary(self) -> Dict[str, Any]:
        scans = self.connection.execute(
            """
            SELECT COUNT(*) AS count,
                   COALESCE(SUM(opportunity_count), 0) AS opportunities,
                   COALESCE(SUM(eligible_count), 0) AS eligible,
                   MIN(generated_at) AS first_scan_at,
                   MAX(generated_at) AS last_scan_at
            FROM scans
            """
        ).fetchone()
        snapshot_identities = set()
        for row in self.connection.execute(
            "SELECT source, source_id, payload_json FROM opportunity_snapshots"
        ).fetchall():
            issue_url = None
            try:
                payload = json.loads(row["payload_json"])
                issue_url = (payload.get("opportunity") or {}).get("issue_url")
            except (TypeError, ValueError):
                pass
            if isinstance(issue_url, str) and issue_url.startswith(
                "https://github.com/"
            ):
                snapshot_identities.add("github:" + issue_url.lower().rstrip("/"))
            else:
                snapshot_identities.add(f"{row['source']}:{row['source_id']}")
        attempts = self.connection.execute(
            """
            SELECT COUNT(*) AS count,
                   COALESCE(SUM(agent_cost_usd + compute_cost_usd), 0) AS costs,
                   COALESCE(SUM(human_hours), 0) AS human_hours,
                   COALESCE(SUM(payout_usd), 0) AS payouts,
                   COALESCE(SUM(CASE WHEN status IN ('merged', 'paid') THEN 1 ELSE 0 END), 0)
                     AS wins
            FROM attempts
            """
        ).fetchone()
        event_count = self.connection.execute(
            "SELECT COUNT(*) AS count FROM attempt_events"
        ).fetchone()
        state_event_count = self.connection.execute(
            "SELECT COUNT(*) AS count FROM attempt_state_events"
        ).fetchone()
        usage = self.connection.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN kind = 'agent_input_tokens' THEN value ELSE 0 END), 0)
                    AS input_tokens,
                COALESCE(SUM(CASE WHEN kind = 'agent_cached_input_tokens' THEN value ELSE 0 END), 0)
                    AS cached_input_tokens,
                COALESCE(SUM(CASE WHEN kind = 'agent_output_tokens' THEN value ELSE 0 END), 0)
                    AS output_tokens,
                COALESCE(SUM(CASE WHEN kind = 'agent_reasoning_tokens' THEN value ELSE 0 END), 0)
                    AS reasoning_tokens,
                COALESCE(SUM(CASE WHEN kind = 'agent_runs' THEN value ELSE 0 END), 0)
                    AS agent_runs
            FROM attempt_metrics
            """
        ).fetchone()
        direct_costs = float(attempts["costs"])
        payouts = float(attempts["payouts"])
        human_hours = float(attempts["human_hours"])
        human_cost = human_hours * ScoringConfig().human_hour_value_usd
        calibration = self.outcome_calibration()
        evaluation = pilot_evaluation(
            terminal_attempts=calibration["terminal_outcomes"],
            wins=calibration["wins"],
            direct_costs_usd=direct_costs,
            human_hours=human_hours,
            payouts_usd=payouts,
        )
        return {
            "scans": int(scans["count"]),
            "opportunities_observed": int(scans["opportunities"]),
            "distinct_opportunities": len(snapshot_identities),
            "eligible_observations": int(scans["eligible"]),
            "first_scan_at": scans["first_scan_at"],
            "last_scan_at": scans["last_scan_at"],
            "attempts": int(attempts["count"]),
            "attempt_events": int(event_count["count"]),
            "state_events": int(state_event_count["count"]),
            "wins": int(attempts["wins"]),
            "costs_usd": round(direct_costs, 2),
            "payouts_usd": round(payouts, 2),
            "human_hours": round(human_hours, 2),
            "valued_human_cost_usd": round(human_cost, 2),
            "cash_net_income_usd": round(payouts - direct_costs, 2),
            "net_income_usd": round(payouts - direct_costs - human_cost, 2),
            "agent_usage": {
                "runs": int(usage["agent_runs"]),
                "input_tokens": int(usage["input_tokens"]),
                "cached_input_tokens": int(usage["cached_input_tokens"]),
                "output_tokens": int(usage["output_tokens"]),
                "reasoning_tokens": int(usage["reasoning_tokens"]),
            },
            "outcome_calibration": calibration,
            "pilot_evaluation": evaluation,
        }
