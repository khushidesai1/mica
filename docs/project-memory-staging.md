# Project memory staging verification

Verified October 10, 2026 (America/Los_Angeles; October 11 UTC). Source commit
`503f05a01218ee6ef49955d3ba2e8f0074595d6d` is deployed at 100% traffic as
`pistachio-staging-memory-503f05a01218`. Memory uses the private repository
`khushidesai1/mica-memory`, branch `main`.

## Release and checks

- Ruff (`src tests tools`) and strict mypy passed; pytest: **702 passed, 6 skipped**.
- [GitHub CI](https://github.com/khushidesai1/mica/actions/runs/38099630839) passed.
- Cloud Build `35581e81-52db-4c14-baa1-0d8040db2c25` passed all ten steps,
  including API/worker compatibility, source and attachment checks, Linux telemetry,
  checkpoint recovery, compute policy, and synthetic CosMx/Cellpose checks.
- Preview and live health returned 200; removed UI routes returned 404; unsigned
  ingress and unauthenticated internal memory endpoints returned 401.
- Promotion occurred with no active events or jobs; the task queue was resumed.
  No Terraform, secret contents, or IAM permissions changed.

The worker base first exceeded Docker's layer limit. Flattening preserved its filesystem
and runtime metadata, and passed the scientific checks. Release overlays now remove
obsolete application modules. Two stale release checks were updated to current APIs
without removing their underlying gates.

## Live evidence

The Slack canaries posted clearly labeled bot messages and submitted corresponding signed
test events for the authorized test user. They exercised deployed routing, actual model
calls, Cloud Tasks, Slack replies, GitHub commits, and a Batch worker. These were synthetic
ingress events, not manually typed Slack messages. The plot used a synthetic 3×3 matrix.
Commit IDs below belong to the memory repository.

| Check | Observed result |
|---|---|
| Real credential and store | Write `ca1d296bc5111b23284f43ad106b8af4caae52fa` read back with identical text/OID; conflicting write `fae9112ffaa265d62eb23cd4d6c7bbfe63011aa4` preserved under `_unmerged/`; cleanup `e71beaa7ceef6326d163f010e423d306d1ee3093`. A direct stale-head mutation returned `STALE_DATA` without committing. |
| Guide import | Existing CRC guide imported once as `7f2aa086f101c880736f435f83eb088d7275ab37`; original GCS guide preserved. |
| Prompt → saved preference | Exact prompt “we always use viridis for heatmaps” produced attributed `conventions.md` in editor commit `1a5c89665d8a49a3500296f22d4f9c1fc7de425a`. Event `fc8ea774e6a1b08a566924ccf19ec7d9` completed its memory task. |
| Fresh DM | After more than two minutes, event `d1eebd30316b9f5e4f16e01bd0e3c84e` read that exact head and answered “Viridis!” with a chat reply and no worker. Final-revision event `69ef122ee89320cac1668d03f6c72fad` also recalled viridis and the saved recipe; its editor correctly made no changes. |
| Worker read/write | Event `5ea4ee52c02c56ceb567575cbd305420` pinned manifest head `1a5c89665d8a49a3500296f22d4f9c1fc7de425a`, matching the head captured before submission. The Spot worker succeeded, returned a verified PNG, and saved its recipe/index as worker commit `14833b08c71d6b31fa563bec6fc289c272910189`. Proposal base and receipt matched. |
| DM → channel | CRC editor commit `99f6b97587b0539ddb595ddcf79c809e8ef9b838` saved the temporary synthetic-plot label “Memory QA”. Both channel checks recalled it without a worker. The initial channel read followed an unrelated worker commit; CRC file OIDs were unchanged. Final channel event `8c27e1efb11903bdb728e05f71995ce0` and final DM read both used head `18f27a1971fdebec681ebbbe9b97f164a8090382`. |
| Existing thread | Event `054d38cf5a569f5b6e9aa351e8cef112` in a pre-existing DM recalled viridis. Saved workdir, SDK session, project, messages, and attachments were retained. Retired-object inventory was unchanged. |
| Compaction | Isolated `_compaction_smoke` seed `485a0aa30ca8c14d9ead09411b5c135a6b2a42c6` reached 85% of the guide limit. A real Cloud Task produced compaction commit `18f27a1971fdebec681ebbbe9b97f164a8090382`, reducing maximum utilization to 2.475%. `git log -p` showed duplicate merging, preserved attribution/date and byte-identical recipe, and a complete index. |

Temporary QA labels were removed with guarded commits `cc1261ad166eba83b445ec6959a4cd19c8d4b5bb`
and `9b63ff3eb0dd36c42a6850777e2bf229f8dca11c`; the compaction test folder was removed
by `762a1122fc3ccded5fbd71e3a19657073e6c3307`. Read-back confirmed all test folders
empty, the CRC guide unchanged, and the viridis convention and worker recipe preserved.
The DM default was restored to `breast-alchemy`. Git history was retained.

## Deployed log excerpts

Cloud Run originally suppressed successful root-logger messages. The final revision uses
a dedicated INFO memory logger. Its successful editor canary added the temporary QA label
as commit `0ba875727d07d69757af88426984530fe8b0a9cf`.
The original viridis save is evidenced by its GitHub commit and task receipt; these log
excerpts are from subsequent checks on the final revision. Times are UTC, October 11.

```text
04:04:13 memory_commit pid=p_05b107ac9f31b40b event=849e8e6956599a0c290500f8e5c351b3 source=editor commit=0ba8757 changed=1 unmerged=0 rejected=0 outcome=committed
04:07:35 memory_commit pid=_compaction_smoke event=memory-compact-_compaction_smoke-485a0aa source=compaction commit=18f27a1 changed=4 unmerged=0 rejected=0 outcome=committed
04:08:31 memory_read pid=p_05b107ac9f31b40b event=69ef122ee89320cac1668d03f6c72fad head=18f27a1 files=5 chars=4999 source=github ms=441
04:08:40 memory_commit pid=p_05b107ac9f31b40b event=69ef122ee89320cac1668d03f6c72fad source=editor commit=none changed=0 unmerged=0 rejected=0 outcome=nothing
04:10:13 memory_read pid=pista-cosmx-6k-crc event=8c27e1efb11903bdb728e05f71995ce0 head=18f27a1 files=2 chars=2117 source=github ms=623
```

## Limits of this verification

One additional attribution request (event `d3a1e6426f9c83e8ebd5056dacd8d5d6`) failed
in routing before the editor ran: “Couldn't determine the next step. Please retry or
clarify your request.” That phrasing is **not verified**; its failure was not counted
as a successful memory test. The required preference, recall, worker, sharing, continuity,
and compaction scenarios passed. These results do not establish reliability for every prompt.

Live testing also found that `claude-opus-5-5` rejects forced tool choice. Only the
memory editor/compactor use the compatible `claude-opus-5`; routing and workers retain
their existing model. Memory remains shared across authorized users of a project;
repository readers can see its contents. See [operations](project-memory.md) for limits,
administrative correction, disabling writes, and rollback.
