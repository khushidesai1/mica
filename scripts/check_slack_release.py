"""Offline image gate for the coordinated Slack release; no cloud/model calls."""

import asyncio
import hashlib
import importlib.metadata
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx

import mica
from mica.agents.analysis import (
    access,
    attachments,
    commands,
    direct,
    memory_archive,
    memory_workspace,
    project_memory,
    reference_repos,
    write_access,
)
from mica.agents.analysis.config import Settings
from mica.agents.analysis.progress import TaskProgress, check_in_due
from mica.agents.analysis.registry import Registry
from mica.agents.analysis.slack_format import activity_blocks
from mica.agents.analysis.storage import LocalStorage
from mica.services.tools import BOOTSTRAP_TOOLS
from mica.tool_catalog import ATOMX_TRANSFER_ID, SPATIAL_ANALYSIS_ID


def check(source: Path, mode: str) -> None:
    installed = Path(mica.__file__).parent
    files = sorted((source / "src/mica").rglob("*.py"))
    for file in files:
        assert (
            file.read_bytes() == (installed / file.relative_to(source / "src/mica")).read_bytes()
        ), file
    print(f"SOURCE_VERIFIED: {len(files)} application files")
    from mica.agents.analysis.email_delivery import TerminalResult
    from mica.connectors.gmail_client import verified_message

    assert verified_message(b"From: forged@example.com\r\n\r\nunsigned") is None
    assert TerminalResult(attempt=0, text="image gate").artifacts == []
    print("EMAIL_IDENTITY_AND_RESULT_IMPORTS_VERIFIED")
    # Pinned older workers and saved scripts may still import the former package.
    import importlib

    assert importlib.import_module("pistachio.models") is importlib.import_module("mica.models")
    legacy = importlib.import_module("pistachio")
    assert Path(legacy.__file__).read_bytes() == (source / "src/pistachio/__init__.py").read_bytes()
    settings = Settings(_env_file=None)
    assert settings.long_run_threshold_seconds == 600
    assert settings.long_run_update_seconds == 7200
    assert settings.max_run_seconds == 604800
    assert not settings.transfers_enabled
    assert not check_in_due(0, None, 599, 600, 7200)
    assert check_in_due(0, None, 600, 600, 7200)
    assert not check_in_due(0, 600, 7799, 600, 7200)
    assert check_in_due(0, 600, 27 * 3600, 600, 7200)
    card = activity_blocks(
        [TaskProgress("job", "Analysis", "Running")], check_in="Run elapsed: 1d 3h 0m"
    )[0]["tasks"][0]
    assert card["status"] == "in_progress" and "details" in card
    assert {ATOMX_TRANSFER_ID, SPATIAL_ANALYSIS_ID} <= {t.id for t in BOOTSTRAP_TOOLS}
    print("LONG_RUN_PROGRESS_AND_TOOL_CATALOG_VERIFIED")
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary).resolve()
        store = LocalStorage(work / "storage")
        png = b"\x89PNG\r\n\x1a\nsynthetic"
        client = httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=png))
        )
        with patch.object(attachments.httpx, "Client", return_value=client):
            uploaded = attachments.ingest(
                "synthetic-token",
                store,
                [{"name": "plot.png", "url_private_download": "https://files.slack.com/mock"}],
                "projects/test/runs/thread",
                "image-gate",
                settings,
            )
        assert uploaded[0].key.startswith("tasks/slack-analysis/uploads/")
        assert uploaded[0].sha256 == hashlib.sha256(png).hexdigest()
        media = attachments.router_media(store, uploaded)
        assert media[1]["type"] == "image"
        assert attachments.materialize(store, work / "work", uploaded)
        assert (
            direct.Decision(route="view", reply="Look", view_ids=[uploaded[0].id]).route == "view"
        )
        print("ATTACHMENT_TRANSPORT_VERIFIED")
        from mica.agents.analysis.deliverables import package_folder
        from mica.agents.analysis.outputs import retention_reason
        import zipfile

        folder = work / "outputs/package"
        (folder / "nested/helpers").mkdir(parents=True)
        (folder / "nested/helpers/run.py").write_text("print(1)")
        (folder / "nested/results.bin").write_bytes(b"result")
        package = package_folder(work, "outputs/package")
        archive = work / package["attachment"]
        with zipfile.ZipFile(archive) as zipped:
            assert zipped.read("package/nested/helpers/run.py") == b"print(1)"
            assert zipped.read("package/nested/results.bin") == b"result"
        assert package["files"] == 2
        assert not retention_reason(archive.relative_to(work), archive.stat().st_size)
        print("COMPLETE_NESTED_SCRIPT_FOLDER_ATTACHMENT_VERIFIED")
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        synthetic_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = synthetic_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        assert len(reference_repos.app_jwt(pem, "123").split(".")) == 3
        reference_work = work / "reference-work"
        reference_repos.materialize(
            reference_work,
            [
                reference_repos.RepoSnapshot(
                    repo="azizilab/image-gate",
                    commit="a" * 40,
                    files={"nested/script.py": "print('synthetic offline check')"},
                )
            ],
        )
        reference_root = reference_work / "inputs/repos/image-gate"
        assert (reference_root / "nested/script.py").is_file()
        assert (reference_root / ".pin").read_text() == "azizilab/image-gate@" + "a" * 40 + "\n"
        assert memory_workspace.scan(reference_work, settings)[0] == {}
        reference_repos.materialize(reference_work, [])
        assert not (reference_work / "inputs/repos").exists()
        print("REFERENCE_REPO_APP_SIGNING_AND_WORKER_SNAPSHOT_VERIFIED")
        doc = Registry(store).load()
        pid = doc.create("test@example.com", "test", "gs://test/data/")
        grant = access.Grant("test@example.com", True, {}, doc, pid, True, "T:D")
        snapshot = project_memory.Snapshot(
            head_oid="a" * 40,
            files={
                "PROJECT.md": project_memory.File(text="Verified guide", oid="b" * 40),
                "conventions.md": project_memory.File(
                    text="type: user\nsource: Test, 2026-10-10\nstatus: current\n\nUse viridis",
                    oid="c" * 40,
                ),
            },
        )
        memory_workspace.materialize(work / "memory-work", snapshot, settings)
        assert memory_workspace.proposal(work / "memory-work", snapshot, settings).changes == {}
        assert "viridis" in project_memory.context(snapshot, "heatmaps", settings)
        assert project_memory.GUIDE_POLICY in direct.SYSTEM
        for removed in (
            "user_memory",
            "conversation_memory",
            "knowledge_mirror",
            "knowledge",
            "knowledge_commands",
        ):
            assert not (installed / "agents/analysis" / (removed + ".py")).exists(), removed
        assert (
            Path("/opt/pistachio/templates/PROJECT.template.md").read_bytes()
            == (source / "templates/PROJECT.template.md").read_bytes()
        )
        original = "type: fact\nsource: synthetic image gate\nstatus: current\n\nArchived evidence."
        layout = memory_archive.prepare(
            {}, {f"data/note-{n:03d}.md": original for n in range(200)}, settings
        )
        archived = project_memory.Snapshot(
            head_oid="a" * 40,
            files={
                p: project_memory.File(text=t, oid="b" * 40)
                for p, t in layout.items()
                if t is not None
            },
        )
        assert "archive/INDEX.md" in archived.files
        assert len([p for p in archived.files if p.startswith("archive/index-")]) > 1
        memory_workspace.materialize(work / "archive-work", archived, settings)
        assert not memory_workspace.proposal(work / "archive-work", archived, settings).changes
        context = project_memory.context(archived, "archived evidence", settings)
        assert len(context) <= settings.memory_router_chars and "File: archive/" not in context
        path = next(
            p for p in archived.files if p.startswith("archive/") and not memory_archive.is_index(p)
        )
        (work / "archive-work" / "knowledge" / path).unlink()
        proposal = memory_workspace.proposal(work / "archive-work", archived, settings)
        assert path in proposal.rejected and path not in proposal.changes
        print("MEMORY_ARCHIVE_PAGES_AND_WORKER_COMPATIBILITY_VERIFIED")
        print("GITHUB_MEMORY_SNAPSHOT_AND_GUIDE_POLICY_VERIFIED")
        for text in ("help", "help all", "help write", "write help"):
            cmd = commands.parse(text)
            assert cmd is not None
            reply = commands.run(
                cmd,
                grant,
                Registry(store),
                None,
                {"payload": {"team_id": "T", "event": {"channel": "D"}}},
                "help",
                settings,
            )
            assert all(
                word in reply
                for word in (
                    "write grant",
                    "write status",
                    "write revoke",
                    "until revoked",
                    "overwrites",
                    "project's linked bucket",
                )
            )
            assert "new files only" not in reply
        assert commands.parse("write grant test gs://test/exports/").args == (
            "test",
            "gs://test/exports/",
        )
        assert "no expiry" in write_access.description(
            write_access.WriteGrant(
                id="a" * 32,
                uri="gs://test/exports/",
                actor="test@example.com",
                admin_id="T:U",
                created_at=write_access.now(),
            )
        )
        print("WRITE_COMMANDS_AND_HELP_VERIFIED")
        if mode == "worker":
            check_worker(work, source, settings)


def check_worker(work: Path, source: Path, settings: Settings) -> None:
    for command in ("mica-analyze", "pistachio-analyze"):
        assert "Local Mica agent" in subprocess.check_output([command, "--help"], text=True)
    from PIL import Image

    from mica.agents.analysis.runner import Context, build_options

    assert importlib.metadata.version("pypdfium2") == "5.14.0"
    assert all(shutil.which(name) for name in ("bwrap", "socat"))
    tool = Path("/opt/pistachio/tools/preview.py")
    assert tool.read_bytes() == (source / "tools/preview.py").read_bytes()
    image = work / "large.png"
    Image.new("RGB", (6000, 4000), "red").save(image)
    output = subprocess.check_output([sys.executable, str(tool), str(image)], cwd=work, text=True)
    result = json.loads(output)
    preview = Path(result["preview"])
    assert max(result["width"], result["height"]) <= 1568
    assert preview.stat().st_size <= 1_500_000
    pdf = work / "pages.pdf"
    Image.new("RGB", (800, 600), "blue").save(
        pdf, "PDF", save_all=True, append_images=[Image.new("RGB", (800, 600), "red")]
    )
    output = subprocess.check_output(
        [sys.executable, str(tool), str(pdf), "--page", "2", "--text"], cwd=work, text=True
    )
    assert json.loads(output)["pages"] == 2
    opts = build_options(
        Context(work, work / "data"),
        settings,
        memory_context="User hypothesis: unverified; use viridis",
    )
    assert "# Project knowledge" in opts.system_prompt["append"]
    assert "User hypothesis: unverified" in opts.system_prompt["append"]
    assert "viridis" in opts.system_prompt["append"]
    assert opts.max_buffer_size == 16 * 1024 * 1024
    assert opts.env["BASH_MAX_TIMEOUT_MS"] == "604800000"
    assert opts.env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    guard = opts.hooks["PreToolUse"][0].hooks[0]

    async def guard_check():
        for path, denied in ((image, True), (preview, False)):
            reply = await guard(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Read",
                    "tool_input": {"file_path": str(path)},
                },
                None,
                None,
            )
            assert bool(reply) == denied

    asyncio.run(guard_check())
    print("BOUNDED_IMAGE_AND_PDF_PREVIEWS_VERIFIED")


if __name__ == "__main__":
    check(Path(__file__).resolve().parents[1], sys.argv[1])
