"""Full checkout fidelity, bounded patches, and independent Git tree verification."""

import hashlib
import io
import subprocess
import tarfile

import pytest

from mica.agents.analysis import pull_request_files as files
from mica.agents.analysis.checkpoints import restore_workspace, workspace_snapshot
from mica.agents.analysis.pull_requests import patch_for
from mica.agents.analysis.storage import LocalStorage

COMMIT, EVENT, PROPOSAL = "a" * 40, "b" * 32, "c" * 32


def checkout(storage, contents=None, modes=None):
    contents = contents or {"src/main.py": b"print('before')\n", "README.md": b"Read me\n"}
    modes = modes or {}
    index = files.Index(
        commit=COMMIT,
        entries={
            p: files.Entry(sha=files.blob_sha(data), mode=modes.get(p, "100644"))
            for p, data in contents.items()
        },
    )
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as archive:
        for path, data in contents.items():
            info = tarfile.TarInfo("repo-pin/" + path)
            info.size, info.mode = len(data), int(modes.get(path, "100644")[-3:], 8)
            if modes.get(path) == "120000":
                info.type, info.linkname, info.size = tarfile.SYMTYPE, data.decode(), 0
            archive.addfile(info, io.BytesIO(data))
    filtered = files.filtered_archive(raw.getvalue(), index, files.Limits())
    index_raw = index.model_dump_json().encode()
    prefix = files.prefix(EVENT)
    pin = files.Pin(
        repo="other-owner/project",
        commit=COMMIT,
        branch="main",
        checkout_key=prefix + "checkout.tar.gz",
        index_key=prefix + "index.json",
        sha256=hashlib.sha256(filtered).hexdigest(),
        index_sha256=hashlib.sha256(index_raw).hexdigest(),
    )
    storage.put(pin.checkout_key, filtered)
    storage.put(pin.index_key, index_raw)
    return pin, index, {p: d for p, d in contents.items() if index.entries[p].included}


def proposal(changes):
    return files.Proposal(
        id=PROPOSAL,
        commit=COMMIT,
        title="Update the script",
        body="Explain the change",
        checks="pytest: passed",
        changes=changes,
    )


def test_full_checkout_preserves_modes_binaries_filters_secrets_and_links(tmp_path):
    storage = LocalStorage(tmp_path / "store")
    content = {
        "run.sh": b"echo ok\n",
        "fixture.bin": b"\x00\x01\xff",
        ".env": b"PRIVATE=x",
        "shortcut": b"run.sh",
        "src/test.ts": b"test();\n",
    }
    pin, index, base = checkout(storage, content, {"run.sh": "100755", "shortcut": "120000"})
    assert set(index.omitted) == {".env", "shortcut"}
    assert files.load_checkout(storage, pin)[1] == base
    work = tmp_path / "work"
    files.materialize(storage, work, pin, resume=False)
    editable = work / "contrib" / pin.repo
    assert (editable / "run.sh").stat().st_mode & 0o777 == 0o755
    assert (work / "inputs/pull-request" / pin.repo / "run.sh").stat().st_mode & 0o777 == 0o555
    assert (editable / "fixture.bin").read_bytes() == content["fixture.bin"]
    assert files.scan_changes(work, pin, index, base) == []
    (editable / "src/test.ts").write_text("changed();\n")
    (editable / "new.txt").write_text("new")
    (editable / "run.sh").unlink()
    changes = files.scan_changes(work, pin, index, base)
    files.validate(proposal(changes), pin, index, base)
    assert {c.path for c in changes} == {"src/test.ts", "new.txt", "run.sh"}


@pytest.mark.parametrize(
    "path", ["/absolute", "../escape", "a/../b", ".git/config", "a\\b", "a\nb", "a//b", "./file"]
)
def test_proposal_paths_cannot_escape_checkout(path):
    with pytest.raises(ValueError):
        files.path_name(path)


@pytest.mark.parametrize(
    "change",
    [
        files.Change(path=".github/workflows/build.yml", content="run: echo unsafe"),
        files.Change(path=".env", content="x=y"),
        files.Change(path="shortcut/child", content="file"),
        files.Change(path="README.md", content="change", mode="100755"),
        files.Change(path="fixture.bin", content="replace binary"),
        files.Change(path="README.md/child", content="conflict"),
        files.Change(path="new.sh", content="exec", mode="100755"),
        files.Change(path="missing.txt"),
        files.Change(path="README.md", content="Read me\n"),
    ],
)
def test_controller_rejects_worker_supplied_unsafe_changes(tmp_path, change):
    store = LocalStorage(tmp_path / "store")
    pin, index, base = checkout(
        store,
        {"README.md": b"Read me\n", "fixture.bin": b"\x00\xff", "shortcut": b"README.md"},
        {"shortcut": "120000"},
    )
    with pytest.raises(ValueError):
        files.validate(proposal([change]), pin, index, base)


def test_byte_caps_checksum_tampering_and_symlinks_fail_closed(tmp_path):
    store = LocalStorage(tmp_path / "store")
    pin, index, base = checkout(store)
    limited = pin.model_copy(update={"limits": files.Limits(file_bytes=5)})
    with pytest.raises(ValueError, match="byte"):
        files.validate(proposal([files.Change(path="new", content="123456")]), limited, index, base)
    work = tmp_path / "work"
    files.materialize(store, work, pin, resume=False)
    target = work / "contrib" / pin.repo / "escape"
    target.symlink_to(tmp_path)
    with pytest.raises(ValueError, match="Symlinks"):
        files.scan_changes(work, pin, index, base)
    store.put(pin.index_key, b"{}")
    with pytest.raises(ValueError, match="changed"):
        files.load_checkout(store, pin)


def test_resume_preserves_deletions_edits_and_large_unchanged_fixtures(tmp_path):
    store = LocalStorage(tmp_path / "store")
    pin, index, base = checkout(
        store, {"main.ts": b"before", "delete.txt": b"delete", "test.bin": b"\x00" * (3 * 1024**2)}
    )
    home = tmp_path / "first"
    work = home / "work/thread"
    files.materialize(store, work, pin, resume=False)
    edit = work / "contrib" / pin.repo
    (edit / "main.ts").write_text("after")
    (edit / "delete.txt").unlink()
    archive = home / "snapshot.tar.gz"
    workspace_snapshot(archive, home, work=work, partial=False)
    restored = tmp_path / "second"
    new_work = restored / "work/thread"
    with archive.open("rb") as stream:
        restore_workspace(stream, restored, work=new_work)
    files.materialize(store, new_work, pin, resume=True)
    changes = files.scan_changes(new_work, pin, index, base)
    files.validate(proposal(changes), pin, index, base)
    assert {c.path: c.content for c in changes} == {"main.ts": "after", "delete.txt": None}
    assert (new_work / "contrib" / pin.repo / "test.bin").stat().st_size == 3 * 1024**2


def test_exact_tree_and_patch_agree_with_git_including_omitted_files(tmp_path):
    store = LocalStorage(tmp_path / "store")
    content = {
        "README.md": b"old without newline",
        "empty": b"",
        "src/run.sh": b"echo ok\n",
        ".env": b"unchanged",
        "binary": b"\x00\xff",
        "src-foo": b"sibling",
    }
    pin, index, base = checkout(store, content, {"src/run.sh": "100755"})
    changes = [
        files.Change(path="README.md", content="new without newline"),
        files.Change(path="empty"),
        files.Change(path="new.txt", content=""),
    ]
    row = proposal(changes)
    files.validate(row, pin, index, base)
    repo = tmp_path / "git"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    for path, data in content.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(int(index.entries[path].mode[-3:], 8))
    git("add", ".")
    assert git("write-tree") == files.tree_sha(index, [])
    patch, _ = patch_for(row, base)
    patch_file = tmp_path / "change.patch"
    patch_file.write_text(patch)
    git("apply", str(patch_file))
    git("add", "-A")
    assert git("write-tree") == files.tree_sha(index, changes)


def test_executable_edits_refused_before_commit_api_can_change_mode(tmp_path):
    store = LocalStorage(tmp_path / "store")
    pin, index, base = checkout(store, {"run.sh": b"echo before\n"}, {"run.sh": "100755"})
    with pytest.raises(ValueError, match="Executable"):
        files.validate(
            proposal([files.Change(path="run.sh", content="echo after\n", mode="100755")]),
            pin,
            index,
            base,
        )


@pytest.mark.parametrize("path", ["file with spaces.txt", 'file"quoted.txt', "unicode-λ.txt"])
def test_review_patch_applies_for_supported_filenames(tmp_path, path):
    store = LocalStorage(tmp_path / "store")
    pin, index, base = checkout(store, {path: b"before\n"})
    row = proposal([files.Change(path=path, content="after\n")])
    files.validate(row, pin, index, base)
    patch, _ = patch_for(row, base)
    work = tmp_path / "work"
    work.mkdir()
    (work / path).write_bytes(base[path])
    changed = subprocess.run(
        ["git", "apply", "-"], input=patch, cwd=work, capture_output=True, text=True
    )
    assert changed.returncode == 0, changed.stderr
    assert (work / path).read_text() == "after\n"
