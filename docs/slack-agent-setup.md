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
Enable this with `AGENT_DIRECT_CHAT_ENABLED=true`. Ordinary questions, planning, brief edits,
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
on its five-attempt retry limit. Separate threads can run concurrently. Initialization jobs
serialize project binding changes. The worker saves SQLite state, Claude transcripts and the
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
a fresh Claude context can include the visible Slack thread history.

## Existing staging resources verified

- Project: `azizilab-aml`; region: `us-central1`.
- Cloud Run: `pistachio-staging`.
- Artifact bucket: `pistachio-staging-artifacts-5304c19c`.
- Control identity: `pistachio-staging-control@azizilab-aml.iam.gserviceaccount.com`.
- Worker identity: `pistachio-staging-worker@azizilab-aml.iam.gserviceaccount.com`.
- Existing Slack bot token and signing secret; existing `PISTACHIO_SLACK_USER_MAP` remains required.
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
code, `run_log.jsonl` and `meta.json` under `projects/<slug>/runs/<thread_ts>/turn-<n>/`. Slack
uploads supported result files under 200 MB. `datasets_touched` reflects paths visible in tool
inputs; it is not a syscall audit of files opened within scripts.

Rollback: stop accepting new Claude events by restoring the previous backend after active
Claude jobs drain. Existing web chat remains unchanged. Retain Claude project objects and the
image configuration for later resumption; do not restore the blanket bucket deletion rule.

## Local development

```bash
.venv/bin/pip install -e '.[dev,slack,analysis-test]'
.venv/bin/python tests/make_fixture.py ./fixture_data
.venv/bin/python -m pistachio.agents.analysis.cli --help
.venv/bin/pytest -q
.venv/bin/ruff check src tests tools
.venv/bin/mypy src/pistachio
```

The local CLI can use `--no-sandbox` on macOS only. `pistachio-slack` is an optional local Socket
Mode entrypoint, not the deployed service. Live testing requires explicit
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

Claude drafts from `init_inventory.json` and the template using workspace-only Read/Write/Edit.
Bash, Glob, Grep, and data-mount reads are denied for this turn. Normal analysis tools return
after initialization, including after checkpoint restoration. The draft marks unknown schemas,
counts and filename-based guesses; detailed file inspection is deferred until needed.

Retrying the same project/folder binding preserves its identity and saved brief. A different
binding still needs `--force`. Spot provisioning still adds its normal cold-start latency.


## Figure validation without previews

Per the user's current preference, the compute agent does not preview generated images or PDFs.
Its Read hook denies visual files before the SDK can embed image bytes in its JSON stream.
The prompt directs it to check successful script completion, nonempty output files, computed
tables and concise logs. It must not encode image contents into tool output or claim visual
inspection. Figure generation and full-resolution Slack uploads are unchanged. This avoids
the observed SDK 1 MB JSON-buffer failure when reading a 6.5 MB spatial-map PNG.
