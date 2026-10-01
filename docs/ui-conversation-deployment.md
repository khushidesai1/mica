# UI and shared conversation deployment — 29 September 2026

Deployed to [Pistachio staging](https://pistachio-staging-h5a7b7akbq-uc.a.run.app).
Revision `pistachio-staging-00050-xay` serves 100% of traffic.

The release includes the supplied SVG logo, softer workspace styling, project sidebar,
on-demand project creation, recent-analysis search, memory panel, responsive navigation,
and the shared UI/Slack conversation transcript with centrally selected progress updates.
The current technical-autonomy fixes remain included. Worker image, environment,
authentication configuration, service account and resource settings are unchanged.

- Build: `2b4f2cfe-f0c8-41d8-a81b-b78a93a070d6` (SUCCESS).
- API: `us-central1-docker.pkg.dev/azizilab-aml/pistachio-staging/api@sha256:e0c25d01924bed7b7c36ad24789d341b6f86f8dc6a3e21a6e64aeea6904a059b`.
- Previous revision: `pistachio-staging-00048-zek`.
- Source: `gs://azizilab-aml_cloudbuild/source/1790659690.221993-e39137cb90804a769bdd20b6e718d756.tgz`.

Validation: 139 tests passed with 3 skips; lint and type checks passed. Container checks
confirmed the installed template, logo, and conversation model. Preview and normal staging
URLs returned healthy responses, the served logo matched the supplied SVG byte-for-byte,
and the live API schema exposed the conversation transcript. The home page retained its
expected authentication redirect. No ERROR-level logs were returned for the new revision
at rollout verification. Desktop/mobile interactions were checked locally in the preceding
UI work; no authenticated live user analysis or Slack message was created for this rollout.

Start a new analysis for a complete saved conversation. Historical conversation messages
are not reconstructed for older tasks; see [channel behavior](channels.md).

Rollback command:

```bash
gcloud run services update-traffic pistachio-staging --project=azizilab-aml \
  --region=us-central1 --to-revisions=pistachio-staging-00048-zek=100
```

The prior model schema predates the conversation field. Before rolling back while new task
records exist, verify schema compatibility or deploy a compatible forward fix; a traffic-only
rollback is not guaranteed to preserve access to records containing new fields.
