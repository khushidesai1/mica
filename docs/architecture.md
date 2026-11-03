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

The shared task service is the durable boundary for the existing Slack backend, Gmail, and API
clients. A user's project/channel binding creates a delivery route, allowing API-created tasks
and historical web tasks to continue in Slack. Per-task delivery correlation records the exact
Slack conversation and thread so updates and approvals stay attached to the same task.

The Claude Slack backend has its own conversation and isolated computation loop, described in
[Slack setup](slack-agent-setup.md). Both backends use the headless HTTP control plane. There is
no browser application; removing its pages and login does not remove the task service, agent
loops, memory, connectors, execution backends, or worker callbacks.

Delivery is best-effort and never changes analysis state. Task state remains authoritative, so a
missed notification can be recovered through Slack or the authenticated task API.
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

`MICA_PLANNER_VERSION=2` enables the staged pipeline for newly created tasks. Existing tasks
retain the planner version stored on their record and continue through the legacy workflow.

The local executor exists only for tests/development. The Docker executor applies a read-only root,
no network, dropped capabilities, and an unprivileged UID.

### Shared Slack and email analysis surfaces

Labeled Gmail requests enter `BatchBridge` after OAuth mailbox validation and independent DKIM
verification of the raw message. `Authentication-Results` and `Reply-To` never establish identity.
Email receipts use mailbox plus message ID; private conversation keys add the verified sender and
Gmail thread ID. Checkpoints carry their private owner. Registry defaults are shared across email
and Slack DMs, while existing threads remain pinned to their original project.

The router, admission, worker, project registry and memory backend are shared. Email's trusted
surface marker denies project-data writes and transfers at admission, worker and broker boundaries.
Slack retains its existing delivery adapter; email suppresses worker progress and passes one
semantic terminal result to the control plane. That callback validates the active attempt,
current membership and artifact prefix/content hash before producing one MIME message through a
CAS-protected outbox. Mailbox secrets never reach the science worker or model process.

### Project reference repositories

AziziLab repositories in the GitHub App catalog are available read-only across all projects,
along with explicitly approved public references such as `khushidesai1/nympheas-temp`,
without another project-owner registration. The router sees the shared catalog and selects up
to five repositories needed by a compute request. App-backed content fetches check current
installation access with a scoped token; approved public references recheck public visibility.

Project owners and co-owners can also set default references through a private Slack confirmation
(`repos add azizilab/repo [project]` or “add azizilab/repo as a reference for this project”).
`repos [project]` lists shared repositories and project defaults;
`repos remove azizilab/repo [project]` removes a default, not the shared catalog entry.
Both exact and natural-language changes require the same owner authorization and confirmation;
email can read selections but changes happen in private Slack. Adding a repository exposes its
selected content to authorized project members and published channel audiences; it never changes
project membership or grants GitHub write permissions.

Before submitting compute, the control plane resolves each selected repository's default branch
to an immutable commit and reads eligible blobs with a short-lived GitHub App installation token
restricted to that repository and `contents:read`. Approved public references instead use anonymous
reads and commit-pinned raw files verified against Git blob hashes. The installation selection
and exact public allowlist are the administrator-controlled ceiling. The API never uses the
project-memory repository token for this access, and no GitHub key or token enters a worker
manifest, workspace or prompt.
Workers receive read-only `inputs/repos/<repo>/` snapshots with `.pin` commit provenance and
`.snapshot.json` listing included paths and omissions. Repository text remains untrusted input.

Snapshots are partial by design: at most five repositories per request, 100 files and 2 MiB per
repository, 256 KiB per file. Only source, configuration, documentation and UTF-8 notebooks qualify;
binary files, links, submodules, hidden paths, vendored directories, secret-like content and
oversized files are skipped. Notebook outputs and embedded attachments are removed. A truncated
GitHub tree fails closed. Fetch failures are recorded in the failures channel with project context,
and the request continues with an explicit unavailable-reference notice to the worker.

Snapshots are fixed for an event's retries, and every new turn replaces restored reference inputs,
including clearing removed or unavailable repositories. Removing a reference advances the project's
access revision, preventing stale workers from delivering previously authorized reference content.
This cannot recall content already sent to a user. Reference inputs never become project-memory
write proposals; adaptations belong in `code/`.

Slack amendments (`AGENT_AMENDMENTS`, default off) optionally deliver same-requester text through
the trusted worker's existing post-tool hook. One bounded, generation-checked addenda record
serializes consumption against fallback; the original follow-up queue slot remains pending until
the running turn ends, so later turns cannot overtake it. Unconsumed text stays a follow-up,
attachments and commands keep the normal flow, and email is unchanged. Only new workers launched
with support enabled offer buttons; an existing worker is never assumed to support the feature.
Consumption records handoff to the SDK hook, not confirmed execution or inclusion in the final
answer. If that worker fails or restarts after handoff, the amendment is marked failed with a
check-and-resend notice rather than automatically repeating possible side effects. A process crash
between its durable handoff record and SDK receipt remains an acknowledgment ambiguity.

### Conversation tools and worker guidance

Every Slack and email request uses the same conversation API and official Anthropic SDK.
Adaptive thinking uses `AGENT_CONVERSATION_EFFORT=medium`. A bounded loop can call
`list_folders`, `view_files` and `read_sources` in any order, or propose a command, transfer or
compute worker. Each tool rechecks access; terminal decisions are persisted before existing
idempotent handlers and approval controls run. Signed thinking stays in the private provider
transcript. Stored step logs contain tool names, argument sizes and status, never source text.
Exact command grammar and handlers share the policy table in `agents/analysis/commands.py`.
Natural-language cancellation is checked before queueing only when work is already active.
Pending requests from the retired router restart in the conversation loop without reusing
legacy decisions. Already running workers retain their saved launch and recovery state.
An active predecessor triggers one control check for cancellation, write revocation and
amendment eligibility; queued commands retain their normal authorization and approval flow.
Direct answers and worker answers use the same Markdown-to-Slack conversion, preserving code
and formatting headings, bold text and links. Saved conversation text and email stay unchanged.
Slack approvals use the green primary action style. Progress keeps one compact expandable
working/thinking status with native task rows, public summaries and long-run check-ins. Completed
tasks retain their completion state; interrupted tasks and terminal failures keep an error
indicator. Service-side failed/cancelled messages and the compatibility fallback use a red flag.

Agent rules live where they are used: code enforces access, sandbox and write constraints; tool
descriptions explain delivery and operations; a `UserPromptSubmit` hook provides current memory,
write grants, compute policy and the toolkit; trusted bundled skills explain spatial analysis,
Cellpose and project knowledge. The system prompt retains voice, workspace layout, scientific
standards and the project guide. Prompt budgets are checked in `test_conversation_tools.py` and
`test_worker_restructure.py`. Plugin skills load from `/opt/pistachio/plugin` with
`setting_sources=[]`, so agent-written workspace settings cannot grant permissions.

`send_files` explicitly selects attachments, including unchanged files for resending. New/changed
outputs remain a delivery safety net. Named files are deduplicated and carried in checkpoint
`pending_outputs`; unsafe, empty, oversized and unsupported attachments return per-file errors.
Memory writers supply missing entry headers from the path and application attribution before
validation, preserving existing valid headers and superseded status.
