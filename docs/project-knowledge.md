> Superseded by [GitHub project memory](project-memory.md). The GCS knowledge store and its commands are removed. The material below describes the previous implementation.

# Project knowledge

Project knowledge lets analysis workers retain verified data facts, results and their definitions,
working recipes, pitfalls, corrections, and open questions within a project. Knowledge is always on; GitHub mirroring remains optional.

## Configuration

| Environment variable | Default |
| --- | --- |
| `AGENT_KNOWLEDGE_INDEX_CHARS` | `4000` |
| `AGENT_KNOWLEDGE_FILE_KB` | `16` (scripts have a fixed 64 KB limit) |
| `AGENT_KNOWLEDGE_MAX_FILES` | `200` |
| `AGENT_KNOWLEDGE_TOTAL_KB` | `1024` |
| `AGENT_KNOWLEDGE_ROUTER_CHARS` | `6000` |
| `AGENT_KNOWLEDGE_GITHUB_REPO` | empty, mirror disabled |
| `AGENT_KNOWLEDGE_GITHUB_BRANCH` | `main` |
| `AGENT_KNOWLEDGE_GITHUB_TOKEN_SECRET` | empty; full Secret Manager version resource |

The worker receives the knowledge limits, plus a boolean indicating whether unmirrored
history must be retained. It receives neither the GitHub token nor its secret resource or repository
configuration. The analysis sandbox receives no new credentials or network access.

The project guide is `knowledge/PROJECT.md` (100,000 bytes, no provenance header). Chat
corrections and init save it immediately; worker edits save through the same versioned path.
`brief` shows the current guide and `undo knowledge` reverts the latest save. The retired
`save brief` and `clean brief` commands explain automatic saving. Legacy published guides migrate
once under the project lock, with `source: migrated brief`; the original object is retained.
Unsaved thread drafts are not migrated.

## Live files, validation and history

GCS remains authoritative at `projects/{project_id}/knowledge/`. Workers replace any restored
workspace `knowledge/` directory with the current project copy at the start of an attempt.
Knowledge is excluded from full and partial thread archives. Recovery snapshots carry a separate
bounded, checksummed diff of pending edits and their original base generations. Restoring those
edits preserves conflict detection against newer shared knowledge.

Allowed paths are `PROJECT.md`, `INDEX.md`, `lessons.md`, `open.md`, `data/<topic>.md`, `results/<topic>.md`,
and `recipes/<name>.md`, `.py`, `.R`, or `.sh`. Markdown entries other than the index and project guide begin with
`type:`, `source:`, and `status:` lines. The save path checks allowed names, ordinary files,
UTF-8, provenance headers, secret patterns, and file/count/total limits. Invalid replacements
are rejected rather than being interpreted as deletions. All prompt inclusions identify saved
knowledge as untrusted advisory data. Patient identifiers and raw data rows are prohibited by
the agent instructions; the secret scan is not a patient-identifier classifier.

Each save uses per-file generation checks and a short project lease to keep aggregate limits
consistent. Competing versions are preserved in `_unmerged/` and linked from the index when its
limits allow. A rejected conflict copy remains reviewable in its history when the active folder
is full. Notices report rejected paths; the attempt record records reads, writes and the save key.
The control plane delivers notices with durable receipts after the worker checkpoints, so a
Slack outage cannot interrupt saving the thread workspace.
Cancellation places a marker that prevents a finishing worker from saving knowledge.

History lives at `projects/{project_id}/knowledge-history/{stamp}/`:

- `before/<path>` preserves replaced/deleted content.
- `after/<path>` preserves the exact saved content for delayed mirroring, independently of GCS
  object versioning or later changes to live files.
- `CHANGES.json` records operations, saved generations, event, attempt, member key, time,
  conflict/rejection information and mirror receipts.

A write-ahead journal and durable operation receipts recover interrupted saves before subsequent
saves. History keeps the newest 100 completed saves. When mirroring is configured, older pending
mirror payloads are retained until delivered, so a mirror outage does not destroy its backlog.
Operation receipts remain after history pruning to prevent old retries from reapplying edits.

## Slack controls

Project access is checked before reading or changing knowledge, including in published channels.

- `knowledge`: index, file count and last update.
- `knowledge show <path>`: first 3,500 characters of an entry.
- `forget knowledge <path>`: remove the entry and its index link, retaining history.
- `undo knowledge`: restore the files changed by the latest save, with generation checks.
- `knowledge github on` / `knowledge github off`: owner controls for mirroring.

Shared and published projects mirror by default when a repository is configured; private projects
require owner opt-in. An explicit `off` also overrides automatic mirroring for shared projects.
Sharing notices explain that members see the project's knowledge. GitHub deletion confirmations
explain that older content remains in Git history. Human edits on GitHub do not sync back.

The router receives the bounded index plus at most two Markdown entries ranked by word overlap
and date. It may answer questions about recorded facts with their dates; current data, new numbers,
and figures still require computation.

## Background tasks

Memory editing now runs through `POST /api/v1/internal/slack-analysis/memory/{event_id}` after
reply delivery and worker-slot release. It uses a 90-second HTTP timeout inside a 150-second task
budget. Existing summary/edit receipts suppress duplicate model calls and notices. Transient
failures propagate to the task dispatcher for retries; logs contain the exception type, not its
message or conversation content. Each invocation emits one `memory_editor` outcome (`saved`,
`unchanged`, `skipped:<reason>`, or `failed:<Type>`) with summary length and saved edit count.
Channel summaries are shared across threads within the same project/channel; DM summaries remain
private per person/project. Routine personal edits do not reset either conversation cache.

Mirroring uses `POST /api/v1/internal/slack-analysis/knowledge-mirror/{project_id}/{stamp}`.
Both endpoints require the configured Cloud Tasks dispatcher identity. The existing maintenance
endpoint re-enqueues complete, unmirrored saves older than ten minutes.

Mirror tasks process retained saves in stamp order, require a private repository, scan content
again, and use the [GitHub Git database API](https://docs.github.com/en/rest/git) to create blobs,
trees and commits. The first mirror includes older unchanged knowledge files, reconstructing
the saved state from history so it excludes later results. Subsequent commits contain the
changed paths. Branch changes trigger a rebuild on the current head. A saved candidate SHA
and ancestry check recover a lost branch-update acknowledgement without a duplicate reachable
commit. Model text never becomes the commit message or author. The configured bot identity is
used; messages contain only project name, counts, date and event identifier.

## Admin restore

`tools/knowledge_restore.py` previews a repository commit by default. It reads the configured
private repository and Secret Manager credential, validates the snapshot using the same rules,
and applies a diff through normal knowledge history only when `--apply` is supplied.

```sh
.venv/bin/python tools/knowledge_restore.py \
  --project p_0123456789abcdef \
  --commit <full-40-character-commit-sha> \
  --actor admin@example.org
```

Add `--apply` to perform the reviewed restore. The tool supports `AGENT_STORAGE=local` and
`AGENT_LOCAL_STORAGE_ROOT` for local storage; otherwise it uses the configured GCS bucket.

## Deployment and verification

The earlier knowledge release passed 705 local tests. Current implementation and verification
are tracked in [the routing and memory plan](worker-routing-and-memory-plan.md). The coordinated
release must build API and workers from one source commit, drain old jobs, preserve
existing environment and secret mappings, and check authenticated endpoint boundaries
before promoting traffic. Deployment evidence is saved under `.pistachio/deployments/`.

Before enabling the mirror, an administrator must create a private repository with an initialized
branch, configure a separate token with contents write access to that repository, and make its
Secret Manager version readable only by the control-plane identity. Never reuse the existing
read-only GitHub credential or grant the mirror secret to workers.

The staging queue permits five attempts over one hour with 10–300 second backoff. Existing
control-plane and worker storage grants permit knowledge/history writes and conditional deletes
within the project prefix; this release needs no broader storage permissions. The plan's live Slack evaluation, slow-model observation, GCS
inspection and GitHub smoke test remain deployment-stage checks. The memory model can be changed
with `AGENT_MEMORY_MODEL` if observed latency warrants it.
