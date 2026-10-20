# Initial implementation quality

The developer must resolve data representation, library/API compatibility and full-data
feasibility before writing its first complete program. Execution remains a correctness gate;
it should not be the first place obvious integration assumptions are checked.

## Evidence behind the redesign

The recent neighborhood task recorded four source revisions. Its first worker checks exposed
DataFrame coordinates being indexed as ndarray rows and the display label `Euclidean` being
passed to an API requiring `euclidean`. The runtime probe previously listed `obsm` keys but
did not describe the stored representations or API signatures. Candidate review happened
after those worker runs and returned to the feasibility of exact Silhouette evaluation for
2,471,637 observations, approximately 6.1 trillion pair distances per resolution. Blocking
that computation reduces working memory but does not remove the total quadratic work.

These are preparation failures. Increasing the repair allowance addresses recovery, not
the quality of the initial implementation.

## Enforced workflow

1. **Probe the actual runtime and inputs.** Read HDF5 structure without materializing expression
   or coordinate matrices. Report DataFrame versus array representations, positional indexing,
   shapes, dtypes and installed API signatures/documentation. Missing APIs/backends appear as
   evidence, not guessed capabilities. Keep the successful probe in subsequent model context.
2. **Review an implementation plan.** Before source edits, require a successful probe from the
   pinned runtime, every effective parameter's consumer/API adaptation, input contracts,
   standard library calls, full-scale work/memory estimates, modules, independent tests and
   exact output specs. Reject missing bindings, unsupported memory estimates, unresolved
   blockers and unrequested outputs before invoking review. Internal design review addresses
   all material issues together. Genuine changes to scientific meaning still require consent.
3. **Invest in the first draft.** Use high reasoning with a 300-second ceiling for preparation
   and initial authoring, independently of the general worker's reasoning/time settings.
   Require all planned modules and production tests in the first source revision and compile
   that draft before accepting it. The deadline is a ceiling, not a desired response time.
4. **Review before execution.** Code cannot launch test, verification or full-run operations
   until the current revision passes review against its plan. An unchanged reviewed revision
   reuses its review when submitted, then still undergoes clean verification. Edits invalidate
   review and verification. No runtime test or scientific invariant is removed.
5. **Use repairs as a fallback.** Retain bounded technical repair with concrete diagnostics.
   Review all known issues together, including adjacent API/data boundaries. Do not lower the
   revision limit merely to hide failed first attempts or turn a technical problem into a
   user decision.

The reviewed preparation is bound to scientific authority, runtime image and plan content.
Amendments invalidate it. Persisted plan/review references survive control-plane restarts.

## Rollout and measurement

New development sessions record `preparation_version=1`. Existing records deserialize with
version 0 and keep their workflow. The operator can retain explicitly pinned previous workers
in `MICA_BATCH_COMPATIBLE_IMAGES`; execution uses each session's own admitted image.
Unknown worker images are rejected. This supports draining existing work without changing its
runtime or invalidating its verification hashes.

`first_candidate_source_hash` and `first_check_passed` record the first draft's execution outcome;
review records carry their phase and source hash. A null first-check outcome means the initial
draft was never executed, not that it passed. Count rejected first-draft reviews separately
from runtime defects and infrastructure retries. Compare first-draft acceptance, first-check
success, revisions to validated completion, end-to-end time, model tokens and compute cost.
Count all retained `edit_files` requests as well as accepted source revisions, so rejected
incomplete drafts do not disappear from the quality measurement.

The local tests establish gate enforcement, complete-draft execution, review reuse, restart
durability, runtime invalidation, actual HDF5 representations and model effort selection.
They do not demonstrate an empirical increase in model first-pass accuracy. Evaluate that on
matched held-out tasks under the same scientific contracts, with independent output checks;
do not count an unvalidated or weakened analysis as a successful first attempt.

Deployment should use paired API/worker builds with execution-schema compatibility checks,
then Batch conformance including the enriched probe, Louvain/Silhouette execution and artifact
validation before traffic promotion.

## Deployment

Staging revision `pistachio-staging-00060-poh` serves 100% of traffic. The paired build
`000d80fd-4146-40ac-922e-886db800dd93` passed execution-schema compatibility. Local validation
passed 170 tests with three skips, plus lint/types; isolated rollout validation passed 50 tests.
Batch job `mica-initial-quality-smoke-20260930` passed the enriched probe, clean verification,
Louvain/Silhouette execution, output validation and sandbox conformance. Preview and live health
and schema checks passed. No existing user task record was reset or amended for this rollout.

Exact images and rollout evidence are recorded in
`.mica/deployments/initial-quality-20260930.json`. Empirical model first-pass improvement
has not yet been measured on new live analyses.
