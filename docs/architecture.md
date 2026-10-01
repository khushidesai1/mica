# Architecture

Cloud Run is the authenticated control plane and scales to zero. Firestore holds projects, tasks,
session items, memories, deduplication records, channel bindings, and the immutable tool registry.
Cloud Tasks makes task intake asynchronous and limits dispatch concurrency to three. Google Cloud
Batch creates bounded workers; GCS holds inputs and output bundles.

Planner v2 uses `gpt-5.6-sol` once to produce an immutable, versioned `MasterPlan`. Deterministic
registry and resource services resolve normal cases. Bounded low-reasoning workers plan figure
presentation and generate code, with `gpt-5.6-sol` as the repair fallback. In production, the API
never runs generated code.

## State and data flow

1. A channel adapter authenticates an identity, resolves one explicit project, and creates an
   idempotent `TaskEnvelope`. Ingress is recorded for provenance, but it does not own execution or
   delivery.
2. The context builder enforces ACLs and renders policy, request, contract, two exchanges, summary,
   at most eight retrieved memories, immutable input metadata, and approved tool versions.
3. The master planner returns a `MasterPlan`, including procedure-level `ParameterSpec` records.
   Only `SCIENTIFIC` and `VISUAL_SEMANTIC` values block for approval. The resulting `DecisionSet`
   is bound to the exact master-plan ID and revision.
4. Each downstream `TaskStage` records input/output hashes, dependencies, attempts, model settings,
   latency, token/cache usage, and status. Approval resumes the first dependent stage and cannot
   increment the completed master-planner stage.
5. Approved code is stored with its contract, context manifest, resources, tools, environment,
   logs, and checksums. Cloud submission first reserves a `SUBMITTING` attempt. Its deterministic
   Batch job ID includes the attempt and bundle hash; only a confirmed job becomes `QUEUED`.
   Reconciliation safely resubmits a reserved or legacy orphaned attempt without incrementing it.
   Submission stops with a visible failure after three unsuccessful control-plane calls. A Batch
   worker runs the immutable bundle and publishes a completion event.
6. The control plane records the result pointer, not an unattributed scientific fact. Results show
   approved scientific settings, semantic figure settings, and automatic presentation settings;
   the full `parameter_manifest.json` remains in the reproducibility bundle.

## Channel-independent agent

The task service is the durable agent boundary. Slack, Gmail, and the browser are adapters around
the same project, task, approval, and memory records; no adapter runs its own agent loop. A user's
project/channel binding also creates a delivery route. Lifecycle updates can therefore be sent to
Slack even when the task was submitted in the browser, while the browser project feed shows work
submitted through Slack. Per-task delivery correlation records the exact Slack conversation and
thread so subsequent updates and approvals stay attached to the same task.

Delivery is best-effort and never changes analysis state. Task state remains authoritative, so a
missed notification can be recovered by reopening the task in any authorized surface.
Progress delivery is keyed by the immutable progress-event ID, preventing stage transitions from
reposting the same milestone.

Batch completion delivery uses a leased event claim. A handler marks the event complete only after
the state transition and any repaired-attempt submission succeed. Concurrent delivery receives a
retryable response, and a failed handler releases its claim for redelivery.

## Parameter consent

The master planner reviews primary-procedure parameters rather than enumerating every NumPy,
pandas, or Matplotlib argument. Defaults are not treated as scientifically appropriate merely
because they are defaults. Scientific settings and figure semantics produce one compact,
task-scoped approval. Cosmetic presentation and routine implementation settings never block, but
their effective values and sources are retained. Parameter sweeps and automatic tuning require
explicit authorization.

`PISTACHIO_PLANNER_VERSION=2` enables the staged pipeline for newly created tasks. Existing tasks
retain the planner version stored on their record and continue through the legacy workflow.

The local executor exists only for tests/development. The Docker executor applies a read-only root,
no network, dropped capabilities, and an unprivileged UID.
