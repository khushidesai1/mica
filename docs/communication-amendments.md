# Follow-up communication repair — 30 September 2026

Status: deployed to [Mica staging](https://pistachio-staging-h5a7b7akbq-uc.a.run.app).
Revision `pistachio-staging-00068-nih` serves 100% of staging traffic.

## Observed incident

The live task `task_b1527ea2792b41cd9d3fd84699ab64c2` failed after accepting sampled Silhouette
scoring and specifying 10,000 cells, uniform sampling, and permission for the agent to choose a
seed. A read-only inspection found both replies in the durable handoff, along with the original
proposal and later clarification. This incident did not lose the replies. It failed the newly
introduced requirement that model-authored evidence quote the exchange literally:
`param_silhouette_sample_size: amendment evidence must quote the actual exchange`.

The old amendment schema also lacked a basis for delegated or automatic choices. A chosen seed
could fail numeric evidence checks because the user had intentionally not supplied a number.
The failure then reached the conversation layer as a request for the user to fix internal evidence.

## Repair

- Amendments cite saved message IDs. The service resolves provenance from the saved text; models
  do not copy quotes. Accepted proposals reference both the proposal and acceptance, including
  older proposals still retained after a clarification. Legacy quoted outputs remain readable.
- Explicit choices, accepted proposals, delegated settings, routine defaults, and new research
  proposals have distinct bases. Delegated seeds are reproducible agent choices, not falsely
  reported as user-specified. Routine choices cannot overwrite an approved value.
- Intake and amendment instructions normalize ordinary prose, fragments, lists, key/value text,
  JSON, and number words internally. Numeric comparison supports common equivalent notation.
  Necessary proposals should be complete enough to accept in one reply; accepting a method does
  not trigger a questionnaire for every routine implementation detail.
- Bounded internal repair receives the rejected output and valid saved references. Evidence or
  schema repair questions are not routed to the user. If repair still fails, the task fails honestly
  with a short explanation; the detailed diagnostic remains in its stage audit.
- The conversation agent receives recent dialogue and effective settings and produces concise,
  grounded confirmations. Backend evidence errors are sanitized before both model and fallback
  presentation, including records written by the previous release. User input itself is preserved.

Omitted plan components still survive unchanged. Numeric drift from approved 5,000 to 10,000 is
still rejected. Semantic interpretation of delegation and non-numeric changes still relies on the
model; references establish provenance, not a general proof of semantic correctness.

## Validation

- Full workspace suite: 218 passed, 3 skipped; Ruff and Mypy passed. One existing Starlette/AnyIO
  deprecation warning remains.
- Isolated release against the exact live build source: 83 relevant tests passed, Ruff and Mypy
  passed. Only six source files differ from revision `pistachio-staging-00066-liv`.
- Three synthetic evaluations used the deployed planner model, `gpt-5.6-sol`, through the real
  amendment runner and conversation renderer. All succeeded on the first amendment attempt:
  exact assent preserved 5,000; number-word input selected 10,000 uniformly sampled cells with a
  delegated seed; general assent to sampling selected routine reproducible defaults without asking
  for another decision. These fixtures contained no real task/project records or datasets.
- Model replies were concise and contained no extra clarification requests or internal evidence
  language. Results are saved in `.pistachio/deployments/communication-model-eval-20260930.json`.
- A local, deterministic check loaded the incident's saved handoff and successfully bound a
  10,000-cell amendment to its actual reply ID. This check made no model request or database write.

This is targeted model evaluation, not a full live scientific run. Existing failed tasks are not
restarted by deploying the release, and their stored records were not manually edited.

## Deployment

- Cloud Build `7d454b96-48ed-4e47-a17a-672fda6f8caf` succeeded, including packaged schema checks
  and exact execution-contract compatibility with the current scientific worker.
- API image: `us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/api@sha256:e242cb4a73a8fac470d9b4cc836290147baf5496eaf8646b313705d6ac1c0f50`.
- Preview and normal staging health/schema checks passed. Runtime environment, worker image,
  resource limits, and service configuration match the prior live revision. No ERROR-level logs
  were returned for the new revision during the rollout check.
- Deployment metadata and isolated source diff: `.pistachio/deployments/communication-20260930.*`.
  The separate latency preview was not promoted, and the scientific worker was not rebuilt.

New parsed request records may contain `delegated_choices`. Prefer a compatible forward fix over
routing back to a strict schema that cannot deserialize those records.
