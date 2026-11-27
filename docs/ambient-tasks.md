# Ambient tasks

Mica can start a private worker thread when a new sample's transfer markers arrive. Tasks are
operator configuration, not model instructions. `AGENT_AMBIENT_TASKS` is a JSON list and defaults
to `[]`. Deploy it empty; enabling scientific processing is a separate operator action.

```json
[
  {
    "id": "sample-segmentation",
    "project": "p_0123456789abcdef",
    "enabled": false,
    "dry_run": true,
    "enabled_at": "2026-11-06T00:00:00Z",
    "authorized_by": {"email": "owner@example.org", "slack_user": "UOWNER"},
    "markers": ["raw_outputs/raw/{sample}/done.txt", "raw_outputs/additional_files/{sample}/done.txt"],
    "skip_if_exists": "resegmentation/{sample}/",
    "output": "resegmentation/{sample}/",
    "recipe": "recipes/ambient_segmentation.md",
    "recipients": [{"email": "owner@example.org", "slack_user": "UOWNER"}],
    "worker": {"tier": "standard", "on_demand": true, "max_hours": 1},
    "return_scripts": false,
    "max_concurrent": 1,
    "max_starts_per_day": 4,
    "task": "For {sample}, validate {markers}, follow {recipe}, publish under {output}, and deliver the segmentation report."
  }
]
```

Use real project/member identities when enabling. Slack workspace identity comes from the bot's
`auth.test`, not from text. The owner and every recipient must resolve to their configured email
and have current project access. The owner must still own or co-own the project. The recipe must
exist in that project's GitHub memory. Paths are literal bucket-relative templates with exactly
one `{sample}` segment; output folders end in `/`. No traversal, formatting expressions, wildcard
paths, duplicate recipients or CPU dollar-budget settings are accepted. Project/bucket/write
ceiling checks happen against the live registry at startup and publishing.

Enabled tasks keep their configured recipes active during memory saves and index compaction.
The recipe takes priority in the bounded index and cannot be deleted while its task is enabled.
Workers receive only their project's task definitions so their memory saves enforce the same rule.
Disabling a task removes that retention requirement. A previously archived recipe must be restored
to its configured path; blocked arrivals then retry automatically without another CPU approval.

## Discovery and admission

The existing five-minute Batch reconciliation route runs an isolated ambient scan with a
45-second budget. Delimiter listings visit one page of 50 sample folders per task, retain the
page cursor, and wrap at the end; they never recurse through whole sample contents. Queue work
is bounded at 500 records per scan. All configured marker objects must exist. Their creation
times and generations identify a create-only Firestore receipt named
`ambient-arrival:<task>:<sample>:<generation-hash>`. Identical generations never retrigger;
replacing markers creates a new arrival linked to the previous receipt. Existing
`ambient:<task>:<sample>` receipts remain valid first arrivals without migration. A separate
`ambient-sample:<task>:<sample>` pointer identifies the latest receipt. A marker older than
`enabled_at` ignores only that arrival; newer generations of the same sample can still trigger.
Markers changed before admission supersede the queued arrival instead of blocking it.

Each arrival goes through the ordinary owner conversation with project memory, source reads
and folder listings. Mica chooses whether to run the configured task, skip with a reason, or
ask the owner. Marker times, existing output, and up to ten linked earlier arrivals (outcome,
decision and thread) are facts for that decision. The compatible `skip_if_exists` field names
the output folder to report; it no longer skips work automatically. Earlier receipts remain
linked beyond the bounded context window.

Code replaces any model-selected worker resources with the operator preset, clears reference
repositories and overwrite requests, and permits publishing only through the create-only
event grant. Any other proposed action becomes an owner reply requesting an ordinary follow-up;
queue control, preferences, transfers, access changes and PRs cannot execute during triage.
The worker receives the configured run instructions, while the receipt records the triage route
and first line of its reason. Session imports retain their separate trusted compute path.

Queued samples start in marker-completion order, subject to per-task concurrency, UTC-day start
limits and the normal worker slots. Start limits count admitted arrivals, including replies.
Admission is serialized across scans and reserves a start
before posting. The worker allocator still enforces its slot leases. Failed starts remain
recoverable using their original root message, event identity, deadline and grant. Use a new
task ID if changing authority after startup has already posted notices.

With `enabled: true, dry_run: true`, scans only read and log decisions: no receipts, leases,
Slack messages or jobs. Disabled tasks are not scanned. Missing recipes or changed access block
startup and are rechecked on subsequent scans. A single owner notice reports a block when that
Slack identity can still be verified; no project detail is sent to an identity mismatch.
Malformed dependency responses, HTTP/transport failures and unavailable identity checks remain
retrying, with no policy-block alert. Retries retain the same reservation, event, grant and
deadline. Successful admission clears the old startup reason. Reconciliation also clears stale
reasons on already-accepted receipts (including finished runs) and updates a confirmed private
blocked notice to say the run recovered. The edit rechecks owner identity and project access,
never posts a replacement,
and retries delivery without restarting the run. Missing recipes and actual permission or
scope changes still block admission.

The owner gets a new DM thread explicitly attributed to their configured automation. Other
configured collaborators get a start notice only when a compute job is submitted, after any
required approval and capacity checks, and the final summary/PDF under that notice. A skip or
question is delivered only in the owner's thread. Recipient lists are frozen at admission;
current membership and identity are checked before delivery.
Replies are ordinary private project threads. Delivery receipts suppress retries; a network
failure with an uncertain Slack send is reconciled by message/file identity. If Slack history
cannot prove what happened, delivery remains pending for operator reconciliation rather than
blindly posting again. No collaborator is selected by model output.

## Event grants and deadlines

The controller creates `write-access/events/<sha256(event_id)>.json` in Mica's artifact bucket.
The grant binds one event, project, owner, sample, issuer and normalized output folder. Its ID
has a distinct namespace. The publisher rechecks ownership, expiry, project bucket, operator
write ceiling and issuer configuration on each operation, together with the existing identity,
cancellation and stopped-attempt checks. An invalid event grant never falls back to a broader
project grant. Writes use generation-zero preconditions; overwrite approvals cannot authorize
replacement in an ambient run. A follow-up message is a new event and uses ordinary project
write grants.

The output grant already contains the sample folder. Stage `outputs/gcs/images/`,
`outputs/gcs/points/`, and the other result directories relative to that root. Both the worker
and controller reject a repeated bucket-relative grant prefix before publication, preventing a
nested duplicate such as `resegmentation/TMA37/resegmentation/TMA37/`.

The start fixes `deadline_at` to the smaller of `max_hours` and the deployment's runtime ceiling.
Every CPU/GPU attempt uses that same absolute deadline, including resize, preemption and
continuation. GPU starts additionally require the existing requester-bound approval. The write
grant expires at that deadline plus one configured upload window. CPU presets do not accept
`compute_budget_usd`; there is no CPU billing cap. Tier, runtime, concurrency and starts per day
bound configured resource use; ambient CPU runs cannot resize above their configured tier.

GPU tasks also require the existing structured `gpu_sizing` fields in `worker` (host RAM,
GPU RAM, vCPUs, GPU count, basis and evidence); optional `gpu_type` selects an enabled family.
These requirements come from the operator's recipe assessment. The scanner does not guess
resource needs. The owner must approve the resulting hardware request in the run thread.

Disabling/removing a task or changing its project, owner or output stops new starts and immediately
blocks further publishing under its event grants. Existing compute continues until its deadline
or cancellation in the owner's thread. Changing only recipients or worker limits affects future
starts. To roll back, empty `AGENT_AMBIENT_TASKS`; do not widen write permissions.

## Completeness and reports

The spatial-analysis skill supplies `check_cosmx_sample.py`. It requires both verified marker-v1
text records and their authoritative JSON, compares exact file/size listings and FOV sets, and
reads TIFF headers without decoding pixels. Listing-only backfills fail. These checks do not
reread all checksums or establish that a scientifically meaningful result is possible.

The `segmentation-report` skill produces one self-contained PDF for polygon or label
segmentations. The summary, comparison panels, flags, complete review and method details stay
in that PDF, with no page cap. Its default `pdf_only: true` emits no JSON or PNG sidecars. It requires verified scale, channels and
coordinate alignment. Image reads use a low-resolution overview and full-resolution crop
windows; labels and transcript sampling are streamed. The worker views crops, adds visual-review
notes, and delivers the report directly to the configured recipients.
Report version 2 anchors crops to actual cells and shows identical stain pixels alongside
prior/final mask overlays, with channel legends and calibrated scale bars. Per-FOV assignment
ranking requires a disclosed minimum of 100 sampled transcripts; unsupported fields remain
visible as sampling limitations. Same-sample count/area comparisons are separated from cohort
context, and missing cohort transcript data is not plotted as a comparison. Background-only
crops or invisible masks must be corrected before delivery.
Keep only the final PDF in the attachment folder. Publish scientific data separately; report
PDFs, QC bundles and review intermediates do not go to the project bucket unless explicitly
requested. `return_scripts: false` on a task suppresses automatic script ZIPs without changing
the owner's preference for ordinary conversations. Omit it to inherit that preference.
Already-started tasks retain their saved setting. Before final delivery, the worker validates its thread-memory JSON and lets
the same bounded model turn correct malformed prose; identity and coverage remain application-owned.

## Release and smoke checks

Use the coordinated `cloudbuild/build-slack-release.yaml` pipeline, preserving the current
service configuration and compatible image pins. Its report gate uses tiny synthetic local
inputs under `/opt/sap`; no new image family, infrastructure, IAM or secret changes are needed.
For the first rollout of generation-based arrivals, list changed-generation arrivals read-only
for each enabled task before promotion. Advance only each live task's `enabled_at` to the
rollout cutoff in the verified configuration update, preserving every other field, to avoid
replaying historical arrivals or duplicating manual work. Older revisions ignore the new
per-arrival receipts if rolled back.

Preserve existing operator task configuration during subsequent code releases. Do not clear an enabled
task or its recipients; configuration changes require their own verified rollout.

Before enabling a scientific task:

1. Run its discovery in dry-run mode with an old cutoff and inspect existing-sample decisions.
2. Use an isolated project owned by the operator and an exact `_ambient_smoke/<id>/` prefix
   already within the write ceiling. Record every created GCS, memory and connector-state path.
3. Configure only the owner's DM, the standard tier and a one-hour allowance. Upload a tiny
   verified sample and a smoke recipe that checks it, reports it and publishes `qc/`.
4. Verify one root, one run, one delivered PDF, a terminal receipt, no retrigger, correct event
   grant metadata and the one-hour deadline. Do not message collaborators for this check.
5. Replace the markers after noting in memory that the earlier copy was archived; verify a
   linked arrival and observe whether Mica runs or asks. Then record that the re-transfer is a
   duplicate, replace markers again, and verify an owner-only skip with no job.
6. Disable the smoke task, delete the exact synthetic prefixes/state/config and temporary DM
   messages, and verify deletion. Do not run full scientific segmentation for smoke validation.
7. The project owner reviews a report on an existing published sample, finalizes the project
   recipe and explicitly enables the real task with `enabled_at` set to the current time.

The first genuine arrival verifies the scientific end-to-end workflow. Unit tests and synthetic
smokes do not replace owner acceptance of the report or biological parameters.
