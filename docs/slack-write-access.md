# Scoped GCS writes from Slack

Slack supports project-scoped GCS output grants. The feature is disabled unless
an administrator configures the writer identity, approved storage locations and Slack write
administrators. See [deployment status](slack-agent-deployment.md) for the live configuration.

## Commands

Send these in a DM with Mica. The project argument is a Mica project name or ID,
not a Google Cloud project ID. Write administrators must also have access to that project.
The destination must be in that project's linked data bucket. Both the user's membership
and the bucket association are checked when granting and again during publishing; losing
membership or changing the association disables the grant.

```text
write grant liver gs://lab-outputs/liver/ 1h
write grant liver gs://lab-outputs/liver/
write status liver
write revoke liver
help write
```

The second example deliberately has **no expiry: access continues until revoked**. Slack
shows “no expiry — until revoked” both when granting and when checking status. Timed grants
accept a positive integer followed by `m`, `h`, or `d`, up to 365 days. Expiry is measured from
the original Slack message, so delayed delivery and retries cannot extend access.

Each project has one active destination. A new grant replaces the old grant, and a revoke
removes it. Grant/status/revoke run without waiting for a busy compute worker. They do not
change the project's data location or thread binding, and they never invoke a model.
Status is available to project members; granting/revoking requires the separate write-admin
allowlist. Commands and successful uploads are recorded under `write-access/` in the existing
artifact bucket. This prefix is excluded from the Slack deployment's temporary-data lifecycle.

The destination can be a whole bucket or a folder prefix, but it must be within the configured
ceiling. Prefixes end in `/`, so permission for `project-a/` does not include `project-a-backup/`.
Inside Mica's own artifact bucket, only `exports/` destinations are allowed; runtime
metadata, grants and checkpoints cannot be output targets.

## Saving files

After granting access, ask Mica to save the desired results to the approved GCS folder.
The worker stages requested files under `outputs/gcs/`.
For example, `outputs/gcs/reports/summary.csv` becomes
`gs://lab-outputs/liver/reports/summary.csv`. Other generated outputs retain normal Slack delivery.
For a complete result store, preserve its `points/`, `shapes/`, and `tables/*.zarr` layout in
`outputs/gcs/<path relative to grant>/`. For example, with a bucket-root grant, use
`outputs/gcs/resegmentation/tma15_atomx_prior/`. Close writers, validate the store, then call
`publish_gcs` with that folder as soon as it is complete. The tool is available only when
the current run has a write grant. Unchanged inputs, especially large image stores, should
not be copied unless requested.

Successful mid-turn publications survive later analysis errors. On a successful turn,
end-of-turn delivery publishes remaining new/changed files and skips confirmed publications
from the same event. A stopped turn does not automatically publish unfinished work.
Both the tool and final Slack reply report confirmed GCS destinations. Partial failures list
errors separately; a multi-object store is not an atomic upload. Verify all expected objects
before using it. Once publication begins, staged copies are immutable for safe retry; put
revised results at a new destination path.

The same grant permits new files and confirmed overwrites. Name the destination when asking
to replace an existing output. Before starting the worker, Mica probes each requested path once
and presents the nonempty folders/files in a concise summary with **Approve and run** and **Cancel** buttons.
Only the requester can approve it in the original Slack thread. Approval expires after one
hour if unused; a confirmed run stays bounded by its normal execution limits. There is no
separate overwrite grant. New destinations start without this confirmation.

Confirmation records the inspection time and approved paths, without enumerating files.
At publication, only an existing generation created before that inspection can be replaced,
and its generation is pinned in the copy request. Later-created or changed versions are
rejected; request a new run to confirm them. An absent destination remains create-only.
Unknown destinations discovered later remain create-only until a new request confirms them.
Other existing files stay unchanged, including extra keys in a result store: this is not a
folder synchronization or delete operation. Use a fresh folder when a new store layout would
leave obsolete keys behind. A retry recognizes its completed object without copying again.
There are no application caps on file count, individual file size, or total output size;
GCS's 5 TiB object limit, worker disk capacity, run budget, and concurrency limits still apply.

Workers publish batches of up to 500 paths, with eight concurrent batches and at most eight
concurrent staging uploads. The controller copies directly between GCS buckets with `rewrite`;
large-file bytes do not pass through Cloud Run. Each request has an 840-second time slice and
each object retains its own mutex and receipt. Unfinished rewrites persist a token and resume
in later requests. Publication details are immutable JSONL shards; indexes and Slack replies
contain per-folder totals instead of thousands of object URIs.
Transient GCS transport failures retry the affected paths while preserving completed receipts
and the other results in the batch. Permanent access and generation conflicts remain failures.

After verifying destination generation, size, checksum, and publication metadata, the controller
saves a durable receipt and deletes the corresponding staged source generation. Retries work
even after cleanup. Failed or unfinished copies retain their staging data for retry. Already
published GCS data is excluded from subsequent workspace snapshots; small logs/manifests remain.
The artifact bucket's existing task lifecycle is the fallback for abandoned transfers (30 days
in staging). Staging also has seven-day soft-delete retention, so logically deleted data can
remain billable until that expires. This release does not change those bucket policies.


A run captures the current grant when it is submitted. Granting access does not retroactively
authorize a running job. Replacing or revoking that grant blocks further publishing by the
old job; send a new request to use the new grant. Every turn includes an application-set
grant line that supersedes earlier thread statements. The read-only input mount does not
indicate whether output publishing is authorized. Expiry, revocation, changed scope, removed
admin authority, project membership changes and forgotten conversation contexts are checked
again during uploads and before committing. An upload already completing may finish;
revocation does not delete files that were already uploaded.

The same project-grant check is reusable for future ambient-task runners. The current
integration covers the existing Slack Batch worker; it does not add an ambient scheduler.

## Configuration and cloud permissions

Configure the control service (not the agent sandbox):

```text
AGENT_WRITE_ADMIN_IDS=T123:U123
AGENT_WRITE_ALLOWED_PREFIXES=gs://lab-outputs/liver/,gs://lab-outputs/brain/
AGENT_WRITE_SERVICE_ACCOUNT=pistachio-staging-writer@PROJECT.iam.gserviceaccount.com
AGENT_WRITE_UPLOAD_SECONDS=840
```

The first three default to empty, which disables writes. Onboarding admins are not implicitly
write admins. Changes to the admin or prefix configuration are checked against existing grants.
The broker time slice accepts 1–840 seconds and is copied into each run's request manifest.

`infra/slack_writes.tf` provides optional configuration through
`slack_agent_write_admin_ids` and `slack_agent_write_prefixes`. It provisions:

- A dedicated writer account with `storage.objects.create`, `storage.objects.get`, and
  `storage.objects.delete`, conditioned to approved bucket prefixes. Cloud Storage requires
  `delete` to overwrite an object; the application exposes no delete operation.
  `get` reconciles retries and generation preconditions prevent replacing an unapproved version.
- Permission for the control account alone to impersonate that writer, plus IAM Credentials
  and Security Token Service API enablement. Workers cannot impersonate it.
- Control-account write access to grant/audit records and project metadata, so narrowing the
  old broad grants need not break Slack registry/brief persistence.

Server-side copies additionally require `storage.objects.get` for the writer on staged exports
in the artifact bucket: `tasks/slack-analysis/events/<event_id>/write-exports/`. Grant only source
read access, not source writes or access to other event records. Staging uses the get-only custom
role `mica_staging_export_reader` with a conditional bucket binding; this prerequisite was
applied separately with explicit authorization. The existing Terraform file does not provision
that additional binding. The controller already has source-delete access for cleanup.

Buckets must already exist. Folder-restricted bucket IAM ceilings require uniform bucket-level
access; whole-bucket ceilings also support buckets with object ACLs. Every individual upload
is still downscoped to its exact object. Replacement tokens use the object-admin permission
boundary intersected with the writer's three-permission IAM role; create-only calls keep the
creator/viewer boundary. The Terraform custom role
belongs to `var.project_id`, so these Terraform-managed target buckets must belong to that
project. Cross-project destinations need an equivalent role in their owning project, configured
separately. Cloud deployment credentials need permission to configure the bucket policies.
Terraform does not create data buckets or change their uniform-access setting.

Grant commands update durable application policy; they do **not** edit IAM on every command.
The dedicated writer retains the configured maximum scope. The trusted publisher checks the
current project grant between rewrite iterations, obtains a short-lived impersonated credential,
and downscopes it to the exact destination plus read-only access to the exact staged source.
Tokens and rewrite tokens never leave
the control service. The worker can stage outputs but cannot mint writer tokens or call the
publisher from inside the model sandbox. Input mounts remain read-only.
The trusted `publish_gcs` MCP tool invokes the existing publisher outside that sandbox.
The GCS client retains refreshable impersonated/downscoped credentials, so rewrite requests
can refresh them during copies beyond their 300-second lifetime; it never receives a frozen
writer token from a worker. See [Google Auth's credential refresh contract](https://google-auth.readthedocs.io/en/latest/reference/google.auth.downscoped.html).

Time expiry and revocation are enforced by the publisher. They do not revoke the writer's
underlying IAM ceiling. This avoids giving the runtime permission to edit bucket IAM policies.
Indefinite grants do not create long-lived service-account keys.

Before enabling this in the existing deployment, reconcile the live project-wide
`roles/storage.objectAdmin` bindings on the control and worker accounts. Those bindings were
observed during the initial investigation and are broader than the repository's intended
project-wide `objectViewer` roles. IAM grants are additive: adding scoped grants does not remove
an independently existing broad grant, and Terraform member resources will not automatically
remove an out-of-band binding. Preserve the required artifact-prefix writes while removing
that broader data write access. See the deployment record for the current live IAM state.

Validate the rollout with a disposable test prefix: require approval before starting a
replacement run, replace the confirmed version, preserve a changed version and an unapproved
sibling, and verify grant revocation and expiry. Check exact-object credential boundaries.
Local tests mock cloud I/O; they do not establish live IAM conformance.
See [result-store staging verification](gcs-result-stores-staging.md) for the deployed
release, unit-test scope, and the successful 287-object live publish check.
See [overwrite staging verification](gcs-overwrites-staging.md) for the live approval,
replacement, unchanged-sibling, and cleanup checks.
See [scaled publishing verification](scaled-publishing-deployment.md) for the subsequent
batched rewrite rollout and large-transfer checks.

References: [Cloud Storage IAM conditions](https://docs.cloud.google.com/storage/docs/access-control/iam)
and [credential access boundaries](https://docs.cloud.google.com/iam/docs/downscoping-short-lived-credentials).

## Publication receipts and manifests

The agent describes saved folders and what they contain. The tool returns per-folder confirmed
file counts and bytes, failure counts, and at most five error examples. Slack appends one
`Saved to GCS` line per destination folder and at most three error examples, keeping the
agent's summary first even when delivery fails. A store with thousands of objects does not
produce thousands of reply lines.

Each publish writes totals and JSONL shard references to `logs/gcs-publish-<id>.json`
in the workspace; these logs are not Slack attachments. Shards hold per-path status, size,
destination, and errors. The controller streams them into protected audit storage. It retains immutable
`write-access/uploads/<event>/publications/<id>/manifest.json` records. The event's
`manifest.json` indexes these records/shards and aggregates folder totals. Existing controller
per-object receipts remain the authority for upload confirmation. Explicit folder groups
survive retries and worker replacement; automatic-only exports group by their top-level folder.

Workers stage manifest reports under their existing event transport prefix. The controller
archives them during normal job polling and before terminal reconciliation; workers have no
write permission to the protected audit prefix. Archive paths and reported destinations are
checked against the event and captured grant, and earlier publication reports are immutable.
The manifests are worker reports; controller per-object receipts confirm actual uploads.

Overwrite confirmations are stored under `write-access/approvals/<event>.json`, outside the
worker's writable area. They bind the project, requester, access revision, grant, destination
paths, and inspection time. Duplicate clicks cannot start another worker. Current access and grants
are rechecked on confirmation and during upload. Controller receipts include the replaced
object generation. See [Cloud Storage upload permissions](https://docs.cloud.google.com/storage/docs/uploading-objects#required_roles).
