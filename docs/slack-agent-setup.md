# Slack-first agent rollout inside Mica

The Claude agent runs inside the existing Mica app. Slack is the conversational interface;
the web application has been removed. The shared task service and all connection, memory, and
execution capabilities remain behind the headless HTTP service. Nothing is moved into `legacy/`.

The original plan describes a dedicated VM and Socket Mode. The user's subsequent direction
supersedes that deployment choice: retain the existing Slack bot, HTTP webhook, Cloud Run
service, Cloud Tasks queue, and GCP Batch Spot resources. No new Slack channel, Socket Mode
token, always-on VM, or Modal account is required.

## What runs where

`Slack DM → signed webhook → Cloud Tasks → Cloud Run conversation → Slack reply`

Requests that need code execution, calculations, scientific data processing or plots proceed to
a sized Batch worker. Information questions stay in the control service: listing names/folders/sizes,
inspecting authorized repository source, reading registered text attachments, and recalling earlier
thread records. CSV/TSV/JSON text can be inspected directly; computing new counts still needs a worker.
Incomplete listings report their coverage or ask for a narrower folder instead of starting a worker.
Conversation routing is always enabled. Ordinary questions, planning, brief reads,
`help`, `brief`, `cancel`, and bounded initial setup run in the control service.
The direct Claude call has no shell, filesystem, or network tools. It can answer, draft an initial guide,
request a project-scoped metadata listing, request bounded information reads, or request isolated
computation. Source inspection/README requests use the existing GitHub App to read commit-pinned
source snapshots, with exact-path follow-ups for larger repositories. The reply cites source paths
and the commit, distinguishes inspected code from executed/tested behavior, and reports missing
coverage. Requested README text is delivered directly through the ordinary chunked reply path;
Mica does not start a worker merely to compose documentation or claim an attachment was created.
All users use the conversation tool loop with the same access checks.
`AGENT_CONVERSATION_EFFORT` defaults to `medium`. With `AGENT_FLEXIBLE_HARNESS=true`, the
`AGENT_CONVERSATION_MAX_CALLS` default of 6 and 90-second delivery slice are checkpoint boundaries:
the same request continues with its evidence on another delivery. There is no total routing
deadline. The former 120-second, six-call and three-retry stops become nonblocking check-ins.
An unresolved routing request also gets a check-in after 24 hours. Keep working, Cancel task and
Report a problem are bound to the requester; silence and reports continue the task. Reports go
through the configured Mica failures channel. See [deployment](deployment.md) for rollout and
recovery details. Receipts accepted before enabling the policy keep their previous limits.
List, view and source-read tools can run in any order. Each source read remains bounded to
384 KiB and text attachments to 64 KiB; these limits protect individual reads. Active conversation
context is compacted while the full original evidence remains saved. Source inspection never
executes code.
All reads are restricted to authorized catalog/project repositories, registered attachments and
this thread's history. Access is checked before each tool and again before delivery. Invalid tool
input is returned to Claude for correction. Timeouts retry with saved context and backoff;
truncated tool proposals are repaired before execution. Refusals and permanent provider errors
receive an explanation. Failed routing never invents computation or bypasses approval. Terminal
actions persist before dispatch and retry without another model call.
Tool inputs can reuse the requester's earlier messages in the same project and thread. The
controller checks original event receipts rather than treating assistant replies or memory
paraphrases as user-supplied values. Destination validation runs inside the conversation loop
so a proposal can recover missing context and correct itself before dispatch. Access checks and
private transfer confirmation still apply. `brief` and `memory` display saved content; explicit
guide or memory edits use a worker and must verify the save before claiming completion.
Unselected principals retain the legacy fixed-stage router during this rollout. Cancellation
bypasses queued work and stops the active job without starting another. Only active predecessors
need a pre-queue natural-language control check.

A verified user who lacks membership in a project published to the current channel gets a message
naming its owner and the exact `share` command to run in the owner's DM with Mica. Unpublished
channels and private conversations keep a generic denial that does not disclose project details.

The new worker calls the Claude Agent SDK directly. It does not invoke Mica's shared task planners,
TaskContract generation, approval stages, or multi-stage execution pipeline. Each Slack thread
has one resumed Claude compute session, a workspace, and the current shared `knowledge/PROJECT.md` guide.
The brief stays unchanged by default. It is revised only when requested or when user-provided
or verified information fills a necessary gap in the project/file guide. Scientific observations,
hypotheses, results, conversation notes, and research TODOs belong in the hidden conversation cache. Routine
results, file listings, and plot adjustments do not trigger brief maintenance or save reminders.
Initial setup and ordinary guide corrections save automatically; `undo knowledge` reverts a save. The worker uses
built-in Read/Write/Edit/Glob/Grep/Bash tools and the pinned science environment.

A control-service reply acknowledges each accepted input. Direct replies update it in place;
computation requests update it before any Spot startup. That same
Slack message reports queue waits, startup elapsed time, worker readiness, and completion/errors.
Public commentary appears as a short paragraph above one compact native activity disclosure.
The paragraph explains a useful finding or what Mica is checking next; routine tool details stay
in the disclosure. Its title names the current task or queue/startup state, with “Mica is working”
or “Mica is thinking” as the fallback. Expand it to inspect recent tools and long-run check-ins. Cellpose
entries name the crop being processed; completion is only shown after confirmation. The disclosure
has no padded container, large heading, duplicate status line or separate “On it…” acknowledgment.
Slack owns the disclosure animation and hover styling; no custom border, color or rounded wrapper
is added. Worker notes are top-level text confirmed by a following tool call in the same API
message; thinking blocks are never rendered. The conversation API requests public progress
updates with reasoning omitted, and uses only text notes when a fallback model answers.
Notes are bounded plain text and stay out of application run logs, history and memory. The final
answer appears once, leads with the outcome and deliverables, and keeps important caveats visible.
Finished/stopped cards discard commentary. Email still sends no progress.
Phase changes come from SDK lifecycle events. Identical
states are deduplicated; worker updates are coalesced to at most once per 30 seconds in the same
message, with terminal states shown immediately. Unsupported
surfaces fall back to small context text. Status failures do not prevent work, and webhook/poll
deliveries reuse the stored message timestamp.

Write and GPU approvals return to this same compact activity presentation after a durable
decision, with the approved scope in the disclosure. Approval does not imply the worker has
started: remaining prerequisites and queue waits stay explicit. Cancelled or expired requests
use quiet status text without a running or success icon; request-again controls remain available.
Transfer results and failures remain readable outside the disclosure. Activity block rejection
falls back through the same transport for both approval resumption and ordinary progress.

Response patterns (illustrative wording; claims require confirmed results):

| Situation | Presentation |
| --- | --- |
| Lookup | “I found the sample table. I’m checking its columns.” above “Reading sources…” |
| Analysis | “The groups have different sample sizes. I’m checking the comparison.” above the current task |
| Write approval accepted | “Preparing approved changes…” with the authorized scope in details |
| Write finished | “Moved 3 files to `archive/`. Copies were verified before source removal.” |
| Interrupted | State what finished, what remains, and the supported next action; do not imply a retry has started |
| Partial delivery | Lead with the result, then identify the missing file or delivery failure in visible reply text |

Firestore receipts deduplicate Slack delivery, order accepted messages within a thread, and
limit active Claude jobs to three. Named polling tasks use the existing queue without depending
on its five-attempt retry limit. Separate threads can run concurrently. Registry writes use generation checks to serialize project binding changes. The worker saves SQLite state, Claude transcripts and the
workspace to the existing artifact bucket after a turn; the next Spot job restores the same
absolute paths. Direct chat updates the small checkpoint record with the revised brief and
recent conversation without downloading the workspace archive. The next compute worker overlays
that record on its restored workspace; compute replies update the same conversation record.
A small durable record preserves the current thread brief and turn number after the
workspace expires. Cold starts add Batch provisioning latency to computation requests.

A preempted turn is not automatically replayed: it may already have spent money or posted a
reply. The previous completed checkpoint remains available, and Slack receives a failure notice
when the controller observes termination. A new message retries. Delivery to Slack and saving
a checkpoint are separate operations, so a crash between them can leave a delivered reply with
an older session checkpoint. If Cloud Tasks exhausts retries during a prolonged service outage,
inspect the nonterminal `claude-event:*` receipt and re-enqueue its current tick; the job ID stays
stable. Older web-created Slack threads do not import the old planner's private session state;
a fresh Claude context starts from the authorized brief. Arbitrary Slack history is not imported.

## Existing staging resources verified

- Project: `azizilab-aml`; region: `us-central1`.
- Cloud Run: `pistachio-staging`.
- Artifact bucket: `pistachio-staging-artifacts-5304c19c`.
- Control identity: `pistachio-staging-control@azizilab-aml.iam.gserviceaccount.com`.
- Worker identity: `pistachio-staging-worker@azizilab-aml.iam.gserviceaccount.com`.
- Existing Slack bot token and signing secret. The old `MICA_SLACK_USER_MAP` is used only by the legacy mapped mode and migration.
- Existing Batch worker network/subnetwork: `default`; private workers have no external IPs.

Deployment verification found existing `nat-router` / `nat-config` covering all subnets on
`default`. The rollout reuses it; no NAT resource is needed. Keep `slack_agent_nat_enabled=false`.
Generated Bash remains network-isolated; only the trusted worker/Claude runtime uses outbound
access to Slack, Claude and Google APIs.

## Prepared code and rollout controls

- `src/mica/agents/analysis/`: single-session agent, briefs, provenance and Spot adapter.
- `docker/Dockerfile.slack-agent`: extends the existing pinned science image with the SDK,
  `socat`, and metadata inspection tools. The shared task worker keeps its compatible image.
- `cloudbuild/build-slack-agent.yaml`: builds the new image only.
- `infra/slack_agent.tf` and optional variables: project-prefix write access, selected data bucket
  readers and optional NAT. No runtime Secret Manager permission is added to the worker.
- `MICA_SLACK_AGENT_BACKEND=existing` is the default. Set `claude_batch` only at cutover.
- `MICA_SLACK_AGENT_IMAGE` must be a digest-pinned image.

The existing worker identity already has project-wide objectAdmin access from Mica's current
infrastructure (verified during the follow-up rollout). This change reuses it rather than claiming the plan's dedicated least-privilege
identity. Only configured buckets are mounted, mounts are read-only, and tools are restricted
to the current project's data path. The current staging deployment has already opted into the
outer container profile exception for nested sandboxing; the new worker honors that existing
staging/test-only flag. The inner Claude sandbox must pass its live gate and is never disabled
for Slack.

The SDK's `TMPDIR` uses a short, private `/tmp/pista-*` directory for each invocation, removed
when the invocation finishes, fails, or is cancelled. Do not place it under the durable private
workspace: those paths exceed the Unix socket limit and prevent the sandbox's bridge sockets
from starting. Durable workspace and session paths remain unchanged. The live acceptance test
must exercise a production-length private workspace, including session restoration.

The runner sets `CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR=1` so every Bash command
returns to the thread's writable workspace. A `cd` into project data applies within that
command only. Persisting a data-directory cwd can make the next sandbox fail while creating
its `.claude` metadata, before even a recovery `cd` can execute. Keep this setting on resumed
sessions too, and test a real Python command after entering a nested read-only data folder.

Staging also requires the separately approved `AGENT_BATCH_PROC_MOUNT_OPT_IN=true`
(`slack_agent_proc_mount_opt_in` in Terraform). This adds Docker's
`--security-opt systempaths=unconfined` so the inner sandbox can mount its private `/proc`.
It is rejected outside staging/test or without the existing nested sandbox opt-in. The worker
remains non-root; no privileged container, host PID namespace, or SYS_ADMIN capability is added.
Keep this flag false by default and rerun the isolation gate before changing these settings.

## Setup sequence

1. Create an Anthropic API key. Add it to a new Secret Manager secret in the existing project,
   for example `pistachio-staging-anthropic-api-key`, using the console or an approved secure
   local input flow. Do not paste it into chat, commit it, put it in tfvars, or pass it as a
   command-line value. The existing control identity reads the secret. A worker-authenticated,
   non-cached endpoint supplies only this credential and the existing Slack token to trusted
   bootstrap while its job is active; neither is stored in job metadata or GCS.
2. Set `AGENT_DATA_PROJECT=azizilab-aml` to accept any bucket owned by that project. The control
   service needs `roles/storage.bucketViewer` there to discover bucket ownership; each job mounts
   only its selected bucket read-only. No per-bucket deployment or data copy is needed. `init`
   accepts `gs://` links and Google Cloud Console/Storage URLs, including Slack link markup.
   `AGENT_DATA_BUCKETS` remains an explicit-list fallback when no project is configured.
3. Build the worker using the existing deployed science digest:

   ```bash
   gcloud builds submit --project=azizilab-aml \
     --config=cloudbuild/build-slack-agent.yaml \
     --substitutions=_SCIENCE_IMAGE=EXISTING_SCIENCE_DIGEST,_SLACK_IMAGE=NEW_WORKER_TAG
   ```

   Resolve the resulting worker image digest. Build/deploy the updated API through Mica's
   existing release path, initially with `MICA_SLACK_AGENT_BACKEND=existing`.
4. Prepare a Terraform plan against the **existing state**, preserving all existing values:
   `slack_agent_image`, `slack_agent_data_buckets`, `slack_agent_anthropic_secret`, `slack_agent_data_project`, and optionally
   `slack_agent_nat_enabled=true`. Leave `slack_agent_enabled=false` while preparing. Review
   any unrelated drift before applying. Do not recreate the app, bucket or worker account.
   Terraform is not installed in the implementation environment, so validate this plan before use.
   The current rollout uses scoped gcloud updates; see `slack-agent-deployment.md` for live state.
5. Review the artifact bucket lifecycle change: existing task/input/incoming/paused prefixes
   still expire after 30 days, including legacy workspace archives. New `projects/` objects preserve
   briefs, provenance and frozen thread metadata. This increases retention/storage use. Keep
   the image configured during a rollback so the old blanket lifecycle rule is not restored.
6. Check existing Slack scopes and subscriptions against `deploy/slack-app-manifest.yaml`.
   Retain `/integrations/slack/events` and `message.im`. Add file/history/user scopes only if
   missing; reinstall the existing app if scopes change. Channel mentions are optional; the
   existing bot DM is sufficient. Do not enable Socket Mode or import a new app.
7. Run the Linux isolation and live fixture checks in `docs/acceptance.md` with the pinned worker
   image and real private data mounts. Then cut over Slack only using
   `MICA_SLACK_AGENT_BACKEND=claude_batch` (Terraform: `slack_agent_enabled=true`).
8. In the existing bot DM, send `init project-slug gs://bucket/prefix/` and review the saved guide. A mention is unnecessary in the DM. New top-level messages start threads;
   reply in the bot's thread to continue an analysis. In a channel, mention the bot on the initial
   message and commands. Reply with missing details or corrections in ordinary language to revise
   the shared guide; corrections save automatically and apply on the next turn in every thread. Uploading a UTF-8 `.md` file
   under 100 KB with `brief` remains an optional way to replace the shared brief.

The local implementation was followed by the user-authorized deployment recorded in
`slack-agent-deployment.md`. That record is the source of truth for current deployment status.

## Settings and use

See `deploy/slack.env.example`. Defaults are 500 model turns, a $5 model threshold and three concurrent
standard jobs. Large and xlarge jobs in separate threads have no application-level concurrency
cap; GCP quota and capacity still apply. Each conversation remains ordered, and per-request time,
model and access controls still apply. Queued requests do not allocate a worker, so parallelism
reduces waiting time but does not inherently reduce compute cost.
Ordinary analysis normally starts on standard Spot (8 CPUs / 64 GiB); segmentation,
full-sample ingest and training normally start on large on-demand (32 CPUs / 256 GiB).
Large input expansion or prior measurements can select xlarge (64 CPUs / 512 GiB).
The seven-day `AGENT_MAX_RUN_SECONDS` limit is one absolute deadline across queueing and all
attempts. One upward resize and one preemption restart are allowed, at most three attempts total,
with the same total model allowance. Recovery prefers on-demand unless explicitly constrained.
Validated stages resume; unsaved or corrupt stages rerun automatically. Unknown task death alone
does not justify a larger worker, and uncertain final publication is never replayed automatically.
There is no infrastructure pricing or spending-approval flow.
File caching uses at most 400,000 MiB across mounted buckets, with range-read caching enabled. Validate the
Batch image's gcsfuse flags and available disk space during the canary.

Scoped GCS output writes are available through `write grant`, `write status`, and `write revoke`
in DMs after administrator configuration. Omit the grant duration for no expiry (until revoked).
See [Slack write access](slack-write-access.md) for examples, limits, and the cloud configuration.

Project controls: `init <slug> gs://bucket/prefix/` creates and selects a project, `switch` lists
your projects in a DM with copyable commands, and `switch <name or ID>` selects one for new
threads. The compact `init`/`switch` guide appears in unlinked chats and after initialization.
`help` shows every command in a grouped bullet list (`help all` is an alias). Init automatically
changes the DM's selection and
includes a command to switch back to the previous project. `use <project>` and `projects` remain
aliases.

`brief` shows the current project guide. Guide edits save automatically to shared knowledge,
with history and concurrent-edit protection; `undo knowledge` reverts the most recent save. Omit unknown guide details rather than adding
guesses or TODO lists. Rebinding does not
change existing thread data/briefs. Each turn writes changed outputs,
code, `run_log.jsonl` and `meta.json` under the audience-specific paths documented below. Slack
uploads supported result files under 200 MB. `datasets_touched` reflects paths visible in tool
inputs; it is not a syscall audit of files opened within scripts.

Rollback after the private-project cutover must retain ACL enforcement: drain work and use a
known compatible API/worker pair, or disable Slack ingress. Do not restore a pre-ACL backend.
Retain the registry, private memory, and lifecycle rules; legacy files do not make an old writer safe.

## Local development

```bash
.venv/bin/pip install -e '.[dev,slack,analysis-test]'
.venv/bin/python tests/make_fixture.py ./fixture_data
.venv/bin/python -m mica.agents.analysis.cli --help
.venv/bin/pytest -q
.venv/bin/ruff check src tests tools
.venv/bin/mypy src/mica
```

The local CLI can use `--no-sandbox` on macOS only. Slack commands use the HTTP control plane;
the Socket Mode entrypoint has been removed. Live testing requires explicit
`MICA_RUN_LIVE_TESTS=1` and an API key. See the
[Claude SDK reference](https://code.claude.com/docs/en/agent-sdk/python),
[Batch volume reference](https://docs.cloud.google.com/batch/docs/reference/rest/v1/projects.locations.jobs#Volume),
and [gcsfuse flags](https://docs.cloud.google.com/storage/docs/cloud-storage-fuse/cli-options).


## Initialization and file listings

`init` lists the data folder's immediate visible children through the trusted Google Cloud
Storage API. Pagination follows every page within a shared 20-second budget, without retries
or descending into subfolders. Zarr/N5 stores remain opaque; hidden/output paths are excluded.
Each folder reports `complete`; a timeout or listing error makes it false. No file contents,
schemas, cell counts, or biological facts are inferred from names.

For ordinary chat, the router resolves descriptive names from the project guide, project
memory, and conversation. It requests all relevant relative folders in one step (at most 50),
listing the root only when context cannot resolve a needed folder. Every path is checked
against project access, bucket admission, traversal, and array-store restrictions before listing.
The reply names the folders used. The conversation loop can request another bounded listing;
incomplete metadata alone never starts a worker or proves that missing entries are absent.

One `inventory event=<id> paths=<n> entries=<n> complete=<bool> ms=<n>` log records each step
without names. The conversation loop stores compact tool-step metadata, then a durable final
reply or action decision. Delivery retries reuse that final decision. The legacy path retains
pending intermediate routing records for unselected principals.
The initial guide is generated without a worker even if the listing is unavailable.

Retrying the same project/folder binding preserves its identity and saved brief. Init automatically
selects the project in your DM; no `--force` is needed. The flag remains accepted for compatibility.
Both init and `switch` change the default for new threads and preserve existing thread contexts.
Spot provisioning latency applies only to subsequent computation.


## Attachments in Slack

Attach images, PDFs, tables (`.csv`, `.tsv`, `.xlsx`, `.parquet`, `.json`, `.h5ad`), or text
in a project thread and say what you want to do. File-only messages get a short clarification.
Questions about a supported image or PDF can be answered directly; data analysis, re-plotting,
large images and full-table inspection use a compute worker. The worker sees read-only copies
under `inputs/`. Follow-ups can refer to uploads or files Mica delivered earlier.

Default upload caps are **10 files per message, 100 MB per file, and 250 MB per message**,
configurable with `AGENT_ATTACHMENT_MAX_FILES`, `AGENT_ATTACHMENT_MAX_MB`, and
`AGENT_ATTACHMENT_MAX_MESSAGE_MB` on the API. The limits apply to actual downloaded bytes.
Larger datasets belong in the project's mounted GCS bucket. Each thread tracks up to 50 files;
older delivered-output references are dropped before user-upload references.

After admission and project access checks, uploads stream from private Slack URLs into the
artifact bucket under `tasks/slack-analysis/uploads/<thread-run-scope>/attachments/`.
**Uploaded originals expire under the existing 30-day lifecycle**, as chosen for this feature.
They are excluded from workspace archives, so later checkpoints do not renew their retention.
Private uploads use the same user/project/thread scope as private provenance. The durable
thread registry keeps metadata and output references; conversation excerpts and derived outputs
follow existing provenance retention. Deleting the Slack original does not delete the stored copy.
Missing or expired originals require a new upload. There are no separate file permissions.

The router receives only current-message content or images/PDFs explicitly requested through its
`view` route after another access check. Each call can show up to 4 PNG/JPEG/GIF/WebP images
at 3.75 MB each and 2 PDFs at 5 MB each, plus at most 8 KiB of each current text/table file.
Larger or unsupported visuals need a sandbox preview. No OCR, HEIC, SVG rendering, audio, or video
is provided. For SVG, create a PNG twin. Full-size output files retain their existing retention.

`brief` with one UTF-8 `.md` still replaces the shared brief byte for byte. `brief` with another
file type is rejected. Send attachments separately from `init` and `save brief`.

## Figures and visual checks

Reports and figures are delivered as Slack attachments. The user cannot access the worker's
`outputs/` directory or the agent bucket. New or modified PDFs, PNGs, and other supported
files in `outputs/` are uploaded to the requesting thread before the final answer.
Subfolders such as `outputs/gap_report/` are included recursively. No top-level copy is required.
To resend existing files, the agent refreshes their modification times so delivery includes them again.
Files must fit the 200 MB per-file limit; larger reports need compression or splitting.
Upload failures produce an explicit delivery warning instead of a success claim, and the
workspace remains eligible for checkpointing so a later resend can reuse the files.

The compute agent inspects figures through sandbox-rendered previews in `.previews/`.
Only PNG/JPEG previews up to 1568 px on the long edge and 1.5 MB can enter visual Read,
with at most 12 views per turn. The SDK JSON buffer is 16 MiB. Images and PDF pages are
rendered by `tools/preview.py`; no media decoder runs in the control plane or worker host.
Previews are cleared before and after a turn and excluded from delivery and checkpoints.
Full-resolution originals are delivered unchanged. Visual checks supplement computed checks;
they do not establish scientific correctness. SVG and HEIC require a PNG alternative.


## Identity, projects, and private memory

### Owner access requests

Set `AGENT_ACCESS_REQUEST_OWNER_ID` to the single designated owner's `workspace:member`
Slack identity to enable owner approval cards in that person's DM. This is separate from
channel-onboarding admins, failure operators and write administrators; it grants none of
those roles. An empty setting disables this workflow. Owner delivery uses the existing
`chat:write` scope and records Slack's returned DM ID; no `im:write` scope is needed.

When a verified Slack member asks for project work from an unauthorized channel, Mica
requests owner review before invoking the model or starting a worker. A member may already
have project access in DMs while their channel still needs publication and binding.
In a private conversation without project access, Claude has a `request_access` proposal
tool and explicit access context. Existing members with no selected project are asked to
choose a project. Account/help/setup commands remain available. Identity lookup outages
and invalid/non-workspace identities are not treated as requests that can bypass admission.

The owner receives the requester, verified email, origin and a bounded excerpt of the
request, with a green **Approve access…** button and **Deny**. Approve opens a project
selector listing only projects the reviewer owns. The confirmation explicitly grants that
person membership; for a channel request it also publishes and binds that project to the
channel, warning that all channel viewers (including future members) can read replies.
It cannot replace another channel binding or grant another owner's project. It does not
grant write permissions, cloud IAM, co-ownership, or compute approval.

Approvals recheck both Slack identities, current ownership, project access revision and
channel binding. Decisions are bound to the original DM/card and modal. They expire after
24 hours; repeated requests from the same person and scope on a UTC day share one card.
Declined requests stay declined for that day. Durable registry receipts prevent old clicks
or retries after a crash from restoring subsequently revoked access. The requester receives
the decision and must send a fresh task; no old analysis is automatically restarted.

Delivery uses durable intent records under `access-request:` connector state. An ambiguous
post is held rather than blindly duplicated. The requester is not told an owner was notified
unless Slack returned the message timestamp. Failed card edits can be retried by revisiting
the card; ambiguous decision notifications require operator inspection before resending.

The live bot uses `AGENT_ACCESS_MODE=workspace`: active human members of its Slack workspace
can use their own private agent DM without joining an access channel. Slack must expose a valid
email for the member. Projects and personal memory stay private; adding Mica to a channel
does not publish any DM content. Invite the bot, explicitly `publish <project> #channel` from
the owner's DM, then mention it with `switch <project>` in that channel. Other people need
explicit project sharing to invoke it; channel viewers can read replies posted there.

The optional default `AGENT_ACCESS_MODE=channel` admits members of **any enabled access channel** in the
bot's workspace, including when they use the bot in DMs. Enabled channels are stored atomically
in `projects/_access/channels.json`. The lab list below also admits its members in channel mode;
with no enabled channels or matching lab member, agent access is denied.
Membership does not automatically share projects or publish their contents into a channel.

The bot obtains each human member's normalized email from Slack. That retains the existing
per-email project/memory identity, provided the previous manual mapping matched their Slack email.
A bot, deleted user, wrong-workspace request, or missing readable email is denied. Review mismatched
legacy mappings during migration; never automatically merge different identities. In workspace or channel mode,
`MICA_SLACK_USER_MAP`, `MICA_ALLOWED_EMAILS`, and `AGENT_ALLOWED_USER_IDS` do not restrict
Slack admission. The task API keeps its existing email allowlist and OIDC authentication.
`AGENT_ACCESS_MODE=mapped` explicitly retains the previous manual Slack mapping/allowlist behavior.

Admission is checked before intake, commands, queued work, credential delivery, worker restoration,
and result/file delivery. Slack member lists and profiles have a short process-local cache:
`AGENT_ACCESS_CACHE_SECONDS=30` by default (0–60 seconds). Removal from the last enabled channel
blocks access once that cache expires; removal from just one channel preserves access through
another. Expired results are never reused after an API failure. An unavailable channel grants no
access, but a separately verified enabled channel can still establish eligibility. Disabling an
access channel bypasses the membership cache because the approval registry is read each time.
Removing access does not delete personal memory or shared-project grants; returning eligible users
retain their own data and still face the independent project ACL checks.

### Lab members and all-projects access

`projects/_access/lab.json` in the artifacts bucket is the shared lab pre-approval list.
`members` grants Slack admission in channel mode through a verified, workspace-scoped human
profile, without adding anyone to projects. `all_projects` must be a subset of `members` and
grants use of all current and future projects. It does not confer ownership, sharing, renaming,
publication or approval rights, and channel work still requires project publication.
Workspace mode already admits active humans; mapped mode and the task API allowlist are unchanged.
Both the API and workers read the lab document on every relevant check. Removing a lab grant
takes effect on the next check, including when the Slack profile is cached. A user may still
qualify through an enabled channel, workspace admission or explicit project membership.
A missing document is an empty list. Slack lookup failures retain the existing transient-error
behavior; the list never substitutes for verification of an active human profile.

The initial list contains `sk4698@columbia.edu`, `az831@nyu.edu`,
`alvand.vahedi.2706@gmail.com`, `aat2221@columbia.edu`, `elham@azizilab.com`,
`il2332@columbia.edu`, `jm5707@columbia.edu`, `kevin.hoffer-hawlik@columbia.edu`,
`ls3456@columbia.edu`, `mz2934@cumc.columbia.edu`, `nk3047@cumc.columbia.edu`, and
`mv3112@columbia.edu`. Only `elham@azizilab.com` initially has all-projects access.
The live document is authoritative; addresses must match Slack profile emails for Slack admission.

Inspect and edit it with the operator script, using existing GCS credentials:

```bash
PYTHONPATH=src .venv/bin/python scripts/lab_members.py \
  --bucket mica-staging-artifacts-azizilab-aml show > /tmp/mica-lab.json
# Edit /tmp/mica-lab.json; show prints the current generation to stderr.
PYTHONPATH=src .venv/bin/python scripts/lab_members.py \
  --bucket mica-staging-artifacts-azizilab-aml set /tmp/mica-lab.json \
  --generation GENERATION_FROM_SHOW
```

The JSON shape is `{"version":1,"members":["person@example.com"],"all_projects":[]}`.
The script normalizes emails, collapses duplicates, validates the subset, prints the diff and
performs one compare-and-swap write. A concurrent change fails without retrying or overwriting
it; inspect the current list before trying again. Use `--generation 0` for create-only seeding
after a coordinated API and worker release. `--root PATH` supports offline inspection and edits.

### Optional channel-based admission

Use this mode only when membership in selected channels should determine who can use the bot.
The live workspace mode does not need this onboarding:

1. Set `AGENT_ACCESS_MODE=channel` and configure the people allowed to manage access channels in
   `AGENT_ACCESS_ADMIN_IDS`, as comma-separated `workspace:member` Slack IDs. Terraform exposes
   `slack_agent_access_mode` and `slack_agent_access_admin_ids`. This small admin list controls
   onboarding only; it is not the list of people allowed to use the agent. Admin status does not
   grant access to other people's projects or bypass channel membership for ordinary requests.
2. Add `channels:read`, `groups:read`, and `users:read.email` alongside existing `users:read`,
   `app_mentions:read`, and chat permissions; subscribe to `app_mention`. Apply the delta in
   `deploy/slack-app-manifest.yaml` to the existing app and reinstall if Slack requires it.
   The existing signed HTTP webhook is retained. No Socket Mode or new bot is needed.
3. Invite Mica to a channel. A designated admin sends `@Mica enable this channel`.
   The bot checks workspace identity, channel visibility, membership-read and email permissions,
   then records approval. This works for public and private channels. Inviting the bot alone does
   not authorize access, and an ordinary member cannot enable a new channel.
4. Repeat the invitation and command in each additional access channel. There is no channel-ID
   configuration or redeployment per channel. Members can then initialize their own projects
   in DMs. Use explicit project `publish` and `use` commands when a project should reply in a
   channel; enabling an access channel does not expose a project there.
5. An admin can send `@Mica disable this channel` to remove that channel from admission,
   or `agent channels` in a DM to list enabled channels. These admin commands work before the
   admin has ordinary agent access, allowing the first channel to be enabled. Disable the channel
   before removing the bot; removing the bot alone is not the command to revoke approval. Any
   resulting membership lookup failure denies access through that channel.

Use an invite-only channel when access should be granted by invitation. Anyone who joins a public
enabled channel is intentionally eligible. Slack guest/externally shared channel members are
eligible too if they are in the verified member list and Slack exposes their email to this bot.
Slack cannot retroactively hide messages already posted to a channel when someone loses access.
Channel membership is the admission source, not a synchronization job or a manually copied list.

Slack API references: [channel members](https://docs.slack.dev/reference/methods/conversations.members/),
[user identity and email scopes](https://docs.slack.dev/reference/methods/users.info/).


Projects are private by default. Their opaque IDs are independent of display names: Alice’s
`liver` and Bob’s `liver` are separate projects with separate `PROJECT.md` files. Repeating init
with the same owner, name, and data URI reuses that project; changing the URI requires a new name.
Each thread retains its selected project even after `switch` changes the default. Send a new
message outside the existing thread to start with the selected project. Existing threads
refresh their shared guide on each turn. Guide edits are visible to project members and
authorized channels. Concurrent conflicting edits are retained in `_unmerged/` for reconciliation.

| Command | Where and effect |
| --- | --- |
| `init liver gs://bucket/folder/` | DM: create/reuse and select your project, save its initial guide, and offer a command to switch back. |
| `help` / `help all` | Show the same complete, grouped command list. |
| `switch` (alias: `projects`) | DM: list owned and explicitly shared projects with copyable switch commands; mark the default for new threads. |
| `switch liver` or `switch p_…` (alias: `use`) | Select a project for new threads. Ambiguous shared names require an ID. |
| `rename liver liver-atlas` | Owner in DM: change the project name. An ID can replace the old name. Use 3–40 lowercase letters, digits, or hyphens; the new name must not belong to another project you own. |
| `share liver @user` / `unshare liver @user` | Owner in DM: grant an enabled user or revoke a former member; email links also work. |
| `publish liver #channel` / `unpublish liver #channel` | Owner in DM: authorize/withdraw replies visible to the entire channel audience. |
| `switch p_… [--force]` in a channel | Owner selects an explicitly published project; replacing a binding requires its current owner too. |
| `brief` | View the current shared guide, after an access check. |
| `cancel` | Cancel queued/active computation after an access check. |
| `memory [path]` | Show the shared project index or an allowed project-memory file. |

Renaming preserves the project ID, data location, briefs, saved outputs, sharing, channel
bindings, and existing threads. Use the new name or unchanged ID in later commands; a legacy
project whose ID is its original name still accepts that original ID. Renaming does not edit
old Slack messages or rewrite project names in saved brief text.

Sharing a project grants access to its guide, computation and shared conversation memory,
including compact summaries of DMs, channels and email. Repository readers can see the saved
memory too. Channel publication is a separate decision: everyone viewing that Slack channel
can read posted content. Removing membership does not retract previously posted messages.
Queued workers recheck access before restoring context and delivering results.

Project memory saves automatically after conversational turns. Correct remembered facts in
plain language. `memory` shows the project index and `brief` shows its guide. Legacy `remember`,
`forget`, `memory context` and `forget context` workflows are retired. Memory is advisory data,
never authorization or proof that a scientific claim is verified. See
[project memory](project-memory.md) for budgets, ordering, migration and retention.

DM outputs live at `projects/_users/<user-hash>/runs/<project-id>/<thread-key>/turn-<n>/`.
Authorized channel outputs retain `projects/<project-id>/runs/<thread-ts>/turn-<n>/`.
The agent’s filesystem sandbox cannot browse another run or the user-memory store. These are
application isolation boundaries: the trusted service accounts still have the existing bucket
permissions. Lab-wide data-bucket access remains intentional; this is not per-user data IAM.

## Private-project migration and cutover

Deploy only within the user's authorized scope. The current user authorized the complete source
sync, storage migration, and Slack permission reinstall. Record the actual deployed revision,
paired image digests, and migration verification in `slack-agent-deployment.md`.

1. Confirm onboarding admins, membership/email scopes, and legacy identity mappings. Build the
   headless API and Claude worker from the same source snapshot. `cloudbuild/build-slack-api.yaml`
   uses `docker/Dockerfile.api` and requires `_API_IMAGE` plus the compatible shared task worker
   `_WORKER_IMAGE`. It checks that UI routes/assets are absent and that the worker execution
   contract matches. The old frozen web-image overlay is retired. See [deployment](deployment.md)
   for paired-worker updates if that compatibility check fails.
2. Stop Slack ingress to old code. Drain or cancel old Cloud Tasks receipts and Batch jobs;
   verify no old registry/brief writers can run, including delayed retries. Keep ingress stopped
   until the new API and worker are both ready. A second migration pass while old writers are
   live does not provide safe cutover.
3. Run a read-only report, then review ownership mappings. The report excludes brief contents:

   ```bash
   .venv/bin/python tools/migrate_slack_registry.py --bucket ARTIFACT_BUCKET \
     --team T_WORKSPACE --user-map /secure/user-map.json --owners /secure/owners.json
   ```

   The default report writes a draft local owners file only if one does not exist. Confirm it
   with the project owners. Unowned projects fail closed. Legacy channel pointers are migrated
   without publication permission; each owner must explicitly publish before channel replies.
4. After reviewing the report and confirming the drain, apply with the same arguments plus
   `--apply --quiesced`. This writes a registry backup, adds missing entries atomically, preserves
   legacy files, and rejects ownership/data/binding drift. Reapplying with unchanged inputs is a
   no-op. The `--quiesced` flag is an operator attestation, not an automatic cloud drain.
5. Deploy a tagged revision without traffic using the paired digest-pinned images. Run health,
   credential, sandbox, and two-user checks from the plan. Promote it, then restore Slack ingress
   for the selected admission mode. Workspace mode admits verified workspace members directly;
   channel mode needs initial access channels enabled using the commands above. For channel mode,
   verify one user with overlapping memberships, removal from
   the last enabled channel, denied non-admin onboarding, and private project/memory boundaries.
   New API code refuses unmigrated projects; new workers refuse incompatible old manifests.
6. Roll back only to a known ACL-compatible pair after draining. If none exists, keep Slack
   ingress disabled until repaired. Never route private-project traffic to the old slug backend.

For local rehearsal, replace `--bucket` with `--local-root /tmp/mica-migration-fixture`.
Keep owner maps, real configuration, Terraform state, and credentials out of Git. On another
laptop, clone the repository, select the implementation branch, recreate the virtualenv and local
configuration, and authenticate to the existing cloud resources. The deployed runtime and its
memory are in GCP; the laptop does not need to remain running.


## Project guide and thread context

`knowledge/PROJECT.md` records project purpose, data locations, file/folder uses and verified
metadata. Guide updates save automatically. Preferences, observations, results and pending work
use the corresponding project-memory entries. `brief` displays the guide.

The router reads project memory, the current thread's compact summary and uncovered log entries,
then the last four verbatim exchanges from its working checkpoint. `THREADS.md` lists project
thread progress. Authorized `memory_paths` lookups can read another thread when the overview
is ambiguous. Workers Read/Grep the project snapshot before the current thread and other
threads. A summary's turn number does not imply it covers every earlier event.

## Long-run limits and closing summaries

`AGENT_MAX_TURNS` defaults to 500 as a sanity limit. Execution time and the existing model
budget remain the primary bounds. Each attempt reserves 10% of its model allowance, up to
$0.50, for one closing call if the SDK reports a turn or model-budget limit.

The closing call resumes the same session with all tools and MCP servers disabled,
`max_turns=1`, and at most 60 seconds within the remaining execution deadline. It summarizes
completed work, saved results, unfinished work, and the next step. Its spend is counted in
normal accounting. The original error remains, so Slack still displays `Stopped` and appends
a short limit notice. A failed or empty closing call preserves the previous partial reply
and retry message. Other errors do not trigger a summary call.

## Large GCS publications and failed requests

Output approval probes each requested folder without enumerating its files. Publishing uses
batched, resumable GCS server-side copies and removes verified staged sources after recording
receipts; see [write access and cleanup](slack-write-access.md). Full stage checkpoints have no
application size cap. New-protocol stage archives and leftover write exports are deleted only
after the logical request is terminal and Batch confirms worker exit. Recovery attempts retain
them. Cleanup retries independently of Slack delivery; receipts and manifests remain. Historical
events are not swept.

### Durable memory and lean workspaces

GitHub is the durable destination for compact thread logs, summaries and accepted scripts.
The coordinated `dual` → `github` → `github_only` rollout preserves GCS history until every
legacy conversation is imported and verified. During the first two phases, legacy history
recall remains available. The final phase uses project memory and disables GCS history access.

The workspace archive remains a temporary working copy of code, notes, logs and small reports.
A worker restores it while available, then falls back to its own thread's GitHub scripts.
Recovery stages retain their existing same-request integrity and authorization checks. Code
is omitted from separate provenance uploads when thread memory is enabled. Dataset stores,
`outputs/gcs/`, scratch and caches retain their existing exclusion rules.

Daily maintenance moves eligible outputs after 10 days and deletes a source only 30 days after
a verified copy. Archives with rejected scripts, unmatched outputs or legacy SDK transcripts
remain retained. Cleanup checks active work and source generations; pending memory is retried
and reported rather than silently dropped. Failed turns retain pending intent without promoting
partial results. Commands and cancellations add no memory. See
[project memory](project-memory.md) for exact rollout controls and migration commands.

## Troubleshooting access checks and stopped runs

Transient Slack identity errors do not revoke project access. An already verified worker can
continue computing for `AGENT_ACCESS_UNAVAILABLE_GRACE_SECONDS` (default 1800) while checks retry.
Publishing results and delivering replies require a successful current check. Registry unshares,
binding changes and signature mismatches still stop work immediately.

Look for `access_check_unavailable event=... reason=...` in worker logs. A prolonged outage stops
with `access_unverifiable`; a definite access change uses `access_revoked`; the execution deadline
uses `compute_time_allowance`. `Cancel` records `cancelled` with cause `user_cancelled`. These are
not OOM failures; worker history reports OOM only with kernel evidence. The final notice includes
receipt-confirmed folder counts when access can be verified. An incomplete folder must be
republished before use.

New workers cancel active publishing tools, persist the stop reason and enforce a shutdown
deadline. Completed objects retain their receipts. Cancellation allows cooperative shutdown,
with forced Batch deletion as a fallback; older in-flight workers keep their existing protocol.

## Transfers

AtoMx transfers use a dedicated transfer worker and the user's saved SFTP login, with a pinned
SSH host key to authenticate the server. User credentials do not replace host-key verification.
Transfers remain disabled until the image, independently verified host key, service identity,
network access and scratch-bucket canary are confirmed.

The completion-marker update is implemented but **not deployed** as of 20 October 2026.
Image builds, live bucket checks and rollout are on hold pending key confirmation. No project
configuration or transfer flag has been changed.

Verified transfers create `done.txt` in both destination sample folders after publishing the
authoritative `transfer_status/<sample>.done.json`. The admin-only `atomx-transfer backfill`
command can repair missing text or record legacy bucket listings; it defaults to a dry run and
never connects to SFTP. Backfilled records are explicitly listing-only, not source verification.
There is no Slack backfill command or backfill Batch job. See the
[transfer CLI documentation](../tools/atomx_transfer/README.md) for format, concurrency rules and
verification behavior.

## Email requests

Email can use the same Claude agent, projects and analysis workers as Slack. The initial mailbox
is `azizi.lab.341+mica@gmail.com`; intake remains disabled until mailbox OAuth and the Mica label
are configured (see [deployment](deployment.md#gmail-activation)). Only labeled, cryptographically
verified messages are accepted. DKIM must align exactly with the sender's domain; malformed,
unsigned and unaligned mail is silently dropped. Gmail aliases and sent messages cannot trigger
reply loops. The Slack email allowlist is not an email membership gate.

Send `projects` to list your projects, or `switch <project>` to select the default for new threads
in both email and Slack DMs. Existing conversations keep their project. A single legacy DM default
is inherited; conflicting defaults require an explicit switch. Private email history belongs to
one sender in one Gmail thread; adding another sender does not grant access to that history.

Each request receives an acknowledgement and one final email containing its answer and supported
attachments. Progress updates are suppressed. Final replies include only current project members
from the original To/Cc list, and name recipients omitted for lack of membership. Project access
is checked again before delivery. Attachment limits also apply to encoded MIME size; ask in the
same email thread for smaller files when needed. Slack cannot retrieve private email artifacts.

Email supports reading data and returning analysis artifacts. Project-data writes, transfers,
access-management commands and interactive approval controls require Slack. Internal conversation
checkpoints and project knowledge continue to work normally.

### Session upload cards

`import session` opens a project-scoped workflow in a Slack DM. Natural-language import
requests use the same command. The modal accepts one Claude Code JSONL file, optional
highlights, and an optional request to perform after the verified import review.
Slack's [file input element](https://docs.slack.dev/reference/block-kit/block-elements/file-input-element/)
requires `files:read` and has a 100 MB limit; the reference manifest already includes that
scope. Server-side type and byte limits apply independently of the file picker. Modal
submission stores a durable receipt and acknowledges without downloading the file.

Deploy the API and CPU/GPU workers together. Leave `AGENT_SESSION_IMPORT_PROJECTS` empty during
the release, verify the installed app's scope and modal with a synthetic test project, then
add approved project IDs, or set it to `*` for all current and future projects while retaining
ordinary project access checks. Measure a full-size slice; if it spends over 60% of the existing
per-turn budget, reduce `AGENT_SESSION_IMPORT_SLICE_CHARS`. Verify a two-pass import, a separate
follow-on question and a new-thread reference lookup before enabling a collaborator's project.
See [project memory](project-memory.md#detailed-results-and-session-imports) for storage,
continuation, retention and partial-save behavior.

## VM and other cloud-access requests

With `AGENT_CLOUD_ACCESS` configured, invite Mica to the bundle's request channel (initially
#vm-access). Do not enable it as an analysis channel. A top-level message gets a request button,
with the requester's email prefilled and a required note. Targets may be other people. Lab
members are preapproved only when the bundle enables that policy; other targets require the
configured approver's card in the thread. Existing access sends no welcome email.

Email supports the same configured bundles and a narrowly scoped approval reply; other
administrative and project-write operations still require Slack. See
[cloud-access requests and activation](cloud-access.md) for the settings, role boundary,
welcome delivery and guided live checks.

## Agent experience

Mica can use Slack's Agent messaging experience with the existing app ID and message endpoints.
Apply the delta in `deploy/slack-app-manifest.yaml` to the **existing** app, preserving its request
URLs, interactivity, and other settings. `features.agent_view` supplies the description;
the API supplies per-user suggested prompts. Adding `assistant:write` requires reinstalling
the app. Some agent features
require a paid workspace. Treat the app switch as one-way: Slack explicitly disallows returning
from `agent_view` to `assistant_view`, and does not promise a reverse path for a plain bot app.

Native session behavior is separately controlled by `AGENT_SLACK_SESSIONS=false` by default.
After enabling the `agent_session_stopped` subscription, set it to `true` on the control-plane
API. Use the existing `runtime_environment` map when recording this nonsecret setting in the
deployment configuration; no new infrastructure is needed. The controller passes its native
session decision to each newly submitted worker in the request manifest.

In DMs, native sessions show working/ready/waiting-for-input status and an initial conversation
title. Mica retains user-renamed titles. Working status refreshes within thirty-five minutes
through polling or the existing five-minute reconciler, before Slack's one-hour expiry. Native
Stop enters the same durable queue as typing `cancel`: it stops earlier running and queued
requests in that DM thread, subject to existing access checks, without needing a check-in card.
Delayed cancellation never targets messages sent after the stop timestamp. Shared-channel
native sessions are not enabled; existing mentions, progress cards and cancellation remain.
Transfer controllers continue to own their transfer cards and cancellation after handoff.

In native sessions, Slack owns the working indicator and Stop control. Mica skips content-free
receipt/thinking messages and posts its first message when it has a step, commentary, queue
notice, approval or answer. Current activity appears in the plan title; task rows show completed
or failed steps, with no second spinner. Before any tasks finish, additional details appear as
plain context. Finished/stopped/error endings retain their existing presentation. Check-ins keep
**Keep working** and **Report a problem**, while Slack provides Stop. Channels, email and
sessions-disabled rendering retain their existing behavior.

Progress rendering requires a coordinated API and CPU/GPU worker release. Existing workers keep
their saved presentation mode; newly submitted workers use the controller's current decision.
Rollback this presentation release using the prior API revision and worker pins. No Slack app
configuration change is needed when native sessions are already enabled.

Final Slack answers use standard Markdown blocks, including small comparison tables. Model
angle syntax is escaped to prevent mentions and disguised Slack links. Unsupported Markdown
blocks fall back to the existing `mrkdwn` conversion. Code fences and tables stay together
within Slack's 12,000-character message limit; larger blocks continue with repeated fences or
table headers. An individual oversized header or row is delivered in bounded literal pieces.
Notification text carries the same escaped content. Email retains its existing delivery path.

`AGENT_SLACK_STREAMING=false` controls streaming of conversation answers only. After 300
characters of public text, an answer may stream; short answers post whole. Thinking blocks
never stream. A later tool call ends the stream and leaves its public narration in the thread.
Worker answers continue through their existing final-message delivery. Streams stop at the
model-call deadline, nine minutes or 10,000 escaped characters, with remaining answer content
delivered as Markdown messages. Finished activity remains separate from the answer.

Only one stream may be started per request. Its empty creation and returned message ID are
saved before content is appended. Retried slices use ordinary delivery and remove a partial
stream. Unattributable empty orphans are left untouched and Slack finalizes them automatically.
Failed cleanup retries through one tick after terminal completion, then reports through existing
delivery-failure observability; a partial message may remain. Stream writes recheck project and
reference access and cancellation. A stream stopping queues the usual derived session status.

The Messages tab shows **Project: &lt;name&gt;** or **No project selected** and up to four
`AGENT_SUGGESTED_PROMPTS` templates (a JSON array of `{title, message}` objects). `{project}`
is replaced with the current default project's name; templates needing a project are omitted
when none is selected. The defaults offer a dataset listing, a plot, the brief and generic help.
Unchanged prompts are not resent. These updates use no model or compute job.

The Home tab lists usable projects, roles and data folders. Selecting a project updates the
default for new conversations through the same permission checks and registry operation as
typing `use`. Existing threads keep their project. The picker shows at most 100 projects,
ordered by recent usage, including the current selection. A typed project switch refreshes
Home and Messages too.

For an existing agent app, the owner applies a merge-only manifest delta: remove static
`agent_view.suggested_prompts`, enable `app_home.home_tab_enabled`, and subscribe to
`app_home_opened`. This delta adds no scopes. Deploy the coordinated API and workers with
streaming off, then verify Markdown, mention escaping, prompts, project selection, old-thread
isolation and an email. Enable streaming only after those checks and test a long answer and
a one-line answer. Re-adding static prompts disables dynamic suggestions; turning Home off
removes the picker; turning the streaming flag off restores ordinary answer delivery.

Slack owns the **Stop Mica** label. Custom loading text through Slack's legacy status bridge
requires a separate owner-observed compatibility check; it is not enabled by these settings.

Session updates run in authenticated Cloud Tasks. Receipt markers preserve updates across
enqueue failures, process crashes and Slack errors; the existing reconciler retries them.
These presentation tasks do not execute analyses or alter their outcomes. Email is unchanged.

Roll out in this order:

1. Deploy the API with the flag off, preserving credentials and worker pins.
2. Validate a separate test app in the lab workspace using Socket Mode. Capture sanitized DM,
   thread, split-view, file-share and Stop payloads; check old-DM history and replies, suggested
   prompts, titles, native status, cancellation and what the admin display toggle restores.
3. The app owner applies the manifest delta and reinstalls. Credential replacement, if required,
   is a separate operator step. Verify the deployed service can actually reply with its token.
4. With native sessions still off, verify old/new DM threads, split view, channel mentions and
   follow-ups, file intake/output, approvals and a labeled email test.
5. Enable the Stop subscription and flag together. Verify status transitions and cancel only
   your own small test computation, confirming both the user notice and actual job termination.

For native-session rollback, turn the flag off and remove the Stop subscription. Already queued
presentation tasks honor the flag; previously accepted cancellation receipts still run. Existing
working indicators expire within an hour. The admin display toggle is not a guaranteed app
migration rollback; rely only on behavior observed in the separate test app. Keep cutover pending
until both live phases pass, and clean up any recorded smoke artifacts.

References: [Slack agent sessions](https://docs.slack.dev/ai/agent-sessions/),
[app manifest](https://docs.slack.dev/reference/app-manifest/).
