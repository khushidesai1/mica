# Pistachio

The new **Slack agent** follows the [single-session Claude design](docs/slack-agent-plan.md).
It lives inside Pistachio and reuses the existing Slack HTTP webhook and GCP Batch Spot jobs. See
[Slack setup and rollout](docs/slack-agent-setup.md) for reusing the existing Slack app,
Google Cloud project, and artifact bucket. The web chat continues to use the backend described below.

Pistachio is an approval-gated bioinformatics agent service built on the OpenAI Agents SDK. A
channel-independent task service handles work behind Slack, Gmail, and the browser, with durable
project-scoped memory and isolated Google Cloud Batch jobs. Tasks can be started in one surface and
followed or approved in another.

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

Open <http://localhost:8080>. With no `OPENAI_API_KEY`, the deterministic offline planner keeps
local tests and interface development usable. It is not a scientific model substitute.

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
- `src/pistachio/api`: OAuth/OIDC site and versioned API.
- `src/pistachio/connectors`: Slack, Gmail, notifications, and read-only GitHub catalog.
- `src/pistachio/execution`: validation plus local, Docker, and Batch backends.
- `src/pistachio/worker`: credential-separated Batch bootstrap/collector entry point.
- `config/tools.yaml`: pinned Azizilab bootstrap metadata.
- `infra`: staging/production Terraform.

See [architecture](docs/architecture.md), [deployment](docs/deployment.md), [channels](docs/channels.md),
[memory](docs/memory.md), and [security/operations](docs/security.md).

## Working from another laptop

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
