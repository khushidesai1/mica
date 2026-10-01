# Slack agent deployment — 1 October 2026

Current staging revision: `pistachio-staging-00083-car` (image previews disabled; direct conversation and living briefs;
Spot only for computation/data inspection; see follow-ups below). The initial Slack-first rollout used revision
`pistachio-staging-00073-ruj`.
The existing app, bot, HTTP webhook, Cloud Tasks queue and Batch Spot resources are reused.
The web agent code and science worker image are preserved. Real-data binding and a user-led
Slack conversation remain first-use steps; the deployment canary used synthetic data only.

## Prepared and verified

- The user saved version 1 of `pistachio-staging-anthropic-api-key` in Secret Manager.
  Key values were not displayed or copied into repository files.
- Worker build `47b91ab7-aff0-477b-be9c-8ccc2435f632`: SUCCESS.
- Worker image: `us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/slack-agent@sha256:b9c2441439dedb4eef41905bd18c75e8c452d596114d7b45651a34e8802ff81d`.
- API build `f72fe229-977f-453c-8fd3-15d09f6dcc43`: SUCCESS.
- API image: `us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/api@sha256:ebbbe6ecd4046389e156eb2880e2914ad0ff553f0abc1fc547f6f22c5c47e9c9`.
- Validated revision `pistachio-staging-00073-ruj`: healthy; promoted after the live gate passed.
- Preview URL: https://slack-preview---pistachio-staging-h5a7b7akbq-uc.a.run.app
- Live and preview `/api/v1/healthz`: HTTP 200. Preview credentials endpoint without identity:
  HTTP 401. Existing home-page authentication redirect remains HTTP 307.
- The API image layers only the three Slack integration changes and new agent files on the
  exact previous deployed image. The build verifies 53 existing files are byte-for-byte unchanged.
  This preserves the deployed web engine even though the local workspace has newer web code.
- `nat-router` / `nat-config` already covers all subnets in the existing network. No NAT was
  created. This corrects the earlier incomplete network inventory.
- Worker artifact permissions now include an objectAdmin condition limited to `projects/`.
  Existing permissions remain. No worker Secret Manager role was added.
- The artifact lifecycle preserves `projects/` and retains age-30 deletion for
  `tasks/`, `inputs/`, `incoming/`, and `paused/`.
- Synthetic fixture uploaded to
  `gs://pistachio-staging-artifacts-5304c19c/inputs/slack-fixture/spatial.h5ad`.
- Full regression suite: 246 passed, 4 skipped; Ruff and strict mypy pass.

## Live acceptance

Canary job: `pistachio-slack-canary-20261001-d` in `us-central1`, one n2-highmem-8 Spot instance.
Each Claude test turn is capped at $3. The canary does not post Slack messages. It checks the
existing Slack token with auth.test, uses the worker-authenticated preview endpoint for keys,
reads only synthetic fixture data, and tests sandbox isolation, plotting and session restoration.
Its report is stored under `tasks/slack-analysis/acceptance/20261001-d.json` in the artifact bucket.

Result: **SUCCEEDED**, 64.15 seconds of task runtime after Spot provisioning. The model was
`claude-opus-5-5`; the two successful turns cost $0.170637 in total (excluding earlier diagnostic
calls and cloud charges). The report confirms:

- Mounted data readable and not writable; outside-workspace writes blocked.
- API/Slack credentials absent from Bash and inaccessible through the private process filesystem.
- Direct Internet, proxied HTTP(S) and Google metadata-server requests blocked.
- A real sample-B plot and Python script generated from the 6,000-cell synthetic fixture.
- Session/workspace archive uploaded to GCS, local files removed, archive downloaded/restored,
  then the same Claude session edited and reran its original plotting script.
- Existing Slack token authenticated with all required scopes, including `files:write`.

The successful report and plot are under
`gs://pistachio-staging-artifacts-5304c19c/tasks/slack-analysis/acceptance/20261001-d.json`
and `.../20261001-d/plot.png`. The temporary credential grant was marked complete/inactive.
No actual Slack message or file was posted during the test.

Diagnostic history: the first canary failed before executing the agent because the Batch COS
host makes `/mnt` read-only. Host mounts/cache now use `/mnt/stateful_partition`; container
data paths remain `/mnt/gcs/...` with read-only bind mounts. The second canary authenticated successfully, but Docker blocked the inner private `/proc`
mount. The user explicitly approved the staging-only `systempaths=unconfined` exception.
The third canary started the sandbox and correctly blocked an outside write with EROFS,
which the original probe did not recognize. The corrected probe accepts only EACCES, EPERM
or EROFS and additionally verifies mounted-file write denial, direct/proxied network denial,
metadata-server denial and no exposed parent-process credentials. The final canary passed with the corrected pinned worker.

The user approved adding only `files:write` to the existing Slack app `A0C417ELV1N`, and the
app was reinstalled in azizilab. The existing verified webhook URL and `message.im` subscription
are unchanged. `users:read` remains absent; display names fall back to Slack user IDs.

## Cutover and rollback

Revision `pistachio-staging-00073-ruj` was promoted after live acceptance. Only five environment
settings differ from the original snapshot: the Slack backend, pinned Slack worker image,
allowed data bucket, Anthropic secret binding, and the approved proc-mount opt-in. Existing web
settings and the control identity are unchanged.

Post-cutover verification confirmed 100% live traffic on this revision, HTTP 200 health,
the existing HTTP 307 sign-in redirect, HTTP 401 for unsigned Slack events, and HTTP 401 for
unauthenticated credential requests. Older preview tags were preserved.

To roll back, route traffic to `pistachio-staging-00068-nih`; retain project artifacts and the new
retention policy. No user data is migrated or deleted by this rollout.

## First use and remaining acceptance

The existing app receives DMs via `message.im`; optional channel-mention scopes/subscriptions
were not added. In the existing Pistachio agent DM, send `help`, then
`init <project-slug> gs://bucket/prefix/` and approve its draft with `save brief`.
The synthetic test folder is `gs://pistachio-staging-artifacts-5304c19c/inputs/slack-fixture/`.
The follow-up enables automatic buckets in `azizilab-aml`, including the user-selected real bucket
`gs://khushi-crc-xenium/`.

Actual Slack reply/upload delivery, real project survey/brief approval, multi-user concurrency,
and intentional mid-turn Spot preemption remain user-led acceptance exercises. These are
covered by offline tests, but the deployment canary did not claim to execute them live.


## Follow-up: automatic project buckets and visible progress

User requested any bucket in `azizilab-aml` without per-bucket setup, and visible Slack receipt/work
updates. The follow-up adds `AGENT_DATA_PROJECT=azizilab-aml` and grants the control identity
`roles/storage.bucketViewer` (bucket metadata/get/list only). The worker already has a broader
project-wide `roles/storage.objectAdmin` role; this corrects the earlier objectViewer inventory.
No additional object-write permission is granted by this follow-up.

For each request, the controller discovers the project's current buckets, verifies the requested
bucket belongs to that project, and mounts only the selected bucket read-only. Frozen thread
contexts keep their original bucket after channel rebinding. Unsupported/external links produce
a visible error. Google Cloud Console and Storage URLs and Slack link formatting are normalized.

The Cloud Tasks handler posts a receipt before provisioning, updates that message while waiting
for Spot, then hands its timestamp to the worker. The worker reports readiness, tool activity, a
30-second heartbeat, and completion/errors. Startup updates occur once per elapsed minute.

Validation: 262 tests passed, 4 skipped; Ruff and strict mypy (76 source files) passed.
API build `643a9e01-7bdb-40b8-baff-af3c4aa8add2` succeeded with the existing web preservation gate.
Worker build `d0d0f9e5-6298-42d5-97bb-e94d6a2160de` succeeded.

- API image: `us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/api@sha256:05a3078800367ac1b720b2d11abdba9233bf2a22c2f7db7e492783cb1845a75e`.
- Worker image: `us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/slack-agent@sha256:8a401faf467b076a6a488529fe42ce36f2eff395897322109ec546185c81119d`.
- Revision `pistachio-staging-00075-nif` received 100% live traffic at that rollout (superseded by revision 77 below). Health, existing web sign-in
  redirect, and unauthenticated credential rejection verified after promotion.
- Retried the original failed init once via Cloud Tasks as event
  `df15b3175b47cc0c223fdacb88298756`, preserving its actual Slack message/thread. Conditional
  Firestore writes prevent overwriting a newer intake; a stable retry ID prevents duplicate retries.
- Verified the actual Slack status message `1790821541.274539` in that original thread and
  verified the job selected only `khushi-crc-xenium` for its read-only mount.
- Verified actual elapsed-startup updates in Slack, then the same message changed to
  `List data folder and show template — working (1m 0s)…`. At that point the real init was surveying
  the selected bucket; it was subsequently stopped as recorded below. This follow-up verified
  receipt, bucket selection, worker startup, tool activity and heartbeat delivery end to end.


## Follow-up: bounded init to eliminate recursive bucket scans

The user requested a fix for the slow setup and deployment. The previous init was stopped after
remaining in recursive filesystem listing for more than ten minutes. Its receipt is terminal
(`failed`), so the thread/config/concurrency leases can be released by the normal controller.

The replacement uses capped Storage metadata API listings and an enforced workspace-only init
tool policy. Live metadata measurement against `khushi-crc-xenium`: **1.42 seconds, 4 requests,
36 entries**, no file contents. Regular analysis retains its existing tools and isolation.
The standalone directory inspector also lists at most 100 immediate entries instead of rglob.

Worker build: `17862f74-98b3-410f-9729-fc60fe113a1c` succeeded. The API/web image is unchanged.
Worker digest: `sha256:bd15c0b2882723349e0f3baacfdacabcb3be07b739ffe8166be985285b1c7c2d`.
Revision `pistachio-staging-00077-zok` was health-checked and promoted to 100% live traffic.
Full tests: 267 passed, 4 skipped; Ruff and strict mypy (77 source files) pass.
The attempted retry was safely blocked before any write because newer input existed in the
thread. The latest receipt, `0e4972f1fe8d055b69e311df315da0bf`, is the user’s `cancel`
message and has completed. No replacement init was queued; the cancellation is respected.
A full live draft/upload with the new worker remains unverified until a new init is requested.
Post-promotion checks confirm revision 77 has 100% traffic, live/preview health succeeds, the
existing sign-in redirect is preserved, and unauthenticated worker credentials are denied.


## Follow-up: direct conversation and living project briefs

The user requested brief revision through ordinary chat and workers only when computation is
needed. `AGENT_DIRECT_CHAT_ENABLED=true` routes questions, planning, brief edits, help, brief
view/save, cancellation, and bounded init through the existing Cloud Run control service.
Claude can return a reply/revised draft or request computation; the direct model has no shell
or file-access tools. API failures do not fall back to a worker. Only data inspection, code
execution, and plotting requests submit Batch Spot jobs.

The draft can remain incomplete. Both direct and compute instructions accept corrections and
new project facts from chat, preserve unrelated content, distinguish evidence from guesses,
and maintain TODOs. Revisions apply to the current thread immediately; `save brief` publishes
for future threads. Uploading Markdown remains optional. The small GCS checkpoint stores the
current draft and recent conversation; the compute worker overlays those updates even when
restoring an older workspace archive. Worker results feed the conversation record afterward.
Cancellation bypasses the ordinary per-thread queue and deletes active compute work, retaining
its concurrency slot until termination is observed. No worker is started for cancellation.

- API build `02350d2e-aee8-47de-a220-3c390ee8d596`: SUCCESS, including web preservation checks.
- API digest: `sha256:baf343fb8d89c59981a26082f60e055415e5225edf421ae7afdedef76a1080c3`.
- Worker build `80e29f2f-b109-490b-8291-c5c02898b65a`: SUCCESS.
- Worker digest: `sha256:e5b876edd0b8caec39a727d51ef4868a77a1bd4e5f6b284c9cd2d45fc6ed3710`.
- Full suite: 281 passed, 4 skipped. Ruff and strict mypy (78 source files) pass.
- Real Claude API checks: brief edit selected reply and returned a revised draft in 5.6 seconds;
  context question selected reply in 3.4 seconds; plotting selected compute in 6.1 seconds.
  These used synthetic context without posting Slack messages or starting compute jobs.
- Preview health 200, existing sign-in redirect 307, unauthenticated worker credentials 401.
- Existing identities, bucket permissions, network and sandbox settings remain unchanged.
  The control service already has project-wide objectAdmin, sufficient for bounded metadata
  listings. This change adds no cloud or Slack permissions.

Revision `pistachio-staging-00079-hal` now serves 100% of live traffic. Post-promotion
checks confirm healthy live/preview endpoints, preserved web sign-in, and rejection of
unauthenticated worker credential requests.


## Follow-up: skip generated-image previews

Per the user's preference, the compute prompt now checks successful execution, nonempty output
files and computed summaries instead of opening figures for visual review. The Read hook blocks
images and PDFs before the CLI can emit their encoded contents. Full-resolution output creation
and Slack upload paths are unchanged; the SDK buffer and sandbox policy are unchanged.

- Worker build `3a6873b0-e527-4aad-bc76-feb49f4a36fe`: SUCCESS.
- Worker digest: `sha256:e1a50be73400880f6991dbc49790a655e45f5d659592e01a300299e5ad0c841f`.
- API/web image unchanged (`sha256:baf343fb8d89c59981a26082f60e055415e5225edf421ae7afdedef76a1080c3`).
- Full suite: 282 passed, 4 skipped. Ruff and strict mypy (78 source files) pass.

Revision `pistachio-staging-00081-tat` serves 100% of live traffic. Live and preview health,
existing sign-in, and worker credential rejection checks passed after promotion. The pinned
API image is unchanged and direct chat remains enabled. No analysis was automatically rerun.


## Follow-up: conversational working status

Replaced the direct-route implementation explanation with `Working on it…` as requested.
API build `b016ae51-7fc7-4dec-9a35-e62a675766aa` passed, including preservation checks.
API digest: `sha256:d598f3a0ec046ba0b4f55c0322c8b4a9be11d09e0f7eee2e0ff374443055d8a1`.
The no-preview compute worker and routing configuration are unchanged. The 10 direct-chat
tests and Ruff passed; this wording-only change adds no tests or permissions.

Revision `pistachio-staging-00083-car` serves 100% of live traffic; live health passed after promotion.
