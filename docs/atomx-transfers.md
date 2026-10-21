# AtoMx transfers

Ask Mica in Slack or email to connect AtoMx. It sends a private link to a first-party form,
where you enter your existing SFTP username and password. Links expire after 15 minutes and
open only once. Your saved connection belongs to your verified email identity, works across
your authorized projects, and is never shared with project collaborators. Ask to replace or
forget it to open the same form. Saving a login does not test it or approve a transfer.

Request a sample transfer and optionally its export folder, destination alias, and raw-data
base such as `gs://your-bucket/project/raw/`. An explicit destination takes precedence over
`PROJECT.md`; otherwise Mica uses a single clear raw folder documented there. If the guide is
ambiguous, Mica asks for the path. It does not use the analysis mount or a `processed` folder
as an implicit destination. Each sample gets a child folder under the selected base.
Additional files use a documented corresponding folder, or `.additional_files/<sample>/`
under that raw base. Progress/completion records live under `.mica-transfers/` there.

Mica inventories the requested sample using your connection, then privately sends a preview
with the selected source run, file count, bytes and exact destinations. Open its private form
to confirm. A changed connection or source requires a new preview. Completion means source
sizes and CRC32C matched the uploaded objects and the destination manifest was checked.
Existing different objects are never overwritten. `done.txt` in both sample folders points
to the detailed completion record. Ask for transfer status or cancellation in either interface.
Connection links and transfer notices go only to the requesting user, including email threads
with multiple recipients. Separate durable outbox stages leave the normal email reply intact.

## Runtime and operator setup

The existing Claude worker image includes the `atomx-transfer` tool. Transfer jobs invoke its
CLI directly; no language model runs in that job. The reviewed download-only adaptations of
`get_tma.sh` and `get_tma_addfiles.sh` are pinned to upstream
`5d864b6cd13f1873364aa70b9ac1939f4c656616`. The existing Python adapter selects and verifies
an exact manifest, and the scripts download that manifest using strict host checking. Uploads
use the existing create-only verification engine. Disk sizing follows the preview (15% margin
plus 40 GB; default maximum 2,048 GB), with a free-space check and automatic temporary cleanup.

Deploy with `cloudbuild/build-slack-release.yaml`; its offline AtoMx gates check encryption,
revocation, installed source and script dependencies. Set `AGENT_TRANSFERS_ENABLED=true`,
`AGENT_TRANSFER_HOST`, `AGENT_TRANSFER_PORT`, and `AGENT_TRANSFER_HOST_KEY` to the trusted
provider's public `key-type base64-key` record. Never acquire trust through `accept-new` or
an unverified `ssh-keyscan`. The default image and service account are the existing immutable
`MICA_SLACK_AGENT_IMAGE` and `MICA_BATCH_SERVICE_ACCOUNT`; optional transfer overrides remain
available. No per-user cloud secret, VM or service account is needed.

Credentials are AES-GCM encrypted in private Firestore connector records, keyed by normalized
verified email plus provider. HKDF derives a separate encryption key from the existing
`MICA_SIGNED_LINK_SECRET` (legacy setting aliases still work). The configured value must have
at least 32 bytes and 12 distinct bytes. Rotating that application secret invalidates existing
links and encrypted connections; users reconnect. Forget removes the live ciphertext. Platform
backups retain their normal retention policy. Link/session records contain only token hashes
and expire logically after 15 minutes; they do not contain passwords. GET requests cannot
consume links. The form uses an explicit POST, same-origin checks, an HttpOnly Secure SameSite
cookie, a CSRF token, no external assets, and no-store/no-referrer headers.

Worker bootstrap requires both the expected service-account OIDC identity and a separate
run-specific bearer capability delivered only in the trusted Batch runnable environment.
Only its hash is persisted in private controller state; it is absent from GCS request files,
model context and analysis-worker environments. The worker service account must not have
Batch job read/list or private controller datastore access. Operators with job-inspection
permissions are trusted administrators. Every bootstrap/token refresh rechecks the current
owner, connection revision, project access, destination and active job. Replacement/forget
immediately revokes new access; an already issued scoped GCS token can last up to five minutes.
The controller cancels obsolete jobs on its next durable poll.

Writes use five-minute downscoped tokens from the existing writer service account, limited to
this sample's two folders and three status objects. The worker's normal identity remains
read-only in data buckets. Destination prefixes must fall within `AGENT_WRITE_ALLOWED_PREFIXES`,
match the project's bucket, and have existing writer permissions (create/get/delete for data
and generation-checked markers). Collaborators are limited to the project's linked prefix;
project owners can explicitly approve imports elsewhere in the same bucket within the operator
ceiling. This does not change the project's analysis mount or grant cloud IAM.

A bucket absent from the write ceiling or writer IAM requires operator configuration before
transfers to it can succeed. Routine application deployments do not alter IAM or secrets.
Worker failure/cancellation is also recorded privately in Firestore; a killed worker can leave
a stale `.in_progress.json` marker until the next authorized transfer checks its age and lease.

The initial rollout deliberately omits a live SFTP canary at the owner's request. Users will
enter credentials and confirm real-data transfers themselves; offline checks do not establish
provider connectivity or effective write access to an arbitrary project bucket.

Private Slack links reuse the requester's existing one-to-one DM. Connecting from that DM requires
Mica's existing posting permission; it does not require permission to open another DM. Requests from
shared conversations can only send a link through a separate one-to-one DM. If Slack permanently
refuses that delivery, Mica gives a safe instruction to continue in its DM instead of retrying forever.
A delivery delayed past the link lifetime receives a fresh, unused link; the expired bearer stays
invalid. Already-consumed links are never revived or silently reissued by the same request.
