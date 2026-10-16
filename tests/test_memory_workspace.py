"""Unsafe workspace entries cannot become memory changes or accidental deletions."""

import os

import pytest

from mica.agents.analysis import memory_workspace, project_memory
from mica.agents.analysis.config import Settings

NOTE = "type: fact\nsource: Alice\nstatus: current\n\nA fact"


@pytest.mark.parametrize("kind", ["symlink", "directory_symlink", "fifo", "secret", "oversize"])
def test_invalid_replacement_is_rejected_without_deleting_original(tmp_path, kind):
    settings = Settings(_env_file=None)
    base = project_memory.Snapshot(
        head_oid="a" * 40, files={"data/fact.md": project_memory.File(text=NOTE, oid="b" * 40)}
    )
    memory_workspace.materialize(tmp_path, base, settings)
    target = tmp_path / "knowledge/data/fact.md"
    target.unlink()
    if kind == "symlink":
        target.symlink_to("/etc/passwd")
    elif kind == "directory_symlink":
        target.parent.rmdir()
        target.parent.symlink_to("/tmp", target_is_directory=True)
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind == "secret":
        target.write_text(NOTE + "\ngithub_pat_should_not_be_stored")
    else:
        target.write_text(NOTE + "x" * 20_000)
    proposal = memory_workspace.proposal(tmp_path, base, settings)
    assert proposal.changes == {} and proposal.rejected


def test_snapshot_replaces_workspace_and_pending_restores_only_original_head(tmp_path):
    settings = Settings(_env_file=None)
    base = project_memory.Snapshot(head_oid="a" * 40)
    memory_workspace.materialize(tmp_path, base, settings)
    (tmp_path / "knowledge/open.md").write_text(NOTE)
    pending = memory_workspace.pending(tmp_path, base, settings)
    memory_workspace.materialize(tmp_path, base, settings)
    assert not (tmp_path / "knowledge/open.md").exists()
    memory_workspace.restore_pending(pending, tmp_path, base, settings)
    assert (tmp_path / "knowledge/open.md").read_text() == NOTE
