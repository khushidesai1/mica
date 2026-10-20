"""Standalone job entrypoint. Exception text never enters logs or artifacts."""

from __future__ import annotations

import argparse
import getpass
import json
import re
import socket
import sys
from pathlib import Path
from uuid import uuid4

import yaml

from .adapters import GCS, SFTP, saved_login
from .core import (
    Object,
    Transfer,
    backfill,
    destination_objects,
    fingerprint,
    inventory,
    now,
    plan,
    sample_name,
)

CONFIG_ROOT = Path("/opt/atomx/config")
REVISION = Path("/opt/atomx/REVISION")


def put(bucket, name, body):
    existing = bucket.read(name)
    bucket.write(name, body, existing[1] if existing else None)


def run_job(uri: str) -> int:
    match = re.fullmatch(
        r"gs://([a-z0-9._-]+)/tasks/transfers/([a-f0-9]{32})/(inventory|pull)-request.json", uri
    )
    if not match:
        raise ValueError("Invalid job request URI")
    artifact = GCS(match[1])
    prefix = f"tasks/transfers/{match[2]}/{match[3]}"
    loaded = artifact.read(prefix + "-request.json")
    if loaded is None:
        raise ValueError("Request not found")
    request = loaded[0]
    remote = None
    try:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+\.yaml", request["layout"]):
            raise ValueError("Invalid layout")
        config = yaml.safe_load((CONFIG_ROOT / request["layout"]).read_text())
        if request["bucket"] != config["bucket"] or request["run_id"] != match[2]:
            raise ValueError("Request does not match configuration")
        if not re.fullmatch(
            r"projects/[a-z0-9-]+/secrets/[a-zA-Z0-9_-]+/versions/[0-9]+",
            request["login_secret_version"],
        ):
            raise ValueError("An exact secret version is required")
        request["tool_version"] = REVISION.read_text().strip()
        remote = SFTP(config["sftp"], saved_login(request["login_secret_version"]))
        bucket = GCS(config["bucket"])
        if match[3] == "inventory":
            result = inventory(remote, bucket, request)
            put(bucket, "transfer_status/_inventory.json", result)
            put(artifact, prefix + "-result.json", {"status": "done", "inventory": result})
        else:
            result = Transfer(
                remote, bucket, request, lambda p: put(artifact, prefix + "-progress.json", p)
            ).run()
            put(artifact, prefix + "-result.json", {"status": "done", "marker": result})
        return 0
    except Exception as exc:
        from paramiko import AuthenticationException

        status = "auth_failed" if isinstance(exc, AuthenticationException) else "failed"
        result = {"status": status, "error": status, "finished": now(), "run_id": match[2]}
        put(artifact, prefix + "-result.json", result)
        print(json.dumps(result))
        return 1
    finally:
        if remote is not None:
            remote.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Verified AtoMx SFTP → GCS transfers")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("job").add_argument("--request", required=True)
    for name in ("inventory", "pull", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--config", required=True)
        if name != "verify":
            p.add_argument("--export-root", default="")
        if name != "inventory":
            p.add_argument("--sample", required=True)
        if name == "inventory":
            p.add_argument("--publish", action="store_true")
        if name == "pull":
            p.add_argument("--as", dest="as_sample", default="")
            p.add_argument("--yes", action="store_true", help="Confirm the printed transfer plan")
        if name == "verify":
            p.add_argument("--allow-backfilled", action="store_true")
    p = sub.add_parser("backfill", help="Record bucket listings without checking the AtoMx source")
    p.add_argument("--config", required=True)
    p.add_argument("--sample")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Preview only (the default)")
    mode.add_argument("--yes", action="store_true", help="Write create-only completion markers")
    args = parser.parse_args()
    try:
        if args.command == "job":
            return run_job(args.request)
        config = yaml.safe_load(Path(args.config).read_text())
        bucket = GCS(config["bucket"])
        if args.command == "backfill":
            result = backfill(
                bucket,
                {
                    "sample": args.sample,
                    "dry_run": not args.yes,
                    "run_id": uuid4().hex,
                    "tool_version": REVISION.read_text().strip() if REVISION.exists() else "manual",
                    "requested_by": "manual:" + socket.gethostname(),
                },
            )
            print(json.dumps(result))
            return 0
        if args.command == "verify":
            sample = sample_name(args.sample)
            if bucket.read(f"transfer_status/{sample}.in_progress.json"):
                raise ValueError("Sample is still in progress")
            done = bucket.read(f"transfer_status/{sample}.done.json")
            if not done:
                raise ValueError("No done marker")
            if done[0].get("provenance") == "backfill":
                print("Listing-only (backfilled); source not checked, no checksum verification.")
                return 0 if args.allow_backfilled else 1
            if done[0].get("provenance", "transfer") != "transfer":
                raise ValueError("Unknown marker provenance")
            expected = [Object(**x) for x in done[0]["manifest"]]
            actual = destination_objects(bucket, sample)
            if sorted(expected, key=lambda f: f.name) != sorted(actual, key=lambda f: f.name):
                raise ValueError("Manifest mismatch")
            print("Verified: crc32c (destination matches the recorded transfer manifest)")
            return 0
        login = {
            "username": input("AtoMx username: "),
            "password": getpass.getpass("AtoMx password: "),
        }
        remote = SFTP(config["sftp"], login)
        try:
            request = {
                **vars(args),
                "run_id": uuid4().hex,
                "tool_version": "manual",
                "requested_by": "manual:" + socket.gethostname(),
                "login_saved_by": login["username"],
            }
            if args.command == "inventory":
                result = inventory(remote, bucket, request)
                if args.publish:
                    put(bucket, "transfer_status/_inventory.json", result)
                print(json.dumps(result))
            else:
                run, files = plan(
                    remote, args.export_root, args.sample, args.as_sample or args.sample
                )
                request["plan_fingerprint"] = fingerprint(run, files)
                print(
                    json.dumps(
                        {
                            "run_folder": run,
                            "files": len(files),
                            "bytes": sum(f.size for f in files),
                        }
                    )
                )
                if not args.yes and input("Transfer these files? Type yes: ") != "yes":
                    return 0
                Transfer(remote, bucket, request, lambda p: print(json.dumps(p))).run()
            return 0
        finally:
            remote.close()
    except Exception:
        print("Transfer operation failed; check configuration and status markers.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
