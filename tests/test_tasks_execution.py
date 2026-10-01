from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import task_service
from google.cloud import storage as google_storage

from pistachio.execution.backends import LocalExecutor
from pistachio.models import (
    AgentTaskPlan,
    ApprovalDecision,
    ApprovalKind,
    ApprovalRequest,
    ApprovalStatus,
    AttachmentRef,
    DatasetInspection,
    ExecutionBundle,
    HyperparameterSetting,
    HyperparameterSource,
    Project,
    ResourcePlan,
    TaskContract,
    TaskEnvelope,
    TaskRecord,
    TaskStatus,
    ToolInvocation,
    ToolPlan,
)
from pistachio.services.tasks import TaskService
from pistachio.services.uploads import attachment_tag


class SequencedHarness:
    def __init__(self) -> None:
        self.calls = 0

    async def plan(self, *, task_id, envelope, context):
        self.calls += 1
        if self.calls == 1:
            return AgentTaskPlan(
                contract=TaskContract(
                    objective="Differential expression",
                    missing_scientific_decisions=["Choose a statistical test."],
                    requires_execution=True,
                ),
                resource_plan=ResourcePlan(),
                tool_plan=ToolPlan(),
                response="A test must be chosen.",
            )
        script = '''"""Write the approved deterministic test result."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("--input-dir", type=Path, required=True)
parser.add_argument("--output-dir", type=Path, required=True)
parser.add_argument("--seed", type=int, default=0)
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)
(args.output_dir / "approved.json").write_text(json.dumps({"method": "wilcoxon"}))
'''
        return AgentTaskPlan(
            contract=TaskContract(
                objective="Differential expression with Wilcoxon",
                requested_outputs=["approved.json"],
                authorized_scientific_actions=["Use the Wilcoxon test."],
                requires_execution=True,
            ),
            resource_plan=ResourcePlan(),
            tool_plan=ToolPlan(),
            script=script,
            response="Ready.",
        )

    async def compact_session(self, conversation_id: str) -> None:
        return None


async def test_scientific_approval_replans_and_records_reproducible_bundle(
    repository, settings
) -> None:
    project = await repository.create_project(
        Project(slug="science", name="Science", owner="alice@example.org")
    )
    harness = SequencedHarness()
    service = task_service(repository, settings, harness)
    task, _ = await service.create(
        TaskEnvelope(
            project_id=project.id,
            conversation_id="conv-science",
            principal_id="alice@example.org",
            prompt="Run differential expression.",
        )
    )
    task = await service.process(task.id)
    assert task.status == TaskStatus.NEEDS_APPROVAL
    approval = next(item for item in task.approvals if item.kind == ApprovalKind.SCIENTIFIC_STEP)
    with pytest.raises(ValueError, match="explicit decision"):
        await service.decide_approval(
            task.id,
            approval.id,
            "alice@example.org",
            ApprovalDecision(approved=True),
        )
    task = await service.decide_approval(
        task.id,
        approval.id,
        "alice@example.org",
        ApprovalDecision(approved=True, response="Use the Wilcoxon test."),
    )
    assert task.status == TaskStatus.SUCCEEDED
    assert harness.calls == 2
    assert task.result and task.result.artifacts[0].name == "approved.json"
    attempt = settings.artifact_root / task.id / "attempt-1"
    assert (attempt / "task.py").exists()
    assert (attempt / "environment.lock").exists()
    assert (attempt / "context_manifest.json").exists()
    manifest = json.loads((attempt / "context_manifest.json").read_text())
    assert {item["kind"] for item in manifest} >= {"current_request", "task_contract"}


async def test_scientific_approval_can_acknowledge_before_replanning(repository, settings) -> None:
    project = await repository.create_project(
        Project(slug="deferred", name="Deferred", owner="alice@example.org")
    )
    harness = SequencedHarness()
    service = task_service(repository, settings, harness)
    task, _ = await service.create(
        TaskEnvelope(
            project_id=project.id,
            conversation_id="conv-deferred",
            principal_id="alice@example.org",
            prompt="Run differential expression.",
        )
    )
    task = await service.process(task.id)
    approval = next(item for item in task.approvals if item.kind == ApprovalKind.SCIENTIFIC_STEP)

    acknowledged = await service.decide_approval(
        task.id,
        approval.id,
        "alice@example.org",
        ApprovalDecision(approved=True, response="Use the Wilcoxon test."),
        defer_replan=True,
    )

    assert acknowledged.status == TaskStatus.RECEIVED
    assert harness.calls == 1
    saved_approval = next(item for item in acknowledged.approvals if item.id == approval.id)
    assert saved_approval.payload["decision"] == "Use the Wilcoxon test."


def test_custom_other_value_replaces_proposed_setting() -> None:
    normalized = TaskService._normalize_structured_decision(
        {
            "fields": [
                {
                    "id": "cluster_range",
                    "label": "Candidate cluster counts",
                    "proposed_value": "k=2 through k=15",
                }
            ]
        },
        '{"values":{"cluster_range":"k=3 through k=20"}}',
    )

    assert normalized == "Candidate cluster counts: k=3 through k=20"


async def test_event_delivery_is_idempotent(repository, settings) -> None:
    project = await repository.create_project(
        Project(slug="dedupe", name="Dedupe", owner="alice@example.org")
    )
    harness = SequencedHarness()
    service = task_service(repository, settings, harness)
    envelope = TaskEnvelope(
        project_id=project.id,
        external_event_id="same-event",
        conversation_id="conv",
        principal_id="alice@example.org",
        prompt="Run differential expression.",
    )
    first, created = await service.create(envelope)
    second, created_again = await service.create(envelope)
    assert created
    assert not created_again
    assert first.id == second.id


async def test_unmanaged_file_attachment_is_rejected(repository, settings) -> None:
    project = await repository.create_project(
        Project(slug="inputs", name="Inputs", owner="alice@example.org")
    )
    service = task_service(repository, settings, SequencedHarness())
    with pytest.raises(ValueError, match="managed upload"):
        await service.create(
            TaskEnvelope(
                project_id=project.id,
                conversation_id="conv",
                principal_id="alice@example.org",
                prompt="Read this file.",
                attachments=[
                    AttachmentRef(
                        filename="passwd",
                        uri="file:///etc/passwd",
                        checksum_sha256="a" * 64,
                        quarantined=False,
                        authorization_tag=attachment_tag(
                            settings,
                            "alice@example.org",
                            "file:///etc/passwd",
                            "a" * 64,
                            "",
                        ),
                    )
                ],
            )
        )


class HyperparameterHarness:
    def __init__(self) -> None:
        self.calls = 0

    async def plan(self, *, task_id, envelope, context):
        self.calls += 1
        confirmed = "latent_dim=8" in envelope.prompt
        parameter = HyperparameterSetting(
            name="latent_dim",
            value=8 if confirmed else 16,
            default_value=16,
            source=(
                HyperparameterSource.USER_SPECIFIED
                if confirmed
                else HyperparameterSource.TOOL_DEFAULT
            ),
            tunable=True,
            scientific_impact="Controls representation capacity.",
            requires_user_confirmation=True,
            confirmed_by_user=confirmed,
            user_question="Choose Decipher latent_dim (default 16).",
        )
        invocation = ToolInvocation(
            tool_id="tool-azizilab-decipher",
            tool_name="Decipher",
            version="0.1.3",
            entrypoint="decipher train",
            hyperparameters=[parameter],
            review_complete=True,
            review_evidence=["https://github.com/azizilab/decipher/tree/9610b31"],
        )
        script = ""
        if confirmed:
            script = '''"""Record the explicitly approved Decipher configuration."""
from __future__ import annotations
import argparse
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("--input-dir", type=Path, required=True)
parser.add_argument("--output-dir", type=Path, required=True)
parser.add_argument("--seed", type=int, default=0)
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)
'''
        return AgentTaskPlan(
            contract=TaskContract(objective="Run Decipher", requires_execution=True),
            resource_plan=ResourcePlan(),
            tool_plan=ToolPlan(
                approved_tool_ids=["tool-azizilab-decipher"], invocations=[invocation]
            ),
            script=script,
            response="Review Decipher parameters." if not confirmed else "Ready.",
        )

    async def compact_session(self, conversation_id: str) -> None:
        return None


async def test_hyperparameter_approval_replans_and_reports_effective_values(
    repository, settings
) -> None:
    project = await repository.create_project(
        Project(slug="parameters", name="Parameters", owner="alice@example.org")
    )
    harness = HyperparameterHarness()
    service = task_service(repository, settings, harness)
    task, _ = await service.create(
        TaskEnvelope(
            project_id=project.id,
            conversation_id="conv-parameters",
            principal_id="alice@example.org",
            prompt="Run Decipher.",
        )
    )
    task = await service.process(task.id)
    assert task.status == TaskStatus.NEEDS_APPROVAL
    approval = next(item for item in task.approvals if item.kind == ApprovalKind.HYPERPARAMETER)
    with pytest.raises(ValueError, match="hyperparameter approval"):
        await service.decide_approval(
            task.id,
            approval.id,
            "alice@example.org",
            ApprovalDecision(approved=True),
        )
    task = await service.decide_approval(
        task.id,
        approval.id,
        "alice@example.org",
        ApprovalDecision(approved=True, response="Use latent_dim=8."),
    )
    assert task.status == TaskStatus.SUCCEEDED
    assert harness.calls == 2
    assert task.result
    assert task.result.tool_usage[0].hyperparameters[0].value == 8
    manifest = settings.artifact_root / task.id / "attempt-1" / "metadata"
    payload = json.loads((manifest / "effective_hyperparameters.json").read_text())
    assert payload[0]["hyperparameters"][0]["value"] == 8


class RepairingToolReviewHarness:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    async def plan(self, *, task_id, envelope, context):
        self.calls += 1
        self.prompts.append(envelope.prompt)
        invocation = []
        if self.calls == 2:
            invocation = [
                ToolInvocation(
                    tool_id="tool-runtime-numpy",
                    tool_name="NumPy",
                    version="2.1.3",
                    entrypoint="numpy.asarray",
                    review_complete=True,
                    review_evidence=[
                        "https://numpy.org/doc/2.1/reference/generated/numpy.asarray.html"
                    ],
                    no_tunable_hyperparameters_reason=(
                        "Array conversion exposes no scientific tuning parameter in this script."
                    ),
                )
            ]
        script = '''"""Record completion after a complete tool review."""
from __future__ import annotations
import argparse
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("--input-dir", type=Path, required=True)
parser.add_argument("--output-dir", type=Path, required=True)
parser.add_argument("--seed", type=int, default=0)
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)
'''
        return AgentTaskPlan(
            contract=TaskContract(objective="Use NumPy", requires_execution=True),
            resource_plan=ResourcePlan(),
            tool_plan=ToolPlan(approved_tool_ids=["tool-runtime-numpy"], invocations=invocation),
            script=script,
            response="Ready.",
        )

    async def compact_session(self, conversation_id: str) -> None:
        return None


async def test_incomplete_tool_review_is_repaired_without_empty_user_approval(
    repository, settings
) -> None:
    project = await repository.create_project(
        Project(slug="repair", name="Repair", owner="alice@example.org")
    )
    harness = RepairingToolReviewHarness()
    service = task_service(repository, settings, harness)
    task, _ = await service.create(
        TaskEnvelope(
            project_id=project.id,
            conversation_id="conv-repair",
            principal_id="alice@example.org",
            prompt="Use NumPy to compute the requested result.",
        )
    )

    task = await service.process(task.id)

    assert harness.calls == 2
    assert "PLANNING VALIDATION ERROR" in harness.prompts[1]
    assert task.status == TaskStatus.SUCCEEDED
    assert all(item.kind != ApprovalKind.HYPERPARAMETER for item in task.approvals)


def test_identical_approved_compute_plan_is_carried_through_replanning() -> None:
    approved = ApprovalRequest(
        task_id="task-1",
        kind=ApprovalKind.COMPUTE,
        title="Compute plan needs approval",
        detail="runtime exceeds six hours",
        payload={"profile": "memory-large", "timeout_hours": 12.0},
        status=ApprovalStatus.APPROVED,
        decided_by="alice@example.org",
    )
    regenerated = ApprovalRequest(
        task_id="task-1",
        kind=ApprovalKind.COMPUTE,
        title="Compute plan needs approval",
        detail="runtime exceeds six hours",
        payload={"profile": "memory-large", "timeout_hours": 12.0},
    )

    carried = TaskService._carry_forward_compute_approvals([approved], [regenerated])

    assert carried == [approved]
    assert carried[0].status == ApprovalStatus.APPROVED


async def test_local_executor_provides_writable_home_and_plotting_cache_dirs(settings) -> None:
    script = '''"""Verify isolated scientific runtime directories."""
import argparse
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--input-dir", type=Path, required=True)
parser.add_argument("--output-dir", type=Path, required=True)
parser.add_argument("--seed", type=int, default=0)
args = parser.parse_args()

paths = {
    "home": str(Path.home()),
    "config": os.environ["XDG_CONFIG_HOME"],
    "cache": os.environ["XDG_CACHE_HOME"],
    "matplotlib": os.environ["MPLCONFIGDIR"],
}
for raw_path in paths.values():
    path = Path(raw_path)
    assert path.is_dir()
    (path / "write-check").write_text("ok", encoding="utf-8")
assert os.environ["MPLBACKEND"] == "Agg"
args.output_dir.mkdir(parents=True, exist_ok=True)
(args.output_dir / "runtime-paths.json").write_text(json.dumps(paths), encoding="utf-8")
'''
    bundle = ExecutionBundle(
        task_id="task_runtime_paths",
        script=script,
        contract=TaskContract(objective="Verify runtime paths", requires_execution=True),
        resource_plan=ResourcePlan(),
        tool_plan=ToolPlan(),
    )

    result = await LocalExecutor(settings).execute(bundle)

    assert result.exit_code == 0
    paths = json.loads(
        (
            settings.artifact_root
            / bundle.task_id
            / "attempt-1"
            / "outputs"
            / "runtime-paths.json"
        ).read_text()
    )
    assert paths["home"].endswith("/runtime/home")
    assert paths["matplotlib"].endswith("/runtime/matplotlib")


async def test_local_executor_stages_generation_pinned_gcs_dataset(
    settings, monkeypatch
) -> None:
    content = b"immutable-h5ad-placeholder"

    class FakeBlob:
        generation = 42
        size = len(content)
        md5_hash = "verified-md5"
        crc32c = None

        def reload(self, *, if_generation_match: int) -> None:
            assert if_generation_match == 42

        def download_to_filename(self, target: Path, *, if_generation_match: int) -> None:
            assert if_generation_match == 42
            Path(target).write_bytes(content)

    class FakeBucket:
        def blob(self, name: str, *, generation: int) -> FakeBlob:
            assert name == "datasets/cells.h5ad"
            assert generation == 42
            return FakeBlob()

    class FakeStorageClient:
        def bucket(self, name: str) -> FakeBucket:
            assert name == "research-data"
            return FakeBucket()

    monkeypatch.setattr(google_storage, "Client", FakeStorageClient)
    script = '''"""Verify a generation-pinned dataset was staged."""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--input-dir", type=Path, required=True)
parser.add_argument("--output-dir", type=Path, required=True)
parser.add_argument("--seed", type=int, default=0)
args = parser.parse_args()
files = sorted(args.input_dir.glob("*.h5ad"))
assert len(files) == 1
args.output_dir.mkdir(parents=True, exist_ok=True)
(args.output_dir / "input-name.txt").write_text(files[0].name, encoding="utf-8")
'''
    bundle = ExecutionBundle(
        task_id="task_gcs_dataset",
        script=script,
        contract=TaskContract(
            objective="Read the supplied dataset",
            immutable_inputs=["gs://research-data/datasets/cells.h5ad"],
            requires_execution=True,
        ),
        resource_plan=ResourcePlan(),
        tool_plan=ToolPlan(),
        dataset_inputs=[
            DatasetInspection(
                uri="gs://research-data/datasets/cells.h5ad",
                generation="42",
                size_bytes=len(content),
                checksum="verified-md5",
            )
        ],
    )

    result = await LocalExecutor(settings).execute(bundle)

    assert result.exit_code == 0
    assert result.artifacts[0].name == "input-name.txt"


def test_required_h5ad_input_is_blocked_before_execution_when_unavailable() -> None:
    task = TaskRecord(
        envelope=TaskEnvelope(
            project_id="project-1",
            conversation_id="conversation-1",
            principal_id="alice@example.org",
            prompt="Analyze the dataset.",
        ),
        contract=TaskContract(objective="Analyze the dataset", requires_execution=True),
        generated_script='files = args.input_dir.glob("*.h5ad")',
    )

    assert TaskService._missing_execution_inputs(task) == [
        "an attached or generation-pinned .h5ad dataset"
    ]

    task.dataset_inspections = [
        DatasetInspection(
            uri="gs://research-data/datasets/cells.h5ad",
            generation="42",
            size_bytes=10,
            checksum="verified-md5",
        )
    ]
    assert TaskService._missing_execution_inputs(task) == []
