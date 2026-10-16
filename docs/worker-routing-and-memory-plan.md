# Simpler worker routing, automatic recovery and steadier memory: implementation plan

Written 10 October 2026 for an implementer (Codex or a developer) who doesn't have the conversation
that produced it. It targets `main` at `d864e17`; line numbers refer to that commit. It
supersedes `docs/compute-admission-fix-plan.md`, `docs/spot-vs-on-demand-decision-plan.md` and
`docs/worker-sizing-plan.md`.

Revised 10 October 2026 to retain automatic recovery when launch sizing is wrong. This is a
design with a local implementation now in this checkout (see the verification record below).
It has not been deployed. The starting checkout was `d864e17` with
uncommitted work; preserve that work. This plan also supersedes the pricing and approval design
in `docs/worker-resizing-plan.md`, while retaining its validated recovery primitives.

**Operating rule:** choose a worker that should finish the requested job at launch. If evidence
later shows that it cannot fit, automatically move the same request to a suitable larger worker
once. The agent owns sizing, saving progress and continuation. The user does not have to choose
a machine, approve a quote, plan checkpoints or resubmit the request for that recovery.

## What the lab asked for

1. **Size workers by the job, not by price.** Remove price quotes, dollar budgets and approval
   buttons. Keep the balance: Claude still picks the worker before launch from the data sizes in
   the bucket, the scope of the request and the tool it will run.
   - Plotting and ordinary analysis: Spot `standard`, as today.
   - Segmentation, ingest and model training: a bigger, longer on-demand worker.
   - If sizing proves insufficient: one automatic upward resize, with a short progress notice.
     This is a fallback, never a reason to start deliberately undersized.
2. **Conversation and memory should carry over reliably.** Today they don't (§1.2).
3. **Project context should grow with use.** The project brief becomes part of project
   knowledge and saves automatically (§3.3).
4. **Channel memory spans threads.** In a channel, conversation memory is kept per project and
   channel, so a new thread starts with what earlier threads there discussed.

Not in scope: transfers, GCS write grants, script attachments, runtimes and access modes.

## 0. Read first (rules for the implementer)

1. **Go one milestone at a time (§4).** Write failing tests first. Finish each milestone with
   `.venv/bin/pytest -q`, `.venv/bin/ruff check src tests tools` and `.venv/bin/mypy src/mica`
   all passing. Tests must not use the network or GCP.
2. **Slack-side code stays in `src/mica/agents/analysis/`** (deployment constraint). The
   only other files to touch are `api/slack_analysis.py`, `connectors/slack.py` (to remove the
   compute button hook), `tools/spatial_analysis.md`, `deploy/slack.env.example`, docs and tests.
3. **Delete, don't flag.** Remove the price and approval code paths outright. Don't leave a flag
   that restores them. Tests that only cover deleted behavior are deleted with it.
4. **Ask the human before** reading production logs or GCS, building or pushing images,
   deploying, changing Cloud Run environment variables, or running any Batch job.
5. **Don't add** embeddings, cross-project memory, a GitHub mirror by default, new approval
   steps, or routine probe/quote/resize planning. Retain only the bounded automatic recovery
   described in §3.1.1. The GitHub mirror stays exactly as it is today: opt-in, and not
   configured in staging (no `AGENT_KNOWLEDGE_GITHUB_REPO` or token secret on revision
   `pistachio-staging-router-3e87f2f9721e`). Knowledge saves only to GCS.

## 1. Background

### 1.1 Compute today

On staging, `AGENT_TIERS_ENABLED=true` and `AGENT_ON_DEMAND_ENABLED=true`. Every compute request
goes through:

- **The router's launch choice.** `direct.py` returns `WorkerChoice` (category, tier, on_demand,
  reason), and `compute_control.launch_choice` validates it. **Keep this** (the lab wants it).
- **Price admission.** `compute_batch.prepare_attempt` fetches Google's whole price catalog,
  sets a $50 request budget and a $1 probe budget, reserves model spend, and computes an
  affordable duration (`compute_batch.py:28-74`, `compute_control.py:57-108`). A catalog
  timeout here failed a request on 2026-10-10 (see `docs/compute-admission-fix-plan.md` §4).
- **On-worker quoting.** `ComputeManager` (`compute_tools.py`) limits an unapproved attempt to
  the probe window, and denies Bash timeouts over 10 minutes until the agent calls
  `estimate_worker` then `request_worker` (`compute_tools.py:249-262`).
- **Resizing.** A resize or mode switch checkpoints, hands off and relaunches
  (`compute_batch.py:146-232`, `batch_worker.py:175-243`, `:313-373`, `:445-480`), with an
  approval button when the budget is short (`compute_interactions.py`). The TMA101 switch on
  10-09 cost about 7.5 minutes.
- **Preemption.** A Spot preemption (exit 50001) fails the request with a message about the
  model budget (`compute_batch.py:224-230`).

The original plan estimated about 1,400 source lines and 1,200 test lines across pricing,
quoting, approvals and handoffs. These are not all disposable: `checkpoints.Stages`,
`workspace_snapshot`, the controlled worker interrupt and attempt identity checks support
recovery independently of prices. Remove the pricing and approval machinery; extract and
simplify those recovery primitives before deleting their old callers. Recount after extraction.

### 1.2 Why conversations lose context (verified in code)

In DMs, the thread's workspace path is derived from a stamp
(`access.private_workdir`, `access.py:181-192`):

```
sha256(grant.signature : user_memory.epoch [: conversation_epoch])
```

Before every routed message, `access.fresh_private_state` (`access.py:195-242`) compares the
saved workdir with a freshly computed one. On any difference it **retires the thread**:
- a new empty workdir,
- `session_id=None`, so the agent session is gone,
- `messages=[]`, so recent exchanges are gone,
- `thread_files=[]`, so attachments are gone.

The stamp changes far more often than intended:

| Trigger | Why it changes the stamp |
|---|---|
| The background memory editor replaces text in a profile or preferences block | `memory.epoch += 1` on `block_replace` (`user_memory.py:203`) |
| The editor or the user deletes one note (`forget 3`) | `memory.epoch += 1` (`user_memory.py:189`, `:275`). Any `forget` also bumps `context_epoch` (`:277`), which retires every DM conversation summary. |
| The user runs `init` for a new project, or is shared into one | `grant.signature` includes `doc.access_signature(email)`, a hash over **every** project the user can see (`registry.py:204-210`) |
| Anyone runs `share`/`unshare`/`publish`/`unpublish` on **any** project the user can see | `access_revision += 1` (`registry.py:180`, `:197`) |

So one automatic memory edit, or one `init`, quietly resets **every** DM thread in **every**
project for that user. The same values also abort work already in progress:
- `direct.py:484` and `:493`: "Memory changed. Send a new request to use fresh context."
- `direct.py:673`, `:721`, `:774`, and `batch.py:458-473`: "Project access changed. Send a new
  request."
- `access.worker_permitted` (`access.py:127-174`), checked before delivery
  (`slack_app.py` raises `PermissionError`), so a finished analysis is thrown away with "Sorry,
  something broke on my end."
- The knowledge-save notice is skipped when the signature changed
  (`knowledge_mirror.py:367`).

The reset was designed for an explicit `forget all`, and it is tested that way
(`tests/test_analysis_memory.py:118`). It was never meant to fire on routine edits.

### 1.3 Why project context doesn't grow

- **The brief is a thread draft until someone types `save brief`.** That includes the initial
  guide drafted by `init` (`direct.py:812` stores the draft in thread state only; only `save brief`
  calls `write_brief`, `direct.py:577`). New threads start from the last saved brief, often
  "No brief yet".
- **The brief and knowledge overlap.** Both are shared per project and both hold "data facts",
  but they behave differently. The brief is a per-thread snapshot, saved by hand, with a legacy
  cleanup review (`briefs.py`, `brief_cleanup`, `legacy_brief`, brief originals). Knowledge is
  always the latest version, auto-saved with history, `undo knowledge` and conflict handling
  (`knowledge.py`).
- **Channel conversation memory is per thread** (`conversation_memory.py:59`), so a new channel
  thread starts with no conversation memory.

### 1.4 Facts to confirm in staging first (M0)

The memory editor failed 9 of 9 logged runs between 10-06 and 10-09, before `711a15f` moved it to
its own Cloud Task. Whether it works now, how often threads are retired, and whether knowledge is
saving are all unverified.

## 2. Target design

| Concern | After this plan |
|---|---|
| Worker choice | The router picks category, tier and Spot vs on-demand from data sizes, scope and tool. Trusted `launch_choice` validates it. This choice remains in effect unless the bounded recovery policy applies. |
| Compute prices, budgets, approvals, quotes | None. The model budget per logical turn (`AGENT_MAX_BUDGET_USD_PER_TURN`) remains the same total guard across attempts; §3.1.1 reserves part of it for recovery. |
| Time limit | Every tier uses `max_run_seconds` (default 7 days). Recovery inherits the original request deadline; it does not get another seven days. Per-tier ceilings are removed. |
| Concurrency | `max_concurrent_runs` (standard) and `max_concurrent_large_runs` (large and xlarge) stay as quota guards. |
| Sizing recovery | One automatic upward resize per logical request on validated memory or scratch-capacity evidence. Resume saved work; rerun only work not safely saved. No automatic downsizing or repeated tier ladder. |
| Spot preemption | One automatic restart of the same turn, normally on on-demand at the same tier, from the latest verified recovery state. Explicit hard provisioning restrictions still apply. |
| Retry bound | One capacity resize and one preemption restart; at most three attempts total, with the same deadline, model guard and user constraints. |
| Measured history | One row per terminal attempt, including failures and recovery reasons: actual hours, longest step, peak memory and outcome. Both router and worker see recent rows. |
| Thread continuity | The workspace path is stable per user, project and thread. Only `forget context` (that scope) and `forget all` start fresh context. |
| Access checks | Every check stays, but the signature covers only **this** project: email, project, channel, private flag, this project's `access_revision`, `data_uri`, identity policy. |
| Project guide | `knowledge/PROJECT.md`. Saved immediately from init, chat or analysis, with a Slack notice and `undo knowledge`. |
| Knowledge | Always on (the `AGENT_KNOWLEDGE_ENABLED` flag is removed). The GitHub mirror stays optional. |
| Conversation memory | DMs: per user and project, as today. Channels: per project and channel. |
| Memory editor | One run per completed turn, as today, with one outcome log line per run. |

## 3. Design details

### 3.1 Job-based sizing (router)

- Keep `WorkerChoice` (`direct.py:151-155`) and `launch_choice` (`compute_control.py:20-38`,
  moved into `tiers.py` when `compute_control.py` is deleted).
- Rewrite `WORKER_POLICY` (`direct.py:140-148`) in plain terms:
  - **Category from the tool and scope.** Cellpose, Proseg or sap ingest over whole samples →
    `full_segmentation` or `full_ingest`. Fitting a model → `training`. Everything else →
    `analysis`, including plots, statistics and reading earlier outputs.
  - **Tier from data size and history.** Use `data_inventory` sizes, `worker_history` rows and
    knowledge recipes (which record the worker tier and runtime).
    - `analysis` stays `standard` unless the inputs or history show more than about 40 GiB of
      memory.
    - Heavy categories default to `large`. Use `xlarge` when the inputs are many large images
      or history shows more than about 150 GiB peak.
  - **Spot vs on-demand.** Heavy categories are on-demand, and `analysis` is Spot. Honor an
    explicit user request for on-demand, Spot, or a bigger or smaller worker.
- **Let the router look before sizing.** After an `inventory` listing the router may now choose
  `compute` as well as `reply`. Today it must reply (`direct.py:745`). This is how "assess the
  data sizes in the bucket" happens within one message. Add one policy line: for a heavy request
  whose input size isn't known from context, choose `inventory` on the input folder first.
- **The worker prompt names its machine:** tier, machine type, CPUs, RAM, Spot or on-demand,
  and the launch reason. Drop quote, compute-budget and mandatory-probe language. Replace the
  old `estimate_worker` / `request_worker` workflow with the short recovery rule in §3.1.1.
  Retain instructions to save completed expensive stages; this is the agent's responsibility.
- **Keep the per-command resource note** (`resources.Sampler.end`, sent through the PostToolUse
  hook). It tells the agent when a command came near memory limits. It moves out of
  `ComputeManager` and runs on every worker, including the tool-failure hook.
- **Validate the intended launch behavior.** The existing `launch_choice` forces `analysis`
  onto `standard` and can silently clamp to `max_tier`. Change it when moving it into `tiers.py`:
  accept a justified larger analysis tier, honor explicit initial sizing requests within policy,
  and report when a known requirement exceeds the permitted catalog instead of claiming a
  clamped worker fits. Input file bytes are not a peak-RAM estimate; include image dimensions,
  dtype, expanded arrays and tool behavior when available. No mandatory worker-side probe gate.

### 3.1.1 Automatic recovery when sizing is wrong

**Scope and limits**

Here, a mid-run resize means ending one worker attempt and continuing the same logical request
on a larger worker. It does not preserve running processes or resize the live VM. Keep category,
original request, scientific method, inputs and access scope. Never quietly reduce the analysis
to fit, and never ask the user to design a recovery plan.

The controller owns `attempt`, `resize_count`, `preemption_count`, `request_deadline`, current
tier/provisioning, original launch choice, hard user constraints and recovery state. Counts are
durable and monotonic: at most one upward capacity change and one preemption restart, hence at
most three attempts. Do not add feature flags or user-facing retry settings. A request for a
smaller initial worker is distinct from an explicit hard limit such as “standard only” or “do
not resize”; preserve hard limits during recovery. An explicit provisioning restriction also
persists. By default, a capacity recovery uses on-demand; an explicit Spot-only constraint keeps
Spot. A preemption restart keeps the current tier and switches to on-demand unless prohibited.

**Evidence and destination**

| Signal | Controller action |
|---|---|
| Live worker discovers a measured or metadata-derived memory/scratch requirement that exceeds its usable capacity | Validate evidence, save progress and request one larger attempt before starting the known-unfit step. |
| A command fails with trusted OOM evidence and the agent survives | Let the worker request recovery; it must not repeat the same unfit command until killed. |
| The worker dies with a provider OOM diagnosis or a persisted increase in its own cgroup OOM-kill counter | The controller initiates capacity recovery without needing a final tool call or final upload. |
| Batch reports Spot preemption (the existing code's exit 50001 path) | Apply the separate preemption allowance; do not infer insufficient memory. |
| Generic exit 137/SIGKILL, timeout, code error, access failure, user cancellation, or merely high sampled RAM | Do not classify as capacity failure or automatically buy a larger worker. |

Unknown failures remain unknown if no trustworthy evidence survived. Final telemetry is not
guaranteed after a hard kill; record missing samples as unknown, not zero. Disk recovery requires
evidence of exhausted usable scratch capacity; missing paths, permissions and remote quota
errors are not evidence that a larger disk will help.

Expose one narrow worker tool, `recover_worker`, accepting bounded structured evidence (failed
step or next step, memory/disk requirement if known, evidence references and a brief handoff).
The worker does not supply a price, approval, arbitrary machine type or retry count. Authenticate
the calling attempt and validate finite sizes, available tiers, cancellation, access and the
remaining deadline. The controller chooses the smallest higher permitted tier that fits the
evidence with headroom; use a named, tested 25% headroom default on estimated working memory
and scratch, after system/cache/checkpoint reserves. Check per-process memory and concurrency
as well as aggregate RAM. Skip directly from `standard` to `xlarge` when `large` cannot fit.
If all that is known is a verified OOM, allow one next-tier attempt and record that the estimate
is only a lower bound. Do not repeat that speculation or clamp a known oversized requirement
to the largest tier. If no permitted tier fits, stop with the concrete requirement and a feasible
next option, without asking the user to select infrastructure. Changes to the scientific scope
still need the user's direction.

**Saving and resuming progress**

- Retain the existing immutable `Stages` storage and `checkpoint_stage` tool, independent of
  compute quotes. The agent saves and validates completed expensive stages after writers close,
  with input versions and method/parameter fingerprints. Reuse only matching stages within this
  request; do not add cross-request caching. A command or transcript checkpoint is not a saved
  scientific result.
- Retain bounded, consistent `workspace_snapshot` and periodic partial saves (existing default
  15 minutes, partial workspace cap 256 MiB), plus a save at completed-stage boundaries. Persist
  a small trusted resource summary separately every 30 seconds and on command completion so
  the controller can use evidence that predates a kill. A failed upload retains the last verified
  snapshot. Preserve archive path, checksum, size, identity and safe-extraction checks.
- For cooperative recovery, stop admitting tools, interrupt the SDK, stop/reap running commands,
  then save closed files and a handoff with completed and remaining steps. Only publish the
  recovery pointer after its referenced objects are uploaded and verified. If this save fails,
  do not label it successful: use the prior verified checkpoint or the turn-start state.
- For an abrupt failure, use the latest verified snapshot and committed stage manifests for
  this event. Fall back to the thread state pinned at turn start if necessary. Scratch, live
  processes and unsaved outputs are lost; automatically redo work since the valid checkpoint.
  If a checkpoint is missing or corrupt, explicitly record the fallback and never trust partial
  files as completed stages. Preserve attachment references and existing runtime/script state.
- Knowledge reloads through the existing versioned commit path. A recovery checkpoint must
  preserve pending knowledge edits and their original base versions separately, so changes made
  before a resize are neither lost nor allowed to overwrite newer shared knowledge silently.

**Controller transition and delivery**

Use a small price-free recovery module alongside `batch.py`. Under the existing event lease,
persist the cause, destination and recovery pointer before scheduling a successor. Wait until
the old Batch attempt is confirmed terminal, release its slot, then acquire the destination
pool's slot. Persist successor identity and submission intent before calling Batch create; on
an uncertain response, reconcile the same job ID instead of launching another job. Use
`claude-<event>` initially and `claude-<event>-r1` / `-r2` for successors; duplicate polls, tool
calls and task deliveries cannot create extra attempts or consume another allowance.

Check current attempt, project access, conversation reset and cancellation before scheduling,
starting and publishing. A stale worker cannot publish a checkpoint pointer, knowledge change
or final result. Persist delivery state across attempts and reuse the existing publication
checks; do not redeliver already completed outputs or repeat committed writes. If a prior
non-idempotent publication has an ambiguous outcome, reconcile it before replaying that step.
Transfer/GCS write authorization and access policies are unchanged. Intermediate handoffs
are not completed turns; run memory editing and final success delivery only on completion of
the logical request. Wait for quota within the original deadline rather than starting another
smaller attempt. Cancellation terminates queued recovery too.

**Time and model guard**

Set one deadline at initial worker admission. Every replacement receives only the remaining
time, and all Bash timeouts are bounded by it. Keep the total configured model guard for the
logical turn, including resumed SDK sessions; a replacement cannot reset it. Keep a small
per-attempt model-usage ledger, separated from the deleted infrastructure-price ledger.

The old `settle` charges all remaining model allowance when a killed worker cannot report
usage. Keeping that behavior without reserving recovery capacity would disable precisely the
hard-failure fallback this plan promises. Reserve 10% of the logical turn's model allowance for
each still-possible recovery attempt (normally 20% total); the initial attempt receives the
rest. Before each invocation, persist its allowance. Settle known usage once; if usage is lost,
charge that invocation's assigned allowance, not the entire logical turn. A successor receives
the remaining amount less the reserve for any further eligible recovery. Release unused
reservations at terminal completion. This changes allocation inside the guard, not its total;
it deliberately leaves a smaller initial model allowance. Test with missing usage reports and
resumed SDK cumulative totals. Budget exhaustion ends with a clear explanation, not an approval
button or a silently renewed budget.

**User-visible behavior and learning**

Update the existing progress message once per transition. With a verified stage checkpoint:
“This step needs more memory than expected. I'm moving to a larger worker and continuing from
saved progress.” Without one: “This step needs more memory than expected. I'm restarting it
on a larger worker; work since the last save will be repeated.” For ordinary preemption:
“The Spot worker was reclaimed; restarting on an on-demand worker.” Adapt the mode wording
when an explicit Spot-only constraint keeps Spot. No approval buttons or instruction to resend.

Record every terminal attempt, including the failed original: initial and actual tier, cause,
category, tool/input fingerprint, provisioning, observed resource use, missing telemetry and
outcome. Expose related failures and successful recoveries within the authorized project to
subsequent router sizing so the same known workload starts on its demonstrated tier next time.
Measure first-attempt completion and resize frequency; repeated recovery of similar jobs
indicates a routing defect to fix.

### 3.2 Context continuity (access, memory)

- `access.private_workdir(settings, grant, thread_key)`: stop deriving the path from the
  signature and memory epoch. Use `work_root/<user_key>/<project_id>/<thread_key>`, plus
  `/context-<n>` only after an explicit context reset (see below). Existing threads keep their
  current saved `workdir`. Compare paths only when the conversation epoch changed, never by
  recomputing the old stamp.
- `fresh_private_state` retires a thread only when that scope's **conversation epoch** changed.
  The only things that change it are `forget context` and `forget all`.
- `user_memory`:
  - `block_replace` and `note_delete` stop bumping `epoch`.
  - `forget <n|profile|preferences>` stops bumping `context_epoch`.
  - Only `forget all` bumps `context_epoch`. Keep `epoch` only as the CAS guard inside
    `apply` (rename it `edit_epoch`).
  - Accepted trade-off: after forgetting a single note, earlier text may remain in that
    thread's agent transcript. Slack history shows it anyway.
- Delete the `memory_epoch` checks from `direct.py:486-494`, `:673`, `:721`, `:774`,
  `batch.py:461-464`, `access.worker_permitted:174`, `user_memory.after_turn:369-371` and
  `:420-421`, and `slack_analysis.py:249-250` and `:328`. Remove the `memory_epoch` record and
  manifest field.
- `Grant.signature` drops `doc.access_signature(email)` and uses `self.project.access_revision`
  instead. `RegistryDoc.access_signature` and its test go away.

### 3.3 The brief becomes `knowledge/PROJECT.md`

- **Paths and limits (`knowledge.py`).** Allow the path `PROJECT.md` in `ENTRY`. It needs no
  type/source/status header, and its limit is 100 KB (other Markdown entries keep
  `knowledge_file_kb`).
- **Migration (lazy, once per project).** The first time `knowledge.load` runs for a project
  with no `PROJECT.md`, if `projects/<id>/PROJECT.md` exists, commit it as `PROJECT.md` with
  `source: migrated brief`, under the project lock. Leave the old object in place as a backup.
  Unsaved per-thread drafts are not migrated; nobody else could see them.
- **Router.**
  - `brief` in the context is the current `knowledge/PROJECT.md`.
  - Rename `Decision.draft` to `guide`. When it is set, the control plane commits it right away
    through `knowledge.commit`, with base versions loaded at turn start, then posts the usual
    knowledge notice ("Updated the project guide · undo knowledge to revert").
  - Init commits the first guide the same way, so new threads see it immediately.
  - Remove `brief_cleanup`, `legacy_brief`, and the "Say save brief to share it" rule.
- **Worker.** The system prompt's "Project brief" section shows `knowledge/PROJECT.md` from the
  turn-start download. The agent edits it like any other knowledge file, and the existing
  trusted save stores it after the turn. Remove the `outputs/PROJECT.md` special handling
  (`slack_app.py:348-392`) and `Context.project_md`/`brief_generation` as a source of truth.
- **Commands.**
  - `brief` shows the guide.
  - `brief` with an attached `.md` replaces it (commit plus notice).
  - `save brief` and `clean brief` reply: "The project guide now saves automatically. Use
    `undo knowledge` to revert a change."
- **Delete.** `briefs.py` (keep its `POLICY` text and move it into the knowledge rules as the
  guide's content policy), `registry.write_brief`, brief-policy files, brief originals, and
  `access.read_state`'s legacy `turn-*/outputs/PROJECT.md` lookup. Also delete every
  `knowledge_enabled` branch (30 sites), because knowledge is always on.

### 3.4 Conversation memory scope and the editor

- `conversation_memory.key` for channels becomes
  `projects/<project_id>/context/channel-<sha256(team:channel)[:32]>.json`. DMs keep
  `projects/_users/<user>/context/<project_id>.json`.
  - Old per-thread channel summaries are not migrated.
  - Update `EDITOR_RULES` (`conversation_memory.py:38`): "Channel turns return no personal
    edits; their summary is shared by threads in that channel."
- `forget context` in a channel clears that channel's summary. Threads in that channel start
  fresh on their next message (the existing conversation-epoch mechanism, unchanged).
- **Memory editor (`user_memory.after_turn`).**
  - With the epochs gone, the only skip reasons left are "command", "not complete",
    "cancelled", "already done" and "access lost".
  - Log exactly one line per run, with no content:
    `memory_editor event=<id> outcome=<saved|unchanged|skipped:<reason>|failed:<ExceptionType>>
    summary_chars=<n> edits=<k>`. Then failures and skips can be counted in Cloud Logging.

## 4. Milestones

### M0. Baseline in staging (human-run, read-only)

After `gcloud auth login`, record these in §6:

```bash
gcloud storage ls "gs://pistachio-staging-artifacts-5304c19c/projects/_threads/*/retired/**" | wc -l
gcloud storage ls -l "gs://pistachio-staging-artifacts-5304c19c/projects/_users/*/memory.json" "gs://pistachio-staging-artifacts-5304c19c/projects/_users/*/context/*.json" "gs://pistachio-staging-artifacts-5304c19c/projects/_threads/*/context/*.json"
gcloud storage ls "gs://pistachio-staging-artifacts-5304c19c/projects/*/knowledge/**" | wc -l
gcloud logging read 'resource.labels.service_name="pistachio-staging" AND (textPayload:"Background memory editor did not complete" OR textPayload:"Memory changed" OR textPayload:"Project access changed")' --project=azizilab-aml --freshness=7d --limit=200 --format="value(timestamp,textPayload)"
gcloud compute regions describe us-central1 --project=azizilab-aml --format="table(quotas.filter(metric:N2_CPUS))"
```

The quota check matters because `max_concurrent_large_runs` × 64 vCPUs must fit the N2 quota.

### M1. Stop context resets and false aborts (ship first; §3.2)

Tests first, in `tests/test_analysis_memory.py`, `tests/test_conversation_memory.py` and
`tests/test_analysis_direct.py`:

1. After the editor applies `block_replace`, the next DM message in an existing thread keeps
   `workdir`, `session_id`, `messages` and `thread_files`, and creates no `retired/` object.
2. The same holds after `forget 3`, after `init` of another project, and after `share` of
   another project to this user.
3. `forget all` and `forget context` still retire the thread. Keep
   `test_forget_resets_transcript_draft_workspace_and_queued_worker`, minus its
   `worker_permitted`-on-memory-epoch assertion.
4. A worker whose user changed preferences mid-run still delivers its result.
5. Unsharing **this** project still denies at routing, at admission and before delivery
   (existing tests).

### M2. Size by job, remove pricing and retain automatic recovery (§3.1–3.1.1)

Tests first:

1. **Launch.** Each category, tier and on_demand combination starts the expected machine and
   provisioning. Update the table in `tests/test_analysis_launch_choice.py:24-48` for larger
   analysis requests and explicit user constraints. A known oversized requirement is not
   silently clamped. No price fetch happens: `pricing` no longer exists.
2. **Router sizing.** A router response that chooses `inventory` and then `compute` starts a
   worker in one message, and the second router call receives `data_inventory`. Related OOM
   and successful-recovery history reaches the router. Known large jobs are sized correctly
   initially, without depending on a first attempt failing.
3. **Worker.** No `estimate_worker` or old quote-driven `request_worker`. Keep `checkpoint_stage`
   and add `recover_worker`. A Bash timeout of 6 h is allowed on every tier when at least 6 h
   remain. The prompt names the machine, has no compute prices and imposes no probe gate.
4. **Cooperative resize.** Valid evidence causes a checkpoint, terminal old attempt, slot
   transfer and one larger on-demand attempt in the same event. Test a direct
   `standard` → `xlarge` choice, explicit hard restrictions and the absence of approval buttons.
   Reject arbitrary machine types, non-finite requirements and forged/stale evidence references.
5. **Hard failure.** A worker with trusted OOM evidence recovers even when its final attempt
   record and final telemetry are absent. Unknown SIGKILL/137, high RAM alone, code errors,
   timeout and cancellation do not trigger resizing. Disk evidence is validated separately.
6. **Preemption and bounds.** Exit 50001 restarts once, normally on-demand at the same tier,
   with the appropriate notice. A later OOM may consume the independent resize allowance;
   repeated OOM or preemption cannot consume an allowance twice. Combined causes do not launch
   duplicate successors. At most three attempts ever exist for the request.
7. **Recovery correctness.** Validated saved stages survive; unfinished stages rerun. Exercise
   missing, corrupt, oversized and foreign checkpoints, failed handoff upload, stale worker
   writes, missing intermediate outputs and preservation of pending knowledge edits. Messages
   distinguish resumed progress from restarted work. Runtime/script/attachment state survives.
8. **Controller races.** Duplicate polling, repeated recovery calls, uncertain Batch-create
   responses and cancellation while waiting for a large slot create no extra jobs. No successor
   runs before its predecessor is terminal. Recheck project access and conversation resets.
   Verify delivery markers prevent redelivery and memory editing runs once at logical completion.
9. **Limits.** The remaining deadline and model allowance carry across all attempts. Test lost
   usage charged against the persisted invocation allowance, reserves left for recovery and
   resumed SDK cumulative costs counted once. No available tier, an exhausted allowance, or a
   deadline produces a specific final explanation; no generic request to plan a bigger machine.
10. **History.** Each terminal attempt writes one idempotent row (category, input/tool identity,
    tier, provisioning, outcome, recovery cause, hours, longest step, peak GiB). Missing final
    metrics use verified partial telemetry or stay unknown. Rows in the old format are skipped
    without error; a failed original is retained even when its successor succeeds.
11. **In-flight records at deploy.** An `awaiting_approval` record ends with "This request
   expired during an update; please send it again." A running record with `compute_version`
   may complete through the normal legacy result reader, without importing deleted pricing
   modules. Old handoff-only records must not be treated as completed analysis or silently
   reinterpreted as authorized recovery. The rollout drains old work before M2; test an unexpected
   remaining legacy handoff ends with the explicit update-expiry explanation. This migration
   exception is not the behavior of new requests.

Changes:

- **Extract before deleting.** Move bounded attempt transitions and model accounting into
  `recovery.py`, and the worker recovery tools and controlled interrupt into `recovery_tools.py`,
  both inside `src/mica/agents/analysis/`. Move price-independent shared types such as
  `Provisioning` and record validation bases out of `compute_quotes.py`. Move `event_prefix`
  to the recovery module so checkpoints do not depend on a deleted `compute_batch.py`.
- **Delete** `pricing.py`, `compute_quotes.py`, `compute_control.py`, `compute_batch.py`,
  `compute_interactions.py` and the old `compute_tools.py` after extraction. Remove the Slack
  compute-button hook, infrastructure quotes/ledgers and only tests/fixtures exclusive to them.
  Preserve and adapt recovery, checkpoint validation, accounting and resource tests rather than
  deleting `test_analysis_compute*.py` wholesale.
- **Keep and simplify** `checkpoints.Stages`, `workspace_snapshot`, restore and attempt-result
  validation. Decouple their storage limits from price policy. `tools/measure.py` need not be
  required in the prompt; measurement and closed-stage saves still remain useful worker tools.
- **Move and update** `launch_choice` in `tiers.py`, and `exit_code` in `batch.py`. Add a trusted
  failure classifier that distinguishes known OOM/preemption from unknown task death.
- **`batch.py`.** In `advance`, after `direct.handle` returns `False`, apply `record["launch"]`
  directly for attempt zero: tier, provisioning and slot pool. The recovery module owns any
  successor launch. `job_spec` keeps explicit attempt identity, remaining duration and model
  allowance; remove infrastructure-budget and price-dependent arguments, not recovery metadata.
- **`batch_worker.py`.** Always start `Sampler`, write `resources-a<N>.json` and
  `attempt-a<N>.json`, including the knowledge record expected by the notice reader. Use the same
  worker execution path for initial and resumed attempts, with a bounded resume manifest.
  Keep partial checkpoints and cooperative handoff; neither reports logical completion.
- **`runner.py`.** Resource hooks run on success and failure regardless of recovery. Retain a
  minimal SDK-client interrupt wrapper for cooperative handoff; plain `query()` alone does not
  replace that contract. Preserve correct resumed-session cost baselines in the new model ledger.
- **`calibration.py`.** `Row` drops price estimates and adds attempt/recovery identity plus
  `outcome: Literal["complete","failed","oom","preempted","resized"]`. Capture idempotently
  in the terminal branch of `batch.advance`; a successful handoff is `resized`, not `complete`.
- **`config.py`.** Remove `tiers_enabled`, `on_demand_enabled`, `on_demand_step_minutes`,
  `on_demand_total_hours`, `compute_quote_only`, `compute_auto_budget_usd`,
  `compute_probe_budget_usd`, `compute_probe_seconds` and `max_preemption_relaunches`.
  The two recovery counts become fixed controller policy. Keep `launch_heavy_tier`, `max_tier`,
  concurrency guards and `partial_checkpoint_seconds`. Stage checkpoint byte caps were
  subsequently retired to support large datasets. Move authoritative tier/attempt into the worker manifest;
  remove the old settings only after all readers use that manifest. Add no pricing flags.
- **`api/slack_analysis.py`.** Replace authenticated quote/request operations with the narrow
  recovery request schema. Retain worker identity, attempt, access and cancellation checks;
  ordinary Slack text cannot forge a recovery transition. Keep existing runtime APIs intact.
- **`tools/spatial_analysis.md:86-98`.** Replace pricing and approval text with: "Your worker
  was sized for this job at launch. Save completed expensive stages as you go. If measured
  requirements show the next step will not fit, use `recover_worker` to continue the same
  request on a suitable larger worker. Recovery is automatic and limited to one upward resize;
  do not deliberately start small or ask the user to resubmit. Unsaved work may need repeating."

Land extraction, pricing removal and the bounded recovery together for M2. Do not ship an
intermediate release that deletes checkpoints and asks users to recover sizing mistakes.

### M3. The brief becomes project knowledge (§3.3)

Tests first:
1. Init commits `knowledge/PROJECT.md`, and a brand-new thread's router context and worker
   prompt contain it with no `save brief`.
2. A chat correction ("sample B is in raw/B2/") produces a `guide`. It is committed, a notice
   is posted, and `undo knowledge` restores the previous guide.
3. A project with only `projects/<id>/PROJECT.md` migrates on first load, exactly once, even with
   two concurrent loads.
4. A worker edit to `knowledge/PROJECT.md` saves through the normal knowledge save. An edit to
   `outputs/PROJECT.md` is just an output file.
5. `save brief` and `clean brief` return the new explanation. `brief` shows the current guide.
6. No `knowledge_enabled` references remain: `grep -rn knowledge_enabled src` is empty.

### M4. Channel-wide conversation memory and an observable editor (§3.4)

Tests first:
1. Two threads in the same published channel: after thread 1's editor saves a summary, thread 2's
   router context and worker manifest carry it.
2. A DM summary still never appears in a channel, and a channel summary never appears in a
   DM.
3. `forget context` in a channel thread clears the channel summary, and another thread in that
   channel starts fresh on its next message.
4. Each editor run logs exactly one `memory_editor` line with the right outcome, including
   `failed:<Type>` when the model call raises. The task still raises, so Cloud Tasks retries.

### M5. Router and help cleanup

- **`direct.SYSTEM`.**
  - Remove the legacy-brief and cleanup paragraphs (`direct.py:103-122`).
  - Replace "draft" wording with the immediate-save guide rule.
  - Keep the reply/inventory/view/compute/transfer descriptions.
  - Snapshot the final prompt in `tests/fixtures/compute/prompts-before-launch.json`.
- **`help_text.COMMAND_HELP`.**
  - Drop `save brief` and `clean brief`.
  - Change `forget <…>` to say only `forget all` and `forget context` start fresh context.
  - Note that channel memory is shared by the channel's threads.
- **`deploy/slack.env.example`.** Remove the deleted variables and add a comment that
  leftover ones are ignored (`extra="ignore"`).
- **Docs.** Mark `docs/project-knowledge.md` and `docs/return-scripts.md` sections that mention
  brief drafts as superseded. Mark the old pricing/approval portions of
  `docs/worker-resizing-plan.md` superseded by this plan, preserving historical deployment facts.

## 5. Rollout (each step needs the human's approval)

1. Drain active work before M1 too: its project-scoped access signature differs from older
   worker manifests. Deploy M1 alone to staging. Run the §6 reset check again after a day of normal use. The
   `retired/` count should only grow from `forget context` or `forget all`.
2. Deploy M2–M5 together when `gcloud batch jobs list --filter="labels.pistachio-agent=claude
   AND status.state:(QUEUED OR SCHEDULED OR RUNNING)"` is empty.
3. Remove these Cloud Run variables: `AGENT_TIERS_ENABLED`, `AGENT_ON_DEMAND_ENABLED`,
   `AGENT_COMPUTE_QUOTE_ONLY`, `AGENT_COMPUTE_AUTO_BUDGET_USD`, `AGENT_ON_DEMAND_STEP_MINUTES`,
   `AGENT_ON_DEMAND_TOTAL_HOURS`, `AGENT_COMPUTE_PROBE_BUDGET_USD`,
   `AGENT_COMPUTE_PROBE_SECONDS`, `AGENT_MAX_PREEMPTION_RELAUNCHES`,
   `AGENT_TIER`, `AGENT_ATTEMPT`, `AGENT_KNOWLEDGE_ENABLED`. Leftovers are ignored.
4. Staging checks in a DM and in a published channel:
   - A plot request lands on Spot `standard`.
   - "Run Proseg on the full TMA101 sample" lists inputs, then starts on-demand `large`, then
     cancel it.
   - Two follow-ups in the same thread keep the earlier conversation, even after a `remember`
     that rewrites a preference.
   - `init` of a throwaway project doesn't reset other threads.
   - A guide correction in chat is visible from a new thread.
   - A second channel thread sees the first thread's summary.
   - An approved bounded recovery smoke test saves a small completed stage, requests a larger
     worker and continues the same Slack request with one notice and one final delivery.
   - An approved controlled failure test supplies trusted OOM evidence with no final worker
     report; the controller restarts automatically and identifies any work that must repeat.
     Keep ambiguous SIGKILL, duplicate polls, budget-reservation and race tests in local fakes.
   - Cancel during recovery and confirm no successor runs. Verify a recovered request retains
     its original deadline and model guard, and the next equivalent request sees its history.

## 6. Baseline and results (fill in)

| Check | Before (M0) | After M1 | After M5 |
|---|---|---|---|
| `retired/` thread states | | | |
| Memory files (user / DM context / channel context) | | | |
| Knowledge files | | | |
| Editor outcomes in 7 days (saved / unchanged / skipped / failed) | | | |
| "Memory changed" / "Project access changed" replies in 7 days | | | |
| N2 CPU quota (us-central1) | | | |
| First-attempt completion / automatic resizes / successful recoveries | | | |
| Preemption restarts / exhausted recovery allowances / unknown worker deaths | | | |

## 7. Local implementation and verification — 10 October 2026

M1–M5 are implemented in the existing checkout, preserving the pre-existing work. No cloud
logs or GCS were read, no images were built or pushed, and no deployment or Batch job was run.
The staging observations in the earlier baseline section are supplied historical context,
not measurements repeated during this implementation. The live comparison table above remains
unfilled until an authorized staging evaluation.

| Milestone | Local full-suite result |
| --- | --- |
| M1: stable context and project-scoped access checks | 908 passed, 9 skipped |
| M2: job-based launch and bounded automatic recovery | 846 passed, 9 skipped |
| M3: shared guide, migration, automatic saves and undo | 844 passed, 9 skipped |
| M4: channel-wide summaries and editor outcome logging | 851 passed, 9 skipped |
| M5: prompts/help/docs and final recovery edge cases | 856 passed, 9 skipped |

Final checks: `.venv/bin/pytest -q` (54.89 seconds), `.venv/bin/ruff check src tests tools`,
`.venv/bin/mypy src/mica`, and `git diff --check` pass. Test-count reductions remove obsolete
pricing, approval, disabled-knowledge and draft-publication behavior; recovery and isolation tests
remain and were extended. The final suite includes cold resize with one final delivery, verified
stage reuse, corrupt-stage recomputation, pending guide edits with their original CAS versions,
unknown model-usage reservation, reported usage overages, original deadlines during queueing and
interrupted submission, cancellation, stale attempts, duplicate polls, and context resets.

The final router/schema/worker snapshot is `tests/fixtures/compute/prompts-routing-memory.json`.
Old price-only files/fixtures were backed up locally before removal. Existing saved workdirs
remain valid; the published guide migrates lazily with the original object retained. Old unsaved
thread drafts and old per-thread channel summaries are not imported into shared context.

Recovery remains intentionally bounded: one upward resize and one preemption restart, at most
three attempts, one original deadline and one total model allowance. Trusted persisted OOM
telemetry permits capacity recovery; an unexplained exit 137 does not. A kill before telemetry
is persisted can therefore remain an unknown failure. Final-publication uncertainty stops replay.
These limits, real worker OOM/preemption evidence, and cloud identity boundaries still need the
staging checks in §5 before rollout.

Before pushing to `main`, the implementation was isolated from unrelated local changes and
verified again: 751 tests passed, 6 skipped; Ruff, mypy (116 source files), and `git diff --check`
passed. The lower count excludes unrelated tests present in the original working checkout.
The router's automatic tool-selection compatibility fix and its tests are included because
the configured model does not support forced tool selection. No deployment was performed.
