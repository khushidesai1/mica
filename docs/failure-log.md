# Failure log

Mica records detected operational failures in Firestore and publishes one Slack summary per incident.
This is an incident index: Cloud Logging, task state and worker artifacts remain the detailed evidence.
It does not change job execution, request retries, or users' ability to resend requests.

## Setup

Create a private operator channel, invite Mica, and configure `AGENT_FAILURE_CHANNEL` with its channel
ID (`C…` or `G…`). Mica needs its existing `chat:write` permission. An empty setting disables posting,
not failure recording. No new credentials, scheduler, queue, IAM grant or Terraform apply is needed.
The channel must be limited to operators allowed to see project identities across the workspace.

Summaries include a fixed explanation, project, latest verified requester, interface, count,
first/last occurrence, and event/incident IDs. They exclude request/response content, exception text,
filenames, credentials and private action links. Background work does not invent an originating user.
A summary with several occurrences shows the latest requester, not everyone affected.

The existing five-minute `reconcile-batch` schedule delivers at most five due incident messages per
invocation, independently of the shared task reconciler. Expect up to five minutes of latency in normal
operation; backlog, rate limiting or outages can add delay. Repeated occurrences update the same
message. After 24 hours without a new occurrence, the next failure starts a new episode/message.
Silence and retention never mark an incident resolved. Operators can discuss a failure in its thread;
replies and reactions do not change application state.

## Coverage

The control plane reports exhausted routing, failed computation, worker control API failures,
access-verification/storage failures, memory editing and compaction, email and Slack response delivery,
and AtoMx inventory, transfer, authentication and private delivery failures. Expected input rejection,
access revocation, cancellation, resizing and successfully recovered Spot preemption are not alarms.
One failed attempt and a replay of its observation count once. A fresh failed attempt counts again.
For response delivery, the occurrence is the affected message stage; repeated attempts to deliver that
same response remain one incident occurrence.

This does not capture infrastructure failures before application handlers run, arbitrary worker stderr,
or every exception in the older shared task pipeline. Those remain in Cloud Logging. Failure recording
has a three-second budget and cannot change the originating request's outcome. If Firestore is
unavailable, a sanitized `failure_reporter persistence_unavailable` warning remains in Cloud Logging;
there is no claim that the incident index survives an outage of its own store without gaps.

## Delivery and investigation

- `failures/<incident-id>` stores the episode and delivery state, Slack channel/timestamp, latest
  observation, count, and delivered count. `failure_groups` selects the current episode; separate
  `failure_occurrences` receipts prevent redelivery from inflating counts. Writes/counts are atomic.
- A leased sender saves intent before calling Slack. Explicit rejections (including rate limits)
  remain pending and retry with backoff. Updates to a known message timestamp can safely retry.
- A timeout/crash after a first post may mean Slack accepted it. The record becomes `unconfirmed`;
  Mica never blindly posts it again. Once per hour, it uses existing optional channel-history access
  to search a bounded send-time window for the same message ID posted by Mica. A confirmed match
  restores the timestamp and allows ordinary count updates. No match, missing history permission,
  or an inconclusive page leaves the record held and logs `delivery_unconfirmed` with the incident ID.
  Mica currently has history access in its failures channel; no additional Slack scopes are required.
- For a held incident, inspect the saved channel/send time and Slack message before any manual repair.
  Do not clear the send intent merely because a search found nothing. The record remains available
  for investigation even while its Slack delivery is uncertain.
- Use the event ID to inspect `connector_state/claude-event:<event-id>` and the corresponding Cloud
  Logging entries. Email `unconfirmed` incidents additionally require inspecting the existing GCS
  email outbox; an alert does not authorize resending an ambiguous email.
- Reporter failures are tagged `failure_reporter` and never recursively create incidents. Any future
  logging sink must exclude that component, not just one HTTP endpoint.

Daily `compact-idle` maintenance deletes expired incident history, pointers and occurrence receipts
in bounded batches after 30 days of inactivity. Active episodes remain. A delayed duplicate beyond
receipt retention can count anew. Cleanup never changes jobs, project data or credentials. All queries
use ordinary single-field indexes; no custom Firestore index or TTL policy is required.
