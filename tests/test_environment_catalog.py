import json
from dataclasses import replace
from pathlib import Path

from mica.agents.analysis import toolkit_catalog
from mica.agents.analysis.config import Settings
from mica.agents.analysis.runtime_install import RuntimeRecord
from mica.agents.analysis.runtime_store import PREFIX, RuntimeStore
from mica.agents.analysis.storage import LocalStorage
from mica.agents.analysis.tiers import capabilities


def runtime(settings):
    name = "r_" + "a" * 16
    return RuntimeRecord(
        id=name,
        kind="overlay",
        python=str(settings.runtime_root / name / "bin/python"),
        request=["torch"],
        python_version="3.12",
        base_fingerprint="base",
        top_level={"torch": "2.7.1"},
        packages={"torch": "2.7.1", "nvidia-cuda-runtime-cu12": "12.6"},
        validated={"ok": True},
    )


def test_capabilities_and_worker_list_identical_named_environments(tmp_path):
    settings = Settings(
        _env_file=None,
        runtimes_enabled=True,
        runtime_root=tmp_path,
        gpu_worker=True,
        gpu_tier_enabled=True,
        gpu_types="t4,l4",
        gpu_image="us-docker.pkg.dev/project/images/gpu@sha256:" + "a" * 64,
    )
    record = runtime(settings)
    gpu = toolkit_catalog.environment_view(settings, [record])
    catalog = capabilities(settings, [record])
    assert catalog["environments"]["gpu"] == gpu
    prompt = toolkit_catalog.environment_prompt(settings, [record])
    assert all(row["id"] in prompt and row["python"] in prompt for row in gpu)
    assert next(row for row in gpu if row["id"] == record.id)["cuda"]
    cpu = catalog["environments"]["cpu"]
    assert not any(row["cuda"] for row in cpu)
    assert (
        next(row for row in cpu if row["id"] == "cellpose-3.1.1.3")["python"]
        == "/opt/sap/bin/python"
    )
    assert (
        next(row for row in gpu if row["id"] == "cellpose-3.1.1.3")["python"]
        == "/opt/cellpose/3.1.1.3/bin/python"
    )


def test_only_validated_registered_runtime_paths_are_selectable(tmp_path):
    settings = Settings(
        _env_file=None, runtimes_enabled=True, runtime_root=tmp_path, gpu_worker=True
    )
    record = runtime(settings)
    invalid = [
        replace(record, id="../../python"),
        replace(record, python="/usr/bin/python"),
        replace(record, validated={"ok": False}),
        replace(record, kind="image"),
    ]
    assert len(toolkit_catalog.environments(settings, invalid)) == 4
    assert (
        len(
            toolkit_catalog.environments(
                settings.model_copy(update={"runtimes_enabled": False}), [record]
            )
        )
        == 4
    )
    cpu = replace(record, packages={"torch": "2.7.1+cpu"})
    assert not toolkit_catalog.environments(settings, [cpu])[-1].cuda


def test_gpu_image_stack_metadata_matches_committed_dependency_locks():
    rows = {
        row.id: row
        for row in toolkit_catalog.environments(Settings(_env_file=None, gpu_worker=True))
    }
    root = Path(__file__).resolve().parents[1]
    for name, filename in (
        ("cellpose-3.1.1.3", "requirements-cellpose3-gpu.txt"),
        ("cellpose-4.2.1.1", "requirements-cellpose-sam-gpu.txt"),
    ):
        packages = dict(value.split(" ", 1) for value in rows[name].packages)
        lock = (root / "docker" / filename).read_text()
        for package in ("torch", "nvidia-cuda-runtime-cu12"):
            assert any(
                line.startswith(f"{package}=={packages[package]} ") for line in lock.splitlines()
            )
        assert rows[name].cuda


def test_cpu_torch_with_cuda_dependency_is_not_advertised_as_cuda():
    assert not toolkit_catalog.runtime_cuda(
        {"torch": "2.7.1+cpu", "nvidia-cuda-runtime-cu12": "12.6.77"}
    )


def test_proseg_native_discovery_matches_pinned_build_for_cpu_and_gpu():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "deploy/proseg-cli.json").read_text())
    dockerfile = (root / "docker/Dockerfile.slack-release").read_text()
    for field in ("builder", "crate_url", "crate_sha256", "executable"):
        assert manifest[field] in dockerfile
    assert "cargo build --locked --release --bin proseg" in dockerfile
    for gpu in (False, True):
        settings = Settings(_env_file=None, gpu_worker=gpu)
        sap = next(row for row in toolkit_catalog.environment_view(settings) if row["id"] == "sap")
        assert not sap["cuda"]
        assert manifest["executable"] in sap["executables"][0]
        assert "Proseg CLI " + manifest["version"] in toolkit_catalog.environment_prompt(settings)


def test_unavailable_saved_runtime_store_does_not_block_conversation(monkeypatch):
    def unavailable(self):
        raise OSError("storage unavailable")

    monkeypatch.setattr(RuntimeStore, "index", unavailable)
    assert (
        toolkit_catalog.saved_runtimes(object(), Settings(_env_file=None, runtimes_enabled=True))
        == []
    )


def test_index_descriptor_roundtrip_needs_no_manifest_reads(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, runtimes_enabled=True, runtime_root=tmp_path / "runtimes")
    storage = LocalStorage(tmp_path / "storage")
    record = runtime(settings)
    store = RuntimeStore(storage, settings)
    store.update_index(record)
    entry = store.index()[record.id]
    assert "packages" not in entry["environment"]
    assert entry["environment"]["top_level"] == record.top_level

    def no_manifest(self, name):
        raise AssertionError("Discovery must not read the immutable manifest")

    monkeypatch.setattr(RuntimeStore, "load", no_manifest)
    discovered = toolkit_catalog.saved_runtimes(storage, settings)
    assert len(discovered) == 1 and discovered[0].id == record.id
    gpu = settings.model_copy(update={"gpu_worker": True})
    assert toolkit_catalog.environment_view(gpu, discovered) == toolkit_catalog.environment_view(
        gpu, [record]
    )


def test_legacy_discovery_is_bounded_and_malformed_descriptors_are_not_used(tmp_path, monkeypatch):
    import json

    settings = Settings(_env_file=None, runtimes_enabled=True, runtime_root=tmp_path / "runtimes")
    storage = LocalStorage(tmp_path / "storage")
    record = runtime(settings)
    malformed = toolkit_catalog.environment_descriptor(record)
    malformed["version"] = 2
    index = {f"r_{number:016x}": {"created_at": str(number)} for number in range(10)}
    index[record.id] = {"environment": malformed, "created_at": "newest"}
    storage.put(f"{PREFIX}/index.json", json.dumps(index).encode())
    loaded = []

    def load(self, name):
        loaded.append(name)
        return replace(record, id=name, python=str(settings.runtime_root / name / "bin/python"))

    monkeypatch.setattr(RuntimeStore, "load", load)
    assert len(toolkit_catalog.saved_runtimes(storage, settings)) == 3
    assert len(loaded) == 3 and record.id not in loaded
    descriptor = toolkit_catalog.environment_descriptor(record)
    for changes in ({"python": "/usr/bin/python"}, {"validated": False}, {"cuda": "true"}):
        assert (
            toolkit_catalog.descriptor_environment(record.id, {**descriptor, **changes}, settings)
            is None
        )
