# Pistachio

Pistachio is a **Slack agent with a headless backend**. The Slack agent follows the
[single-session Claude design](docs/slack-agent-plan.md) and reuses the existing Slack app,
HTTP webhook, Cloud Run service, connections, and GCP Batch Spot jobs. See
[Slack setup and rollout](docs/slack-agent-setup.md) for configuration.

In the agent DM: `init <name> gs://bucket/folder/` creates and selects a project, with a command
to switch back to the previous one; `switch` lists your
projects; `switch <name or ID>` selects one for new threads. Existing threads keep their project.
Send `help` for this quick guide or `help all` for every command.

Attach images, PDFs, tables, or text directly in a project thread. Pistachio can answer visual
questions about supported images/PDFs in chat and use a compute worker for analysis or editing.
It can also inspect the figures it creates and answer follow-ups about files it delivered.
The defaults are 10 files per message, 100 MB per file, and 250 MB total. Uploaded originals
expire after 30 days; derived outputs keep their existing project retention. Figure previews
are downscaled, while Slack receives full-resolution originals. See
[attachments and visual checks](docs/slack-agent-setup.md#attachments-in-slack) for details.

There is no web application or browser login. The HTTP service remains necessary for Slack
webhooks, Cloud Tasks, worker callbacks, health checks, and authenticated task APIs. Project and
private memory, data access, analysis, code execution, approvals, progress, and artifact delivery
remain available. The shared OpenAI Agents SDK task service, Gmail integration, read-only GitHub
catalog, and existing task records are retained. Historical `web` task provenance stays compatible
with stored records and worker schemas.

## Safety invariants

- Every executable task has a fresh `TaskContract`; memory never grants scientific consent.
- Every primary procedure is version-pinned. Scientific and interpretation-changing figure
  settings require current-task confirmation; presentation and routine technical settings are
  selected automatically and reported after execution.
- Planner v2 runs one authoritative `MasterPlan`, then resumes immutable tool, resource, figure,
  code, validation, and execution stages without restarting the scientific planner.
- Generated analysis code runs without network access in the local Docker backend. Cloud workers
  receive immutable bundles and publish checksummed output bundles.
- GitHub access is metadata/contents-read only. The runtime contains no SSH keys, `gh`, commit,
  push, or pull-request capability.
- New tools remain version-specific proposals until a user approves the exact source and version.

## Local development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
.venv/bin/uvicorn pistachio.api.app:app --reload --port 8080
```

Check service health with `curl http://localhost:8080/api/v1/healthz`. Use Slack for conversations;
there is no page at `/`. API clients can use `/api/v1` and the schema at `/openapi.json`.
With no `OPENAI_API_KEY`, the shared task service uses its deterministic offline planner for local
tests. It is not a scientific model substitute. See the Slack setup guide for Claude configuration.

For the Docker isolation fixture, build the science image, start Compose, and run the smoke client:

```bash
docker build -f docker/Dockerfile.science -t pistachio-science:local .
docker compose up --build -d
.venv/bin/python scripts/local_fixture.py
```

```bash
.venv/bin/ruff check src tests
.venv/bin/mypy src/pistachio
.venv/bin/pytest
```

## Repository map

- `src/pistachio/agents`: Agents SDK harness, session adapter, context builder, and memory manager.
- `src/pistachio/api`: headless versioned API, OIDC authentication, and Slack worker callbacks.
- `src/pistachio/connectors`: Slack, Gmail, notifications, and read-only GitHub catalog.
- `src/pistachio/execution`: validation plus local, Docker, and Batch backends.
- `src/pistachio/worker`: credential-separated Batch bootstrap/collector entry point.
- `config/tools.yaml`: pinned Azizilab bootstrap metadata.
- `infra`: staging/production Terraform.

See [architecture](docs/architecture.md), [deployment](docs/deployment.md), [channels](docs/channels.md),
[memory](docs/memory.md), and [security/operations](docs/security.md).

## Working from another laptop

Use the [Mac setup and Codex handoff note](docs/new-mac-setup.md) for the exact branch,
authentication steps, private local backup, and verified deployment inventory.

Clone this repository, install Python 3.12, and recreate the development environment:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,slack,analysis-test]'
cp .env.example .env
.venv/bin/pytest -q
```

The Claude Slack service uses Cloud Run, Cloud Tasks, GCP Batch, Firestore, and GCS. Its deployed
runtime does not require a developer laptop to remain on. A clone supplies the source, tests,
deployment definitions, and documentation; cloud project briefs and thread checkpoints stay in GCS.
See [Slack setup](docs/slack-agent-setup.md) and the
[deployment record](docs/slack-agent-deployment.md) for the existing infrastructure.

Credentials, `.env`, virtual environments, local `.pistachio/` artifacts, Terraform state, and
`infra/terraform.tfvars` are intentionally excluded from Git. Sign into GitHub and Google Cloud
on the new laptop and configure local credentials separately. Production secrets stay in Secret
Manager. Transfer any needed local artifacts securely.

Terraform currently has no remote backend configured. Before managing the existing infrastructure
from another laptop, securely transfer the current `infra/terraform.tfstate` and
`infra/terraform.tfvars`, or migrate the state to an access-controlled remote backend. Keep the
original state and its backup until the handoff is verified. Do not apply Terraform against empty
state for the existing deployment. Repository access alone does not transfer cloud permissions or
local state.

The opt-in code builder adds a durable inspect/edit/test/repair loop, clean verification,
result review, and reproduction packages. Read the [design](docs/code-builder-redesign.md)
and [implementation and rollout notes](docs/code-builder-implementation.md) before enabling
builder protocol 2. Existing tasks keep their stored protocol; protocol 1 remains the default
until execution, persistence, and live-model release gates pass.
