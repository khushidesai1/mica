# Code builder protocol 2: implementation and rollout

Date: 28 September 2026. Status: opt-in implementation; release gates remain open.

This implements the core lifecycle from [the design](code-builder-redesign.md). It is
available for CPU Python analyses and is disabled by default. A user-approved staging trial
is now deployed; see [the deployment record](protocol2-staging-deployment.md) for exact
images, runtime corrections, passed cloud checks, and the staging-only security exception.
The design describes the target system; this document distinguishes implemented behavior
from work and validation still needed before default rollout.

## Behavior

After scientific, tool, figure, and compute decisions are resolved, an executable task
enters a persisted development session. The session first probes its real inputs and
runtime, then accepts one typed developer action per continuation:

1. Inspect runtime/input metadata or retained observations.
2. Edit individual Python/Markdown files against an exact source revision.
3. Execute regression tests and a bounded end-to-end smoke analysis.
4. Inspect private artifacts and revise the implementation.
5. Submit the candidate for a separate model review and fresh verification operation.
6. Run the verified source on the full inputs.
7. Validate required artifacts, retain a reproduction archive, commit publication, and
   review actual result evidence before exposing success.

Technical failures return diagnostics to development within the remaining budget.
Scientific changes yield `NEEDS_INPUT` and use the existing amendment path. Infrastructure
faults retry the same immutable operation with a distinct Batch job retry suffix.
Progress notifications continue during execution; a zero exit status alone cannot deliver
results. Partial and failed artifacts stay in private audit storage.

The action protocol is a typed `DevelopmentAction` response executed by the orchestrator.
It does not require a long-lived SDK tool session or a persistent VM. Source files are still
carried in structured edit requests, with a 32-file/240 KB revision limit. This bounds the
first implementation; it is not an unrestricted repository coding environment.

## Implementation map

| Area | Responsibility |
| --- | --- |
| `models.py`, `config.py` | Versioned sessions, actions, operations, failures, verification, artifact contracts, budgets and rollout flags. |
| `agents/development.py`, `agents/harness.py` | Developer, candidate-review and result-review prompts/calls; bounded image input; model/token provenance. |
| `services/development.py` | Durable continuations, immutable revisions, authority fingerprints, budget reservation, review and completion gates. |
| `storage/{base,memory,firestore}.py` | Compare-and-swap task updates; event leases prevent concurrent continuations. |
| `execution/workspace.py` | Task-scoped immutable content-addressed source/evidence storage, safe paths and atomic local publication. |
| `execution/development.py`, `checks.py` | Shared probe/test/verify/run contract, sandbox launch, effective seeds, logs, output validation and evidence retention. |
| `execution/checkpoints.py` | Declared JSON checkpoints, dependency keys and atomic writes. |
| `execution/reproduction.py` | Exact source, trusted launchers, configuration, environment inventory, checksums and standalone replay launcher. |
| `worker/main.py`, `execution/publication.py` | Recoverable publication snapshot, idempotent uploads, commit verification and completion delivery. |
| `services/tasks.py`, `services/dispatch.py`, `api/routes.py`, `execution/backends.py` | Existing planner, approval, queue, cancellation and completion integration. |

## Execution and evidence contracts

Generated code implements:

```text
analysis.py --mode smoke|run --input-dir /inputs --output-dir /outputs
            --config /config/effective_parameters.json --seed INTEGER
```

Source, inputs, configuration, trusted harnesses and cached checkpoint inputs are mounted
read-only. Scratch and current outputs are writable. The common bootstrap seeds Python and
NumPy and records the effective seed. Per-procedure distinct seeds remain in configuration;
the developer must pass them to the corresponding scientific APIs. A read-only config and
code review do not prove that arbitrary code used every parameter correctly.

`run_check` runs discovered `unittest` tests and real-input smoke; zero discovered tests
fails. Candidate acceptance independently reviews the source, freezes output/checkpoint
contracts, and repeats checks in a fresh workspace. A full operation verifies the exact
source, input checksums, configuration and image against that clean verification report.

The trusted validator requires exactly the declared output paths, nonzero file sizes, no
symlinks, and format-specific validation. Supported formats are JSON, CSV, TSV, PNG, PDF,
SVG, text, H5AD, HDF5, NPY, NPZ and Parquet. Optional readers must exist in the execution
image. JSON keys/table columns can be required. These checks detect structural problems;
scientific invariants belong in task tests and review. Model-authored tests are not an
independent scientific oracle.

The model can inspect retained text and PNG previews. Result review receives up to 6,000
characters per supported text artifact and PNGs up to 4 MB each/8 MB total. Large artifacts
and other formats receive metadata-only evidence with that limitation stated. Automated
PDF/SVG validation is implemented, but rendering them for model visual QA is not. The
worker verification report describes automated checks; the later model review is retained
separately in session audit records.

Docker uses no network, a read-only root, unprivileged UID, dropped capabilities, process,
CPU and memory limits, and controlled mounts. The Batch worker stages and publishes with
credentials outside generated execution, then uses bubblewrap namespaces, an environment
allowlist and resource limits. Namespace failures fail closed. The unisolated runner is
available only with `env=test`; it is not evidence of sandbox conformance. GPU protocol 2
execution is rejected pending a supported isolation/replay contract.

Output/scratch sizes are monitored and logs are capped. Disk monitoring is reactive and
is not a hard per-workspace filesystem quota. Staging time and peak resource telemetry are
not fully accounted for by the command runner. These limitations must be exercised and
resolved as required by the staging resource-conformance gate.

## Durability, repair and checkpoints

An operation is reserved and its immutable bundle retained before dispatch. Task version
checks reject stale interaction/completion updates. Source/action/review/result records
live in artifact storage rather than large Firestore documents. API restarts reconstruct
the next action from stored records; model calls interrupted before their result is saved
may repeat, with the prior reserved turn still counted.

A cloud worker writes a publication snapshot before uploading individual artifacts.
Redelivery can resume uploads from that snapshot without repeating analysis. A commit
manifest is written last; completion acceptance checks result/artifact checksums and
operation identity. There remains a crash window before a completed analysis has uploaded
its snapshot, in which the analysis can need recomputation. Exactly-once computation is
not claimed.

Checkpoint producers are explicit source files/modules. Keys include producer code and
statically discovered local imports, all effective parameters, input checksums, runtime
image and declared schema. Dynamic imports conservatively depend on all source files.
Only schema-valid complete JSON checkpoints are retained; failed downstream runs can
reuse them after repair. Verification starts without cached checkpoints. JSON is suitable
for the initial fixture but not a scalable large sparse-matrix cache. Scientific validity
of a declared producer boundary still depends on tests and review.

Amendments retain spent compute/model/revision counters and source, then conservatively
invalidate verification and contracts. The existing planner is invoked with prior
current-task values. Fine-grained plan-dependency invalidation and a guarantee that only
the changed question is asked remain follow-up work. The current active wall-clock timer
restarts after an amendment; cumulative operation/cost/turn caps continue to apply.

Follow-up replanning now finishes tool, resource, and figure planning and resolves pending
approvals before resuming a retained development workspace. The resume shortcut requires
the development session to belong to the current interaction revision; an old workspace
alone cannot bypass replanning. Both immediate and queued approval continuations use this
path. Stale progress messages are cleared when a follow-up starts a new revision.

For a follow-up, the master planner supplies `amendment_explanation`: the requested change,
the chosen approach, and the reason for any suggestion not adopted. The Conversation Agent
converts this into the saved task `followup_response`, visible alongside approvals and
progress in the web UI and channel messages. It is generated once per revised plan, with a
deterministic fallback if the model is unavailable. Required methods cannot silently be
treated as optional suggestions. Deployed to staging on 29 September 2026; see [the rollout record](followup-replan-deployment.md).

## Configuration and migration

Existing records default to `builder_protocol_version=1`, and each new task persists its
selection at intake. There is no destructive migration or automatic conversion of active
legacy work. `planner_version` is independent; protocol 2 requires planner 2's resolved
master plan and decisions.

For a conformance-approved local/staging environment:

```dotenv
PISTACHIO_PLANNER_VERSION=2
PISTACHIO_BUILDER_PROTOCOL_VERSION=2
PISTACHIO_DEVELOPMENT_ENABLED=true
PISTACHIO_DEVELOPMENT_MODEL=gpt-5.6-sol
PISTACHIO_EXECUTION_BACKEND=docker
PISTACHIO_WORKER_SANDBOX=docker
PISTACHIO_BATCH_IMAGE=REGISTRY/SCIENCE_IMAGE@sha256:ACTUAL_DIGEST
```

Use the actual approved image digest, not the placeholder. Local/test environments also
accept an immutable local `sha256:...` image ID. Cloud uses `execution_backend=gcp_batch`;
the worker selects bubblewrap. Runtime selection must agree with the submitted bundle.
An OpenAI key is required for real development; there is no fake offline analysis fallback.

Defaults: 40 model turns including reviews, 12 executable operations, 20 source revisions,
1,800 reserved development seconds, 7,200 wall seconds, 300 seconds per development
operation, and 100 MB monitored scratch/output budget. Full runs use the approved task
timeout and have a separate two-attempt cap. Compute approval estimates cover two full
runs, development checks and startup allowance. Reservations are conservative and are
not refunded for short operations. Actual token counts are recorded; a model-dollar price
ledger and empirical model comparison are not implemented.

To stop new adoption, set `BUILDER_PROTOCOL_VERSION=1` with the normal `PISTACHIO_` prefix.
Leave development enabled while draining existing protocol 2 tasks. Disabling development
also prevents their continuations; it does not translate them back to protocol 1.

## Validation performed

Initial implementation checks are recorded below. Subsequent deployment validation is
recorded in [the staging report](protocol2-staging-deployment.md); that report supersedes
the earlier absence of real Firestore, GCS, and bounded Batch execution checks.

Local checks on the initial implementation:

- `ruff check src tests`: passed.
- `mypy src/pistachio`: passed for 50 source files.
- `pytest -q`: 106 passed, 3 skipped. The skips are two opt-in Docker cases and the
  scientific module missing its optional packages in the host virtual environment.
- The separate pinned Linux acceptance image ran all three scientific tests during its
  build: hand-computed neighbor compositions, four-sample AnnData/Louvain assessment and
  repeatability across chunk sizes, and invalid alignment/category handling.
- An earlier real Docker boundary run passed. Subsequent boundary/replay runs timed out
  during container startup before Python emitted runtime evidence. Direct container start
  also stalled; Docker replay conformance is therefore **not currently passed**.

The deterministic suite exercises repair/resume, clean verification, seed/config binding,
zero-exit invalid outputs, timeout evidence, immutable output contracts, actual text in
result review, review rejection/amendment, stale updates, checkpoint reuse/invalidation,
and interrupted cloud publication using a simulated object store. The live-model workflow
is mocked in those orchestration tests.

The four-sample fixture uses 200 cells with sample sizes 41/47/53/59, a missing category,
partial chunks, per-sample 30-neighbor graphs and the 20-resolution Louvain assessment.
It reopens a real H5AD file, but its repeatability test runs in one Python process. It is
not a fresh full-task replay or a reconstruction of the user's failed production task.
A missing `pkg_resources` dependency was encountered while building the Linux fixture;
the test image pins setuptools to supply it. This is not evidence that the production
failure had that cause.

Reproduce the scientific checks separately:

```bash
docker build -f docker/Dockerfile.development-test -t pistachio-development-test:local .
PISTACHIO_CONFORMANCE_IMAGE="$(docker image inspect --format '{{.Id}}' pistachio-development-test:local)" \
  .venv/bin/pytest -q tests/test_development_docker.py
```

The image-build tests and Docker boundary/replay tests serve different purposes; passing
the former cannot substitute for passing the latter.

## Gates still open

1. Pass both real Docker boundary and fresh reproduction tests on a functioning runtime.
   Extend the scientific fixture to a full delivered multi-figure task, inspect stable
   neighborhood colors, and replay that task in a fresh process/image with numeric checks.
2. Exercise real Firestore transactions, Cloud Tasks redelivery, Batch cancellation,
   bubblewrap credential/network/resource isolation, partial GCS publication and recovery
   in staging. Current publication tests use a fake object store; no staging cloud run
   was performed.
3. Add trusted task-specific reference checks where available and adversarial tests for
   weakening generated tests, scientific drift and inadequate smoke coverage. Current
   mandatory format/runtime checks are protected, but test quality and semantic coverage
   still depend on model review. Add stronger runtime capability probes for optional
   scientific backends before expensive work.
4. Complete broader figure QA and scalable checkpoint formats, amendment refinement,
   environment build-recipe/lock provenance, image retention and access checks, and
   empirical resource measurements required by the design.
5. Run repeated live-model baseline-versus-protocol-2 trials at equal cumulative budgets.
   No local OpenAI key was available, so verified completion rate, false-success rate,
   cost and latency improvements have not been measured. Record denominators and uncertainty;
   do not infer model reliability from deterministic test counts.

Keep protocol 1 as the default until these gates establish the release criteria in the
design. The implementation enables iterative development and verifiable delivery, but
does not yet establish production readiness or diagnose the original Louvain failure.

## Technical autonomy and recovery correction — 29 September 2026

The developer selects installed support packages and compatible APIs autonomously. Routine
package/version selection, extra source edits, roundoff, and corrections to unjustified test
tolerances are technical repairs. A developer amendment must explicitly identify itself as
scientific and describe its effect on research meaning before it can yield NEEDS_INPUT.
Candidate/result reviewers also need an explicit scientific impact to request user input.
Scientific choices and meaningful correctness checks remain binding.

The bounded loop now allows 40 model turns and 20 edits. Existing operation, runtime, cost and
full-run caps remain. Successful consecutive fixed probes are reused. Observations identify
their source hash and whether they describe current code, preventing old errors from being
presented as results of an untested repair.

Planner-v2 package preparation starts automatically through the controlled build service; it
no longer creates TOOL_ADD user approvals. Tasks wait internally for that build, and missing
build infrastructure or failed builds are technical failures, not scientific questions. The
staging package build trigger is currently unconfigured, so arbitrary new packages cannot yet
be provisioned there; the installed runtime remains available to the coder. Legacy planner-v1
tool approvals are unchanged.

Cloud Tasks processing and reconciliation share a durable process lease. Reconciliation
uses the normal planner/resume path; incomplete planning raises a nonfatal development
conflict. Duplicate or obsolete completions cannot resume an amended session. Previously
approved identical compute resources survive a follow-up, and repeated dataset URLs are
inspected once.

Validation: 139 tests passed, 3 skipped in the shared workspace; lint and type checks passed.
The isolated deployment bundle also passed 61 targeted tests and lint, preserving the existing
UI separately from work completed in another chat. These checks do not establish successful
completion or live-model quality on the full 2,471,637-cell dataset.

## Standard method defaults

Requesting a method authorizes its standard package workflow and documented defaults for
unspecified arguments. Shared instructions and structured decision scopes keep routine defaults
and repairs inside the agent loop. Only substantive research-design changes may request input.
See [method-defaults-deployment.md](method-defaults-deployment.md) for the Louvain regression,
provenance handling, validation, and deployment record.
