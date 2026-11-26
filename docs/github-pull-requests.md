# GitHub pull requests from Slack

Mica supports a personal GitHub connection in a one-to-one Slack DM. Each user connects their
own GitHub account; eligible repositories are the intersection of that user's push access and
the repositories on which the Mica GitHub App is installed. This is part of Mica. The separate
GitHub App registration supplies GitHub's consent and repository permissions; users do not need
another agent or a separate Mica installation.

## Using it

1. Send `connect github` to Mica in a DM. Open the private link within 15 minutes. Check the
   displayed Mica account, choose **Continue to GitHub**, and authorize your GitHub account.
2. Install the Mica GitHub App on selected repositories, or ask their owner to install it.
   `github status` shows your connected account and repositories with push access.
3. Ask Mica to make a PR in `owner/repo`. It pins the default branch's commit, checks out a
   filtered copy, makes bounded text edits, and reports tests it ran.
4. Review the separate Slack approval card, PR description, and downloadable exact patch.
   Only the original requester can approve. Approval creates one new `mica/<slug>-<id>` branch,
   one commit, and a draft PR. It never merges, force pushes, modifies an existing PR, or writes
   to the default branch. Ordinary repository CI can run when this new branch is pushed.
5. `disconnect github` removes local credentials and attempts to revoke the GitHub grant.
   If GitHub cannot confirm revocation, Mica tells you to revoke it in GitHub Settings too.

Reconnects and disconnects invalidate outstanding approvals. Make a new request to obtain a fresh
patch and approval; Mica preserves and reports any partial branch already created. Delivery
retries with the same connection reconcile the original branch and PR. Pending proposals expire
after one hour. Replacing a pending proposal invalidates its older buttons.

## Privacy and retention

A Slack thread becomes requester-private as soon as personal repository work is admitted. This
is sticky across follow-ups, reconnects and project switches. Mica retains its local history,
checkpoints and private artifacts for that requester, but excludes the thread from shared project
memory, history imports and artifact promotion. Shared coordination, transfers, task queues and
project publishing are disabled there. Start another Slack thread for shared project work.
Repository code is processed by Mica's existing analysis worker and configured model provider.
Disconnecting GitHub revokes future GitHub operations; it does not erase retained conversation data.

OAuth secrets and access/refresh tokens remain in the controller. Tokens and the PKCE verifier
are encrypted with the existing application key, with provider, owner and connection revision
binding. Links are single-use; OAuth state expires after ten minutes and requires the browser's
Secure, HttpOnly, SameSite=Lax binding cookie. The consent form session uses SameSite=Strict,
origin checks and CSRF protection. No token is included in the model prompt or worker manifest.
An application key change requires reconnecting GitHub; AtoMx's existing credential format,
namespace, cookies and links are unchanged.

## Checkout and patch limits

The controller downloads a pinned archive and verifies blobs against the complete Git tree.
Unchanged binary fixtures and executable files remain available for tests. Secret-like files,
links, submodules and unsafe paths are omitted and cannot be overwritten through a proposal.
The worker's editable checkout is `contrib/<owner>/<repo>/`; its read-only base is
`inputs/pull-request/<owner>/<repo>/`. Checkpoints retain edits and deletions for recovery.

Default limits are 32 MiB compressed checkout, 128 MiB expanded content, 20,000 tree entries,
50 changed files, 256 KiB per old/new file and 1 MiB total old plus new bytes. Changes must be UTF-8
text without NULs or detected secrets. Workflow files, executable edits, mode changes, symlinks,
binaries and path conflicts are rejected. Unchanged executable modes are preserved; deleting an
executable is supported. New files use mode `100644`. GitHub's
[FileAddition input](https://docs.github.com/en/graphql/reference/git#fileaddition) has no mode
field; the first version conservatively refuses executable edits.

The controller freezes exact changes, patch, identity and expected full Git tree under
`write-access/pull-requests/`, outside worker-writable storage. Each write rechecks requester,
project access, cancellation, feature/pilot settings, connection revision and repository push
access. Commits use `expectedHeadOid`; recovery verifies the full tree, parent and proposal marker.
A different branch head stops publication. Checks on the card are worker-reported, not an
independent CI guarantee.

## Operator setup

The feature defaults off. Create one public GitHub App for Mica, with metadata read, Contents
read/write, and Pull requests read/write. Do not grant Workflows, administration, organization or
account permissions. Enable expiring user access tokens. Set its callback to
`<MICA_CONTROL_PLANE_URL>/integrations/github/callback`, disable webhooks, and allow users outside
the owning organization to install it. Keep the existing read-only reference and memory App
credentials separate. PR operations never use App private keys or installation tokens.

An operator must apply the declared `github-pr-client-secret` Secret Manager container and store
its value through the established secret-management procedure. This is separate from an
application deploy. Configure:

| Setting | Value |
| --- | --- |
| `AGENT_PULL_REQUESTS` | `true` only after registration and pilot setup |
| `AGENT_PULL_REQUEST_USER_IDS` | Comma-separated `team:user` IDs for the pilot; empty admits all verified users |
| `AGENT_GITHUB_PR_CLIENT_ID` | GitHub App client ID |
| `AGENT_GITHUB_PR_CLIENT_SECRET_SECRET` | Full Secret Manager path with a numeric version |
| `AGENT_GITHUB_PR_APP_SLUG` | Registered App slug for installation links |
| `AGENT_GITHUB_TOKEN_REFRESH_MARGIN_SECONDS` | Default 300; refresh before expiry |

The existing controller identity must be able to read that secret version. Never pass the
client secret or tokens to a worker. Personal credentials use Firestore connector state; the
application's existing signing/encryption key must remain configured. See
[GitHub user tokens](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app)
and [refresh](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/refreshing-user-access-tokens).

Build API, CPU, GPU and shared workers from one source using `cloudbuild/build-slack-release.yaml`.
Promote immutable image pins together, preserving runtime configuration. Deploy disabled first.
Before enabling for everyone, the owner must complete the OAuth flow in a private test DM and
use a disposable repository to verify discovery, one approved draft PR, denied/discarded/stale
approvals, account switching, disconnect/revocation and token refresh. For a bounded refresh
check, use a near-expiry test credential once; do not leave an always-refresh threshold enabled
while publishing. Remove the disposable branch/PR and any recorded smoke artifact prefixes.
GitHub registration, secret provisioning, App installation and live user consent are rollout
requirements; unit tests or a healthy disabled revision do not establish them.
