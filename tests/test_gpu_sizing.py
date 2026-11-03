"""Resource guarantees, exact GPU approvals and provider placement without live allocations."""

import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.api_core.exceptions import NotFound
from google.cloud import batch_v1
from test_analysis_batch import bridge  # noqa: F401
from test_gpu_compute import GPU_IMAGE, click, gpu, start  # noqa: F401

from mica.agents.analysis import (
    cellpose_tools,
    compute_approval,
    conversation,
    gpu_capacity,
    recovery,
    tiers,
)
from mica.agents.analysis.batch import job_spec
from mica.agents.analysis.config import Settings
from mica.agents.analysis.gpu_profiles import Sizing, profile
from mica.agents.analysis.recovery_tools import RecoveryManager


def settings():
    return Settings(
        _env_file=None,
        gpu_tier_enabled=True,
        gpu_image=GPU_IMAGE,
        gpu_flexible_sizing=True,
        gpu_types="t4,l4,a100,a100-80gb,h100",
    )


def request(ram=24, vram=12, cpus=4, count=1, **changes):
    return tiers.WorkerChoice(
        category="training",
        tier="gpu",
        reason="Assessed model and host memory",
        gpu_sizing=Sizing(
            host_memory_gib=ram,
            gpu_memory_gib=vram,
            vcpus=cpus,
            gpu_count=count,
            basis="measured",
            evidence="Prior peak usage and batch tensors with 20 percent headroom.",
            multi_gpu_reason="Independent batches run in one process per GPU." if count > 1 else "",
        ),
        **changes,
    )


def shapes(choice, config=None):
    return [tiers.resolve("gpu", config or settings(), name) for name in choice["gpu_candidates"]]


def test_assessment_is_mandatory_and_multiple_gpus_need_parallelism():
    with pytest.raises(ValueError, match="Assess GPU resources"):
        tiers.launch_choice(
            tiers.WorkerChoice(category="analysis", tier="gpu", reason="Run a GPU task"),
            settings(),
        )
    with pytest.raises(ValueError, match="parallelism"):
        Sizing(
            host_memory_gib=100,
            gpu_memory_gib=40,
            vcpus=8,
            gpu_count=2,
            basis="estimated",
            evidence="Model size with measured headroom",
        )


def test_small_workload_stays_small_with_all_families_enabled():
    choice = tiers.launch_choice(request(), settings())
    selected = shapes(choice)
    assert [shape.gpu_type for shape in selected] == ["t4", "l4"]
    assert selected[0].memory_gib == 24 and selected[0].gpu_count == 1
    assert all(shape.gpu_count == 1 for shape in selected)


def test_explicit_128_gib_requirement_survives_assessment_and_fallback():
    choice = tiers.launch_choice(request(min_memory_gib=128), settings())
    selected = shapes(choice)
    assert {shape.gpu_type for shape in selected} == {"t4", "l4"}
    assert all(shape.memory_gib >= 128 and shape.gpu_count == 1 for shape in selected)
    assert selected[0].machine == "n1-custom-20-131072"
    assert choice["constraints"]["min_memory_gib"] == 128


@pytest.mark.parametrize(
    "vram,families", [(40, {"a100", "a100-80gb"}), (80, {"a100-80gb", "h100"})]
)
def test_large_vram_uses_larger_families_and_never_pools_device_memory(vram, families):
    choice = tiers.launch_choice(request(vram=vram), settings())
    assert {shape.gpu_type for shape in shapes(choice)} == families
    assert all(shape.gpu_memory_gib >= vram for shape in shapes(choice))
    with pytest.raises(tiers.WorkerUnavailable, match="No worker was started"):
        tiers.launch_choice(request(vram=100, count=2), settings())


def test_h100_mode_is_explicit_and_never_upgrades_gpu_count_to_obtain_on_demand():
    choice = tiers.launch_choice(request(gpu_type="h100", allow_gpu_fallback=False), settings())
    assert choice["gpu_candidates"] == ["a3-highgpu-1g"]
    assert choice["provisioning"] == "SPOT"
    with pytest.raises(tiers.WorkerUnavailable, match="require Spot"):
        tiers.launch_choice(
            request(gpu_type="h100", allow_gpu_fallback=False, on_demand=True), settings()
        )


@pytest.mark.parametrize(
    "identifier", ["arbitrary-machine", "t4-1-96-624", "t4-2-8-300", "t4-8-48-200", "t4-1-3-10"]
)
def test_untrusted_or_invalid_profiles_cannot_become_vm_policies(identifier):
    with pytest.raises(ValueError):
        profile(identifier)


def test_gpu_job_binds_multiple_devices_and_keeps_isolation(gpu):  # noqa: F811
    gpu.settings = settings().model_copy(update={"data_buckets": gpu.settings.data_buckets})
    choice = tiers.launch_choice(request(ram=128, count=2), gpu.settings)
    job = job_spec(
        gpu.app,
        gpu.settings,
        "a" * 32,
        tier="gpu",
        duration=600,
        gpu_type=choice["gpu_profile"],
        provisioning=choice["provisioning"],
    )
    policy = job.allocation_policy.instances[0].policy
    assert policy.machine_type == "n1-custom-20-131072"
    assert policy.accelerators[0].count == 2
    task = job.task_groups[0].task_spec
    assert task.compute_resource.memory_mib == 128 * 1024
    assert "--cap-drop=ALL" in task.runnables[-1].container.options
    assert job.allocation_policy.network.network_interfaces[0].no_external_ip_address
    with pytest.raises(ValueError, match="requires Spot"):
        job_spec(
            gpu.app,
            gpu.settings,
            "a" * 32,
            tier="gpu",
            duration=600,
            gpu_type="a3-highgpu-1g",
            provisioning="STANDARD",
        )


def test_device_enumeration_only_exposes_real_nvidia_character_devices(monkeypatch):
    devices = {"/dev/nvidia0", "/dev/nvidia1", "/dev/nvidiactl", "/dev/nvidia-uvm"}
    monkeypatch.setattr(Path, "exists", lambda path: str(path) in devices | {"/dev/nvidia2"})
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda path: SimpleNamespace(
            st_mode=stat.S_IFCHR if str(path) in devices else stat.S_IFLNK
        ),
    )
    assert set(map(str, cellpose_tools.gpu_devices())) == devices


async def test_approved_80_gib_fallback_preserves_resources_and_binds_spot_mode(
    gpu,  # noqa: F811
    monkeypatch,
):
    gpu.settings.gpu_flexible_sizing = True
    gpu.settings.gpu_types = settings().gpu_types
    monkeypatch.setattr(
        conversation,
        "run",
        AsyncMock(
            return_value=conversation.Decision(
                route="compute",
                reply="Run the model with assessed GPU memory.",
                worker=request(vram=80),
            )
        ),
    )
    event_id, record = await start(gpu)
    card = gpu.test_slack.chat_update.call_args.kwargs["text"]
    assert "80 GiB VRAM each" in card and "Sizing assessment" in card
    assert "H100" in card and "Spot" in card
    # User must approve the exact hardware, fallback provisioning and assessment together.
    approval = compute_approval.load(gpu.store, event_id)[0]
    original = record["launch"]["gpu_candidate_modes"]["a3-highgpu-1g"]
    record["launch"]["gpu_candidate_modes"]["a3-highgpu-1g"] = "STANDARD"
    with pytest.raises(ValueError, match="choices changed"):
        compute_approval.validate(gpu, gpu.store, record, approval)
    record["launch"]["gpu_candidate_modes"]["a3-highgpu-1g"] = original
    await compute_approval.handle(gpu, gpu.store, click(event_id, record))
    await gpu.process(event_id, record["tick"])
    record = await gpu.repository.get_connector_state("claude-event:" + event_id)
    deadline = record["request_deadline"]
    job = batch_v1.Job(
        status=batch_v1.JobStatus(
            state="SCHEDULED", status_events=[batch_v1.StatusEvent(description="QUOTA_EXCEEDED")]
        )
    )
    gpu.test_client.get_job.side_effect = lambda **kw: job
    await gpu.process(event_id, record["tick"])
    assert gpu.test_client.create_job.call_count == 1
    gpu.test_client.get_job.side_effect = NotFound("deleted")
    record = await gpu.repository.get_connector_state("claude-event:" + event_id)
    assert await gpu.process(event_id, record["tick"]) == "pending"
    record = await gpu.repository.get_connector_state("claude-event:" + event_id)
    assert record["gpu_type"] == "h100" and record["gpu_profile"] == "a3-highgpu-1g"
    assert record["provisioning"] == "SPOT" and record["request_deadline"] == deadline
    compute_approval.validate(gpu, gpu.store, record, compute_approval.load(gpu.store, event_id)[0])
    manifest = recovery.worker_manifest(record, gpu.settings)["recovery"]
    manager = object.__new__(RecoveryManager)
    manager.tier, manager.manifest = "gpu", manifest
    assert "234 GiB RAM" in manager.prompt() and "80 GiB VRAM per device" in manager.prompt()
    assert gpu_capacity.claim_start(gpu.store, event_id, 5, "start")
    assert not gpu_capacity.claim_start(gpu.store, event_id, 5, "fallback")
