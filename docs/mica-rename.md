# Mica naming and compatibility

Mica is the new application name. Use `mica` for the Python package, `mica-api`,
`mica-analyze` and `mica-worker` for commands, and `MICA_*` for application settings.
The repositories are `khushidesai1/mica` and `khushidesai1/mica-memory`. Slack uses the
existing app with display name **Mica** and username **mica**; its app ID, callbacks,
permissions and conversation history stay the same.

## Existing installations

Old `PISTACHIO_*` settings remain accepted, including `.env` files. An explicit
`MICA_*` value takes precedence over the old spelling in the same settings source.
Existing `pistachio` imports and CLI commands forward to the current implementation.
Previously generated scripts receive both environment spellings; old progress messages
and already-posted Slack approval/transfer actions remain valid.

Local state and new script checkpoints use `.mica/`. Move an existing local
`.pistachio/` directory to `.mica/` before starting the updated application, and update
any explicit artifact-root setting. If both directories exist, reconcile their contents
before moving; do not overwrite either tree. Repair linked worktrees with
`git worktree repair .mica/worktrees/<name>` after moving them. Existing worker
checkpoints can still restore verified scripts from `.pistachio/scripts/`; their next
checkpoint uses `.mica/scripts/`.

Cloud Run, buckets, queues, service accounts, secrets and image repository IDs retain
their existing names. Container paths `/srv/pistachio` and `/opt/pistachio` remain
stable so pinned runtimes and saved scripts keep working.
SDK tool names, object metadata and idempotency fields retain their wire identifiers.
These compatibility names are intentional; changing them requires a separate migration.
Historical source-hash manifests keep the original filenames they attest to.

## Development and rollout

```bash
python -m pip install -e '.[dev,slack,analysis-test]'
mica-api
.venv/bin/ruff check src tests
.venv/bin/mypy src/mica src/pistachio
.venv/bin/pytest
```

Use the coordinated release pipeline to update all application images together. Keep
existing secret mappings and resource IDs. Rename the memory repository and update
`AGENT_MEMORY_REPO` together; verify access using the existing scoped credential.
Do not rewrite research notes or historical conversations as a branding change.
Cloud project IDs remain stable: renaming a project updates its GitHub memory folder
and guide through an internal stable-ID mapping. A separately requested storage
migration can also move its physical staging prefix, while existing logical run and
checkpoint keys continue to resolve. See [project memory](project-memory.md).
