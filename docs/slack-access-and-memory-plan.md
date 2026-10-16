# Plan: Private projects, explicit sharing, and Letta‑lite user memory for Mica's Slack agent

> Source behavior update, 10 October 2026: [the routing and memory implementation plan](worker-routing-and-memory-plan.md) supersedes the pricing/quote gates, thread-draft guide flow, knowledge feature flag, and per-thread channel summaries described below. Those passages remain historical design/deployment evidence. This local implementation has not been deployed or validated in staging.

> Historical record: the web UI and browser login have since been removed from source.
> Web rollout/preservation instructions below are superseded by the current
> [headless deployment guide](deployment.md#headless-api-rollout).

> Original implementation plan, followed with the approved corrections below. Local implementation
> is complete; cloud build, migration, deployment, and live checks remain approval-gated.

**Later approved admission model:** membership in any admin-enabled Slack channel replaces the
manual Slack user/email allowlists. Admins onboard channels by inviting Mica and mentioning
`enable this channel`; `disable this channel` removes an access channel. Identity comes from Slack,
with the same normalized-email memory keys. Project ACLs and private memory remain independent.
See the current setup guide for the short membership cache, required scopes, and bootstrap admins.
The original mapped-user details below now describe only the explicit legacy mapped mode.

**Approved corrections implemented:** explicit owner channel publication separate from membership;
private DM output prefixes; init memory suggestions outside the shared draft; context invalidation
on forgetting/access changes; access-filtered memory-editor inputs; permanent replay receipts;
generation-checked brief saves; quiesced migration and ACL-compatible rollback only; fresh string
web event IDs (never `None`); preserve the existing Cloud Tasks `get_principal` import.
See [the current setup and cutover guide](slack-agent-setup.md) for the executable workflow.
The original design details below are retained as provenance; these corrections take precedence.

**Summary.** Every Slack message gets resolved to a user, and that user's access to the thread's project is checked before anything is read. Projects get opaque IDs, an owner, and invited members, so a project's name is just a label and its `PROJECT.md` can't be inherited by someone else. Each user gets a small private memory modeled on Letta: two always-loaded blocks plus a searchable notes archive. The agent edits it after DM turns and announces each change. The browser gets four small isolation fixes.

---

## 0. Read first

1. **Where Slack code is allowed to live (deployment constraint).** `docker/Dockerfile.api-slack` builds the deployed API image on top of the frozen, already-deployed web image. It copies only `src/mica/agents/analysis/` and `src/mica/api/slack_analysis.py`. `cloudbuild/build-slack-api.yaml` then runs `scripts/check_slack_preservation.py`, which fails if any other deployed file changed. So:
   - Put every new Slack-side module in `src/mica/agents/analysis/`.
   - Add no new imports of web modules (`mica.services.*`, `mica.agents.{memory,context,harness,session}`, `mica.api.auth`). In `receive()`, replace the lazy `from mica.connectors.slack import _reply` with a direct `AsyncWebClient` call, the way `BatchBridge.notify` already does (`batch.py:140`).
   - From the web app, use only `Repository.get/set_connector_state`, `begin_event`, `release_event`, and settings the Slack code already reads (`slack_user_map`, `gcs_bucket`, `slack_bot_token`, …). Read `allowed_emails` as `getattr(app, "allowed_emails", ())`.
   - New settings go in `agents/analysis/config.py` as `AGENT_*` fields. Add no new third-party dependencies.
   - The worker image (`docker/Dockerfile.slack-agent`) copies all of `src/`. Always build the API and worker images from the same commit.
2. **The browser fixes (Part B, §8) edit web files.** They take effect only at the next full web deploy, not with the Slack build. Slack code must never depend on them.
3. **Each milestone ends green:** `.venv/bin/pytest -q`, `.venv/bin/ruff check src tests tools`, `.venv/bin/mypy src/mica`.
4. **Fail closed.** If the identity, project, membership, or registry is missing, refuse with a short message. Never loosen the runner's tool guard or sandbox.
5. **Ask the human before any cloud action:** migration `--apply`, Cloud Run environment changes, image builds, deploys.
6. **The repo has no commits yet.** Ask the user to commit the current tree as a baseline before M0, so each milestone is a reviewable diff.

---

## 1. Context

### Problems (verified in code)

- **Identity is checked only on arrival.**
  - `receive()` (`api/slack_analysis.py:38-43`) only checks that the Slack user appears in `slack_user_map`, then discards the email.
  - `allowed_emails` is never applied to Slack.
  - The check runs before the channel/thread filter (`:47-56`). So unmapped users get replies to ordinary channel chatter, and two replies when they mention the bot.
- **Projects are found by channel alone** (`direct.py:168`, `slack_app.py:122`, `batch.py:194-197`). Briefs and runs are stored under a global slug (`projects/{slug}/…`). `created_by` is recorded but never read.
- **The reproduced leak.** A `--force` rebind clears the old project's channel (`projects.py:76-77`). Anyone mapped can then `init <same slug>` in another chat and inherit its `PROJECT.md` and `runs/`.
- **No one is checked for `save brief`, `.md` upload, or `cancel`** (`direct.py:194-253`, `slack_app.py:149-173` and `283-312`, `batch.py:496-556`). In a channel, any mapped user can continue a tracked thread.
- **There is no user-level memory.**
- **Browser problems:**
  - Legacy-planner sessions are keyed by `conversation_id` alone (`agents/harness.py:404-412`, `450`).
  - Web clients can set `conversation_id`, `source`, and `external_event_id` (`api/routes.py:181-192`).
  - `auth_mode` defaults to `dev` with no environment guard (`config.py:26`).

### Outcome: three scopes behind one access check

| Scope | Holds | Stored at | Read/write | In the model's context |
|---|---|---|---|---|
| User | `profile` and `preferences` blocks, notes archive | `projects/_users/{sha256(email)[:32]}/memory.json` | that user only | only in that user's DM threads |
| Project | `PROJECT.md` (datasets, columns, goals, conventions, caveats), `runs/` | `projects/{project_id}/…` | owner and invited members | every thread of that project |
| Thread | conversation, draft brief, workspace, SDK session | `projects/_threads/{thread_key}/checkpoint.json` plus archive (format unchanged) | members of the thread's project | that thread |

`init` always creates (or retries into) a project owned by the person who ran it. The draft lives in that thread, and `save brief` publishes it to that project only.

### Decided with the user

- **Data access stays lab-wide.** Any mapped, allowlisted user can point a project at any bucket allowed today (`AGENT_DATA_BUCKETS` / `AGENT_DATA_PROJECT`). Only projects, threads, and memory become private. Keep `BatchBridge.resolve_data` as the single data check, so a per-user policy can be added there later.
- **Projects are private by default.** The owner runs `share`/`unshare` to invite other mapped users.
- **The browser gets four small isolation fixes**, not the new memory.
- **User memory is "Letta-lite"** (§7).

### Deliberate simplifications

- **The internal user ID is the normalized email.** Both surfaces already use it: the web `Principal.id` and the values in `slack_user_map`. Storage keys use `sha256(email)[:32]` so no email appears in an object name. There is no users table. Changing someone's email would need a one-off script, which is acceptable for a lab.
- **Project IDs go in the existing `Context.project_slug` field.** Don't add `Context` fields or new top-level checkpoint keys: in-flight and rolled-back workers call `Context(**data)` (`batch_worker.py:111`) and rebuild the checkpoint from scratch (`:134-139`). Legacy projects keep `id = old slug`, so their briefs, `runs/`, workdirs, and the `read_state` fallback work unchanged.
- **One control path.**
  - Commands, chat, init drafting, and authorization all run in the control plane (`direct.py`), which is how staging already runs.
  - Delete `AGENT_DIRECT_CHAT_ENABLED`, the worker's command handling, and the local Socket Mode entrypoint (`agents/analysis/main.py`, `pyproject.toml:47`).
  - The worker only runs compute turns for threads the control plane has already created and authorized.
  - This also fixes the worker answering "help me plot…" with the help text (`slack_app.py:123-134`).

---

## 2. Invariants (each needs at least one test)

- **I1.** Every event resolves to a mapped, allowlisted email. If it doesn't, it gets exactly one threaded "not enabled" reply, and only for DMs and mentions, never for channel chatter.
- **I2.** Project membership is checked when the message reaches the front of its thread's queue, before reading the brief, calling a model, or touching the workspace.
  - `cancel` has its own check.
  - Compute is checked again just before job submission, and again in the worker.
- **I3.** Briefs and runs are keyed by project ID. Names are unique per owner. A channel binding is only a pointer; changing it never moves or exposes a brief.
- **I4.** A thread is fixed to its project when it is created. Membership is checked on every message, so `unshare` applies from the next one.
- **I5.** User memory is loaded only for the requesting user, and only in DMs. It is filtered by project access *before* ranking, and never written into `PROJECT.md` unless the user asks.
- **I6.** Deny messages never contain brief text, thread content, or project names. At most they name the owner's email.
- **I7.** Every registry and memory change is idempotent under Cloud Tasks redelivery and GCS retries.

---

## 3. Storage and concurrency

```
projects/_registry/registry.json                    one CAS-updated document
projects/_registry/backup-<UTC>.json                written by the migration tool before --apply
projects/{project_id}/PROJECT.md                    shared brief (legacy id = old slug)
projects/{project_id}/runs/<thread_ts>/turn-<n>/    unchanged (outputs.py:44)
projects/_threads/{thread_key}/checkpoint.json      unchanged format
projects/_users/{sha256(email)[:32]}/memory.json    Letta-lite memory (§7)
```

Everything lives under `projects/`:
- the bucket lifecycle keeps it (only `tasks/`, `inputs/`, `incoming/`, `paused/` expire; see `infra/main.tf:58-66`);
- the worker's IAM condition already covers it (`infra/slack_agent.tf:17`);
- it matches the existing `_threads/` convention.

```json
{"version": 1,
 "projects": {"p_3f2a9c01d4e5b6a7": {"name": "liver", "owner": "alice@lab.org",
   "members": ["bob@lab.org"], "data_uri": "gs://khushi-crc-xenium/liver/",
   "created_at": "2026-10-02T15:04:05Z"}},
 "bindings": {"T0123:D0456": {"project_id": "p_3f2a9c01d4e5b6a7",
   "bound_by": "alice@lab.org", "bound_at": "2026-10-02T15:04:05Z"}}}
```

- **New IDs** are `"p_" + secrets.token_hex(8)`. They can't collide with legacy `[a-z0-9-]` slugs or with the `_`-prefixed folders.
- **Conditional writes.** Add `Storage.put_if(key, data, generation: str | None) -> str`:
  - GCS: `if_generation_match=int(generation)`, or `0` to create. Map `PreconditionFailed` to a new `Conflict` exception.
  - Local: a module-level lock plus a sha256 comparison.
- **Update helper.** Add `update_model(storage, key, Model, change, attempts=5)`. It reads the document, applies `change`, skips the write if nothing changed, and retries on `Conflict`. `change` may raise `ValueError` to abort.
- **No registry cache.** Read the registry fresh for every check; it's one small GET. Delete `Projects.reload()`, which lists every object under `projects/` (`projects.py:47`).
- **Idempotency.**
  - Creating the same (owner, name, uri) again returns the existing ID.
  - `bind` and `share` are set operations.
  - Memory edits are keyed by the Slack-derived `event_id` (§7.3).

---

## 4. Access rules

| Action | Who may do it |
|---|---|
| Start or continue a thread, `brief`, `save brief`, `.md` upload, compute | owner or member of the thread's project |
| `cancel` | DM: the DM user. Channel: owner or member |
| `init` in a DM | any resolved user (the new project is theirs) |
| `use <project>` in a DM | owner or member of that project |
| `init` / `use` in a channel | owner of the target project. If the channel is already bound, they must also own the current project (or it must no longer exist) **and** pass `--force` |
| `share` / `unshare` | owner, in a DM |
| `projects`, `memory`, `remember`, `forget` | the user themself, in a DM |

- **Thread project:** `checkpoint.context.project_slug` if the thread already has a context; otherwise the channel's binding when the thread is created.
- **Private:** the channel ID starts with `D` (and `channel_type == "im"` when present; `app_mention` payloads have no `channel_type`).
- **Deny text:** "This conversation uses a private project. Ask its owner (alice@lab.org) to run `share <project> @you`." A missing or unregistered project gets "This thread's project is unavailable."

---

## 5. Commands (`agents/analysis/commands.py`)

**Parsing.**
- Match case-insensitively after removing **leading** `<@…>` mentions, with exact token counts.
- Anything that doesn't match the grammar is normal chat. "use log scale" and "forget it" must reach the model.
- For `share`/`unshare`, parse the target from the raw text, because `message_text` strips every mention (`data_links.py:9`). Accept `<@U…>`, `<mailto:a@b|a@b>`, or a bare email, and resolve it through `slack_user_map` plus the allowlist.

| Command | Grammar | Where |
|---|---|---|
| `help` | exact | anywhere. Shows the commands, the active project's name and owner, and your role (your email only in DMs) |
| `projects` | exact | DM. Lists your own and shared projects and marks the active one |
| `init` | `init <name> <gs-link> [--force]` (keep `init_args` errors) | new thread |
| `use` | `use <name\|p_id> [--force]` | anywhere |
| `share` / `unshare` | `share <name\|p_id> <@user\|email>` | DM |
| `brief` / `save brief` / `cancel` | exact (`brief` may carry one `.md`) | in a thread |
| `memory` | exact | DM |
| `remember` | `remember <1–600 chars>` | DM |
| `forget` | `forget <n>` \| `forget profile` \| `forget preferences` \| `forget all` | DM |

**Resolving `<name>`**, in order:
1. a project ID;
2. your own project with that name;
3. a shared project with that name, if exactly one exists.

If it's ambiguous, list `p_id (owner)` for each match. Never reveal projects the user can't use.

**Duplicate hint.** On `init` in a DM, if the user already has a project on the same `data_uri`, say so ("You also have `crc-old` on this folder; send `use crc-old` to switch").

---

## 6. Request flow

### `receive()` (`api/slack_analysis.py`)
1. Keep the structural checks (`:21-36`).
2. Move the channel/thread filter (`:47-56`) **before** the identity check.
3. Apply `AGENT_ALLOWED_USER_IDS`.
4. Call `identity.email_for(app, team, user)`. If it returns `None`, post one threaded "not enabled" reply (DMs and mentions only) and return.
5. Delete the `direct_chat_enabled` validation (`:57-59`).
6. Call `bridge.accept(payload)`.

### `BatchBridge.process()`
- `cancel` is always handled; remove the flag gate at `:311-313`.
- Before cancelling, call `access.resolve(...)`. If the user isn't allowed, finish with the deny text.
- Queue ordering is unchanged; then call `advance()`.

### `BatchBridge.advance()`
- **Every pending, non-compute record goes to `direct.handle`.** Remove the `:335` gate.
- **For compute:**
  - Run `access.resolve` again and refuse if not allowed.
  - `resolve_data` takes the bucket from the init link or the thread's frozen `data_path`, as today. Delete the channel fallback (`:193-197`).
  - Store `principal`, `project_id`, and `private` on the record.
  - Delete the `claude-config` lease code (`:382-397`, `:493-494`, and the other releases). Registry writes now use CAS.
- **Manifest** (`:423-427`) becomes `{payload, thread_key, notice_ts, principal, project_id, project_name, private, user_memory}`. `user_memory` is rendered only when private (§7.2). Old workers ignore the extra keys.
- **When a job succeeds** in a private thread, run the memory editor (§7.3), best effort.

### `direct.handle()`
```
g = access.resolve(app, settings, storage, record)   # email, private, state, doc, project_id, allowed
if not g.email: finish(NOT_ENABLED)
cmd = commands.parse(raw_text)
if cmd is an account/memory command: finish(await commands.run(...)); return
if cmd is init:
    refuse if the thread already has a context (unless record["direct_init"])
    resolve_data(uri)                                  # unchanged lab-wide bucket check
    pid = Registry.update(create_and_bind(team, channel, g.email, name, uri, private=g.private, force=cmd.force))
    record["direct_init"] = True
else:
    if not g.project_id: finish(NOT_LINKED)
    if not g.allowed:   finish(deny_text)            # nothing read or written; no model call
if the thread has no context: build it exactly as today (direct.py:174-183) with
    project_slug = pid, workdir = work_root/pid/f"{channel}-{thread}", brief = read_brief(pid)
upload / save brief / brief branches unchanged, now via write_brief(ctx["project_slug"])
decide() context adds project_name, plus user_memory when private
after a delivered non-command reply in a DM: memory editor (§7.3), best effort
```
Rename the thread-transcript helper `remember` (`direct.py:117`, also `batch_worker.py:23` and `138`) to `record_exchange`, to free the word for the memory command.

### `batch_worker.main()`
1. Load the manifest and checkpoint.
2. Compute `permitted = context and principal and project_id and context["project_slug"] == project_id and Registry(storage).load().can_use(project_id, principal)`.
3. If not permitted, update the notice with a refusal and exit. Don't restore the archive or write a checkpoint.
4. Otherwise restore as today.
5. Build `Runner(settings, user_memory=…, project_name=…)`, passing memory only when `private`.
6. Call `SlackAgent.handle(event, key)` directly. Remove `Projects`/`reload` (`:104`, `:117`) and the forced `app_mention` type (`:120`).

### `SlackAgent` (compute only)
- Load the saved context, or fail with "This thread has no saved context…".
- Build the prompt (`slack_app.py:192-201`) and call `run()`, which publishes with `self.storage`.
- Delete `accept`, `react`, `brief`, `register`, the help/init/save-brief handling, and the history fetch.

### `Runner` and prompts
- Signature chain: `Runner(settings, *, sandbox=True, user_memory="", project_name="")` → `build_options(...)` → `build_system_append(ctx, settings, user_memory, project_name)`.
- Add a "# Your memory" section after the brief section (`prompts.py:74-75`), only when it's non-empty.
- **Never put memory in the user prompt.** Prompts are saved to the project's shared `runs/…/meta.json` (`outputs.py:62-73`).

---

## 7. Letta-lite user memory (`agents/analysis/user_memory.py`)

### 7.1 Structure
```json
{"version": 1, "next_seq": 13,
 "blocks": {"profile":     {"text": "…", "updated_at": "…"},
            "preferences": {"text": "…", "updated_at": "…"}},
 "notes": [{"seq": 12, "text": "Leiden res 0.5 worked well for neighborhoods",
            "project_id": "p_…", "created_at": "…", "event_id": "…"}],
 "applied_events": ["<event_id>", "…"]}
```
- **Limits:** each block ≤1,500 characters; at most 200 notes, each ≤600 characters; retain permanent hashed operation receipts.
- **Stable numbers.** A note's `seq` is never reused, so `forget 12` stays correct after other edits.

### 7.2 Reading (DMs only)
1. Load only the requester's file. That is the ownership filter.
2. Drop notes whose `project_id` the user can no longer use. That is the access filter, and it runs before ranking.
3. Rank the remaining notes by keyword overlap with the request and by recency (`exp(-age_days/60)`). On `init`, add the bucket's folder names to the request. Keep the top 5, within 2,500 characters.
4. Render:
   ```
   # Your memory (private to you; user-level, not facts about this project)
   ## Profile
   …
   ## Preferences
   …
   ## Possibly relevant notes
   #12 [from crc-xenium, 2026-10-02] Leiden res 0.5 worked well for neighborhoods
   ```
5. Pass it into `decide()` as `user_memory`, and through the manifest into the worker's system prompt. Never into channel or group threads.

**Prompt rules** (add to `direct.SYSTEM` and to the worker's memory section):
- Apply preferences unless the user or the brief says otherwise.
- Notes from other projects are suggestions only. Confirm before relying on one here, and never present it as a fact about this project.
- Don't copy memory into the brief unless the user asks.
- Don't claim to have saved anything to memory; a confirmation is posted when memory changes. Users review it with `memory`.
- On `init` in a DM, show memory only as attributed suggestions in the private reply, outside the draft, marked "(from your memory, please confirm)".

### 7.3 Writing: the post-turn memory editor
After a non-command DM turn has been delivered, the control plane makes one small model call. Copy the `httpx` pattern from `direct.decide`; the model is `AGENT_MEMORY_MODEL`, defaulting to `AGENT_MODEL`. It exposes one tool, `edit_memory`:
```python
class Edit(BaseModel):
    op: Literal["block_append", "block_replace", "note_add", "note_delete"]
    block: Literal["profile", "preferences"] | None = None
    old: str | None = None                      # block_replace: exact substring; empty new text deletes it
    text: str | None = Field(default=None, max_length=600)
    seq: int | None = None                      # note_delete
class Edits(BaseModel):
    edits: list[Edit] = Field(default_factory=list, max_length=3)
```

**Input to the call:** both blocks, the 20 notes most related to this exchange (so it can avoid duplicates), the project name, and the user message plus reply, each trimmed to 4,000 characters.

**Instructions for the editor:**
- Save only durable, user-level information:
  - **profile:** role, field, recurring organisms, platforms, data types;
  - **preferences:** things the user stated or confirmed (figure style, defaults, reply style);
  - **notes:** reusable lessons.
- Never save project dataset facts (they belong in `PROJECT.md`), one-off requests, results, guesses, secrets or credentials, patient identifiers, or text copied from data or filenames.
- Update an existing entry instead of adding a duplicate.
- Follow "forget …" requests.
- Most of the time, return no edits.

**Validation.** Reject a single failing edit and keep the others. Check:
- the block size limit;
- that `block_replace.old` is an exact substring;
- for secret-like patterns: Slack tokens (`xox?-`), API keys (`sk-`), AWS keys (`AKIA…`), private-key headers, `password=`;
- at most 3 edits;
- DMs only.

**Applying edits.**
- Write with `update_model`, keyed by `event_id`; skip if the event is already in `applied_events`.
- New notes get `project_id` = the thread's project.

**Notice.** If anything changed, post one thread message, e.g. "📝 Memory updated (preferences): added “prefers viridis colormaps”. Reply `memory` to review, or `forget 12` / `forget preferences` to undo."

**When it runs.**
- **Direct turns:** in `direct.handle`, after the reply has been delivered.
- **Compute turns:** in `advance()` when the job succeeds, using the exchange the worker stored (the `messages` entry whose `id == event_id`).

**Failure handling.** Best effort with a 30-second timeout. Log and continue on any error; it must never fail or delay the reply. Setting `AGENT_MEMORY_AUTO_EDIT=false` turns it off.

### 7.4 Commands
- **`memory`** shows both blocks and the 20 newest notes as `#seq [project, date] text`. Notes from projects the user can no longer use are marked "(project no longer shared)". It ends with how to change things.
- **`remember <text>`** adds a note directly; idempotent by `event_id`.
- **`forget <n>` / `forget profile` / `forget preferences` / `forget all`** first records what it will delete on the receipt (`record["forget_targets"]`), then deletes it. A redelivered message can't delete a different item.
- **Plain-language changes** ("I don't need PDFs anymore") are handled by the editor.

---

## 8. Part B: browser isolation fixes (local; ship with the next full web deploy)

- **B1 Session keys.**
  - Add `session_key(envelope)` = `"conv_" + sha256(project_id \0 principal_id \0 conversation_id)[:32]` in `agents/session.py`.
  - Use it at `harness.py:450`, at the compaction call (`tasks.py:1529-1531`; rename `compact_session`'s parameter to `session_id`), and in the idle sweep (`tasks.py:2226-2227`).
  - Old legacy-planner sessions are simply orphaned.
- **B2 The server sets task identity fields.** In `create_task` (`api/routes.py:181-192`), web submissions always get `source="web"`, a fresh `conversation_id`, and a fresh server-generated `external_event_id`. This also blocks two attacks:
  - using `source="slack"` to post task updates into an arbitrary Slack DM;
  - using `external_event_id="process:<task_id>"` to block a task from being processed.
- **B3 Auth config guard.**
  - Add a `Settings` model validator: `auth_mode="dev"` is allowed only when `env in {"local","test"}`.
  - Also lowercase `allowed_emails` when the input is a list or tuple (`config.py:122-127`).
- **B4 Legacy memory candidates.** At `tasks.py:500-504`, give each model-written `memory_candidate` a fresh server ID, `owner_id=None`, `version=1`, no embedding, and candidate status. This stops a model from overwriting another project's memory by choosing an ID.

---

## 9. Milestones

### M0: Write the failing regression tests first
- Create `tests/test_analysis_isolation.py`. Mark every test `xfail(strict=True)` and name the milestone that fixes it.
  - Drive the tests only through Slack payloads (`slack_analysis.receive`, `bridge.accept`/`process`), with a `decide` stub keyed on `context["request"]`. That way the test bodies survive the refactors.
- In `tests/test_analysis_batch.py`, extend `payload()` with `user`, `kind`, and `channel_type`, and add `"T1:U2": "bob@example.com"` to the fixture's user map.
- **Fixed in M1:**
  - unmapped channel chatter gets no reply;
  - an unmapped mention plus its `message` copy gets exactly one threaded reply;
  - `allowed_emails` is enforced.
- **Fixed in M3b:**
  - Alice runs `init liver`, `save brief` with "ALICE-SECRET", then `init brain --force`. Bob's `init liver …` never sees ALICE-SECRET.
  - In Alice's channel thread, Bob can't run `brief`, can't edit the draft by chat and `save brief`, and can't `cancel` her job.
  - Bob's denial happens before `decide()` and contains no brief text.

### M1: Identity on arrival (Release 1, API image only)
- Add `identity.py` with:
  - `email_for(app, team, user)`
  - `is_private(event)`
  - `user_key(email)`
  - `target_email(app, team, token)`
  - `async post(token, channel, thread_ts, text)`
- Rewrite `receive()` as in §6.
- Remove the xfail from the M1 tests. Add tests for an unmapped DM and for `AGENT_ALLOWED_USER_IDS`.

### M2: One control path and a compute-only worker
- **Delete:**
  - `direct_chat_enabled` (`config.py:40`; `extra="ignore"` keeps the old env var harmless);
  - `main.py` and `pyproject.toml:47`;
  - the worker-init `claude-config` block;
  - the `resolve_data` channel fallback;
  - `AGENT_ALLOWED_USER_IDS` from the job environment (`batch.py:77`).
- Make the flag gates in `batch.py` unconditional.
- Rename `remember` to `record_exchange`.
- Slim `SlackAgent` and `batch_worker` as in §6. Don't add the registry check yet.
- **Tests to delete:** the worker-mode command tests in `tests/test_analysis_slack.py` (init, save, routing, allowlist, brief upload, invalid init) and `test_init_mapping_changes_are_serialized`.
- **Tests to keep:** the formatting, cleanup, heartbeat, failed-turn, and draft-revision tests. Run them against `handle()` with a pre-seeded `Context`.
- **Tests to move or adapt:** move the interrupted-init retry test to the direct tests; adapt `test_dynamic_project_buckets…` and `test_worker_cold_start_resume…`.
- **New tests:**
  - the worker runs "help me plot" as compute;
  - a worker with no saved context posts an error and writes no checkpoint.

### M3a: Registry and migration tool (not wired in yet)
- **`storage.py`:** `Conflict`, `put_if`, `update_model`.
- **New `registry.py`:**
  - models `ProjectEntry` and `Binding`;
  - `RegistryDoc` with `can_use`, `owns`, `visible`, `find`, `bound`, `create`, `bind`, `create_and_bind`, `share`, `unshare`;
  - `Registry(storage).load` and `.update`;
  - `read_brief` / `write_brief`, moved from `projects.py:87-98`.
- **New `tools/migrate_slack_registry.py`:**
  - **Dry run (default).** For each legacy `projects/*/project.json`, report:
    - the slug, and `created_by` mapped to an email (via `--user-map`);
    - the channel, the data URI, and the brief size;
    - how many thread checkpoints use it;
    - whether it's already registered.

    It also writes a draft `owners.json`.
  - **`--apply --owners owners.json --team T…`:**
    - write a registry backup first;
    - add only missing projects (`id = slug`) and `T:C` bindings;
    - skip projects with no owner (fail closed) and list them;
    - never modify legacy files (they're needed for rollback);
    - running it again changes nothing.
- **`tests/test_analysis_registry.py`:**
  - `put_if` conflicts;
  - `update_model` keeps a concurrent writer's change;
  - idempotent create, bind, and share;
  - the binding-rule matrix;
  - `find` ambiguity and invisibility of other users' projects;
  - the ported `test_project_boundaries` cases;
  - migration report, apply, re-apply, backup, unowned projects, untouched legacy files.

### M3b: Enforce access everywhere (API and worker)
- **New `access.py`:** `Grant`, `resolve`, `deny_text`, and `read_state` (moved from `direct.py:94-114`).
- **New `commands.py`:** help, projects, init, use, share, unshare, brief, save brief, cancel.
- Wire up `direct.handle`, `process`, `advance`, and `batch_worker` as in §6.
- Delete `projects.py` and all `claude-config` code.
- In tests, a `seed_project(store, name, owner, binding, brief)` fixture replaces `Projects.bind`.
- Remove the xfail from the M3b tests.
- **New tests:**
  - `unshare` applies from the next message, and the worker refuses a queued compute;
  - the worker refuses a mismatched `project_id`, a missing principal, or a non-member;
  - channel bind/rebind ownership and `--force`;
  - DM `use` of a shared project;
  - a thread with a legacy ID continues and publishes to `projects/liver/runs/…`;
  - the `read_state` legacy fallback still works;
  - "use log scale" reaches the model;
  - `share` works with `<@U2>` and with `<mailto:…|…>`.

### M4: Letta-lite memory (API and worker)
- **Build:**
  - `user_memory.py`: load, retrieve, render, validate/apply edits, commands;
  - the editor call;
  - the `Runner`/`prompts` plumbing and the manifest field;
  - the prompt rules;
  - the settings `AGENT_MEMORY_AUTO_EDIT` and `AGENT_MEMORY_MODEL`.
- **Where memory appears:**
  - DM `decide()` and the DM compute manifest/`Runner` get memory;
  - channel threads for the same user get none;
  - Bob's memory never shows up in Alice's contexts.
- **Retrieval:**
  - the access filter runs before ranking: a note from an unshared project is left out of prompts but shown in `memory` as "(project no longer shared)";
  - ranking and the character budget.
- **The editor:**
  - edits are validated (size, exact `old`, secrets, at most 3);
  - edits apply once even when redelivered;
  - the notice is posted;
  - an editor failure doesn't affect the reply;
  - it doesn't run in channels or for commands.
- **Commands and init:**
  - `remember` and `forget` are idempotent, and `#seq` numbers stay stable;
  - `init` in a DM includes memory as suggestions; in a channel it doesn't.

### M5: Browser fixes (Part B; independent, can run in parallel)
B1–B4, each with a test:
- **B1:** the same `conversation_id` across projects or users gives different sessions, and compaction and the idle sweep use the same key (`tests/test_compaction.py`).
- **B2:** `POST /api/v1/tasks` ignores client-supplied `source`, `conversation_id`, and `external_event_id` (follow `tests/test_api.py`, with `auth_mode="iap"`).
- **B3:** `Settings(env="staging", auth_mode="dev")` raises.
- **B4:** a forged memory candidate can't overwrite another project's item (`tests/test_memory_context.py`).

### M6: Docs and environment
- `docs/slack-agent-setup.md`: commands, identity, access rules, memory, migration, rollout.
- `docs/memory.md`: a short pointer (the web memory is unchanged).
- `deploy/slack.env.example`: remove `AGENT_DIRECT_CHAT_ENABLED`; add `AGENT_MEMORY_AUTO_EDIT` and `AGENT_MEMORY_MODEL`.
- `infra/main.tf:368-371`: remove `AGENT_DIRECT_CHAT_ENABLED`.

---

## 10. Rollout (ask before every cloud step)

1. **Release 1 (M1):** the API image, built with `cloudbuild/build-slack-api.yaml` (which runs the preservation check).
   - Before deploying, confirm that every email in `MICA_SLACK_USER_MAP` is also in `MICA_ALLOWED_EMAILS`. Terraform sets both (`infra/main.tf:281`, `353`).
   - Confirm the base image's `Settings` has `allowed_emails`.
2. **Release 2 (M2–M3b):**
   1. Build the API and worker images from one commit.
   2. Run the migration dry run and review `owners.json` with the user, then run `--apply`.
   3. Deploy one tagged revision with no traffic, using the new API image and `MICA_SLACK_AGENT_IMAGE` set to the new worker digest. One revision changes both.
   4. Run the health and credential checks, then promote.
   5. Old ingress, tasks, and workers must be drained before migration and remain stopped until cutover; do not use a second pass as a substitute.
3. **Release 3 (M4, memory):** same path. It can ship together with Release 2 if preferred.
4. **Live check:** the two-user DM check in §11.

**In-flight work:**
- Old receipts are re-authorized from the payload plus the checkpoint, so they need no new fields.
- Jobs already submitted keep their old image.
- Leftover `claude-config` leases expire on their own.
- Legacy projects with no owner are denied; the dry run shows how many threads that affects.

**Rollback:** drain first and use only an ACL-compatible API/worker pair, or disable Slack ingress. Never restore the pre-ACL slug backend. Keep the registry, memory, and legacy files.

---

## 11. Verification

- **Every milestone:** `pytest -q`, `ruff check src tests tools`, `mypy src/mica`.
- **Import boundary:** this must print nothing:
  ```
  grep -rnE "from mica\.(services|agents\.(memory|context|harness|session))" src/mica/agents/analysis src/mica/api/slack_analysis.py
  ```
- **Build gate:** `cloudbuild/build-slack-api.yaml` passes `check_slack_preservation.py` and the Dockerfile smoke test.
- **Live, in staging, led by the user**, with two mapped users A and B in DMs:
  1. A runs `init crc gs://…` and then `save brief`. B runs `init crc gs://…` and gets B's own empty draft. B never sees A's brief.
  2. B's `projects` lists only B's projects. A runs `share crc @B`; B runs `use crc` and can analyze. A runs `unshare crc @B`; B's next message gets the deny text with no project details.
  3. A says "I always want viridis and 300-dpi PNGs" and sees the "📝 Memory updated…" notice. In a new thread on a different project, A's plot uses viridis. `memory` shows the preference, and `forget preferences` clears it.
  4. If channel events are enabled, a channel thread shows no memory and refuses `memory`.

---

## 12. Out of scope (known, not fixed here)

- Per-user data permissions. The user chose lab-wide access; `resolve_data` is where a policy would go.
- Browser use of the new user memory or project registry.
- Other web issues: the `retrieve()` write-back race, planner-v2 `master_hash` sensitivity to retrieval, and the global slug race in Firestore.
- Authorized channel runs still use `thread_ts`; private runs additionally use the user hash and full thread key.
- Memory consolidation jobs, embeddings, and memory tools used during an analysis (the "live tools" option).
- Admin tooling beyond the migration script, such as transferring ownership when someone leaves.
