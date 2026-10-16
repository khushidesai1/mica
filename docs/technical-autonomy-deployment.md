# Technical autonomy deployment — 29 September 2026

[Mica staging](https://pistachio-staging-h5a7b7akbq-uc.a.run.app) serves revision
`pistachio-staging-00048-zek` at 100% traffic.

The developer handles routine installed-package/API choices and technical repairs internally.
Scientific amendments require a concrete statement of scientific impact. The loop now permits
40 model turns and 20 edits, reuses consecutive successful probes, and labels observations with
the code revision they tested. Compute, operation and runtime limits remain bounded.

The recovery scheduler uses the normal planning entrypoint and a durable process lease. A
partially rebuilt plan is a nonfatal scheduling conflict, and old completion events cannot
resume an amended session. Follow-ups retain equivalent approved compute resources and do not
count repeated dataset URLs twice.

- Build: `fc7c0248-8458-48cb-a1d9-83aa82c5248c` (SUCCESS).
- API: `us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/api@sha256:32773d0cf352fb2ca1bf633b8a545f57297a8fdf6d57c053afd6180ee8b5be63`.
- Rollback: `pistachio-staging-00046-fig`.
- Worker image, runtime environment and security settings preserved.
- Isolated deployment source starts from the prior deployed build, with the agent fixes
  applied. The separately prepared UI and conversation redesigns are not included.

Validation: 139 shared-workspace tests passed with 3 skips; lint and types passed. The isolated
rollout source passed 61 relevant tests and lint. Preview and live health returned HTTP 200,
and live API schema inspection confirmed the new code. The first live schema check ran while
traffic promotion was pending and still saw the prior revision; it passed after routing completed.
No ERROR-level logs were returned for the new revision during the deployment check.

Planner-v2 package builds are automatic rather than user approvals, but staging does not yet
have a configured package-build trigger. This remains a technical infrastructure limitation.
No full user analysis was rerun, and the current task's pending compute approval was not changed.
