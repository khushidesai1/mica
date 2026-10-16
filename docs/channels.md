# Channel setup

## Headless service

Slack is the conversational interface. The browser application and browser OAuth sessions have
been removed. The task API accepts Google OIDC bearer tokens (or IAP identity when configured),
then applies the email allowlist and project ACL. Cloud Tasks, Batch, and Gmail service-account
authentication remain available. Local development can still use development identity.

For the Claude Slack backend, follow [Slack setup](slack-agent-setup.md), including channel
admission, private projects, shared briefs, user memory, and Batch execution. The next section
documents the retained shared task-service Slack backend.

## Shared task-service Slack backend

Create a Slack app with `chat:write`, `im:history`, and `im:read`; enable Events API `message.im` and
point it to `/integrations/slack/events`. Configure the signing secret and bot token. Map Slack user
IDs to canonical allowlisted emails using `MICA_SLACK_USER_MAP`, for example
`{"U123":"researcher@example.org"}`. For installations receiving events from more than one
workspace, prefer workspace-qualified keys such as
`{"T123:U123":"researcher@example.org"}`; unqualified user-ID keys remain supported.

A DM has no project until the user sends `project: project-slug`. The binding expires in 14 days.
When no project is bound, the bot also presents signed interactive controls to select an authorized
project or create one in a modal. Configure Slack Interactivity with
`/integrations/slack/actions` as the Request URL. Every acknowledgement states the active project.
Binding a project also makes that DM the user's preferred Slack delivery route for the project.
Tasks started through the API can therefore post progress, approvals, failures, and results to the
same DM. Each task gets its own Slack thread when its first update is delivered.

The task service owns a persisted `conversation` transcript. It selects public updates and
renders each assistant turn once before delivery. Slack posts the saved text as separate replies
with native controls; API clients can read the same transcript. API-originated user follow-ups
are mirrored to Slack, while Slack-originated messages are already present there. Acknowledgements,
settings decisions, replans, and results use this same path, including for historical web tasks.

Routine updates are selected centrally rather than emitted for every internal stage. An eligible
phase change can produce an update after 60 seconds; continued work within one phase can report
the latest milestone after 120 seconds. Identical progress text is suppressed. Execution starts,
new execution attempts, approvals, input requests, follow-up acknowledgements, revised plans,
and terminal outcomes bypass that cadence. These intervals are event-driven, not timer-generated
filler: a quiet job does not invent progress. The detailed execution log still retains the latest
500 milestones, including sample labels and completed/total counts, in the task's technical data.

Each turn has a stable identity and event key. Storage preserves already-published text when an
older worker writes a task. Slack checkpoints delivered message IDs individually and uses a
repository lease to serialize delivery; retries drain the undelivered transcript in order rather
than regenerate it. A crash between Slack accepting a post and checkpointing its ID can still
repeat that post; delivery is not exactly-once. Existing tasks default to an empty transcript and
start recording at their next event; historical messages are not reconstructed or resent.

Each submitted task uses the originating message as its Slack thread. API-originated and historical web tasks
can also be continued from their mirrored Slack thread. Correlation combines the task ID,
workspace/channel, thread timestamp, and approval ID. Substantive model-written replies use short
paragraphs and preserve the supported outcome, limitations, and next step without inventing
scientific claims. The integration handles DMs and their threads, not shared-channel mentions.

Scientific and hyperparameter approvals open in Slack modals, where users can accept proposed
values, choose an option, or supply a custom value. Large forms continue in pages of 40 settings;
all pages must be reviewed before the final confirmation records approval. Draft reviews expire
after one hour and cannot be reused after an approval changes. Other approval types use a native
review modal with an explicit approve/decline choice and decision details. Compute approvals retain
the **Approve resource usage** button and confirmation dialog. No approval links to the old website.
Modal pagination uses Slack's [view submission response](https://docs.slack.dev/surfaces/modals/#update-a-view-via-response_action).

While planning is still mutable, replies in the task thread are added as newer user context and
restart planning from the updated request. Once immutable execution has started, Slack asks the
user to cancel and send a revised request instead of silently changing a running analysis. Users
can cancel with the task's **Cancel task** button or by replying `cancel` or `stop` in the thread.

The authenticated API retains project/task history, memory management, attachments, approvals,
context updates, cancellation, and artifact downloads. The `source` field remains provenance,
including the legacy `web` value, so saved tasks and execution schemas stay compatible.

## Gmail

Grant the runtime identity the required delegated Gmail scopes and publish Gmail watch events to the
provisioned topic. Its push subscription uses a Google OIDC token from the dedicated control-plane
service account. Scheduler renews the watch daily. New subjects must contain `[project:slug]`;
replies reuse the stored thread binding. Unresolved threads receive one-hour signed project links.

Channel attachments are limited to 100 MB. They are streamed into quarantine, checked for archive
path traversal/member and expansion limits, checksummed, and only then promoted. Use the authenticated upload API or versioned `gs://` URI for larger data.
