# Follow-up replanning deployment — 29 September 2026

Deployed to [Pistachio staging](https://pistachio-staging-h5a7b7akbq-uc.a.run.app).
Revision `pistachio-staging-00046-fig` receives 100% of normal staging traffic.

Follow-ups complete replanning and approvals before the retained development workspace
resumes. The planner supplies a reason for its chosen response to an amendment, and the
Conversation Agent translates it into a saved explanation visible alongside approvals
and progress in the web UI and channel updates.

- Cloud Build: `ac119219-5ac8-4411-a256-03fe620fe7ff` (SUCCESS).
- API image: `us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/api@sha256:e23e8c671da7519bc50f2e1918378348e7969dc56becd6af99fb8c54f13be5c9`.
- Rollback revision: `pistachio-staging-00044-soc`.
- Worker image and runtime configuration are unchanged. These fixes concern API-side
  orchestration, planning, conversation presentation, and the web template; the worker
  execution bundle is unchanged.

Validation: 120 tests passed, 3 skipped; Ruff and Mypy passed. Preview and normal staging
health endpoints returned HTTP 200, and both exposed the new task and master-plan
explanation fields in the API schema. No ERROR-level logs were returned for the new
revision during the rollout check. No real user analysis was rerun; live-model quality
and an end-to-end analysis remain unverified by these deployment checks.

The prior failed task was not changed. New requests and eligible follow-ups use the fixes.
