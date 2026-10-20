# Optional script attachments

Script return defaults to **off**. `scripts on` opts a Slack user in, `scripts off` opts them
out, and `scripts` reports their effective setting and whether it comes from their preference
or the deployment default. Commands work in DMs and channels and apply only to their sender.
Channel attachments are visible to the channel like other analysis results.

`AGENT_RETURN_SCRIPTS_DEFAULT=false` is the code default. Preferences live separately from
model-edited memory in `projects/_users/{user_key}/settings.json`, with compare-and-set updates
and replay receipts. The control plane freezes the setting at the first worker submission and
retains it for retries and continuations. False is omitted from `request.json`; it never adds
capture hooks, prompt text or attachments.

With the setting on, trusted hooks snapshot an explicitly invoked workspace script before Bash
starts, then retain that snapshot only after a successful tool result. Supported entry points
include Python, Rscript, R `-f`, Julia, shell interpreters and directly invoked script paths.
Runtime interpreter paths and quoted paths work. `cd` is tracked within simple `&&` chains.
Files merely mentioned as arguments, inline code, platform tools and `measure.py` probes are
excluded. Inputs, previews, outputs, scratch files, symlinks and nonregular files are excluded.
Captures are limited to ten scripts, each at most 1 MB of UTF-8 text, and scanned for possible
credentials. Repeated successful runs replace and reorder the captured version.

The implementation makes these refinements to the supplied plan:

- It copies before execution, so self-edits or later model edits cannot change the returned
  bytes. A script that changes during a compound command is omitted because the exact executed
  version cannot be verified.
- Pipelines, semicolon lists and `||` branches can hide failures or skip script invocations.
  Identified scripts in these commands are omitted with a short explanation. Shell variables,
  substitutions, loops and scripts invoked internally by another script are not traced.
- A workspace manifest is never trusted as an attachment list. The runner owns snapshots in
  memory; checkpoint metadata outside the model workspace records their hashes and order.
  Continuations verify the archived copies against that metadata. A new request discards the
  prior capture set. The analysis sandbox is unchanged.

Delivery appends captured scripts after regular outputs in the existing single Slack upload,
using the existing authorization checks and delivery registry. Basenames are preserved where
unique; parent prefixes and deterministic suffixes disambiguate collisions. Script bytes are
unchanged. Rejected scripts add a short note without replacing the analysis answer.
The general output extension filter and existing `outputs.publish` provenance path are unchanged.

Exact returned copies are also retained under `outputs/returned-scripts/turn-N/`, archived to
that turn's storage prefix, and entered in the thread file registry for restoration and review.
The temporary `.mica/scripts/` cache is deleted after delivery attempts and on cancellation;
continuations carry it in the existing checkpoint archive. With the setting off, explicitly
asking to attach a `.py`, `.R` or `.sh` file still has the previous extension-filter limitation.

## Verification

Delivery/provenance fixtures were originally captured from `711a15f`. The prompt, hooks and
request fixtures now reflect always-on knowledge and automatic recovery; the retired knowledge-off
variant has been removed. Script-return remains off by default. Fixtures cover replies, uploads,
thread file records, provenance and request JSON. Tests cover preferences, request freezing,
execution snapshots, limits, credentials, continuations, failures, cancellation and delivery.

A manual observation used the installed SDK **0.2.163** and bundled CLI **2.1.286** with a
loopback-only mock model API: `printf success` fired `PostToolUse`; `exit 7` fired
`PostToolUseFailure`. The sanitized observation is saved in
`tests/fixtures/return-scripts/sdk-hooks.json` and replayed in offline tests. No paid model call
or Slack message was used. The CLI probe was a local diagnostic; the test suite has no network
or GCP dependency for this feature.

Rollout uses coordinated API and worker images from one source commit. Keep
`AGENT_RETURN_SCRIPTS_DEFAULT=false`, then verify an opt-in user's analysis and a follow-up with
`scripts off` in Slack.

Deployment evidence is retained locally under `.mica/deployments/return-scripts-20261009/`.
