# Browser project memory and context

The Claude Slack agent has a separate private user-memory layer shared across that user’s
accessible projects. See [Slack identity, sharing, and memory](slack-agent-setup.md#identity-projects-and-private-memory).
The browser model described below remains project-scoped; it does not read Slack memory.

Projects are the only cross-task and cross-channel memory boundary. There is no global scientific
memory. Raw session history remains in the audit store while only the two latest other-task
exchanges are eligible for injection as advisory context.

Within an active task, follow-ups retain a separate protected `PlanHandoff`: the previous master
plan, effective decision set, contract, pending question, the last twelve conversation messages,
and all replies since that baseline. This is durable task-local evidence, not retrieved project
memory. A short confirmation is interpreted against the exact question it answers. Planner-v2
follow-ups produce sparse edits; omitted components and approved values survive unchanged.
Amendment provenance cites stored message IDs; the service retrieves the source text rather than
requiring a model to reproduce a quotation. Explicit choices, accepted proposals, delegated
choices, and routine defaults remain distinct. Delegation is scoped to the named setting and
does not mark the agent's chosen value as user-specified. The conversation layer accepts free-form
input and handles structured conversion internally. Internal evidence/schema failures receive a
bounded repair attempt and remain developer diagnostics, not requests for user formatting work.
The handoff has a provenance/hash entry in the context manifest and is not trimmed to fit advisory
memory. If protected context itself exceeds the budget, planning stops rather than losing consent.
See [the continuity investigation](followup-plan-continuity.md).

`MemoryItem` supports `active`, `candidate`, `superseded`, `pinned`, and `inactive`. Explicit user
facts/corrections, verified dataset metadata, approved exact tool versions, task summaries, artifact
pointers, approvals, and requested follow-ups may be promoted automatically. Inferences and
scientific conclusions stay candidates. Secrets, credentials, PHI, raw datasets, chain-of-thought,
and debugging chatter are rejected by policy and must not be promoted.

Retrieval filters by project and active state before scoring semantic similarity, exact entity/tag
overlap, and recency. The top eight items share an 8,000-token budget. The total context is capped at
40,000 estimated tokens; the lowest-ranked memory is removed first, then older dialogue. Every run
writes `context_manifest.json` with IDs, versions, hashes, and provenance.

Five completed tasks or roughly 60,000 uncompacted input tokens schedules asynchronous compaction.
Fourteen inactive days force a summary and clear active SDK sessions; durable memory stays
retrievable. Automatic unpinned memory becomes inactive after 90 days. Deleting a memory means the
next context build cannot retrieve it; completed task manifests remain immutable audit evidence.
