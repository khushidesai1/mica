# Worker schema mismatch recovery — 29 September 2026

The latest three neighborhood tasks failed in their first protocol-2 input probe. The API
on revision `pistachio-staging-00056-wuw` serialized `decision_scope` in execution parameters,
but the worker remained on science image `sha256:cd2fe1d03a7c9ccc920e0bca5c74008a28ae771b6855e4415efc05aa7b9e64f0`.
That older worker rejected the fields with Pydantic `extra_forbidden` during `download_bundle`,
before staging inputs or running generated code. Reconciliation reported the startup exception as
an infrastructure fault and retried the same incompatible bundle twice.

The “code was retained” terminal message is generic: these particular tasks had an empty source
workspace and had not reached code generation. They produced no validated scientific results.

The replacement worker copies the application package from the exact deployed API image while
preserving the science image's Python dependencies, operating system and sandbox configuration.
The new build gate exports the API execution schema plus a bundle containing implementation,
method-default and legacy research-design settings. The candidate worker must deserialize it
losslessly and report an identical execution schema before publishing.

Build `02fdeaf3-f735-4ca2-954f-f64e41ef82e7` passed the compatibility gate and produced
science image `sha256:4949f21b664cccbcfc624fdd663014d9f40fbe1de37560ca330a5030855fcd3a`.
The first build stopped at a workspace output-permission issue in the new check; the check now
exports via stdout, retaining unprivileged application containers and read-only source mounts.

The bounded Batch conformance job first deserializes the actual failed bundle, then runs synthetic
verification and analysis with Louvain, Silhouette scoring, artifact validation and isolation checks.
This validates the repaired infrastructure; it does not establish scientific correctness on the
user's dataset. The cloud job succeeded with `FAILED_BUNDLE_SCHEMA_VALIDATED` and
`PROTOCOL2_WORKER_CONFORMANCE_PASSED`.

Staging now serves revision `pistachio-staging-00058-xoy` at 100% traffic. Preview/live health
checks passed, and `PISTACHIO_BATCH_IMAGE` was the only runtime setting changed. The API image,
scientific libraries, authentication and sandbox configuration were preserved.

The latest failed task, `task_11acc4455f0c4a6d8ba9025817347e49`, was recovered by appending a new
probe operation under the tested runtime. A conditional Firestore update retained its exact
scientific authority, prior operations, result references and spent budget. The new reservation
totals $0.8333 within the existing $31.875 approved envelope. No older duplicate task was restarted.
The task was dispatched through the normal authenticated Cloud Tasks queue. Deployment and
recovery evidence is recorded in `.pistachio/deployments/worker-sync-20260929.json`.

The real-data recovery probe succeeded in 188 seconds, passed runtime/input-schema validation,
and durably published its result with exit code 0. The task advanced into normal model-driven
development/planning. Full composition vectors, clustering outputs and resolution selection
are not yet validated; the completed checks establish that the recurring startup failure is fixed.
