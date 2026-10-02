# Slack-first agent rollout inside Pistachio

The Claude agent replaces Slack's agent layer inside the existing Pistachio app. The browser
continues to use its current agent. Nothing is moved into `legacy/`.

The original plan describes a dedicated VM and Socket Mode. The user's subsequent direction
supersedes that deployment choice: retain the existing Slack bot, HTTP webhook, Cloud Run
service, Cloud Tasks queue, and GCP Batch Spot resources. No new Slack channel, Socket Mode
token, always-on VM, or Modal account is required.

## What runs where

`Slack DM → signed webhook → Cloud Tasks → Cloud Run conversation → Slack reply`

Only requests that need data inspection, code execution, or plots proceed to a Batch Spot job.
Conversation routing is always enabled. Ordinary questions, planning, brief edits,
`help`, `brief`, `save brief`, `cancel`, and bounded initial setup run in the control service.
The direct Claude call has no shell, filesystem, or network tools. It can answer, revise a draft,
or request isolated computation. API failures produce a visible retry message, never a worker
fallback. Cancellation bypasses queued work and stops the active job without starting a new one.

The new worker calls the Claude Agent SDK directly. It does not invoke Pistachio's web planners,
TaskContract generation, approval stages, or multi-stage execution pipeline. Each Slack thread
has one resumed Claude compute session, a workspace, and its own PROJECT.md brief snapshot.
Chat corrections and verified discoveries can revise that thread’s draft as work proceeds. The worker uses
built-in Read/Write/Edit/Glob/Grep/Bash tools and the pinned science environment.

A control-service reply acknowledges each accepted input. Direct replies update it in place;
computation requests update it before any Spot startup. That same
Slack message reports queue waits, startup elapsed time, worker readiness, and completion/errors.
During analysis it shows tool activity and a heartbeat every 30 seconds. Status failures do not
prevent work, and repeated webhook/poll deliveries reuse the stored message timestamp.

Firestore receipts deduplicate Slack delivery, order accepted messages within a thread, and
limit active Claude jobs to three. Named polling tasks use the existing queue without depending
on its five-attempt retry limit. Separate threads can run concurrently. Registry writes use generation checks to serialize project binding changes. The worker saves SQLite state, Claude transcripts and the
workspace to the existing artifact bucket after a turn; the next Spot job restores the same
absolute paths. Direct chat updates the small checkpoint record with the revised brief and
recent conversation without downloading the workspace archive. The next compute worker overlays
that record on its restored workspace; compute replies update the same conversation record.
A small durable record preserves the current thread brief and turn number after the
workspace expires. Cold starts add Batch provisioning latency to each message.

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
- Existing Slack bot token and signing secret. The old `PISTACHIO_SLACK_USER_MAP` is used only by the legacy mapped mode and migration.
- Existing Batch worker network/subnetwork: `default`; private workers have no external IPs.

Deployment verification found existing `nat-router` / `nat-config` covering all subnets on
`default`. The rollout reuses it; no NAT resource is needed. Keep `slack_agent_nat_enabled=false`.
Generated Bash remains network-isolated; only the trusted worker/Claude runtime uses outbound
access to Slack, Claude and Google APIs.

## Prepared code and rollout controls

- `src/pistachio/agents/analysis/`: single-session agent, briefs, provenance and Spot adapter.
- `docker/Dockerfile.slack-agent`: extends the existing pinned science image with the SDK,
  `socat`, and metadata inspection tools. The web worker keeps its image.
- `cloudbuild/build-slack-agent.yaml`: builds the new image only.
- `infra/slack_agent.tf` and optional variables: project-prefix write access, selected data bucket
  readers and optional NAT. No runtime Secret Manager permission is added to the worker.
- `PISTACHIO_SLACK_AGENT_BACKEND=existing` is the default. Set `claude_batch` only at cutover.
- `PISTACHIO_SLACK_AGENT_IMAGE` must be a digest-pinned image.

The existing worker identity already has project-wide objectAdmin access from Pistachio's current
infrastructure (verified during the follow-up rollout). This change reuses it rather than claiming the plan's dedicated least-privilege
identity. Only configured buckets are mounted, mounts are read-only, and tools are restricted
to the current project's data path. The current staging deployment has already opted into the
outer container profile exception for nested sandboxing; the new worker honors that existing
staging/test-only flag. The inner Claude sandbox must pass its live gate and is never disabled
for Slack.

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

   Resolve the resulting worker image digest. Build/deploy the updated API through Pistachio's
   existing release path, initially with `PISTACHIO_SLACK_AGENT_BACKEND=existing`.
4. Prepare a Terraform plan against the **existing state**, preserving all existing values:
   `slack_agent_image`, `slack_agent_data_buckets`, `slack_agent_anthropic_secret`, `slack_agent_data_project`, and optionally
   `slack_agent_nat_enabled=true`. Leave `slack_agent_enabled=false` while preparing. Review
   any unrelated drift before applying. Do not recreate the app, bucket or worker account.
   Terraform is not installed in the implementation environment, so validate this plan before use.
   The current rollout uses scoped gcloud updates; see `slack-agent-deployment.md` for live state.
5. Review the artifact bucket lifecycle change: existing task/input/incoming/paused prefixes
   still expire after 30 days, including workspace archives. New `projects/` objects preserve
   briefs, provenance and frozen thread metadata. This increases retention/storage use. Keep
   the image configured during a rollback so the old blanket lifecycle rule is not restored.
6. Check existing Slack scopes and subscriptions against `deploy/slack-app-manifest.yaml`.
   Retain `/integrations/slack/events` and `message.im`. Add file/history/user scopes only if
   missing; reinstall the existing app if scopes change. Channel mentions are optional; the
   existing bot DM is sufficient. Do not enable Socket Mode or import a new app.
7. Run the Linux isolation and live fixture checks in `docs/acceptance.md` with the pinned worker
   image and real private data mounts. Then cut over Slack only using
   `PISTACHIO_SLACK_AGENT_BACKEND=claude_batch` (Terraform: `slack_agent_enabled=true`).
8. In the existing bot DM, send `init project-slug gs://bucket/prefix/`, review its draft, then
   reply `save brief`. A mention is unnecessary in the DM. New top-level messages start threads;
   reply in the bot's thread to continue an analysis. In a channel, mention the bot on the initial
   message and commands. Reply with missing details or corrections in ordinary language to revise
   the draft; no Markdown upload is needed. This also works later in an analysis thread.
   `save brief` publishes the revised draft for new threads. Uploading a UTF-8 `.md` file
   under 100 KB with `brief` remains an optional way to replace the shared brief.

The local implementation was followed by the user-authorized deployment recorded in
`slack-agent-deployment.md`. That record is the source of truth for current deployment status.

## Settings and use

See `deploy/slack.env.example`. Defaults are 40 model turns, $5 per message and three concurrent
jobs. Each job requests 8 CPUs / 64 GiB on Spot, a 500 GB boot disk, up to one hour of runtime,
and no task retry. The controller cancels jobs still unresolved after two hours. File caching
uses at most 400,000 MiB across mounted buckets, with range-read caching enabled. Validate the
Batch image's gcsfuse flags and available disk space during the canary.

Commands: `help`, `init <slug> gs://bucket/prefix/ [--force]`, `brief`, and `save brief` in a draft
thread, including an analysis thread where the agent has revised the brief. The agent uses
local draft revisions in subsequent turns, while other existing threads keep their snapshots.
New threads receive revisions after `save brief`. Unknowns can remain TODOs; user-provided
information and verified findings should be distinguished from guesses. Rebinding does not
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
.venv/bin/python -m pistachio.agents.analysis.cli --help
.venv/bin/pytest -q
.venv/bin/ruff check src tests tools
.venv/bin/mypy src/pistachio
```

The local CLI can use `--no-sandbox` on macOS only. Slack commands use the HTTP control plane;
the Socket Mode entrypoint has been removed. Live testing requires explicit
`PISTACHIO_RUN_LIVE_TESTS=1` and an API key. See the
[Claude SDK reference](https://code.claude.com/docs/en/agent-sdk/python),
[Batch volume reference](https://docs.cloud.google.com/batch/docs/reference/rest/v1/projects.locations.jobs#Volume),
and [gcsfuse flags](https://docs.cloud.google.com/storage/docs/cloud-storage-fuse/cli-options).


## Bounded initialization

`init` prepares a metadata-only sample through the trusted Google Cloud Storage API: at most
4 one-page listings, 80 entries per page, 200 retained entries, 2 levels below the requested
folder, and a 15-second listing budget with each request capped at 5 seconds and no retries.
Zarr/N5 stores are opaque entries; hidden/output folders are skipped. An unavailable/partial
listing still produces an explicitly incomplete draft rather than starting an unbounded scan.

The control-plane model drafts from the bounded inventory without tools or a Spot worker.
Normal data inspection is a separate computation request. The draft marks unknown schemas,
counts and filename-based guesses; detailed file inspection is deferred until needed.

Retrying the same project/folder binding preserves its identity and saved brief. A different
DM binding at init still needs `--force`; `use` switches the default for new threads.
Spot provisioning latency applies only to subsequent computation.


## Figure validation without previews

Per the user's current preference, the compute agent does not preview generated images or PDFs.
Its Read hook denies visual files before the SDK can embed image bytes in its JSON stream.
The prompt directs it to check successful script completion, nonempty output files, computed
tables and concise logs. It must not encode image contents into tool output or claim visual
inspection. Figure generation and full-resolution Slack uploads are unchanged. This avoids
the observed SDK 1 MB JSON-buffer failure when reading a 6.5 MB spatial-map PNG.


## Identity, projects, and private memory

The default `AGENT_ACCESS_MODE=channel` admits members of **any enabled access channel** in the
bot's workspace, including when they use the bot in DMs. Enabled channels are stored atomically
in `projects/_access/channels.json`. With no enabled channels, ordinary agent access is denied.
Membership does not automatically share projects or publish their contents into a channel.

The bot obtains each human member's normalized email from Slack. That retains the existing
per-email project/memory identity, provided the previous manual mapping matched their Slack email.
A bot, deleted user, wrong-workspace request, or missing readable email is denied. Review mismatched
legacy mappings during migration; never automatically merge different identities. In channel mode,
`PISTACHIO_SLACK_USER_MAP`, `PISTACHIO_ALLOWED_EMAILS`, and `AGENT_ALLOWED_USER_IDS` do not restrict
Slack admission. The browser keeps its own existing email allowlist and authentication.
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

### Onboard additional channels from Slack

One-time setup, included in the next approved deployment:

1. Set `AGENT_ACCESS_MODE=channel` and configure the people allowed to manage access channels in
   `AGENT_ACCESS_ADMIN_IDS`, as comma-separated `workspace:member` Slack IDs. Terraform exposes
   `slack_agent_access_mode` and `slack_agent_access_admin_ids`. This small admin list controls
   onboarding only; it is not the list of people allowed to use the agent. Admin status does not
   grant access to other people's projects or bypass channel membership for ordinary requests.
2. Add `channels:read`, `groups:read`, and `users:read.email` alongside existing `users:read`,
   `app_mentions:read`, and chat permissions; subscribe to `app_mention`. Apply the delta in
   `deploy/slack-app-manifest.yaml` to the existing app and reinstall if Slack requires it.
   The existing signed HTTP webhook is retained. No Socket Mode or new bot is needed.
3. Invite Pistachio to a channel. A designated admin sends `@Pistachio enable this channel`.
   The bot checks workspace identity, channel visibility, membership-read and email permissions,
   then records approval. This works for public and private channels. Inviting the bot alone does
   not authorize access, and an ordinary member cannot enable a new channel.
4. Repeat the invitation and command in each additional access channel. There is no channel-ID
   configuration or redeployment per channel. Members can then initialize their own projects
   in DMs. Use explicit project `publish` and `use` commands when a project should reply in a
   channel; enabling an access channel does not expose a project there.
5. An admin can send `@Pistachio disable this channel` to remove that channel from admission,
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
Each thread retains its selected project even after `use` changes the default. Existing threads
keep their brief snapshots. `save brief` and an optional `.md` upload are explicit publication
actions to project members and authorized channel audiences. A stale save is rejected if another
thread changed the shared brief; start a new thread to review and reconcile it.

| Command | Where and effect |
| --- | --- |
| `init liver gs://bucket/folder/ [--force]` | DM: create/reuse your project and draft its brief. |
| `projects` | DM: list owned and explicitly shared projects only. |
| `use liver` or `use p_…` | Select a project for new threads. Ambiguous shared names require an ID. |
| `share liver @user` / `unshare liver @user` | Owner in DM: grant an enabled user or revoke a former member; email links also work. |
| `publish liver #channel` / `unpublish liver #channel` | Owner in DM: authorize/withdraw replies visible to the entire channel audience. |
| `use p_… [--force]` in a channel | Owner selects an explicitly published project; replacing a binding requires its current owner too. |
| `brief` / `save brief` | View this draft / publish it, after an access check. |
| `cancel` | Cancel queued/active computation after an access check. |
| `memory` | DM: show private profile/preferences and the newest 20 numbered notes. |
| `remember <text>` | DM: add a private note (up to 600 characters). |
| `forget <number\|profile\|preferences\|all>` | DM: delete the selected memory and invalidate previous private context. |

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
in the initial brief. Applying memory to a later draft requires the user’s request; publishing
that draft with `save brief` deliberately shares its contents.

Profile and preferences are each bounded to 1,500 characters. Up to 200 notes have stable numbers;
retrieval selects at most five within 2,500 characters, using keyword overlap and recency. Atomic
writes prevent lost concurrent updates. Permanent operation receipts prevent delayed deliveries
from resurrecting forgotten content. The post-reply editor proposes at most three changes, rejects
credential-like text, and sends a confirmation only after saving. It is best effort with a
30-second bound and cannot fail the main reply. Set `AGENT_MEMORY_AUTO_EDIT=false` to disable it;
explicit memory commands still work. `AGENT_MEMORY_MODEL` defaults to `AGENT_MODEL`.

Forgetting, replacing memory, or changes to accessible project grants invalidate private context.
The next turn starts a new workspace and SDK session, clears the recent transcript/private draft,
and reloads the shared brief. Retired small checkpoints are kept as audit records, and run
numbers continue so old outputs are not overwritten. The first private turn after upgrading a
legacy checkpoint also starts fresh; an unsaved old draft is retained in the retired checkpoint
for reviewed recovery, not injected automatically. This also blocks old queued memory snapshots. Access revisions
ensure revoke-then-regrant does not revive an old grant. Forgotten content is removed from future
retrieval; this is not deletion of historical Slack messages, explicitly saved shared briefs,
old outputs, or retained audit archives. Registry/memory receipts and private outputs are under
the durable `projects/` prefix; include them in retention and backup planning.

DM outputs live at `projects/_users/<user-hash>/runs/<project-id>/<thread-key>/turn-<n>/`.
Authorized channel outputs retain `projects/<project-id>/runs/<thread-ts>/turn-<n>/`.
The agent’s filesystem sandbox cannot browse another run or the user-memory store. These are
application isolation boundaries: the trusted service accounts still have the existing bucket
permissions. Lab-wide data-bucket access remains intentional; this is not per-user data IAM.

## Private-project migration and cutover (approval required)

These changes are prepared locally. The deployment history document describes the older live
revision; it is not evidence that this migration has run. Ask before every cloud build, migration,
or deployment, as required by the implementation plan.

1. Confirm onboarding admins, membership/email scopes, legacy identity mappings, and the immutable base web image. Build the Slack API and
   worker from the same commit using the existing layered build files. The Slack-only API copies
   `agents/analysis/` and `api/slack_analysis.py`; the independent browser fixes ship only with
   a subsequent full web release. Keep the three existing Slack integration patches unchanged.
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
   for admin onboarding. Enable the initial access channels using the commands above; ordinary
   requests remain denied until then. Verify one user with overlapping memberships, removal from
   the last enabled channel, denied non-admin onboarding, and private project/memory boundaries.
   New API code refuses unmigrated projects; new workers refuse incompatible old manifests.
6. Roll back only to a known ACL-compatible pair after draining. If none exists, keep Slack
   ingress disabled until repaired. Never route private-project traffic to the old slug backend.

For local rehearsal, replace `--bucket` with `--local-root /tmp/pistachio-migration-fixture`.
Keep owner maps, real configuration, Terraform state, and credentials out of Git. On another
laptop, clone the repository, select the implementation branch, recreate the virtualenv and local
configuration, and authenticate to the existing cloud resources. The deployed runtime and its
memory are in GCP; the laptop does not need to remain running.
