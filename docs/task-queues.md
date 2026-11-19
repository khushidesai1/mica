# Task queues

Ask Mica in Slack for several runs in one message, for example:

“Transfer TMA43, TMA47 and TMA49 using the usual layout. Segment each after its transfer
finishes, then make a comparison report once all three analyses finish.”

Mica can create a durable graph of up to 20 explicit tasks. Each task names its prerequisites.
Independent tasks enter the existing compute or transfer queues together and use the deployed
capacity limits. A dependent task starts only after its prerequisites complete; a transfer's
preview, approval or completed conversational handoff does not count as a completed transfer.
A failure or request for more information blocks dependent steps; unrelated tasks continue.
For analysis steps, the existing bounded outcome classifier also checks the saved task and
final reply: a worker that exits normally after reporting a scientific blocker does not release
dependent tasks. An uncertain or unavailable completion assessment needs attention before the
workflow can continue. Downstream workers still inspect their actual inputs.
Tasks never silently restart failed scientific work or replay successful steps.

The original request gets a compact progress list showing queued, running, approval and terminal
states, with prerequisite IDs. Ask for queue status without waiting for the workflow to finish.
Ask to cancel the queue, or use Stop in its conversation, to stop its pending and active tasks.
Cancellation waits for active controllers to acknowledge shutdown and retains completed outputs.
Queue controls are scoped to the requester, project and original conversation. Queue creation is
currently a Slack capability; email requests retain their existing single-run flow.

Every transfer still requires its private exact-source/destination approval. Each analysis uses
the normal compute, write and overwrite checks. A queue does not grant access or increase runtime
capacity. User constraints apply to every task; queued workers cannot create nested queues or
perform project-management actions as a side effect of an analysis. Each task has its own
checkpoint, workspace and retained output prefix. Prerequisite result summaries are passed as
evidence to downstream tasks, and normal access checks apply again before execution.

Graph state and child receipts live in the existing connector state store; existing Cloud Tasks
polls and reconciliation recover dispatch after a restart. Child IDs are deterministic within a
queue, so retries cannot create additional runs. A fresh request creates a new queue; historical
requests are not automatically expanded or replayed by deployment.

Deploy the controller and CPU/GPU worker application sources together with
`cloudbuild/build-slack-release.yaml`, retaining the verified scientific runtime bases and the
shared worker contract gate. Shared-channel queued workers use the controller's isolated retained
output prefix. Check transfer slots and jobs before rollout and wait for running transfers to
finish. No Terraform, IAM, secret or new queue is needed. Do not roll back to a controller without
task-graph support while graphs are active; finish or explicitly cancel them first.
