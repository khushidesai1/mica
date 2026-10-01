# Protocol 2 staging deployment, 28 September 2026

Status: deployed and enabled for manual user testing. Revision
`pistachio-staging-00044-soc` receives 100% of staging traffic and the normal health
endpoint returns HTTP 200.

Open [Pistachio staging](https://pistachio-staging-h5a7b7akbq-uc.a.run.app). Start a **new task** in the web app or existing Slack
integration to use protocol 2. Existing tasks retain their stored builder protocol.
Authentication, secrets, channel configuration and IAM were preserved.

## Deployed artifacts

- Project: `azizilab-aml`; region: `us-central1`; service: `pistachio-staging`.
- Image build: `8cc39622-381e-472c-9906-30a46d835a14`.
- API image: `us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/api@sha256:f169b449b34ee51fc0d33477c2f0cea36a079db59db97b9f293ead07762d8586`.
- Science image: `us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/science@sha256:cd2fe1d03a7c9ccc920e0bca5c74008a28ae771b6855e4415efc05aa7b9e64f0`.
- Worker conformance job: `pistachio-protocol2-smoke-runtime-0928`; result: **SUCCEEDED**.
- Rollback revision: `pistachio-staging-00040-rz7`.

Enabled staging flags:

```dotenv
PISTACHIO_BUILDER_PROTOCOL_VERSION=2
PISTACHIO_DEVELOPMENT_ENABLED=true
PISTACHIO_DEVELOPMENT_MODEL=gpt-5.6-sol
PISTACHIO_BATCH_NESTED_SANDBOX_OPT_IN=true
```

## Verified

The real Batch worker completed clean verification and a full synthetic run using actual
Louvain, Silhouette scoring and Matplotlib. It produced `figure.png`, `summary.json` and
`reproduction.zip`, passed trusted artifact validation and reported
`PROTOCOL2_WORKER_CONFORMANCE_PASSED`.

The test also asserted that generated code cannot write source/configuration/inputs, read
the synthetic host environment marker or host temporary file, see `/proc/self/environ`,
or reach the metadata network. The worker remained unprivileged. These are concrete
boundary checks, not proof against all sandbox escapes.

Real Firestore version checks, stale-write rejection and event leases passed. Real GCS
publication, commit validation and fresh-worker publication recovery passed. Temporary
records and all four synthetic storage objects were removed afterward. Synthetic Batch
jobs ran bounded checks; no user task or Slack/email test message was created.

The last full local suite passed 110 tests with 3 skips; lint and type checks passed.
After the final library-path correction, all 32 affected development/Batch tests passed.
The skips are the host scientific module and two local Docker opt-in tests. The prior
Linux scientific fixture passed separately. The cloud smoke does not establish live-model
quality, full scientific replay, or correctness on the user's original AnnData dataset;
those remain manual/evaluation work.

## Approved staging exception and runtime fixes

Default Batch container policies prevented bubblewrap from constructing its namespaces.
Automatic approval review initially rejected weakening the outer container policy. The
user explicitly approved the staging-only trial and traffic promotion conditional on
passing isolation checks.

Only protocol-2 workers with the explicit staging opt-in receive:

```text
--security-opt seccomp=unconfined --security-opt apparmor=unconfined
```

This removes two outer container security layers. Unprivileged execution, the inner
bubblewrap filesystem/network/PID namespaces and resource controls remain. The opt-in is
false by default, is not applied to legacy workers, and is rejected in production by code
and a regression test. This is a staging test configuration, not production approval.

Real worker checks exposed further runtime issues that were corrected without adding
privileges: the sandbox omits `/proc` entirely instead of binding host process information,
and explicitly sets `LD_LIBRARY_PATH=/usr/local/lib` because it omits the image's `/etc`
loader cache. Louvain's build/runtime dependencies are installed and pinned in the science
image. Upstream Louvain/pkg_resources deprecation warnings remain nonfatal; the method
was not silently changed to Leiden.

## Rollback and follow-up

For immediate service rollback:

```bash
gcloud run services update-traffic pistachio-staging --project=azizilab-aml \
  --region=us-central1 --to-revisions=pistachio-staging-00040-rz7=100
```

The prior revision does not understand protocol-2 task records. Stop or drain active
protocol-2 tasks before rollback; already-running Batch jobs are not cancelled by routing
changes. For a gradual stop, set `PISTACHIO_BUILDER_PROTOCOL_VERSION=1` on the current code
while leaving development enabled long enough to drain existing tasks.

Keep the exception restricted to staging during manual tests. Production rollout still
requires broader sandbox/resource conformance, live-model evaluation, scientific acceptance
and reproducibility checks described in [the design](code-builder-redesign.md). Future
Terraform or deployment runs must intentionally preserve or reset these test flags.
