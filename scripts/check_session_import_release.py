"""Offline installed-image gate for the session renderer and durable review schema."""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from mica.agents.analysis import memory_workspace, session_import, session_render, session_review
from mica.agents.analysis.config import Settings

with tempfile.TemporaryDirectory() as folder:
    path = Path(folder) / "session.jsonl"
    path.write_text(json.dumps({
        "type": "user", "sessionId": "synthetic-release-check",
        "message": {"role": "user", "content": "Adjusted for age and stage; n=43; HR 5.3."},
    }) + "\n")
    stats: dict[str, int] = {}
    blocks = list(session_render.render(path, stats))
    assert len(blocks) == 1 and "Adjusted for age and stage" in blocks[0][1]
    assert stats["session_records"] == 1
    review = session_review.Review(highlights={"h1": "not_found"})
    proposal = memory_workspace.Proposal(base_head="", review=review)
    assert memory_workspace.decode_pending(
        proposal.model_dump_json().encode(), Settings(_env_file=None)
    ).review == review
settings = Settings(_env_file=None, session_import_projects="*")
bridge = SimpleNamespace(settings=settings)
assert session_import.enabled(bridge, "new-project")
settings.session_import_projects = ""
assert not session_import.enabled(bridge, "new-project")
settings.session_import_projects = "project-one"
assert session_import.enabled(bridge, "project-one")
assert not session_import.enabled(bridge, "new-project")
print("Installed session renderer, review contract and project enablement verified")
