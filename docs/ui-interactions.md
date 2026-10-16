# Workspace interaction refinements

> Historical record: the web UI and browser login have since been removed from source.
> Web rollout/preservation instructions below are superseded by the current
> [headless deployment guide](deployment.md#headless-api-rollout).

The message history scrolls independently above a bottom composer. Both the new-analysis
form and follow-up form use this dock; long inputs can scroll within its bounded height.
The sidebar can collapse and resize with a pointer or arrow keys, scrolls on short screens,
and becomes an overlay on mobile. Recent analysis names wrap instead of being truncated
to one line.

The workspace chooser supports searching names and handles, highlights the current
workspace, and offers project creation in its existing on-demand dialog. Buttons share
control sizing, focus states, colors, and transition timing. Reduced-motion preferences
disable animation and smooth scrolling. Existing conversation elements survive polling;
only new messages animate, and updates do not pull a reader away from older messages.

Download transcript exports the saved conversation as UTF-8 plain text with speaker
labels. It preserves the canonical message text also used by Slack. Historical messages
not present in the saved conversation are not reconstructed by the download.

Validation on 29 September 2026: 139 Python tests passed, 3 skipped; Ruff, mypy, and
JavaScript syntax checks passed. Local browser checks covered workspace filtering and
keyboard selection, sidebar keyboard resizing/collapse, mobile navigation, a 500-pixel
high window, composer bounds, and the downloaded transcript contents. No browser console
errors were observed. The changes use the existing server-rendered component styles and
require no new frontend runtime or CSS dependency.

## Staging deployment

Revision `pistachio-staging-00052-bip` serves 100% of traffic at
[Mica staging](https://pistachio-staging-h5a7b7akbq-uc.a.run.app).
Build `54718d11-cf47-45f3-ba1c-513f1ba6a825` succeeded. Preview and live health
and logo checks passed; authentication still redirects to login. No ERROR-level
logs were returned for the new revision during rollout verification. The runtime
configuration is identical except for the API image. The application source diff
from the previous deployment contains only `api/templates/index.html`.

No signed-in live analysis was submitted. Deployment details and immutable image/source
references are recorded in `.pistachio/deployments/ui-interactions-20260929.json`.
The previous revision uses the same data model and can be restored with:

```bash
gcloud run services update-traffic pistachio-staging --project=azizilab-aml \
  --region=us-central1 --to-revisions=pistachio-staging-00050-xay=100
```
