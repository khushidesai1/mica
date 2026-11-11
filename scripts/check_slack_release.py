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
from types import SimpleNamespace
from unittest.mock import patch

import httpx

import mica
from mica.agents.analysis import (
    access,
    amendments,
    attachments,
    commands,
    conversation,
    direct,
    gcs_operations,
    memory_archive,
    memory_workspace,
    project_memory,
    prompts,
    reference_repos,
    thread_memory,
    write_access,
    write_approval,
)
from mica.agents.analysis.config import Settings
from mica.agents.analysis.progress import TaskProgress, check_in_due
from mica.agents.analysis.registry import Registry
from mica.agents.analysis.slack_format import activity_blocks, activity_fallback, status_blocks
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
    assert settings.thread_memory == "dual"
    thread_files = {}
    for seq in (3, 1, 2):
        update = thread_memory.make_update(
            "release_probe",
            f"probe_{seq}",
            seq,
            "2026-11-05",
            "Mica",
            "channel",
            True,
            thread_memory.Prose(paraphrase=f"Verified step {seq}.", summary=f"Step {seq}."),
            [],
        )
        thread_files.update(thread_memory.apply(thread_files, update, settings))
    assert [e.seq for e in thread_memory.log_entries(thread_files, "release_probe")] == [1, 2, 3]
    assert '"seq": 1' in thread_memory.context(thread_files, "release_probe")
    assert thread_memory.writer_view(thread_files, "release_probe")["covered"] == [
        "probe_1",
        "probe_2",
        "probe_3",
    ]
    print("THREAD_MEMORY_ORDERING_AND_COVERAGE_VERIFIED")
    assert importlib.metadata.version("anthropic") == "2.3.0"
    assert all(
        tool["strict"] is (tool["name"] != "start_worker") for tool in conversation.tools_for(True)
    )
    assert settings.long_run_threshold_seconds == 600
    assert settings.long_run_update_seconds == 7200
    assert settings.max_run_seconds == 604800
    assert not settings.amendments
    assert not settings.transfers_enabled
    assert not check_in_due(0, None, 599, 600, 7200)
    assert check_in_due(0, None, 600, 600, 7200)
    assert not check_in_due(0, 600, 7799, 600, 7200)
    assert check_in_due(0, 600, 27 * 3600, 600, 7200)
    activity = activity_blocks(
        [TaskProgress("job", "Analysis", "Running")], check_in="Run elapsed: 1d 3h 0m"
    )[0]
    assert activity["type"] == "plan" and activity["tasks"][0]["title"] == "Analysis"
    assert activity["tasks"][0]["status"] == "in_progress"
    assert "Run elapsed: 1d 3h 0m" in str(activity["tasks"])
    assert {ATOMX_TRANSFER_ID, SPATIAL_ANALYSIS_ID} <= {t.id for t in BOOTSTRAP_TOOLS}
    print("LONG_RUN_PROGRESS_AND_TOOL_CATALOG_VERIFIED")
    commentary = "The destination is empty. Preparing the move for approval."
    presentation = activity_blocks(
        [TaskProgress("probe", "Checking destination files", "Read")], note=commentary,
    )
    assert presentation[0] == {"type": "section", "text": {
        "type": "plain_text", "text": commentary,
    }}
    assert presentation[1]["type"] == "plan"
    assert presentation[1]["title"] == "Checking destination files"
    assert activity_fallback(presentation)[0] == presentation[0]
    resumed = status_blocks("", "Preparing approved changes…", detail="Approved: move 3 files")
    assert resumed[0]["type"] == "plan" and "Approved: move 3 files" in str(resumed)
    for state in ("waiting", "cancelled", "expired"):
        assert status_blocks("", "No work started", state=state)[0]["type"] == "context"
    terminal = activity_blocks([], finished=True, note=commentary)
    assert len(terminal) == 1 and "in_progress" not in str(terminal)
    print("SLACK_COMMENTARY_APPROVAL_AND_TERMINAL_PRESENTATION_VERIFIED")
    native = activity_blocks(
        [TaskProgress("done", "Read files", "Reading", "complete"),
         TaskProgress("current", "Checking counts", "Checking")],
        native_session=True, note=commentary,
    )
    assert native[1]["title"] == "Checking counts"
    assert [t["task_id"] for t in native[1]["tasks"]] == ["done"]
    assert "in_progress" not in str(native)
    first = status_blocks("", "Checking files", detail="4 folders", native_session=True)
    assert first[0]["tasks"] == [] and "4 folders" in str(activity_fallback(first))
    assert activity_blocks([], finished=True, native_session=True) == activity_blocks(
        [], finished=True,
    )
    print("NATIVE_SESSION_SINGLE_WORKING_STATE_VERIFIED")
    operation = gcs_operations.Operation(
        action="move", source="gs://release-probe/TMA1/",
        destination="gs://release-probe/TMA1_old/",
    )
    proposed = conversation.Decision(
        route="storage", reply="Requested", write_operations=[operation]
    )
    assert conversation.Decision.model_validate_json(proposed.model_dump_json()) == proposed
    approval = write_approval.Approval(
        event_id="a" * 32, project_id="probe", grant_id="b" * 32, principal="synthetic",
        access_signature="synthetic", paths=[], inspected_at=1, expires_at=3601,
        operations=[operation], operation_counts=[2],
    )
    confirmation, _ = write_approval.blocks(approval, "gs://release-probe/")
    assert operation.source in confirmation and operation.destination in confirmation
    assert "Move folder" in confirmation
    assert "propose_gcs_operations" in {tool["name"] for tool in conversation.tools_for(True)}
    assert "propose_gcs_operations" not in {tool["name"] for tool in conversation.tools_for(False)}
    print("EXACT_GCS_OPERATION_CONFIRMATIONS_VERIFIED")
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary).resolve()
        store = LocalStorage(work / "storage")
        amendments.add(
            store,
            "a" * 32,
            "b" * 32,
            amendments.Addendum(
                text="Label both axes",
                principal="test@example.com",
                signature="test",
                created=1,
            ),
        )
        assert "Label both axes" in amendments.consume(store, "a" * 32, "test@example.com", "test")
        assert not amendments.consume(store, "a" * 32, "test@example.com", "test")
        assert amendments.close(store, "a" * 32).items["b" * 32].consumed
        print("BOUNDED_AMENDMENT_HANDOFF_VERIFIED")
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
        import zipfile

        from mica.agents.analysis.deliverables import package_folder
        from mica.agents.analysis.outputs import retention_reason

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
        assert project_memory.GUIDE_POLICY in prompts.build_system_append(
            SimpleNamespace(survey_only=True), settings
        )
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
    assert "project-knowledge skill" in opts.system_prompt["append"]
    assert "User hypothesis: unverified" not in opts.system_prompt["append"]
    assert opts.setting_sources == []
    assert opts.skills == [
        "mica:spatial-analysis",
        "mica:cellpose",
        "mica:project-knowledge",
        "mica:segmentation-report",
        "mica:session-import",
    ]
    plugin = Path("/opt/pistachio/plugin")
    for file in (source / "skills").rglob("*"):
        if file.is_file():
            assert (plugin / file.relative_to(source / "skills")).read_bytes() == file.read_bytes()

    async def context_check():
        hook = opts.hooks["UserPromptSubmit"][0].hooks[0]
        output = await hook({"hook_event_name": "UserPromptSubmit"}, None, None)
        assert "User hypothesis: unverified" in output["hookSpecificOutput"]["additionalContext"]
        assert "viridis" in output["hookSpecificOutput"]["additionalContext"]

    asyncio.run(context_check())

    async def plugin_check():
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        isolated = ClaudeAgentOptions(
            cwd=str(work),
            setting_sources=[],
            plugins=opts.plugins,
            skills=opts.skills,
            tools=["Read", "Skill"],
            permission_mode="dontAsk",
            env={
                "CLAUDE_CONFIG_DIR": str(work / "init-config"),
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "ANTHROPIC_API_KEY": "initialization-only-not-a-key",
            },
        )
        async with asyncio.timeout(45):
            async with ClaudeSDKClient(options=isolated) as client:
                info = await client.get_server_info()
                names = {item["name"] for item in info["commands"]}
                assert set(opts.skills) <= names

    asyncio.run(plugin_check())
    print("TRUSTED_PLUGIN_INITIALIZATION_VERIFIED_WITHOUT_MODEL_CALL")
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
