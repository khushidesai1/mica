import hashlib
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from pistachio.agents.analysis import attachments as a
from pistachio.agents.analysis.config import Settings
from pistachio.agents.analysis.storage import LocalStorage

PNG = b"\x89PNG\r\n\x1a\n" + b"image"


def file_record(store, *, id="f12345678", name="plot.png", data=PNG, source="user", **kwargs):
    kind, media_type = a.classify(name, data[:8192])
    key = f"tasks/slack-analysis/uploads/thread/{id}/{name}"
    store.put(key, data)
    return a.ThreadFile(
        id=id,
        source=source,
        name=name,
        path=f"inputs/12345678/01-{name}" if source == "user" else f"outputs/{name}",
        kind=kind,
        media_type=media_type,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        key=key,
        event_id="12345678",
        turn=1 if source == "agent" else None,
        user="U1",
        **kwargs,
    )


@pytest.mark.parametrize(
    "head,kind,media",
    [
        (PNG, "image", "image/png"),
        (b"\xff\xd8\xff", "image", "image/jpeg"),
        (b"GIF89a", "image", "image/gif"),
        (b"RIFFxxxxWEBP", "image", "image/webp"),
        (b"II*\0", "image", "image/tiff"),
        (b"%PDF-1.7", "pdf", "application/pdf"),
    ],
)
def test_magic_overrides_extension(head, kind, media):
    assert a.classify("malicious.csv", head) == (kind, media)


def test_classification_and_safe_names():
    assert a.classify("table.csv", b"a,b") == ("table", "text/csv")
    assert a.classify("notes.md", b"hello") == ("text", "text/markdown")
    assert a.classify("fake.png", b"hello")[0] == "other"
    for name in ("../../plot.png", ".hidden", "café.PNG", "Ａ.csv", ".", "", "x" * 200 + ".csv"):
        safe = a.safe_name(name)
        assert safe and not safe.startswith(".") and "/" not in safe and len(safe) <= 100
    assert a.safe_name("Ａ.csv") == "A.csv"
    assert a.safe_name("x" * 200 + ".csv").endswith(".csv")


@pytest.fixture
def store(tmp_path):
    return LocalStorage(tmp_path / "store")


def test_streaming_storage_abort_and_download(store, tmp_path):
    with store.open_write("nested/good") as stream:
        stream.write(b"ok")
    with pytest.raises(ValueError), store.open_write("nested/aborted") as stream:
        stream.write(b"partial")
        raise ValueError("stop")
    assert store.keys("nested") == ["nested/good"]
    store.download("nested/good", tmp_path / "copy")
    assert (tmp_path / "copy").read_bytes() == b"ok"
    assert store.read_prefix("nested/good", 1) == b"o"


@pytest.mark.parametrize(
    "url",
    [
        "http://files.slack.com/x",
        "https://files.slack.com.evil/x",
        "https://evil/x",
        "https://user:pw@files.slack.com/x",
        "https://files.slack.com:444/x",
        "https://files.slack.com:bad/x",
        "file:///etc/passwd",
    ],
)
def test_invalid_urls(url):
    with pytest.raises(ValueError, match="private Slack"):
        a.validate_url(url)


def slack_file(**kwargs):
    return {
        "name": "plot.png",
        "size": 1,
        "user": "U1",
        "id": "F1",
        "url_private_download": "https://files.slack.com/file",
        **kwargs,
    }


def mock_download(monkeypatch, handler):
    client_type = httpx.Client
    seen = []

    def make(**kwargs):
        seen.append(kwargs)
        return client_type(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(a.httpx, "Client", make)
    return seen


def ingest(store, files, settings=None):
    return a.ingest(
        "test-token",
        store,
        files,
        "projects/p/runs/thread",
        "12345678event",
        settings or Settings(_env_file=None),
    )


def test_streaming_ingest_and_files_info(store, monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer test-token"
        if request.url.host == "slack.com":
            return httpx.Response(200, json={"ok": True, "file": slack_file()})
        return httpx.Response(200, content=PNG)

    seen = mock_download(monkeypatch, respond)
    files = ingest(store, [slack_file(url_private_download=None, file_access="check_file_info")])
    f = files[0]
    assert f.sha256 == hashlib.sha256(PNG).hexdigest() and f.size == len(PNG)
    assert f.key.startswith("tasks/slack-analysis/uploads/p/runs/thread/attachments/")
    assert store.get(f.key)[0] == PNG
    assert f.path == "inputs/12345678/01-plot.png"
    assert len(calls) == 2 and seen[0]["follow_redirects"] is False


@pytest.mark.parametrize("status", [302, 403])
def test_redirects_and_error_responses_not_followed(store, monkeypatch, status):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://evil/x"})

    mock_download(monkeypatch, respond)
    with pytest.raises(ValueError, match="Slack did not"):
        ingest(store, [slack_file()])
    assert len(calls) == 1 and not store.keys("tasks")


@pytest.mark.parametrize("per_file", [True, False])
def test_actual_streamed_caps_ignore_declared_size(store, monkeypatch, per_file):
    mock_download(
        monkeypatch,
        lambda request: httpx.Response(200, content=b"x" * (1_000_001 if per_file else 600_000)),
    )
    files = [slack_file()] * 2
    settings = Settings(_env_file=None, attachment_max_mb=1, attachment_max_message_mb=1)
    with pytest.raises(ValueError, match="1 MB.*GCS"):
        ingest(store, files, settings)
    assert not any("02-" in key for key in store.keys("tasks"))


def test_preflight_rejections():
    settings = Settings(_env_file=None)
    files = [
        slack_file(mode="external"),
        slack_file(hidden_by_limit=True),
        slack_file(mode="tombstone"),
        slack_file(size=150_000_000),
    ]
    problems = a.slack_problems(files, settings)
    assert len(problems) == 4
    assert "GCS" in " ".join(problems)
    assert a.slack_problems([slack_file()] * 11, settings)


def test_merge_dedupe_and_agent_first_eviction(store):
    user = file_record(store).model_dump()
    old = file_record(store, id="o12345678", source="agent").model_dump()
    new = file_record(store, id="f22222222").model_dump()
    assert a.merge([user, old], [new, new], cap=2) == [user, new]
    assert a.merge([user], [new], cap=1) == [new]
    assert a.merge([user], [user]) == [user]


def test_router_media_limits_and_listing(store):
    files = [file_record(store, id=f"f{i:08x}") for i in range(6)]
    files += [file_record(store, id=f"o{i:08x}", name="p.pdf", data=b"%PDF-1.7") for i in range(3)]
    media = a.router_media(store, files)
    assert [b["type"] for b in media].count("image") == 4
    assert [b["type"] for b in media].count("document") == 2
    assert media[0]["text"] == "Attachment f00000000: plot.png"
    heads = a.router_heads(store, files, {f.id for f in files})
    listing = a.router_listing(files, {f.id for f in files}, heads, media=media)
    assert listing[0]["shown"] and not listing[4]["shown"] and listing[4]["reason"]
    assert all(f["current"] for f in listing)
    missing = files[0].model_copy(update={"key": "missing"})
    assert a.router_media(store, [missing]) == []
    large = files[0].model_copy(update={"size": 4_000_000})
    assert a.router_media(store, [large]) == []
    text = file_record(store, name="data.csv", data=b"a,b\n" * 5000)
    heads = a.router_heads(store, [text], {text.id})
    assert len(heads[text.id]) == 8192
    assert "head" not in a.router_listing([text], set(), {})[0]


def test_materialize_integrity_outputs_and_listing(store, tmp_path):
    user = file_record(store)
    output = file_record(store, id="o12345678", source="agent")
    missing = file_record(store, id="f33333333", name="missing.png").model_copy(
        update={"key": "gone"}
    )
    work = tmp_path / "work"
    present = a.materialize(
        store, work, [user.model_dump(), output.model_dump(), missing.model_dump()]
    )
    assert [f.id for f in present] == [user.id, output.id]
    path = work / user.path
    assert path.read_bytes() == PNG and path.stat().st_mode & 0o222 == 0
    path.chmod(0o600)
    path.write_bytes(b"tampered")
    a.materialize(store, work, [user.model_dump()])
    assert path.read_bytes() == PNG
    listing = a.worker_listing(present, {user.id})
    assert "attached to this message by <@U1>" in listing
    assert "you delivered it in turn 1" in listing
    assert "outputs/plot.png" in listing


def test_materialize_never_follows_workspace_links(store, tmp_path):
    f = file_record(store)
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (work / "inputs").symlink_to(outside)
    assert a.materialize(store, work, [f.model_dump()]) == []
    assert not list(outside.iterdir())
    malicious = f.model_copy(update={"path": "../escape"})
    assert a.materialize(store, work, [malicious.model_dump()]) == []


def test_gcs_streaming_primitives():
    from pistachio.agents.analysis.storage import GCSStorage

    storage = object.__new__(GCSStorage)
    storage.bucket = Mock()
    writer = storage.bucket.blob.return_value.open.return_value
    writer.__enter__ = Mock(return_value=writer)
    writer.__exit__ = Mock(return_value=False)
    with storage.open_write("key"):
        pass
    storage.bucket.blob.return_value.open.assert_called_once_with("wb", chunk_size=8 * 1024 * 1024)
    storage.download("key", Path("/tmp/copy"))
    storage.bucket.blob.return_value.download_to_filename.assert_called_once_with("/tmp/copy")


def test_materialize_restores_latest_version_of_reused_output_path(store, tmp_path):
    old = file_record(store, source="agent", id="o11111111", data=PNG + b"old")
    latest = file_record(store, source="agent", id="o22222222", data=PNG + b"new")
    latest.turn = 2
    work = tmp_path / "work"
    present = a.materialize(store, work, [old, latest])
    assert (work / latest.path).read_bytes() == PNG + b"new"
    assert [f.id for f in present] == [latest.id]


def test_thread_registry_and_combined_media_caps(store):
    records = [file_record(store, id=f"f{i:08x}") for i in range(52)]
    assert len(a.merge([], records)) == 50
    first = a.router_media(store, records[:4])
    second = a.router_media(store, records[4:8])
    combined = a.combine_media(first, second)
    assert len(combined) == 8
    assert a.shown_ids(combined) == {f.id for f in records[:4]}


def test_control_plane_and_worker_host_do_not_import_decoders():
    import ast

    root = Path(__file__).resolve().parents[1] / "src/pistachio"
    paths = [*(root / "agents/analysis").glob("*.py"), *(root / "api").glob("*.py")]
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                imports = [x.name for x in node.names]
            elif isinstance(node, ast.ImportFrom):
                imports = [node.module or ""]
            else:
                continue
            assert not any(name.split(".")[0] in {"PIL", "pypdfium2", "pypdf"} for name in imports)
