> Source behavior update, 10 October 2026: [the routing and memory implementation plan](worker-routing-and-memory-plan.md) supersedes the pricing/quote gates, thread-draft guide flow, knowledge feature flag, and per-thread channel summaries described below. Those passages remain historical design/deployment evidence. This local implementation has not been deployed or validated in staging.

## Email requests and renamed project state — 2026-10-20

Revision `pistachio-staging-rename-554fb1e` serves staging with coordinated build
`8684099b-cc93-4729-af67-6c33e08500fd` from source `554fb1e`. It includes the email
analysis route, shared private-project defaults, authenticated worker email delivery,
and memory/storage name mappings. Gmail intake remains disabled pending mailbox-owner
OAuth provisioning and watch activation; see [deployment](deployment.md#gmail-activation).
The intended mailbox alias is `azizi.lab.341+mica@gmail.com`, with label `Mica`.

Validation: Ruff and mypy passed; 970 tests passed and 6 skipped. All configured build gates
passed, including headless API/source checks, worker source/compute checks, and shared-worker
schema compatibility. Optional spatial sizing retained its configured skip. Preview health
returned 200, removed routes 404, and unsigned Slack/Gmail ingress and unauthenticated email
callbacks 401. Runtime settings were preserved apart from the coordinated image references.

The `crc-cosmx` memory guide moved atomically from the legacy folder into
`projects/crc-cosmx/`, with its stable-ID mapping in `.mica/projects.json`, in memory commit
`a4d9a2982f5d0fe354994deb9a246668724c410e`. Existing guide content was preserved. Future
rename commands synchronize the registry and memory before confirming completion.

The staging prefix `projects/pista-cosmx-6k-crc/` was copied to `projects/crc-cosmx/`:
94 objects, 40,251,270 bytes, verified by size and checksums. The storage index redirects
existing logical keys to the new folder. The owner requested retaining all 94 originals;
they are backups, not active destinations. Task dispatch was paused with no active workers
or in-flight tasks during cutover, then restored. No synthetic smoke objects were created.

Local state now uses `.mica/`, including linked worktrees and new script checkpoints.
Old script checkpoints still restore verified contents. Evidence is retained locally at
`.mica/deployments/email-requests-20261019/`. Rollback must preserve the folder mappings or
reconcile physical data before using older writers; simply restoring an old image is unsafe.

Immutable images:

- API: `us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/api@sha256:461f1e15f8ec9644bbd165ce7dd79b899032890846b8516ae9378b6ee8f90645`
- Claude worker: `us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/slack-agent@sha256:6ee24f2651a3013f58ffcc3ad03796daf755a64760b27df2fe5c1539429ae9b1`
- Shared science worker: `us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/science@sha256:86acffefebb4bf0c928623382b7cbc67cdcafcb3607eea595f693d3704fc6249`

# Verified deployment source recovery — 8 October 2026

The live service `pistachio-staging-resize-20261006` serves 100% of traffic in
`azizilab-aml/us-central1`. Its API, Slack spatial worker, and shared science worker
all match successful Cloud Build `208c3a12-186a-4cbf-b4ba-77fec73dd4d7`, built
7 October 2026 UTC (6 October Eastern). Worker tiers are enabled through `xlarge`.

The exact versioned source archive was recovered into `main`; its SHA-256 matches
Cloud Build provenance. The build used source label
`worktree-06c398dd8387f9ae981e1e40d8f0d862d8d7bb5c4c03596ddef1343905e777c3`.
See [the recovery manifest](deployed-source-20261008.json) for immutable image digests,
archive generation, and per-file hashes. All 11 build steps succeeded, including
source consistency, API/worker compatibility, compute, and spatial sizing checks.

This source adds long-running jobs, worker resizing, resource measurements,
checkpoints, runtime provisioning, and transfer/spatial tooling. Application source,
tools, templates, and available build files were recovered byte-for-byte. The README
was then updated for canonical `main` and valid documentation links. Tests,
documentation, Terraform, and `config/tools.yaml` were absent from the build archive;
the repository copies are retained and are not verified as the original release versions.
An older Batch test was updated from the former one-hour limit to the deployed seven-day
limit, using `total_seconds()` to include whole days. Existing local planning documents
remain uncommitted and are preserved separately.
No cloud deployment or infrastructure change was made during recovery.

Recovery validation: Ruff passed; strict mypy passed for 114 source files; compilation
and the headless API gate passed. The retained offline suite initially had 551 passing
tests and three failures; all three passed in focused reruns after updating the stale
timeout assertion, installing the already-pinned PDF dependency, and permitting the
local Unix socket test. Three tests were skipped and three deselected. The full
container-only release gate was not rerun locally because `/opt/pistachio` is absent;
its source, long-run, and attachment checks passed before that environment limitation.
The successful Cloud Build supplied the full container release-gate evidence.

`main` is now the canonical development branch. The historical handoff branch is
fully included in its history and can be removed. The older deployment records below
are historical; use the recovery manifest for the verified image inventory.

---

# Slack agent deployment — current release 6 October 2026 UTC

Previously recorded revision: `pistachio-staging-writes-20261006`, serving 100% of traffic. This
release adds scoped GCS output grants and one structured Slack command summary. Historical
pre-cutover rollback instructions below do not apply to the current private-project runtime.

## Project-linked GCS write grants and structured help — 6 October 2026 UTC

`help` now shows one grouped bullet list with a concise description for each command.
`help all` remains an alias; `help write` provides optional examples. In a DM, designated
write admins can grant, inspect, or revoke create-only output access. Omitting duration means
no expiry, until revoked. The issuer must belong to the selected project, and the destination
must be in that project's linked data bucket. Those associations are checked again during
publishing. Existing objects cannot be overwritten or deleted.

- Deployed source: `b9abc9f2132eb76bc99000cad1041446fc8b0389` (local branch
  `codex/private-project-memory`). API and Slack worker were built from the same 90-file source
  snapshot; the shared science image is unchanged.
- Local verification: 553 passed, 6 skipped before the final help reformat; all 91 affected
  project/write tests passed afterward. Ruff, strict mypy, and Terraform validation passed.
- Build `4b6b7a17-6dce-4606-b801-162b2a9cd137`: SUCCESS, all seven steps passed, including installed-source
  consistency, structured help and write commands, worker previews, headless API routes, and
  compatibility with the existing science worker.
- The dedicated `pistachio-staging-writer` account has only `storage.objects.create` and
  `storage.objects.get`, bound to `alchemy-breast-cancer` and `khushi-crc-xenium`, the two
  currently linked project buckets. The existing onboarding admin is also the designated
  write admin. New buckets require administrator provisioning before grants can target them.
  No project write grant was created as part of deployment.
- Only the control account can impersonate the writer. Each upload receives an object-scoped
  short-lived credential inside the trusted publisher. The existing project-wide
  `roles/storage.objectAdmin` bindings were removed from both control and worker identities;
  project-wide reads and required artifact-prefix writes were preserved. The existing legacy
  ACL setting on `khushi-crc-xenium` was preserved.
- A temporary Cloud Run job using the real control identity passed 15 live checks: publishing
  and retry reconciliation in both buckets, denied sibling-object writes, denied overwrites,
  denied deletes using scoped and unscoped writer credentials, revoked/expired grants blocked,
  and an unapproved bucket denied. These used disposable objects and synthetic local grants;
  the live project registry was not changed.
- Preview and live checks passed: health 200, removed UI/docs 404, unsigned Slack events 401,
  unauthenticated worker credentials 401, and unauthenticated output-publishing requests 401.
  Runtime configuration was preserved apart from the image pair and three write settings.
- Cutover found zero queued tasks and zero active Batch jobs. The revision serves 100% of
  traffic, dispatch is RUNNING, and the temporary preview tag has been removed. Artifact
  lifecycle remains 30 days for temporary prefixes; `write-access/` and `projects/` are retained.
- After IAM restriction, both runtime identities passed 18 further live checks. Required
  artifact writes and data reads still work; direct writes into either data bucket are denied.
  Workers cannot write grant records or impersonate the writer. All three verification jobs
  and their disposable objects were cleaned up.
- No paid model calls or Slack messages were initiated for deployment verification.

Deployed image digests (repository prefix
`us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/`):

| Image | Digest |
| --- | --- |
| `api` | `sha256:eb35dad83008de2ebbff1b3152c45368f1ac0ad91f357855e1de5184a54fdcfa` |
| `slack-agent` | `sha256:2348e9a1ee304b9b69df9a84ca71c2f94d03c81d09a31a0a8e8edbef69b593f7` |
| `science` (unchanged) | `sha256:0972b8ae1e37a272b0ed54dae18b3412f034c47e83520b5805437bfb8f0a8041` |

Evidence, immutable source, runtime snapshots, live IAM policies, and canary results:
`.mica/deployments/slack-write-access-20261006/`. The preceding compatible API/worker pair
is `pistachio-staging-memory-20261006`, recorded below. Roll back the pair together after draining
work; keep the narrowed IAM grants. The old pair does not publish new GCS outputs.

This rollout used targeted cloud changes rather than a full Terraform apply. Before a future
Terraform rollout, reconcile the existing stale variables/state with the live release and import
the manually provisioned writer account/custom role and IAM members. Set the write-admin and
write-prefix variables to the live values above. Do not apply old default-empty write settings
or old image variables to this service.

## Separate conversation memory from project documentation — 6 October 2026 UTC

DM conversation summaries are scoped to person + project; channel summaries stay in their own
thread. Each is capped at 6,000 characters, injected separately into chat and compute requests,
and retained until cleared. Normal updates share the existing background editor call, remain
silent, and preserve SDK sessions. Clear commands invalidate queued/running context using reset
epochs. The project guide retains concise, necessary auto-updates but excludes observations,
hypotheses, results, and research TODOs. Legacy cleanup remains a reviewable thread draft with
its original preserved; only `save brief` publishes it. `memory context`, `forget context`, and
`clean brief` are documented in the setup guide.

- Deployed source: `5bcc3fd87d5d802c81802ee322dbe94701aa0067`, pushed to
  `codex/private-project-memory`.
- Full local suite: 488 passed, 6 skipped; subsequent focused checks passed (including one
  additional compute-cleanup regression). Ruff and strict mypy passed for all 87 source files.
  Coverage includes cross-thread continuity, scope isolation, concurrent updates, stale workers
  after clearing, failed-run exclusion, restored workspace overlays, full guide review, and
  preservation of cleanup originals.
- Build `4f785829-ba82-4ccd-b887-412432432831`: SUCCESS, all seven steps passed. Both images
  contain the same 87-file application snapshot. Gates verified conversation memory and guide
  policy, template freshness, attachment transport, PNG/PDF previews, headless routes, and
  compatibility with the unchanged shared science worker.
- The preview and live service passed health (200), removed UI/docs (404), unsigned Slack event
  (401), and unauthenticated worker credential (401) checks. Runtime configuration was preserved
  apart from the API and Slack worker images. No new dependencies or infrastructure were added.
- The existing analysis was allowed to finish successfully. After its completion poll drained,
  dispatch was paused; the final check found no queued tasks or active workers. The new revision
  now receives 100% of traffic, dispatch is RUNNING, and the preview tag was removed. No
  error-level revision logs appeared during release checks.
- No paid model calls, user analyses, or Slack messages were initiated for release verification.
  The opt-in observation-versus-guide model evaluation was added but has not been run. Existing
  briefs are reviewed on subsequent requests or `clean brief`; they were not bulk rewritten.
  Upload retention remains 30 days.

Deployed image digests (repository prefix
`us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/`):

| Image | Digest |
| --- | --- |
| `api` | `sha256:3e078e02fcd563d6413a66d86fc342d01377e1fc102cc588af3b360975c682c1` |
| `slack-agent` | `sha256:104d3292c674ba035c43e6ea1472cfee483a8bc6244523a3889087b1d480f5f5` |
| `science` (unchanged) | `sha256:0972b8ae1e37a272b0ed54dae18b3412f034c47e83520b5805437bfb8f0a8041` |

Evidence, immutable source, runtime snapshots, and drain checks:
`.mica/deployments/conversation-memory-20261006/`. The preceding API/worker pair is revision
`pistachio-staging-attachments-20261005`, recorded below. Always drain and deploy the pair
together. After accepting new memory operations, do not run an old writer that drops reset
markers; preserve/reset the new caches before any re-enable following a rollback.

## Attachments and image vision — 5 October 2026

This source change adds streamed Slack uploads, bounded sandbox previews, router media and
`view` requests, and a persistent thread file registry. User uploads use 30-day `tasks/` retention;
output provenance remains under `projects/`. No API dependency or infrastructure change is needed.
The worker adds pinned `pypdfium2==5.14.0`; Pillow is already in the science image.

- Deployed source: `dba922971d41df4174a8827a7adafa3886344544`, pushed to
  `codex/private-project-memory`. Local verification: 467 passed, 5 skipped; Ruff and strict
  mypy passed.
- Build `957e1a5f-f2c6-4ed0-b424-3464150e16c8`: SUCCESS, all seven steps passed. Both images
  were checked against the same 85-file application snapshot. Offline image gates covered
  attachment transport, real PNG/PDF previews and buffer bounds, headless routes, and
  compatibility with the unchanged shared science worker.
- The tagged preview and live service passed health (200), removed UI/docs (404), unsigned
  Slack event (401), and unauthenticated worker credential (401) checks. Runtime configuration
  was preserved apart from the API and Slack worker images.
- Dispatch was paused for promotion; the drain check found no queued tasks, in-flight
  dispatches, or active Batch jobs. The new revision receives 100% of traffic, dispatch is
  running again, and the temporary preview tag was removed. No error-level revision logs
  appeared during release checks.
- Existing bucket lifecycle rules were verified to expire uploads under `tasks/` after 30 days.
  No paid model calls, user analyses, or Slack messages were used for verification; end-to-end
  model vision behavior has not been live-tested.

Deployed image digests (repository prefix
`us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/`):

| Image | Digest |
| --- | --- |
| `api` | `sha256:9c12591914d8cdb43c80802baab87c2f1419a501c7f2ec8b032aab4d63d16f44` |
| `slack-agent` | `sha256:4efd1144189b5288e6bff3907a7acca4fa00a2852ca26aa84397ed309cf6d6e7` |
| `science` (unchanged) | `sha256:0972b8ae1e37a272b0ed54dae18b3412f034c47e83520b5805437bfb8f0a8041` |

Evidence and the immutable source bundle: `.mica/deployments/attachments-20261005/`.
The prior compatible revision is `pistachio-staging-brief-20261005`; its API/worker pair is
recorded below and in the private release manifest.

### Rollout procedure

Obtain human approval before **each** cloud build, configuration change, deploy, and paid live test.
Do not infer permission from completion of local tests. The rollout sequence is:

1. Run `.venv/bin/pytest -q`, `.venv/bin/ruff check src tests tools`, and
   `.venv/bin/mypy src/mica`. The opt-in `test_agent_looks_at_its_figure` remains skipped
   unless `MICA_RUN_LIVE_TESTS=1` is explicitly approved.
2. Build API and worker images **from one commit**. For a deployment onto the existing pinned
   runtime, use `cloudbuild/build-slack-release.yaml`: it preserves the API dependencies, adds the
   pinned PDF renderer to the worker, and verifies installed source, attachment transport, real
   PNG/PDF previews, headless routes, and compatibility with the existing shared worker. Fresh
   builds may use `build-slack-api.yaml` and `build-slack-agent.yaml`. The checkpoint and manifest
   fields must agree.
3. Drain queued/running work before promotion. Old workers write checkpoints without
   `thread_files` and would discard references for an active thread during a mixed rollout.
4. Deploy a tagged API revision with no traffic, pairing the API image with the new worker
   digest in `MICA_SLACK_AGENT_IMAGE`. Preserve all existing environment and secret mappings.
   Upload caps default to 10 / 100 MB / 250 MB; merge overrides only if requested.
5. Check health and authenticated worker callbacks. With approval, test in a staging DM:
   screenshot question without compute; CSV → plot → larger points; a follow-up asking about
   the delivered figure without compute; a 6.5 MB PNG through a worker preview; unchanged
   `brief` + Markdown behavior; oversized-upload rejection; and a non-member denied before download.
   Run the opt-in large-figure live check and verify no SDK buffer overflow.
6. Promote only after reviewing staging results and obtaining approval. To roll back, restore the
   preceding compatible API/worker pair together. Old code ignores file registry fields and once
   again rejects ordinary file messages. Stored uploads still expire after 30 days.

An optional preview-only early release requires a separate M1-only source revision: build the
worker, update only the configured worker digest, and run the approved live preview check. Do not
mix the full attachment worker change with the old API as an early-release shortcut.

## Update briefs only when needed — 5 October 2026

Both direct chat and compute instructions now leave the project brief unchanged by default.
The agent updates it only when asked or when user-provided or verified information fills a
concrete missing detail needed for the current task. Routine results, file listings, inspected
schemas, and plot changes alone do not trigger edits. Unrelated TODOs do not trigger extra
inspection or questions. Replies omit brief commentary and save reminders when no edit occurred.
Initial setup still creates a draft, and only `save brief` publishes it. Existing threads receive
the revised instructions on their next request.

- Only `direct.py` and `prompts.py` changed from the preceding live application source.
  The direct routing tool also describes `draft` as null by default.
- Local verification: 404 passed, 3 skipped, 1 live test deselected. The Unix-socket test passed
  separately outside the local sandbox; Ruff, strict mypy, compileall, headless API checks,
  and diff checks passed.
- Build `56ab1f14-4fb0-4adb-a8cd-c299a3d3f2c4`: SUCCESS. All 83 installed application files
  were verified in the API, Slack worker, and shared science images. Build gates also checked
  worker compatibility, headless routes, socket paths, nested report delivery, and project renaming.
- Preview and live checks returned health 200, removed UI/docs 404, and unauthenticated worker
  credentials 401. Runtime settings were preserved apart from the three pinned image references.
  The new revision receives 100% of traffic. No error-level logs appeared during release checks.
- No paid model calls, user analyses, or Slack messages were used for verification. The existing
  test suite and image checks passed; actual model adherence to the new wording was not live-tested.

Deployed image digests (repository prefix
`us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/`):

| Image | Digest |
| --- | --- |
| `api` | `sha256:6fda54ea6df55c05a3b9c5368a32dc7f81fb9836a04a9f2ebe1fa2bdfe05a081` |
| `slack-agent` | `sha256:afcc8d5f33af94c9dc4b8cb01c1a0b803ee48ae1c0fc31d377e1e2b7d9c80c29` |
| `science` | `sha256:0972b8ae1e37a272b0ed54dae18b3412f034c47e83520b5805437bfb8f0a8041` |

Evidence and the source bundle: `.mica/deployments/brief-updates-20261005/`.
The prior compatible revision is `pistachio-staging-rename-20261005`; no storage schema changed.

## Rename existing projects — 5 October 2026

In the owner's DM, `rename <name or ID> <new-name>` changes only the project name. Names
must contain 3–40 lowercase letters, digits, or hyphens and be unique among that owner's
projects. IDs, data locations, briefs, outputs, access grants, channel bindings, and thread
contexts remain intact. The command is included in `help`; invalid requests receive usage
or permission errors without invoking a model or compute worker. Atomic registry writes and
durable operation receipts prevent duplicate names and unsafe Slack retry effects.

- Local verification: 404 passed, 3 skipped, 1 live test deselected; Ruff, strict mypy,
  compileall, and headless API checks passed.
- Build `0b2b4937-5532-4b06-9db6-b0efcd071c26`: SUCCESS. Only `commands.py`, `registry.py`,
  and `help_text.py` differ from revision 97's application source. Existing dependency
  images were retained; all 83 installed application files were verified in all three images.
- Build checks verified rename commands in the API and Slack images, worker contracts,
  headless routes, long workspace socket paths, and nested report delivery. The shared
  science image was checked for source and contract compatibility; it does not execute
  Slack commands and intentionally lacks their dependencies. An initial build attempt
  incorrectly ran the Slack-only smoke check there; this check placement was corrected
  before the successful build. No application changes were needed for that correction.
- Preview and live checks returned health 200, removed UI/docs 404, and unauthenticated
  worker credentials 401. Runtime configuration matched the preceding service except for
  the three pinned image references. The preview was checked before moving 100% of traffic.
- No data migration, actual project rename, paid model call, or Slack message was performed
  as part of release verification.

Deployed image digests (repository prefix
`us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/`):

| Image | Digest |
| --- | --- |
| `api` | `sha256:0cfdcf890474d96da5262cbdeb1a233069c9a0eee0209b061bda4ccdc6aaecee` |
| `slack-agent` | `sha256:aae1d2a862be5cfc004f659555e778e8479b8325c6796f328bf50e65817b6711` |
| `science` | `sha256:2963e57f7fb1a8f7b2c89e660bee016be6535988fce119f1aa0dd6280128a3b3` |

Evidence, source bundle, build results, and pre-release runtime configuration:
`.mica/deployments/project-rename-20261005/`. The prior compatible revision is
`pistachio-staging-00097-log`; this rename-only release did not change the storage schema.

## Metadata routing, conversational replies, and Slack reports — 5 October 2026

Ordinary chat can now request a bounded inventory of object names, folders, and sizes on
Cloud Run without starting a Spot worker. Reading file contents, calculating results, and
generating or resending reports continue to use the worker. Replies default to conversational
prose, with lists reserved for useful lists of files, results, changes, or condensed steps.

The agent is explicitly told that users cannot access its workspace or `outputs/`. Reports
must be attached to Slack. Nested files were already supported by the recursive collector;
the reported "top-level only" explanation did not match the deployed code. Instructions and
regression checks now cover `outputs/gap_report/cellpose_gap_report.pdf` directly. Existing
files can be resent by refreshing their modification times without moving or regenerating them.
Uploads precede the final answer. Failed uploads produce a delivery warning and allow the
workspace to be checkpointed so the next request can resend the same reports.

- Build `f800de8c-442b-41ee-89b1-52f7eec1feed` succeeded. Five application files changed from
  the prior release; all 83 application files were verified in the API, Slack worker, and
  shared worker images. Existing dependency images and unrelated runtime settings were preserved.
- Local suite: 394 passed, 4 skipped; Ruff and strict mypy passed. After adding the exact
  nested-file case, all 18 Slack tests passed. In-image checks covered nested PDF/PNG uploads,
  resends, upload failures, source synchronization, worker contracts, and private socket paths.
- Synthetic live canary `mica-delivery-canary-20261005` succeeded in 43.93 seconds of
  worker runtime. The live model chose inventory/reply for a metadata listing and compute for
  resending files; its casual response was prose. It generated a nested PDF and PNG, resent
  the unchanged files, reported a simulated upload failure, and preserved both through a GCS
  checkpoint round trip. Slack auth and required file scopes passed. All Slack writes were
  mocked; no synthetic messages or files were sent to users. Its temporary grant was deactivated.
- Preview and live health returned 200; removed UI/docs returned 404; unsigned Slack events
  and unauthenticated worker credential requests returned 401. Runtime fingerprints matched.
  The verified revision now receives all traffic, and the temporary preview tag was removed.

Evidence and immutable image digests: `.mica/deployments/slack-delivery-20261005/`.
Canary report:
`gs://pistachio-staging-artifacts-5304c19c/tasks/slack-analysis/acceptance/slack-delivery-20261005.json`.

## Working-directory recovery and build callbacks — 5 October 2026

A follow-up analysis entered the read-only data directory and persisted that cwd across
commands. Its next sandbox failed trying to create `.claude`, preventing even a recovery
`cd`. The runner now sets `CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR=1` for every SDK
invocation, including resumed sessions. Commands return to the writable workspace; the
prompt explains that directory changes apply only within a command.

The live log review also found ordinary deployment build notifications reaching the
project-wide tool-build callback without `_TOOL_ID`. These are now acknowledged as ignored
instead of attempting an invalid empty Firestore document lookup. Actual tool-build updates
and duplicate notification handling are covered by regression tests.

- Revision `pistachio-staging-00095-ted` uses build `d62a90d4-3c13-4b98-b139-9ea3d8d6149b`.
  All three images were checked against the same 83-file application source snapshot; their
  immutable digests and preserved runtime fingerprint are in the release manifest.
- Local verification: 362 tests passed, 4 skipped; Ruff, strict mypy, and diff checks passed.
  In-image checks covered source synchronization, worker contracts, headless endpoints,
  project controls, socket paths, cwd settings, and deployment-build callback handling.
- Real worker `mica-cwd-canary-20261005-v4` reproduced the persisted data-directory cwd
  with the old setting, resumed that same SDK session with the fix, and verified Python
  started in the writable workspace after a separate compound directory-change command.
  The synthetic fixture did not reproduce the exact metadata-creation error; the test
  directly verifies prevention of the directory drift observed in the failed user thread.
- Synthetic plotting, editing and rerunning the script after a GCS checkpoint round trip,
  SDK session identity, and all eight isolation probes passed. Both turns used a 129-byte
  private workspace. No sandbox permission was relaxed.
- Earlier diagnostic canaries and the final canary had their temporary grants deactivated.
  No user analysis or Slack message was replayed. The existing thread checkpoint was
  confirmed intact; the next message can continue with the corrected worker.

Evidence: `.mica/deployments/workspace-cwd-20261005/`. The final harness is saved as
`acceptance-tested.py` and embedded in the Batch job's commands. The report is at
`gs://pistachio-staging-artifacts-5304c19c/tasks/slack-analysis/acceptance/workspace-cwd-20261005-v4.json`.
The three earlier reports preserve the diagnostic differences between root-folder,
standalone-cd, and real-Python probes.

## Sandbox socket fix — 5 October 2026

A real private-DM worker started successfully but its Bash sandbox could not initialize bridge
sockets. The workspace path was 129 bytes; its previous `TMPDIR` was 134 bytes before the SDK
added a socket filename, exceeding Linux's Unix socket path limit. The earlier synthetic canary
used a shorter workspace and missed this regression. Restarting with the same path was insufficient.

Each SDK invocation now uses a unique mode-0700 `/tmp/pista-*` scratch directory, cleaned on
success, failure, or cancellation. Durable workspace paths, project bindings, and resumed SDK
sessions remain unchanged. Sandboxing, network denial, read-only data, and credential isolation
remain enforced; no additional container privileges or IAM grants were added.

- Build `ee3f6c58-a4a3-4feb-bfce-c64d16e4272c` succeeded using the exact prior image dependencies.
  All three images contain the same 83-file application snapshot; only `runner.py` changed.
- API: `us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/api@sha256:6f54dc3009090b5e5b17788286e328182de542b0e9d48b89d109e5c8c1921dbd`.
- Slack worker: `us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/slack-agent@sha256:2a26cb4076d3631dc43cd2475dce57f5b4160a08b86169b47e060d937ee37a6f`.
- Shared science worker: `us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/science@sha256:a0196cae8d00d9ba722794ed68431c8b3fac4ab03cc5e2394f7993bb3cac1287`.
- 358 tests passed, 4 skipped; Ruff and strict mypy passed. Real Unix socket creation and scratch
  cleanup are covered by regression tests and by the Linux worker-image build gate.
- Live canary `mica-socket-canary-20261005` succeeded before promotion, with a 129-byte
  private workspace path, synthetic plotting, GCS session restoration, and a successful resumed
  turn. All eight isolation probes passed. Worker runtime was 71.19 seconds; model cost was
  $0.236102 excluding cloud resources. The temporary credential grant was deactivated.
- No Slack test message or user analysis was replayed. The next request in the existing thread
  starts a fresh worker with the fixed image. No project reinitialization is required.

The acceptance harness now always uses a production-length private path. Evidence is in
`.mica/deployments/sandbox-sockets-20261005/`; the live report is at
`gs://pistachio-staging-artifacts-5304c19c/tasks/slack-analysis/acceptance/sandbox-sockets-20261005.json`.

## Full source sync and private-project cutover

The user authorized deployment of all recent changes, retaining personal agent DMs and optional
explicitly published channel conversations. The approved Slack permission update and reinstall
were completed in the Codex in-app browser. The existing secret-backed bot token reports all
12 expected scopes, including member email lookup, channel information, mentions, and channel
history. Events are `message.im`, `app_mention`, `message.channels`, and `message.groups`.
Existing webhook and interactivity URLs are unchanged.

- Build: `37a86b0f-a83a-4475-8e4f-a4deecd3a122` (SUCCESS).
- API: `us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/api@sha256:8d9cb9dd46677524e92d97d68103f153b08c5ac609b4786c4f581dc59177b9ae`.
- Slack worker: `us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/slack-agent@sha256:058887a3f2d2f579c20f073478a7cac94748955dc0a57c6a709d01ebf2d77314`.
- Shared science worker: `us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/science@sha256:c5ab805aab2314d69d1ce172a0badb4a5be59276f8e1e88a0bb50222dd255630`.
- `AGENT_ACCESS_MODE=workspace`: personal DMs require no access-channel membership. Projects
  and personal memory remain private. Project sharing and channel publication are explicit.
- `init` creates and selects a DM project and offers a switch-back command. `switch` lists
  accessible projects; `switch <name or ID>` selects the default for new threads. Existing
  threads retain their project. `help` displays the compact guide; `help all` is complete help.
- Validation: 355 tests passed, 4 skipped; Ruff and strict mypy passed. All 83 application source
  files match the same build snapshot in all three images. Headless startup, project controls,
  Slack imports/binaries, and the API/shared-worker contract passed.
- Preview and live health returned 200; removed UI/docs returned 404; unsigned Slack events and
  unauthenticated worker credential requests returned 401. Unrelated runtime configuration
  was fingerprinted and preserved. All old preview tags were removed at cutover.

The queue was paused and public ingress blocked across all 15 URLs before migration. No active
Batch jobs, queued tasks, or active Slack receipts remained. The existing project's owner was
verified against Slack, its registry entry and DM binding were migrated, and 13 metadata files
were backed up beneath `projects/_migration/full-sync-20261005/original/`. Original metadata,
checkpoints, and outputs remain intact; project retention is unchanged. Intake and queue dispatch
were restored after promotion.

Legacy private threads start a fresh SDK context on their next turn under the new private
workspace paths. Previous drafts/checkpoints remain available for reviewed recovery; unsaved
old drafts are not automatically published as shared project briefs.

Release evidence is in `.mica/deployments/full-sync-20261005/`, including source hashes,
digest pins, endpoint checks, migration checks, and backup object generations. No test Slack
message or upload is sent by the deployment checks.

Live synthetic acceptance job `mica-full-sync-canary-20261005` succeeded in 62.72 seconds
of worker runtime. It authenticated to the new ACL-enforced credential endpoint, verified Slack
scopes, generated a synthetic-data plot, round-tripped the session archive through GCS, and
resumed the same session to edit and rerun its script. All eight isolation probes passed:
readable data, no Slack credentials in generated code, blocked outside writes, blocked data
writes, blocked direct/proxied network and metadata access, and hidden parent credentials.
The model was `claude-opus-5-5`; the two turns cost $0.203556, excluding cloud resources.
The temporary credential receipt was marked complete/inactive. The report is at
`gs://pistachio-staging-artifacts-5304c19c/tasks/slack-analysis/acceptance/full-sync-20261005.json`.
The canary serialized the newer structured progress events as text in its test logger.
Actual Slack message/upload delivery remains a user-led check; no synthetic message was posted.

Rollback must retain private-project ACL enforcement. Use a verified compatible image pair or
pause ingress while repairing; never restore the historical slug-only API or worker.

## Historical rollout — 1 October 2026

The original rollout used `pistachio-staging-00073-ruj`; subsequent selective overlays reached
`pistachio-staging-00087-xey`. The following sections describe those historical releases.

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

Canary job: `mica-slack-canary-20261001-d` in `us-central1`, one n2-highmem-8 Spot instance.
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
were not added. In the existing Mica agent DM, send `help`, then
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


## Follow-up: casual voice and compact progress — 3 October 2026

Revision `pistachio-staging-00085-bac` serves 100% of staging traffic. Replies use concise,
conversational prompts. Status messages use grey context blocks with the current action and
recent steps, then collapse to a completion or error line. Run timers are removed; the slow
Spot startup minute count remains. The default loading emoji is `:hourglass_flowing_sand:`;
`AGENT_LOADING_EMOJI` also accepts a workspace custom emoji and is forwarded to workers.

The live service was still on baseline `87c4312`. This release applies only the eight voice
and progress source-file changes to that exact deployed source, rather than deploying later
local access-control, registry, command-routing, or memory work. The reproducible source,
backport script, diff, image references, and checks are saved under
`.mica/deployments/slack-voice-progress-20261004-002043/` (UTC release timestamp).

- API build `47baa119-7add-4053-86ef-ba190ae6d948`: SUCCESS, including web preservation checks.
- API image: `us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/api@sha256:2314dcdf817e5320e1147530df0fb99f8563a3b2e9fdfa3b74f2d830c96b6296`.
- Worker build `1f2c980a-2015-4623-aad3-b0b787ca763d`: SUCCESS.
- Worker image: `us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/slack-agent@sha256:af14efeeea754733405e3c4ff96271ef305990e7502672c6b2db9781651f4c91`.
- The scoped release passed 287 tests, with 4 skipped; Ruff, strict mypy (78 source files),
  and compileall passed. The main working-tree implementation passed 321 tests, with 4 skipped.
- Preview and live checks: health HTTP 200, existing sign-in redirect HTTP 307, unauthenticated
  worker credentials HTTP 401, and unsigned Slack events HTTP 401. No ERROR-level logs were
  returned for the new revision during preview checks.
- A runtime configuration fingerprint confirmed settings were preserved apart from the API
  image and pinned Slack worker image. No scopes, permissions, migrations, or data writes were
  added to the rollout. No Slack messages or compute jobs were submitted as deployment tests.

Desktop/mobile appearance, actual reply tone, and custom animated emoji remain user-led
Slack acceptance checks. Roll back by routing traffic to `pistachio-staging-00083-car`.


## Follow-up: expandable activity and loading circle — 5 October 2026

Revision `pistachio-staging-00087-xey` serves 100% of staging traffic. Analysis runs
now display Slack's native expandable `plan` block: a short activity heading and
the two most recent tool tasks. Tool-result events drive complete/error indicators,
including parallel calls; an interrupted or unconfirmed task is never labeled successful.
The completed task list remains expandable after the answer is delivered. Tasks use
short public tool descriptions, not raw commands, tool output, or model reasoning.

Startup and compact fallback statuses use the installed workspace emoji
`:mica-loading:`. Native plan cards supply their own circular progress indicator.
If Slack rejects plan blocks, the run falls back to the existing compact status.
Updates remain throttled and failed updates are retried by the heartbeat.

- Worker build `40480d1f-21f3-4713-b4d0-347b9e40e545`: SUCCESS.
- Worker image: `us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/slack-agent@sha256:38cae548e37393bde5931dc2b33708418d952a05876d55698a3b6f93b6166c9b`.
- API image remains the exact image used by revision 85. Only the pinned Slack worker
  image and `AGENT_LOADING_EMOJI` environment setting changed. Runtime fingerprint
  checks passed before and after promotion.
- The release backports five analysis-module files onto revision 85's source. It does
  not include concurrent local web removal, access, memory, or routing changes.
- Release tests: 292 passed, 4 skipped. Ruff, strict mypy (79 source files), and
  compileall passed. The local focused Slack/runner suite also passed (25 tests).
- Slack Block Kit Builder verified the collapsed summary and clickable two-task
  dropdown in desktop/mobile previews. No Slack messages were posted for testing.
- Preview and live: health HTTP 200, existing sign-in redirect HTTP 307, credentials
  without identity HTTP 401, unsigned Slack event HTTP 401. Preview error-log check
  returned no ERROR-level entries.

Release source, hashes, build details, and preview/live checks are saved under
`.mica/deployments/slack-expandable-progress-20261005/`. User-led acceptance:
start a new analysis in Slack and tap its activity heading. Already-running workers
keep their previous image. Roll back by routing traffic to `pistachio-staging-00085-bac`.
