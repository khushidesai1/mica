# Project backchannel

Mica's Claude conversation and analysis workers can use `project_backchannel` to exchange
scientific progress across threads in the same project. `AGENT_BACKCHANNEL` defaults to true.
An investigating thread can post a question or recommendation while another thread is working;
that worker receives updates at its next tool boundary and can answer through the same tool.

Use a common topic, such as `segmentation-recipe`, for related work. Messages can be updates,
questions, answers, blockers or recipes. Answers reference the original message ID. Recipe notes
should contain the reproducible method: software versions, inputs/priors, parameters, seeds,
evidence and any immutable source reference. Consumers record the chosen message ID and actual
settings in their methods. Different recommendations remain visible. Mica is guided to ask the
investigating thread and defer dependent work when required settings are unresolved, rather
than silently picking a default. This is advisory coordination, not a scientific dependency
scheduler or an execution lock for arbitrary scripts.

The application assigns project, source thread, event, worker attempt, timestamp and sequence.
Exact publication retries deduplicate. Notes are visible only through current project access;
worker requests also require an active compatible attempt. Cancellation and access revocation
use the existing worker authorization and lease. Agent messages do not grant compute, transfer,
bucket-write or project permissions and never become human amendments.

Only deliberately authored project scientific messages enter this board. It does not copy raw
DMs, email, private links, personal details or connector inventories. The tool explicitly tells
agents about project-wide visibility and rejects credential-like text. Verified transfers add
only the sample identifier, verified file count and byte count. Existing GitHub project memory
has its own sharing policy: it already shares attributed DM/email paraphrases with project
members, as described in [project memory](project-memory.md#shared-thread-memory). This feature
does not make that older memory private or delete its history.

Workers check when a turn starts and at tool boundaries, at most once per ten seconds except
the initial turn refresh. If an update is first observed immediately before Bash or GPU execution,
the proposed command is returned to the agent for reconsideration before it runs. A command
already executing continues with its original settings. Long multi-sample scripts should use
safe stages to benefit from updates between samples. No background model polling or independent
agent turns start merely because a note arrives. Idle threads read updates on their next turn.
Workers already launched from older images cannot gain the feature retroactively. Delivery to
the model is not evidence that it adopted a recipe or completed a requested action.

The existing GCS application store holds one generation-checked board per stable project ID.
It keeps 128 recent ordinary notes and 64 recipe recommendations, with 2,000 UTF-8 bytes per
ordinary note and 8,000 per recipe. Recipes are not silently evicted; reaching their limit
requires operator archival before another can be added. The last 2,048 publication receipts
deduplicate retries. Reads include at most 12 messages and 24,000 serialized message bytes,
plus a bounded recipe-ID catalog. Read `after=0` to page retained history, `after=<cursor>` to
continue, or `message_id` to retrieve one full retained message. Initial context shows recent
notes and recipe references; omission and retention gaps are explicit. An unavailable store is
reported as unavailable, never as proof that a shared method is settled.

Deploy API and CPU/GPU worker application sources together using the coordinated release
pipeline. The worker manifest enables the tool only for newly routed compatible requests;
existing worker images, scientific parameters and running commands remain pinned. Disabling
`AGENT_BACKCHANNEL` removes conversation tools and rejects worker broker calls. Backchannel
failure does not cancel an already-running command or authorize a replacement job.
