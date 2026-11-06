# Cloud access requests

Mica can request operator-configured Google Cloud roles for a named email address. This is
separate from project membership and analysis admission. It does not enable analysis in the
request channel or grant access to Mica projects.

Cloud requests and approval replies do not inherit the sender's default Mica project or enter
its conversation history or memory. Email approval notes use the requester's message, and replies
go only to the verified sender. An access bundle can target a Google Cloud project with no
corresponding Mica project; the operator must explicitly configure that bundle and its IAM
permissions. The initial VM bundle targets `azizilab-aml`.

`AGENT_CLOUD_ACCESS` is a JSON object keyed by bundle name. An empty object (the default)
disables intake and hides the conversation tool. Each bundle provides `title`, `gcp_project`,
`roles`, `channel` (`T…:C…`), `approver` (`T…:U…`, defaults to
`AGENT_ACCESS_REQUEST_OWNER_ID`), `approver_emails`, `allow_lab_members`, `guide_url`, and
`welcome`. The welcome text can include `{guide_url}`. Channels must be distinct and share the
approver's workspace. Email addresses are normalized; roles must be predefined roles, with at
most ten roles per bundle. Owner, IAM and Resource Manager administration roles are rejected.
The conversation model chooses only a bundle, email, and short note; it cannot select a
project, roles, approver, or welcome text.

## User flow

Invite Mica to the configured request channel. Do not run `enable this channel`: that command
is for analysis admission. A top-level message or mention gets one request button. Thread
replies are ignored. The form pre-fills the clicker's workspace email and requires a note of
1–500 characters; the target may be another person. Slack DM requests point to this form.

A target in the live lab membership list is preapproved only when `allow_lab_members` is true.
Otherwise Mica posts a card in the request thread and tags the configured approver. Only that
person can decide from the original card. Requests expire after seven days. If both roles
already exist without conditions, Mica reports that fact without a grant or welcome email.

Email requesters can name themselves or an address appearing in their message (including a
forwarded body). Mica asks for the address if the model supplies an unmentioned target. Pending
email requests send the card to the approver's DM and a separate approval email to each
configured approver address. The requester gets the normal initial response, then a separate
outcome when decided. A verified approver reply must be in that exact approval email thread,
with `approve` or `deny` as its first nonempty top-posted line. Quoted commands, different
threads, and other senders are not approvals. An approver's own email request still needs this
explicit decision, even when the target is a lab member.

Cloud access is the sole email IAM-approval exception. Project-data writes, transfers, project
membership changes and other administrative approvals continue to require Slack.

## Grant and delivery guarantees

`AGENT_ACCESS_SERVICE_ACCOUNT` names the dedicated access service account. Only the API control
plane impersonates it. The worker identities receive no new grant permissions. The grant client
reads IAM policy version 3 and uses its etag; it adds `user:<email>` to missing unconditional
role bindings while preserving all existing members and conditions. Conflicts have bounded
retries. Missing Google accounts, domain restrictions and insufficient permissions produce
clear failures.

Requests snapshot their bundle and reject decisions after configuration changes. Durable
request leases serialize decisions, and a target reservation prevents duplicate pending
requests. The approval timer also resumes interrupted grants and delivery. Add-only grants
can be retried after an uncertain write. Each successful request sends one welcome email to
the grantee. Control notices share the existing conditional-write Gmail outbox: an uncertain
send is reconciled against sent content when possible, otherwise held for operator review.
It is never blindly resent. An approval card flags uncertain approval or welcome email.

Request state is stored under `cloud-access:<id>` in connector state, including target,
requester, bundle, decision actor/time, grant outcome and delivery receipts. Gmail notice
intents live under `projects/_gmail/outbox/notices/`. Cloud Audit Logs record the access
identity's policy writes. Do not delete an unconfirmed outbox intent to force a resend.

## Operator activation

An API-only release can ship with `AGENT_CLOUD_ACCESS` empty. Use
`cloudbuild/build-slack-api.yaml`, verify the existing worker contract, and preserve the live
worker image pins and all other environment settings.

The `infra/mica-staging` root adds:

1. `mica-staging-access@azizilab-aml.iam.gserviceaccount.com`.
2. Token Creator for the existing control identity on that account.
3. Conditional Project IAM Admin for the access account. The condition uses
   `modifiedGrantsByRole.hasOnly(access_grant_roles)` to limit changed roles.
4. The access service-account environment setting on Cloud Run.

For the VM bundle, the operator sets `access_grant_roles` to
`["roles/editor", "roles/iap.tunnelResourceAccessor"]`, and sets
`runtime_environment.AGENT_CLOUD_ACCESS` to the reviewed VM bundle JSON. These IAM changes
require an operator Terraform apply; a normal application deploy does not apply them.
The condition limits **roles, not recipients**: compromise of the control plane could grant
Editor to an outside account. It also does not enforce add-only behavior; Mica's grant client
enforces that. Review this permission boundary before applying.

Use the existing private backend and complete current operator tfvars described in
[the state runbook](mica-resource-migration.md#state-backend-preparation). Preserve the current
API digest, worker pins, memory settings, secrets and migration stage. Prepare and review a
saved plan against live state. Expect the three new resources above and a Cloud Run update
adding only the access settings. Stop if the plan deletes, replaces, or changes anything else.
Back up the existing state and apply that exact reviewed saved plan, without `-auto-approve`.
Do not run a fresh empty-root apply or use the old `infra` root for this change. The private
rollout handoff contains the concrete local plan path and command.

If Cloud Run traffic is pinned, applying configuration may create a ready revision without
serving it. Verify that revision's settings, preview its health/auth behavior, then explicitly
move traffic to it using the normal release process before testing.

Recipients are assumed to have access to the setup guide; this flow does not change its
sharing. Mica is already a member of #vm-access in the initial AziziLab configuration.

## Guided live acceptance

The initial mailbox is **mica@azizilab.com**, the approver is
**khushi.desai@columbia.edu**, and the temporary test account is
**khushi.desai1115@gmail.com**. Keep screenshots/permalinks, message receipts, and before/after
IAM policies in the private release evidence. The operator performs the following actions;
the implementation assistant can prepare commands and verify read-only evidence between them.

1. Check that the test account is not a lab member and has neither configured role. If it
   already has real access, stop and select another authorized test identity.
2. Test the role ceiling by attempting `roles/viewer` through the access service account.
   Expect `PERMISSION_DENIED` on `setIamPolicy`. A token-creation failure is inconclusive;
   it does not prove the role ceiling. If this unexpectedly succeeds, stop activation and
   remove that exact test binding with the operator's own credentials.
3. In **#vm-access**, post `VM access test`. Click **Request VM access**, enter
   `khushi.desai1115@gmail.com` and note `Temporary rollout check — deny this request`.
   Click **Deny** on the resulting approval card. Expect a declined result and unchanged IAM.
4. From **khushi.desai1115@gmail.com**, send a new email to **mica@azizilab.com**:
   `Please give VM access to khushi.desai1115@gmail.com for a temporary rollout check.`
   In the approval email received at **khushi.desai@columbia.edu**, reply with `approve` on
   the first line. Expect both roles and one welcome email to the test account, with the
   setup link and “Start at Step 2.” The DM card must show the outcome. A mapped organization
   sharing-policy refusal is also a recorded result; it means Gmail targets cannot be granted.
5. Test an existing lab member who already has both roles. Confirm the address against the
   live lab list and policy first. Expect “already has access”, no grant, and no welcome.
6. Remove only the temporary roles actually added for the test. Do not remove preexisting
   bindings or real lab-member grants. For successful VM approval, the exact commands are:

   ```bash
   gcloud projects remove-iam-policy-binding azizilab-aml \
     --member='user:khushi.desai1115@gmail.com' \
     --role='roles/editor' --condition=None
   gcloud projects remove-iam-policy-binding azizilab-aml \
     --member='user:khushi.desai1115@gmail.com' \
     --role='roles/iap.tunnelResourceAccessor' --condition=None
   ```

Verify both roles are absent afterwards. If the ceiling probe unexpectedly added Viewer, also
remove that exact binding with `--role='roles/viewer' --condition=None`. Do not delete the real
request audit or email outbox records. No GCS smoke objects are needed for this workflow.
