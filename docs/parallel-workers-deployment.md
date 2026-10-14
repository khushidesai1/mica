# Parallel large-worker deployment

On 14 October 2026, staging revision `pistachio-staging-parallel-94f68d555b8a`
was promoted to 100% traffic from source `94f68d555b8a932d554eee997248824df2471b27`.

Large and xlarge requests in separate conversations now have independent submission leases,
with no application-level cap on their combined active count. Standard jobs retain their
configured concurrency limit. Turns in one conversation remain ordered; duplicate delivery,
per-request budgets/deadlines, access checks and GCP quota still apply. The obsolete
`AGENT_MAX_CONCURRENT_LARGE_RUNS` environment variable was removed.

This controller-only release uses `cloudbuild/build-slack-api.yaml` and preserves both deployed
worker images, existing approvals and the publication protocol. Already-running workers keep
their original leases and release them normally. No jobs were cancelled or restarted by this
rollout. No IAM, quota, secret or Terraform changes were made for this release.

Validation:

- Ruff and mypy passed; pytest: 791 passed, 6 skipped.
- Regressions exercise 24 independent requests for each heavy tier, legacy lease completion,
  duplicate delivery, cancellation, same-conversation ordering and the standard-worker limit.
- [GitHub CI](https://github.com/khushidesai1/pistachio/actions/runs/38498710062) passed.
- Cloud Build `4191f7ca-5db2-4ed8-b7c0-08af7940ac32` passed all four gates, including the
  execution-contract check against the unchanged science worker.
- Preview and live checks passed: health 200, removed UI/docs 404, unsigned Slack and
  unauthenticated worker endpoints 401. Configuration comparison confirmed only the API
  image and obsolete concurrency setting changed from the previously live revision.
- Task dispatch resumed after promotion. TMA16 remained on its existing worker. TMA17 had
  already been cancelled in Slack before rollout and was not restarted. No synthetic jobs
  or data writes were submitted for this controller-only check.

Rollback: route traffic to `pistachio-staging-overwrite-52033ddec667`; this restores the old
shared heavy-worker cap for new submissions. Existing jobs retain their recorded lease keys.
