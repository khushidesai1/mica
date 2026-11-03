from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, Response
from google.api_core.exceptions import Forbidden, NotFound, ServiceUnavailable
from google.cloud import batch_v1
from test_analysis_batch import bridge, payload  # noqa: F401

from mica.agents.analysis import (
    compute_approval,
    conversation,
    gpu_capacity,
    gpu_runtime,
    recovery,
    tiers,
)
from mica.agents.analysis.batch import GPU_BOOT_IMAGE, job_spec
from mica.agents.analysis.config import Settings
from mica.agents.analysis.recovery_tools import RecoveryManager
from mica.api import slack_analysis

GPU_IMAGE = "us-docker.pkg.dev/project/images/gpu@sha256:" + "b" * 64


def test_gpu_worker_prompt_does_not_promise_cpu_resize():
    manager = object.__new__(RecoveryManager)
    manager.tier = "gpu"
    manager.manifest = {"provisioning": "STANDARD"}
    prompt = manager.prompt()
    assert "cannot resize or move to a CPU tier" in prompt
    assert "suitable larger worker" not in prompt


@pytest.fixture
def gpu(bridge, monkeypatch):  # noqa: F811
    bridge.settings.gpu_tier_enabled = True
    bridge.settings.gpu_image = GPU_IMAGE
    bridge.settings.gpu_default_seconds = 7200
    bridge.settings.gpu_max_seconds = 7200
    monkeypatch.setattr(compute_approval, "AsyncWebClient", lambda **kw: bridge.test_slack)
    monkeypatch.setattr(
        conversation,
        "run",
        AsyncMock(
            return_value=conversation.Decision(
                route="compute",
                reply="Segment this crop with Cellpose-SAM.",
                worker=tiers.WorkerChoice(
                    category="analysis",
                    tier="gpu",
                    gpu_type="l4",
                    reason="Cellpose-SAM segmentation",
                ),
            )
        ),
    )
    return bridge


def test_gpu_is_outside_cpu_resize_ladder_and_requires_compatible_image():
    assert "gpu" not in tiers.ORDER
    for settings in [Settings(_env_file=None), Settings(_env_file=None, gpu_tier_enabled=True)]:
        with pytest.raises(ValueError, match="verified GPU image"):
            tiers.resolve("gpu", settings)
    settings = Settings(_env_file=None, gpu_tier_enabled=True, gpu_image=GPU_IMAGE)
    assert tiers.resolve("gpu", settings).machine == "g2-standard-8"
    with pytest.raises(ValueError, match="CPU-only"):
        tiers.launch_choice(
            tiers.WorkerChoice(
                category="analysis", tier="gpu", max_tier="large", reason="Explicit GPU request"
            ),
            settings,
        )


def test_gpu_job_uses_gpu_image_drivers_and_bounded_duration(gpu):
    job = job_spec(
        gpu.app, gpu.settings, "a" * 32, tier="gpu", duration=600, provisioning="STANDARD"
    )
    instance = job.allocation_policy.instances[0]
    assert instance.policy.machine_type == "g2-standard-8" and instance.install_gpu_drivers
    assert instance.policy.boot_disk.image == GPU_BOOT_IMAGE
    task = job.task_groups[0].task_spec
    assert task.runnables[0].script.text == gpu_runtime.SETUP
    assert task.runnables[0].timeout.total_seconds() == 120
    worker = task.runnables[-1]
    assert worker.container.image_uri == GPU_IMAGE
    assert "--gpus" not in worker.container.options
    assert "--privileged" not in worker.container.options
    for required in (
        "--runtime=nvidia",
        "--user=65532:65532",
        "--cap-drop=ALL",
        "no-new-privileges",
    ):
        assert required in worker.container.options
    assert worker.environment.variables["AGENT_GPU_WORKER"] == "true"
    cpu = job_spec(gpu.app, gpu.settings, "a" * 32)
    assert len(cpu.task_groups[0].task_spec.runnables) == 1
    assert "--runtime=nvidia" not in cpu.task_groups[0].task_spec.runnables[0].container.options
    assert task.max_run_duration.total_seconds() == 600
    with pytest.raises(ValueError, match="duration"):
        job_spec(gpu.app, gpu.settings, "a" * 32, tier="gpu", duration=7201)


def test_gpu_recovery_never_resizes_or_extends_original_deadline():
    settings = Settings(_env_file=None, gpu_tier_enabled=True, gpu_image=GPU_IMAGE)
    choice = tiers.launch_choice(
        tiers.WorkerChoice(
            category="analysis", tier="gpu", on_demand=False, reason="Explicit Spot GPU"
        ),
        settings,
    )
    record = {"launch": choice, "gpu_approved_at": 100}
    recovery.initialize(record, settings, now=120)
    assert (
        record["request_deadline"] == 100 + settings.gpu_default_seconds
        and recovery.remaining_recoveries(record, settings) == 1
    )
    record["preemption_count"] = 8
    assert recovery.remaining_recoveries(record, settings) == 0
    with pytest.raises(ValueError, match="do not resize"):
        recovery.destination(
            record,
            recovery.RecoverCall(
                attempt=0,
                cause="capacity",
                memory_gib=100,
                evidence="Measured memory demand",
                handoff="retry",
            ),
            settings,
        )


async def start(gpu):
    event_id = await gpu.accept(payload("3.0", text="Use Cellpose-SAM on this crop"))
    assert await gpu.process(event_id, 0) == "awaiting_approval"
    record = await gpu.repository.get_connector_state("claude-event:" + event_id)
    return event_id, record


def click(event_id, record, action="approve"):
    return dict(
        type="block_actions",
        team={"id": "T1"},
        user={"id": "U1"},
        channel={"id": "D1"},
        message={"ts": record["notice_ts"]},
        actions=[{"action_id": compute_approval.PREFIX + action, "value": event_id}],
    )


async def test_gpu_requires_requester_confirmation_and_duplicate_cannot_restart(gpu):
    event_id, record = await start(gpu)
    gpu.test_client.create_job.assert_not_called()
    assert "slot" not in record
    assert "NVIDIA L4" in gpu.test_slack.chat_update.call_args.kwargs["text"]
    buttons = gpu.test_slack.chat_update.call_args.kwargs["blocks"][-1]["elements"]
    assert buttons[0]["style"] == "primary" and "style" not in buttons[1]
    await compute_approval.handle(gpu, gpu.store, click(event_id, record))
    assert await gpu.process(event_id, record["tick"]) == "running"
    await compute_approval.handle(gpu, gpu.store, click(event_id, record))
    assert gpu.test_client.create_job.call_count == 1


@pytest.mark.parametrize("field", ["user", "team", "channel", "message"])
async def test_gpu_confirmation_is_bound_to_original_requester(gpu, field):
    event_id, record = await start(gpu)
    action = click(event_id, record)
    action[field] = {"ts" if field == "message" else "id": "other"}
    with pytest.raises(ValueError, match="Only the requester"):
        await compute_approval.handle(gpu, gpu.store, action)
    gpu.test_client.create_job.assert_not_called()


@pytest.mark.parametrize(
    "change", ["image", "duration", "access", "cancel", "expire", "hardware", "disabled", "price"]
)
async def test_changed_expired_or_declined_gpu_request_never_submits(gpu, monkeypatch, change):
    event_id, record = await start(gpu)
    if change == "cancel":
        await compute_approval.handle(gpu, gpu.store, click(event_id, record, "cancel"))
    elif change == "image":
        gpu.settings.gpu_image = GPU_IMAGE.replace("b" * 64, "c" * 64)
    elif change == "duration":
        gpu.settings.gpu_max_seconds = 3600
    elif change == "access":
        gpu.app.slack_user_map["T1:U1"] = "other@example.com"
    elif change == "hardware":
        record["launch"]["gpu_type"] = "t4"
        await gpu.repository.set_connector_state("claude-event:" + event_id, record)
    elif change == "disabled":
        gpu.settings.gpu_types = "t4"
    elif change == "price":
        gpu.settings.gpu_hourly_estimate_usd = 3.0
    else:
        approval = compute_approval.load(gpu.store, event_id)[0]
        monkeypatch.setattr(compute_approval.time, "time", lambda: approval.expires_at + 1)
    assert await gpu.process(event_id, record["tick"]) == "failed"
    gpu.test_client.create_job.assert_not_called()


@pytest.mark.parametrize(
    "gpu_type,machine,memory,accelerator",
    [
        ("t4", "n1-standard-8", 30, "nvidia-tesla-t4"),
        ("l4", "g2-standard-8", 32, ""),
    ],
)
async def test_gpu_hardware_choice_reaches_approval_and_submission(
    gpu, monkeypatch, gpu_type, machine, memory, accelerator
):
    monkeypatch.setattr(
        conversation,
        "run",
        AsyncMock(
            return_value=conversation.Decision(
                route="compute",
                reply="Segment this crop.",
                worker=tiers.WorkerChoice(
                    category="analysis",
                    tier="gpu",
                    gpu_type=gpu_type,
                    allow_gpu_fallback=False,
                    reason="Requested GPU model",
                ),
            )
        ),
    )
    event_id, record = await start(gpu)
    assert record["launch"]["gpu_type"] == gpu_type
    text = gpu.test_slack.chat_update.call_args.kwargs["text"]
    assert "NVIDIA " + gpu_type.upper() in text
    assert f"{memory} GiB host RAM" in text
    assert ("$3.00" if gpu_type == "t4" else "$4.00") in text
    await compute_approval.handle(gpu, gpu.store, click(event_id, record))
    # Changing the default after approval must not change the selected hardware.
    gpu.settings.gpu_default_type = "l4" if gpu_type == "t4" else "t4"
    assert await gpu.process(event_id, record["tick"]) == "running"
    job = gpu.test_client.create_job.call_args.kwargs["job"]
    policy = job.allocation_policy.instances[0].policy
    assert policy.machine_type == machine
    assert [(a.type_, a.count) for a in policy.accelerators] == (
        [(accelerator, 1)] if accelerator else []
    )
    assert job.task_groups[0].task_spec.compute_resource.memory_mib == memory * 1024
    saved = await gpu.repository.get_connector_state("claude-event:" + event_id)
    assert recovery.worker_manifest(saved, gpu.settings)["recovery"]["gpu_type"] == gpu_type


def test_gpu_preference_can_use_available_model_but_strict_choice_is_preserved():
    settings = Settings(_env_file=None, gpu_tier_enabled=True, gpu_image=GPU_IMAGE, gpu_types="t4")
    assert tiers.available_gpus(settings) == ["t4"]
    with pytest.raises(ValueError, match="l4 is unavailable"):
        tiers.launch_choice(
            tiers.WorkerChoice(
                category="analysis",
                tier="gpu",
                gpu_type="l4",
                allow_gpu_fallback=False,
                reason="Use L4 only",
            ),
            settings,
        )
    with pytest.raises(ValueError, match="requires the GPU"):
        tiers.launch_choice(
            tiers.WorkerChoice(
                category="analysis", tier="standard", gpu_type="t4", reason="Invalid CPU request"
            ),
            settings,
        )
    choice = tiers.launch_choice(
        tiers.WorkerChoice(category="analysis", tier="gpu", reason="Unspecified GPU model"),
        settings,
    )
    assert choice["gpu_type"] == "t4"
    choice = tiers.launch_choice(
        tiers.WorkerChoice(category="analysis", tier="gpu", gpu_type="l4", reason="Prefer L4"),
        settings,
    )
    assert choice["gpu_type"] == "t4" and choice["requested_gpu_type"] == "l4"


def test_gpu_memory_requirement_excludes_smaller_fallback():
    settings = Settings(_env_file=None, gpu_tier_enabled=True, gpu_image=GPU_IMAGE)
    choice = tiers.launch_choice(
        tiers.WorkerChoice(
            category="analysis",
            tier="gpu",
            gpu_type="l4",
            min_gpu_memory_gib=20,
            reason="Measured twenty GiB requirement",
        ),
        settings,
    )
    assert choice["gpu_candidates"] == ["l4"]


@pytest.mark.parametrize("host,vram", [(128, 0), (0, 48), (128, 48)])
def test_gpu_rejects_unsupported_host_or_device_memory(host, vram):
    settings = Settings(_env_file=None, gpu_tier_enabled=True, gpu_image=GPU_IMAGE)
    with pytest.raises(tiers.WorkerUnavailable) as error:
        tiers.launch_choice(
            tiers.WorkerChoice(
                category="training", tier="gpu", gpu_type="l4",
                min_memory_gib=host, min_gpu_memory_gib=vram,
                reason="Explicit requested memory",
            ),
            settings,
        )
    assert "L4: 32 GiB host RAM, 24 GiB GPU VRAM" in str(error.value)
    assert "T4: 30 GiB host RAM, 16 GiB GPU VRAM" in str(error.value)
    assert "No worker was started" in str(error.value)


def test_host_memory_requirement_also_excludes_smaller_gpu_fallback():
    settings = Settings(_env_file=None, gpu_tier_enabled=True, gpu_image=GPU_IMAGE)
    choice = tiers.launch_choice(
        tiers.WorkerChoice(
            category="training", tier="gpu", gpu_type="t4", min_memory_gib=32,
            reason="At least 32 GiB host RAM required",
        ),
        settings,
    )
    assert choice["gpu_type"] == "l4" and choice["gpu_candidates"] == ["l4"]
    assert choice["constraints"]["min_memory_gib"] == 32


def test_approval_and_start_receipts_use_existing_control_only_prefix():
    assert compute_approval.key("a" * 32) == "write-access/compute-approvals/" + "a" * 32 + ".json"


async def approved(gpu):
    event_id, record = await start(gpu)
    assert "automatically try 1 NVIDIA T4" in gpu.test_slack.chat_update.call_args.kwargs["text"]
    await compute_approval.handle(gpu, gpu.store, click(event_id, record))
    await gpu.process(event_id, record["tick"])
    return event_id, await gpu.repository.get_connector_state("claude-event:" + event_id)


@pytest.mark.parametrize("trigger", ["capacity", "timeout"])
async def test_l4_falls_back_once_after_deletion_preserving_approval_and_limits(gpu, trigger):
    event_id, record = await approved(gpu)
    original_job, deadline = record["job"], record["request_deadline"]
    approval = compute_approval.load(gpu.store, event_id)[0]
    if trigger == "timeout":
        record["submitted"] -= gpu.settings.gpu_queue_wait_seconds + 1
    job = batch_v1.Job(
        status=batch_v1.JobStatus(
            state="SCHEDULED",
            status_events=[
                batch_v1.StatusEvent(
                    description="ZONE_RESOURCE_POOL_EXHAUSTED"
                    if trigger == "capacity"
                    else "Scheduling"
                )
            ],
        )
    )

    gpu.test_client.get_job.side_effect = lambda **kw: job
    await gpu.repository.set_connector_state("claude-event:" + event_id, record)
    assert await gpu.process(event_id, record["tick"]) == "running"
    gpu.test_client.delete_job.assert_called_once_with(name=original_job)
    assert gpu.test_client.create_job.call_count == 1  # No overlap with the old job.
    assert not gpu_capacity.claim_start(gpu.store, event_id, 0, "start")
    # Deletion in progress cannot create another job, including across duplicate polls.
    job.status.state = "DELETION_IN_PROGRESS"
    record = await gpu.repository.get_connector_state("claude-event:" + event_id)
    await gpu.process(event_id, record["tick"])
    assert gpu.test_client.create_job.call_count == 1
    gpu.test_client.get_job.side_effect = NotFound("deleted")
    record = await gpu.repository.get_connector_state("claude-event:" + event_id)
    assert await gpu.process(event_id, record["tick"]) == "pending"
    record = await gpu.repository.get_connector_state("claude-event:" + event_id)
    assert record["gpu_type"] == "t4" and record["attempt"] == 1
    assert record["request_deadline"] == deadline and record["model_spent"] == 0
    assert compute_approval.load(gpu.store, event_id)[0] == approval
    gpu.test_client.get_job.side_effect = lambda **kw: batch_v1.Job(
        status=batch_v1.JobStatus(state="RUNNING")
    )
    assert await gpu.process(event_id, record["tick"]) == "running"
    created = gpu.test_client.create_job.call_args.kwargs
    assert created["job_id"].endswith("-r1")
    assert created["job"].allocation_policy.instances[0].policy.machine_type == "n1-standard-8"
    assert created["job"].task_groups[0].task_spec.max_run_duration.total_seconds() <= 7200
    saved = await gpu.repository.get_connector_state("claude-event:" + event_id)
    assert recovery.worker_manifest(saved, gpu.settings)["recovery"]["gpu_type"] == "t4"
    assert not await gpu_capacity.fallback(gpu, event_id, saved, gpu.test_client, job, "SCHEDULED")


async def test_gpu_approval_with_deployed_control_plane_storage_scope(gpu, monkeypatch):
    put = gpu.store.put_if

    def scoped_put(key, data, generation):
        if not key.startswith(("tasks/", "projects/", "write-access/")):
            raise Forbidden("Control-plane object prefix is outside its IAM condition")
        return put(key, data, generation)

    monkeypatch.setattr(gpu.store, "put_if", scoped_put)
    event_id, record = await start(gpu)
    await compute_approval.handle(gpu, gpu.store, click(event_id, record))
    assert compute_approval.load(gpu.store, event_id)[0].status == "approved"


@pytest.mark.parametrize("first", ["start", "fallback"])
async def test_gpu_credential_endpoint_and_fallback_are_mutually_exclusive(gpu, monkeypatch, first):
    event_id, record = await approved(gpu)
    monkeypatch.setattr(
        slack_analysis, "worker_context", AsyncMock(return_value=(gpu, gpu.store, record))
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(services=SimpleNamespace(settings=gpu.app)))
    )
    assert gpu_capacity.claim_start(gpu.store, event_id, 0, first)
    if first == "fallback":
        with pytest.raises(HTTPException) as error:
            await slack_analysis.credentials(event_id, request, Response())
        assert error.value.status_code == 403
    else:
        gpu.app.anthropic_api_key = Mock(get_secret_value=lambda: "fake-key")
        result = await slack_analysis.credentials(event_id, request, Response())
        assert result["ANTHROPIC_API_KEY"] == "fake-key"
        assert not gpu_capacity.claim_start(gpu.store, event_id, 0, "fallback")


@pytest.mark.parametrize(
    "block", ["started", "cancelled", "expired", "strict", "legacy", "unknown"]
)
async def test_gpu_fallback_never_replays_started_or_unapproved_work(gpu, block):
    event_id, record = await approved(gpu)
    if block == "started":
        assert gpu_capacity.claim_start(gpu.store, event_id, 0, "start")
    elif block == "cancelled":
        record["cancel_requested"] = True
    elif block == "expired":
        record["request_deadline"] = 0
    elif block == "strict":
        record["launch"]["gpu_candidates"] = ["l4"]
    elif block == "legacy":
        record["launch"].pop("gpu_candidates")
    job = batch_v1.Job(
        status=batch_v1.JobStatus(
            state="SCHEDULED",
            status_events=[
                batch_v1.StatusEvent(
                    description="permission denied"
                    if block == "unknown"
                    else "ZONE_RESOURCE_POOL_EXHAUSTED"
                )
            ],
        )
    )
    assert not await gpu_capacity.fallback(gpu, event_id, record, gpu.test_client, job, "SCHEDULED")
    gpu.test_client.delete_job.assert_not_called()
    assert record["attempt"] == 0


async def test_gpu_submission_capacity_rejection_uses_distinct_fallback_job(gpu):
    event_id, record = await start(gpu)
    await compute_approval.handle(gpu, gpu.store, click(event_id, record))
    create = gpu.test_client.create_job.side_effect
    gpu.test_client.create_job.side_effect = ServiceUnavailable("ZONE_RESOURCE_POOL_EXHAUSTED")
    assert await gpu.process(event_id, record["tick"]) == "pending"
    record = await gpu.repository.get_connector_state("claude-event:" + event_id)
    gpu.test_client.create_job.side_effect = create
    assert await gpu.process(event_id, record["tick"]) == "running"
    assert gpu.test_client.create_job.call_args.kwargs["job_id"].endswith("-r1")


@pytest.mark.parametrize("existing_job", [False, True])
async def test_gpu_access_revoked_during_preparation_never_submits(gpu, monkeypatch, existing_job):
    event_id, record = await start(gpu)
    await compute_approval.handle(gpu, gpu.store, click(event_id, record))
    if not existing_job:
        gpu.test_client.delete_job.side_effect = NotFound("No submitted job")
    ready = compute_approval.ready
    calls = 0

    async def revoke_before_submit(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            gpu.app.slack_user_map["T1:U1"] = "revoked@example.com"
        return await ready(*args)

    monkeypatch.setattr(compute_approval, "ready", revoke_before_submit)
    assert await gpu.process(event_id, record["tick"]) == ("running" if existing_job else "failed")
    gpu.test_client.delete_job.assert_called_once()
    saved = await gpu.repository.get_connector_state("claude-event:" + event_id)
    assert saved["cancel_requested"]
    gpu.test_client.create_job.assert_not_called()
