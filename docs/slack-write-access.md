# Scoped GCS writes from Slack

Slack supports project-scoped, create-only GCS output grants. The feature is disabled unless
an administrator configures the writer identity, approved storage locations and Slack write
administrators. See [deployment status](slack-agent-deployment.md) for the live configuration.

## Commands

Send these in a DM with Pistachio. The project argument is a Pistachio project name or ID,
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
Inside Pistachio's own artifact bucket, only `exports/` destinations are allowed; runtime
metadata, grants and checkpoints cannot be output targets.

## Saving files

After granting access, ask Pistachio to save the desired results to the approved GCS folder.
The worker stages only new/changed files under `outputs/gcs/` after a successful analysis turn.
For example, `outputs/gcs/reports/summary.csv` becomes
`gs://lab-outputs/liver/reports/summary.csv`. Other generated outputs retain normal Slack delivery.
The user-facing Slack reply includes the actual GCS destinations after confirmed publication.

Grants permit **creating new objects only**. Existing objects cannot be overwritten or deleted;
use a new filename for revised results. A retry of the same upload can recognize its completed
object without creating another copy. The default limit is 512 MiB per file, at most 50 files
per turn, and a bounded streaming upload duration. `AGENT_WRITE_MAX_MB` controls the file-size
limit (1–10000 MiB). Generated files remain available when publishing fails.

A run captures the current grant when it is submitted. Granting access does not retroactively
authorize a running job. Replacing or revoking that grant blocks further publishing by the
old job; send a new request to use the new grant. Expiry, revocation, changed scope, removed
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
AGENT_WRITE_MAX_MB=512
```

The first three default to empty, which disables writes. Onboarding admins are not implicitly
write admins. Changes to the admin or prefix configuration are checked against existing grants.

`infra/slack_writes.tf` provides optional configuration through
`slack_agent_write_admin_ids` and `slack_agent_write_prefixes`. It provisions:

- A dedicated writer account with only `storage.objects.create` and `storage.objects.get`,
  conditioned to the approved bucket prefixes. `get` reconciles upload retries.
- Permission for the control account alone to impersonate that writer, plus IAM Credentials
  and Security Token Service API enablement. Workers cannot impersonate it.
- Control-account write access to grant/audit records and project metadata, so narrowing the
  old broad grants need not break Slack registry/brief persistence.

Buckets must already exist. Folder-restricted bucket IAM ceilings require uniform bucket-level
access; whole-bucket ceilings also support buckets with object ACLs. Every individual upload
is still downscoped to its exact object. The Terraform custom role
belongs to `var.project_id`, so these Terraform-managed target buckets must belong to that
project. Cross-project destinations need an equivalent role in their owning project, configured
separately. Cloud deployment credentials need permission to configure the bucket policies.
Terraform does not create data buckets or change their uniform-access setting.

Grant commands update durable application policy; they do **not** edit IAM on every command.
The dedicated writer retains the configured maximum scope. The trusted publisher checks the
current project grant before each chunk, obtains a short-lived impersonated credential, and
downscopes it to the single destination object. Tokens and resumable-upload URLs never leave
the control service. The worker can stage outputs but cannot mint writer tokens or call the
publisher from inside the model sandbox. Input mounts remain read-only.

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

Validate the rollout with an approved test prefix: create a new object, refuse an existing
object and a sibling prefix, revoke/expire a grant, and verify the dedicated writer cannot
delete objects. Local tests mock cloud I/O; they do not establish live IAM conformance.

References: [Cloud Storage IAM conditions](https://docs.cloud.google.com/storage/docs/access-control/iam)
and [credential access boundaries](https://docs.cloud.google.com/iam/docs/downscoping-short-lived-credentials).
