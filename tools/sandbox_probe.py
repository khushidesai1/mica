"""Run through the agent's sandbox on the approved VM; never writes to data."""

import argparse
import errno
import json
import os
import socket
from pathlib import Path

import httpx


def blocked_write(path):
    try:
        # No creation or truncation: even an unexpected success cannot alter data.
        fd = os.open(path, os.O_WRONLY)
    except OSError as exc:
        assert exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS}, exc
    else:
        os.close(fd)
        raise AssertionError("Sandbox permitted write access to mounted data")


def blocked_connection(host, port):
    try:
        with socket.create_connection((host, port), timeout=5):
            pass
    except OSError:
        pass
    else:
        raise AssertionError("Sandbox permitted a direct network connection")


def blocked_http(url, headers=None):
    try:
        response = httpx.get(url, headers=headers, timeout=5)
    except httpx.HTTPError:
        pass
    else:
        assert response.status_code >= 400, "Sandbox permitted a proxied network request"


def probe(data_file, outside):
    with Path(data_file).open("rb") as handle:
        assert handle.read(1), "Cannot read mounted data"
    assert all(
        not os.environ.get(k)
        for k in (
            "SLACK_BOT_TOKEN",
            "SLACK_APP_TOKEN",
            "PISTACHIO_SLACK_BOT_TOKEN",
            "ANTHROPIC_API_KEY",
        )
    ), "Credential exposed to generated code"
    outside = Path(outside)
    try:
        with outside.open("x") as handle:
            handle.write("sandbox probe")
    except OSError as exc:
        assert exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS}, exc
    else:
        outside.unlink()
        raise AssertionError("Sandbox permitted a write outside the workspace")
    blocked_write(data_file)
    blocked_connection("1.1.1.1", 443)
    blocked_connection("169.254.169.254", 80)
    blocked_http("https://example.com")
    blocked_http(
        "http://metadata.google.internal/computeMetadata/v1/instance/id",
        headers={"Metadata-Flavor": "Google"},
    )
    # A private PID namespace must not expose credentials held by the trusted parent.
    for environ in Path("/proc").glob("[0-9]*/environ"):
        try:
            entries = environ.read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        for entry in entries:
            name, _, value = entry.partition(b"=")
            if name in {b"ANTHROPIC_API_KEY", b"SLACK_BOT_TOKEN", b"PISTACHIO_SLACK_BOT_TOKEN"}:
                assert not value, "Credential exposed through the process filesystem"
    with Path("outputs/sandbox-probe.json").open("w") as handle:
        json.dump(
            {
                "data_read": True,
                "slack_tokens_empty": True,
                "outside_write_blocked": True,
                "network_blocked": True,
                "data_write_blocked": True,
                "metadata_blocked": True,
                "proxy_network_blocked": True,
                "parent_credentials_hidden": True,
            },
            handle,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_file")
    parser.add_argument(
        "outside", help="Nonexistent file inside an existing, service-user-owned directory"
    )
    args = parser.parse_args()
    probe(args.data_file, args.outside)
