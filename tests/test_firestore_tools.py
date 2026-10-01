from __future__ import annotations

from pistachio.models import ToolProposal, ToolRecord, ToolStatus
from pistachio.storage.firestore import deserialize_tool


def test_deserialize_tool_record() -> None:
    record = ToolRecord(
        id="runtime",
        name="Runtime",
        source_url="https://example.org/runtime",
        version="1.0",
        install_recipe="already installed",
        status=ToolStatus.AVAILABLE,
    )

    restored = deserialize_tool(record.model_dump(mode="json"))

    assert type(restored) is ToolRecord
    assert restored.status == ToolStatus.AVAILABLE


def test_deserialize_tool_proposal_preserves_proposal_metadata() -> None:
    proposal = ToolProposal(
        id="proposal",
        name="Proposed tool",
        source_url="https://example.org/proposal",
        version="1.2.3",
        install_recipe="pip install proposed-tool==1.2.3",
        why_needed="The approved runtime lacks this capability.",
        security_notes="Read-only source review completed.",
    )

    restored = deserialize_tool(proposal.model_dump(mode="json"))

    assert isinstance(restored, ToolProposal)
    assert restored.why_needed == proposal.why_needed
    assert restored.security_notes == proposal.security_notes
