# Architecture

Operator-configured [ambient tasks](ambient-tasks.md) use the existing reconciliation schedule
to discover newly completed samples and submit trusted presets into ordinary private worker
threads. Create-only discovery receipts, event-bound output grants and fixed run deadlines keep
starts idempotent and publishing scoped. Tasks default off; project recipes determine the
scientific method and the bundled segmentation-report skill provides reusable QC outputs.

Lab admission is configured in `projects/_access/lab.json`, read live by the API and workers.
Its `members` list admits verified Slack humans in channel mode even outside enabled channels;
workspace and legacy mapped admission retain their existing behavior. Lab membership does not
share any projects. The `all_projects` subset can use every current and future project through
the registry's normal authorization checks, without becoming owners or changing project members.
Those grants are never serialized into the project registry. Channel publication, owner-only
actions, and compute/write/transfer confirmations still apply. Future pre-approval features use
this same lab list instead of introducing another allowlist.

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

The Claude backend's Slack disclosure uses bounded public narration before tool calls as its
collapsed title, falling back to specific startup/tool status and then the generic activity state.
Worker narration is confirmed within one API message; thinking blocks are ignored. Conversation
requests enable public progress updates while reasoning stays omitted, and fallback responses
use only text notes. Notes render as plain text, use the existing throttle/heartbeat, and are not
added to application run logs, history or memory. Terminal cards clear them; email remains final-only.

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

[Project backchannel](project-backchannel.md) adds project-scoped scientific updates, questions,
answers and recipe references through the trusted conversation/worker tool boundary. A bounded
GCS board preserves concurrent publications; workers check at safe tool boundaries. The broker
revalidates project access and active attempt. Peer messages remain advisory and separate from
human amendments. Verified transfer completions contribute minimal sample-readiness facts.

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

Worker sizing uses assessed resource needs and expected duration. CPU jobs estimated at two
hours or more use on-demand capacity unless the requester chose Spot; the operator can change
that threshold. Old workload categories remain readable but no longer select resources.
GPU shapes, approval, placement and hardware guarantees retain the same catalog and checks.
On an approved GPU worker, `run_gpu` executes a workspace Python script using a named environment
from the shared toolkit registry. The sandbox exposes allocated GPU devices, clears credentials,
blocks networking and keeps project data and `inputs/` read-only. Successful output folders save
as durable checkpoints. Device observations are reported explicitly; method provenance must still
confirm actual GPU use. The conversation catalog and worker guidance share the same environment
list, including validated saved runtimes. Cellpose is a bundled skill and versioned environment;
its CPU path uses Bash and its GPU path uses the general tool.

Approval kinds register in `agents/analysis/approvals.py`: compute, administrative commands,
overwrite, project access and amendments. One signed Slack dispatcher resolves existing action
prefixes and modal callbacks. Shared helpers own conditional object writes, repository leases,
expiry and card updates; kind callbacks retain their specific identity, project, cost, scope and
idempotency checks. Approval objects keep their existing keys and generations. Repository-backed
access decisions keep their original lease, and registry/addendum mutations retain their own CAS.
New confirmations schedule a durable expiry check on the existing authenticated task queue.
Expired cards preserve saved work and offer Request again; renewal never grants access or starts
computation without another explicit decision. Run renewal cannot overtake a newer thread request.
Amendment choices expire after one hour and keep the message as a follow-up; they can be reopened
while the predecessor still runs. Failure notification cards remain separate.

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

`send_files` explicitly selects files or folders, including unchanged outputs for resending.
Folders become complete ZIPs with paths preserved. Opted-in code delivery snapshots changes under
`code/` after execution tools and adds one ZIP of the last captured bytes to the same output path;
it does not infer successful execution from shell syntax. Changed files remain provenance inputs,
not implicit attachments. Only selected files produce delivery or retention notices. Named files
are deduplicated and carried in checkpoint `pending_attachments`, separately from internal
`pending_outputs`; unsafe, empty, oversized and unsupported selections return per-file errors.
Memory writers supply missing entry headers from the path and application attribution before
validation, preserving existing valid headers and superseded status.


Transfer controllers use the generic `transfer` reply handoff and scheduled-task namespace.
Email accepts generic logical stages while retaining the legacy durable outbox identity so
older controllers can see new send intents during rollout or rollback. The compatibility
helper reads either saved namespace. An ambiguous receipt or conflicting old/new pair is held
for reconciliation, never resent.
Provider commands, connection receipts and failure codes keep their existing identifiers.
Core modules obtain trusted skill IDs from the toolkit registry rather than naming individual
methods. A source-boundary test keeps provider and incident-specific method names out of the
conversation, compute, delivery and failure-assessment mechanisms.

### Configured cloud-access approvals

The `cloud_access` approval kind handles a separate, optional role-grant workflow. Configured
channels bypass analysis admission and use a bound request form; email can propose a configured
bundle for a verified sender or an explicitly mentioned target. A live lab-member check can
preapprove a target. Other requests require the configured Slack approver or an exact verified
`approve`/`deny` reply in the dedicated approval email thread. This IAM-only exception leaves
project write and administrative email restrictions intact.

The control plane impersonates a dedicated access identity with a Terraform role ceiling.
An add-only IAM client preserves version-3 policy conditions and uses etag retries. Durable
request leases, target reservations, expiry/resumption timers and the existing Gmail outbox
separate approval, granting and notification. Bundles are snapshotted; settings changes cannot
broaden a pending request. Ambiguous emails are held, not resent. See
[cloud access](cloud-access.md) for the permission boundary and operator rollout.
