from __future__ import annotations

import time

import pytest

from mica.connectors.slack import verify_slack_request
from mica.execution.validation import ScriptRejected, validate_script


@pytest.mark.parametrize(
    "script",
    [
        '"""Bad."""\nimport argparse, requests\nrequests.get("https://example.org")',
        '"""Bad."""\nimport argparse, socket\nsocket.socket()',
        '"""Bad."""\nimport argparse\nfrom google.cloud import storage\nstorage.Client()',
        '"""Bad."""\nimport argparse, gcsfs\ngcsfs.GCSFileSystem()',
        '"""Bad."""\nimport argparse\n# git push origin main\nprint("no")',
    ],
)
def test_generated_script_blocks_network_and_git_writes(script: str) -> None:
    with pytest.raises(ScriptRejected):
        validate_script(script)


def test_generated_script_requires_real_data_preflight_mode() -> None:
    script = '''"""Missing real-data preflight mode."""
import argparse
from pathlib import Path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.parse_args()

if __name__ == "__main__":
    main()
'''
    with pytest.raises(ScriptRejected, match=r"--preflight"):
        validate_script(script, require_preflight=True)


def test_slack_signature_and_replay_window() -> None:
    import hashlib
    import hmac

    body = b'{"type":"event_callback"}'
    timestamp = str(int(time.time()))
    secret = "secret"
    signature = (
        "v0="
        + hmac.new(
            secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
        ).hexdigest()
    )
    assert verify_slack_request(body, timestamp, signature, secret)
    assert not verify_slack_request(body, str(int(timestamp) - 301), signature, secret)
