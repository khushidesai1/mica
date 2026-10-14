# Deployment

## Build immutable images

Resolve the Python base image tag to a digest, pass it through `PYTHON_IMAGE`, and publish both
images to Artifact Registry. Record the resulting digests in Terraform variables.

```bash
docker build -f docker/Dockerfile.api \
  --build-arg PYTHON_IMAGE=python:3.12.12-slim@sha256:REPLACE \
  -t REGION-docker.pkg.dev/PROJECT/REPO/api:VERSION .
docker build -f docker/Dockerfile.science \
  --build-arg PYTHON_IMAGE=python:3.12.12-slim@sha256:REPLACE \
  -t REGION-docker.pkg.dev/PROJECT/REPO/science:VERSION .
docker push REGION-docker.pkg.dev/PROJECT/REPO/api:VERSION
docker push REGION-docker.pkg.dev/PROJECT/REPO/science:VERSION
```

Use your normal Cloud Build pipeline instead of the illustrative commands if it already records
SBOMs and provenance.

For application code updates, `cloudbuild/deploy-code-update.yaml` builds and explicitly pushes
both images, runs the isolated-executor Matplotlib smoke test inside the new science image, resolves
both tags to immutable digests, and deploys the API while updating `PISTACHIO_BATCH_IMAGE` to the
new science digest. A successful image build alone is not a deployment: Cloud Run otherwise keeps
the prior worker digest for every newly submitted Batch job.

The coordinated pipeline exports `ExecutionBundle` from the API image and checks it inside the
worker before publishing either image. The check includes all decision scopes, requires an exact
schema match and verifies lossless bundle deserialization. API health checks alone do not establish
worker compatibility. Include `scripts/check_worker_compatibility.py` in the build context.

For an API-only release that changed execution models, synchronize the worker before running new
tasks. `cloudbuild/refresh-worker.yaml` builds a candidate from the existing science digest and copies
the application package from the exact deployed API digest. This preserves scientific dependencies
and tests the API/worker contract before pushing. It does not deploy automatically: run the bounded
protocol-2 Batch conformance check, then update `PISTACHIO_BATCH_IMAGE` to the candidate digest.
Keep worker sandbox settings unchanged. Existing tasks pin their runtime and need a deliberate,
audited recovery; changing the service variable only affects newly initialized sessions.

## Headless API rollout

The web application, Google browser OAuth flow, and interactive API documentation pages have been
removed. `/`, `/auth/*`, `/assets/pista_logo.svg`, `/docs`, and `/redoc` return 404.
Use `/api/v1/healthz` for health checks. The JSON API, `/openapi.json`, signed Slack ingress,
service-account OIDC callbacks, Gmail, and GitHub connections remain in the same service.

`cloudbuild/build-slack-api.yaml` now builds the complete headless API from source using
`docker/Dockerfile.api`. Supply `_API_IMAGE` and the existing compatible shared task worker as
`_WORKER_IMAGE`; `_PYTHON_IMAGE` is a pinned Python base image. The build verifies the absence of
the UI and checks the API/worker execution contract before publishing. The old `_BASE_API_IMAGE`
overlay and web-file preservation scripts are retired. If the execution contract differs, use
the coordinated API/worker pipeline above; do not bypass the compatibility check. Build the
Claude worker separately with `cloudbuild/build-slack-agent.yaml` when its source changes.

Preserve deployed Slack tokens, signing secrets, channel access settings, project registry, memory,
queues, worker images, and bucket connections. Browser OAuth variables are no longer consumed or
mounted; remove them from local tfvars/environment files when convenient. The old OAuth Secret
Manager resource remains tracked to avoid deleting existing credentials as part of UI removal.
Keep the separate signed-link secret: uploads and Gmail project selection still use it.
No Terraform apply or cloud deployment is implied by these source changes.

## Release validation

Run Ruff, mypy and the fast automated suite, then use small, bounded live smoke checks for
changed behavior. Prefer fakes for large file counts, large object sizes and failure injection.
Do not run lengthy model conversations or full-scale data transfers for routine releases;
reserve those for explicit scale validation or a specific unresolved production issue.
Keep build, authentication and worker compatibility gates, and clean up synthetic outputs.

## Provision

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars
terraform init
terraform plan
terraform apply
```

Set `planner_version = 2` in staging. New tasks store this value permanently; nonterminal legacy
tasks remain on planner v1. The default worker is `gpt-6-luna` at low reasoning and falls back to
`gpt-5.6-sol` low if the configured worker model is unavailable. Promote the flag only after the
stage latency and approval-continuity smoke tests pass.

The 180-second master-planner and 150-second worker limits are failure guardrails, not latency
targets. Stage timing remains visible in task metadata, and completed stages survive a timeout so
only the failed stage is retried. Their bounded worst-case path remains below the 900-second Cloud
Tasks dispatch deadline.

Use separate GCP projects or at minimum separate Terraform state and resource names for staging and
production. First target the Secret Manager resources, add the OpenAI, Slack, GitHub, and
link-signing secret versions with `gcloud secrets versions add`, then run the full apply (Cloud Run
references `latest`). Never put secret values in committed tfvars.

Configure the Slack event and interactivity URLs, Gmail delegated credentials/watch, and GitHub App
after the first apply. Create a read-only Cloud Build trigger for this repository using
`cloudbuild/tool-builder.yaml`, place its trigger ID in `tool_build_trigger`, and grant its build
service account only source-read, Artifact Registry write, scanner, and Cloud Build log access.
An exact tool approval starts that trigger; only a successful scan/smoke-test completion marks the
tool available and requeues waiting tasks. Run staging smoke tests for Slack, Gmail, the API, unknown-tool approval,
GPU/high-cost approval, duplicate delivery, Spot retry, and missing-completion reconciliation before
promoting the same image digests to production.

Batch workers use the configured VPC and subnet with no external IP address. The subnet must have
Private Google Access enabled so workers can pull Artifact Registry images and exchange bundles with
Cloud Storage and Pub/Sub. A five-minute reconciliation job marks terminal Batch failures and
recovers successful results if a worker completion event is missed.

The Terraform queue caps concurrent dispatches at three and Cloud Run scales to zero. Batch hard
limits and the $20 gate are enforced again in application policy, independent of Terraform.
