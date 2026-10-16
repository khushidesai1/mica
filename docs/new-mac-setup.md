> **8 October 2026 update:** Use `main`. The deployed resize release has been recovered
> into Git. The branch and current-release instructions below are historical.
> See [the current deployment record](slack-agent-deployment.md) and
> [source provenance](deployed-source-20261008.json). Terraform state remains separate.

# Mica handoff to another Mac

Prepared 5 October 2026. This is the setup guide for the existing Mica Slack agent.
The developer laptop edits and operates the project; the deployed agent runs in Google Cloud
and does not require this laptop to stay on.

## Receiving Mac verification — 5 October 2026

The checkout at `/Users/khushidesai/Documents/mica` was fast-forwarded to
`486dad1` on `codex/private-project-memory`. The existing virtual environment runs
Python 3.12.14 natively on arm64, and all core, `dev`, `slack`, and `analysis-test`
dependencies match the repository's pins; `pip check` passes.

- Ruff, strict mypy (83 source files), compileall, and `HEADLESS_API_VERIFIED` passed.
- All 394 offline tests passed across two runs: 393 in the Codex sandbox and the
  Unix-socket test separately with permission to bind a local socket. Three tests
  were skipped and the paid live test was deselected. The shared test fixture now
  excludes workstation `.env` files: copying the example file had otherwise
  injected its sample email allowlist into tests and caused access failures.
- A temporary local Uvicorn server returned HTTP 200 and `{"status":"ok"}` at
  `/api/v1/healthz`, then was stopped.
- Git remote access, Google Cloud CLI access, and Python Application Default
  Credentials were verified. Both the default project and ADC quota project are
  `azizilab-aml`; Python successfully read the existing artifact bucket's metadata.
- Cloud Run reports `pistachio-staging-00097-log` ready with 100% traffic, and the
  live health endpoint returns `{"status":"ok"}`. See the updated
  [deployment record](slack-agent-deployment.md) for this release. The revision 87
  inventory and source/deployment comparison in section 4 below are the earlier
  handoff snapshot, not the current deployment.
- Docker Desktop was started; Docker client/server 23.0.5 and Compose 2.17.3 respond.
  The optional science-image build and Docker analysis fixture were not run.
- The private Terraform state (serial 80), backup (serial 78), tfvars, benchmarks,
  and deployment history are present and ignored by Git. The local artifacts
  directory is empty; its completeness against the old Mac was not verified.

Use `.venv/bin/python` and the other `.venv/bin/` commands on this Mac. Standalone
`python3.12`, `gh`, and Terraform are not on the shell PATH; none is required for
the verified development workflow or the existing cloud runtime. Install Terraform
only when preparing separately reviewed infrastructure work. No cloud deployment,
Terraform apply, paid model call, or Slack message was made during these checks.

## Start here

- Private GitHub repository: <https://github.com/khushidesai1/mica>.
- Handoff branch: **`codex/private-project-memory`**. Clone this branch explicitly.
- `main` is the older baseline (`87c4312`), not the latest development source.
- Git contains the application, tests, pinned dependencies, infrastructure definitions,
  Slack manifest, loading emoji, and documentation.
- Local Terraform state and deployment/artifact history are in a **separate private archive**.
- Sign into GitHub, Google Cloud, and Codex again on the new Mac. Account credentials do not
  come from the repository.

This handoff is a source backup and workstation setup. It does not deploy the newer code,
migrate project data, change Slack settings, or apply Terraform.

## 1. Install tools and clone the correct branch

Install/open Codex and sign in with the intended account. Install Homebrew if absent using
its official instructions at <https://brew.sh>. With Homebrew available:

```bash
brew install git gh python@3.12
brew install --cask gcloud-cli
gh auth login --hostname github.com --git-protocol https --web
gh auth setup-git
mkdir -p "$HOME/Documents"
cd "$HOME/Documents"
git clone --branch codex/private-project-memory https://github.com/khushidesai1/mica.git
cd mica
git status --short --branch
git log -1 --oneline
```

If the folder already exists, inspect its changes before pulling; do not overwrite work.
The GitHub account must have access to the private repository. The original account is
`khushidesai1`. A new username or clone location is fine; use the new path throughout.

These package names are documented by Homebrew: [Python 3.12](https://formulae.brew.sh/formula/python@3.12),
[GitHub CLI](https://formulae.brew.sh/formula/gh), and
[Google Cloud CLI](https://formulae.brew.sh/cask/gcloud-cli).

Docker is optional for the isolated execution fixture and local image builds; it is not
needed for editing or the normal offline suite. Terraform is only needed for infrastructure
work after restoring and reviewing state (section 5).

## 2. Rebuild the Python environment and verify it

From the repository root:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev,slack,analysis-test]'
if [ ! -e .env ]; then cp .env.example .env; fi
.venv/bin/ruff check src tests
.venv/bin/mypy src/mica
.venv/bin/pytest -q -m 'not live'
.venv/bin/python -m compileall -q src
.venv/bin/python scripts/check_headless_api.py
```

Recreate virtual environments; do not copy `.venv` or `.venv-slack` from the old Mac.
The old environment uses Python 3.12.14 under a Codex-managed absolute path. The source
requires Python >=3.12; use Python 3.12 for this handoff to match CI and the verified setup.
The `slack` extra installs the Claude SDK; `analysis-test` supplies scientific fixture dependencies.
No API keys are required for the offline checks. Keep `MICA_RUN_LIVE_TESTS` unset.

Verification on the original Mac: **335 passed, 3 skipped, 1 deselected** with the command above;
Ruff, mypy (82 source files), compileall, and `HEADLESS_API_VERIFIED` passed. One dependency
deprecation warning was emitted. A fresh environment on the receiving Mac still needs these checks.

Optional local API smoke check, in a separate terminal:

```bash
.venv/bin/uvicorn mica.api.app:app --host 127.0.0.1 --port 8080
```

Then run `curl --fail http://127.0.0.1:8080/api/v1/healthz`. The development source has no web UI;
`/` returns 404. `.env.example` uses local development settings, not the live cloud configuration.
There was no root `.env` on the original Mac at handoff time.

## 3. Authenticate to the existing Google Cloud project

Use the Google account that already has access to `azizilab-aml`:

```bash
gcloud auth login
gcloud config set project azizilab-aml
gcloud auth application-default login
gcloud auth application-default set-quota-project azizilab-aml
gcloud run services describe pistachio-staging \
  --project=azizilab-aml --region=us-central1 \
  --format='yaml(status.url,status.latestReadyRevisionName,status.traffic)'
gcloud storage buckets describe gs://pistachio-staging-artifacts-5304c19c
```

CLI login and Application Default Credentials serve different clients; the Python Google
libraries use the latter. If access/quota-project setup fails, resolve that account's IAM access
rather than creating replacement infrastructure. Read-only metadata checks are enough to verify
the laptop connection; no Slack messages, compute jobs, or model calls are needed.

Runtime Anthropic/OpenAI keys, Slack token/signing secret, GitHub integration token, and signed-link
secret already live in Secret Manager. Leave them there. Local paid integration work, if later
requested, needs separately configured credentials. Do not print secret values into chat or Git.
The agent's read-only GitHub integration is separate from the developer's `gh` login.

## 4. Know what is deployed versus what is in Git

The following was verified from Cloud Run metadata on 5 October 2026:

| Setting | Existing value |
| --- | --- |
| GCP project / region | `azizilab-aml` / `us-central1` |
| Cloud Run service | `pistachio-staging` |
| Serving revision | `pistachio-staging-00087-xey`, 100% traffic |
| Service URL | `https://pistachio-staging-h5a7b7akbq-uc.a.run.app` |
| Cloud Tasks queue | `pistachio-staging` |
| Artifact / quarantine bucket | `pistachio-staging-artifacts-5304c19c` |
| Slack backend | `claude_batch` |
| Direct conversation | `AGENT_DIRECT_CHAT_ENABLED=true` |
| Data project | `azizilab-aml` |
| Loading emoji | `:mica-loading:` (already installed in the Slack workspace) |
| Control service account | `pistachio-staging-control@azizilab-aml.iam.gserviceaccount.com` |
| Worker service account | `pistachio-staging-worker@azizilab-aml.iam.gserviceaccount.com` |

Image repository prefix: `us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/`.

| Image | Verified digest |
| --- | --- |
| `api` | `sha256:2314dcdf817e5320e1147530df0fb99f8563a3b2e9fdfa3b74f2d830c96b6296` |
| `slack-agent` | `sha256:38cae548e37393bde5931dc2b33708418d952a05876d55698a3b6f93b6166c9b` |
| `science` | `sha256:eecdeb404d52714bbfc3f4d016f642467721c137afd119e604e24cfd0ca0b9ec` |

Revision 87 backported progress changes onto the earlier deployed source. **The handoff branch
also contains newer headless API, access-control, project/private-memory, and channel-routing work
that must not be assumed deployed.** In particular, the current source removes the browser UI,
while the recorded live release retains it. Do not deploy the branch just to move laptops.

Read [deployment history](slack-agent-deployment.md), [current Slack setup](slack-agent-setup.md),
[access/memory migration plan](slack-access-and-memory-plan.md), and
[headless deployment guide](deployment.md#headless-api-rollout) before a future release.
The access/memory change requires its reviewed ownership migration and quiesced cutover.
Preserve the existing Slack app, webhook, scopes, service identities, sandbox controls, and data.
Recheck live metadata before any later deployment; this table is a dated snapshot.

Shared project briefs, thread checkpoints/workspaces, and results already in GCS, plus Firestore
task/receipt records, stay in the same cloud project. Moving the laptop does not relocate them.
The private archive below is a backup of local files, not an export of all cloud data.

## 5. Transfer the private local archive

On the original Mac the transfer directory is:

```text
/Users/khushidesai/Documents/mica/.pistachio/laptop-handoff/
```

Transfer `mica-private-2026-10-05.tar.gz` and its `.sha256` file using AirDrop or an
encrypted storage/transfer method under your control. The archive itself is not encrypted.
It contains sensitive local state and a Codex configuration reference; keep it out of Git,
chat attachments, and shared/public folders. The directory is ignored by Git and excluded from
the existing Docker/Cloud Build upload rules.

The archive preserves:

- `infra/terraform.tfstate`, `infra/terraform.tfstate.backup`, and `infra/terraform.tfvars`.
- `.pistachio/artifacts/`, `.pistachio/benchmarks/`, and `.pistachio/deployments/`, including
  exact source and verification records for the deployed progress releases (about 338 MB
  uncompressed in total on the old Mac).
- `codex-reference/config.toml.source-reference` and a configuration inventory for manual review.

It excludes virtual environments, provider caches, Git history (available on GitHub), and
GitHub/Google/Codex login caches. No custom user skills or global `~/.codex/AGENTS.md` were found.

After placing the archive and checksum together in Downloads, extract to a separate directory:

```bash
cd "$HOME/Downloads"
shasum -a 256 -c mica-private-2026-10-05.tar.gz.sha256
mkdir -p mica-private-2026-10-05
chmod 700 mica-private-2026-10-05
tar -xzf mica-private-2026-10-05.tar.gz -C mica-private-2026-10-05
```

On a **fresh clone**, restore the three `infra/` files to the clone's `infra/` directory and
merge the archived `.pistachio/` directory into the clone's `.pistachio/`. If either already
contains local files, compare/back them up before copying. Retain the old Mac's originals
until the new setup has been verified. The archive contains local artifacts and may include
private research or conversation material.

**Terraform is currently local-state based.** The saved state reports Terraform 1.15.9,
serial 80; its filesystem date is 21 September 2026. Cloud Run has subsequently been updated
using `gcloud`, so this state/tfvars are recovery inputs, not a guarantee of current live settings.
Use a compatible Terraform version, retain the checked-in provider lockfile, and review drift
against the actual resources before applying. Do not run `terraform apply` with empty state or
blindly apply the archived tfvars: that could recreate resources or revert the live deployment.
A controlled move to a remote state backend can be planned separately.

## 6. Open the project in Codex

Add/open the new clone as a Codex project, then give it this file. Codex's global settings are
outside the repository in `~/.codex/config.toml`; project configuration can live in
`.codex/config.toml` ([official configuration documentation](https://learn.chatgpt.com/docs/config-file/config-basic#codex-configuration-file)).
This repository has no project `AGENTS.md` or `.codex` configuration to restore.

The private archive includes the old global config as a **reference**, not a file to install
wholesale. Review chosen preferences against the new Mac's existing configuration. The old config
contains absolute paths, project entries, MCP configuration, plugin choices, and approval settings.
Reinstall/reconnect any desired plugins through the app and adapt paths; retain the new Mac's
appropriate permission settings. No plugin is required for Mica's Python test suite.
Codex chat history, automations, and saved remote-host connections are not migrated by this repository
or archive. This guide supplies the project handoff context for a new chat.

## Paste this into Codex on the new Mac

> Set up my existing Mica agent development workspace on this Mac. The private repository is
> https://github.com/khushidesai1/mica and the handoff branch is codex/private-project-memory.
> Clone that branch into ~/Documents/mica if needed, preserving any existing local work, then
> read docs/new-mac-setup.md and follow it. Install missing development tools, recreate the Python
> 3.12 environment with dev, slack, and analysis-test extras, and run the offline checks. Help me
> complete GitHub and Google Cloud browser sign-ins when required. If I provide the private archive,
> verify its checksum and restore its local artifacts and Terraform state without overwriting newer
> files. Verify read-only access to the existing azizilab-aml Cloud Run service. The cloud agent is
> already running; do not deploy, migrate cloud data, change Slack settings, run paid analysis, or
> apply Terraform as part of laptop setup. Report anything that still needs my input.
