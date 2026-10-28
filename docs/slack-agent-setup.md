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

Only requests that need file contents, code execution, or plots proceed to a sized Batch worker.
Listing object names, folders, and sizes uses bounded metadata calls on Cloud Run, including
in ordinary chat. Reading CSV headers or counting cells still requires the worker.
Conversation routing is always enabled. Ordinary questions, planning, brief edits,
`help`, `brief`, `cancel`, and bounded initial setup run in the control service.
The direct Claude call has no shell, filesystem, or network tools. It can answer, update the shared guide,
request a project-scoped metadata listing, or request isolated computation. Invalid routing output
gets one immediate correction attempt with field-level feedback. If that fails, or the model API
times out, rate-limits, or returns a server error, the original request stays pending and retries
through the existing queue no earlier than 30 and 60 seconds after each failure. There are at most
three routing cycles; each decision gets one correction and at most one media fallback. The initial
decision and any inventory/view follow-up share a 120-second routing deadline. Exhaustion reports
an internal failure and preserves the conversation. Only schema
field names and error types are logged, never invalid values. Each attempt rechecks access;
failed routing never invents a computation or bypasses write approval. Display-only worker workload
and reason labels are truncated to 200 characters; short reasons become "unspecified". Routes,
resource choices, paths, and replies still require validation. Cancellation bypasses queued work
and stops the active job without starting a new one.

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
During analysis the same message shows “Mica is working” followed by the configured loading
emoji while tools run, and “Mica is thinking” during model processing. The expandable activity
list keeps current tool descriptions and confirmed completion states; Cellpose entries name
the crop being processed. Phase changes come from SDK lifecycle events, never private reasoning
text. Identical states are deduplicated, updates are throttled, and long-run check-ins report
elapsed time without posting extra messages. Status failures do not prevent work, and repeated
webhook/poll deliveries reuse the stored message timestamp.

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

See `deploy/slack.env.example`. Defaults are 500 model turns, $5 per message and three concurrent
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
The reply names the folders used. Incomplete listings and requests for another listing route
to a worker that can finish the work; missing entries in partial results never prove absence.

One `inventory event=<id> paths=<n> entries=<n> complete=<bool> ms=<n>` log records each step
without names. Intermediate routing decisions are saved for failure diagnosis and marked pending
until a final reply or compute decision is ready. Delivery retries reuse the final decision.
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

The live bot uses `AGENT_ACCESS_MODE=workspace`: active human members of its Slack workspace
can use their own private agent DM without joining an access channel. Slack must expose a valid
email for the member. Projects and personal memory stay private; adding Mica to a channel
does not publish any DM content. Invite the bot, explicitly `publish <project> #channel` from
the owner's DM, then mention it with `switch <project>` in that channel. Other people need
explicit project sharing to invoke it; channel viewers can read replies posted there.

The optional default `AGENT_ACCESS_MODE=channel` admits members of **any enabled access channel** in the
bot's workspace, including when they use the bot in DMs. Enabled channels are stored atomically
in `projects/_access/channels.json`. With no enabled channels, ordinary agent access is denied.
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
| `memory` | DM: show private profile/preferences and the newest 20 numbered notes. |
| `remember <text>` | DM: add a private note (up to 600 characters). |
| `forget <number\|profile\|preferences\|all>` | DM: delete the selected memory and invalidate previous private context. |

Renaming preserves the project ID, data location, briefs, saved outputs, sharing, channel
bindings, and existing threads. Use the new name or unchanged ID in later commands; a legacy
project whose ID is its original name still accepts that original ID. Renaming does not edit
old Slack messages or rewrite project names in saved brief text.

Sharing a project grants access to its shared brief and computation, not another user’s DM
history or memory. Channel publication is a separate explicit decision: every current or future
viewer of that Slack channel can read posted content, even if they cannot invoke the agent.
Removing a member does not retract Slack messages or remove them from a published channel’s
audience. Unpublish disables future replies there. Old queued workers recheck grants before
restoring context and before delivering results. Previously submitted pre-ACL workers must be
drained at cutover.

Memory is stored only at `projects/_users/<hash-of-normalized-email>/memory.json`. No personal
memory is injected into channels or group conversations. DM retrieval may use notes from another
project belonging to, or currently shared with, that same user. Access filtering happens before
ranking and before the memory editor receives context. Revoked notes appear as hidden placeholders
in `memory`; their content is excluded. A block containing contributions from a revoked project
is hidden as a whole. Notes retain their original project attribution; they are suggestions,
not facts about the current project. Init displays them in a private reply, never automatically
in the initial brief. Applying personal memory to the shared guide requires the user’s request; saving
guide edits deliberately shares their contents through project knowledge.

Profile and preferences are each bounded to 1,500 characters. Up to 200 notes have stable numbers;
retrieval selects at most five within 2,500 characters, using keyword overlap and recency. Atomic
writes prevent lost concurrent updates. Permanent operation receipts prevent delayed deliveries
from resurrecting forgotten content. The post-reply editor proposes at most three changes, rejects
credential-like text, and sends a confirmation only after saving. It runs as a separate retried task with a 90-second model timeout and a
150-second task deadline, after the main reply is delivered. Set `AGENT_MEMORY_AUTO_EDIT=false` to disable it;
explicit memory commands still work. `AGENT_MEMORY_MODEL` defaults to `AGENT_MODEL`.

Routine personal-memory edits, deletions, sharing and unrelated project changes keep the current
workspace, SDK session and transcript. `forget context` and `forget all` explicitly reset the
applicable context; retired checkpoints retain audit history and output numbering continues.
Permission checks and revoke-then-regrant protection remain tied to the current project's access
revision. Existing saved workdirs stay valid through the migration. Removed memory is excluded
from future retrieval, while existing conversation context remains until explicitly reset.
Slack messages, old outputs and audit archives retain their existing lifecycle.

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


## Conversation cache and project guide

`knowledge/PROJECT.md` documents project purpose, data location, file/folder uses, and verified
metadata needed to select or interpret data. Necessary guide changes save automatically with
versioned history. Observations, hypotheses, results and research TODOs remain outside the guide.
`brief` shows it; `undo knowledge` reverts the most recent knowledge save. Legacy published guides
migrate once, leaving the old object as a backup. Unsaved thread drafts are not imported.

A hidden Markdown summary (at most 6,000 characters) is stored inside a versioned JSON record:

- DMs: `projects/_users/<user-key>/context/<project-id>.json`, shared only across that person's
  DM threads for the same project.
- Channels: `projects/<project-id>/context/channel-<hash-of-team-and-channel>.json`, shared across
  threads only in that project/channel.

Every chat and compute request receives the authorized summary separately from the guide,
personal preferences, and recent messages. It is advisory context, not scientific evidence,
permission to compute, or proof that an old file is available in the current workspace. Updates
reuse the existing background editor (`AGENT_MEMORY_AUTO_EDIT`, `AGENT_MEMORY_MODEL`), are silent,
and run only after completed conversational/compute turns. Private preference edits retain their
existing notices. Whole-summary replacements compact obsolete details instead of accumulating
notes. Conditional-write conflicts trigger up to two fresh summarization attempts; failures leave
the latest cache intact without failing the user's completed request. The record holds at most
256 crash-recovery event IDs; normal permanent deduplication uses the existing event receipts.

`memory context` shows the applicable cache; `forget context` clears it. It remains available
until cleared. Routine personal-memory edits and individual deletions preserve conversation
context. `forget context` resets the current scope; `forget all` resets every private project cache. Reset epochs block
stale background writes, resumed SDK sessions, queued requests, and worker delivery. Clearing
bypasses an earlier running request in the same thread. Stored outputs, old Slack messages, and
audit records remain; clearing controls future active context rather than deleting historical
artifacts. Routine summary updates do not reset working sessions. Uploads retain their existing
30-day lifecycle. No new database, embeddings, or infrastructure permissions are required.

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

Project knowledge remains versioned in GitHub. Each new human turn starts a fresh SDK session
with bounded recent exchanges, an 8,000-character cumulative handoff, and selected project
knowledge. The `recall_thread` tool searches paginated full exchanges from the same thread,
including failed requests; `event_id` retrieves a record in character-offset pages. Older
decisions remain retrievable after they leave the 12-exchange prompt window. The full project
guide is available in `knowledge/PROJECT.md`; its injected excerpt is bounded.

Full exchanges and final small workspace archives are retained under
`projects/_threads/<thread>/history/` and `archives/`, outside the temporary `tasks/` lifecycle.
Code, notes, text logs and small supported reports are retained. Dataset formats, array stores,
`outputs/gcs/`, inputs, scratch and caches are excluded from both checkpoints and provenance
uploads. Code/notes allow text files up to 2 MiB, logs up to 8 MiB, reports up to 200 MB.
Optional logs/reports are trimmed toward a 1 GiB total; required recovery state and retained
code/notes take priority. `logs/checkpoint-excluded.json` reports counts, bytes and bounded
examples. Published datasets are read from their bucket locations; unpublished datasets must
be regenerated. Verified publication locations also enter the worker's project-memory proposal.

Within the same request, recovery preserves the active SDK session and cost records, a consistent
SQLite backup, executed-script copies and immutable completed stages. Worker calls use the
pinned CLI's one-hour cache and 12,000-character inline Bash output setting. Full SDK tool-result
text files are copied into `logs/bash/` up to 8 MiB each. One-hour caching does not cover commands
longer than its TTL, and its cache writes cost more than five-minute writes.

Existing checkpoints are never bulk-deleted by this change. Their existing 30-day lifecycle
still applies until migration. Before expiry, an operator can stream a legacy archive into a
verified durable replacement without deleting its source:

```bash
.venv/bin/python scripts/migrate_thread_checkpoint.py --bucket ARTIFACT_BUCKET --thread THREAD_KEY
```

Migration checks the uploaded archive checksum and updates the thread pointer only if it has
not changed. A failed migration preserves the original pointer and archive. Run large migrations
as separate maintenance in GCP: gzip archives must be streamed once even when most members are
excluded. Legacy worker restore also filters datasets while streaming to avoid expanding them
onto the worker disk. No Terraform, IAM or lifecycle changes are required.

Failed requests remain available to follow-ups in the thread. Routed failures can add concise
pending work to shared project memory; cancelled turns and commands are excluded. See
[project memory](project-memory.md) for retention, attribution, and compaction behavior.

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
