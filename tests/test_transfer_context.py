"""Context supplies proposed paths; only the frozen private preview authorizes a copy."""

# ruff: noqa: F811
import copy
import json
from unittest.mock import AsyncMock, Mock

import pytest
from test_analysis_batch import bridge  # noqa: F401
from test_atomx_delivery import private_record  # noqa: F401
from test_conversation_tools import Client, run, tool, turn  # noqa: F401
from test_gmail_intake import email_record, mail, signing  # noqa: F401
from test_transfer_cards import OWNER, transfer  # noqa: F401

from mica.agents.analysis import atomx_connections, conversation, direct, transfer_destinations
from mica.agents.analysis.registry import RegistryDoc
from mica.agents.analysis.transfer_config import TransferProject, TransferRequest
from mica.agents.analysis.transfers import Transfers


@pytest.fixture
def project():
    doc = RegistryDoc()
    pid = doc.create(OWNER, "project", "gs://lab-data/project/")
    doc.share(pid, OWNER, "collaborator@example.com")
    return doc, pid


def test_realistic_guide_prose_does_not_invent_a_second_raw_folder(project):
    doc, pid = project
    guide = """Data location: `gs://lab-data/project/`
AtoMx export: `raw_outputs/raw/<TMA>/*_metadata_file.csv.gz`
Raw outputs: `gs://lab-data/project/raw_outputs/raw/<TMA>/`
Additional files: `gs://lab-data/project/raw_outputs/additional_files/<TMA>/`
Images rebuilt by image-only ingest from raw Morphology2D.
Legacy examples: gs://lab-data/invalid/*
"""
    config = transfer_destinations.resolve(
        TransferRequest(sample="TMA37"), guide, "Start this transfer", doc, pid, OWNER,
    )
    assert config.raw_prefix == "project/raw_outputs/raw/"
    assert config.additional_prefix == "project/raw_outputs/additional_files/"
    assert "gs://lab-data/project/raw/" not in transfer_destinations.guide_paths(
        guide, doc.projects[pid].data_uri,
    )


@pytest.mark.parametrize("folder", ["raw/", "`raw`", "`raw/`"])
def test_documented_relative_raw_folders_remain_usable(project, folder):
    doc, pid = project
    config = transfer_destinations.resolve(
        TransferRequest(sample="TMA37"), f"gs://lab-data/project/\nRaw data: {folder}",
        "Transfer", doc, pid, OWNER,
    )
    assert config.raw_prefix == "project/raw/"


@pytest.mark.parametrize("sample,alias", [("TMA37", ""), ("Slide 1", "TMA37")])
@pytest.mark.parametrize("suffix", ["", "TMA37", "TMA37/"])
def test_final_sample_paths_and_bases_produce_the_same_layout(project, sample, alias, suffix):
    doc, pid = project
    config = transfer_destinations.resolve(
        TransferRequest(sample=sample, as_sample=alias,
                        destination="gs://lab-data/project/raw/" + suffix,
                        additional_destination="gs://lab-data/project/additional_files/" + suffix),
        "", "Those two paths are correct", doc, pid, OWNER,
    )
    assert config.raw_prefix == "project/raw/"
    assert config.additional_prefix == "project/additional_files/"
    assert config.status_prefix == "project/raw/.mica-transfers/"


@pytest.mark.parametrize("raw,extra,actor", [
    ("gs://other-bucket/raw/", "gs://lab-data/project/additional/", OWNER),
    ("gs://lab-data/project/raw/", "gs://other-bucket/additional/", OWNER),
    ("gs://lab-data/outside/raw/", "gs://lab-data/outside/additional/", "collaborator@example.com"),
    ("gs://lab-data/project/raw/", "gs://lab-data/project/raw/", OWNER),
    ("gs://lab-data/project/raw/", "gs://lab-data/project/raw/TMA37/nested/", OWNER),
    ("gs://lab-data/project/../raw/", "gs://lab-data/project/additional/", OWNER),
    ("gs://lab-data/project/*/", "gs://lab-data/project/additional/", OWNER),
    ("gs://lab-data/project/raw/", "gs://lab-data/project/additional/", "stranger@example.com"),
])
def test_inference_cannot_bypass_location_or_membership_checks(project, raw, extra, actor):
    doc, pid = project
    with pytest.raises(ValueError):
        transfer_destinations.resolve(
            TransferRequest(sample="TMA37", destination=raw, additional_destination=extra),
            "", "Start the transfer", doc, pid, actor,
        )


@pytest.mark.parametrize("text", [
    "Start this transfer",
    "These two paths are correct",
    "These two paths are correct, save them as defaults as well",
])
async def test_contextual_proposal_needs_no_literal_paths_or_guide_worker(turn, text):
    turn[2]["payload"]["event"]["text"] = text
    sdk = Client(tool(
        "propose_transfer", sample="TMA37", destination="gs://lab-data/raw/TMA37/",
        additional_destination="gs://lab-data/additional_files/TMA37/",
    ))
    result = await run(turn, sdk)
    assert result.route == "transfer"
    assert [s["tool"] for s in turn[2]["conversation_steps"]] == ["propose_transfer"]
    turn[0].test_client.create_job.assert_not_called()


async def test_prior_requests_are_bounded_and_private_to_requester_and_project(transfer):
    service, pending = transfer
    service.config = Mock(return_value=TransferProject(bucket="lab-data"))
    service.connections.status = AsyncMock(return_value={"status": "saved"})
    ids = []
    for number in range(8):
        p = copy.deepcopy(pending)
        p.update(id=f"{number:032x}", state="expired")
        if number == 7:
            p["requested_by"] = "someone-else@example.com"
        if number == 6:
            p["project"] = "another-project"
        await service.save(p)
        ids.append(p["id"])
    await service.repo.set_connector_state(
        "atomx-user-runs:" + atomx_connections.digest(OWNER), {"ids": ids},
    )
    private = await service.context(pending["project"], OWNER, include_requests=True)
    assert len(private["samples"]) == 6
    assert sum("request" in row for row in private["samples"]) == 5
    request = private["samples"][0]["request"]
    assert request["sample"] == "Slide 1" and request["as_sample"] == "TMA37"
    assert request["destination"] == "gs://lab-data/raw/"
    assert request["additional_destination"] == "gs://lab-data/additional/"
    shared = await service.context(pending["project"], OWNER)
    assert all("request" not in row for row in shared["samples"])
    assert "gs://lab-data/raw/" not in json.dumps(shared)


@pytest.mark.parametrize("recipients,private", [
    ([OWNER, "mica+requests@gmail.com"], True),
    (["mi.ca@gmail.com"], True),
    ([OWNER, "mica+requests@gmail.com", "collaborator@example.com"], False),
])
async def test_personal_transfer_context_is_not_added_to_shared_email(
    mail, monkeypatch, recipients, private,
):
    eid, record = await email_record(mail)
    record["payload"]["event"]["text"] = "What transfer would you propose?"
    record["payload"]["_email"]["recipients"] = recipients
    mail.decision.return_value = conversation.Decision(route="reply", reply="Review the preview.")
    context = AsyncMock(return_value={"available": True})
    monkeypatch.setattr(Transfers, "context", context)
    assert await direct.handle(mail, eid, record, mail.store)
    context.assert_awaited_once_with(mail.pid, OWNER, include_requests=private)
