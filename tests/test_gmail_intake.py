# ruff: noqa: F811
"""Offline Gmail authentication, history recovery and shared-agent admission."""

import base64
import json
from email.message import EmailMessage
from email.policy import SMTP
from types import SimpleNamespace
from unittest.mock import Mock

import dkim
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from googleapiclient.errors import HttpError
from httplib2 import Response
from pydantic import SecretStr
from test_analysis_batch import bridge  # noqa: F401

from mica.agents.analysis import access, attachments
from mica.agents.analysis.config import CHECKPOINT_PREFIX
from mica.agents.analysis.registry import Binding, RegistryDoc
from mica.connectors import gmail, gmail_client

SENDER = "alice@example.com"


def message_bytes(sender=SENDER, attachment=False, text="projects"):
    msg = EmailMessage(policy=SMTP)
    msg["From"], msg["To"] = sender, "mica+requests@gmail.com"
    msg["Subject"], msg["Message-ID"] = "Example request", "<request@example.com>"
    msg.set_content(text)
    if attachment:
        msg.add_attachment(b"a,b\n1,2\n", maintype="text", subtype="csv", filename="counts.csv")
    return msg.as_bytes()


@pytest.fixture
def signing(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    dns = b"v=DKIM1; k=rsa; p=" + base64.b64encode(public)
    verify = dkim.DKIM.verify
    monkeypatch.setattr(
        dkim.DKIM,
        "verify",
        lambda self, idx=0, dnsfunc=None: verify(self, idx=idx, dnsfunc=lambda *a, **k: dns),
    )

    def sign(raw):
        return (
            dkim.sign(
                raw,
                b"mica",
                b"example.com",
                private,
                include_headers=[b"from", b"to", b"subject", b"message-id"],
            )
            + raw
        )

    return sign


@pytest.fixture
def mail(bridge, monkeypatch, signing):
    bridge.app.gmail_account = "mi.ca+requests@gmail.com"
    bridge.app.gmail_label = "Mica"
    bridge.app.gmail_oauth_client_id = "test-client"
    bridge.app.gmail_oauth_client_secret = SecretStr("test-secret")
    bridge.app.gmail_refresh_token = SecretStr("test-refresh")
    api = Mock()
    messages = {}
    api.users.return_value.messages.return_value.get.side_effect = lambda id, **kw: Mock(
        execute=lambda: messages[id]
    )
    api.users.return_value.labels.return_value.list.return_value.execute.return_value = {
        "labels": [{"id": "Label_1", "name": "Mica"}]
    }
    api.users.return_value.getProfile.return_value.execute.return_value = {
        "historyId": "100",
        "emailAddress": "mi.ca@gmail.com",
    }
    api.users.return_value.messages.return_value.send.return_value.execute.return_value = {
        "id": "sent1"
    }
    api.users.return_value.messages.return_value.list.return_value.execute.return_value = {}
    monkeypatch.setattr(gmail_client, "service", lambda settings: api)
    bridge.gmail, bridge.mail_messages = api, messages

    def add(mid="a1", sender=SENDER, **kwargs):
        raw = signing(
            message_bytes(sender, kwargs.pop("attachment", False), kwargs.pop("text", "projects"))
        )
        messages[mid] = {
            "id": mid,
            "threadId": "abcd",
            "internalDate": "1000000",
            "labelIds": ["Label_1"],
            "sizeEstimate": len(raw),
            "raw": base64.urlsafe_b64encode(raw).decode(),
            **kwargs,
        }
        return messages[mid]

    bridge.add_message = add
    return bridge


def test_dkim_cryptographic_boundary(signing):
    raw = signing(message_bytes())
    assert gmail_client.verified_message(raw)[0] == SENDER
    forged = b"Authentication-Results: mx.google.com; dkim=pass header.d=example.com\r\n"
    assert (
        gmail_client.verified_message(forged + raw.replace(b"projects", b"share private")) is None
    )
    assert gmail_client.verified_message(forged + message_bytes()) is None
    assert gmail_client.verified_message(b"From: bob@example.com\r\n" + raw) is None
    assert gmail_client.verified_message(signing(message_bytes("alice@other.example"))) is None
    assert gmail_client.verified_message(raw.replace(b"<request@example.com>", b"bad id")) is None
    assert (
        gmail_client.verified_message(raw.replace(b"alice@example.com", b"mallory@example.com"))
        is None
    )


async def test_reply_command_excludes_signature_and_quoted_history(mail):
    mail.add_message(
        text=(
            "projects\r\n\r\nSincerely,\r\nAlice Example\r\nColumbia University\r\n"
            "On Oct 20, 2026 at 12:26 AM, Mica wrote:\r\n> On it…\r\n"
            "> share private outsider@example.com\r\n"
        )
    )
    await gmail.ingest(mail, mail.gmail, "a1", "Label_1", 0)
    record = next(
        v for k, v in mail.repository.connector_state.items() if k.startswith("claude-event:")
    )
    assert record["payload"]["event"]["text"] == "projects"
    assert "> share private" in record["payload"]["_email"]["original_text"]
    assert record["intake_principal"] == SENDER


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Please compare A and B.\n\nKeep all rows.\n\n-- \nAlice",
            "Please compare A and B.\n\nKeep all rows.",
        ),
        ("projects\n\nSent with Spark", "projects"),
        (
            "On Monday Mica wrote:\n> Prior question\n\nUse all samples.",
            "On Monday Mica wrote:\n> Prior question\n\nUse all samples.",
        ),
        ("> projects\n\nWhat does that command do?", "> projects\n\nWhat does that command do?"),
        ("Sincerely,\nPlease analyze this.", "Sincerely,\nPlease analyze this."),
        (
            "Please keep the line\nRegards,\ninside the example.",
            "Please keep the line\nRegards,\ninside the example.",
        ),
        ("On Monday Mica wrote:\n> share private outsider@example.com", ""),
        ("> projects", "> projects"),
        (
            "Forwarded message:\nprojects\n\nUse this as a formatting example.",
            "Forwarded message:\nprojects\n\nUse this as a formatting example.",
        ),
        (
            "On Monday Mica wrote:\n> Old request\n\nBest,\nAlice\n\nUse all samples.",
            "On Monday Mica wrote:\n> Old request\n\nBest,\nAlice\n\nUse all samples.",
        ),
        (
            "Here is a literal example:\n```\nprojects\n\n-- \nAlice\n```\nExplain it.",
            "Here is a literal example:\n```\nprojects\n\n-- \nAlice\n```\nExplain it.",
        ),
    ],
)
def test_request_text_preserves_new_instructions(text, expected):
    assert gmail_client.request_text(text) == expected


async def test_unique_message_and_sender_keys_and_replay(mail):
    mail.add_message("a1")
    mail.add_message("a2")  # identical internalDate and Gmail thread
    mail.add_message("a3", sender="bob@example.com")
    for mid in ("a1", "a2", "a3", "a1"):
        await gmail.ingest(mail, mail.gmail, mid, "Label_1", 0)
    receipts = [
        v for k, v in mail.repository.connector_state.items() if k.startswith("claude-event:")
    ]
    assert len(receipts) == 3
    alice = [r for r in receipts if r["intake_principal"] == SENDER]
    bob = next(r for r in receipts if r["intake_principal"] != SENDER)
    assert alice[0]["thread_key"] == alice[1]["thread_key"] != bob["thread_key"]
    assert all(r["surface"] == "email" for r in receipts)


@pytest.mark.parametrize(
    "change",
    [
        {"labelIds": []},
        {"labelIds": ["Label_1", "SENT"]},
        {"internalDate": "1"},
        {"raw": base64.urlsafe_b64encode(message_bytes()).decode()},
    ],
)
async def test_ineligible_mail_never_accepts_or_replies(mail, change):
    mail.add_message(**change)
    await gmail.ingest(mail, mail.gmail, "a1", "Label_1", 100)
    assert not mail.repository.connector_state
    mail.gmail.users().messages().send.assert_not_called()


async def email_record(mail):
    mail.add_message()
    await gmail.ingest(mail, mail.gmail, "a1", "Label_1", 0)
    return next(
        (k.split(":", 1)[1], v)
        for k, v in mail.repository.connector_state.items()
        if k.startswith("claude-event:") and v.get("surface") == "email"
    )


async def test_member_identity_ignores_slack_allowlist_and_isolates_private_context(
    mail, monkeypatch
):
    eid, record = await email_record(mail)
    mail.app.allowed_emails = ("unrelated@example.com",)
    monkeypatch.setattr(access, "resolve_email", Mock(side_effect=AssertionError("Slack lookup")))
    grant = access.resolve(mail.app, mail.settings, mail.store, record)
    assert grant.allowed and grant.email == SENDER
    mail.store.put(
        access.checkpoint_key(record["thread_key"]),
        json.dumps(
            {
                "private_owner": "bob@example.com",
                "context": {"project_slug": mail.pid},
                "secret": "private",
            }
        ).encode(),
    )
    denied = access.resolve(mail.app, mail.settings, mail.store, record)
    assert not denied.allowed and denied.state == {} and not denied.email


async def test_email_attachment_uses_common_limits(mail):
    mail.add_message(attachment=True)
    await gmail.ingest(mail, mail.gmail, "a1", "Label_1", 0)
    eid, record = next(
        (k.split(":", 1)[1], v)
        for k, v in mail.repository.connector_state.items()
        if k.startswith("claude-event:") and v.get("surface") == "email"
    )
    files = attachments.ingest_email(
        mail.store, record, "projects/_users/test/runs/proj/thread", eid, mail.settings
    )
    assert files[0].name == "counts.csv" and files[0].kind == "table"
    assert mail.store.get(files[0].key)[0] == b"a,b\n1,2\n"
    mail.settings.attachment_max_files = 1
    mail.settings.attachment_max_mb = 0
    with pytest.raises(attachments.AttachmentError):
        attachments.ingest_email(mail.store, record, "projects/test", eid, mail.settings)


async def test_history_replays_page_after_midpage_failure_and_processes_label_additions(mail):
    key = gmail.state_key(gmail_client.mailbox(mail.app))
    mail.store.put(key, json.dumps({"history_id": "90", "since": 0}).encode())
    mail.add_message("a1")
    history = mail.gmail.users().history().list().execute
    history.return_value = {
        "historyId": "100",
        "history": [
            {
                "messagesAdded": [{"message": {"id": "a1"}}],
                "labelsAdded": [{"message": {"id": "a2"}}],
            },
        ],
    }
    with pytest.raises(KeyError):
        await gmail.consume(mail, mail.gmail, "100")
    assert json.loads(mail.store.get(key)[0])["history_id"] == "90"
    mail.add_message("a2")
    await gmail.consume(mail, mail.gmail, "100")
    assert json.loads(mail.store.get(key)[0])["history_id"] == "100"
    assert len([k for k in mail.repository.connector_state if k.startswith("claude-event:")]) == 2
    before = history.call_count
    await gmail.consume(mail, mail.gmail, "95")
    assert history.call_count == before


async def test_watch_renewal_preserves_pending_cursor_and_expiry_resync_is_bounded(mail):
    key = gmail.state_key(gmail_client.mailbox(mail.app))
    mail.store.put(key, json.dumps({"history_id": "80", "since": 500}).encode())
    mail.gmail.users().watch().execute.return_value = {"historyId": "110"}
    await gmail.renew_watch(SimpleNamespace(settings=mail.app, repository=mail.repository))
    assert json.loads(mail.store.get(key)[0])["history_id"] == "80"
    mail.add_message()
    mail.gmail.users().history().list().execute.side_effect = HttpError(
        Response({"status": "404"}), b"expired"
    )
    mail.gmail.users().messages().list().execute.return_value = {"messages": [{"id": "a1"}]}
    await gmail.consume(mail, mail.gmail, "100")
    assert json.loads(mail.store.get(key)[0])["history_id"] == "100"
    assert mail.gmail.users().messages().list.call_args.kwargs["q"] == "after:0"


def test_legacy_default_conflicts_switching_and_revocation():
    doc = RegistryDoc()
    first = doc.create(SENDER, "first", "gs://lab-data/")
    second = doc.create(SENDER, "second", "gs://lab-data/")
    doc.bindings["T1:D1"] = Binding(project_id=first, bound_by=SENDER)
    assert doc.active_for(SENDER, "EMAIL", "E1", private=True) == first
    doc.bindings["T2:D2"] = Binding(project_id=second, bound_by=SENDER)
    assert doc.active_for(SENDER, "EMAIL", "E1", private=True) is None
    doc.bind("EMAIL", "E1", SENDER, second, private=True)
    assert doc.active_for(SENDER, "T1", "D1", private=True) == second
    doc.bind("T1", "D1", SENDER, first, private=True)
    assert doc.active_for(SENDER, "EMAIL", "E1", private=True) == first
    del doc.projects[first]
    assert doc.active_for(SENDER, "EMAIL", "E1", private=True) is None


async def test_email_compute_has_no_write_capability_even_with_active_project_grant(mail):
    from test_analysis_write_flow import allow

    from mica.agents.analysis.batch import BatchBridge

    mail.settings.write_admin_ids = "T1:U1"
    mail.settings.write_allowed_prefixes = "gs://lab-data/liver/output/"
    mail.settings.write_service_account = "writer@example.com"
    assert await allow(mail)
    eid, record = await email_record(mail)
    # Route a real email turn through the shared controller; the fake model chooses compute.
    record["payload"]["event"]["text"] = "Analyze the data"
    await mail.repository.set_connector_state(f"claude-event:{eid}", record)
    original = BatchBridge.resolve_data
    BatchBridge.resolve_data = lambda self, record: (self.settings, {"lab-data"})
    try:
        await mail.process(eid, 0)
    finally:
        BatchBridge.resolve_data = original
    manifest = json.loads(mail.store.get(f"{CHECKPOINT_PREFIX}/events/{eid}/request.json")[0])
    state = json.loads(mail.store.get(access.checkpoint_key(record["thread_key"]))[0])
    assert manifest["surface"] == "email" and manifest["write_grant"] is None
    assert state["private_owner"] == SENDER
    assert access.worker_permitted(mail.store, manifest, state, settings=mail.settings)
    manifest["write_grant"] = {"id": "forged"}
    assert not access.worker_permitted(mail.store, manifest, state, settings=mail.settings)
    manifest["write_grant"] = None
    state["private_owner"] = "bob@example.com"
    assert not access.worker_permitted(mail.store, manifest, state, settings=mail.settings)


async def test_first_watch_sets_one_activation_cutoff_and_disabled_watch_is_noop(mail):
    key = gmail.state_key(gmail_client.mailbox(mail.app))
    mail.gmail.users().watch().execute.return_value = {"historyId": "110"}
    services = SimpleNamespace(settings=mail.app, repository=mail.repository)
    await gmail.renew_watch(services)
    state = json.loads(mail.store.get(key)[0])
    assert state["since"] > 0 and state["history_id"] == "100"
    mail.gmail.users().getProfile().execute.return_value = {
        "historyId": "120",
        "emailAddress": "mi.ca@gmail.com",
    }
    await gmail.renew_watch(services)
    assert json.loads(mail.store.get(key)[0]) == state
    mail.app.gmail_refresh_token = SecretStr("")
    assert await gmail.renew_watch(services) == {"enabled": False}


async def test_overlapping_consumers_cannot_advance_cursor(mail):
    import hashlib

    key = gmail.state_key(gmail_client.mailbox(mail.app))
    state = {"history_id": "80", "since": 0}
    mail.store.put(key, json.dumps(state).encode())
    mutex = "gmail-consume:" + hashlib.sha256(gmail_client.mailbox(mail.app).encode()).hexdigest()
    claim = await mail.repository.begin_event(mutex, lease_seconds=300)
    with pytest.raises(RuntimeError, match="busy"):
        await gmail.consume(mail, mail.gmail, "100")
    assert json.loads(mail.store.get(key)[0]) == state
    await mail.repository.release_event(mutex, claim.token)


async def test_foreign_mailbox_and_untrusted_pubsub_identity_are_rejected(mail):
    from fastapi import HTTPException
    from starlette.requests import Request

    services = SimpleNamespace(settings=mail.app, repository=mail.repository)
    raw = json.dumps(
        {
            "message": {
                "data": base64.b64encode(
                    json.dumps(
                        {
                            "emailAddress": "stranger@gmail.com",
                            "historyId": "100",
                        }
                    ).encode()
                ).decode()
            }
        }
    ).encode()

    async def receive():
        return {"type": "http.request", "body": raw}

    request = Request(
        {
            "type": "http",
            "headers": [],
            "app": SimpleNamespace(state=SimpleNamespace(services=services)),
        },
        receive,
    )
    with pytest.raises(HTTPException) as error:
        await gmail.gmail_push(request, SimpleNamespace(id="attacker@example.com"))
    assert error.value.status_code == 403
    with pytest.raises(HTTPException) as error:
        await gmail.gmail_push(request, SimpleNamespace(id=mail.app.cloud_tasks_service_account))
    assert error.value.status_code == 400
    mail.gmail.users().history().list.assert_not_called()


@pytest.mark.parametrize(
    "address",
    ["mica@gmail.com", "mi.ca@gmail.com", "mica+any@gmail.com", "mi.ca+requests@googlemail.com"],
)
def test_all_consumer_mailbox_aliases_are_excluded(mail, address):
    assert gmail_client.is_own(address, mail.app)


def test_transient_dns_failure_retries_instead_of_discarding(monkeypatch):
    import dns.exception

    monkeypatch.setattr(
        gmail_client.dns.resolver, "resolve", Mock(side_effect=dns.exception.Timeout)
    )
    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        gmail_client.dns_txt(b"test._domainkey.example.com")


async def test_watch_rejects_oauth_credentials_for_another_mailbox(mail):
    mail.gmail.users().getProfile().execute.return_value = {
        "historyId": "100",
        "emailAddress": "foreign@gmail.com",
    }
    with pytest.raises(ValueError, match="different Gmail mailbox"):
        await gmail.renew_watch(SimpleNamespace(settings=mail.app, repository=mail.repository))
    mail.gmail.users().watch.assert_not_called()
