"""Control migration admission and replay durable Slack receipts without printing content.

Uses the operator's ADC for Firestore and gcloud impersonation for the internal
replay endpoint. It never changes IAM, routing, queues, secrets, or worker state.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

PREFIX = "migration-slack:"


def service_name(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,63}", value):
        raise argparse.ArgumentTypeError("Expected a Cloud Run service name")
    return value


def control(client: Any, key: str, field: str, value: str, expected: str) -> None:
    reference = client.collection("connector_state").document(key)

    @firestore.transactional
    def update(transaction: Any) -> None:
        snapshot = reference.get(transaction=transaction)
        current = snapshot.to_dict() or {}
        if current.get(field, "missing") != expected:
            raise ValueError("Control changed; inspect status before retrying")
        # Never hand off ownership while a replay request still holds its lease.
        lock = client.collection("events").document("migration-replay-lock").get(
            transaction=transaction
        ).to_dict() or {}
        if lock.get("status") == "processing" and lock.get(
            "lease_expires_at", datetime.min.replace(tzinfo=UTC)
        ) > datetime.now(UTC):
            raise ValueError("A replay request is still active; wait for it to finish")
        transaction.set(reference, {field: value, "updated": time.time()})

    update(client.transaction())


def receipts(client: Any) -> list[dict[str, Any]]:
    collection = client.collection("connector_state")
    query = collection.where(
        filter=FieldFilter("__name__", ">=", collection.document(PREFIX))
    ).where(filter=FieldFilter("__name__", "<", collection.document(PREFIX + "\uf8ff")))
    # Only select metadata: operator status/replay never downloads message contents.
    return [
        {"id": item.id.removeprefix(PREFIX), **(item.to_dict() or {})}
        for item in query.select(["status", "created"]).stream()
    ]


def replay(client: Any, url: str, account: str, limit: int) -> int:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}
        or parsed.query or parsed.fragment or parsed.username or parsed.password
    ):
        raise ValueError("Replay URL must be an HTTPS service origin")
    if not re.fullmatch(r"[a-zA-Z0-9._-]+@[a-zA-Z0-9.-]+\.iam\.gserviceaccount\.com", account):
        raise ValueError("Expected the configured control service account")
    origin = url.rstrip("/")
    token = subprocess.run(
        ["gcloud", "auth", "print-identity-token", f"--impersonate-service-account={account}",
         f"--audiences={origin}", "--include-email"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    pending = sorted(
        (item for item in receipts(client) if item.get("status") == "buffered"),
        key=lambda item: (item.get("created", 0), item["id"]),
    )
    with httpx.Client(timeout=250, follow_redirects=False) as session:
        for item in pending[:limit]:
            result = session.post(
                f"{origin}/api/v1/internal/maintenance/replay-slack/{item['id']}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if result.status_code != 200 or result.json().get("status") != "replayed":
                # Never print a response body: upstream errors may include private content.
                raise RuntimeError(
                    f"Replay stopped with HTTP {result.status_code}; receipt retained"
                )
    return min(len(pending), limit)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status")
    status.add_argument("--service", required=True, type=service_name)
    set_mode = commands.add_parser("set-mode")
    set_mode.add_argument("--service", required=True, type=service_name)
    set_mode.add_argument("--mode", required=True, choices=("open", "draining", "frozen"))
    set_mode.add_argument(
        "--expect", required=True, choices=("missing", "open", "draining", "frozen")
    )
    owner = commands.add_parser("set-replay-owner")
    owner.add_argument("--service", required=True, type=service_name)
    owner.add_argument("--expect", required=True, help="Previous owner service or 'missing'")
    drain = commands.add_parser("replay")
    drain.add_argument("--url", required=True)
    drain.add_argument("--control-account", required=True)
    drain.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    client = firestore.Client(project=args.project)
    if args.command == "status":
        state = client.collection("connector_state").document(
            f"migration-admission:{args.service}"
        ).get().to_dict() or {}
        replay_owner = client.collection("connector_state").document(
            "migration-replay"
        ).get().to_dict() or {}
        rows = receipts(client)
        print(json.dumps({
            "service": args.service, "mode_override": state.get("mode"),
            "replay_service": replay_owner.get("service"),
            "buffered": sum(item.get("status") == "buffered" for item in rows),
            "replayed": sum(item.get("status") == "replayed" for item in rows),
        }))
    elif args.command == "set-mode":
        control(client, f"migration-admission:{args.service}", "mode", args.mode, args.expect)
        print(json.dumps({"service": args.service, "mode": args.mode}))
    elif args.command == "set-replay-owner":
        control(client, "migration-replay", "service", args.service, args.expect)
        print(json.dumps({"replay_service": args.service}))
    else:
        if not 1 <= args.limit <= 1000:
            parser.error("--limit must be between 1 and 1000")
        print(json.dumps({"replayed": replay(client, args.url, args.control_account, args.limit)}))


if __name__ == "__main__":
    main()
