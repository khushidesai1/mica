"""Control-plane notices share the one-send CAS outbox and hold ambiguous delivery."""

# ruff: noqa: F811
import base64
import hashlib
import json

import httplib2
import pytest
from googleapiclient.errors import HttpError
from test_analysis_batch import bridge  # noqa: F401
from test_email_transport import sent_messages
from test_gmail_intake import mail, signing  # noqa: F401

from mica.agents.analysis import email_delivery as delivery


async def send(mail, **kwargs):
    return await delivery.notice(
        mail, "test-notice", "target@example.com", "Your access", "Welcome", **kwargs
    )


def outbox(mail):
    key = "projects/_gmail/outbox/notices/" + hashlib.sha256(b"test-notice").hexdigest() + ".json"
    return json.loads(mail.store.get(key)[0])


async def test_new_thread_is_single_recipient_and_idempotent(mail):
    mail.gmail.users().messages().send().execute.return_value = {"id": "c1", "threadId": "beef"}
    mail.gmail.reset_mock()
    assert await send(mail) == {"id": "c1", "thread_id": "beef"}
    assert await send(mail) == {"id": "c1", "thread_id": "beef"}
    assert len(sent_messages(mail)) == 1
    message = sent_messages(mail)[0]
    assert str(message["To"]) == "target@example.com"
    assert message["Cc"] is message["Bcc"] is None
    assert "threadId" not in mail.gmail.users().messages().send.call_args.kwargs["body"]
    with pytest.raises(ValueError, match="different content"):
        await delivery.notice(mail, "test-notice", "other@example.com", "Your access", "Welcome")
    assert len(sent_messages(mail)) == 1


async def test_existing_thread_notice_uses_reply_headers(mail):
    await send(mail, thread_id="abcd", in_reply_to="<request@example.com>")
    message = sent_messages(mail)[0]
    assert message["In-Reply-To"] == message["References"] == "<request@example.com>"
    assert mail.gmail.users().messages().send.call_args.kwargs["body"]["threadId"] == "abcd"


async def test_unknown_send_is_held_without_resend_even_if_search_empty(mail):
    mail.gmail.users().messages().send().execute.side_effect = TimeoutError()
    mail.gmail.reset_mock()
    with pytest.raises(TimeoutError):
        await send(mail)
    for _ in range(2):
        with pytest.raises(delivery.UnconfirmedDelivery):
            await send(mail)
    assert len(sent_messages(mail)) == 1
    assert outbox(mail)["status"] == "sending"


async def test_sent_search_requires_matching_content_and_sent_label(mail):
    mail.gmail.users().messages().send().execute.side_effect = TimeoutError()
    mail.gmail.reset_mock()
    with pytest.raises(TimeoutError):
        await send(mail)
    raw = mail.gmail.users().messages().send.call_args.kwargs["body"]["raw"]
    mail.gmail.users().messages().list().execute.return_value = {"messages": [{"id": "c1"}]}
    mail.mail_messages["c1"] = {"id": "c1", "threadId": "beef", "raw": raw, "labelIds": ["INBOX"]}
    with pytest.raises(delivery.UnconfirmedDelivery):
        await send(mail)
    mail.mail_messages["c1"]["labelIds"] = ["SENT"]
    mail.mail_messages["c1"]["raw"] = base64.urlsafe_b64encode(
        base64.urlsafe_b64decode(raw).replace(b"Welcome", b"Tampered")
    ).decode()
    with pytest.raises(delivery.UnconfirmedDelivery):
        await send(mail)
    mail.mail_messages["c1"]["raw"] = raw
    assert await send(mail) == {"id": "c1", "thread_id": "beef"}
    assert len(sent_messages(mail)) == 1


async def test_known_rejection_allows_retry(mail):
    mail.gmail.users().messages().send().execute.side_effect = HttpError(
        httplib2.Response({"status": 429}), b"rate limited"
    )
    mail.gmail.reset_mock()
    with pytest.raises(HttpError):
        await send(mail)
    mail.gmail.users().messages().send().execute.side_effect = None
    mail.gmail.users().messages().send().execute.return_value = {"id": "c1", "threadId": "beef"}
    assert await send(mail) == {"id": "c1", "thread_id": "beef"}


@pytest.mark.parametrize(
    "recipient",
    ["a@example.com,b@example.com", "group:a@example.com", "a@example.com\nBcc: b@example.com"],
)
async def test_notice_rejects_multi_recipient_or_injected_address(mail, recipient):
    with pytest.raises(ValueError):
        await delivery.notice(mail, "bad", recipient, "Access", "Welcome")
    mail.gmail.users().messages().send.assert_not_called()
