# UI regression fixes — 29 September 2026

> Historical record: the web UI and browser login have since been removed from source.
> Web rollout/preservation instructions below are superseded by the current
> [headless deployment guide](deployment.md#headless-api-rollout).

Reproduced and corrected in the workspace template:

- The recent-analysis list extended outside a native details element and was clipped.
  It now uses an accessible disclosure button with an explicitly constrained scrolling
  body. A 20-item fixture confirms the last analysis is reachable. Grid rows retain
  their content height so wrapped titles and status labels do not overlap.
- The main grid's implicit column could exceed the available width at tablet sizes,
  clipping controls. Its column now permits shrinking, and the header can wrap.
- Follow-up drafts leaked between analyses. They are now retained separately by task ID.
- Five-second sidebar refreshes removed the focused button. Existing rows are reused
  while their text and state are refreshed.
- Approval fields with a preselected custom-entry option initially hid their required
  input. Custom-entry visibility now initializes immediately; labels are associated
  with their controls.
- Load failures appeared in hidden forms. A workspace-level status region now shows
  failures and reconnect status, and clears after recovery. Actions are disabled while
  another analysis loads; stale context/cancel errors do not overwrite a newer view.

Validation used a read-only local fixture with 20 conversations and a custom-entry
approval. Checked desktop, 900×580, 700×500, and 390×400 layouts; mobile drawer and
disclosure actions; sidebar scrolling; focus preservation after refresh; draft isolation
and restoration; required-field validation; missing-task errors; and JavaScript console.
No horizontal overflow remained at the tested widths. JavaScript syntax checks passed.

The workspace Python suite passed (150 tests, 3 skipped), and mypy passed. Two lint
violations were observed in unrelated in-progress backend files. The deployment source
was therefore based on the previous deployed snapshot with only this template updated;
its lint check and all 8 API tests passed. No unrelated backend changes were deployed.

## Deployment

Revision `pistachio-staging-00054-get` serves 100% of staging traffic. Build
`3fcdc07a-6094-4f55-807c-c3a5f2091e7c` succeeded. Both preview and normal staging
returned health 200, logo 200, the expected API schema, and the protected home-page
redirect. The runtime configuration was preserved. No authenticated live analysis
was submitted. The previous revision is `pistachio-staging-00052-bip`.

Rollout logging exposed a separate existing backend issue: the internal tool-build
completion handler accepts non-tool Cloud Build events, then tries to look up an empty
tool ID and returns 500. This was observed for the deployment build; it is outside the
UI changes and has not been changed here. The UI health and authentication checks passed.
Immutable deployment references are in
`.mica/deployments/ui-regression-fixes-20260929.json`.
