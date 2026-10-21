# Migrating the deployed resources to Mica

The migration creates an independent `mica-staging` stack alongside the working
`pistachio-staging` stack. `infra/mica-staging/` owns only the new resources. The existing
Terraform root/state continues to own Firestore, project APIs, the `cloud-builds` topic,
network, and legacy resources. Do not copy its state, rename its resource addresses, or
use `-target` to divide the create/delete parts of a replacement.

The new root has three explicit stages:

| Stage | Behavior |
| --- | --- |
| `foundation` | Creates empty storage/registry/secrets, three identities and additive grants, an empty queue, and two topics. No API, scheduler, subscriber, secret version, or data copy. |
| `preview` | Adds the API with migration admission frozen and four paused schedulers. No push subscriptions. Requires populated secret versions and an approved API image with admission support. |
| `cutover` | Activates schedules and creates push subscriptions. Requires `cutover_verified=true` after the operator handoff/storage checks; this is not an automated traffic switch. |

Terraform never manages secret values. Existing `mica-gmail-*` and `mica-github-secret`
resources remain unchanged. Old image repositories and bucket data remain available for
saved references until a separate retirement review. Existing container paths and wire
identifiers remain compatible.

## Private metadata and reviewed inputs

Use Terraform **1.13.5**, verified against HashiCorp's release checksum, and the committed
Google provider **6.50.0** lockfile. Run commands from the repository root unless shown otherwise.
Use an ignored directory with restrictive permissions; exported service metadata contains
account/configuration information even though secret references contain no secret values.

```bash
umask 077
mkdir -p .mica/migration
gcloud run services describe pistachio-staging --project=azizilab-aml \
  --region=us-central1 --format=json > .mica/migration/source-service.json
gcloud projects get-iam-policy azizilab-aml --format=json \
  > .mica/migration/project-iam.json
python scripts/mica_migration.py capture \
  --service .mica/migration/source-service.json \
  --project-iam .mica/migration/project-iam.json \
  --artifact-bucket mica-staging-artifacts-azizilab-aml \
  --output .mica/migration/foundation.tfvars.json
```

Capture resolves `MICA_*` over legacy aliases, retains all effective feature settings and
both worker pins, copies secret references only, and records the source-spec hash. It
refuses overwriting an existing snapshot. Before applying, compare a fresh source export
against the recorded source spec, and review all bucket/project/service-account IAM and
the runtime manifest. The capture is not a substitute for that access review.

Review the named new resources for collisions using Cloud Run, bucket, queue, registry,
secret and service-account list/describe calls. Do not import or overwrite an existing
resource unexpectedly carrying the new name. The new bucket's source lifecycle is held
off while copying/auditing so aged checkpoints cannot disappear during migration; restore
the reviewed prefix-specific retention after reference compatibility is verified.

## State backend preparation

The new root temporarily uses a **private local backend**. The administrator is the single
operator for this stack; Terraform's local state lock serializes commands on that filesystem.
Keep locking enabled and do not run competing copies of the state from another host. A future
move to a GCS backend needs its own verified administrator-only bucket and an explicit state
migration; do not upload or copy the legacy root's state.

Use an absolute durable directory in the primary checkout's ignored `.mica/terraform/` tree,
**outside any temporary worktree**. Restrict directories to mode `0700` and state, backup, and
saved plan files to `0600`. Keep dated backups on the administrator-controlled machine before
and after each apply, and arrange an administrator-controlled encrypted backup of that directory.
No secret values are managed by this root, but its state and metadata remain private.

```bash
umask 077
# Set this to the durable primary checkout, not a /tmp worktree.
MICA_STATE_DIR=/ABSOLUTE/PRIVATE/PRIMARY_CHECKOUT/.mica/terraform/mica-staging
mkdir -p "$MICA_STATE_DIR/backups"
chmod 700 "$MICA_STATE_DIR" "$MICA_STATE_DIR/backups"
terraform -chdir=infra/mica-staging init -reconfigure \
  -backend-config="path=$MICA_STATE_DIR/terraform.tfstate"
# Before and after every apply, if a state file exists:
if [ -f "$MICA_STATE_DIR/terraform.tfstate" ]; then
  cp -p "$MICA_STATE_DIR/terraform.tfstate" \
    "$MICA_STATE_DIR/backups/terraform-$(date -u +%Y%m%dT%H%M%SZ).tfstate"
  chmod 600 "$MICA_STATE_DIR/terraform.tfstate" "$MICA_STATE_DIR"/backups/*.tfstate
fi
```

Authenticate Terraform using operator ADC or a short-lived access token in the process
environment; never put credentials in backend files, tfvars, scripts, or logs. Do not initialize
from the legacy directory, disable locking, import existing singleton resources, or reinitialize
the backend to an empty path after resources exist. Losing local state would require a separately
reviewed recovery, not another create-only apply. Confirm the backend's absolute path before
planning and applying; preserve the local `.terraform` backend metadata until the next init.

The application project's existing runtime identities inherit `roles/storage.objectViewer`.
Private bucket bindings and uniform bucket access cannot override that inherited read access,
so a state bucket in the same project is not sufficiently isolated. A tag-conditioned IAM Deny
prototype was evaluated but is not part of this implementation: `roles/iam.denyAdmin` must be
granted at organization scope, and its create/update permissions cannot be placed in a project
custom role. A project-level grant of that role is invalid. No organization grant, deny policy,
tag, or state bucket is needed for the local backend. If remote state is introduced later, use
an administrative project without runtime grants or separately approve and verify isolation
before uploading any state. See [Google's deny access roles](https://cloud.google.com/iam/docs/deny-access)
and [Terraform's local backend](https://developer.hashicorp.com/terraform/language/backend/local).

## Foundation plan and apply

```bash
terraform -chdir=infra/mica-staging plan \
  -var-file=../../.mica/migration/foundation.tfvars.json \
  -out=../../.mica/migration/foundation.tfplan
terraform -chdir=infra/mica-staging show -json \
  ../../.mica/migration/foundation.tfplan > .mica/migration/foundation-plan.json
python scripts/mica_migration.py check-plan \
  --plan-json .mica/migration/foundation-plan.json \
  --inputs .mica/migration/foundation.tfvars.json
```

The gate rejects every delete/replacement, import/address move, authoritative IAM resource,
unrecognized resource ownership, legacy resource name, primitive IAM role, unexpected bucket,
early push subscription, and active preview scheduler. It compares the saved plan's inputs
with the reviewed file. Existing-resource updates require an exact `--allow-update ADDRESS`
exception after reviewing that diff. Do not add exceptions to make an unexplained plan pass.

Review the full saved plan and new grants as well as the gate output. Then apply **that exact
plan file** with `terraform -chdir=infra/mica-staging apply ../../.mica/migration/foundation.tfplan`.
Do not apply a newly generated, unreviewed plan or use `-target`. A scratch read-only plan is useful for review, but regenerate against the exact durable local
backend using refreshed operator credentials before applying. Back up existing state first;
check mode `0600` on state and backup files afterwards.

## Operator secret copies

The capture evidence contains each old source/version and corresponding new secret name.
Source version numbers need not match destination version numbers: the first copy is new
version **1**, which the preview references explicitly. Existing Mica secret references keep
their selected versions. The signing key also encrypts stored AtoMx credentials and must be
copied unchanged. Do not rotate it during this migration.

Khushi runs the following locally after foundation creation. It reads the captured mapping,
refuses any destination that already has a version, buffers each source value only in memory,
and passes it to `gcloud` through stdin. It prints no values and creates no credential files.
Codex must not run this operator step. If interrupted, inspect completed destinations and
remove their entries from a separate reviewed pending-copy file before resuming; do not
blindly create version 2 or overwrite an existing credential.

```bash
python - <<'PY'
import json
import subprocess
from pathlib import Path

project = "azizilab-aml"
evidence = json.loads(Path(".mica/migration/foundation.tfvars.evidence.json").read_text())
copies = evidence["operator_secret_copies"]
for entry in copies:
    versions = json.loads(subprocess.check_output([
        "gcloud", "secrets", "versions", "list", entry["target"],
        "--project=" + project, "--format=json",
    ]))
    if versions:
        raise SystemExit("Destination already has versions: " + entry["target"])
for entry in copies:
    value = subprocess.check_output([
        "gcloud", "secrets", "versions", "access", entry["source_version"],
        "--project=" + project, "--secret=" + entry["source"],
    ])
    if not value:
        raise SystemExit("Source was empty; stopped before adding a version")
    subprocess.run([
        "gcloud", "secrets", "versions", "add", entry["target"],
        "--project=" + project, "--data-file=-", "--quiet",
    ], input=value, check=True, stdout=subprocess.DEVNULL)
    del value
    print("Created initial version for " + entry["target"])
PY
```

## Preview and data preparation

Copy the three currently approved images with a verified registry-copy tool such as `crane
copy`; compare source and destination digests. Do not rebuild to perform an image rename.
Keep old historical URIs accessible. Changing the default science image URI requires retaining
the previously approved current URI in the compatible-image list; do not blanket-allow old
historical digests that have not passed the current execution checks.

Create a new reviewed input file from the foundation inputs with `stage=preview`. Set its API
digest to the release with admission guards; supply both verified worker pins. Use the actual
new URL after initial preview creation; do not guess the hashed Cloud Run hostname. Keep
`MICA_MIGRATION_SERVICE=mica-staging` and `MICA_MIGRATION_DEFAULT_MODE=frozen`. Precreate the
frozen admission document with `scripts/mica_admission.py` before starting the new service.
The root sets these guard settings explicitly; no jobs, watch renewal, or maintenance should
run during preview. Check health/auth and admission without sending synthetic user messages.

Bulk-copy every required managed-bucket prefix while the old stack runs, then perform the
bounded inventory/generation/reference audit using the storage migration tooling. New GCS
generations differ from source generations. Preserve old pinned reads or apply a verified
mapping; never remove generation checks or rewrite scientific buckets outside this migration.
Do not discard task/checkpoint prefixes based only on their lifecycle age.

The storage helper is read-only and does not perform the copy or apply record patches. For
the final frozen copy, capture source inventory before and after the copy, plus destination
inventory, then require the verification report to pass:

```bash
python scripts/mica_storage_migration.py inventory \
  --source pistachio-staging-artifacts-5304c19c \
  --destination mica-staging-artifacts-azizilab-aml --side source \
  --output .mica/migration/source-before.jsonl
# Perform the separately reviewed copy, then repeat with source-after.jsonl.
python scripts/mica_storage_migration.py inventory \
  --source pistachio-staging-artifacts-5304c19c \
  --destination mica-staging-artifacts-azizilab-aml --side destination \
  --output .mica/migration/destination.jsonl
python scripts/mica_storage_migration.py verify \
  --source pistachio-staging-artifacts-5304c19c \
  --destination mica-staging-artifacts-azizilab-aml \
  --source-before .mica/migration/source-before.jsonl \
  --source-after .mica/migration/source-after.jsonl \
  --destination-inventory .mica/migration/destination.jsonl \
  --output .mica/migration/copy-verification.json
```

Inventory limits/truncation must be resolved; a partial listing cannot prove a complete copy.
Verification compares application/custom metadata and standard object metadata. It treats only
`goog-reserved-file-mtime` separately: `gcloud storage rsync` adds this bookkeeping field from
the source object's creation time when the field is absent. A new destination value must match
that UTC timestamp in integer seconds; an existing source value must survive exactly. Other
reserved metadata keys remain part of the strict metadata comparison. This allows the documented
[modification-time metadata](https://docs.cloud.google.com/storage-transfer/docs/metadata-preservation)
without accepting arbitrary metadata changes. The separate Storage Transfer Service marker
`x-goog-reserved-source-generation` is excluded from the metadata hash; actual destination
generations always come from the API, never this marker. Recapture inventories created by an older
tool version if source objects already had the modification-time field. A successful bulk-copy
check while the old stack is live still requires a final frozen sync before cutover.
Use `snapshot-proposals --help` for dry-run persisted-reference patches, and review those
proposals separately. Neither helper creates resources, transfers object bodies, nor changes
live records. A checked-in migration tool or successful offline plan does not mean any cloud
resource has been created; deployment evidence must record actual applies and checks.

## Cutover and rollback

Deploy admission-capable code to the old service with its own service ID and an explicit open
document before the window. Hold new dispatch, buffer/retry ingress, drain queued work/workers,
then allow old in-flight requests to finish before freezing writes and completing final sync.
Recheck no active transfers, all object checks and generation mappings, and shared Firestore
state. Long-running jobs postpone cutover unless their old-route compatibility is proven.

Prepare a separately reviewed `cutover` plan with `cutover_verified=true`, the actual new URL,
new identities/topics/queue/bucket, and verified secret/image references. Disable old schedules
and consumers in the coordinated handoff before enabling replacements. Khushi changes Slack
callbacks and approves the Gmail watch/topic/renewal handoff. Do not leave both watch renewers
active. Use admission replay ownership to drain buffered Slack requests before opening normal
dispatch, and preserve Gmail history/cursor continuity. Record each step and exact smoke path.

Before new writes, rollback can restore old routing/producers. After new writes, rollback needs
another hold and verified reverse delta/generation/task-state reconciliation before routing is
restored. Old resources remain available until old links, pinned objects, paused jobs and image
references are handled. Retirement is a separate exact deletion review, never the end of a
smoke check. Cleanup only recorded synthetic paths.

This work can be prepared overnight; the final write-freeze duration depends on active jobs,
copy delta and verification. It does not promise uninterrupted job dispatch during cutover.
