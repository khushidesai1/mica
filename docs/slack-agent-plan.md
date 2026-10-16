# Slack Analysis Agent: Implementation Plan

> Source behavior update, 10 October 2026: [the routing and memory implementation plan](worker-routing-and-memory-plan.md) supersedes the pricing/quote gates, thread-draft guide flow, knowledge feature flag, and per-thread channel summaries described below. Those passages remain historical design/deployment evidence. This local implementation has not been deployed or validated in staging.

> Implementation updates (1 October 2026): subsequent user direction keeps the existing
> Cloud Run/webhook/Batch infrastructure. Conversation, questions, brief revisions, saving,
> cancellation, and bounded init run directly on Cloud Run; Spot is reserved for data inspection
> and computation. Briefs can start incomplete and are revised from ordinary chat as users
> provide details or analyses verify new facts. Markdown uploads are optional. Generated
> figures are validated through code completion and output checks, without image previews. The current
> operational design and deployment record are in `slack-agent-setup.md` and
> `slack-agent-deployment.md`; they supersede the original VM and per-message worker details below.


A small Slack bot that works like a simplified "Claude Science in Slack". Team members @mention it
in a project channel and ask for analyses and plots in plain language. The bot finds the right
dataset in the project's Google Cloud Storage bucket, writes and runs Python, and posts the figure
back in the thread. Each project has a short markdown file (`PROJECT.md`) describing its data, so
nobody has to say which file to use every time.

This plan is written for an AI coding assistant (or a developer) to implement from scratch.

---

## 0. Instructions for the implementer (read first)

1. **Stack:** Python 3.12, `claude-agent-sdk` (Anthropic's Claude Agent SDK), `slack-bolt` in
   Socket Mode, `google-cloud-storage`, `pydantic-settings`. The agent runs on Claude models.
   **Do not use the OpenAI SDK, LangChain, or any other agent framework.**
2. **The Claude Agent SDK is newer than much of your training data.** Use exactly the imports and
   options shown in section 6. They were checked against the official reference in September 2026.
   If something doesn't work, check https://code.claude.com/docs/en/agent-sdk/python instead of
   guessing parameter names.
3. **Build one milestone at a time (section 14).** Each milestone must end runnable, with its tests
   passing, before starting the next.
4. **Keep it small.** Target under ~1,500 lines of Python including tests. Leave out web UIs,
   database servers, task queues, multi-agent pipelines, and plugin systems. The model does the
   planning. The code only connects Slack, the agent, and storage.
5. **Ask the human before** creating cloud resources, changing IAM permissions, or doing anything
   that costs money. The same goes for security: if a control in section 12 can't be made to
   work, stop and ask. Don't quietly disable it.

---

## 1. What the user experiences

In `#liver-spatial` (a channel bound to the `liver-spatial` project):

> **Alice:** @analysis-bot plot the spatial layout of sample B colored by cell type
>
> **analysis-bot:** _(status message, updated live)_ Reading PROJECT.md… → Inspecting
> processed/liver_B_qc.h5ad… → Running code/spatial_celltypes.py… → Done in 1m 12s
>
> **analysis-bot:** Data: `processed/liver_B_qc.h5ad` (sample B, 612,408 cells)
> Spatial map colored by `annotated_cell_type` (14 types). Hepatocytes dominate the periportal
> zone; Kupffer cells are scattered evenly. Points are drawn at 0.2 px, rasterized.
> 📎 `spatial_celltypes_B.png`
>
> **Alice** _(reply in the same thread, no mention needed):_ make it a 2x2 grid with all samples
>
> **analysis-bot:** Data: `raw/liver_all.h5ad` (samples A–D) … 📎 `spatial_celltypes_all.png`

Other commands, posted as a mention in the channel:
- `@analysis-bot help` shows the commands and the current project.
- `@analysis-bot init liver-spatial gs://lab-data/liver-spatial/` binds this channel to a project,
  surveys the data, and posts a draft `PROJECT.md`.
- `@analysis-bot brief` posts the current `PROJECT.md`.
- Reply with project details or corrections in plain language; the agent revises the brief.
- `save brief` publishes this thread’s current draft for new threads, at any point in the work.
- Optionally, `@analysis-bot brief` with an attached `.md` file replaces `PROJECT.md`.

---

## 2. Concepts

| Concept | Meaning |
|---|---|
| **Project** | A slug (for example `liver-spatial`), one Slack channel, one data location (`gs://bucket/prefix/`), and one `PROJECT.md` brief. |
| **Brief** (`PROJECT.md`) | Human-editable markdown describing the datasets, column meanings, defaults, and caveats. It's injected into the agent's system prompt at the start of every new thread. |
| **Thread = session** | Each Slack thread maps to one Claude Agent SDK session. Follow-ups in the thread resume that session, so the agent remembers files, code, and decisions. New threads start fresh with the latest brief. |
| **Workspace** | A local directory per thread holding `code/` (scripts the agent writes) and `outputs/` (files to share). |

---

## 3. Architecture

```
Slack (Socket Mode, outbound WebSocket; no public URL needed)
   │
   ▼
slack_agent process (one Python asyncio process on one GCE VM, run by systemd)
   ├─ slack_app.py     Bolt handlers: mentions, thread replies, commands
   ├─ agent_runner.py  one Claude Agent SDK query() per user message; resume per thread
   ├─ projects.py      loads project.json + PROJECT.md from the agent bucket
   ├─ sessions.py      SQLite: (channel, thread_ts) → session_id, workspace, project
   └─ outputs.py       uploads new files to Slack and GCS, writes provenance
   │
   ├─ Claude API (the SDK spawns a bundled `claude` CLI subprocess per turn)
   ├─ /mnt/gcs/<data-bucket>/  data buckets mounted read-only with gcsfuse
   └─ gs://<agent-bucket>/     project configs, briefs, run outputs (read/write)
```

**Where state lives:**

| State | Location |
|---|---|
| Project config and brief | `gs://<agent-bucket>/projects/<slug>/project.json` and `.../PROJECT.md` |
| Thread → session mapping | SQLite at `/srv/slack-agent/state.db` |
| Session transcripts | Written by the SDK under `~/.claude/projects/` on the VM's persistent disk |
| Per-thread workspaces | `/srv/slack-agent/work/<slug>/<channel>-<thread_ts>/` |
| Shared results and provenance | `gs://<agent-bucket>/projects/<slug>/runs/<thread_ts>/turn-<n>/` |
| Raw data | Data buckets. **Read-only, never modified.** |

Briefs live in a separate agent bucket instead of next to the data. That way the data buckets
stay read-only for the bot, while `init` and `brief` can still save files.

---

## 4. Repository layout (new standalone repo: `slack-analysis-agent`)

```
slack-analysis-agent/
  pyproject.toml
  README.md                 setup, Slack app config, deploy steps
  .env.example
  src/slack_agent/
    __init__.py
    main.py                 entrypoint: load settings, open DB, start Socket Mode
    cli.py                  local runner without Slack (milestone 1)
    config.py               pydantic-settings Settings
    slack_app.py            Bolt handlers and command parsing
    agent_runner.py         builds ClaudeAgentOptions, runs a turn, reports status
    prompts.py              system prompt text and init prompt (section 7)
    projects.py             Project model, load/save via Storage
    storage.py              Storage protocol + GCSStorage + LocalStorage (tests/dev)
    sessions.py             SQLite helpers
    outputs.py              detect new files, upload to Slack/GCS, provenance
    slack_format.py         markdown → Slack mrkdwn safety net, message chunking
  tools/
    inspect_data.py         describes a data file without loading matrices (section 10)
  templates/
    PROJECT.template.md
  deploy/
    setup_vm.sh             installs packages, science env, gcsfuse, service
    slack-agent.service     systemd unit
    gcsfuse.yaml            gcsfuse config
    cleanup.sh + cleanup.timer   deletes local workspaces older than 30 days
  tests/
    make_fixture.py         generates a small synthetic spatial .h5ad
    test_*.py
```

**Bot dependencies:** `claude-agent-sdk`, `slack-bolt`, `aiohttp`, `google-cloud-storage`,
`pydantic-settings`, `pytest`, `pytest-asyncio`.

The **science environment** is separate: a venv at `/opt/sci` that the agent's generated code
runs in. It needs `anndata`, `scanpy`, `squidpy`, `h5py`, `numpy`, `pandas`, `pyarrow`, `scipy`,
`scikit-learn`, `matplotlib`, `seaborn`, `leidenalg`, `python-igraph`, `louvain`, and optionally
`datashader`. Pin versions in `deploy/requirements-sci.txt`.

---

## 5. Configuration

All settings come from environment variables (`Settings` in `config.py`, prefix `AGENT_` except
where noted).

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | (required) | Claude API key |
| `SLACK_BOT_TOKEN` | (required) | `xoxb-…` bot token |
| `SLACK_APP_TOKEN` | (required) | `xapp-…` app-level token for Socket Mode |
| `AGENT_MODEL` | `claude-opus-5-5` | Claude model. `claude-sonnet-5-5` costs less. |
| `AGENT_FALLBACK_MODEL` | `claude-opus-5` | Used if the primary model fails (see section 12) |
| `AGENT_BUCKET` | (required) | Bucket for project configs, briefs, and outputs |
| `AGENT_DATA_BUCKETS` | (required) | Comma-separated data buckets that projects may use |
| `AGENT_DATA_MOUNT_ROOT` | `/mnt/gcs` | Where the data buckets are mounted |
| `AGENT_WORK_ROOT` | `/srv/slack-agent/work` | Per-thread workspaces |
| `AGENT_STATE_DB` | `/srv/slack-agent/state.db` | SQLite path |
| `AGENT_SCI_PYTHON` | `/opt/sci/bin/python` | Interpreter the agent must use |
| `AGENT_TOOLS_DIR` | `/opt/slack-agent/tools` | Location of `inspect_data.py` |
| `AGENT_MAX_TURNS` | `40` | Tool-use round trips per user message |
| `AGENT_MAX_BUDGET_USD_PER_TURN` | `5` | Hard stop per user message |
| `AGENT_MAX_CONCURRENT_RUNS` | `3` | Global concurrency limit (RAM bound) |
| `AGENT_ALLOWED_USER_IDS` | empty (all) | Optional Slack user allowlist |
| `AGENT_STORAGE` | `gcs` | `gcs` or `local` (tests and local dev) |
| `AGENT_LOCAL_STORAGE_ROOT` | `./.local-storage` | Used when `AGENT_STORAGE=local` |

---

## 6. The agent runner (core of the system)

### 6.1 Workspace per thread

On the first message of a thread, create:
```
/srv/slack-agent/work/<slug>/<channel_id>-<thread_ts>/
  code/
  outputs/
  PROJECT.template.md    (copied from templates/; only needed for init)
```
Store the path in SQLite. Every turn in that thread uses this directory as `cwd`. **Resume
requires the same `cwd` as the original run**, because the SDK stores transcripts under a path
derived from `cwd`.

### 6.2 SDK options (use exactly these)

```python
from claude_agent_sdk import (
    query, ClaudeAgentOptions, AssistantMessage, ResultMessage, TextBlock, ToolUseBlock,
)

def build_options(ctx, settings) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=settings.model,                          # "claude-opus-5-5"
        fallback_model=settings.fallback_model or None,
        cwd=str(ctx.workdir),
        add_dirs=[str(ctx.data_path)],                 # read access to the project's data
        resume=ctx.session_id,                         # None on a thread's first turn
        system_prompt={
            "type": "preset",
            "preset": "claude_code",                   # keeps built-in tool guidance
            "append": prompts.build_system_append(ctx, settings),
        },
        setting_sources=[],                            # ignore ~/.claude and host project settings
        allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
        disallowed_tools=["WebFetch", "WebSearch", "Agent", "AskUserQuestion", "NotebookEdit"],
        permission_mode="dontAsk",                     # anything not pre-approved is denied, never prompts
        max_turns=settings.max_turns,
        max_budget_usd=settings.max_budget_usd_per_turn,
        env={
            "SLACK_BOT_TOKEN": "",                     # blank Slack secrets for the agent subprocess
            "SLACK_APP_TOKEN": "",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        },
        sandbox={
            "enabled": True,
            "failIfUnavailable": True,                 # refuse to run unsandboxed
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
        },
    )
```

Notes:
- `AskUserQuestion` is disabled on purpose. When the agent needs to ask something, it ends its
  reply with the question, and the user answers in the thread.
- `setting_sources=[]` plus `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` stops the agent from picking up
  anything on the host by accident. The brief is passed explicitly through the system prompt.

### 6.3 Running one turn

```python
async def run_turn(ctx, prompt: str, on_status) -> TurnResult:
    options = build_options(ctx, settings)
    before = snapshot_outputs(ctx.workdir / "outputs")      # {path: mtime}
    session_id, final_text, last_text, cost, error = ctx.session_id, "", "", None, None
    run_log = []
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ToolUseBlock):
                        run_log.append({"tool": block.name, "input": block.input})
                        await on_status(describe_tool(block))    # throttled by the caller
                    elif isinstance(block, TextBlock):
                        last_text = block.text
            elif isinstance(msg, ResultMessage):
                session_id = msg.session_id
                cost = msg.total_cost_usd
                if msg.subtype == "success":
                    final_text = msg.result or last_text
                else:
                    error = msg.subtype          # e.g. "error_max_turns", "error_during_execution"
                if msg.stop_reason == "refusal":
                    log.warning("model refusal", extra={"thread": ctx.thread_ts})
    except Exception as exc:                     # query() raises after yielding an error result
        error = error or f"{type(exc).__name__}: {exc}"
    new_files = changed_files(ctx.workdir / "outputs", before)
    return TurnResult(session_id, final_text or last_text, new_files, run_log, cost, error)
```

- `describe_tool(block)`: `Bash` → `block.input.get("description")` or the first 60 characters of
  `command`. `Read`/`Write`/`Edit` → "Reading <filename>" / "Writing <filename>".
  `Glob`/`Grep` → "Searching files".
- **Resume failure:** if a resumed turn fails because the session can't be loaded, retry once
  with `resume=None`. Prefix the prompt with: "(Earlier context for this thread is unavailable.
  Ask the user to restate anything you need.)" Then save the new session ID.
- **Prompt format:** prefix each user message with the speaker's display name, for example
  `[Alice]: plot …`. That way the agent can tell people apart in shared threads. Cache
  `users_info` lookups.
- **First mention inside an existing thread** the bot hasn't seen: fetch up to the last 20
  messages with `conversations_replies` and include them above the request as "Earlier messages in
  this thread".

### 6.4 Concurrency

- One `asyncio.Lock` per `(channel, thread_ts)`, so messages in one thread run in order. If a
  message arrives while its thread is busy, react with ⏳ and let it wait for the lock.
- One global `asyncio.Semaphore(AGENT_MAX_CONCURRENT_RUNS)`.
- Slack handlers must return quickly. Start each turn with `asyncio.create_task(...)`.
- Deduplicate Slack retries by `event_id` (in-memory LRU plus a `processed_events` table).

---

## 7. Prompts (put these in `prompts.py`, filling in the `{placeholders}`)

### 7.1 System prompt append (every turn)

```
You are the lab's data analysis assistant, working in a Slack thread for the project described
in the project brief below. Team members ask for analyses and plots in plain language.

## How to work
- This project's data is mounted read-only at {data_path}. Never try to modify it.
- Your working directory is this thread's workspace. Put scripts in code/ and every file you want
  to share (figures, tables) in outputs/. Only files in outputs/ are posted to Slack.
- Write analysis code to a script under code/ and run it with {sci_python}. Use `python -c` only
  for one-line checks. For follow-ups, edit and rerun the earlier scripts from this thread.
- Use only packages already installed in {sci_python}. You cannot install packages or reach the
  internet. If something you need is missing, say so.

## Choosing data
- Use the project brief first, then list {data_path}, to decide which file(s) fit the request.
  To see a file's structure (columns, coordinates, sizes) without loading it, run:
  {sci_python} {tools_dir}/inspect_data.py <path>
- If exactly one file clearly fits, use it. If two or more plausibly fit and the choice would
  change the result, ask one short question listing the options instead of guessing.
- Start every final reply with the data you used, for example:
  "Data: `processed/liver_B_qc.h5ad` (sample B, 612,408 cells)".

## Large files
- Files can be tens of GB. Open .h5ad files with anndata.read_h5ad(path, backed="r") and load only
  the obs columns and obsm entries you need. Don't load the expression matrix unless the request
  needs it, and subset first if it does.
- For scatter plots with more than about 200k points, use small markers with rasterized=True (or
  datashader if installed). Say so if you subsampled for display.

## Figures and scientific choices
- Save figures as PNG at 200 dpi (also PDF if asked). Label axes and legends in plain words.
  Follow any figure conventions in the project brief.
- State every choice that changes the result: sample subsets, filtering, normalization, neighbor
  counts, clustering method and resolution, thresholds, subsampling for statistics, random seeds.
  If neither the user nor the brief specifies one, use a standard default and say what you picked.
  If the choice is major, ask first.

## Replying in Slack
- Keep replies short: the data used, what you did, what the figure shows, and any caveats.
- Use Slack formatting: *bold*, bullet lists, `code`. No markdown headings or tables.
- Report numbers exactly as computed. Never describe a result you didn't compute.
- If something fails, say what failed in one or two sentences and what you would try next.

# Project brief ({project_slug})
{project_md}
```

### 7.2 Init prompt (used by the `init` command as the user message)

```
Survey the data folder {data_path} and draft a project brief.
1. List the folder recursively. Skip hidden files and any outputs/ folders.
2. Run {sci_python} {tools_dir}/inspect_data.py on each data file (.h5ad, .csv, .tsv, .parquet,
   .loom). For folders with many similar files, inspect a few representative ones.
3. Write outputs/PROJECT.md following PROJECT.template.md in your working directory. Fill in what
   you can infer. Mark guesses with "(guess, please confirm)". Write TODO where you can't tell.
4. Reply with a 3–5 line summary of what you found and the questions the team should answer in
   the brief.
```

---

## 8. Slack behavior

### 8.1 Slack app setup (document this in the README)

- Enable **Socket Mode**. Create an app-level token with the `connections:write` scope (this is
  `SLACK_APP_TOKEN`).
- Bot token scopes: `app_mentions:read`, `chat:write`, `channels:history`, `groups:history`,
  `files:read`, `files:write`, `reactions:write`, `users:read`.
- Event subscriptions (bot events): `app_mention`, `message.channels`, `message.groups`.
- Invite the bot to each project channel.

Use `slack_bolt.async_app.AsyncApp` with
`slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler`.

### 8.2 Routing rules

1. Ignore messages from bots (including itself), edits, deletions, and other `subtype`s, except
   `file_share`, which is needed for brief uploads.
2. If `AGENT_ALLOWED_USER_IDS` is set, ignore everyone else.
3. **`app_mention`:**
   - The text starts with `help`, `init`, or `brief` → run that command (8.3).
   - The channel isn't bound to a project → reply: "This channel isn't linked to a project yet.
     Run `@analysis-bot init <project-slug> gs://bucket/path/`."
   - Otherwise → start or continue a session. The thread key is `thread_ts` if the message is in
     a thread, else the message's own `ts`, which then becomes the thread root.
4. **Plain `message` in a thread** that already exists in SQLite → continue that session. No
   mention is needed.
5. Everything else → ignore.

### 8.3 Commands

- `help`: list the commands and show the channel's project slug and data location.
- `init <slug> <gs://bucket/prefix/>`:
  - Validate that the slug matches `^[a-z0-9-]{3,40}$` and the bucket is listed in
    `AGENT_DATA_BUCKETS`.
  - Save `project.json`, then run an agent turn with the init prompt in a fresh thread workspace.
  - Post the summary and offer: "Reply with details or corrections and I’ll revise this draft.
    Reply `save brief` when you want new threads to use it. No Markdown upload needed."
  - Unknowns may remain TODOs. Update the draft throughout the conversation with user-provided
    information and verified discoveries; preserve unrelated content and label uncertainty.
  - Refuse if the channel is already bound, unless the user adds `--force`.
- `save brief` (reply in any thread with a draft): publish that thread’s current draft to
  `projects/<slug>/PROJECT.md` in the agent bucket.
- `brief` with no file: upload the current `PROJECT.md` to the thread.
- `brief` with an attached `.md` file: download it using the `url_private_download` URL with an
  `Authorization: Bearer <bot token>` header, check that it's valid UTF-8 and under 100 KB, and
  save it as `PROJECT.md`. Reply with "Brief updated. New threads will use it."

### 8.4 Replies and status

- On start, post one status message in the thread ("Working on it…"). Update it with
  `chat_update` at most every 5 seconds using `describe_tool()` text. At the end, update it to
  "Done in 1m 12s" or "Stopped: <reason>".
- Post the final text as a new thread message. Split it into chunks of at most 3,500 characters.
  Run it through `slack_format.to_mrkdwn()`, which converts `**bold**` → `*bold*`, strips `#`
  headings, and turns markdown links `[t](u)` into `<u|t>`.
- Upload the files that are new or changed in `outputs/` this turn using
  `client.files_upload_v2(channel=..., thread_ts=..., file_uploads=[...])`. Upload `.png`,
  `.jpg`, `.pdf`, `.svg`, `.csv`, `.tsv`, `.md`, and `.txt`. Skip files over 200 MB with a note.
- Errors: post a short plain message ("I hit an error while running this: <one line>.") and log
  the details with the thread key. Never post stack traces.

---

## 9. Projects and the brief

### 9.1 Storage layout (agent bucket)

```
gs://<agent-bucket>/projects/<slug>/project.json
gs://<agent-bucket>/projects/<slug>/PROJECT.md
gs://<agent-bucket>/projects/<slug>/runs/<thread_ts>/turn-<n>/...
```

`project.json`:
```json
{
  "slug": "liver-spatial",
  "slack_channel_id": "C0123ABCD",
  "data_uri": "gs://lab-data/liver-spatial/",
  "created_by": "U0456EFGH",
  "created_at": "2026-10-01T15:04:05Z"
}
```

- At startup, load every `project.json` into an in-memory map `channel_id → Project`. Reload
  after `init`.
- `data_path` (local) = `{AGENT_DATA_MOUNT_ROOT}/{bucket}/{prefix}`.
- At the **start of each new thread**, fetch the latest `PROJECT.md`. Existing threads keep the
  brief they started with, because it's already in their session context. If there's no brief
  yet, use: "No brief yet. Rely on listing and inspecting the data folder."

### 9.2 `templates/PROJECT.template.md`

```markdown
# <Project name>

One or two sentences on what this project studies.

Data location: gs://<bucket>/<prefix>/   (read-only)

## Datasets
| File | Samples | Assay / platform | Cells or rows | Notes |
|---|---|---|---|---|
| raw/example_all.h5ad | A–D | Xenium | 2.4M | All samples combined, unfiltered |
| processed/example_B_qc.h5ad | B | Xenium | 612k | QC-filtered. Use this for sample B. |

## Column meanings
- Sample ID: `obs["sample_id"]`
- Cell type: `obs["annotated_cell_type"]`
- Spatial coordinates: `obsm["spatial"]` (microns)

## Defaults
- Prefer processed/ files over raw/ when both exist.
- Figure style: <colors, fonts, sizes, if the team has conventions>

## Caveats
- <for example: "Sample D has a staining artifact in the upper-left region; exclude it unless asked.">

## Glossary
- <lab shorthand the agent should understand, for example "PP zone = periportal">
```

---

## 10. Data access

### 10.1 Mounting buckets (gcsfuse, read-only)

Mount each data bucket at `/mnt/gcs/<bucket>` at boot using systemd mount units or a oneshot
service run as root, with `-o ro,allow_other` so both the bot user and the sandboxed agent can
read it. Enable the gcsfuse file cache on a large local disk. Random reads into multi-GB `.h5ad`
files are slow without it.

`deploy/gcsfuse.yaml` (verify key names against the installed gcsfuse version's docs):
```yaml
implicit-dirs: true
cache-dir: /mnt/cache/gcsfuse
file-cache:
  max-size-mb: -1
  cache-file-for-range-read: true
```
Mount: `gcsfuse --config-file /etc/gcsfuse.yaml -o ro,allow_other <bucket> /mnt/gcs/<bucket>`

### 10.2 `tools/inspect_data.py`

A CLI the agent runs to describe a file **without loading large matrices**. It runs under
`/opt/sci/bin/python`.

```
usage: inspect_data.py PATH [--max-categories 20] [--json]
```

For `.h5ad`:
- File size. `n_obs` and `n_vars` from the shape attributes in HDF5, using `h5py`. Never read `X`.
- `obs` columns: name, dtype, number of unique values, the top categories with counts (up to
  `--max-categories`), and min/max for numeric columns. Read only the `obs` group, either via
  `anndata`'s element reader for the installed version or directly with `h5py`, handling
  categorical encoding.
- Key, shape, and dtype for each entry in `obsm`, `layers`, and `varm` (shapes only, no data).
  Top-level `uns` keys.
- Guesses printed at the end: likely sample column, cell-type column, spatial key.

For `.csv`/`.tsv`: columns, dtypes from the first 10,000 rows, the first 5 rows, and the file
size. For `.parquet`: schema and row count from metadata, plus the first 5 rows.
For folders: a summary of the files inside.

Output is readable plain text by default, and JSON with `--json`.

(If you have access to the old Mica repo, `src/mica/services/datasets.py` contains a
working metadata-only `.h5ad` reader you can adapt.)

---

## 11. Outputs and provenance

After each turn, upload to `gs://<agent-bucket>/projects/<slug>/runs/<thread_ts>/turn-<n>/`:
- every new or changed file in `outputs/`
- a snapshot of `code/`
- `run_log.jsonl`: one line per tool call with the tool name, the input (string fields truncated
  to 10 KB), and a timestamp
- `meta.json`: user ID, prompt text, session ID, model, cost (`total_cost_usd`), duration, error
  (if any), the brief's GCS generation number, and `datasets_touched` (paths under `data_path`
  that appear in Read/Bash inputs)

This gives every figure a record of the code, data, and settings that produced it, without
extra work from the agent.

---

## 12. Security and cost controls

| Control | How |
|---|---|
| Data can't be modified | gcsfuse mount is `ro`, and the VM service account has only `roles/storage.objectViewer` on the data buckets |
| Minimal cloud permissions | The VM service account gets: objectViewer on the data buckets, objectAdmin on the agent bucket, log writer. **No Secret Manager access at runtime, no other roles.** |
| Generated code has no network | The SDK command sandbox (section 6.2) with `failIfUnavailable: True` and `allowUnsandboxedCommands: False`. Don't add allowed network domains. |
| Slack tokens hidden from the agent | `env` override blanks `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` for the agent subprocess |
| Only intended people and channels | The bot responds only in channels bound to a project, plus the optional user allowlist |
| No surprise tool use | `permission_mode="dontAsk"`, explicit `allowed_tools`, web and subagent tools disallowed |
| Spend limits | `max_budget_usd` and `max_turns` per message, `AGENT_MAX_CONCURRENT_RUNS`, cost logged to `meta.json` |
| Model refusals | `claude-opus-5-5` has broader safety classifiers, including a bio category. Routine analysis should pass, but `fallback_model` covers failures, and refusals are logged (`stop_reason == "refusal"`) so they can be reviewed. |

The sandbox needs `bubblewrap` and `socat` on Linux. **Milestone 5 must verify that sandboxed
Bash can read `/mnt/gcs/...` and write only inside the workspace.** If the sandbox can't read the
FUSE mount even with `allow_other`, stop and report to the human. Don't turn the sandbox off.

Secrets are written once at provisioning time to `/etc/slack-agent/secrets.env` (mode `0600`,
owned by the service user) and loaded by systemd `EnvironmentFile=`.

---

## 13. Deployment (one GCE VM)

- **Machine:** `n2-highmem-8` (8 vCPU, 64 GB RAM) to start. Plotting 2–3M points from backed
  `.h5ad` files fits comfortably. Increase the size if the team loads full expression matrices.
- **OS:** Ubuntu 24.04 LTS. It ships a recent `bubblewrap`, which the sandbox depends on.
- **Disks:** 50 GB boot disk, plus a 500 GB balanced persistent disk mounted at `/mnt/cache` for
  the gcsfuse cache. It must hold the largest files analyzed together. Sessions, workspaces, and
  SQLite live on the boot disk under `/srv/slack-agent`.
- **Network:** outbound only (Slack WebSocket, `api.anthropic.com`, Google APIs). No inbound
  ports, no load balancer, no public URL.
- **`deploy/setup_vm.sh`** (idempotent):
  1. `apt install bubblewrap socat python3.12-venv fuse3` and install gcsfuse from Google's apt repo.
  2. Create the service user `slackagent` and the directories `/srv/slack-agent`,
     `/opt/slack-agent`, `/mnt/gcs`, `/mnt/cache`.
  3. Create `/opt/sci` with `requirements-sci.txt`. Create `/opt/slack-agent/.venv` with the bot
     package. Copy `tools/` to `/opt/slack-agent/tools`.
  4. Install the gcsfuse config and one mount per bucket in `AGENT_DATA_BUCKETS`.
  5. Install `slack-agent.service` (`User=slackagent`, `Restart=always`,
     `EnvironmentFile=/etc/slack-agent/secrets.env`) and the cleanup timer.
- Update: `git pull`, reinstall the package, then `systemctl restart slack-agent`. Sessions
  survive restarts because transcripts and SQLite are on disk.

---

## 14. Milestones and acceptance criteria

Each milestone must pass `pytest` and satisfy its acceptance checks before moving on.

**M1: Local agent, no Slack, no GCS**
- `config.py`, `prompts.py`, `agent_runner.py`, `tools/inspect_data.py`, `tests/make_fixture.py`
  (a synthetic `.h5ad`: 3 samples, about 2,000 cells each, `obs["sample_id"]`,
  `obs["annotated_cell_type"]` with 5 types, `obsm["spatial"]`).
- `cli.py`: `python -m slack_agent.cli --data ./fixture_data --brief ./PROJECT.md --workdir ./tmp/t1 "plot sample B colored by cell type"`
  prints the reply and the output files. `--resume` continues the last session in that workdir.
- The sandbox can be off locally (a `--no-sandbox` flag for macOS dev only).
- ✅ The PNG exists in `outputs/`, and the reply starts with "Data: …".
- ✅ A second call with `--resume "make the points bigger"` edits the earlier script instead of
  starting over.

**M2: Slack**
- `slack_app.py`, `sessions.py`, `slack_format.py`, `main.py`. Temporarily bind one channel to
  the fixture data through an env var.
- ✅ A mention creates a status message, a reply, and a PNG in the thread.
- ✅ A thread reply without a mention continues the same session.
- ✅ Two messages sent quickly to one thread run in order. Messages in two different threads run
  in parallel.
- ✅ Retried Slack events are not processed twice.
- Tests: handler tests with a `FakeRunner` that yields canned results, plus chunking and mrkdwn
  conversion.

**M3: Projects and briefs**
- `storage.py` (GCS and local), `projects.py`, the commands in section 8.3.
- ✅ `init` binds the channel, posts a sensible draft brief for the fixture, and `save brief`
  stores it.
- ✅ An uploaded `.md` replaces the brief. New threads use it and existing threads don't change.
- ✅ A channel that isn't bound gets the "not linked" message.

**M4: GCS and deployment**
- gcsfuse mounts, `outputs.py` provenance upload, `deploy/` files, README deploy section.
- ✅ On the VM, a real project run writes the outputs, `run_log.jsonl`, and `meta.json` to the
  agent bucket.
- ✅ The service restarts cleanly and an old thread still resumes.

**M5: Hardening and real-data acceptance**
- Sandbox enabled and verified (section 12), env blanking verified (the agent running `env`
  shows no Slack tokens), budgets enforced, cleanup timer working.
- Run these on a real project and paste the results into `docs/acceptance.md`:
  1. "What datasets do we have?" → an accurate summary from the brief and the folder.
  2. "Show the spatial layout of each sample colored by annotated cell type."
  3. "Compute neighborhood composition using the 20 nearest spatial neighbors within each
     sample, cluster it with Louvain at resolution 0.5, and plot the neighborhood clusters in
     space." → States k, the resolution, and the seed. Uses backed loading. Finishes without
     running out of memory.
  4. Follow-up: "Now use resolution 1.0 and compare the two." → Reuses the earlier script.
  5. A request that matches two files → the agent asks which one instead of guessing.
  6. "Delete the raw folder." → The agent refuses or fails harmlessly, and the data is unchanged.

---

## 15. Testing guidance

- Unit tests: `sessions.py` (SQLite round trip), `projects.py` with `LocalStorage`, output change
  detection, `slack_format.py`, command parsing, the `inspect_data.py` output on the fixture.
- Slack handlers are tested with a fake Slack client and `FakeRunner`. Tests make no network calls.
- One opt-in live test (`pytest -m live`, skipped unless `ANTHROPIC_API_KEY` is set) runs M1's
  acceptance check against the real API.

---

## 16. Out of scope for v1 (possible later additions)

- **GPU and heavy jobs on Modal:** add a custom tool (the SDK's in-process MCP tools) called
  `submit_modal_job` that requires a Slack approval button above a cost threshold.
- **Skills for lab tools** (for example Starfysh, Decipher, Echidna, Amici): add
  `.claude/skills/<tool>/SKILL.md` files and enable the `project` setting source and
  `skills="all"`.
- **Anthropic-hosted agent loop** (Claude Managed Agents with a self-hosted sandbox), if running
  the VM becomes a burden.
- Scheduled reports, interactive figures, a web dashboard.
