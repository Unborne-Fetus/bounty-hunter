import argparse
import json
from typing import Any, Dict

from .history import History


def _load_record(report_path: str, source: str, source_id: str) -> Dict[str, Any]:
    with open(report_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    for record in payload.get("opportunities", []):
        opportunity = record["opportunity"]
        if (
            opportunity["source"] == source
            and opportunity["source_id"] == source_id
        ):
            return record
    raise ValueError("opportunity was not found in the report")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage supervised bounty attempts and outcome accounting."
    )
    parser.add_argument("--history-db", default="data/bounty_hunter.db")
    subparsers = parser.add_subparsers(dest="command", required=True)

    queue = subparsers.add_parser("queue")
    queue.add_argument("--report", default="reports/opportunities.json")
    queue.add_argument("--source", required=True)
    queue.add_argument("--source-id", required=True)
    queue.add_argument(
        "--override-rejection",
        action="store_true",
        help="Queue a rejected opportunity for manual investigation.",
    )

    approve = subparsers.add_parser("approve")
    approve.add_argument("attempt_id", type=int)
    approve.add_argument("--note", required=True)
    approve.add_argument("--record-sha256", required=True)

    review = subparsers.add_parser("review")
    review.add_argument("attempt_id", type=int)

    approve_submission = subparsers.add_parser("approve-submission")
    approve_submission.add_argument("attempt_id", type=int)
    approve_submission.add_argument("--note", required=True)
    approve_submission.add_argument("--bundle-sha256", required=True)

    show = subparsers.add_parser("show")
    show.add_argument("attempt_id", type=int)

    event = subparsers.add_parser("record-event")
    event.add_argument("attempt_id", type=int)
    event.add_argument(
        "kind",
        choices=[
            "agent_cost_usd",
            "compute_cost_usd",
            "human_hours",
            "payout_usd",
        ],
    )
    event.add_argument("amount", type=float)
    event.add_argument("--note", required=True)

    events = subparsers.add_parser("events")
    events.add_argument("attempt_id", type=int)

    states = subparsers.add_parser("states")
    states.add_argument("attempt_id", type=int)

    metrics = subparsers.add_parser("metrics")
    metrics.add_argument("attempt_id", type=int)

    metric = subparsers.add_parser("record-metric")
    metric.add_argument("attempt_id", type=int)
    metric.add_argument("kind")
    metric.add_argument("value", type=float)
    metric.add_argument("--note", required=True)

    transition = subparsers.add_parser("transition")
    transition.add_argument("attempt_id", type=int)
    transition.add_argument(
        "status",
        choices=[
            "running",
            "verified",
            "submission_approved",
            "submitted",
            "revision",
            "merged",
            "paid",
            "rejected",
            "abandoned",
        ],
    )
    transition.add_argument("--agent-cost-usd", type=float)
    transition.add_argument("--compute-cost-usd", type=float)
    transition.add_argument("--human-hours", type=float)
    transition.add_argument("--payout-usd", type=float)
    transition.add_argument("--pr-url")
    transition.add_argument("--note")

    subparsers.add_parser("summary")
    return parser


def main() -> int:
    args = _parser().parse_args()
    history = History(args.history_db)
    try:
        if args.command == "queue":
            record = _load_record(
                args.report, args.source, args.source_id
            )
            if not record["score"]["eligible"] and not args.override_rejection:
                raise ValueError(
                    "opportunity is rejected; use --override-rejection only "
                    "for deliberate manual investigation"
                )
            attempt_id = history.queue_from_record(record)
            print(json.dumps(history.get_attempt(attempt_id), indent=2))
        elif args.command == "approve":
            history.approve(
                args.attempt_id, args.note, args.record_sha256
            )
            print(json.dumps(history.get_attempt(args.attempt_id), indent=2))
        elif args.command == "review":
            digest = history.attempt_record_sha256(args.attempt_id)
            print(
                json.dumps(
                    {
                        "attempt": history.get_attempt(args.attempt_id),
                        "record": history.get_attempt_record(args.attempt_id),
                        "record_sha256": digest,
                        "approval_command": (
                            "python3 -m bounty_hunter.workflow approve "
                            f"{args.attempt_id} --record-sha256 {digest} "
                            '--note "REVIEW_NOTE"'
                        ),
                    },
                    indent=2,
                )
            )
        elif args.command == "approve-submission":
            history.approve_submission(
                args.attempt_id, args.note, args.bundle_sha256
            )
            print(json.dumps(history.get_attempt(args.attempt_id), indent=2))
        elif args.command == "show":
            print(json.dumps(history.get_attempt(args.attempt_id), indent=2))
        elif args.command == "record-event":
            history.record_event(
                args.attempt_id, args.kind, args.amount, args.note
            )
            print(
                json.dumps(
                    {
                        "attempt": history.get_attempt(args.attempt_id),
                        "events": history.attempt_events(args.attempt_id),
                    },
                    indent=2,
                )
            )
        elif args.command == "events":
            print(
                json.dumps(history.attempt_events(args.attempt_id), indent=2)
            )
        elif args.command == "states":
            print(json.dumps(history.state_events(args.attempt_id), indent=2))
        elif args.command == "metrics":
            print(json.dumps(history.attempt_metrics(args.attempt_id), indent=2))
        elif args.command == "record-metric":
            history.record_metric(
                args.attempt_id, args.kind, args.value, args.note
            )
            print(json.dumps(history.attempt_metrics(args.attempt_id), indent=2))
        elif args.command == "transition":
            history.transition(
                args.attempt_id,
                args.status,
                agent_cost_usd=args.agent_cost_usd,
                compute_cost_usd=args.compute_cost_usd,
                human_hours=args.human_hours,
                payout_usd=args.payout_usd,
                pr_url=args.pr_url,
                outcome_note=args.note,
            )
            print(json.dumps(history.get_attempt(args.attempt_id), indent=2))
        elif args.command == "summary":
            print(json.dumps(history.summary(), indent=2))
    finally:
        history.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
