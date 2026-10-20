# Natural-language requests

Mica accepts ordinary requests in Slack and email, alongside the existing exact commands.
The application checks identity and interprets account/control intent before loading project
memory or starting computation. The model proposes an operation; application code resolves
the project, checks access, and returns the actual result.

Examples:

| Request | Behavior |
| --- | --- |
| “What projects am I collaborating on?” | Lists projects the verified user can access, even without a current project. |
| “Find projects named atlas” | Searches the user's visible projects. Lists show at most 50 matches and explicitly report truncation. |
| “Use atlas” / “the second one” after a list | Selects an authorized project for new conversations. Existing conversations keep their project. |
| “Create a project named atlas at gs://lab-data/atlas/” | Uses the existing setup flow in a private, new conversation. Requires a user-supplied data URI and normal bucket admission. |
| “Set atlas's default data folder to gs://lab-data/atlas/processed” | Changes the actual registry setting after owner confirmation in private Slack; email gives a Slack handoff. Exact syntax: `data-root atlas gs://lab-data/atlas/processed`. |
| “Show the project guide” / “show saved project memory” | Reads the authorized project's guide or memory without a compute worker. |
| “Include the scripts” / “stop attaching scripts” | Changes the requester's script-attachment preference. |
| “Stop this analysis” | Cancels the earlier queued/running work in the same authorized conversation, including email. |
| “Share atlas with person@example.org” | Previews an exact change with Confirm/Cancel in private Slack. |
| “Rename atlas to atlas-v2” | Requires private Slack confirmation. Both typed and natural email rename requests direct the user to Slack. |
| “Revoke write access to atlas” | An authorized write administrator can preview and confirm without waiting for running computation. Later analysis still waits for that computation. |

Changing a default data location validates the destination against the same bucket admission
as project creation. It updates the registry, not just the brief, and affects every member.
Existing conversations adopt the current location on their next request and reset the worker
session while retaining conversation history and work products. Running requests with the old
access signature can stop; retry them after the change. No files move and no write grant is
created or expanded. The registry location takes precedence over older paths in project memory.

Project access is shared across interfaces: owners can share with an explicit email address,
even if the recipient has no Slack account or enabled channel. Only authenticated owners may
change membership. Natural sharing still requires the existing private Slack confirmation;
explicit `share <project> <email>` and `unshare` retain their existing owner-only behavior.
The recipient must send a verified email from the shared address, list their projects, and
select one. Membership does not send an invitation or grant Slack admission. No Slack channel
publication is required for owners or members to use a project over email. Publishing remains
a separate permission to expose project replies to a Slack channel's audience.
Unknown Slack mentions return a helpful error; recipient lookup outages end with a clear
no-change response instead of retrying indefinitely. Requester authentication still fails closed.

Project creation asks for missing details; it never invents a bucket or substitutes a parent
folder. Clarifications and numbered project choices last ten minutes and are reauthorized.
Natural write grants require an explicit scope and duration. Natural requests cannot add
`--force`, create unlimited grants, or bypass an existing setup or write-access check.

Account lists, project selection, preferences, and account clarifications are sender-only
over email. They are unavailable in shared Slack channels. Project answers and artifacts retain
the existing project-member recipient filtering. Revoked access suppresses project output,
including when a response happens to be sender-only. Account visibility is checked again
immediately before delivery; a changed project list is regenerated from current access.

Administrative confirmations bind the requester, workspace, original message, immutable project
ID, recipient/channel, scope, duration, and current project settings. They expire after ten
minutes. Duplicate clicks use existing durable operation receipts, and interrupted memory
synchronization or notification can resume without repeating the change.

AtoMx secure connection entry and transfers remain a separate capability. This release does
not enable them or deploy a transfer worker. Mica gives an availability explanation rather than
promising a connection link. Never send credentials in Slack or email.

Email replies retain the saved conversation's worker session key across messages. Signatures
and quoted reply history are removed during email intake; the resulting original request text
is retained for auditing and is never replaced by a synthetic command.

## Release checks

The command-intent prompt and schema are included in the routing snapshot. Offline tests cover
account admission, exact-command compatibility, email audience changes, cancellation ordering,
confirmation replay/failure, and project creation. The control-plane uses the existing bounded
router client, and classification plus project routing share the same per-request time budget.
Existing worker request/result formats remain compatible; the email thread key is supplied
through the existing `thread_ts` field.

Deploy through the API release pipeline after the required lint, type, full test and worker
compatibility gates. Keep transfer images and infrastructure unchanged. For acceptance, use
user-initiated private Slack/email messages to list projects, select or create a project,
request an analysis, follow up, and cancel. Confirm the final email in Sent and the user's
mailbox; a Gmail acceptance receipt alone does not establish inbox delivery.
