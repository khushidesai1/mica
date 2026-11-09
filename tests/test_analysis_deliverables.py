# ruff: noqa: F811
import json
import os
import zipfile
from pathlib import Path

import pytest
from test_analysis_slack import bot, event, send  # noqa: F401

from mica.agents.analysis.deliverables import attachment_issue, package_folder, send_tool
from mica.agents.analysis.outputs import retention_reason
from mica.agents.analysis.runner import TurnResult


def tree(work):
    root = work / "outputs/stitch"
    for name, data in {
        "restitch.py": b"import helper",
        "nested/helper.py": b"print(1)",
        "nested/metrics.csv": b"a,b\n1,2",
        "nested/model.bin": b"\x00\x01",
        "figures/view.png": b"figure",
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.utime(path, ns=(1, 1))  # Unchanged helpers must be included too.
    (root / "nested/empty").mkdir()
    return root


def test_complete_script_folder_preserves_every_nested_file_and_empty_directory(tmp_path):
    root = tree(tmp_path)
    result = package_folder(tmp_path, "outputs/stitch")
    archive = tmp_path / result["attachment"]
    with zipfile.ZipFile(archive) as z:
        for path in root.rglob("*"):
            name = "stitch/" + path.relative_to(root).as_posix()
            assert (
                name + "/" in z.namelist() if path.is_dir() else z.read(name) == path.read_bytes()
            )
    assert result["files"] == 5
    assert not retention_reason(archive.relative_to(tmp_path), archive.stat().st_size)


@pytest.mark.parametrize("kind", ["symlink-file", "symlink-dir", "fifo", "hardlink", "budget"])
def test_package_failure_never_leaves_partial_zip(tmp_path, kind):
    root = tree(tmp_path)
    if kind == "symlink-file":
        (root / "link").symlink_to(root / "restitch.py")
    elif kind == "symlink-dir":
        (root / "link").symlink_to(root / "nested", target_is_directory=True)
    elif kind == "fifo":
        os.mkfifo(root / "pipe")
    elif kind == "hardlink":
        os.link(root / "restitch.py", root / "other.py")
    with pytest.raises(ValueError):
        package_folder(tmp_path, "outputs/stitch", limit=10 if kind == "budget" else 200_000_000)
    assert not list((tmp_path / "outputs").glob("*.zip"))


@pytest.mark.parametrize("folder", ["../outside", "/etc", "inputs", "outputs", "outputs/gcs/run"])
def test_packaging_cannot_expose_unapproved_workspace_folders(tmp_path, folder):
    with pytest.raises(ValueError):
        package_folder(tmp_path, folder)


def test_explicit_figure_folder_is_a_complete_zip(tmp_path):
    folder = tmp_path / "outputs/figures"
    folder.mkdir(parents=True)
    (folder / "figure.png").write_bytes(b"figure")
    package = package_folder(tmp_path, "outputs/figures")
    with zipfile.ZipFile(tmp_path / package["attachment"]) as archive:
        assert archive.read("figures/figure.png") == b"figure"


def test_copied_scripts_are_deliverable_without_claiming_they_ran(tmp_path):
    root = tree(tmp_path)
    for path in root.rglob("*.py"):
        assert not attachment_issue(path, tmp_path)
        assert not retention_reason(path.relative_to(tmp_path), path.stat().st_size)


async def test_tool_returns_package_and_exact_members_for_delivery(tmp_path):
    root = tree(tmp_path)
    prepared = {}
    reply = await send_tool(tmp_path, set(), prepared=prepared).handler(
        {"paths": ["outputs/stitch"]}
    )
    result = json.loads(reply["content"][0]["text"])
    assert result["accepted"] == [result["packages"]["outputs/stitch"]["attachment"]]
    assert set(prepared[tmp_path / result["accepted"][0]]) == set(
        p for p in root.rglob("*") if p.is_file()
    )
    assert result["packages"]["outputs/stitch"]["files"] == 5


async def test_actual_missing_script_case_delivers_nested_python_files(bot):
    async def run(ctx, prompt, status):
        paths = []
        for name in ["restitch.py", "nested/helper.py", "imgio.py"]:
            path = ctx.workdir / "outputs/tma28_restitch_scripts" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("print(1)")
            paths.append(path)
        return TurnResult("session", "Sending scripts.", paths, attachments=paths)

    bot.runner.run_turn = run
    await send(bot, event("Please send my scripts"))
    uploads = bot.client.files_upload_v2.call_args.kwargs["file_uploads"]
    assert {Path(p["file"]).name for p in uploads} == {"restitch.py", "helper.py", "imgio.py"}
    assert bot.last_error is None
    assert len(bot.delivered) == 3
    assert all(p["key"] for p in bot.delivered)


async def test_script_zip_plus_figures_has_no_duplicate_script_or_table_attachments(bot):
    async def run(ctx, prompt, status):
        root = tree(ctx.workdir)
        prepared = {}
        await send_tool(ctx.workdir, set(), prepared=prepared).handler(
            {"paths": ["outputs/stitch"]}
        )
        return TurnResult(
            "session",
            "Sending the complete folder.",
            list(root.rglob("*.py"))
            + [root / "nested/metrics.csv", root / "figures/view.png"]
            + list(prepared),
            delivery_packages=prepared,
            attachments=list(prepared) + [root / "figures/view.png"],
        )

    bot.runner.run_turn = run
    await send(bot, event("Send the scripts and outputs"))
    uploads = bot.client.files_upload_v2.call_args.kwargs["file_uploads"]
    assert len(uploads) == 2
    assert Path(uploads[0]["file"]).suffix == ".zip"
    assert uploads[1]["filename"] == "view.png"
    assert bot.last_error is None


async def test_unsupported_output_is_an_explicit_delivery_failure(bot):
    async def run(ctx, prompt, status):
        path = ctx.workdir / "outputs/result.unknown"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"result")
        return TurnResult("session", "Result.", [path], attachments=[path])

    bot.runner.run_turn = run
    await send(bot, event("Send results"))
    assert "unsupported attachment type" in bot.last_reply
    assert bot.last_error == "File delivery incomplete"


def test_changed_tree_fails_without_partial_archive(tmp_path, monkeypatch):
    from mica.agents.analysis import deliverables

    root = tree(tmp_path)
    original = deliverables.open_regular

    def changed(work, relative):
        (root / "late.csv").write_text("new")
        return original(work, relative)

    monkeypatch.setattr(deliverables, "open_regular", changed)
    with pytest.raises(ValueError, match="changed"):
        package_folder(tmp_path, "outputs/stitch")
    assert not list((tmp_path / "outputs").glob("*.zip"))


def test_credential_like_script_blocks_complete_folder(tmp_path):
    root = tree(tmp_path)
    (root / "credential.py").write_text('OPENAI_API_KEY = "sk-' + "x" * 40 + '"')
    with pytest.raises(ValueError, match="credential"):
        package_folder(tmp_path, "outputs/stitch")
    assert not list((tmp_path / "outputs").glob("*.zip"))
