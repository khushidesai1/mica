from __future__ import annotations

import hashlib
import io
import json
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zipfile import ZipFile

import httpx
import pytest

from mica.agents.analysis import runtime_install, runtime_network, runtime_policy, runtime_tools
from mica.agents.analysis.config import Settings
from mica.agents.analysis.runtime_install import (
    Installer,
    ResolutionFailure,
    RuntimeFailure,
    RuntimeRecord,
)
from mica.agents.analysis.runtime_network import WheelMirror
from mica.agents.analysis.runtime_policy import POLICY_VERSION, RuntimePolicyError, normalize
from mica.agents.analysis.runtime_sdist import SourceDistribution, extract_source
from mica.agents.analysis.runtime_store import PREFIX, RuntimeStore, encode
from mica.agents.analysis.runtime_tools import RuntimeManager, ensure_tool
from mica.agents.analysis.runtime_wheels import check_pure_wheel, check_wheel
from mica.agents.analysis.storage import LocalStorage


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, runtimes_enabled=True, runtime_root=tmp_path / "runtimes")


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    # No listener or live network is needed for policy and resolver tests.
    server = Mock(server_port=1234)
    monkeypatch.setattr(runtime_network, "ThreadingHTTPServer", lambda *args: server)
    mirror = WheelMirror(tmp_path / "downloads", set(), 1024**2)
    mirror.root.mkdir()
    return mirror


def index_file(name="docrep", version="0.3.2", *, age=30, wheel=False, **changes):
    filename = f"{name}-{version}" + ("-py3-none-any.whl" if wheel else ".tar.gz")
    return {
        "filename": filename,
        "url": "https://files.pythonhosted.org/packages/" + filename,
        "upload-time": (datetime.now(UTC) - timedelta(days=age)).isoformat(),
        "hashes": {"sha256": "a" * 64},
        **changes,
    }


def wheel(path, *, name="docrep", version="0.3.2", tag="py3-none-any", dependency=""):
    path.mkdir(parents=True, exist_ok=True)
    target = path / f"{name}-{version}-{tag}.whl"
    with ZipFile(target, "w") as archive:
        archive.writestr(f"{name}/__init__.py", "__version__ = '0.3.2'\n")
        archive.writestr(
            f"{name}-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            + (f"Requires-Dist: {dependency}\n" if dependency else ""),
        )
        archive.writestr(
            f"{name}-{version}.dist-info/WHEEL",
            f"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: {tag}\n",
        )
    return target


def source_archive(tmp_path, members=None):
    path = tmp_path / "docrep-0.3.2.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for name, content in (members or {"docrep-0.3.2/setup.py": b"# fixture"}).items():
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    return SourceDistribution(
        "docrep",
        "0.3.2",
        path.name,
        "https://files.pythonhosted.org/packages/" + path.name,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    ), path


def test_vendored_metadata_does_not_replace_root_wheel_identity(tmp_path):
    path = wheel(tmp_path, name="setuptools", version="84.2.1")
    with ZipFile(path, "a") as archive:
        archive.writestr(
            "setuptools/_vendor/packaging-26.3.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: packaging\nVersion: 26.3\n",
        )
        archive.writestr(
            "setuptools/_vendor/packaging-26.3.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
    assert check_wheel(path, set(), 1024**2) == ("setuptools", "84.2.1")
    assert check_pure_wheel(path, set(), 1024**2) == ("setuptools", "84.2.1")
    with pytest.raises(RuntimePolicyError, match="denied"):
        check_wheel(path, {"setuptools"}, 1024**2)
    with ZipFile(path, "a") as archive:
        archive.writestr(
            "other-1.dist-info/METADATA", "Metadata-Version: 2.1\nName: other\nVersion: 1\n"
        )
    with pytest.raises(ValueError, match="Invalid wheel metadata"):
        check_wheel(path, set(), 1024**2)


def record(settings):
    return RuntimeRecord(
        id="r_" + "1" * 16,
        kind="isolated",
        python=str(settings.runtime_root / "r_" / "bin/python"),
        request=["docrep==0.3.2"],
        python_version="3.12.12",
        base_fingerprint=None,
        top_level={"docrep": "0.3.2"},
        packages={"docrep": "0.3.2"},
        validated={"ok": True, "imports": ["docrep"]},
    )


def test_agent_git_sources_refused_but_reviewed_catalog_source_survives():
    with pytest.raises(RuntimePolicyError, match="reviewed catalog"):
        normalize(["git+https://github.com/example/tool@main"])
    request = normalize(["Decipher"])
    assert request.specs == ("scdecipher==0.1.3",)
    assert request.sources[0].commit == "9610b317844c8515e077a81abafea07c7494a54c"


@pytest.mark.parametrize("spelling", ["scikit-learn", "Scikit_Learn", "scikit.learn"])
def test_denylist_normalizes_equivalent_package_names(spelling):
    with pytest.raises(RuntimePolicyError, match="denied"):
        normalize([spelling], denylist={"Scikit_Learn"})


def test_mirror_hides_source_files_from_uv_but_retains_pinned_candidate(mirror, monkeypatch):
    doc = {"name": "docrep", "files": [index_file()]}
    monkeypatch.setattr(mirror, "fetch", lambda *args, **kwargs: json.dumps(doc).encode())
    response, _ = mirror.get("/simple/docrep/")
    assert json.loads(response)["files"] == []
    candidate = mirror.source_candidates(
        "No solution found: docrep>=0.3.2 has no wheels", [], "3.12.12"
    )
    assert len(candidate) == 1 and candidate[0].version == "0.3.2"
    with pytest.raises(ValueError, match="Unrecognized"):
        mirror.get("/files/" + candidate[0].filename)


def test_mirror_single_flight_cache_and_transient_failure_over_http(tmp_path, monkeypatch):
    calls = []
    document = {"name": "docrep", "files": [index_file()]}
    with WheelMirror(tmp_path / "http", {"boto3"}, 1024**2) as server:
        def fetch(url, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                raise httpx.ConnectTimeout("temporary registry failure")
            time.sleep(0.05)
            return json.dumps(document).encode()

        monkeypatch.setattr(server, "fetch", fetch)
        with httpx.Client(trust_env=False, timeout=3) as client:
            url = server.url + "/simple/docrep/"
            assert client.get(url).status_code == 502
            assert not server.policy_errors and not server.simple
            with ThreadPoolExecutor(max_workers=12) as pool:
                responses = list(pool.map(lambda _: client.get(url), range(12)))
            assert all(response.status_code == 200 for response in responses)
            assert all(not response.json()["files"] for response in responses)
            assert len(calls) == 2 and server.indexes["docrep"] == document["files"]
            server.add_wheel(wheel(tmp_path / "built"))
            assert "/built/docrep/" in client.get(url).json()["files"][0]["url"]
            assert len(calls) == 2
            assert client.get(server.url + "/simple/boto3/").status_code == 403
            assert server.policy_errors == ["Package denied by runtime policy"]


@pytest.mark.parametrize("path, upstream, expected", [
    ("/simple/proseg-py/", 404, 404),
    ("/simple/proseg-py/", 429, 502),
    ("/simple/proseg-py/", 500, 502),
    ("/simple/proseg-py/", 503, 502),
    ("/files/sample.whl", 404, 502),
])
def test_mirror_preserves_missing_package_without_hiding_registry_outages(
    tmp_path, monkeypatch, path, upstream, expected
):
    with WheelMirror(tmp_path / "http", set(), 1024**2) as server:
        def fetch(url, **kwargs):
            response = httpx.Response(upstream, request=httpx.Request("GET", url))
            response.raise_for_status()

        monkeypatch.setattr(server, "fetch", fetch)
        server.urls["sample.whl"] = "https://files.pythonhosted.org/packages/sample.whl"
        with httpx.Client(trust_env=False, timeout=3) as client:
            response = client.get(server.url + path)
        assert response.status_code == expected
        assert not server.policy_errors and not server.simple
        if expected == 404:
            assert "Package not found" in response.text
            assert not runtime_install.transient_failure(str(response.status_code) + response.text)
        else:
            assert runtime_install.transient_failure("502 Bad Gateway")


def test_source_selection_obeys_age_python_constraints_and_existing_wheels(mirror):
    mirror.indexes["docrep"] = [
        index_file(version="0.3.1"),
        index_file(),
        index_file(version="0.4.0", age=1),
        index_file(version="0.5.0", **{"requires-python": ">=3.13"}),
    ]
    error = "No solution found: docrep has no wheels"
    assert mirror.source_candidates(error, ["docrep<0.3.2"], "3.12.12")[0].version == "0.3.1"
    assert mirror.source_candidates(error, [], "3.12.12")[0].version == "0.3.2"
    mirror.indexes["docrep"] = [index_file(), index_file(wheel=True)]
    assert not mirror.source_candidates(error, [], "3.12.12")
    assert not mirror.source_candidates("Network failure", [], "3.12.12")
    assert not mirror.source_candidates("No solution found: unrelated", [], "3.12.12")
    mirror.indexes["docrep"] = [index_file(age=1)]
    with pytest.raises(RuntimePolicyError, match="minimum runtime age"):
        mirror.source_candidates(error, [], "3.12.12")


@pytest.mark.parametrize(
    "change",
    [
        {"url": "https://evil.example/docrep.tar.gz"},
        {"url": "https://files.pythonhosted.org@evil.example/docrep.tar.gz"},
        {"url": "http://files.pythonhosted.org/docrep.tar.gz"},
        {"hashes": {}},
    ],
)
def test_source_candidates_require_trusted_host_and_sha256(mirror, change):
    mirror.indexes["docrep"] = [index_file(**change)]
    with pytest.raises(RuntimePolicyError, match="Untrusted or unpinned"):
        mirror.source_candidates("No solution found: docrep has no wheels", [], "3.12.12")


def test_source_download_is_pinned_and_hash_checked(mirror, tmp_path, monkeypatch):
    mirror.indexes["docrep"] = [index_file()]
    source = mirror.source_candidates("No solution found: docrep has no wheels", [], "3.12.12")[0]
    monkeypatch.setattr(mirror, "fetch", lambda url, destination: destination.write_bytes(b"wrong"))
    with pytest.raises(RuntimePolicyError, match="SHA256"):
        mirror.fetch_source(source, tmp_path / "source.tar.gz")
    with pytest.raises(RuntimePolicyError, match="observed PyPI index"):
        mirror.fetch_source(replace(source, sha256="b" * 64), tmp_path / "source.tar.gz")


@pytest.mark.parametrize(
    "members",
    [
        {"../setup.py": b"bad"},
        {"docrep-0.3.2/setup.py": None},
        {"docrep-0.3.2/setup.py": b"", "docrep-0.3.2/ext.c": b"code"},
    ],
)
def test_unsafe_or_compiled_sources_require_admin_build(tmp_path, members):
    source, archive = source_archive(tmp_path, members)
    with pytest.raises(ValueError, match="Unsafe|admin-built"):
        extract_source(source, archive, tmp_path / "source", 1024**2)


def test_source_extract_preserves_only_verified_bounded_files(tmp_path):
    source, archive = source_archive(tmp_path)
    root = extract_source(source, archive, tmp_path / "source", 1024**2)
    assert root.name == "docrep-0.3.2" and (root / "setup.py").read_bytes() == b"# fixture"
    with pytest.raises(ValueError, match="SHA256"):
        extract_source(replace(source, sha256="b" * 64), archive, tmp_path / "bad", 1024**2)
    with pytest.raises(ValueError, match="size limit"):
        extract_source(source, archive, tmp_path / "big", 1)


def test_only_pure_python_wheel_with_matching_metadata_is_accepted(tmp_path):
    assert check_pure_wheel(wheel(tmp_path), set(), 1024**2) == ("docrep", "0.3.2")
    with pytest.raises(ValueError, match="admin-built"):
        check_pure_wheel(wheel(tmp_path, tag="cp312-cp312-manylinux_2_17_x86_64"), set(), 1024**2)
    with pytest.raises(RuntimePolicyError, match="denied dependency"):
        check_pure_wheel(wheel(tmp_path, dependency="boto3"), {"boto3"}, 1024**2)


def test_pure_source_build_runs_existing_offline_builder(settings, tmp_path, monkeypatch):
    installer = Installer(settings)
    installer.scratch = tmp_path
    installer.network = {"HTTP_PROXY": "http://127.0.0.1:1234"}
    source, archive = source_archive(tmp_path)
    source_path = extract_source(source, archive, tmp_path / "source", 1024**2)
    monkeypatch.setattr(runtime_install.shutil, "which", lambda name: "/usr/bin/" + name)
    calls = []
    monkeypatch.setattr(installer, "resolve", lambda *args: "pip==1 --hash=sha256:" + "a" * 64)
    monkeypatch.setattr(
        installer, "skeleton", lambda path, kind: (path / "bin").mkdir(parents=True)
    )

    def run(args, **kwargs):
        calls.append(args)
        if args[0] == "bwrap":
            wheel(tmp_path / "wheel-output-test")
        return ""

    monkeypatch.setattr(installer, "run", run)
    built, name, version = installer.build_wheel(source_path, "test", pure=True)
    assert built.is_file() and (name, version) == ("docrep", "0.3.2")
    install, build = calls
    assert "--require-hashes" in install and "--only-binary" in install
    assert "--unshare-all" in build and "--clearenv" in build
    assert "--no-build-isolation" in build[-1] and "--no-index" in build[-1]
    assert "Sandbox network is reachable" in build[-1]


def test_scvi_shaped_resolution_builds_source_then_retries_only_once(
    settings, mirror, tmp_path, monkeypatch
):
    installer = Installer(settings)
    installer.scratch, installer.mirror = tmp_path, mirror
    source, archive = source_archive(tmp_path)
    mirror.indexes["docrep"] = [index_file(hashes={"sha256": source.sha256})]
    monkeypatch.setattr(mirror, "fetch_source", lambda expected, path: None)
    assert archive.name == source.filename
    calls = []

    def resolve(specs, kind, base):
        calls.append((specs, kind))
        if "docrep" not in mirror.local_wheels:
            raise ResolutionFailure(
                "No solution found: scvi-tools depends on docrep>=0.3.2 with no wheels"
            )
        return (
            "docrep==0.3.2 --hash=sha256:"
            + hashlib.sha256(mirror.local_wheels["docrep"].read_bytes()).hexdigest()
        )

    def build(path, label, *, pure):
        assert pure and (path / "setup.py").is_file()
        installer.built = tmp_path / "built"
        installer.built.mkdir()
        return wheel(tmp_path / "output"), "docrep", "0.3.2"

    monkeypatch.setattr(installer, "resolve", resolve)
    monkeypatch.setattr(installer, "build_wheel", build)
    lock = installer.resolve_runtime(["scvi-tools==1.4.0"], "isolated", {}, "3.12.12")
    assert len(calls) == 2 and "--hash=sha256:" in lock
    assert installer.source_built[0]["source_sha256"] == source.sha256
    assert installer.source_built[0]["wheel_sha256"] == lock.split("sha256:")[1]
    assert installer.source_built[0]["name"] == "docrep"


def test_source_build_cap_prevents_any_build(settings, mirror, tmp_path, monkeypatch):
    settings.runtime_max_source_builds = 0
    installer = Installer(settings)
    installer.scratch, installer.mirror = tmp_path, mirror
    mirror.indexes["docrep"] = [index_file()]
    monkeypatch.setattr(
        installer, "resolve", Mock(side_effect=ResolutionFailure("No solution found: docrep"))
    )
    build = Mock()
    monkeypatch.setattr(installer, "build_sdist", build)
    with pytest.raises(RuntimePolicyError, match="source-build limit"):
        installer.resolve_runtime(["scvi-tools"], "isolated", {}, "3.12.12")
    build.assert_not_called()


def test_failure_cache_expires_and_old_policy_records_are_ignored(settings, tmp_path, monkeypatch):
    store = RuntimeStore(LocalStorage(tmp_path / "store"), settings)
    request, base = normalize(["docrep"]), ("3.12.12", "base", {})
    key = store.failure_key(request, base, False)
    store.save_failure(key, "temporary error")
    assert store.failure(key) == "temporary error"
    raw, _ = store.storage.get(f"{PREFIX}/failures/{key}.json")
    entry = json.loads(raw)
    entry["created_at"] = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    store.storage.put(f"{PREFIX}/failures/{key}.json", json.dumps(entry).encode())
    assert store.failure(key) is None
    store.storage.put(f"{PREFIX}/failures/{key}.json", b'{"error":"old forever cache"}')
    assert store.failure(key) is None
    monkeypatch.setattr(runtime_policy, "POLICY_VERSION", POLICY_VERSION + 1)
    assert store.failure_key(request, base, False) != key
    old = replace(record(settings), policy_version=POLICY_VERSION - 1)
    store.storage.put(f"{PREFIX}/index.json", json.dumps({old.id: {}}).encode())
    store.storage.put(f"{PREFIX}/runtimes/{old.id}/runtime.json", encode(old))
    assert store.find(request, base) is None


@pytest.mark.parametrize("stored", [False, True])
def test_policy_refusals_are_recomputed_and_not_cached(settings, tmp_path, stored):
    installer = Mock()
    installer.identity.return_value = ("3.12.12", "base", {})
    installer.create.side_effect = RuntimePolicyError("Package is too new")
    storage = LocalStorage(tmp_path / "store") if stored else None
    manager = RuntimeManager(settings, installer=installer, storage=storage)
    for _ in range(2):
        with pytest.raises(RuntimePolicyError, match="too new"):
            manager.ensure(["docrep"], "Read documentation")
    assert installer.create.call_count == 2 and not manager.failures
    assert storage is None or not storage.keys(f"{PREFIX}/failures")


@pytest.mark.parametrize("stored", [False, True])
def test_transitive_build_policy_refusal_is_not_cached(
    settings, mirror, tmp_path, monkeypatch, stored
):
    installer = Installer(settings)
    installer.mirror = mirror
    source, archive = source_archive(tmp_path)
    monkeypatch.setattr(
        mirror, "fetch_source", lambda _, path: path.write_bytes(archive.read_bytes())
    )
    monkeypatch.setattr(runtime_install.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(installer, "identity", lambda: ("3.12.12", "base", {}))

    def refuse_build_dependency(specs, kind, base):
        assert "setuptools" in specs and "boto3" not in specs
        mirror.policy_errors.append("Package boto3 is denied by runtime policy.")
        raise ResolutionFailure("No solution found for a transitive build dependency")

    resolve = Mock(side_effect=refuse_build_dependency)
    monkeypatch.setattr(installer, "resolve", resolve)

    def build(*args, **kwargs):
        installer.scratch = tmp_path / f"attempt-{resolve.call_count}"
        installer.scratch.mkdir()
        installer.build_sdist(source)

    monkeypatch.setattr(installer, "create", build)
    storage = LocalStorage(tmp_path / "store") if stored else None
    manager = RuntimeManager(settings, installer=installer, storage=storage)
    for _ in range(2):
        with pytest.raises(RuntimePolicyError, match="boto3.*denied"):
            manager.ensure(["docrep"], "Read documentation")
    assert resolve.call_count == 2 and not manager.failures
    assert storage is None or not storage.keys(f"{PREFIX}/failures")


async def test_mcp_install_failure_records_receipt_and_next_call_can_continue(settings, tmp_path):
    manager = RuntimeManager(
        settings,
        storage=LocalStorage(tmp_path / "store"),
        event_id="a" * 32,
        attempt=2,
    )
    manager.ensure = Mock(side_effect=RuntimeFailure("Compiler required"))
    tool = ensure_tool(manager)
    result = await tool.handler({"packages": ["docrep"], "reason": "Test package setup"})
    assert result["isError"] and "Compiler required" in result["content"][0]["text"]
    key = "tasks/slack-analysis/events/" + "a" * 32 + "/runtime-failure.json"
    assert json.loads(manager.store.storage.get(key)[0]) == {
        "event_id": "a" * 32,
        "attempt": 2,
        "cause": "runtime_install_failed",
    }
    # A clean policy refusal remains a tool result, but does not create an install incident.
    manager.report_failure = AsyncMock()
    manager.ensure.side_effect = RuntimePolicyError("Package denied")
    result = await tool.handler({"packages": ["boto3"], "reason": "Test policy"})
    assert result["isError"]
    manager.report_failure.assert_not_awaited()
    manager.ensure.side_effect = None
    event = manager.record(record(settings), "Continue", reused=True, saved=True)
    manager.ensure.return_value = event
    result = await tool.handler({"packages": ["docrep"], "reason": "Continue"})
    assert json.loads(result["content"][0]["text"])["status"] == "ready"


@pytest.mark.parametrize("cuda", [False, True])
async def test_package_result_identifies_gpu_environment_without_claiming_validation(
    settings, cuda
):
    settings.gpu_worker = True
    runtime = record(settings)
    runtime.python = str(settings.runtime_root / runtime.id / "bin/python")
    runtime.packages = {
        "torch": "2.7.1" if cuda else "2.7.1+cpu",
        "nvidia-cuda-runtime-cu12": "12.6.77",
    }
    manager = RuntimeManager(settings)
    manager.ensure = Mock(return_value=manager.record(runtime, "Ready", reused=True, saved=True))
    result = await ensure_tool(manager).handler({"packages": ["torch"], "reason": "Use CUDA"})
    body = json.loads(result["content"][0]["text"])
    assert body["environment"]["id"] == runtime.id
    assert body["environment"]["cuda"] is cuda
    assert body["gpu_validation"] == "not_run"
    if cuda:
        assert "small CUDA operation" in body["execution_guidance"]
    else:
        assert "not registered for CUDA" in body["execution_guidance"]
        assert "do not replace requested GPU work" in body["execution_guidance"]


async def test_saved_cuda_runtime_can_be_selected_by_existing_gpu_tool_in_same_turn(
    settings, tmp_path, monkeypatch
):
    from mica.agents.analysis import gpu_tools

    settings.gpu_worker = True
    runtime = record(settings)
    runtime.python = str(settings.runtime_root / runtime.id / "bin/python")
    runtime.packages = {"torch": "2.7.1", "nvidia-cuda-runtime-cu12": "12.6.77"}
    python = Path(runtime.python)
    python.parent.mkdir(parents=True)
    python.write_text("# Selection fixture, never executed\n")
    installer = Mock()
    installer.identity.return_value = ("3.12.12", "base", {})
    manager = RuntimeManager(settings, installer=installer)
    manager.store = Mock()
    manager.store.find.return_value = runtime
    save = AsyncMock(return_value={"fixture": True})

    async def select_only(request, work, data, config, records):
        selected = gpu_tools.select_environment(request.environment, config, records)
        return {"environment": selected.id, "exit_status": 0, "timed_out": False}

    monkeypatch.setattr(gpu_tools, "execute", select_only)
    tool = gpu_tools.tools(
        tmp_path, tmp_path, settings, save, records=lambda: manager.records.values()
    )[0]
    request = {
        "environment": runtime.id, "script": "code/run.py", "output_dir": "outputs/check"
    }
    assert (await tool.handler(request))["isError"]
    restored = await ensure_tool(manager).handler({"packages": ["torch"], "reason": "Restore"})
    body = json.loads(restored["content"][0]["text"])
    assert body["environment"]["id"] == runtime.id and body["environment"]["cuda"]
    assert body["gpu_validation"] == "not_run"
    assert not (await tool.handler(request))["isError"]
    manager.store.restore.assert_called_once_with(runtime, installer)
    installer.create.assert_not_called()
    save.assert_awaited_once()


def test_setup_notice_includes_built_source_packages(settings):
    runtime = record(settings)
    runtime.source_built = [{"name": "docrep", "version": "0.3.2"}]
    manager = RuntimeManager(settings)
    event = manager.record(runtime, "Analyze", reused=False, saved=True)
    assert "Built from pure-Python source: docrep 0.3.2" in event.notice()
    event.reused = True
    assert event.notice() == ""


@pytest.mark.parametrize("gpu", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_mcp_registration_is_lazy_for_cpu_and_gpu_workers(
    settings, tmp_path, monkeypatch, gpu, enabled
):
    from mica.agents.analysis.runner import Context, build_options

    settings.runtimes_enabled, settings.gpu_worker = enabled, gpu
    installer = Mock()
    manager = RuntimeManager(settings, installer=installer)
    registered = []

    def server(name, version, tools):
        registered.extend(tool.name for tool in tools)
        return {"type": "sdk", "name": name, "instance": Mock()}

    monkeypatch.setattr(runtime_tools, "create_sdk_mcp_server", server)
    options = build_options(
        Context(tmp_path / "work", tmp_path / "data"), settings, runtime_manager=manager
    )
    assert (runtime_tools.TOOL_NAME in options.allowed_tools) == enabled
    assert ("ensure_packages" in registered) == enabled
    assert not installer.mock_calls


def test_optional_cloud_extra_does_not_block_core_scvi_tools(tmp_path, mirror):
    from mica.agents.analysis.runtime_wheels import check_metadata

    metadata = b"Name: scvi-tools\nVersion: 1.5.2\nRequires-Dist: boto3; extra == 'hub'\n"
    assert check_metadata(metadata, {"boto3"}) == ("scvi-tools", "1.5.2")
    mirror.denied = {"boto3"}
    with pytest.raises(RuntimePolicyError, match="denied"):
        mirror.get("/simple/boto3/")
    with pytest.raises(RuntimePolicyError, match="denied dependency"):
        check_metadata(metadata.replace(b"; extra == 'hub'", b""), {"boto3"})


def test_wheel_only_minimum_age_refusal_is_not_an_install_failure(mirror):
    mirror.indexes["docrep"] = [index_file(age=1, wheel=True)]
    with pytest.raises(RuntimePolicyError, match="minimum runtime age"):
        mirror.source_candidates(
            "No solution found: docrep was published too recently", [], "3.12.12"
        )


def test_saved_source_built_runtime_is_reused_across_projects(settings, tmp_path, monkeypatch):
    storage = LocalStorage(tmp_path / "store")
    store = RuntimeStore(storage, settings)
    saved = record(settings)
    saved.source_built = [{"name": "docrep", "version": "0.3.2", "source_sha256": "a" * 64}]
    path = store.path(saved.id)
    path.mkdir(parents=True)
    lock = "docrep==0.3.2 --hash=sha256:" + "a" * 64
    (path / "lock.txt").write_text(lock)
    saved.lock_sha256 = hashlib.sha256(lock.encode()).hexdigest()
    store.save(saved)
    installer = Mock()
    installer.identity.return_value = ("3.12.12", "different-base", {})
    manager = RuntimeManager(
        settings, installer=installer, principal="another@example.com", storage=storage
    )
    restore = Mock()
    monkeypatch.setattr(manager.store, "restore", restore)
    event = manager.ensure(["docrep>=0.3"], "Reuse shared analysis dependencies")
    assert event.reused and event.saved and event.source_built == saved.source_built
    assert not event.notice()
    installer.create.assert_not_called()
    restore.assert_called_once()


def test_resolution_refreshes_newly_built_wheel_metadata(settings, mirror, tmp_path, monkeypatch):
    installer = Installer(settings)
    installer.scratch, installer.mirror = tmp_path, mirror
    installer.network = {"HTTP_PROXY": mirror.url}
    mirror.add_wheel(wheel(tmp_path / "built"))
    commands = []

    def run(args, **kwargs):
        commands.append(args)
        (tmp_path / "lock.txt").write_text("docrep==0.3.2 --hash=sha256:" + "a" * 64)
        return ""

    monkeypatch.setattr(installer, "run", run)
    installer.resolve(["docrep"], "isolated", {})
    command = commands[0]
    assert command[command.index("--refresh-package") + 1] == "docrep"
    assert "--only-binary" in command and "--exclude-newer" in command


def test_source_retry_does_not_misread_old_wheel_availability_as_a_requirement(mirror):
    mirror.indexes["docrep"] = [index_file(version="0.3.1", wheel=True), index_file()]
    error = (
        "No solution found: only docrep<=0.3.1 is available and scvi-tools depends on\n"
        "docrep>=0.3.2, so requirements are unsatisfiable."
    )
    assert mirror.source_candidates(error, ["scvi-tools"], "3.12.12")[0].version == "0.3.2"


def test_complete_dependency_set_remains_bounded():
    packages = [f"package-{i}==1.0" for i in range(100)]
    assert len(normalize(packages).specs) == 100
    with pytest.raises(ValueError, match="100 packages"):
        normalize(packages + ["overflow"])
    with pytest.raises(ValueError, match="URLs"):
        normalize(packages[:-1] + ["unsafe @ https://example.com/package.whl"])


def test_setup_storage_has_separate_limits_and_reserves_analysis_space(settings, monkeypatch):
    settings.runtime_max_gb = 10 / 1024**3
    settings.runtime_max_scratch_gb = 30 / 1024**3
    settings.runtime_max_local_gb = 100 / 1024**3
    installer = Installer(settings)
    installer.scratch = settings.runtime_root / ".setup-test"
    installer.scratch.mkdir(parents=True)
    runtime = settings.runtime_root / "r_test"
    runtime.mkdir()
    (installer.scratch / "downloads").write_bytes(b"x" * 20)
    (runtime / "package").write_bytes(b"x" * 5)
    disk = SimpleNamespace(free=100 * 1024**3)
    monkeypatch.setattr(runtime_install.shutil, "disk_usage", lambda _: disk)
    installer.check_size()  # Setup can exceed the installed-environment budget.
    (installer.scratch / "downloads").write_bytes(b"x" * 31)
    with pytest.raises(RuntimeFailure, match="setup storage limit"):
        installer.check_size()
    (installer.scratch / "downloads").write_bytes(b"x" * 20)
    (runtime / "package").write_bytes(b"x" * 11)
    with pytest.raises(RuntimeFailure, match="Runtime exceeds"):
        installer.check_size()
    (runtime / "package").write_bytes(b"x" * 5)
    disk.free = 19 * 1024**3
    with pytest.raises(RuntimeFailure, match="disk reserve"):
        installer.check_size()


@pytest.mark.parametrize("stored", [False, True])
def test_transient_retries_preserve_versions_and_distinct_runtime_budget(
    settings, tmp_path, stored
):
    settings.runtime_max_new_per_turn = 1
    installer = Mock()
    installer.identity.return_value = ("3.12.12", "base", {})
    installer.create.side_effect = RuntimeFailure("Failed to download cuDNN: operation timed out")
    storage = LocalStorage(tmp_path / "store") if stored else None
    manager = RuntimeManager(settings, installer=installer, storage=storage)
    for _ in range(3):
        with pytest.raises(RuntimeFailure, match="operation timed out"):
            manager.ensure(["torch==2.4.1"], "Use requested CUDA runtime", isolated=True)
    assert manager.new_count == 1 and installer.create.call_count == 3
    with pytest.raises(RuntimeFailure, match="three attempts"):
        manager.ensure(["torch==2.4.1"], "Same requirements", isolated=True)
    assert not manager.failures
    assert storage is None or not storage.keys(f"{PREFIX}/failures/")
    with pytest.raises(RuntimeFailure, match="new-runtime limit"):
        manager.ensure(["torch==2.7.1"], "Different dependency set", isolated=True)


def test_old_transient_cache_is_ignored_but_resolution_failure_still_cached(settings, tmp_path):
    store = RuntimeStore(LocalStorage(tmp_path / "store"), settings)
    key = store.failure_key(normalize(["torch"]), ("3.12.12", "base", {}), True)
    store.storage.put(f"{PREFIX}/failures/{key}.json", json.dumps({
        "created_at": datetime.now(UTC).isoformat(), "policy_version": POLICY_VERSION,
        "error": "Failed to download nvidia-cudnn-cu12: operation timed out",
    }).encode())
    assert store.failure(key) is None
    store.save_failure(key, "No solution found: conflicting package versions")
    assert "conflicting" in store.failure(key)
    settings.runtime_max_scratch_gb += 1
    assert store.failure_key(normalize(["torch"]), ("3.12.12", "base", {}), True) != key


def test_retried_wheel_clients_share_one_verified_download(tmp_path, monkeypatch):
    artifact = wheel(tmp_path / "wheel").read_bytes()
    calls = []
    started, finish = threading.Event(), threading.Event()
    with WheelMirror(tmp_path / "mirror", set(), 1024**2) as mirror:
        mirror.urls["fixture/docrep.whl"] = "https://files.pythonhosted.org/docrep.whl"

        def fetch(url, *, destination):
            calls.append(url)
            started.set()
            assert finish.wait(3)
            destination.write_bytes(artifact)

        monkeypatch.setattr(mirror, "fetch", fetch)
        with httpx.Client(trust_env=False, timeout=3) as client:
            url = mirror.url + "/files/fixture/docrep.whl"
            with ThreadPoolExecutor(max_workers=8) as pool:
                abandoned = pool.submit(client.get, url, timeout=0.05)
                assert started.wait(2)
                with pytest.raises(httpx.ReadTimeout):
                    abandoned.result()
                readers = [pool.submit(client.get, url) for _ in range(6)]
                finish.set()
                responses = [reader.result() for reader in readers]
            assert all(r.status_code == 200 and r.content == artifact for r in responses)
        assert len(calls) == 1 and not mirror.policy_errors
        assert not list(mirror.root.glob("*.tmp"))


def test_mirror_shutdown_joins_downloads_before_scratch_cleanup(tmp_path, monkeypatch):
    started, stopped = threading.Event(), threading.Event()
    mirror = WheelMirror(tmp_path / "mirror", set(), 1024**2)
    mirror.urls["fixture/wheel"] = "https://files.pythonhosted.org/wheel"

    def fetch(url, *, destination):
        destination.write_bytes(b"partial")
        started.set()
        assert mirror.stopping.wait(3)
        stopped.set()
        raise httpx.ReadError("Package setup stopped")

    monkeypatch.setattr(mirror, "fetch", fetch)
    with ThreadPoolExecutor() as pool:
        with mirror:
            request = pool.submit(httpx.get, mirror.url + "/files/fixture/wheel", timeout=3)
            assert started.wait(2)
        assert stopped.is_set()
        assert not list(mirror.root.glob("*.tmp"))
        assert request.result().status_code == 502
