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

Cloud Run, buckets, queues, service accounts, secrets and image repository IDs retain
their existing names. Saved paths such as `.pistachio/`, `/srv/pistachio` and
`/opt/pistachio` remain stable so checkpoints, runtimes and saved scripts keep working.
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
Do not rewrite research notes, historical conversations or cloud object keys as a
branding change.
