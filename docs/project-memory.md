# GitHub project memory

Project memory uses a private GitHub repository, with one folder per project at
`projects/<project_id>/`. Enable it with `AGENT_MEMORY_REPO=owner/repository`,
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
200 files, and 1 MiB per project. Secret-like content and unsafe paths are rejected.

Run the live store check with `.venv/bin/python tools/memory_smoke.py`. For local setup,
`--github-cli` uses the existing GitHub CLI login without printing or saving its token.
The check writes only to `projects/_smoke/`, checks read-back and conflict preservation,
prints commit IDs, then removes those test files.

Administrators can edit files in GitHub or use `git revert <sha>`; edits take effect on
the next read. Purging Git history with `git filter-repo` and a force-push is a human-only
operation. Anything said about a project, in a DM or a channel, may be remembered for
everyone using that project, and repository readers can see it.

## Turns and viewing memory

Each completed turn has one writer: a forced-tool editor for chat, or the worker's
workspace proposal for computation. `init` commits its guide immediately. Workers receive
an immutable snapshot in the request, edit `knowledge/`, and return a proposal through GCS;
they never connect to GitHub. Recovery keeps the same snapshot and any pending edits.
New turns read the current repository head. Editing memory or sharing another project does
not reset SDK sessions, messages, attachments, or saved working directories.

Use `memory` for the index and active work (or a file listing until an index exists), `memory conventions.md` for a file, or `brief`
for the guide. Correct mistakes in plain language. The old `remember`, `forget`, `knowledge`,
`save brief`, and `clean brief` commands explain this change without editing memory.
Memory saves run silently; use `memory` or `brief` when you want to inspect saved content.

## Compaction and operations

After a turn, compaction is queued once per head when any limit exceeds 80%, conflicts or
rejections exist, or the project reaches a multiple of 20 commits. A forced-tool editor
merges duplicates and conflict copies, preserves sources and working recipes, and reaches
70% of every limit with one index link per entry. Invalid output commits nothing. A newer
head supersedes a pending compaction, so it cannot overwrite a concurrent conversation.
Compaction runs silently. Model inputs, proposals and receipts are cached as temporary GCS
task state so retries use the same inputs and do not duplicate commits.

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
IDs, counts, and failure types. Failed background operations return 503 for Cloud Tasks
retry. Unreadable or invalid repository files need an administrator's correction before
compaction; their contents are never sent to the model.

See [staging verification](project-memory-staging.md) for deployed commit IDs,
live save/recall and compaction evidence, and known verification limits.
