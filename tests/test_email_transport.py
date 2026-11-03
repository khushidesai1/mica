"""Email outbox, semantic worker result, authorization and MIME delivery tests."""

# ruff: noqa: F811
import base64
import hashlib
import json
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_slack import bot, event, send  # noqa: F401
from test_gmail_intake import SENDER, email_record, mail, signing  # noqa: F401

from mica.agents.analysis import access, conversation, direct, email_delivery, surface
from mica.agents.analysis.email_delivery import Artifact, TerminalResult, artifact_prefix, deliver
from mica.agents.analysis.registry import Registry
from mica.agents.analysis.transport import EmailWorkerTransport
from mica.api import slack_analysis
from mica.connectors.notifications import ChannelNotifier
from mica.models import ConversationMessage, TaskEnvelope, TaskRecord, TaskSource, TaskStatus


def sent_messages(mail):
    return [
        BytesParser(policy=policy.default).parsebytes(
            base64.urlsafe_b64decode(call.kwargs["body"]["raw"])
        )
        for call in mail.gmail.users().messages().send.call_args_list
    ]


def test_content_fingerprint_survives_wire_encoding_and_gmail_headers():
    original = EmailMessage(policy=policy.SMTP)
    original["From"] = "mi.ca+requests@gmail.com"
    original["To"] = "alice@example.com, bob@example.com"
    original["Subject"] = "Réponse: " + "long project title " * 12
    original["In-Reply-To"] = "<request@example.com>"
    original["Message-ID"] = "<before@example.com>"
    original["X-Mica-Delivery-ID"] = "unique"
    original.set_content("Résultats\n\nLine two.\n")
    original.add_attachment(b"a,b\n1,2\n", maintype="text", subtype="csv", filename="résultats.csv")
    received = BytesParser(policy=policy.SMTP).parsebytes(original.as_bytes())
    received.replace_header("Message-ID", "<changed@mail.gmail.com>")
    received["Date"] = "Tue, 20 Oct 2026 18:00:00 +0000"
    assert email_delivery.content_fingerprint(original) == email_delivery.content_fingerprint(
        received
    )


async def test_only_final_reply_with_membership_and_threading(mail):
    eid, record = await email_record(mail)
    record["payload"]["_email"]["recipients"] = [
        SENDER,
        "bob@example.com",
        "outsider@example.com",
        "mi.ca+requests@gmail.com",
        "mica@gmail.com",
    ]
    Registry(mail.store).update(
        "share", SENDER, "share", lambda d: d.share(mail.pid, SENDER, "bob@example.com")
    )
    await mail.notify(eid, record, "received", "On it")
    await mail.notify(eid, record, "received", "On it")
    for phase in ("replying", "starting", "waiting", "cancelling"):
        await mail.notify(eid, record, phase, "Still working")
    assert sent_messages(mail) == []
    assert mail.store.keys(f"projects/_gmail/outbox/{eid}/") == []
    await mail.notify(eid, record, "complete", "The answer")
    await mail.notify(eid, record, "complete", "The answer")
    messages = sent_messages(mail)
    assert len(messages) == 1
    final = messages[0]
    assert {a.addr_spec for a in final["To"].addresses} == {SENDER, "bob@example.com"}
    assert "outsider@example.com" in final.get_content()
    assert "not project members" in final.get_content()
    assert final["Subject"] == "Example request"
    assert final["In-Reply-To"] == final["References"] == "<request@example.com>"
    assert mail.gmail.users().messages().send.call_args.kwargs["body"]["threadId"] == "abcd"
    mail.test_slack.chat_postMessage.assert_not_awaited()


async def test_email_process_ignores_unconfirmed_old_ack_and_retries_only_final(mail):
    eid, record = await email_record(mail)
    # An acknowledgement lost during an older deployment must not hold up the answer.
    mail.store.put(
        f"projects/_gmail/outbox/{eid}/ack.json", b'{"status":"sending"}'
    )
    assert await mail.process(eid, 0) == "complete"
    assert await mail.process(eid, 0) == "complete"
    messages = sent_messages(mail)
    assert len(messages) == 1
    assert "On it" not in messages[0].get_content()
    assert "liver" in messages[0].get_content().lower()
    assert mail.store.get(f"projects/_gmail/outbox/{eid}/ack.json")[0] == (
        b'{"status":"sending"}'
    )


async def test_slack_keeps_acknowledgement_and_progress(bridge):
    record = {"surface": "slack", "payload": payload(), "status": "pending"}
    await bridge.notify("slack-progress", record, "received", "On it…")
    await bridge.notify("slack-progress", record, "starting", "Starting your worker…")
    assert bridge.test_slack.chat_postMessage.call_args.kwargs["text"] == "On it…"
    assert bridge.test_slack.chat_update.call_args.kwargs["text"] == "Starting your worker…"


async def test_clarification_is_a_substantive_email_response(mail, monkeypatch):
    eid, _ = await email_record(mail)
    # Natural-language routing can finish this turn with a question the user must answer.

    record = await mail.repository.get_connector_state(f"claude-event:{eid}")
    record["payload"]["event"]["text"] = "Analyze my samples"
    await mail.repository.set_connector_state(f"claude-event:{eid}", record)
    monkeypatch.setattr(
        conversation, "run",
        AsyncMock(return_value=conversation.Decision(route="reply", reply="Which samples?")),
    )
    assert await mail.process(eid, 0) == "complete"
    assert len(sent_messages(mail)) == 1
    assert "Which samples?" in sent_messages(mail)[0].get_content()


@pytest.mark.parametrize("kind", ["result", "failure", "cancelled", "input", "approval"])
async def test_legacy_task_email_excludes_prior_progress(mail, monkeypatch, kind):
    monkeypatch.setattr("mica.connectors.notifications.default", lambda **kw: (None, None))
    monkeypatch.setattr("mica.connectors.notifications.build", lambda *a, **kw: mail.gmail)
    task = TaskRecord(
        envelope=TaskEnvelope(
            project_id="project", principal_id=SENDER, source=TaskSource.EMAIL,
            conversation_id="gmail:abcd", prompt="Analyze samples",
        ),
        status=TaskStatus.RUNNING,
        conversation=[ConversationMessage(event_key=k, kind=k, text="Still working")
                      for k in ("acknowledgement", "progress", "replan", "decision")],
    )
    notifier = ChannelNotifier(mail.app, mail.repository)
    await notifier.notify(task)
    assert sent_messages(mail) == []
    task.conversation.append(ConversationMessage(event_key=kind, kind=kind, text="Useful reply"))
    await notifier.notify(task)
    await notifier.notify(task)
    assert len(sent_messages(mail)) == 1
    assert sent_messages(mail)[0].get_content().strip() == "Useful reply"


async def test_transfer_email_sends_required_links_and_terminal_results_only(mail):
    from mica.agents.analysis.transfers import Transfers

    eid, _ = await email_record(mail)
    mail.app.signed_link_secret = "synthetic-email-connection-key-0123456789"
    mail.app.control_plane_url = "https://mica.test"
    transfers = Transfers(mail, mail.store)
    pending = {"id": eid, "state": "inventory", "requested_by": SENDER, "attempt": 0}
    for state in ("inventory", "prepare", "submitting", "running", "cancelling"):
        pending["state"] = state
        await transfers.notify(pending, f"Progress for {state}", ("cancel",))
    assert sent_messages(mail) == []
    assert "delivery" not in pending and "delivery_progress" not in pending
    for state, button in (("login", "login"), ("preview", "confirm")):
        pending["state"] = state
        await transfers.notify(pending, "Your input is required", (button, "cancel"))
    assert len(sent_messages(mail)) == 2
    assert all("/integrations/atomx#" in message.get_content() for message in sent_messages(mail))
    pending["state"] = "cancelling"
    await transfers.notify(pending, "An error needs attention", progress=False)
    pending["state"] = "done"
    await transfers.notify(pending, "Transfer verified")
    await transfers.notify(pending, "Transfer verified")
    assert len(sent_messages(mail)) == 4
    assert "Transfer verified" in sent_messages(mail)[-1].get_content()


async def test_transfer_handoff_does_not_email_preparation_receipt(mail, monkeypatch):
    from mica.agents.analysis.transfer_config import TransferRequest
    from mica.agents.analysis.transfers import Transfers

    eid, record = await email_record(mail)
    record["payload"]["event"]["text"] = "Transfer sample to gs://lab-data/liver/raw/"
    grant = access.resolve(mail.app, mail.settings, mail.store, record)
    transfers = Transfers(mail, mail.store)
    monkeypatch.setattr(transfers, "authorize", Mock())
    monkeypatch.setattr(transfers, "login", AsyncMock(return_value={"status": "valid"}))
    monkeypatch.setattr(transfers, "schedule", AsyncMock())
    request = TransferRequest(sample="sample", destination="gs://lab-data/liver/raw/")
    reply = await transfers.request(grant, record, eid, request)
    assert "Preparing" in reply
    assert await direct.finish(mail, eid, record, reply)
    pending = await transfers.pending(eid)
    assert pending["state"] == "prepare"
    assert sent_messages(mail) == []
    # A retry of the routing turn still hands delivery to the saved transfer controller.
    record.pop("reply_handoff")
    reply = await transfers.request(grant, record, eid, request)
    assert await direct.finish(mail, eid, record, reply)
    assert sent_messages(mail) == []
    pending["state"] = "done"
    await transfers.notify(pending, "Transfer verified")
    assert len(sent_messages(mail)) == 1
    assert "Transfer verified" in sent_messages(mail)[0].get_content()


async def test_one_final_contains_answer_and_artifact_and_checks_ownership(mail):
    eid, record = await email_record(mail)
    data = b"a,b\n1,2\n"
    sha = hashlib.sha256(data).hexdigest()
    key = artifact_prefix(eid, 0) + sha
    mail.store.put(key, data)
    artifact = Artifact(key=key, name="counts.csv", size=len(data), sha256=sha)
    result = TerminalResult(attempt=0, text="Computed answer", artifacts=[artifact])
    await deliver(mail, eid, record, result.text, kind="final", result=result)
    final = sent_messages(mail)[0]
    assert "Computed answer" in final.get_body().get_content()
    attached = list(final.iter_attachments())
    assert len(attached) == 1 and attached[0].get_payload(decode=True) == data
    eid2 = "b" * 32
    with pytest.raises(PermissionError, match="different event"):
        await deliver(mail, eid2, record, result.text, kind="final", result=result)


async def test_mime_budget_omits_files_with_truthful_recovery_message(mail, monkeypatch):
    eid, record = await email_record(mail)
    data = b"x" * 10_000
    sha = hashlib.sha256(data).hexdigest()
    artifact = Artifact(
        key=artifact_prefix(eid, 0) + sha, name="big.csv", size=len(data), sha256=sha
    )
    mail.store.put(artifact.key, data)
    monkeypatch.setattr(email_delivery, "MAX_MIME_BYTES", 5_000)
    await deliver(
        mail,
        eid,
        record,
        "Answer",
        kind="final",
        result=TerminalResult(
            attempt=0,
            text="Answer",
            artifacts=[artifact],
        ),
    )
    final = sent_messages(mail)[0]
    assert not list(final.iter_attachments())
    assert "big.csv" in final.get_content() and "smaller files" in final.get_content()
    assert "retriev" not in final.get_content()


async def test_send_accepted_response_lost_reconciles_without_resending(mail):
    eid, record = await email_record(mail)
    mail.gmail.users().messages().send.return_value.execute.side_effect = TimeoutError(
        "lost response"
    )
    with pytest.raises(TimeoutError):
        await deliver(mail, eid, record, "Answer", kind="final")
    sent = sent_messages(mail)[0]
    mail.gmail.users().messages().send.reset_mock()
    mail.gmail.users().threads().get().execute.return_value = {"messages": []}
    with pytest.raises(RuntimeError, match="unconfirmed"):
        await deliver(mail, eid, record, "Answer", kind="final")
    mail.gmail.users().messages().send.assert_not_called()
    mail.gmail.users().threads().get().execute.return_value = {
        "messages": [
            {
                "id": "accepted",
                "labelIds": ["SENT"],
                "payload": {
                    "headers": [
                        {"name": "Message-ID", "value": "<gmail-rewrote-this@mail.gmail.com>"},
                        {"name": "X-Mica-Delivery-ID", "value": str(sent["X-Mica-Delivery-ID"])},
                    ]
                },
            }
        ]
    }
    sent.replace_header("Message-ID", "<gmail-rewrote-this@mail.gmail.com>")
    mail.mail_messages["accepted"] = {
        "labelIds": ["SENT"],
        "raw": base64.urlsafe_b64encode(sent.as_bytes()).decode(),
    }
    assert await deliver(mail, eid, record, "Answer", kind="final") == "accepted"
    mail.gmail.users().messages().send.assert_not_called()


@pytest.mark.parametrize("labels, count", [([], 1), (["SENT"], 2)])
async def test_reconciliation_rejects_inbound_or_duplicate_delivery_headers(mail, labels, count):
    eid, record = await email_record(mail)
    mail.gmail.users().messages().send.return_value.execute.side_effect = TimeoutError(
        "lost response"
    )
    with pytest.raises(TimeoutError):
        await deliver(mail, eid, record, "Answer", kind="final")
    sent = sent_messages(mail)[0]
    mail.gmail.users().messages().send.reset_mock()
    mail.gmail.users().threads().get().execute.return_value = {
        "messages": [
            {
                "id": str(index),
                "labelIds": labels,
                "payload": {
                    "headers": [
                        {"name": "X-Mica-Delivery-ID", "value": str(sent["X-Mica-Delivery-ID"])},
                    ]
                },
            }
            for index in range(count)
        ]
    }
    with pytest.raises(RuntimeError, match="unconfirmed"):
        await deliver(mail, eid, record, "Answer", kind="final")
    mail.gmail.users().messages().send.assert_not_called()


@pytest.mark.parametrize("change", ["body", "recipient"])
async def test_reconciliation_checks_content_and_recipients(mail, change):
    eid, record = await email_record(mail)
    mail.gmail.users().messages().send.return_value.execute.side_effect = TimeoutError("lost")
    with pytest.raises(TimeoutError):
        await deliver(mail, eid, record, "Answer", kind="final")
    sent = sent_messages(mail)[0]
    mail.gmail.users().messages().send.reset_mock()
    mail.gmail.users().threads().get().execute.return_value = {
        "messages": [
            {
                "id": "different",
                "labelIds": ["SENT"],
                "payload": {
                    "headers": [
                        {"name": "X-Mica-Delivery-ID", "value": str(sent["X-Mica-Delivery-ID"])},
                    ]
                },
            }
        ]
    }
    if change == "body":
        sent.set_content("Different answer")
    else:
        sent.replace_header("To", "outsider@example.com")
    mail.mail_messages["different"] = {
        "labelIds": ["SENT"],
        "raw": base64.urlsafe_b64encode(sent.as_bytes()).decode(),
    }
    with pytest.raises(RuntimeError, match="unconfirmed"):
        await deliver(mail, eid, record, "Answer", kind="final")
    mail.gmail.users().messages().send.assert_not_called()


async def test_revoke_during_run_suppresses_private_answer_and_cc(mail):
    eid, record = await email_record(mail)
    record["payload"]["_email"]["recipients"] = ["bob@example.com"]
    mail.store.put(
        access.checkpoint_key(record["thread_key"]),
        json.dumps(
            {
                "private_owner": SENDER,
                "context": {"project_slug": mail.pid},
            }
        ).encode(),
    )

    def revoke(doc):
        doc.projects[mail.pid].owner = "bob@example.com"
        doc.projects[mail.pid].members = set()
        return mail.pid

    Registry(mail.store).update("revoke", SENDER, "revoke", revoke)
    await deliver(mail, eid, record, "SECRET FIGURE RESULT", kind="final")
    final = sent_messages(mail)[0]
    assert "SECRET" not in final.get_content()
    assert str(final["To"]) == SENDER


async def test_long_direct_context_uses_single_email(mail):
    eid, record = await email_record(mail)
    text = "Complete context. " * 3000
    await direct.show_context(mail, eid, record, mail.store, text)
    assert len(sent_messages(mail)) == 1
    assert sent_messages(mail)[0].get_content().strip() == text.strip()
    mail.test_slack.chat_postMessage.assert_not_awaited()


@pytest.mark.parametrize("notice_ts", [None, "email"])
async def test_worker_uses_semantic_callback_once_with_all_artifacts(bot, notice_ts):
    bot.terminal_delivery = AsyncMock(return_value=("Final text", ["plot.png"]))
    bot.transport = EmailWorkerTransport()
    await send(bot, event("make a plot", _notice_ts=notice_ts))
    assert bot.completed
    bot.terminal_delivery.assert_awaited_once()
    text, uploads, error = bot.terminal_delivery.call_args.args
    assert "fixture.h5ad" in text and uploads[0]["filename"] == "plot.png"
    bot.client.chat_postMessage.assert_not_awaited()
    bot.client.chat_update.assert_not_awaited()
    bot.client.files_upload_v2.assert_not_awaited()


async def test_worker_callback_fences_attempt_and_broker_refuses_email(mail, monkeypatch):
    eid, record = await email_record(mail)
    record["attempt"] = 1
    monkeypatch.setattr(
        slack_analysis, "worker_context", AsyncMock(return_value=(mail, mail.store, record))
    )
    raw = TerminalResult(attempt=0, text="stale").model_dump_json().encode()

    async def receive():
        return {"type": "http.request", "body": raw}

    request = Request({"type": "http", "headers": []}, receive)
    with pytest.raises(HTTPException) as error:
        await slack_analysis.email_result(eid, request)
    assert error.value.status_code == 403
    record["write_grant"] = {"id": "a" * 32}
    with pytest.raises(HTTPException) as error:
        await slack_analysis.write_output(
            eid,
            slack_analysis.WriteUpload(
                relative_paths=["result.csv"],
                grant_id="a" * 32,
            ),
            request,
            Response(),
        )
    assert error.value.status_code == 403 and error.value.detail == surface.EMAIL_READ_ONLY
    mail.gmail.users().messages().send.assert_not_called()


@pytest.mark.parametrize("phase", ["failed", "denied", "cancelled"])
async def test_terminal_stop_is_one_email_without_progress_spam(mail, phase):
    eid, record = await email_record(mail)
    await mail.notify(eid, record, "received", "On it")
    await mail.notify(eid, record, "cancelling", "Stopping")
    await mail.notify(eid, record, phase, "The request stopped")
    await mail.notify(eid, record, phase, "The request stopped")
    assert len(sent_messages(mail)) == 1
    assert "The request stopped" in sent_messages(mail)[-1].get_content()


async def test_personal_transfer_link_email_is_private_and_redacted(mail):
    from mica.agents.analysis.transfers import Transfers

    eid, record = await email_record(mail)
    mail.app.signed_link_secret = "synthetic-email-connection-key-0123456789"
    mail.app.control_plane_url = "https://mica.test"
    mail.settings.transfers_enabled = True
    grant = access.resolve(mail.app, mail.settings, mail.store, record)
    reply = await Transfers(mail, mail.store).request(grant, record, eid, None)
    assert "private link" in reply and "#" not in reply
    messages = sent_messages(mail)
    assert len(messages) == 1 and str(messages[0]["To"]) == SENDER
    assert "/integrations/atomx#" in messages[0].get_content()
    for key in mail.store.keys("projects/_gmail/outbox/"):
        assert b"/integrations/atomx#" not in mail.store.get(key)[0]
    assert await direct.finish(mail, eid, record, reply)
    assert record["status"] == "complete"
    assert len(sent_messages(mail)) == 1


async def test_worker_callback_accepts_current_result_and_returns_saved_delivery(mail, monkeypatch):
    eid, record = await email_record(mail)
    record["attempt"] = 2
    monkeypatch.setattr(
        slack_analysis, "worker_context", AsyncMock(return_value=(mail, mail.store, record))
    )

    async def callback(text):
        raw = TerminalResult(attempt=2, text=text).model_dump_json().encode()

        async def receive():
            return {"type": "http.request", "body": raw}

        return await slack_analysis.email_result(
            eid, Request({"type": "http", "headers": []}, receive)
        )

    first = await callback("Original answer")
    replay = await callback("Different answer from retried worker")
    assert first == replay and replay["text"] == "Original answer"
    assert len(sent_messages(mail)) == 1


async def test_explicit_gmail_rate_limit_retries_safely(mail):
    from googleapiclient.errors import HttpError
    from httplib2 import Response as HttpResponse

    eid, record = await email_record(mail)
    mail.gmail.users().messages().send().execute.side_effect = [
        HttpError(HttpResponse({"status": "429"}), b"rate limited"),
        {"id": "retry-success"},
    ]
    with pytest.raises(HttpError):
        await deliver(mail, eid, record, "Answer", kind="final")
    assert await deliver(mail, eid, record, "Answer", kind="final") == "retry-success"
