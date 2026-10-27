from unittest.mock import AsyncMock

import pytest
from google.api_core.exceptions import NotFound
from test_analysis_batch import bridge, payload  # noqa: F401

from mica.agents.analysis import compute_approval, direct, recovery, tiers
from mica.agents.analysis.batch import GPU_BOOT_IMAGE, job_spec
from mica.agents.analysis.config import Settings
from mica.agents.analysis.recovery_tools import RecoveryManager

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
    monkeypatch.setattr(compute_approval, "AsyncWebClient", lambda **kw: bridge.test_slack)
    monkeypatch.setattr(
        direct,
        "decide",
        AsyncMock(
            return_value=direct.Decision(
                route="compute",
                reply="Segment this crop with Cellpose-SAM.",
                worker=direct.WorkerChoice(
                    category="analysis", tier="gpu", reason="Cellpose-SAM segmentation"
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
            direct.WorkerChoice(
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
    assert task.runnables[0].container.image_uri == GPU_IMAGE
    assert "--gpus" not in task.runnables[0].container.options
    assert "--privileged" not in task.runnables[0].container.options
    assert task.runnables[0].environment.variables["AGENT_GPU_WORKER"] == "true"
    assert task.max_run_duration.total_seconds() == 600
    with pytest.raises(ValueError, match="duration"):
        job_spec(gpu.app, gpu.settings, "a" * 32, tier="gpu", duration=7201)


def test_gpu_recovery_never_resizes_or_extends_original_deadline():
    settings = Settings(_env_file=None, gpu_tier_enabled=True, gpu_image=GPU_IMAGE)
    choice = tiers.launch_choice(
        direct.WorkerChoice(
            category="analysis", tier="gpu", on_demand=False, reason="Explicit Spot GPU"
        ),
        settings,
    )
    record = {"launch": choice, "gpu_approved_at": 100}
    recovery.initialize(record, settings, now=120)
    assert (
        record["request_deadline"] == 7300 and recovery.remaining_recoveries(record, settings) == 1
    )
    record["preemption_count"] = 1
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


@pytest.mark.parametrize("change", ["image", "duration", "access", "cancel", "expire"])
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
    else:
        approval = compute_approval.load(gpu.store, event_id)[0]
        monkeypatch.setattr(compute_approval.time, "time", lambda: approval.expires_at + 1)
    assert await gpu.process(event_id, record["tick"]) == "failed"
    gpu.test_client.create_job.assert_not_called()


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
