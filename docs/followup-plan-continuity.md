# Follow-up plan continuity investigation — 30 September 2026

Status: deployed to [Mica staging](https://pistachio-staging-h5a7b7akbq-uc.a.run.app).
The initial correction was released as `pistachio-staging-00066-liv`. The subsequent
[communication repair](communication-amendments.md) is now live as `pistachio-staging-00068-nih`.
The initial investigation below describes the first correction, including the literal evidence
requirement replaced by the second release. No live task records were manually edited, and no
user analysis was rerun.

The assistant proposed a reproducible uniform sample of 5,000 cells for Silhouette scoring while
retaining all cells for clustering. The user replied “That’s fine.” The following responses first
claimed there would be no sampling, then described a 10,000-cell scoring sample. Neither response
faithfully represented the proposal the user accepted.

## Confirmed code paths

- `TaskService.add_context` treated every reply as a fresh planning request, clearing the master
  plan, decision set, contract, question/error, and scientific approvals. Protocol 2 appended a
  prose summary of some confirmed values to the prompt, but omitted the developer's pending
  proposal and the full existing plan.
- Intake reconstructed a structured request from accumulated user text without the assistant
  question. The planner-v2 context likewise omitted the current task's conversation and old plan;
  its recent exchanges were summaries of other tasks. The master planner runs without the legacy
  query agent's conversation session, so an SDK memory could not fill this gap.
- Every follow-up generated a complete replacement master plan. There was no deterministic
  carry-forward of untouched components or check that an accepted numeric proposal retained its
  exact value.
- Replan presentation received only the latest user reply and planner prose, without the pending
  question or effective settings.

These paths explain how the illustrated drift can occur. A live model trace was not inspected,
so the exact internal sequence in that particular run is not asserted.

## Changes

`PlanHandoff` preserves the old authority and exact exchange before derived execution state is
invalidated. It survives serialization, service restarts, consecutive follow-ups, and advisory
memory pruning. Follow-ups bypass the isolated intake rewrite and reach the planner with that
protected context. User text is no longer mixed with an automatically appended authorization
summary.

Planner-v2 uses a sparse `MasterPlanAmendment`. The merge materializes effective approval values,
retains every omitted component, and increments the plan revision. Each changed parameter has
verbatim evidence identifying a user request, accepted proposal, or new proposal. Numeric edits
bound to consent must match their evidence, including equivalent notation such as 5k and 5,000.
A mismatch is repaired once before any plan or explanation is published. Unresolved scientific
proposals remain subject to approval. Ambiguous replies pause without replacing the baseline or
resuming execution; repeated dispatches do not repeat the clarification.

The conversation agent receives the pending question and structured effective scientific settings
alongside the amendment explanation. Development still waits for the resulting plan and approvals
before its saved workspace is rebound to the new revision.

## Validation and limits

Validation: 206 tests passed, 3 skipped; Ruff and Mypy passed. The suite emitted one existing
Starlette/AnyIO deprecation warning.

Regression coverage includes the screenshot's 5,000-cell proposal and short consent, attempted
10,000-cell drift, preservation of an unrelated user-edited setting, an explicitly requested later
change to 10,000, serialized restart, both builder protocols, consecutive replies, context-budget
pruning, ambiguous replies, and the presentation payload. The incident regression mocks model
output and verifies rejection/repair before publication; it does not claim live-model reliability.

Semantic interpretation and sparse edit selection still use a model. Numeric evidence checks
and preservation of omitted components provide deterministic constraints, not a general proof
that every textual amendment is semantically correct. A live end-to-end conversation remains
unverified. Existing tasks that already lost their old plan are not repaired retroactively.

## Deployment

- Cloud Build: `530f39fc-7f55-4e9d-8924-b636fc97b9ab` (SUCCESS).
- API: `us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/api@sha256:c25d5444063bcd68da4a12e456e974505e441badd5c9847cd8da3df0eb7a9247`.
- Previous live revision: `pistachio-staging-00060-poh`.
- The isolated source starts from that live revision's verified build archive and applies only
  the six source-file changes for plan continuity. The separate latency preview was not promoted.
- The isolated release passed 51 targeted tests, Ruff, and Mypy. Cloud Build verified the packaged
  handoff schema and exact execution-contract compatibility with the existing scientific worker.
- Preview and normal staging health/schema checks passed. The resolved revision's environment,
  worker image, and resources match the previous live revision. No ERROR-level log entries were
  returned for the new revision during the rollout check.

The deployment record and isolated diff are saved under
`.pistachio/deployments/plan-continuity-20260930.*`. The previous revision does not understand the
new handoff fields; once tasks persist those fields, use a compatible forward fix rather than
blindly routing back to the older schema.
