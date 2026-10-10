# GitHub project memory

Project memory uses a private GitHub repository, with one folder per project at
`projects/<project_id>/`. Enable it with `AGENT_MEMORY_REPO=owner/repository`,
`AGENT_MEMORY_BRANCH=main`, and `AGENT_MEMORY_TOKEN_SECRET` set to a Secret Manager
version resource (`projects/<gcp-project>/secrets/<name>/versions/latest`).
Initialize the branch before use. Give its fine-grained GitHub token Contents read/write
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
