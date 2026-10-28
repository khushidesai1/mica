import json
from unittest.mock import AsyncMock

import pytest
from test_analysis_batch import bridge  # noqa: F401
from test_gpu_compute import approved, click, gpu, start  # noqa: F401

from mica.agents.analysis import cellpose_tools, compute_approval, recovery, tiers
from mica.agents.analysis.checkpoints import Stages, cleanup_temporary
from mica.agents.analysis.config import Settings
from mica.agents.analysis.direct import WorkerChoice
from mica.agents.analysis.storage import LocalStorage


def test_long_gpu_allowance_and_cost_ceiling_use_worst_approved_hardware():
    settings = Settings(_env_file=None, gpu_tier_enabled=True, gpu_image="x@sha256:" + "a" * 64)
    choice = tiers.launch_choice(
        WorkerChoice(
            category="full_segmentation",
            tier="gpu",
            gpu_hours=72,
            compute_budget_usd=24,
            reason="Whole slide segmentation",
        ),
        settings,
    )
    assert choice["max_seconds"] == 12 * 3600  # Both GPUs allowed; use the $2/h estimate.
    legacy = {"launch": {"tier": "gpu"}, "gpu_approved_at": 100}
    recovery.initialize(legacy, settings, now=100)
    assert legacy["request_deadline"] == 7300


async def test_requester_can_change_total_allowance_before_approval(gpu):  # noqa: F811
    gpu.settings.gpu_max_seconds = 7 * 86400
    event, record = await start(gpu)
    action = click(event, record, "allowance")
    action["actions"][0] = {
        "action_id": compute_approval.PREFIX + "allowance",
        "selected_option": {"value": f"{event}:{3 * 86400}"},
    }
    await compute_approval.handle(gpu, gpu.store, action)
    gpu.test_client.create_job.assert_not_called()
    saved = await gpu.repository.get_connector_state("claude-event:" + event)
    receipt = compute_approval.load(gpu.store, event)[0]
    assert receipt.seconds == 3 * 86400 and receipt.choices == compute_approval.choices(saved)
    assert "72 hours" in gpu.test_slack.chat_update.call_args.kwargs["text"]
    await compute_approval.handle(gpu, gpu.store, click(event, saved))
    assert await gpu.process(event, saved["tick"]) == "running"
    created = gpu.test_client.create_job.call_args.kwargs["job"]
    duration = created.task_groups[0].task_spec.max_run_duration.total_seconds()
    assert 3 * 86400 - 60 <= duration <= 3 * 86400
    # An old selection delivered after confirmation cannot extend a running job.
    action["actions"][0]["selected_option"]["value"] = f"{event}:{7 * 86400}"
    await compute_approval.handle(gpu, gpu.store, action)
    assert compute_approval.load(gpu.store, event)[0].seconds == 3 * 86400


async def test_checkpointed_gpu_continuation_keeps_approval_deadline_and_model_spend(gpu):  # noqa: F811
    event, record = await approved(gpu)
    deadline = record["request_deadline"]
    prefix = recovery.event_prefix(event)
    archive = prefix + "/handoff-a0-" + "a" * 32 + ".tar.gz"
    gpu.store.put(archive, b"\x1f\x8btest")
    result = {
        "event_id": event,
        "attempt": 0,
        "archive": archive,
        "sha256": "a" * 64,
        "state": {"context": {"workdir": "/work"}},
        "model_cost": 0.2,
        "stages": {"stages": [{"path": "checkpoints/tile"}]},
        "outcome": "handoff",
    }
    gpu.store.put(prefix + "/partial-a0.json", json.dumps(result).encode())
    body = recovery.RecoverCall(
        attempt=0,
        cause="continue",
        evidence="Completed tile is saved",
        handoff="Continue remaining tiles",
    )
    assert (
        recovery.operate(event, record, body, gpu.settings, gpu.store, now=deadline - 100)["status"]
        == "checkpoint"
    )
    await gpu.repository.set_connector_state("claude-event:" + event, record)
    gpu.store.put(prefix + "/attempt-a0.json", json.dumps(result).encode())
    gpu.test_states[record["job"]] = "SUCCEEDED"
    await gpu.process(event, record["tick"])
    pending = await gpu.repository.get_connector_state("claude-event:" + event)
    assert pending["status"] == "pending" and pending["continuation_count"] == 1
    assert pending["request_deadline"] == deadline and pending["model_spent"] == 0.2
    assert gpu.test_client.create_job.call_count == 1
    await gpu.process(event, pending["tick"])
    current = await gpu.repository.get_connector_state("claude-event:" + event)
    assert current["status"] == "running" and current["job"].endswith("-r1")
    assert current["request_deadline"] == deadline
    assert current["model_reserved"] + current["model_spent"] == current["model_limit"]


def test_durable_masks_survive_terminal_cleanup_and_new_request(tmp_path):
    storage = LocalStorage(tmp_path / "cloud")
    work = tmp_path / "first"
    output = work / "outputs/sam/tile01"
    output.mkdir(parents=True)
    (output / "masks.tif").write_bytes(b"completed-mask")
    (output / "provenance.json").write_text('{"version":"4.2.1.1"}')
    stages = Stages(storage, work, "a" * 32, 0, thread_key="thread", project_id="project")
    saved = stages.capture_result(
        "outputs/sam/tile01", "input-hash/model-v1/channels0,1/tile-extents"
    )
    cleanup_temporary(storage, "a" * 32)
    restored = Stages(
        storage, tmp_path / "next", "b" * 32, 0, thread_key="thread", project_id="project"
    )
    assert restored.available()["checkpoints"][0]["checkpoint_id"] == saved["checkpoint_id"]
    restored.restore_saved(saved["checkpoint_id"])
    assert (restored.work / "outputs/sam/tile01/masks.tif").read_bytes() == b"completed-mask"
    with pytest.raises(ValueError, match="destination exists"):
        restored.restore_saved(saved["checkpoint_id"])
    for thread, project in [("other", "project"), ("thread", "other")]:
        foreign = Stages(
            storage, tmp_path / "foreign", "c" * 32, 0, thread_key=thread, project_id=project
        )
        assert foreign.available()["checkpoints"] == []
        with pytest.raises(FileNotFoundError):
            foreign.restore_saved(saved["checkpoint_id"])


async def test_cellpose_saves_before_success_and_never_repeats_inference_on_save_failure(
    tmp_path, monkeypatch
):
    result = {"version": "4.2.1.1", "model": "cpsam", "output_dir": "outputs/tile"}
    segment = AsyncMock(return_value=result)
    monkeypatch.setattr(cellpose_tools, "segment", segment)
    save = AsyncMock(return_value={"checkpoint_id": "a" * 32, "durable": True})
    tool = cellpose_tools.tools(tmp_path, tmp_path, Settings(_env_file=None), save)[1]
    args = {
        "input_path": "tile.npy",
        "output_dir": "outputs/tile",
        "version": "4.2.1.1",
        "model": "cpsam",
    }
    reply = await tool.handler(args)
    assert json.loads(reply["content"][0]["text"])["checkpoint"]["durable"]
    save.assert_awaited_once()
    save.side_effect = OSError("upload unavailable")
    reply = await tool.handler(args)
    assert reply["isError"] and "retry checkpoint_stage" in reply["content"][0]["text"]
    assert segment.await_count == 2  # One inference per invocation; no automatic replay.


def test_many_tiles_keep_handoffs_bounded_without_losing_saved_results(tmp_path):
    storage = LocalStorage(tmp_path / "cloud")
    work = tmp_path / "work"
    stages = Stages(storage, work, "a" * 32, 0, thread_key="thread", project_id="project")
    for index in range(70):
        path = work / f"checkpoints/tile{index}"
        path.mkdir(parents=True)
        (path / "mask.tif").write_bytes(b"mask")
        stages.capture({"path": str(path.relative_to(work)), "fingerprint": "x" * 1000})
    assert len(json.dumps(stages.manifest())) < 64 * 1024
    assert stages.manifest()["older_stages_available"]
    first = stages.available()
    second = stages.available(first["next_offset"])
    assert len(first["checkpoints"]) + len(second["checkpoints"]) == 70
