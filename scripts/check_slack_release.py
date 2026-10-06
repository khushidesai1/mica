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

import pistachio
from pistachio.agents.analysis import access, attachments, briefs, conversation_memory, direct
from pistachio.agents.analysis.commands import Command
from pistachio.agents.analysis.config import Settings
from pistachio.agents.analysis.registry import Registry
from pistachio.agents.analysis.storage import LocalStorage


def check(source: Path, mode: str) -> None:
    installed = Path(pistachio.__file__).parent
    files = sorted((source / "src/pistachio").rglob("*.py"))
    for file in files:
        assert (
            file.read_bytes()
            == (installed / file.relative_to(source / "src/pistachio")).read_bytes()
        ), file
    print(f"SOURCE_VERIFIED: {len(files)} application files")
    settings = Settings(_env_file=None)
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
        doc = Registry(store).load()
        pid = doc.create("test@example.com", "test", "gs://test/data/")
        grant = access.Grant("test@example.com", True, {}, doc, pid, True, "T:D")
        summary, generation = conversation_memory.load(store, grant, "a")
        conversation_memory.save(
            store, grant, "a", summary, generation, "one", "User hypothesis: unverified"
        )
        assert (
            conversation_memory.load(store, grant, "b")[0].summary == "User hypothesis: unverified"
        )
        conversation_memory.command(Command("forget context"), grant, store, "b", "clear")
        assert not conversation_memory.load(store, grant, "a")[0].summary
        assert not conversation_memory.is_current(store, grant, "a", {})
        assert briefs.POLICY in direct.SYSTEM
        assert (
            Path("/opt/pistachio/templates/PROJECT.template.md").read_bytes()
            == (source / "templates/PROJECT.template.md").read_bytes()
        )
        print("CONVERSATION_MEMORY_AND_GUIDE_POLICY_VERIFIED")
        if mode == "worker":
            check_worker(work, source, settings)


def check_worker(work: Path, source: Path, settings: Settings) -> None:
    from PIL import Image

    from pistachio.agents.analysis.runner import Context, build_options

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
        conversation_summary="User hypothesis: unverified",
        user_memory="Prefer PNG",
    )
    assert "# Conversation memory" in opts.system_prompt["append"]
    assert "User hypothesis: unverified" in opts.system_prompt["append"]
    assert "# Personal preferences" in opts.system_prompt["append"]
    assert opts.max_buffer_size == 16 * 1024 * 1024
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
