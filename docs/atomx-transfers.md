# AtoMx transfers

Ask Mica in Slack or email to connect AtoMx. It sends a private link to a first-party form,
where you enter your existing SFTP username and password. Links expire after 15 minutes and
open only once. Your saved connection belongs to your verified email identity, works across
your authorized projects, and is never shared with project collaborators. Ask to replace or
forget it to open the same form. Saving a login does not test it or approve a transfer.

Request a sample transfer in ordinary language, such as “start that transfer.” Mica uses the
current request, project documents, conversation history, previous personal transfer requests
and folder listings to propose the source selection and destinations. You do not need to repeat
paths or save a guide before receiving the preview. Current corrections take precedence; Mica
asks only when the available context cannot resolve the intended sample or layout. Recent
personal transfer requests are available across threads in the requester's private conversation;
they are suggestions, not approval or evidence that a previous transfer completed.

You may supply an export folder, destination alias, and raw-data parent base such as
`gs://your-bucket/project/raw/`, or the final sample folder `gs://your-bucket/project/raw/TMA37/`.
Mica appends the destination sample name exactly once. An explicitly proposed destination takes
precedence over the guide; omitted destinations can use a single documented raw base. Ordinary
prose such as “raw images” is not treated as a path. The analysis mount and `processed` folder
are not implicit destinations. Each sample gets a child folder under the selected base.
Additional files use a documented corresponding folder, or `.additional_files/<sample>/`
under that raw base. Progress/completion records live under `.mica-transfers/` there.

Name the sample as you know it. Without an export folder, the inventory searches up to three
folder levels below the SFTP root for export folders (folders that contain `DecodedFiles`). Within
them it matches slide folders without regard to case, separators, leading zeros, a bare number
(`37` for `TMA37`) or an appended AtoMx timestamp. An exact folder name wins over loose matches.
An export named for the sample (for example `TMA37_05_11_2026_22_59_07_541`) that holds a single
slide folder also matches, whatever that slide folder is called. The latest timestamped run is
used, directly under the slide folder or one level below it. The bucket folder stays the
requested name (or `as` name). With an export folder, the search stays under that folder; a leading
slash is optional. If nothing matches, the private notice lists the export and slide folders the
search saw. If several match, it lists them and asks you to name one, for example
`transfer "Slide 1" from /TMA37_05_11_2026_22_59_07_541 as TMA37`.

Mica inventories the requested sample using your connection, then privately sends a preview
with the selected source run, file count, size and exact destinations. In Slack, review the
approval card and choose **Approve transfer** or **Cancel transfer** directly in the conversation.
No web approval link is needed. Email retains its private confirmation form. A changed connection
or source requires a new preview. Completion means source
sizes and CRC32C matched the uploaded objects and the destination manifest was checked.
Existing different objects are never overwritten. `done.txt` in both sample folders points
to the detailed completion record. Ask for transfer status or cancellation in either interface.
Connection links and transfer notices go only to the requesting user, including email threads
with multiple recipients. Separate durable outbox stages leave the normal email reply intact.
In an existing Slack DM, the transfer takes over its request's activity message. File checks,
queued/running progress and the final result use the same compact status dropdown as Mica's
other activity. The approval card replaces that status while awaiting a decision; later updates
edit the same message. Full source and destination paths remain visible on the approval card.
A new top-level request starts a thread under that message. Requests from a shared channel use
one separate private message, never publish export details to that channel, and bind approval
to the requester and the privately delivered card. If a login is needed, **Connect AtoMx** opens
the secure credential form; passwords still never enter Slack.

Each approval button binds the displayed source plan, destinations, connection revision, attempt
and expiry. The signed Slack action must match the original private message, workspace and
requester; the controller rechecks the bound plan, current access, login and destination markers
under its existing project lock. Duplicate, stale or copied clicks cannot approve another plan.
The transfer's ordinary scheduled poll owns expiry; no new timer or approval system is added.

Private message timestamps and delivery intent are durable. Repeated status updates are deduplicated,
and a failed update retries the same message. An ambiguous first post is held and reconciled against
a bounded Slack history window for Mica's exact message ID before it can continue; it is never blindly
reposted. If history access is unavailable, the delivery remains held for operator investigation.
Native activity rejection falls back to standard Slack blocks with the same status and controls.
Historical messages are not deleted or replayed by deployment; the next ordinary update adopts the
saved request activity when available. No approval or real transfer is triggered by that update.

## Runtime and operator setup

Context-based destination proposals and sample-folder normalization run in the control plane.
An API-only change to this flow uses `cloudbuild/build-slack-api.yaml`, including its installed
transfer checks and existing worker compatibility gate; preserve the transfer/analysis worker
image pins and runtime settings. No application release confirms an existing transfer request.

The existing Claude worker image includes the `atomx-transfer` tool. Transfer jobs invoke its
CLI directly; no language model runs in that job. The reviewed download-only adaptations of
`get_tma.sh` and `get_tma_addfiles.sh` are pinned to upstream
`5d864b6cd13f1873364aa70b9ac1939f4c656616`. The existing Python adapter selects and verifies
an exact manifest, and the scripts download that manifest using strict host checking. Uploads
use the existing create-only verification engine. Disk sizing follows the preview (15% margin
plus 40 GB; default maximum 2,048 GB), with a free-space check and automatic temporary cleanup.
The worker keeps UID/GID `65532` and a locked, noninteractive local account with home
`/srv/pistachio`. OpenSSH requires that local account independently of the requester's SFTP
login. The installed-worker release gate resolves the account and runs OpenSSH's configuration-only
startup under the actual worker user, with networking disabled, before the image can be released.
The gate also runs both download scripts through OpenSSH's configuration parser and checks that
password authentication remains enabled with strict host checking. `BatchMode=no` must precede
SFTP's `-b` option, which otherwise disables the private-pipe password helper.

Deploy with `cloudbuild/build-slack-release.yaml`; its offline AtoMx gates check encryption,
revocation, installed source and script dependencies. Presentation/controller-only updates can use
the existing API-only pipeline and preserve the verified transfer worker image after source and
worker-contract checks. Check transfer slots and active jobs before rollout; do not deploy during a
user's running transfer. Set `AGENT_TRANSFERS_ENABLED=true`,
`AGENT_TRANSFER_HOST`, `AGENT_TRANSFER_PORT`, and `AGENT_TRANSFER_HOST_KEY` to the trusted
provider's public `key-type base64-key` record. Never acquire trust through `accept-new` or
an unverified `ssh-keyscan`. The default image and service account are the existing immutable
`MICA_SLACK_AGENT_IMAGE` and `MICA_BATCH_SERVICE_ACCOUNT`; optional transfer overrides remain
available. No per-user cloud secret, VM or service account is needed.

The controller resolves and authorizes destination prefixes per request, then supplies them
through authenticated worker bootstrap. Workers use those destinations as their only layout
source and check the request bucket, run ID and job kind against the authorized job. They do not
read a per-bucket YAML file from the image. The offline release gate runs a layout-free inventory
request through this entrypoint as well as checking the installed source.

Failures include a bounded step (`config`, `bootstrap`, `sftp_auth`, `sftp_listing`, `staging`,
`upload`, `verify`, or `markers`) and an allow-listed error class in the job result and log.
Private failure notices use those categories; raw exception messages, server text and credentials
are never included. A failure before the marker stage may have no data-bucket status record.

## Manual verification and backfill

Operator commands use the same destination mapping with your Application Default Credentials.
Supply a local `--config` YAML file with the exact destinations from the request:

```yaml
destinations:
  bucket: your-data-bucket
  raw_prefix: project/raw/
  additional_prefix: project/raw/.additional_files/
  status_prefix: project/raw/.mica-transfers/
```

`atomx-transfer verify --config /path/to/config.yaml --sample SAMPLE` checks the recorded manifest
against the configured sample folders. `atomx-transfer backfill --config /path/to/config.yaml`
previews listing-only completion records; add `--yes` to write them. A backfilled record is not
source verification and `verify` only accepts it with `--allow-backfilled`. Both commands work
without SFTP credentials and exclude nested additional/status directories from sample discovery.
Backfill can also repair missing text markers from an immutable completed record.

Manual `inventory` and `pull` use this same `destinations` block plus an `sftp` block containing
`host`, `port` and an independently verified `host_key` (`key-type base64-key`). They prompt for
the login; never store a password in this file. No example host-key placeholder is shipped as a
usable configuration. Job inventory remains read-only; job pulls always use the controller's
downscoped writer, independent of these operator commands.

## Credentials and authorization

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
