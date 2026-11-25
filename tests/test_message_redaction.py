"""Message redaction retains context without keeping alternate credential copies."""

import json

import pytest

from mica.agents.analysis.transfers import redact_passwords, scrub_password_paste


@pytest.mark.parametrize(("text", "expected"), [
    ("password: abc", "password: [removed]"),
    ("My config has pwd=abc and fails\nWhy?", "My config has pwd: [removed]\nWhy?"),
    ("PASSWORD : 'two words'\r\nnext", "password: [removed]\r\nnext"),
    ("passwd=\"two words\"", "passwd: [removed]"),
    ("password:a\npwd=b\npasswd:c", "password: [removed]\npwd: [removed]\npasswd: [removed]"),
    ("password:a pwd=b\nQuestion", "password: [removed]\nQuestion"),
    ("password:\nWhy is it empty?", "password: [removed]\nWhy is it empty?"),
    ("password\n: this is another line", "password\n: this is another line"),
    ("The password is missing; token=example", "The password is missing; token=example"),
])
def test_password_redaction_is_line_bounded_and_keeps_keyword_set(text, expected):
    redacted, removed = redact_passwords(text)
    assert redacted == expected
    assert removed == (text != expected)
    assert redact_passwords(redacted)[0] == redacted


async def test_email_context_only_password_sets_flag_without_mutating_input():
    payload = {"event": {"text": "List folders"}, "_email": {
        "original_text": "List folders\n\nBest,\npwd=synthetic-secret",
        "trailing_text": "Best,\npwd=synthetic-secret",
        "quoted_text": "On Monday Mica wrote:\n> password=quoted-secret",
    }}
    redacted = await scrub_password_paste(None, payload)
    assert "synthetic-secret" in json.dumps(payload)
    assert "synthetic-secret" not in json.dumps(redacted)
    assert "quoted-secret" not in json.dumps(redacted)
    assert redacted["event"]["atomx_password_removed"] is True
    assert redacted["event"]["text"] == "List folders"
