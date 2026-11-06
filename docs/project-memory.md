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

## Shared thread memory

Project conversation memory is shared with every member of that project, including summaries
of DMs, channels and email. Repository readers can also see it. Memory contains attributed
paraphrases, never verbatim message logs. The four most recent exchanges remain in the GCS
checkpoint as temporary working context. Commands and cancelled turns do not add memory.

Each project has an application-generated `THREADS.md` overview and a folder under
`threads/<thread-key>/` containing `summary.md`, `log.md`, older `log-NNNN.md` pages, and
`scripts/`. Small retained outputs live in `outputs/turn-N/`; `outputs/MANIFEST.json` records
copy receipts and reasons a source was retained. Model-authored thread content is a compact
entry and summary. The application supplies identity, sequence, event id and summary coverage.
Worker proposals use `knowledge/threads/<this-thread>/turn.json`; other threads are read-only.

Memory saves run after delivery. A late save appends its event once, even if a newer turn saved
first. A newer summary cannot discard the previous summary's covered events. Readers include
entries not covered by the summary regardless of their sequence, including entries in older
log pages. Prompt limits produce explicit continuation paths rather than a claim of complete
coverage. Thread log pages preserve each compact event; Git history preserves replaced summary
and script versions without duplicating the growing log into the project archive each turn.

A save is pending before its first queue dispatch. A read-back must verify the compact entry
and script manifest before the event is marked done. Pending proposals live under
`projects/_threads/<thread>/pending/<event>/`, outside expiring task transport. Daily maintenance
re-dispatches pending saves with a new task id and reports saves pending for more than 24 hours.
Capacity rejection, conflicts and unavailable GitHub never block the user's completed turn or
silently discard its recovery handoff. Rejected scripts retain their source and a reason.
Failed routed turns may update pending work in `open.md` and their failure entry; partial
results are not promoted as verified findings.

The router reads project context before thread context and recent exchanges. `read_sources`
accepts `memory_paths` for project files and cross-thread lookup under its existing 384 KiB
content budget. Workers receive bounded text snapshots, including log pages, and the current
thread's scripts. GitHub requests fetch tree metadata first and selected text in batches;
other threads' scripts and all output blobs are excluded. Explicit artifact reads verify blob
identity and byte counts. Workers receive no GitHub credential. The working archive supplies
`code/` while available; otherwise the current thread's GitHub scripts restore it.

Thread limits are separate from the project's 4 MiB/500-file text limits:
`AGENT_THREAD_TEXT_KB=4096`, `AGENT_THREAD_MAX_FILES=4000`,
`AGENT_THREAD_SUMMARY_KB=64` (including coverage metadata), `AGENT_THREAD_LOG_KB=32`,
`AGENT_THREAD_SCRIPT_KB=256`, `AGENT_THREAD_SCRIPTS_KB=2048`,
`AGENT_THREAD_OUTPUT_KB=1024`, and `AGENT_THREAD_OUTPUTS_KB=20480`.
Thread text and file caps apply per project; script/output caps apply per thread. Snapshots and
proposals also have bounded transport envelopes. Storage exhaustion requires an operator
capacity decision; compaction does not silently purge history.

## Thread migration and retention

`AGENT_THREAD_MEMORY=dual` is the coordinated-release default: write GitHub and legacy GCS
history, read legacy thread context. `github` reads GitHub while continuing both writes, so
rollback to `dual` preserves the history written during that phase. `github_only` stops GCS
history reads/writes and enables verified expiry. It requires a fresh operator cutover receipt;
retention markers for rejected artifacts cannot excuse missing conversation history. Do not
roll back from `github_only` expecting new turns to exist in GCS history.

Preview every existing project thread, including inactive threads, before importing:

```bash
.venv/bin/python tools/memory_import.py --bucket ARTIFACT_BUCKET --threads
.venv/bin/python tools/memory_import.py --bucket ARTIFACT_BUCKET --threads --apply
```

Use `--project PROJECT_ID --thread THREAD_KEY` to scope a rehearsal. The preview reports
history and checkpoint-only counts, raw-input and cumulative-context token estimates, and
maximum model calls; model pricing determines actual cost. Apply
uses the configured memory model and credential. It caches paraphrases, records source
versions/hashes, verifies imported events and scripts, and is safe to repeat. A changed source
or resumed thread requires rechecking coverage. Unknown legacy speakers stay explicitly unknown.

Script migration reads archive members without extracting a workspace. Defaults bound a scan
to 64 MiB compressed (`AGENT_THREAD_ARCHIVE_SCAN_MB`), 128 MiB expanded
(`AGENT_THREAD_ARCHIVE_EXPANDED_MB`), 10,000 members, and 30 seconds
(`AGENT_THREAD_ARCHIVE_SCAN_SECONDS`). Oversized archives are not downloaded. Valid scripts
found before a limit can be saved, but the archive remains with a retention reason. A rejected
archive never blocks verified conversation import and never receives deletion authorization.
New GitHub-only threads establish coverage from verified native event receipts; raw legacy
history still requires its own import receipt.

After the read-mode soak, use `tools/memory_import.py --bucket ARTIFACT_BUCKET --verify-cutover`
to recheck exact checkpoint generations, imported content, active/recovering work, and pending
saves. At the quiet cutover boundary, add `--apply` to persist a complete report at
`projects/_registry/thread-memory-cutover.json`. Set `AGENT_THREAD_MEMORY_CUTOVER_AT` to that
report's exact `checked_at` value when enabling `github_only`. A stale migration report or a
missing/mismatched cutover receipt cannot enable this mode. The operator step does not modify
the service itself.

The existing authenticated daily maintenance route moves Mica-created run outputs after
`AGENT_THREAD_OUTPUT_AGE_DAYS=10`, subject to the per-file and per-thread caps. It never moves
`outputs/gcs/` or user destinations. `AGENT_THREAD_EXPIRY_DAYS=30` begins only after a verified
copy exists. Deletion rechecks the GitHub content and exact GCS generation. A thread intake
lease and terminal-state check protect cleanup from new, active and recovering work.
Each maintenance pass rotates through at most five threads and four workspace archives per
thread. Archive verification uses the same scan limits as import; uncovered threads are skipped.

A workspace archive gets an expiry receipt only when every retained script and output has a
matching GitHub copy. Unmatched/oversized members and legacy SDK transcripts keep the source
archive. After expiry, an inactive current pointer is conditionally detached before deleting
its exact generation; the next worker restores scripts from GitHub. Imported history deletes
only with a matching compact-event receipt. No Terraform, secret, IAM or bucket-lifecycle
change is part of this workflow.

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
