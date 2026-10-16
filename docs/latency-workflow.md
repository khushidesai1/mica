# Latency workflow (preparation version 2)

The September 30 trace of `task_fb173233a83b493399483ca05df20199` spent 74.8 minutes
before the second full run started: 23.7 minutes in 36 developer/reviewer calls, 19.2 minutes
starting eight Batch workers, 28.2 minutes in seven completed jobs, and 3.7 minutes in initial
planning and orchestration. It produced 12 source revisions. This is one trace, not a percentile.

## Workflow

New sessions opt in with `MICA_DEVELOPMENT_LATENCY_ENABLED=true`. The persisted version,
acceptance checklist and worker reuse choice survive restarts; existing versions 0/1 retain
their original transitions. The paired API and worker must pass `check_worker_compatibility.py`.
Old pinned workers receive their original execution wire format; the operator must keep their
digests in `MICA_BATCH_COMPATIBLE_IMAGES` until old sessions drain.

A probe supplies structured input representations, installed signatures and sandbox capabilities.
This evidence has its own field, so truncating logs cannot erase it. One high-effort author call
returns a complete implementation plan, source, tests and output specification. Deterministic
checks validate authority bindings, output coverage, resource feasibility and complete source.
The orchestrator then submits clean validation and starts independent review concurrently.
One feedback record combines their findings. Successful unchanged candidates advance directly
into execution: no model call to ask for tests/submission, no separate verification job, and no
repeat unit suite at full-run startup. The validation report binds source, input checksum,
configuration, runtime and trusted suite hashes. Full runs still validate these identities and
all delivered artifacts. Review remains mandatory; failed checks never become approval.

`task_runtime` provides positional input access, typed parameters, configured output paths,
resource checks that tolerate missing host telemetry, and progress reporting. All source and
input mounts remain read-only and each operation receives fresh scratch/output directories.
Author and reviewer receive actual helper signatures and the configured worker allocation.
The first review uses high reasoning effort. Repair reviews receive the previous findings and
changed paths against the same checklist; speculative extra coverage is not a blocking defect.

## Worker lifetime and recovery

`MICA_WORKER_SESSIONS_ENABLED=true` selects one supervisor per task/runtime. The control
plane writes a generation-conditioned command manifest. Epoch and sequence checks reject old
commands; cancellation is sticky within an epoch. Workers poll every two seconds, emit active
heartbeats every 30 seconds, cancel superseded operations, and exit after ten idle minutes or
the task budget deadline. Each operation also has its execution/publication deadline.

The cache is task-local and keyed by pinned input identity and runtime. Initial staging validates
remote generation/checksum and computes SHA-256. Subsequent operations reuse read-only files;
metadata changes trigger a checksum check and corruption fails closed. A fresh supervisor cannot
inherit trusted in-memory cache records. Probe reuse also binds the trusted validation suite.

Wall time includes startup and idle time in budget accounting. Session recovery conservatively
charges elapsed time since the prior session began; it may overestimate downtime, never omit it.
Durable operation publication remains authoritative even when its parent Batch job stays running.
Reconciliation recovers missed completions for RUNNING as well as QUEUED tasks. Model-call leases
on the cloud path expire after the model-call allowance plus recovery margin, rather than the
full scientific run allowance. Full scientific execution is excluded from the development wall
clock. Terminal states stop the supervisor; rollback disables new sessions while old ones drain.

## Timing and estimates

Operation results expose staging, hashing, runtime probe, tests, smoke, analysis and artifact
validation durations and phase timestamps. Durable publication commits add publication timing.
Source requests/reviews retain model duration and token counts. Structured probe evidence and
failure excerpts replace repeated package inventories and successful logs in model context.
Trusted lifecycle events change queued tasks to running. Progress survives concurrent state saves
but clears on a user amendment. Phase ETAs use observed item throughput with a 0.5–2x range; they
explicitly exclude later unmeasured phases. No whole-analysis estimate is fabricated.

First-candidate outcomes distinguish passing, test failure, review rejection and replacement
before validation. A superseded untested first draft is not reported as a successful/null check.

## Release checks

- Run pytest, Ruff and mypy; keep old workflow tests enabled.
- Run the paired build in `cloudbuild/build-latency.yaml` and the scientific namespace conformance
  module `mica.execution.smoke` on the candidate worker.
- Run `scripts/run_latency_canary.py` against the retained authorized spatial task. It creates an
  isolated in-memory task and distinct artifact prefix, uses the real input and model, and stops
  after validated preparation. It never submits full analysis or edits the original task.
- Record revision/model counts, elapsed preparation, operation timings and validation evidence.
  `scripts/summarize_latency_canary.py` reads checksum-verified immutable evidence and creates a
  compact report, preserving failed first candidates and distinguishing reserved cost from billing.
  `scripts/benchmark_small_latency.py` compares the old and new validation paths on aggregation,
  Louvain and Silhouette fixtures inside the pinned sandbox, checking identical output checksums.
  That controlled comparison excludes model calls and separate Batch worker startups.
  The historical baseline includes a failed full run; report this distinction when comparing.
- Targets are acknowledgement under 2 seconds, routine planning under 2 minutes, and at least
  50% less preparation time on the measured workload. Synthetic checks do not establish model
  accuracy, percentile latency, full-population result equivalence or every target.
- Promote only the matching tested images. Exact deployment and benchmark evidence belongs in
  `.pistachio/deployments/` and `.pistachio/benchmarks/`.

## September 30 measured preparation canary

The retained 16.3 GB spatial input reached independently reviewed, passing bounded validation in
787.4 seconds (13.1 minutes). The canary used one Batch worker, three source versions and six
model calls. The first candidate failed: its test config omitted resource metadata and review
found missing production-path coverage. The second candidate passed review but its installed-API
test used an exclusive sparse-entry bound where equality was valid. The third passed both gates.
All failures remain in the benchmark evidence; this is not a first-pass-success claim.

Cold queue/startup took 149.0 seconds, input download/staging 109.6 seconds and hashing 43.3
seconds. Each later validation reused the input in 0.17–0.20 milliseconds, with zero downloads;
dispatch to the existing worker took 2.4–4.1 seconds. Total author/reviewer time was 375.5 seconds.
Reported prompt-cache hits remained zero. The final tests took 47.4 seconds and real-input smoke
took 29.9 seconds, including production initialization, the scalable graph backend, scoring and
all three requested PNG outputs on a bounded population.

The historical trace used 12 source versions, 36 model calls and eight workers over 74.8 minutes.
Its cutoff was the second full-run start and includes a failed full analysis, while this canary
stops before full analysis. The observed preparation reduction exceeds the 50% numerical target,
but does not prove an equivalent end-to-end scientific speedup. Full-population equivalence,
actual billed cost and acknowledgement/planning latency percentiles remain unmeasured. Keep
broad rollout gated until those acceptance criteria have evidence.

The final release additionally supplies actual resource allocation/config contracts to the
models, records model wall timestamps, and retries manifest generation races without discarding
the reusable worker. These are covered by deterministic regressions and the compatibility gate;
the spatial canary above predates these final additions.

The final worker's controlled small-fixture comparison took 27.8 seconds on the legacy path
and 21.6 seconds on the new path (22% less), with identical delivered JSON/PNG checksums. This
excludes model calls and Batch startup; the new path also performs a more extensive initial API
probe. The same cloud job passed all sandbox, shared-helper and unchanged-validation conformance
checks. Local validation passed 191 tests with three environment-dependent skips; Ruff and mypy
passed. The matching release is deployed to the zero-traffic `latency-preview` revision only;
normal traffic and active scientific tasks remain on the existing rollout.
