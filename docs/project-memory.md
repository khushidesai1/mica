# GitHub project memory

Project memory uses a private GitHub repository, with one folder per project at
`projects/<project_id>/` initially, or its renamed folder below. Enable it with
`AGENT_MEMORY_REPO=owner/repository`,
`AGENT_MEMORY_BRANCH=main`, and `AGENT_MEMORY_TOKEN_SECRET` set to a Secret Manager
version resource (`projects/<gcp-project>/secrets/<name>/versions/latest`).
Set `AGENT_MEMORY_MODEL` to a model supporting forced tool choice (default: `claude-opus-5`).
The deployed `claude-opus-5-5` rejected forced tools in live verification; routing and analysis
can keep using that model. Initialize the branch before use. Give its fine-grained GitHub token Contents read/write
on this repository only and give the control service access to the secret. Tokens are
cached for ten minutes and must never be passed to workers.

The store reads an immutable GitHub snapshot and commits with an expected branch head.
Concurrent changes to different files survive; conflicting updates go under
`_unmerged/<event>/<path>` for review and compaction. Unavailable reads return no memory;
failed writes raise so the caller can retry. Logs contain IDs, counts, and outcomes,
never file contents.

Allowed files: `PROJECT.md`, `INDEX.md`, `conventions.md`, `open.md`, `lessons.md`,
`data/*.md`, `results/*.md`, and `recipes/*.{md,py,R,sh}`. Markdown entries require
`type:`, `source:`, and `status:` headers, except the guide and index. Limits are
4,000 index characters, 16 KiB per Markdown entry, 100,000 guide bytes, 64 KiB per recipe,
500 files, and 4 MiB per project, including archives and generated indexes. Secret-like content and unsafe paths are rejected.

Run the live store check with `.venv/bin/python tools/memory_smoke.py`. For local setup,
`--github-cli` uses the existing GitHub CLI login without printing or saving its token.
The check writes only to `projects/_smoke/`, checks read-back and conflict preservation,
prints commit IDs, verifies paged archives and exact originals, then explicitly removes the
isolated test folder using a head-fenced administrative commit. Ordinary writers cannot erase archives.

Administrators can edit files in GitHub or use `git revert <sha>`; edits take effect on
the next read. Purging Git history with `git filter-repo` and a force-push is a human-only
operation. Anything said about a project, in a DM or a channel, may be remembered for
everyone using that project, and repository readers can see it.

## Active entries and retained originals

The application generates `INDEX.md` from active entries. It reserves core entries and gives
new or changed entries priority; overflow moves to the archive without changing its bytes.
`archive/INDEX.md` links to bounded `archive/index-0001.md` pages. Every overwritten or deleted
entry is retained at `archive/<sha256-of-original-path-and-content>/<original-path>`.
This includes recipes, conflict copies, guide updates, and entries demoted by compaction.
The original path, provenance headers, and exact contents remain available. Repeating a version
reuses its archive object; same-named entries from different directories cannot collide.

The active index is not a complete inventory. The 12,000-character router budget stays unchanged;
archive text is never automatically injected. Workers can Read the archive pages or Grep
`knowledge/archive/`, and users can request `memory archive/INDEX.md` or a specific archived path.
Archives can contain superseded facts: check the current entry and provenance before reuse.

All normal writers use the same atomic archive/index policy. They cannot edit archive entries.
If an update plus its retained originals cannot fit the hard storage ceiling, it is rejected
without removing any originals. Compaction does not free retained storage or purge history;
capacity exhaustion requires an operator's reviewed retention/capacity decision.

## Project renames

The owner’s `rename <project> <new-name>` command updates the registry name, then
atomically moves its GitHub memory to `projects/<new-name>/` and updates the guide
heading before reporting completion. The control-plane index `.mica/projects.json`
maps the stable project ID to that folder. If a folder belongs to another project,
Mica adds a stable suffix instead of mixing their memory. Access grants, conversation
checkpoints and saved run IDs remain unchanged.

Transient GitHub failures retry the same rename. A monotonic name revision prevents
an older retry from undoing a newer rename, and in-flight worker proposals resolve
the current folder when committed. Historical snapshots resolve the mapping at their
original commit. Deploy the mapping-aware API and drain older writers before migrating
an existing folder; do not move memory by hand without updating the index.

## Turns and viewing memory

Each eligible terminal turn has one writer: a forced-tool editor for chat, or the worker's
workspace proposal for computation. `init` commits its guide immediately. Workers receive
an immutable snapshot in the request, edit `knowledge/`, and return a proposal through GCS;
they never connect to GitHub. Recovery keeps the same snapshot and any pending edits.
New turns read the current repository head. Editing memory or sharing another project does
not reset SDK sessions, messages, attachments, or saved working directories.

Use `memory` for the index and active work (or a file listing until an index exists), `memory conventions.md` for a file, or `brief`
for the guide. Correct mistakes in plain language. The old `remember`, `forget`, `knowledge`,
`save brief`, and `clean brief` commands explain this change without editing memory.
Memory saves run silently; use `memory` or `brief` when you want to inspect saved content.

Failed requests remain in the thread's 12-exchange history with their original user message,
a short failure notice, and `success: false`. Follow-ups can refer to that intent. Existing
successful exchanges are preserved; commands and cancelled turns add no exchange or memory task.
Access checks still prevent reading or writing a project after access is lost.

A routed failure uses the background editor even if no worker proposal exists. It may update
only `open.md`, recording one attributed, dated line about pending work and why it stopped.
Retries update that line; a later success removes resolved work. Failures before routing stay
in thread history but do not enqueue shared-memory edits. Partial outputs and worker logs are
not saved as failed-turn results. Routine memory updates remain silent.

## Compaction and operations

After a turn, maintenance is eligible for unresolved conflicts, superseded entries, entries
above 85% of their individual limit, quarantined invalid files, or an outdated generated index.
Ordinary growth alone does not trigger a model call. Archive growth is not compaction pressure.
Maintenance works on at most 24,000 serialized characters, keeping each conflict and its canonical
entry together. It skips an individually oversized group without truncating it. The model may
change only selected paths and cannot delete core files. Originals are archived by application
code even when a model merge omits a fact. There is no whole-project reduction target.

A project lease coalesces concurrent tasks. Content fingerprints ignore unrelated projects'
commits. Model attempts are persisted before calls: at most two attempts per input and three
calls per project per hour, with a ten-minute cooldown between new inputs. Pending work resumes
after the budget window. Cached proposals survive transient commit failures. Invalid, oversized,
or truncated output is terminal for that input; it is never repeatedly purchased on retries.
A changed project snapshot supersedes a proposal before commit. Maintenance remains silent.

Controls: `AGENT_MEMORY_COMPACTION_ENABLED=false` stops maintenance alone;
`AGENT_MEMORY_COMPACTION_CHARS`, `AGENT_MEMORY_COMPACTION_ATTEMPTS`,
`AGENT_MEMORY_COMPACTION_CALLS_PER_HOUR`, and `AGENT_MEMORY_COMPACTION_COOLDOWN_SECONDS`
configure the bounded defaults above. Do not pause the shared analysis queue to contain memory.
Firestore connector state `memory-maintenance:<project-id>` records pending work, counters,
cooldown and outcome; `memory-maintenance-lock:<project-id>` is the short lease. GCS task state
under `tasks/slack-analysis/memory-compaction-v2/<project-id>/` caches inputs, attempts and output.
These records are diagnostics, not the authoritative memory store.

Deploy the archive-aware API and workers together. `AGENT_MEMORY_ARCHIVE_AFTER` is a Unix timestamp
(default 0) used for the first rollout: set it beyond the previous API request timeout plus a
safety margin. Until then, maintenance is disabled and normal writes use the previous layout;
originals remain in Git history. After activation, every new replacement uses the archive policy.
Once archive files exist, rollback must retain archive-compatible API and workers.

For migration, set the memory environment variables and run
`.venv/bin/python tools/memory_import.py --bucket <artifact-bucket>` to preview, then add
`--apply` once. Only existing GCS project guides are imported; GitHub guides are never
replaced. Repeating the import is safe. Legacy GCS objects can remain for rollback but are
no longer read during turns.

Verified folder layout belongs in the guide's `Data` section, where the router sees it on every
turn, rather than in compactable `open.md`. To create a missing guide from existing memory,
select reviewed, verbatim layout excerpts explicitly:

```bash
.venv/bin/python tools/memory_import.py --bucket <artifact-bucket> \
  --layout-from-memory <project-id> \
  --layout-fact 'open.md::Raw exports: `raw_outputs/raw/<TMA>/`'
```

Repeat `--layout-fact` for additional facts and add `--apply` after reviewing the preview.
Excerpts must occur in the current memory snapshot. The importer preserves the source files,
skips existing guides, and uses the normal conflict-preserving commit path with source `import`.
Do not select hypotheses, results, or counts for a layout guide.

Set `AGENT_MEMORY_AUTO_EDIT=false` to suspend background writes and compaction (explicit
`init` still saves a guide). Unset `AGENT_MEMORY_REPO` to disable all GitHub memory.
Inspect `memory_read`, `memory_commit`, `memory_editor`, and `memory_compaction` logs for
IDs, counts, and failure types. Transient failures return 503 for bounded Cloud Tasks retries. Terminal compaction validation
failures return success to acknowledge the task and log `exhausted:<rule>`. Inspect `deferred:*`,
`superseded`, `already_complete`, and quarantine/oversized-group outcomes before intervening.
Invalid repository entries stay quarantined for administrator review; their contents never
reach the model.

See [staging verification](project-memory-staging.md) for deployed commit IDs,
live save/recall and compaction evidence, and known verification limits.
