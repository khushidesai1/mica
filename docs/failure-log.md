# Failure log

Mica records detected operational failures in Firestore and publishes one Slack summary per incident.
This is an incident index: Cloud Logging, task state and worker artifacts remain the detailed evidence.
It does not change job execution, request retries, or users' ability to resend requests.

## Setup

Create a private operator channel, invite Mica, and configure `AGENT_FAILURE_CHANNEL` with its channel
ID (`C…` or `G…`). Mica needs its existing `chat:write` permission. An empty setting disables posting,
not failure recording. No new credentials, scheduler, queue, IAM grant or Terraform apply is needed.
The channel must be limited to operators allowed to see project identities across the workspace.

Request summaries include fixed explanations, project, verified requester, interface, report count,
first/last occurrence, and request/incident IDs. When Slack can supply a permalink, an “Open request
thread” link points to the original thread; opening it still requires the viewer's existing Slack
access. They exclude request/response content, exception text, filenames, credentials and private
approval/action links. Background work does not invent an originating user.

Each request has one incident across failure phases, routing context changes, and long-running work.
Distinct requests do not collapse into a single project-level failure. The incident retains every
fixed failure cause; a platform report cannot overwrite the more specific application diagnosis.
The report count counts distinct observations (including both application and platform evidence),
not distinct failed requests. Background and uncorrelated infrastructure incidents still aggregate
by cause and scope.

Request processing saves the authorized working project before its first acknowledgement or command
reply. Existing thread context takes precedence over a user's default for new threads. An authorized
explicit command target (such as `use` or `owners`) replaces that diagnostic context; newly created or
routed project context takes precedence for the resulting work. Account replies retain their private
scope. Retries do not substitute a later default project, and missing or denied access never invents
one. These diagnostic fields grant no access and are not consulted for routing.

Slack responses, including command help and project lists, use bounded parts in the original thread.
Each confirmed part is saved before continuing, so retries resume unfinished delivery without repeating
confirmed parts. The full response is only marked delivered after every part succeeds; email continues
to receive its single final response.

The existing five-minute `reconcile-batch` schedule delivers at most five due incident messages per
invocation, independently of the shared task reconciler. Expect up to five minutes of latency in normal
operation; backlog, rate limiting or outages can add delay. Repeated occurrences update the same
message. For background/infrastructure incidents, 24 hours without a new occurrence starts a new
episode/message. A request keeps its incident even across longer pauses. Active request incidents
are rechecked on later maintenance runs; unchanged status produces no Slack edit. Completion or
failure updates the same alert. A successful request can confirm recovery of earlier processing
errors, but does not establish that memory editing, delivery, reference loading, or a handed-off
transfer recovered; those remain explicitly unconfirmed. Silence and retention never mark an
incident resolved. Operators can discuss a failure in its thread;
replies and reactions do not change application state.

## Coverage

The control plane reports exhausted routing, failed computation, worker control API failures,
access-verification/storage failures, memory editing and compaction, email and Slack response delivery,
and AtoMx inventory, transfer, authentication and private delivery failures. Expected input rejection,
access revocation, cancellation, resizing and successfully recovered Spot preemption are not alarms.
One failed attempt and a replay of its observation count once. A fresh failed attempt counts again.
For response delivery, the occurrence is the affected message stage; repeated attempts to deliver that
same response remain one incident occurrence.

Optional Cloud Run log intake below covers platform/request failures. Arbitrary worker stderr and
every exception in the older shared task pipeline remain outside this index. Application failure recording
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

## Optional Cloud Run infrastructure coverage

The authenticated `/api/v1/internal/maintenance/log-failure` Pub/Sub receiver accepts Cloud Run
request-log HTTP 5xx (including 503) and platform `varlog/system` ERROR or higher entries. It checks
the Cloud project and the receiver's own `K_SERVICE`; other services and log streams are ignored.
Request status alone never proves that application code did or did not record an error. For the
internal Slack/email request-processing route only, the receiver validates the opaque 32-character
request key and looks up its saved receipt. Authorized project/requester metadata comes from that
receipt, never from a log payload or arbitrary path segment. Correlated observations use the same
request incident as application reporting, in either arrival order. A receipt lookup outage retries
Pub/Sub intake; a missing receipt remains an uncorrelated service health alert. Other endpoints and
platform errors remain “Mica infrastructure health alert”, explicitly stating that no specific user
request has been linked. A startup-candidate platform error is therefore not described as a failed
user request. Raw URLs, queries, exception text and payloads never enter Firestore or Slack.

Existing sent incidents are retained as history; deployment does not repost or rewrite historical
alerts. Existing occurrence receipts still deduplicate replays. New correlated reports stop feeding
the old service-wide aggregate; genuinely uncorrelated health issues retain its ordinary delivery
identity and coverage. No incident migration, new index or infrastructure change is required.

The sink and receiver both exclude incident intake and the two maintenance delivery routes. All
container stdout/stderr is excluded, so inline reporter warnings cannot feed back into the log sink.
This intentionally excludes maintenance request failures too; operators inspect those in Cloud Logging.
A system ERROR is reported as a platform error, not classified from arbitrary text as OOM or startup.
Cloud Tasks dispatch failures and Batch infrastructure logs are not claimed as covered by this sink.

Durable intake uses the existing transactional occurrence receipts, keyed by logName and insertId.
If insertId is absent, the stable Pub/Sub messageId is the fallback; independently published copies
without insertId cannot be deduplicated. Receipt writes must succeed before acknowledgment. Storage
failure returns 503 for Pub/Sub retry; malformed, oversized (512 KB), and out-of-scope envelopes are
acknowledged as ignored. This intake sends no Slack messages itself. Existing maintenance delivers
summaries and expires both incidents and receipts. Times/counts describe ingestion, not proof of job
failure or recovery; silence never turns an incident green.

### Operator activation

Application deployment alone does not enable the sink. `infra/failure-logging` is a separate,
additive Terraform stack with four resources: a Pub/Sub topic, logging sink, sink-writer topic IAM
binding and authenticated push subscription. It consumes the existing service URL and control
service account and never manages either Cloud Run service or the infrastructure migration state.
The existing Pub/Sub service agent must already be able to mint tokens for that account, as it does
for the Gmail/completion subscriptions. Use administrator-only Terraform state, outside runtime
bucket access. Do not run the old root infrastructure stack to activate this addition.

Copy its example variables to private operator inputs, fill the current control service account,
and run `terraform init`, `terraform plan -out=<private-plan>` in that stack. Review exactly four
creates and no changes to existing resources before the separately authorized apply. The subscription
retains unacknowledged messages for seven days with retry backoff; it cannot notify Slack while this
same API or Firestore is unavailable. Cloud Logging remains the independent investigation path.
The Pub/Sub topic temporarily carries full exported log entries under restricted GCP access; only
the bounded metadata described above reaches the incident index. Do not grant user/project workers
read access to this topic.

After apply, verify sink publisher IAM, push OIDC audience/principal, and subscription backlog.
Offline fixture tests cover failure injection; never force an error in a colleague's request.
Use a separately authorized labeled integration event for end-to-end wiring, then remove its exact
incident/receipt/message artifacts. No live smoke data is created by a routine API deployment.
At service migration, provision this additive stack for the replacement API and review retirement
of the old sink/subscription only after its backlog is drained.

Cloud Run's [logging documentation](https://docs.cloud.google.com/run/docs/logging) distinguishes
request, platform and container streams; [log routing documentation](https://docs.cloud.google.com/logging/docs/export/configure_export_v2)
describes the sink publisher permission.
